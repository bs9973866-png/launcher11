import hashlib
import io
import json
import os
import stat
import zipfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import subprocess
import urllib.request

from launcher_core import (download_verified_client, download_verified_library, download_verified_libraries, download_verified_version_json,
                           resolve_asset_objects, resolve_library_artifacts, summarize_version_metadata,
                           extract_native_archives, discover_java_candidates, validate_java_executable,
                           build_minecraft_classpath, build_launch_plan,
                           verify_cached_asset_objects, safe_extract_temurin_zip,
                           _fetch_temurin_asset, install_temurin_runtime,
                           search_modrinth_mods, fetch_modrinth_compatible_version,
                           download_verified_modrinth_file, list_installed_mods,
                           remove_installed_mod, _validated_native_member_name, fetch_fabric_loader_versions, fetch_forge_loader_versions, _download_forge_installer,
                           install_fabric_loader, merge_fabric_profile_metadata, merge_forge_profile_metadata,
                           fetch_neoforge_loader_versions, _download_neoforge_installer, merge_neoforge_profile_metadata, fetch_quilt_loader_versions, fetch_quilt_installer_versions, _download_quilt_installer, install_quilt_loader, merge_quilt_profile_metadata, install_modrinth_mrpack)


def sample_metadata():
    return {
        "javaVersion": {"majorVersion": 21},
        "downloads": {"client": {
            "url": "https://piston-data.mojang.com/v1/objects/example/client.jar",
            "sha1": "a" * 40,
            "size": 123456,
        }},
        "assetIndex": {"id": "17"},
        "libraries": [{}, {}],
    }


class VersionMetadataTests(unittest.TestCase):
    def test_valid_metadata_summary(self):
        summary = summarize_version_metadata(sample_metadata())
        self.assertEqual(summary["java_major"], 21)
        self.assertEqual(summary["client_size"], 123456)
        self.assertEqual(summary["client_sha1"], "a" * 40)
        self.assertEqual(summary["asset_index"], "17")
        self.assertEqual(summary["libraries_count"], 2)

    def test_rejects_non_mojang_client_host(self):
        data = sample_metadata()
        data["downloads"]["client"]["url"] = "https://example.com/client.jar"
        with self.assertRaises(ValueError):
            summarize_version_metadata(data)

    def test_rejects_bad_checksum(self):
        data = sample_metadata()
        data["downloads"]["client"]["sha1"] = "not-a-checksum"
        with self.assertRaises(ValueError):
            summarize_version_metadata(data)

    def test_rejects_bad_size(self):
        data = sample_metadata()
        data["downloads"]["client"]["size"] = 0
        with self.assertRaises(ValueError):
            summarize_version_metadata(data)

    def test_java_requirement_is_optional(self):
        data = sample_metadata()
        data.pop("javaVersion")
        self.assertIsNone(summarize_version_metadata(data)["java_major"])


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size):
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class ClientDownloadTests(unittest.TestCase):
    def setUp(self):
        self.payload = b"verified minecraft client bytes"
        self.summary = {
            "client_url": "https://piston-data.mojang.com/client.jar",
            "client_sha1": hashlib.sha1(self.payload).hexdigest(),
            "client_size": len(self.payload),
        }

    def test_download_verifies_and_writes_file(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = Mock()
            opener.open.return_value = FakeResponse(self.payload)
            target = Path(directory) / "client.jar"
            progress = []
            result = download_verified_client(
                self.summary, target,
                progress_callback=lambda done, total: progress.append((done, total)),
                opener=opener,
            )
            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), self.payload)
            self.assertEqual(progress[-1], (len(self.payload), len(self.payload)))

    def test_existing_verified_file_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "client.jar"
            target.write_bytes(self.payload)
            opener = Mock()
            self.assertEqual(download_verified_client(self.summary, target, opener=opener), target)
            opener.open.assert_not_called()

    def test_checksum_failure_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "client.jar"
            target.write_bytes(b"previous safe file")
            bad = dict(self.summary, client_sha1="0" * 40)
            opener = Mock()
            opener.open.return_value = FakeResponse(self.payload)
            with self.assertRaisesRegex(ValueError, "SHA-1"):
                download_verified_client(bad, target, opener=opener)
            self.assertEqual(target.read_bytes(), b"previous safe file")
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_rejects_untrusted_url_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = dict(self.summary, client_url="https://example.org/client.jar")
            opener = Mock()
            with self.assertRaisesRegex(ValueError, "trusted"):
                download_verified_client(bad, Path(directory) / "client.jar", opener=opener)
            opener.open.assert_not_called()

    def test_rejects_oversized_download(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = dict(self.summary, client_size=len(self.payload) - 1)
            opener = Mock()
            opener.open.return_value = FakeResponse(self.payload)
            with self.assertRaisesRegex(ValueError, "larger"):
                download_verified_client(bad, Path(directory) / "client.jar", opener=opener)

    def test_rejects_truncated_download(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = dict(self.summary, client_size=len(self.payload) + 5)
            opener = Mock()
            opener.open.return_value = FakeResponse(self.payload)
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                download_verified_client(bad, Path(directory) / "client.jar", opener=opener)


class VersionJsonCacheTests(unittest.TestCase):
    def setUp(self):
        self.payload = b'{"id":"1.21.4","libraries":[]}'
        self.url = "https://piston-meta.mojang.com/v1/packages/example/1.21.4.json"
        self.sha1 = hashlib.sha1(self.payload).hexdigest()

    def test_downloads_and_verifies_version_json(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = Mock()
            opener.open.return_value = FakeResponse(self.payload)
            target = Path(directory) / "1.21.4.json"
            result = download_verified_version_json(self.url, self.sha1, target, opener=opener)
            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), self.payload)

    def test_reuses_valid_cached_json_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "version.json"
            target.write_bytes(self.payload)
            opener = Mock()
            download_verified_version_json(self.url, self.sha1, target, opener=opener)
            opener.open.assert_not_called()

    def test_rejects_bad_hash_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "version.json"
            target.write_bytes(b"old valid content")
            opener = Mock()
            opener.open.return_value = FakeResponse(self.payload)
            with self.assertRaisesRegex(ValueError, "SHA-1"):
                download_verified_version_json(self.url, "0" * 40, target, opener=opener)
            self.assertEqual(target.read_bytes(), b"old valid content")
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_rejects_untrusted_metadata_host_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = Mock()
            with self.assertRaisesRegex(ValueError, "metadata host"):
                download_verified_version_json(
                    "https://example.com/version.json", self.sha1,
                    Path(directory) / "version.json", opener=opener
                )
            opener.open.assert_not_called()

    def test_rejects_invalid_json_without_replacing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "version.json"
            target.write_bytes(b"previous")
            invalid = b"not json"
            opener = Mock()
            opener.open.return_value = FakeResponse(invalid)
            with self.assertRaisesRegex(ValueError, "valid UTF-8 JSON"):
                download_verified_version_json(
                    self.url, hashlib.sha1(invalid).hexdigest(), target, opener=opener
                )
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(Path(directory).glob("*.part")), [])


