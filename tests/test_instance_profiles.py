import json
import tempfile
import unittest
from pathlib import Path

from instance_profiles import (
    create_instance_profile, delete_instance_profile, list_instance_profiles,
    rename_instance_profile, update_instance_profile_loader, set_instance_profile_loader_profile,
    resolve_instance_mods_directory, resolve_instance_resource_directories,
)


class InstanceProfileTests(unittest.TestCase):
    def test_create_and_list_isolated_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            first = create_instance_profile(root, "PvP Pack", "1.21.8", "fabric")
            second = create_instance_profile(root, "Survival", "1.20.1", "forge")
            self.assertNotEqual(first["id"], second["id"])
            self.assertNotEqual(first["game_directory_path"], second["game_directory_path"])
            listed = list_instance_profiles(root)
            self.assertEqual([item["name"] for item in listed], ["PvP Pack", "Survival"])
            self.assertTrue(Path(first["game_directory_path"]).is_dir())

    def test_normalizes_name_and_rejects_invalid_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = create_instance_profile(Path(temp) / "profiles", "  My   Pack ", "1.21")
            self.assertEqual(profile["name"], "My Pack")
            with self.assertRaises(ValueError):
                create_instance_profile(Path(temp) / "profiles", "../escape", "1.21")
            with self.assertRaises(ValueError):
                create_instance_profile(Path(temp) / "profiles", "Bad Version", "latest")
            with self.assertRaises(ValueError):
                create_instance_profile(Path(temp) / "profiles", "Bad Loader", "1.21", "unknown")

    def test_rename_preserves_profile_id_and_game_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            profile = create_instance_profile(root, "Before", "1.21.8", "fabric")
            game = Path(profile["game_directory_path"])
            world = game / "saves" / "world.txt"
            world.parent.mkdir()
            world.write_text("world", encoding="utf-8")

            renamed = rename_instance_profile(root, profile["id"], "  After   Rename  ")

            self.assertEqual(renamed["id"], profile["id"])
            self.assertEqual(renamed["name"], "After Rename")
            self.assertEqual(renamed["game_version"], "1.21.8")
            self.assertEqual(renamed["loader"], "fabric")
            self.assertEqual(list_instance_profiles(root)[0]["name"], "After Rename")
            self.assertEqual(world.read_text(encoding="utf-8"), "world")

    def test_rename_rejects_duplicate_or_invalid_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            first = create_instance_profile(root, "Alpha", "1.21")
            second = create_instance_profile(root, "Beta", "1.21")
            with self.assertRaisesRegex(ValueError, "already exists"):
                rename_instance_profile(root, second["id"], "alpha")
            with self.assertRaises(ValueError):
                rename_instance_profile(root, second["id"], "../outside")
            self.assertEqual(list_instance_profiles(root)[1]["name"], "Beta")

    def test_loader_update_changes_only_matching_profile_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            profile = create_instance_profile(root, "Pack", "1.21.8", "vanilla")
            world = Path(profile["game_directory_path"]) / "saves" / "world.dat"
            world.parent.mkdir()
            world.write_bytes(b"world")

            updated = update_instance_profile_loader(
                root, profile["id"], "1.21.8", "fabric"
            )

            self.assertEqual(updated["loader"], "fabric")
            self.assertEqual(list_instance_profiles(root)[0]["loader"], "fabric")
            self.assertEqual(world.read_bytes(), b"world")

    def test_loader_profile_binding_is_isolated_per_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            first = create_instance_profile(root, "Fabric Pack", "1.21.8")
            second = create_instance_profile(root, "Forge Pack", "1.21.8")

            bound = set_instance_profile_loader_profile(
                root, first["id"], "1.21.8", "fabric",
                "fabric-loader-0.16.9-1.21.8",
            )

            self.assertEqual(bound["loader"], "fabric")
            self.assertEqual(bound["loader_profile_id"], "fabric-loader-0.16.9-1.21.8")
            resolved_first = __import__("instance_profiles").resolve_instance_profile(
                root, first["id"], "1.21.8"
            )
            resolved_second = __import__("instance_profiles").resolve_instance_profile(
                root, second["id"], "1.21.8"
            )
            self.assertEqual(resolved_first["loader_profile_id"], "fabric-loader-0.16.9-1.21.8")
            self.assertEqual(resolved_second["loader"], "vanilla")
            self.assertIsNone(resolved_second["loader_profile_id"])

    def test_loader_change_clears_old_profile_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            profile = create_instance_profile(root, "Pack", "1.21.8")
            set_instance_profile_loader_profile(
                root, profile["id"], "1.21.8", "fabric",
                "fabric-loader-0.16.9-1.21.8",
            )
            update_instance_profile_loader(root, profile["id"], "1.21.8", "forge")
            resolved = __import__("instance_profiles").resolve_instance_profile(
                root, profile["id"], "1.21.8"
            )
            self.assertEqual(resolved["loader"], "forge")
            self.assertIsNone(resolved["loader_profile_id"])

    def test_loader_profile_binding_rejects_invalid_id_and_wrong_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            profile = create_instance_profile(root, "Pack", "1.21.8")
            with self.assertRaisesRegex(ValueError, "version changed"):
                set_instance_profile_loader_profile(
                    root, profile["id"], "1.20.1", "fabric", "fabric-loader-1.0"
                )
            with self.assertRaisesRegex(ValueError, "profile ID"):
                set_instance_profile_loader_profile(
                    root, profile["id"], "1.21.8", "fabric", "../outside"
                )
            with self.assertRaisesRegex(ValueError, "does not match"):
                set_instance_profile_loader_profile(
                    root, profile["id"], "1.21.8", "forge",
                    "fabric-loader-0.16.9-1.21.8"
                )

    def test_loader_update_rejects_wrong_version_and_unknown_loader(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            profile = create_instance_profile(root, "Pack", "1.21.8")
            with self.assertRaisesRegex(ValueError, "version changed"):
                update_instance_profile_loader(root, profile["id"], "1.20.1", "forge")
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                update_instance_profile_loader(root, profile["id"], "1.21.8", "unknown")
            self.assertEqual(list_instance_profiles(root)[0]["loader"], "vanilla")

    def test_delete_keeps_game_data_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = create_instance_profile(Path(temp) / "profiles", "Keep", "1.21")
            game = Path(profile["game_directory_path"])
            (game / "saves").mkdir()
            (game / "saves" / "world.txt").write_text("world", encoding="utf-8")
            result = delete_instance_profile(Path(temp) / "profiles", profile["id"])
            self.assertFalse(list_instance_profiles(Path(temp) / "profiles"))
            self.assertTrue((game / "saves" / "world.txt").is_file())
            self.assertFalse(result["game_data_deleted"])

    def test_delete_data_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            profile = create_instance_profile(root, "Delete", "1.21")
            result = delete_instance_profile(root, profile["id"], delete_game_data=True)
            self.assertTrue(result["game_data_deleted"])
            self.assertFalse((root / profile["id"]).exists())

    def test_list_rejects_tampered_profile_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            profile = create_instance_profile(root, "Tamper", "1.21")
            metadata = root / profile["id"] / "profile.json"
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            payload["game_directory"] = "../../outside"
            metadata.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "game directory"):
                list_instance_profiles(root)


    def test_mods_directory_is_isolated_per_named_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            first = create_instance_profile(root, "PvP", "1.21.8")
            second = create_instance_profile(root, "Survival", "1.21.8")

            first_mods = resolve_instance_mods_directory(root, first["id"], "1.21.8")
            second_mods = resolve_instance_mods_directory(root, second["id"], "1.21.8")
            self.assertNotEqual(first_mods, second_mods)
            self.assertEqual(first_mods, Path(first["game_directory_path"]) / "mods")
            self.assertEqual(second_mods, Path(second["game_directory_path"]) / "mods")
            with self.assertRaisesRegex(ValueError, "not selected version"):
                resolve_instance_mods_directory(root, first["id"], "1.20.1")

    def test_resource_and_shader_directories_are_isolated_per_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            first = create_instance_profile(root, "PvP", "1.21.8")
            second = create_instance_profile(root, "Survival", "1.21.8")

            first_dirs = resolve_instance_resource_directories(root, first["id"], "1.21.8")
            second_dirs = resolve_instance_resource_directories(root, second["id"], "1.21.8")
            self.assertEqual(set(first_dirs), {"resourcepacks", "shaderpacks"})
            for key in ("resourcepacks", "shaderpacks"):
                self.assertEqual(first_dirs[key], Path(first["game_directory_path"]) / key)
                self.assertEqual(second_dirs[key], Path(second["game_directory_path"]) / key)
                self.assertNotEqual(first_dirs[key], second_dirs[key])
            with self.assertRaisesRegex(ValueError, "not selected version"):
                resolve_instance_resource_directories(root, first["id"], "1.20.1")

    def test_resource_directory_rejects_symbolic_link(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            profile = create_instance_profile(root, "Unsafe", "1.21.8")
            game = Path(profile["game_directory_path"])
            target = Path(temp) / "outside"
            target.mkdir()
            try:
                (game / "shaderpacks").symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Directory symlinks are unavailable on this platform")
            with self.assertRaisesRegex(ValueError, "shaderpacks directory is a symbolic link"):
                resolve_instance_resource_directories(root, profile["id"], "1.21.8")

    def test_mods_directory_rejects_symbolic_link(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            profile = create_instance_profile(root, "Unsafe", "1.21.8")
            game = Path(profile["game_directory_path"])
            target = Path(temp) / "outside"
            target.mkdir()
            try:
                (game / "mods").symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Directory symlinks are unavailable on this platform")
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                resolve_instance_mods_directory(root, profile["id"], "1.21.8")


if __name__ == "__main__":
    unittest.main()
