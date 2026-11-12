import unittest

from launcher_core import summarize_version_metadata


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


if __name__ == "__main__":
    unittest.main()