class LibraryResolutionTests(unittest.TestCase):
    def artifact(self, path):
        return {
            "path": path,
            "url": "https://libraries.minecraft.net/" + path,
            "sha1": "b" * 40,
            "size": 456,
        }

    def test_resolves_artifacts_and_windows_native_classifier(self):
        metadata = {"libraries": [{
            "name": "example:library:1.0",
            "downloads": {
                "artifact": self.artifact("example/library/1.0/library-1.0.jar"),
                "classifiers": {
                    "natives-windows-64": self.artifact("example/library/1.0/library-1.0-natives-windows.jar")
                },
            },
            "natives": {"windows": "natives-windows-\u0024{arch}"},
        }]}
        resolved = resolve_library_artifacts(metadata)
        self.assertEqual(len(resolved), 2)
        self.assertEqual({item["source"] for item in resolved}, {"artifact", "native"})

    def test_applies_allow_and_disallow_rules_in_order(self):
        metadata = {"libraries": [
            {"downloads": {"artifact": self.artifact("allowed.jar")},
             "rules": [{"action": "allow", "os": {"name": "windows"}}]},
            {"downloads": {"artifact": self.artifact("blocked.jar")},
             "rules": [{"action": "allow"}, {"action": "disallow", "os": {"name": "windows"}}]},
            {"downloads": {"artifact": self.artifact("linux.jar")},
             "rules": [{"action": "allow", "os": {"name": "linux"}}]},
        ]}
        self.assertEqual(
            [item["path"] for item in resolve_library_artifacts(metadata)],
            ["allowed.jar"],
        )

    def test_feature_rules_default_to_false(self):
        metadata = {"libraries": [{
            "downloads": {"artifact": self.artifact("demo.jar")},
            "rules": [{"action": "allow", "features": {"is_demo_user": True}}],
        }]}
        self.assertEqual(resolve_library_artifacts(metadata), [])
        self.assertEqual(
            len(resolve_library_artifacts(metadata, features={"is_demo_user": True})), 1
        )

    def test_rejects_path_traversal_and_non_https(self):
        for bad_path, bad_url in [
            ("../outside.jar", "https://libraries.minecraft.net/file.jar"),
            ("safe.jar", "http://libraries.minecraft.net/file.jar"),
        ]:
            metadata = {"libraries": [{
                "downloads": {"artifact": {
                    **self.artifact(bad_path), "url": bad_url
                }}
            }]}
            with self.assertRaises(ValueError):
                resolve_library_artifacts(metadata)

    def test_rejects_bad_checksum_and_size(self):
        for changes in ({"sha1": "bad"}, {"size": 0}):
            artifact = {**self.artifact("lib.jar"), **changes}
            with self.assertRaises(ValueError):
                resolve_library_artifacts({"libraries": [{
                    "downloads": {"artifact": artifact}
                }]})

    def test_ignores_library_not_allowed_for_target_os(self):
        metadata = {"libraries": [{
            "downloads": {"artifact": self.artifact("mac.jar")},
            "rules": [{"action": "allow", "os": {"name": "osx"}}],
        }]}
        self.assertEqual(resolve_library_artifacts(metadata, os_name="windows"), [])


    def test_rejects_windows_style_path_separators(self):
        metadata = {"libraries": [{
            "downloads": {"artifact": self.artifact("..\\\\outside.jar")}
        }]}
        with self.assertRaisesRegex(ValueError, "relative path"):
            resolve_library_artifacts(metadata)

    def test_malformed_os_rule_does_not_crash_or_allow(self):
        metadata = {"libraries": [{
            "downloads": {"artifact": self.artifact("odd.jar")},
            "rules": [{"action": "allow", "os": "windows"}],
        }]}
        self.assertEqual(resolve_library_artifacts(metadata), [])


class LibraryDownloadTests(unittest.TestCase):
    def setUp(self):
        self.payload = b"verified library artifact"
        self.artifact = {
            "path": "org/example/demo/1.0/demo-1.0.jar",
            "url": "https://libraries.minecraft.net/org/example/demo/1.0/demo-1.0.jar",
            "sha1": hashlib.sha1(self.payload).hexdigest(),
            "size": len(self.payload),
            "source": "artifact",
        }

    def test_downloads_and_verifies_library(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = Mock()
            opener.open.return_value = FakeResponse(self.payload)
            progress = []
            result = download_verified_library(
                self.artifact, directory,
                progress_callback=lambda done, total: progress.append((done, total)),
                opener=opener,
            )
            self.assertEqual(result.read_bytes(), self.payload)
            self.assertEqual(progress[-1], (len(self.payload), len(self.payload)))

    def test_reuses_verified_library_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / self.artifact["path"]
            target.parent.mkdir(parents=True)
            target.write_bytes(self.payload)
            opener = Mock()
            result = download_verified_library(self.artifact, directory, opener=opener)
            self.assertTrue(result.samefile(target))
            opener.open.assert_not_called()

    def test_hash_mismatch_preserves_old_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / self.artifact["path"]
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old library")
            bad = dict(self.artifact, sha1="0" * 40)
            opener = Mock()
            opener.open.return_value = FakeResponse(self.payload)
            with self.assertRaisesRegex(ValueError, "SHA-1"):
                download_verified_library(bad, directory, opener=opener)
            self.assertEqual(target.read_bytes(), b"old library")
            self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_rejects_truncated_and_oversized_payloads(self):
        for expected_size, message in ((len(self.payload) + 1, "Incomplete"),
                                       (len(self.payload) - 1, "larger")):
            with self.subTest(expected_size=expected_size), tempfile.TemporaryDirectory() as directory:
                artifact = dict(self.artifact, size=expected_size)
                opener = Mock()
                opener.open.return_value = FakeResponse(self.payload)
                with self.assertRaisesRegex(ValueError, message):
                    download_verified_library(artifact, directory, opener=opener)

    def test_rejects_path_escape_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = dict(self.artifact, path="../outside.jar")
            opener = Mock()
            with self.assertRaisesRegex(ValueError, "relative path|escapes"):
                download_verified_library(artifact, directory, opener=opener)
            opener.open.assert_not_called()

    def test_redirect_handler_rejects_different_host_and_http(self):
        from launcher_core import SameHostLibraryRedirectHandler
        handler = SameHostLibraryRedirectHandler("libraries.minecraft.net")
        request = urllib.request.Request(self.artifact["url"])
        for url in ("https://evil.example/file.jar", "http://libraries.minecraft.net/file.jar"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "redirected"):
                handler.redirect_request(request, None, 302, "Found", {}, url)


class LibraryBatchDownloadTests(unittest.TestCase):
    def make_artifact(self, name, payload):
        return {
            "path": f"org/example/{name}/1.0/{name}.jar",
            "url": f"https://libraries.minecraft.net/org/example/{name}/1.0/{name}.jar",
            "sha1": hashlib.sha1(payload).hexdigest(),
            "size": len(payload),
            "source": "artifact",
        }

    def test_downloads_batch_and_reports_aggregate_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = b"first-library", b"second-library"
            artifacts = [
                self.make_artifact("first", first),
                self.make_artifact("second", second),
            ]
            responses = [FakeResponse(first), FakeResponse(second)]
            opener = Mock()
            opener.open.side_effect = responses
            progress = []
            result = download_verified_libraries(
                artifacts, directory,
                progress_callback=lambda done, total, path: progress.append((done, total, path)),
                opener_factory=lambda _item: opener,
            )
            self.assertEqual([path.read_bytes() for path in result], [first, second])
            self.assertEqual(progress[-1][0:2], (len(first) + len(second), len(first) + len(second)))
            self.assertEqual(opener.open.call_count, 2)

    def test_batch_reuses_verified_files(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = b"cached-library"
            artifact = self.make_artifact("cached", payload)
            target = Path(directory) / artifact["path"]
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            opener = Mock()
            result = download_verified_libraries(
                [artifact], directory, opener_factory=lambda _item: opener
            )
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].samefile(target))
            opener.open.assert_not_called()

    def test_batch_rejects_conflicting_duplicate_paths_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            one = self.make_artifact("same", b"one")
            two = dict(one, sha1=hashlib.sha1(b"two").hexdigest(), size=3)
            opener = Mock()
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                download_verified_libraries(
                    [one, two], directory, opener_factory=lambda _item: opener
                )
            opener.open.assert_not_called()

    def test_batch_rejects_invalid_entry_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = Mock()
            with self.assertRaises(ValueError):
                download_verified_libraries(
                    [self.make_artifact("good", b"ok"), {"path": "../bad"}],
                    directory, opener_factory=lambda _item: opener
                )
            opener.open.assert_not_called()


