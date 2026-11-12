import json
import tempfile
import unittest
from pathlib import Path

from instance_profiles import (
    create_instance_profile, delete_instance_profile, list_instance_profiles,
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


if __name__ == "__main__":
    unittest.main()
