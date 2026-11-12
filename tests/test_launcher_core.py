import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from launcher_core import download_verified_client, summarize_version_metadata


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


if __name__ == "__main__":
    unittest.main()