class AssetResolutionTests(unittest.TestCase):
    def test_resolves_asset_hashes_to_mojang_object_paths(self):
        index = {"objects": {
            "minecraft/sounds/test.ogg": {"hash": "a" * 40, "size": 17},
            "minecraft/textures/test.png": {"hash": "b" * 40, "size": 29},
        }}
        assets = resolve_asset_objects(index)
        self.assertEqual(len(assets), 2)
        self.assertEqual(assets[0]["path"], "aa/" + "a" * 40)
        self.assertEqual(assets[0]["url"], "https://resources.download.minecraft.net/aa/" + "a" * 40)
        self.assertEqual(assets[0]["sha1"], "a" * 40)
        self.assertEqual(assets[0]["size"], 17)

    def test_deduplicates_identical_content_hashes(self):
        index = {"objects": {
            "one": {"hash": "c" * 40, "size": 5},
            "two": {"hash": "c" * 40, "size": 5},
        }}
        self.assertEqual(len(resolve_asset_objects(index)), 1)

    def test_rejects_invalid_asset_hash_and_size(self):
        for metadata in ({"hash": "bad", "size": 5},
                         {"hash": "d" * 40, "size": True},
                         {"hash": "d" * 40, "size": 0}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                resolve_asset_objects({"objects": {"asset": metadata}})

    def test_rejects_missing_objects_map(self):
        with self.assertRaisesRegex(ValueError, "objects map"):
            resolve_asset_objects({})

if __name__ == "__main__":
    unittest.main()



class NativeArchiveExtractionTests(unittest.TestCase):
    def make_archive(self, directory, entries, name="native.jar"):
        path = Path(directory) / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry_name, payload in entries:
                if isinstance(payload, zipfile.ZipInfo):
                    archive.writestr(payload, b"link-target")
                else:
                    archive.writestr(entry_name, payload)
        return path

    def test_extracts_safe_nested_files_and_skips_meta_inf(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_archive(directory, [
                ("org/example/native.dll", b"native-bytes"),
                ("META-INF/MANIFEST.MF", b"manifest"),
            ])
            output = Path(directory) / "natives"
            extracted = extract_native_archives([archive], output)
            self.assertEqual(len(extracted), 1)
            self.assertEqual((output / "org/example/native.dll").read_bytes(), b"native-bytes")
            self.assertFalse((output / "META-INF").exists())

    def test_rejects_parent_traversal_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_archive(directory, [("../escape.dll", b"bad")])
            output = Path(directory) / "natives"
            with self.assertRaisesRegex(ValueError, "unsafe"):
                extract_native_archives([archive], output)
            self.assertFalse((Path(directory) / "escape.dll").exists())

    def test_rejects_absolute_drive_and_backslash_paths(self):
        for member in ("/rooted.dll", "C:/Windows/evil.dll", "folder\\evil.dll"):
            with self.subTest(member=member), tempfile.TemporaryDirectory() as directory:
                if "\\" in member:
                    with self.assertRaises(ValueError):
                        _validated_native_member_name(member)
                else:
                    archive = self.make_archive(directory, [(member, b"bad")])
                    with self.assertRaises(ValueError):
                        extract_native_archives([archive], Path(directory) / "out")

    def test_rejects_symlink_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            link = zipfile.ZipInfo("native-link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive = self.make_archive(directory, [("native-link", link)])
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                extract_native_archives([archive], Path(directory) / "out")

    def test_enforces_entry_and_uncompressed_size_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_archive(directory, [
                ("one.dll", b"1234"), ("two.dll", b"5678")
            ])
            with self.assertRaisesRegex(ValueError, "entry-count"):
                extract_native_archives([archive], Path(directory) / "count", max_entries=1)
            with self.assertRaisesRegex(ValueError, "total extraction"):
                extract_native_archives([archive], Path(directory) / "size", max_total_size=7)
            with self.assertRaisesRegex(ValueError, "file above"):
                extract_native_archives([archive], Path(directory) / "file", max_file_size=3)

    def test_rejects_duplicate_output_paths_across_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.make_archive(directory, [("same.dll", b"first")], "first.jar")
            second = self.make_archive(directory, [("same.dll", b"second")], "second.jar")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                extract_native_archives([first, second], Path(directory) / "out")

    def test_rejects_oversized_entry_before_creating_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_archive(directory, [("native.dll", b"payload")])
            output = Path(directory) / "out"
            # A deliberately restrictive limit trips during the streaming write.
            with self.assertRaisesRegex(ValueError, "file above"):
                extract_native_archives([archive], output, max_file_size=3)
            self.assertEqual(list(output.rglob("*.part")), [])



class JavaDiscoveryTests(unittest.TestCase):
    def test_discovers_java_home_and_extra_path_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = "java.exe" if os.name == "nt" else "java"
            home_java = root / "jdk-home" / "bin" / executable
            extra_java = root / "extra" / executable
            home_java.parent.mkdir(parents=True)
            extra_java.parent.mkdir(parents=True)
            home_java.write_bytes(b"")
            extra_java.write_bytes(b"")
            found = discover_java_candidates(
                extra_paths=[extra_java],
                environ={"JAVA_HOME": str(home_java.parent.parent), "PATH": ""},
                path_value="", include_system=False,
            )
            self.assertEqual(found, [extra_java.resolve(), home_java.resolve()])

    def test_ignores_missing_candidates_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            java = Path(directory) / ("java.exe" if os.name == "nt" else "java")
            java.write_bytes(b"")
            found = discover_java_candidates(
                extra_paths=[java, java, Path(directory) / "missing"],
                environ={"PATH": ""}, path_value="", include_system=False,
            )
            self.assertEqual(found, [java.resolve()])

    def test_parses_modern_java_version_from_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            java = Path(directory) / ("java.exe" if os.name == "nt" else "java")
            java.write_bytes(b"")
            completed = subprocess.CompletedProcess(
                [str(java), "-version"], 0, "", 'openjdk version "21.0.2" 2024-01-16'
            )
            with patch("launcher_core.subprocess.run", return_value=completed) as run:
                result = validate_java_executable(java, expected_major=21)
            self.assertEqual(result["major"], 21)
            self.assertTrue(result["compatible"])
            run.assert_called_once()

    def test_parses_legacy_java_8_and_reports_incompatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            java = Path(directory) / ("java.exe" if os.name == "nt" else "java")
            java.write_bytes(b"")
            completed = subprocess.CompletedProcess(
                [str(java), "-version"], 0, "", 'java version "1.8.0_402"'
            )
            with patch("launcher_core.subprocess.run", return_value=completed):
                result = validate_java_executable(java, expected_major=17)
            self.assertEqual(result["major"], 8)
            self.assertFalse(result["compatible"])

    def test_rejects_missing_and_unrecognized_java(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "java"
            with self.assertRaisesRegex(ValueError, "does not exist"):
                validate_java_executable(missing)
            java = Path(directory) / ("java.exe" if os.name == "nt" else "java")
            java.write_bytes(b"")
            completed = subprocess.CompletedProcess([str(java), "-version"], 1, "", "not java")
            with patch("launcher_core.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(ValueError, "recognizable"):
                    validate_java_executable(java)


class TemurinRuntimeTests(unittest.TestCase):
    def make_zip(self, directory, entries):
        archive_path = Path(directory) / "temurin.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name, payload in entries:
                archive.writestr(name, payload)
        return archive_path

    def test_extracts_java_runtime_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_zip(directory, [
                ("jdk-test/bin/java.exe", b"java-binary"),
                ("jdk-test/release", b"JAVA_VERSION=21"),
            ])
            java = safe_extract_temurin_zip(archive, Path(directory) / "out")
            self.assertEqual(java.relative_to((Path(directory) / "out").resolve()).as_posix(),
                             "jdk-test/bin/java.exe")
            self.assertEqual(java.read_bytes(), b"java-binary")

    def test_rejects_zip_slip_and_cleans_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_zip(directory, [
                ("jdk-test/bin/java.exe", b"java"),
                ("../escape.txt", b"nope"),
            ])
            output = Path(directory) / "out"
            with self.assertRaisesRegex(ValueError, "traversal"):
                safe_extract_temurin_zip(archive, output)
            self.assertFalse((Path(directory) / "escape.txt").exists())

    def test_rejects_archive_without_java_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_zip(directory, [("jdk-test/readme.txt", b"not java")])
            with self.assertRaisesRegex(ValueError, "java.exe"):
                safe_extract_temurin_zip(archive, Path(directory) / "out")

    def test_rejects_non_zip_package_metadata(self):
        payload = [{
            "binary": {
                "os": "windows", "architecture": "x64", "image_type": "jdk",
                "package": {
                    "link": "https://example.com/temurin.zip",
                    "checksum": "a" * 64, "size": 100, "name": "temurin.zip"
                }
            },
            "version": {"major": 21}
        }]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return json.dumps(payload).encode("utf-8")
        class Opener:
            def open(self, request, timeout): return Response()
        with self.assertRaisesRegex(ValueError, "valid Windows x64"):
            _fetch_temurin_asset(21, opener=Opener())

    def test_runtime_installer_is_windows_only_on_this_platform(self):
        if os.name == "nt":
            self.skipTest("Windows platform")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Windows x64"):
                install_temurin_runtime(21, directory)



class ModrinthIntegrationTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, limit):
            return self.payload[:limit]

    class JsonOpener:
        def __init__(self, payload):
            self.payload = payload
            self.request = None
        def open(self, request, timeout):
            self.request = request
            return ModrinthIntegrationTests.Response(
                json.dumps(self.payload).encode("utf-8")
            )

    def test_search_returns_only_compatible_mod_projects(self):
        payload = {"hits": [
            {
                "project_id": "abc12345", "slug": "sodium", "title": "Sodium",
                "description": "Rendering optimization", "downloads": 1200,
                "project_type": "mod", "versions": ["1.21.4"],
            },
            {
                "project_id": "pack1234", "slug": "pack", "title": "A pack",
                "description": "Not a mod", "downloads": 10,
                "project_type": "modpack", "versions": ["1.21.4"],
            },
            {
                "project_id": "old12345", "slug": "old", "title": "Old",
                "description": "Wrong game version", "downloads": 5,
                "project_type": "mod", "versions": ["1.20.1"],
            },
        ]}
        opener = self.JsonOpener(payload)
        result = search_modrinth_mods("sodium", "1.21.4", "fabric", opener=opener)
        self.assertEqual([item["title"] for item in result], ["Sodium"])
        self.assertIn("api.modrinth.com", opener.request.full_url)
        self.assertIn("versions%3A1.21.4", opener.request.full_url)

    def test_search_rejects_invalid_game_version_and_loader(self):
        with self.assertRaisesRegex(ValueError, "concrete Minecraft"):
            search_modrinth_mods("sodium", "latest-release")
        with self.assertRaisesRegex(ValueError, "Unsupported mod loader"):
            search_modrinth_mods("sodium", "1.21.4", "vanilla")

    def test_fetches_matching_version_and_validates_cdn_file(self):
        payload = [{
            "id": "version123", "name": "Sodium build",
            "game_versions": ["1.21.4"], "loaders": ["fabric"],
            "files": [{
                "filename": "sodium.jar", "url": "https://cdn.modrinth.com/data/a/sodium.jar",
                "size": 12, "primary": True,
                "hashes": {"sha1": "a" * 40},
            }],
        }]
        opener = self.JsonOpener(payload)
        result = fetch_modrinth_compatible_version(
            "abc12345", "1.21.4", "fabric", opener=opener
        )
        self.assertEqual(result["file"]["filename"], "sodium.jar")
        self.assertEqual(result["loader"], "fabric")
        self.assertIn("/v2/project/abc12345/version", opener.request.full_url)

    def test_rejects_untrusted_download_host(self):
        payload = [{
            "id": "version123", "name": "Sodium build",
            "game_versions": ["1.21.4"], "loaders": ["fabric"],
            "files": [{
                "filename": "sodium.jar", "url": "https://example.com/sodium.jar",
                "size": 12, "primary": True,
                "hashes": {"sha1": "a" * 40},
            }],
        }]
        with self.assertRaisesRegex(ValueError, "No fabric build"):
            fetch_modrinth_compatible_version(
                "abc12345", "1.21.4", "fabric", opener=self.JsonOpener(payload)
            )

    def test_download_verifies_checksum_and_reuses_cache(self):
        payload = b"mod-jar-bytes"
        digest = hashlib.sha1(payload).hexdigest()
        info = {
            "file": {
                "filename": "sample-mod.jar",
                "url": "https://cdn.modrinth.com/data/sample-mod.jar",
                "size": len(payload), "sha1": digest, "sha512": None,
            }
        }
        class BinaryResponse:
            def __init__(self): self.offset = 0
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit):
                chunk = payload[self.offset:self.offset + limit]
                self.offset += len(chunk)
                return chunk
        class BinaryOpener:
            def open(self, request, timeout): return BinaryResponse()
        with tempfile.TemporaryDirectory() as directory:
            target = download_verified_modrinth_file(
                info, directory, opener=BinaryOpener()
            )
            self.assertEqual(target.read_bytes(), payload)
            reused = download_verified_modrinth_file(info, directory)
            self.assertEqual(reused, target)

    def test_rejects_mod_checksum_mismatch_without_installing_file(self):
        payload = b"mod-jar-bytes"
        info = {
            "file": {
                "filename": "sample-mod.jar",
                "url": "https://cdn.modrinth.com/data/sample-mod.jar",
                "size": len(payload), "sha1": "0" * 40, "sha512": None,
            }
        }
        class BinaryResponse:
            def __init__(self): self.offset = 0
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit):
                chunk = payload[self.offset:self.offset + limit]
                self.offset += len(chunk)
                return chunk
        class BinaryOpener:
            def open(self, request, timeout): return BinaryResponse()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "SHA-1 verification"):
                download_verified_modrinth_file(info, directory, opener=BinaryOpener())
            self.assertEqual(list(Path(directory).iterdir()), [])






class FabricLoaderTests(unittest.TestCase):
    class JsonResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _size=-1):
            return self.payload

    class JsonOpener:
        def __init__(self, payload):
            self.payload = payload
            self.url = None

        def open(self, request, timeout=0):
            self.url = request.full_url
            return FabricLoaderTests.JsonResponse(self.payload)

    def test_fetches_and_validates_fabric_loader_versions(self):
        payload = json.dumps([
            {"loader": {"version": "0.17.0-beta.1", "stable": False}},
            {"loader": {"version": "0.16.10", "stable": True}},
            {"loader": {"version": "../unsafe", "stable": True}},
        ]).encode()
        opener = self.JsonOpener(payload)
        versions = fetch_fabric_loader_versions("1.21.4", opener=opener)
        self.assertEqual([item["version"] for item in versions], ["0.16.10", "0.17.0-beta.1"])
        self.assertEqual(opener.url, "https://meta.fabricmc.net/v2/versions/loader/1.21.4")


    def test_installer_rejects_non_java_executable_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "not-java.exe"
            executable.write_bytes(b"not java")
            with self.assertRaisesRegex(ValueError, "not named java"):
                install_fabric_loader(
                    "1.21.4", "0.16.10", executable, Path(directory) / "voidex"
                )

    def test_rejects_invalid_minecraft_version_before_network(self):
        with self.assertRaisesRegex(ValueError, "concrete Minecraft"):
            fetch_fabric_loader_versions("../latest")

    def test_merges_fabric_profile_and_existing_maven_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = Path("net/fabricmc/fabric-loader/0.16.10/fabric-loader-0.16.10.jar")
            library = root / relative
            library.parent.mkdir(parents=True)
            library.write_bytes(b"fabric-loader")
            base = {
                "id": "1.21.4",
                "mainClass": "net.minecraft.client.main.Main",
                "libraries": [],
                "arguments": {"jvm": ["-Dvanilla=true"], "game": ["--demo"]},
                "javaVersion": {"majorVersion": 21},
                "downloads": {"client": {"url": "https://example.invalid/client.jar"}},
            }
            profile = {
                "id": "fabric-loader-0.16.10-1.21.4",
                "inheritsFrom": "1.21.4",
                "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
                "libraries": [{
                    "name": "net.fabricmc:fabric-loader:0.16.10",
                    "url": "https://maven.fabricmc.net/",
                }],
                "arguments": {"jvm": ["-Dfabric=true"], "game": []},
            }
            merged = merge_fabric_profile_metadata(base, profile, root)
            self.assertEqual(merged["id"], profile["id"])
            self.assertEqual(merged["mainClass"], profile["mainClass"])
            self.assertEqual(merged["javaVersion"], {"majorVersion": 21})
            self.assertEqual(merged["arguments"]["jvm"], ["-Dvanilla=true", "-Dfabric=true"])
            artifact = merged["libraries"][0]["downloads"]["artifact"]
            self.assertEqual(artifact["path"], relative.as_posix())
            self.assertEqual(artifact["size"], len(b"fabric-loader"))
            self.assertEqual(artifact["sha1"], hashlib.sha1(b"fabric-loader").hexdigest())

    def test_rejects_fabric_profile_for_another_game_version(self):
        with self.assertRaisesRegex(ValueError, "does not inherit"):
            merge_fabric_profile_metadata(
                {"id": "1.21.4", "libraries": []},
                {"id": "fabric-loader-0.16.10-1.21.1", "inheritsFrom": "1.21.1",
                 "mainClass": "example.Main", "libraries": []},
                Path("."),
            )

    def test_missing_fabric_library_blocks_launch_profile_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "library is missing"):
                merge_fabric_profile_metadata(
                    {"id": "1.21.4", "libraries": []},
                    {"id": "fabric-loader-0.16.10-1.21.4", "inheritsFrom": "1.21.4",
                     "mainClass": "example.Main",
                     "libraries": [{"name": "net.fabricmc:fabric-loader:0.16.10"}]},
                    Path(directory),
                )


