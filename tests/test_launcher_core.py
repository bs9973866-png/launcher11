import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import urllib.request

from launcher_core import (download_verified_client, download_verified_library, download_verified_version_json,
                           resolve_library_artifacts, summarize_version_metadata)


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


if __name__ == "__main__":
    unittest.main()
