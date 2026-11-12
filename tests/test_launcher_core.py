import hashlib
import stat
import zipfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import urllib.request

from launcher_core import (download_verified_client, download_verified_library, download_verified_libraries, download_verified_version_json,
                           resolve_asset_objects, resolve_library_artifacts, summarize_version_metadata,\n                           extract_native_archives)


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
            self.assertEqual(result, target)
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
            self.assertEqual(result, [target])
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