class ForgeProfileMergeTests(unittest.TestCase):
    def test_merges_forge_profile_and_resolves_installed_maven_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = Path("net/minecraftforge/fmlcore/1.0/fmlcore-1.0.jar")
            target = root / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(b"forge-core")
            base = {
                "id": "1.20.1",
                "mainClass": "net.minecraft.client.main.Main",
                "libraries": [],
                "arguments": {"jvm": ["-Dvanilla=true"], "game": ["--demo"]},
                "javaVersion": {"majorVersion": 17},
            }
            profile = {
                "id": "1.20.1-forge-47.3.0",
                "inheritsFrom": "1.20.1",
                "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
                "libraries": [{
                    "name": "net.minecraftforge:fmlcore:1.0",
                    "url": "https://maven.minecraftforge.net/",
                }],
                "arguments": {"jvm": ["-Dforge=true"], "game": ["--fml.forgeVersion", "47.3.0"]},
            }
            merged = merge_forge_profile_metadata(base, profile, root)
            self.assertEqual(merged["id"], profile["id"])
            self.assertEqual(merged["mainClass"], profile["mainClass"])
            self.assertEqual(merged["javaVersion"], {"majorVersion": 17})
            self.assertEqual(merged["arguments"]["jvm"], ["-Dvanilla=true", "-Dforge=true"])
            artifact = merged["libraries"][0]["downloads"]["artifact"]
            self.assertEqual(artifact["path"], relative.as_posix())
            self.assertEqual(artifact["sha1"], hashlib.sha1(b"forge-core").hexdigest())

    def test_rejects_forge_profile_for_another_game_version(self):
        with self.assertRaisesRegex(ValueError, "does not inherit"):
            merge_forge_profile_metadata(
                {"id": "1.20.1", "libraries": []},
                {"id": "1.20.1-forge-47.3.0", "inheritsFrom": "1.19.4",
                 "mainClass": "example.Main", "libraries": []},
                Path("."),
            )

    def test_rejects_missing_forge_library(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "library is missing"):
                merge_forge_profile_metadata(
                    {"id": "1.20.1", "libraries": []},
                    {"id": "1.20.1-forge-47.3.0", "inheritsFrom": "1.20.1",
                     "mainClass": "example.Main",
                     "libraries": [{"name": "net.minecraftforge:fmlcore:1.0"}]},
                    Path(directory),
                )

    def test_rejects_untrusted_forge_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "net/example/demo/1.0/demo-1.0.jar"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"demo")
            with self.assertRaisesRegex(ValueError, "repository is not trusted"):
                merge_forge_profile_metadata(
                    {"id": "1.20.1", "libraries": []},
                    {"id": "1.20.1-forge-47.3.0", "inheritsFrom": "1.20.1",
                     "mainClass": "example.Main",
                     "libraries": [{"name": "net.example:demo:1.0",
                                    "url": "https://evil.example/"}]},
                    root,
                )




class ForgeLoaderCatalogTests(unittest.TestCase):
    class JsonResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _size=-1):
            return self.payload

    class JsonOpener:
        def __init__(self, payload):
            self.payload = payload
            self.url = None

        def open(self, request, timeout=0):
            self.url = request.full_url
            return ForgeLoaderCatalogTests.JsonResponse(self.payload)

    def test_returns_recommended_before_latest_for_selected_game(self):
        payload = json.dumps({"promos": {
            "1.20.1-latest": "47.4.0",
            "1.20.1-recommended": "47.3.0",
            "1.21.1-latest": "52.0.1",
            "1.20.1-other": "47.2.0",
        }}).encode()
        opener = self.JsonOpener(payload)
        builds = fetch_forge_loader_versions("1.20.1", opener=opener)
        self.assertEqual(builds, [
            {"version": "47.3.0", "channel": "Recommended"},
            {"version": "47.4.0", "channel": "Latest"},
        ])
        self.assertEqual(opener.url,
                         "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")

    def test_rejects_invalid_game_version_before_network(self):
        with self.assertRaisesRegex(ValueError, "concrete Minecraft"):
            fetch_forge_loader_versions("../latest")

    def test_rejects_unsafe_or_wrong_version_promotion(self):
        opener = self.JsonOpener(json.dumps({"promos": {
            "1.20.1-latest": "../malicious",
            "1.20.1-recommended": "1.21.1-52.0.1",
        }}).encode())
        with self.assertRaisesRegex(ValueError, "no published"):
            fetch_forge_loader_versions("1.20.1", opener=opener)

    def test_rejects_malformed_promotions_document(self):
        opener = self.JsonOpener(json.dumps({"promos": []}).encode())
        with self.assertRaisesRegex(ValueError, "promotions map"):
            fetch_forge_loader_versions("1.20.1", opener=opener)




class ForgeInstallerDownloadTests(unittest.TestCase):
    def test_downloads_and_verifies_official_installer(self):
        payload = b"forge-installer-test"
        checksum = hashlib.sha1(payload).hexdigest().encode()
        opener = Mock()
        opener.open.side_effect = [
            io.BytesIO(checksum),
            io.BytesIO(payload),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = _download_forge_installer(
                "1.20.1", "47.3.0", directory, opener=opener
            )
            self.assertEqual(path.name, "forge-1.20.1-47.3.0-installer.jar")
            self.assertEqual(path.read_bytes(), payload)
            self.assertTrue(path.exists())

    def test_rejects_checksum_mismatch_without_leaving_partial_file(self):
        payload = b"unverified-installer"
        opener = Mock()
        opener.open.side_effect = [
            io.BytesIO(b"0" * 40),
            io.BytesIO(payload),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "SHA-1 verification"):
                _download_forge_installer("1.20.1", "47.3.0", directory, opener=opener)
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_rejects_invalid_versions_before_network(self):
        opener = Mock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "version is invalid"):
                _download_forge_installer("../1.20", "47.3.0", directory, opener=opener)
        opener.open.assert_not_called()




class NeoForgeProfileMergeTests(unittest.TestCase):
    def test_merges_profile_and_validates_installed_maven_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = Path("net/neoforged/fmlcore/1.0/fmlcore-1.0.jar")
            target = root / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(b"neoforge-core")
            base = {
                "id": "1.21.1", "mainClass": "net.minecraft.client.main.Main",
                "libraries": [], "arguments": {"jvm": ["-Dvanilla=true"], "game": ["--demo"]},
                "javaVersion": {"majorVersion": 21},
            }
            profile = {
                "id": "1.21.1-neoforge-21.1.172", "inheritsFrom": "1.21.1",
                "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
                "libraries": [{"name": "net.neoforged:fmlcore:1.0",
                               "url": "https://maven.neoforged.net/releases/"}],
                "arguments": {"jvm": ["-Dneoforge=true"], "game": ["--fml.neoForgeVersion", "21.1.172"]},
            }
            merged = merge_neoforge_profile_metadata(base, profile, root)
            self.assertEqual(merged["id"], profile["id"])
            self.assertEqual(merged["mainClass"], profile["mainClass"])
            self.assertEqual(merged["javaVersion"], {"majorVersion": 21})
            self.assertEqual(merged["arguments"]["jvm"], ["-Dvanilla=true", "-Dneoforge=true"])
            artifact = merged["libraries"][0]["downloads"]["artifact"]
            self.assertEqual(artifact["path"], relative.as_posix())
            self.assertEqual(artifact["sha1"], hashlib.sha1(b"neoforge-core").hexdigest())

    def test_accepts_installer_style_neoforge_profile_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = Path("net/neoforged/fmlcore/1.0/fmlcore-1.0.jar")
            target = root / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(b"neoforge-core")
            merged = merge_neoforge_profile_metadata(
                {"id": "1.21.1", "libraries": []},
                {"id": "neoforge-21.1.172", "inheritsFrom": "1.21.1",
                 "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
                 "libraries": [{"name": "net.neoforged:fmlcore:1.0",
                                "url": "https://maven.neoforged.net/releases/"}]},
                root,
            )
            self.assertEqual(merged["id"], "neoforge-21.1.172")

    def test_rejects_profile_for_another_minecraft_version(self):
        with self.assertRaisesRegex(ValueError, "does not inherit"):
            merge_neoforge_profile_metadata(
                {"id": "1.21.1", "libraries": []},
                {"id": "1.21.1-neoforge-21.1.172", "inheritsFrom": "1.20.4",
                 "mainClass": "example.Main", "libraries": []}, Path("."),
            )

    def test_rejects_missing_library(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "library is missing"):
                merge_neoforge_profile_metadata(
                    {"id": "1.21.1", "libraries": []},
                    {"id": "1.21.1-neoforge-21.1.172", "inheritsFrom": "1.21.1",
                     "mainClass": "example.Main",
                     "libraries": [{"name": "net.neoforged:fmlcore:1.0"}]}, Path(directory),
                )

    def test_rejects_untrusted_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "net/example/demo/1.0/demo-1.0.jar"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"demo")
            with self.assertRaisesRegex(ValueError, "repository is not trusted"):
                merge_neoforge_profile_metadata(
                    {"id": "1.21.1", "libraries": []},
                    {"id": "1.21.1-neoforge-21.1.172", "inheritsFrom": "1.21.1",
                     "mainClass": "example.Main",
                     "libraries": [{"name": "net.example:demo:1.0",
                                    "url": "https://evil.example/"}]}, root,
                )

    def test_rejects_explicit_artifact_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "net/neoforged/fmlcore/1.0/fmlcore-1.0.jar"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "SHA-1 mismatch"):
                merge_neoforge_profile_metadata(
                    {"id": "1.21.1", "libraries": []},
                    {"id": "1.21.1-neoforge-21.1.172", "inheritsFrom": "1.21.1",
                     "mainClass": "example.Main",
                     "libraries": [{
                         "name": "net.neoforged:fmlcore:1.0",
                         "downloads": {"artifact": {
                             "path": "net/neoforged/fmlcore/1.0/fmlcore-1.0.jar",
                             "url": "https://maven.neoforged.net/releases/net/neoforged/fmlcore/1.0/fmlcore-1.0.jar",
                             "sha1": hashlib.sha1(b"original").hexdigest(),
                             "size": len(b"changed"),
                         }},
                     }]}, root,
                )


class ModrinthMrpackImportTests(unittest.TestCase):
    def _archive(self, directory, manifest, overrides=None):
        path = Path(directory) / "sample.mrpack"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("modrinth.index.json", json.dumps(manifest))
            for name, payload in (overrides or {}).items():
                package.writestr("overrides/" + name, payload)
        return path

    def _manifest(self, files=None, dependencies=None):
        return {
            "formatVersion": 1, "game": "minecraft", "name": "Test Pack",
            "versionId": "test-1", "files": files or [],
            "dependencies": dependencies or {"minecraft": "1.21.1"},
        }

    def test_imports_client_overrides_into_version_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(
                root, self._manifest(),
                {"config/test.json": b'{"enabled":true}', "options.txt": b"music:0.5"},
            )
            result = install_modrinth_mrpack(archive, root / "voidex")
            instance = root / "voidex" / "instances" / "1.21.1"
            self.assertEqual(result["name"], "Test Pack")
            self.assertEqual(result["files_installed"], 2)
            self.assertEqual((instance / "config/test.json").read_bytes(), b'{"enabled":true}')
            self.assertEqual((instance / "options.txt").read_bytes(), b"music:0.5")

    def test_downloads_and_verifies_manifest_files(self):
        payload = b"sample-mod-jar"
        item = {
            "path": "mods/sample.jar", "downloads": ["https://cdn.modrinth.com/data/test.jar"],
            "fileSize": len(payload),
            "hashes": {"sha1": hashlib.sha1(payload).hexdigest()},
            "env": {"client": "required", "server": "unsupported"},
        }
        opener = Mock()
        opener.open.return_value = io.BytesIO(payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, self._manifest([item]))
            result = install_modrinth_mrpack(
                archive, root / "voidex", opener=opener
            )
            target = root / "voidex" / "instances" / "1.21.1" / "mods" / "sample.jar"
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(result["files_installed"], 1)

    def test_skips_server_only_files(self):
        payload = b"server-only"
        item = {
            "path": "mods/server.jar", "downloads": ["https://cdn.modrinth.com/data/server.jar"],
            "fileSize": len(payload), "hashes": {"sha1": hashlib.sha1(payload).hexdigest()},
            "env": {"client": "unsupported", "server": "required"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, self._manifest([item]))
            result = install_modrinth_mrpack(archive, root / "voidex", opener=Mock())
            self.assertEqual(result["files_installed"], 0)

    def test_rejects_path_traversal_before_writing(self):
        item = {
            "path": "../outside.jar", "downloads": ["https://cdn.modrinth.com/data/test.jar"],
            "fileSize": 4, "hashes": {"sha1": hashlib.sha1(b"test").hexdigest()},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, self._manifest([item]))
            with self.assertRaisesRegex(ValueError, "path is unsafe"):
                install_modrinth_mrpack(archive, root / "voidex", opener=Mock())
            self.assertFalse((root / "outside.jar").exists())

    def test_rejects_untrusted_download_url(self):
        payload = b"test"
        item = {
            "path": "mods/test.jar", "downloads": ["https://evil.example/test.jar"],
            "fileSize": len(payload), "hashes": {"sha1": hashlib.sha1(payload).hexdigest()},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, self._manifest([item]))
            with self.assertRaisesRegex(ValueError, "untrusted download host"):
                install_modrinth_mrpack(archive, root / "voidex", opener=Mock())

    def test_rejects_conflicting_existing_file_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = root / "voidex" / "instances" / "1.21.1"
            instance.mkdir(parents=True)
            (instance / "options.txt").write_text("user settings", encoding="utf-8")
            archive = self._archive(root, self._manifest(), {"options.txt": b"pack settings"})
            with self.assertRaisesRegex(ValueError, "overwrite an existing file"):
                install_modrinth_mrpack(archive, root / "voidex")
            self.assertEqual((instance / "options.txt").read_text(encoding="utf-8"), "user settings")

    def test_rejects_unsupported_format_and_multiple_loaders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = self._archive(root, {**self._manifest(), "formatVersion": 2})
            with self.assertRaisesRegex(ValueError, "Unsupported modpack format"):
                install_modrinth_mrpack(bad, root / "voidex")
            bad_loader = self._archive(
                root, self._manifest(dependencies={
                    "minecraft": "1.21.1", "fabric-loader": "0.16.0", "quilt-loader": "0.29.0",
                })
            )
            with self.assertRaisesRegex(ValueError, "multiple incompatible loaders"):
                install_modrinth_mrpack(bad_loader, root / "voidex")

    def test_rejects_malformed_manifest_and_unsafe_override_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = self._archive(root, {**self._manifest(), "dependencies": []})
            with self.assertRaisesRegex(ValueError, "dependencies are missing"):
                install_modrinth_mrpack(bad, root / "voidex")
            bad_path = root / "unsafe.mrpack"
            with zipfile.ZipFile(bad_path, "w") as package:
                package.writestr("modrinth.index.json", json.dumps(self._manifest()))
                package.writestr("overrides/../escape.txt", b"escape")
            with self.assertRaisesRegex(ValueError, "path is unsafe"):
                install_modrinth_mrpack(bad_path, root / "voidex")

    def test_rejects_zip_symlink_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "symlink.mrpack"
            with zipfile.ZipFile(archive_path, "w") as package:
                package.writestr("modrinth.index.json", json.dumps(self._manifest()))
                info = zipfile.ZipInfo("overrides/link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                package.writestr(info, "target")
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                install_modrinth_mrpack(archive_path, root / "voidex")


    def test_imports_into_selected_named_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voidex = root / "voidex"
            profile_game = voidex / "profiles" / ("a" * 32) / "game"
            profile_game.mkdir(parents=True)
            archive = self._archive(
                root, self._manifest(), {"config/profile.json": b"isolated"}
            )
            result = install_modrinth_mrpack(
                archive, voidex, instance_directory=profile_game,
                instance_game_version="1.21.1",
            )
            self.assertEqual(
                (profile_game / "config/profile.json").read_bytes(), b"isolated"
            )
            self.assertFalse((voidex / "instances" / "1.21.1").exists())
            self.assertEqual(result["instance_directory"], str(profile_game.resolve()))

    def test_rejects_named_instance_version_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voidex = root / "voidex"
            profile_game = voidex / "profiles" / ("b" * 32) / "game"
            profile_game.mkdir(parents=True)
            archive = self._archive(
                root, self._manifest(), {"options.txt": b"pack"}
            )
            with self.assertRaisesRegex(ValueError, "requires 1.21.1"):
                install_modrinth_mrpack(
                    archive, voidex, instance_directory=profile_game,
                    instance_game_version="1.21.4",
                )
            self.assertFalse((profile_game / "options.txt").exists())

    def test_rejects_named_instance_outside_profiles_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voidex = root / "voidex"
            outside = root / "outside"
            outside.mkdir()
            archive = self._archive(root, self._manifest(), {"options.txt": b"pack"})
            with self.assertRaisesRegex(ValueError, "inside VOIDEX profiles"):
                install_modrinth_mrpack(
                    archive, voidex, instance_directory=outside,
                    instance_game_version="1.21.1",
                )
            self.assertFalse((outside / "options.txt").exists())


class QuiltCatalogTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, _size=-1):
            return self.payload

    class Opener:
        def __init__(self, payload):
            self.payload = payload
            self.urls = []
        def open(self, request, timeout=0):
            self.urls.append(request.full_url)
            return QuiltCatalogTests.Response(self.payload)

    def test_filters_loader_builds_and_sorts_stable_first(self):
        payload = json.dumps([
            {"loader": {"version": "0.30.0-beta.1", "stable": False}},
            {"loader": {"version": "0.29.2", "stable": True}},
            {"loader": {"version": "0.29.1", "stable": True}},
        ]).encode()
        opener = self.Opener(payload)
        builds = fetch_quilt_loader_versions("1.21.1", opener=opener)
        self.assertEqual(builds, [
            {"version": "0.29.2", "stable": True},
            {"version": "0.29.1", "stable": True},
            {"version": "0.30.0-beta.1", "stable": False},
        ])
        self.assertEqual(opener.urls[0], "https://meta.quiltmc.org/v3/versions/loader/1.21.1")

    def test_rejects_invalid_game_version_before_network(self):
        opener = Mock()
        with self.assertRaisesRegex(ValueError, "concrete Minecraft"):
            fetch_quilt_loader_versions("../latest", opener=opener)
        opener.open.assert_not_called()

    def test_reads_stable_installer_catalog(self):
        opener = self.Opener(json.dumps([
            {"version": "0.12.2-beta.1", "stable": False},
            {"version": "0.12.1", "stable": True},
        ]).encode())
        self.assertEqual(fetch_quilt_installer_versions(opener=opener), [
            {"version": "0.12.1", "stable": True},
            {"version": "0.12.2-beta.1", "stable": False},
        ])


class QuiltInstallerDownloadTests(unittest.TestCase):
    def test_downloads_and_verifies_quilt_installer(self):
        payload = b"quilt-installer-test"
        opener = Mock()
        opener.open.side_effect = [
            io.BytesIO(hashlib.sha1(payload).hexdigest().encode()),
            io.BytesIO(payload),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = _download_quilt_installer("0.12.1", directory, opener=opener)
            self.assertEqual(path.name, "quilt-installer-0.12.1.jar")
            self.assertEqual(path.read_bytes(), payload)

    def test_rejects_checksum_mismatch_and_cleans_partial_file(self):
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(b"0" * 40), io.BytesIO(b"bad")]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "SHA-1 verification"):
                _download_quilt_installer("0.12.1", directory, opener=opener)
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_rejects_invalid_version_before_network(self):
        opener = Mock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "version is invalid"):
                _download_quilt_installer("../bad", directory, opener=opener)
        opener.open.assert_not_called()


class QuiltProfileMergeTests(unittest.TestCase):
    def test_merges_quilt_profile_and_checks_local_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = Path("org/quiltmc/quilt-loader/0.29.2/quilt-loader-0.29.2.jar")
            target = root / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(b"quilt-loader")
            base = {"id": "1.21.1", "libraries": [],
                    "arguments": {"jvm": ["-Dvanilla=true"], "game": ["--demo"]}}
            profile = {
                "id": "quilt-loader-0.29.2-1.21.1", "inheritsFrom": "1.21.1",
                "mainClass": "org.quiltmc.loader.impl.launch.knot.KnotClient",
                "libraries": [{"name": "org.quiltmc:quilt-loader:0.29.2",
                               "url": "https://maven.quiltmc.org/repository/release/"}],
                "arguments": {"jvm": ["-Dquilt=true"], "game": ["--quilt"]},
            }
            merged = merge_quilt_profile_metadata(base, profile, root)
            self.assertEqual(merged["mainClass"], profile["mainClass"])
            self.assertEqual(merged["arguments"]["jvm"], ["-Dvanilla=true", "-Dquilt=true"])
            self.assertEqual(merged["libraries"][0]["downloads"]["artifact"]["path"], relative.as_posix())

    def test_rejects_wrong_game_inheritance(self):
        with self.assertRaisesRegex(ValueError, "does not inherit"):
            merge_quilt_profile_metadata(
                {"id": "1.21.1", "libraries": []},
                {"id": "quilt-loader-0.29.2-1.21.1", "inheritsFrom": "1.20.4",
                 "mainClass": "example.Main", "libraries": []}, Path("."),
            )

    def test_rejects_missing_quilt_library(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "library is missing"):
                merge_quilt_profile_metadata(
                    {"id": "1.21.1", "libraries": []},
                    {"id": "quilt-loader-0.29.2-1.21.1", "inheritsFrom": "1.21.1",
                     "mainClass": "example.Main",
                     "libraries": [{"name": "org.quiltmc:quilt-loader:0.29.2"}]},
                    Path(directory),
                )

    def test_rejects_untrusted_quilt_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "org/example/demo/1.0/demo-1.0.jar"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"demo")
            with self.assertRaisesRegex(ValueError, "repository is not trusted"):
                merge_quilt_profile_metadata(
                    {"id": "1.21.1", "libraries": []},
                    {"id": "quilt-loader-0.29.2-1.21.1", "inheritsFrom": "1.21.1",
                     "mainClass": "example.Main",
                     "libraries": [{"name": "org.example:demo:1.0",
                                    "url": "https://evil.example/"}]}, root,
                )


class NeoForgeCatalogTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, _size=-1):
            return self.payload

    class Opener:
        def __init__(self, payload):
            self.payload = payload
            self.url = None
        def open(self, request, timeout=0):
            self.url = request.full_url
            return NeoForgeCatalogTests.Response(self.payload)

    def test_filters_maven_versions_for_selected_minecraft_release(self):
        xml = b"<metadata><versioning><versions><version>21.1.172</version><version>21.1.173-beta</version><version>21.1.171</version><version>20.4.240</version><version>21.11.1</version><version>invalid</version></versions></versioning></metadata>"
        opener = self.Opener(xml)
        builds = fetch_neoforge_loader_versions("1.21.1", opener=opener)
        self.assertEqual([item["version"] for item in builds],
                         ["21.1.173-beta", "21.1.172", "21.1.171"])
        self.assertEqual(opener.url,
                         "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml")

    def test_maps_current_minecraft_version_scheme(self):
        xml = b"<metadata><versioning><versions><version>26.1.0.100</version></versions></versioning></metadata>"
        self.assertEqual(fetch_neoforge_loader_versions("26.1", opener=self.Opener(xml))[0]["version"],
                         "26.1.0.100")

    def test_rejects_minecraft_1201_before_network(self):
        opener = Mock()
        with self.assertRaisesRegex(ValueError, "1.20.1"):
            fetch_neoforge_loader_versions("1.20.1", opener=opener)
        opener.open.assert_not_called()

    def test_rejects_malformed_maven_xml(self):
        with self.assertRaisesRegex(ValueError, "invalid Maven metadata"):
            fetch_neoforge_loader_versions("1.21.1", opener=self.Opener(b"<metadata>"))

    def test_rejects_missing_builds_for_selected_game(self):
        xml = b"<metadata><versioning><versions><version>20.4.240</version></versions></versioning></metadata>"
        with self.assertRaisesRegex(ValueError, "no published builds"):
            fetch_neoforge_loader_versions("1.21.1", opener=self.Opener(xml))


class NeoForgeInstallerDownloadTests(unittest.TestCase):
    def test_downloads_and_verifies_sha256_installer(self):
        payload = b"neoforge-installer-test"
        checksum = hashlib.sha256(payload).hexdigest().encode()
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(checksum), io.BytesIO(payload)]
        with tempfile.TemporaryDirectory() as directory:
            path = _download_neoforge_installer("21.1.172", directory, opener=opener)
            self.assertEqual(path.name, "neoforge-21.1.172-installer.jar")
            self.assertEqual(path.read_bytes(), payload)

    def test_rejects_checksum_mismatch_and_cleans_partial_file(self):
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(b"0" * 64), io.BytesIO(b"bad installer")]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "SHA-256 verification"):
                _download_neoforge_installer("21.1.172", directory, opener=opener)
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_rejects_invalid_version_before_network(self):
        opener = Mock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "version is invalid"):
                _download_neoforge_installer("../latest", directory, opener=opener)
        opener.open.assert_not_called()



class InstalledModManagementTests(unittest.TestCase):
    def test_lists_only_regular_jar_files_sorted_by_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "zeta.jar").write_bytes(b"123")
            (root / "Alpha.JAR").write_bytes(b"12")
            (root / "notes.txt").write_text("ignore", encoding="utf-8")
            (root / "folder.jar").mkdir()
            result = list_installed_mods(root)
            self.assertEqual([item["name"] for item in result], ["Alpha.JAR", "zeta.jar"])
            self.assertEqual([item["size"] for item in result], [2, 3])

    def test_missing_mods_directory_is_an_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(list_installed_mods(Path(directory) / "missing"), [])

    def test_removes_only_the_selected_jar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "sodium.jar"
            keep = root / "other.jar"
            target.write_bytes(b"mod")
            keep.write_bytes(b"keep")
            self.assertEqual(remove_installed_mod(root, "sodium.jar"), target.resolve())
            self.assertFalse(target.exists())
            self.assertEqual(keep.read_bytes(), b"keep")

    def test_rejects_paths_and_non_jar_names(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("../outside.jar", "folder\\\\mod.jar", "notes.txt", ""):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    remove_installed_mod(directory, name)

    def test_refuses_symbolic_link_mod_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / (root.name + "-outside.jar")
            outside.write_bytes(b"outside")
            link = root / "linked.jar"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("Symbolic links are not available in this environment.")
            with self.assertRaisesRegex(ValueError, "symbolic-link"):
                remove_installed_mod(root, "linked.jar")
            self.assertEqual(outside.read_bytes(), b"outside")


class LaunchPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.java = self.root / ("java.exe" if os.name == "nt" else "java")
        self.java.write_bytes(b"java")
        self.client = self.root / "client.jar"
        self.client.write_bytes(b"client")
        self.libs = self.root / "libraries"
        self.libs.mkdir()
        self.natives = self.root / "natives"
        self.natives.mkdir()
        self.game = self.root / "game"
        self.game.mkdir()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.auth = {
            "player_name": "PlayerOne",
            "uuid": "12345678-1234-1234-1234-123456789abc",
            "access_token": "explicit-offline-token",
            "user_type": "legacy",
        }
        payload = b"library"
        self.library = {
            "name": "org.example:demo:1.0",
            "downloads": {"artifact": {
                "path": "org/example/demo/1.0/demo-1.0.jar",
                "url": "https://libraries.minecraft.net/org/example/demo/1.0/demo-1.0.jar",
                "sha1": hashlib.sha1(payload).hexdigest(),
                "size": len(payload),
            }},
        }
        target = self.libs / self.library["downloads"]["artifact"]["path"]
        target.parent.mkdir(parents=True)
        target.write_bytes(payload)
        self.metadata = {
            "id": "1.20.4",
            "mainClass": "net.minecraft.client.main.Main",
            "javaVersion": {"majorVersion": 17},
            "assetIndex": {"id": "12"},
            "libraries": [self.library],
            "arguments": {
                "jvm": [
                    "-Djava.library.path=${natives_directory}",
                    "-cp", "${classpath}",
                    {"rules": [{"action": "allow", "os": {"name": "windows"}}],
                     "value": "-Dvoidex.windows=true"},
                ],
                "game": [
                    "--username", "${auth_player_name}",
                    "--uuid", "${auth_uuid}",
                    "--gameDir", "${game_directory}",
                    {"rules": [{"action": "allow", "features": {"has_custom_resolution": True}}],
                     "value": ["--width", "${resolution_width}"]},
                ],
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_classpath_contains_artifact_and_client_but_not_natives(self):
        native_payload = b"native"
        native = {
            "name": "org.example:native:1.0",
            "natives": {"windows": "natives-windows"},
            "downloads": {"classifiers": {"natives-windows": {
                "path": "org/example/native/1.0/native.jar",
                "url": "https://libraries.minecraft.net/org/example/native/1.0/native.jar",
                "sha1": hashlib.sha1(native_payload).hexdigest(),
                "size": len(native_payload),
            }}},
        }
        metadata = dict(self.metadata, libraries=[self.library, native])
        native_path = self.libs / "org/example/native/1.0/native.jar"
        native_path.parent.mkdir(parents=True)
        native_path.write_bytes(native_payload)
        result = build_minecraft_classpath(
            metadata, self.client, self.libs, os_name="windows", arch="x86_64"
        )
        self.assertIn(str(self.client.resolve()), result)
        self.assertIn("demo-1.0.jar", result)
        self.assertNotIn("native.jar", result)
        self.assertIn(";", result)

    def test_missing_required_library_is_rejected(self):
        (self.libs / self.library["downloads"]["artifact"]["path"]).unlink()
        with self.assertRaisesRegex(ValueError, "library is missing"):
            build_minecraft_classpath(
                self.metadata, self.client, self.libs,
                os_name="windows", arch="x86_64"
            )

    def test_modern_arguments_are_substituted_and_rules_applied(self):
        plan = build_launch_plan(
            self.metadata, java_executable=self.java, client_jar=self.client,
            libraries_root=self.libs, natives_directory=self.natives,
            game_directory=self.game, assets_root=self.assets, auth=self.auth,
            java_validation={"compatible": True, "path": str(self.java)},
            os_name="windows", arch="x86_64",
        )
        self.assertEqual(plan["main_class"], "net.minecraft.client.main.Main")
        self.assertIn("PlayerOne", plan["game_arguments"])
        self.assertIn(str(self.game.resolve()), plan["game_arguments"])
        self.assertIn("1280", plan["game_arguments"])
        self.assertIn("-Dvoidex.windows=true", plan["jvm_arguments"])
        self.assertTrue(Path(plan["argv"][0]).samefile(self.java))
        self.assertEqual(plan["cwd"], str(self.game.resolve()))

    def test_argument_rules_exclude_other_platform(self):
        metadata = dict(self.metadata)
        metadata["arguments"] = {
            "jvm": [{"rules": [{"action": "allow", "os": {"name": "linux"}}],
                     "value": "-Dlinux.only=true"}],
            "game": ["--username", "${auth_player_name}"],
        }
        plan = build_launch_plan(
            metadata, java_executable=self.java, client_jar=self.client,
            libraries_root=self.libs, natives_directory=self.natives,
            game_directory=self.game, assets_root=self.assets, auth=self.auth,
            java_validation={"compatible": True, "path": str(self.java)},
            os_name="windows", arch="x86_64",
        )
        self.assertNotIn("-Dlinux.only=true", plan["jvm_arguments"])

    def test_legacy_arguments_are_parsed_without_losing_quoted_values(self):
        metadata = dict(self.metadata)
        metadata.pop("arguments")
        metadata["minecraftArguments"] = '--username "${auth_player_name}" --gameDir "${game_directory}"'
        plan = build_launch_plan(
            metadata, java_executable=self.java, client_jar=self.client,
            libraries_root=self.libs, natives_directory=self.natives,
            game_directory=self.game, assets_root=self.assets, auth=self.auth,
            java_validation={"compatible": True, "path": str(self.java)},
            os_name="linux", arch="x86_64",
        )
        self.assertEqual(plan["game_arguments"][1], "PlayerOne")
        self.assertEqual(plan["game_arguments"][3], str(self.game.resolve()))

    def test_unresolved_placeholder_and_incomplete_auth_are_rejected(self):
        metadata = dict(self.metadata)
        metadata["arguments"] = {"jvm": [], "game": ["${missing_secret}"]}
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            build_launch_plan(
                metadata, java_executable=self.java, client_jar=self.client,
                libraries_root=self.libs, natives_directory=self.natives,
                game_directory=self.game, assets_root=self.assets, auth=self.auth,
                java_validation={"compatible": True, "path": str(self.java)},
                os_name="windows", arch="x86_64",
            )
        with self.assertRaisesRegex(ValueError, "session is incomplete"):
            build_launch_plan(
                self.metadata, java_executable=self.java, client_jar=self.client,
                libraries_root=self.libs, natives_directory=self.natives,
                game_directory=self.game, assets_root=self.assets, auth={"player_name": "PlayerOne"},
                java_validation={"compatible": True, "path": str(self.java)},
                os_name="windows", arch="x86_64",
            )

    def test_java_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            build_launch_plan(
                self.metadata, java_executable=self.java, client_jar=self.client,
                libraries_root=self.libs, natives_directory=self.natives,
                game_directory=self.game, assets_root=self.assets, auth=self.auth,
                java_validation={"compatible": False, "path": str(self.java)},
                os_name="windows", arch="x86_64",
            )


class LaunchIntegrityTests(unittest.TestCase):
    def test_classpath_rejects_library_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"original"
            library = {
                "name": "org.example:tampered:1.0",
                "downloads": {"artifact": {
                    "path": "org/example/tampered/1.0/tampered.jar",
                    "url": "https://libraries.minecraft.net/org/example/tampered/1.0/tampered.jar",
                    "sha1": hashlib.sha1(payload).hexdigest(),
                    "size": len(payload),
                }},
            }
            target = root / "org/example/tampered/1.0/tampered.jar"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"tampered")
            client = root / "client.jar"
            client.write_bytes(b"client")
            with self.assertRaisesRegex(ValueError, "size mismatch|SHA-1 mismatch"):
                build_minecraft_classpath(
                    {"libraries": [library]}, client, root,
                    os_name="windows", arch="x86_64",
                )

    def test_asset_index_objects_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"sound-data"
            digest = hashlib.sha1(payload).hexdigest()
            index = root / "indexes" / "test.json"
            index.parent.mkdir()
            index.write_text(json.dumps({
                "objects": {"sounds/test.ogg": {"hash": digest, "size": len(payload)}}
            }), encoding="utf-8")
            objects = root / "objects" / digest[:2]
            objects.mkdir(parents=True)
            (objects / digest).write_bytes(payload)
            result = verify_cached_asset_objects(index, root / "objects")
            self.assertEqual(result["count"], 1)
            (objects / digest).write_bytes(b"bad")
            with self.assertRaisesRegex(ValueError, "size mismatch|SHA-1 mismatch"):
                verify_cached_asset_objects(index, root / "objects")

    def test_asset_index_rejects_symlinked_object(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"sound-data"
            digest = hashlib.sha1(payload).hexdigest()
            index = root / "index.json"
            index.write_text(json.dumps({
                "objects": {"sounds/test.ogg": {"hash": digest, "size": len(payload)}}
            }), encoding="utf-8")
            objects = root / "objects" / digest[:2]
            objects.mkdir(parents=True)
            outside = root / "outside"
            outside.write_bytes(payload)
            try:
                (objects / digest).symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks are unavailable on this runner.")
            with self.assertRaisesRegex(ValueError, "symbolic-link"):
                verify_cached_asset_objects(index, root / "objects")
