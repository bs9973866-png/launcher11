"""Small, dependency-free helpers for Mojang version metadata.

This module deliberately resolves metadata only. It does not download game files or
start a process; those steps will be added after their integrity and lifecycle
handling can be tested independently.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

MANIFEST_HOST = "piston-meta.mojang.com"
_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def fetch_version_metadata(metadata_url: str, timeout: int = 20) -> dict:
    """Fetch a version JSON document from Mojang's metadata host only."""
    parsed = urlparse(metadata_url)
    if parsed.scheme != "https" or parsed.hostname != MANIFEST_HOST:
        raise ValueError("Version metadata URL must use Mojang's HTTPS metadata host.")
    request = urllib.request.Request(
        metadata_url, headers={"User-Agent": "VOIDEX-Launcher/0.3"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Mojang version metadata must be a JSON object.")
    return payload


def summarize_version_metadata(metadata: dict) -> dict:
    """Validate essential client download metadata and return a safe summary."""
    if not isinstance(metadata, dict):
        raise ValueError("Invalid version metadata document.")
    downloads = metadata.get("downloads")
    client = downloads.get("client") if isinstance(downloads, dict) else None
    if not isinstance(client, dict):
        raise ValueError("This version metadata has no client JAR download.")
    url = client.get("url")
    parsed = urlparse(url if isinstance(url, str) else "")
    if parsed.scheme != "https" or parsed.hostname not in {"piston-data.mojang.com", "launcher.mojang.com"}:
        raise ValueError("Client JAR URL is not on Mojang's HTTPS download host.")
    sha1 = client.get("sha1")
    if not isinstance(sha1, str) or not _SHA1_RE.fullmatch(sha1):
        raise ValueError("Client JAR metadata contains an invalid SHA-1 checksum.")
    size = client.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("Client JAR metadata contains an invalid file size.")

    java = metadata.get("javaVersion")
    major = java.get("majorVersion") if isinstance(java, dict) else None
    if major is not None and (not isinstance(major, int) or isinstance(major, bool) or major < 1):
        raise ValueError("Version metadata contains an invalid Java major version.")

    return {
        "java_major": major,
        "client_url": url,
        "client_sha1": sha1.lower(),
        "client_size": size,
        "asset_index": (metadata.get("assetIndex") or {}).get("id")
        if isinstance(metadata.get("assetIndex"), dict) else None,
        "libraries_count": len(metadata.get("libraries", []))
        if isinstance(metadata.get("libraries", []), list) else 0,
    }


CLIENT_DOWNLOAD_HOSTS = {"piston-data.mojang.com", "launcher.mojang.com"}
DOWNLOAD_CHUNK_SIZE = 256 * 1024


class MojangRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent trusted metadata URLs from redirecting to arbitrary hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in CLIENT_DOWNLOAD_HOSTS:
            raise ValueError("Mojang download redirected to an untrusted host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class MojangMetadataRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep version metadata redirects on Mojang's metadata host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in VERSION_METADATA_HOSTS:
            raise ValueError("Mojang metadata redirected to an untrusted host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_verified_client(summary, destination, progress_callback=None, opener=None):
    """Stream a Mojang client JAR to a temp file; verify size/SHA-1 before atomic replace."""
    if not isinstance(summary, dict):
        raise ValueError("Invalid client download summary.")
    url = summary.get("client_url")
    parsed = urlparse(url if isinstance(url, str) else "")
    if parsed.scheme != "https" or parsed.hostname not in CLIENT_DOWNLOAD_HOSTS:
        raise ValueError("Client download URL is not trusted.")
    expected_sha1 = summary.get("client_sha1")
    expected_size = summary.get("client_size")
    if not isinstance(expected_sha1, str) or not _SHA1_RE.fullmatch(expected_sha1):
        raise ValueError("Invalid expected SHA-1 checksum.")
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size <= 0:
        raise ValueError("Invalid expected download size.")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size == expected_size:
        digest = hashlib.sha1()
        with target.open("rb") as existing:
            for chunk in iter(lambda: existing.read(DOWNLOAD_CHUNK_SIZE), b""):
                digest.update(chunk)
        if digest.hexdigest() == expected_sha1.lower():
            if progress_callback:
                progress_callback(expected_size, expected_size)
            return target

    request = urllib.request.Request(url, headers={"User-Agent": "VOIDEX-Launcher/0.3"})
    if opener is None:
        opener = urllib.request.build_opener(MojangRedirectHandler())

    temp_path = None
    digest = hashlib.sha1()
    downloaded = 0
    try:
        with opener.open(request, timeout=30) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=target.name + ".", suffix=".part",
                dir=str(target.parent), delete=False
            ) as temporary:
                temp_path = Path(temporary.name)
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > expected_size:
                        raise ValueError("Downloaded client JAR is larger than Mojang metadata declares.")
                    temporary.write(chunk)
                    digest.update(chunk)
                    if progress_callback:
                        progress_callback(downloaded, expected_size)

        if downloaded != expected_size:
            raise ValueError(
                f"Incomplete client JAR: expected {expected_size} bytes, received {downloaded}."
            )
        if digest.hexdigest() != expected_sha1.lower():
            raise ValueError("Client JAR SHA-1 verification failed.")

        os.replace(temp_path, target)
        temp_path = None
        return target
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


VERSION_METADATA_HOSTS = {MANIFEST_HOST}
MAX_VERSION_JSON_BYTES = 16 * 1024 * 1024


def download_verified_version_json(metadata_url, expected_sha1, destination,
                                   progress_callback=None, opener=None):
    """Download, hash-check and parse a Mojang version JSON before atomic caching."""
    parsed = urlparse(metadata_url if isinstance(metadata_url, str) else "")
    if parsed.scheme != "https" or parsed.hostname not in VERSION_METADATA_HOSTS:
        raise ValueError("Version JSON URL must use Mojang's HTTPS metadata host.")
    if not isinstance(expected_sha1, str) or not _SHA1_RE.fullmatch(expected_sha1):
        raise ValueError("Invalid expected version JSON SHA-1 checksum.")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)

    def validate_file(path):
        digest = hashlib.sha1()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_SIZE), b""):
                size += len(chunk)
                digest.update(chunk)
                if size > MAX_VERSION_JSON_BYTES:
                    raise ValueError("Version JSON exceeds the maximum allowed size.")
        if digest.hexdigest() != expected_sha1.lower():
            raise ValueError("Version JSON SHA-1 verification failed.")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Downloaded version metadata is not valid UTF-8 JSON.") from exc
        if not isinstance(document, dict):
            raise ValueError("Version metadata must be a JSON object.")
        return document

    if target.is_file():
        try:
            validate_file(target)
            if progress_callback:
                progress_callback(target.stat().st_size, target.stat().st_size)
            return target
        except (ValueError, OSError):
            pass

    request = urllib.request.Request(
        metadata_url, headers={"User-Agent": "VOIDEX-Launcher/0.4"}
    )
    if opener is None:
        opener = urllib.request.build_opener(MojangMetadataRedirectHandler())
    temporary_path = None
    digest = hashlib.sha1()
    downloaded = 0
    try:
        with opener.open(request, timeout=30) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=target.name + ".", suffix=".part",
                dir=str(target.parent), delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_VERSION_JSON_BYTES:
                        raise ValueError("Version JSON exceeds the maximum allowed size.")
                    temporary.write(chunk)
                    digest.update(chunk)
                    if progress_callback:
                        progress_callback(downloaded, 0)
        if digest.hexdigest() != expected_sha1.lower():
            raise ValueError("Version JSON SHA-1 verification failed.")
        try:
            document = json.loads(temporary_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Downloaded version metadata is not valid UTF-8 JSON.") from exc
        if not isinstance(document, dict):
            raise ValueError("Version metadata must be a JSON object.")
        os.replace(temporary_path, target)
        temporary_path = None
        if progress_callback:
            progress_callback(downloaded, downloaded)
        return target
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


_LIBRARY_OS_NAMES = {"windows", "linux", "osx"}
_LIBRARY_ARCHES = {"x86": "32", "x86_64": "64", "arm64": "64"}


def _library_rule_matches(rule, os_name, arch, features):
    if not isinstance(rule, dict):
        return False
    os_rule = rule.get("os", {})
    if os_rule and not isinstance(os_rule, dict):
        return False
    if os_rule.get("name") and os_rule["name"] != os_name:
        return False
    if os_rule.get("arch"):
        expected = os_rule["arch"]
        actual = {"x86_64": "amd64", "x86": "x86"}.get(arch, arch)
        if expected not in {actual, arch}:
            return False
    for feature, expected in (rule.get("features") or {}).items():
        if features.get(feature, False) != expected:
            return False
    return True


def _library_allowed(library, os_name, arch, features):
    rules = library.get("rules")
    if rules is None:
        return True
    if not isinstance(rules, list):
        raise ValueError("Library rules must be a list.")
    allowed = False
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("action") not in {"allow", "disallow"}:
            raise ValueError("Library metadata contains an invalid rule.")
        if _library_rule_matches(rule, os_name, arch, features):
            allowed = rule["action"] == "allow"
    return allowed


def _validated_library_artifact(artifact, source):
    if not isinstance(artifact, dict):
        raise ValueError("Library artifact metadata must be an object.")
    path = artifact.get("path")
    if not isinstance(path, str) or not path or "\\\\" in path:
        raise ValueError("Library artifact has an invalid relative path.")
    relative = Path(path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Library artifact path escapes the libraries directory.")
    url = artifact.get("url")
    parsed = urlparse(url if isinstance(url, str) else "")
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Library artifact URL must use HTTPS.")
    sha1 = artifact.get("sha1")
    if not isinstance(sha1, str) or not _SHA1_RE.fullmatch(sha1):
        raise ValueError("Library artifact contains an invalid SHA-1 checksum.")
    size = artifact.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("Library artifact contains an invalid size.")
    return {
        "path": relative.as_posix(),
        "url": url,
        "sha1": sha1.lower(),
        "size": size,
        "source": source,
    }


def resolve_library_artifacts(version_metadata, os_name="windows",
                              arch="x86_64", features=None):
    """Resolve Mojang library artifacts and native classifiers for one target platform."""
    if not isinstance(version_metadata, dict):
        raise ValueError("Invalid version metadata document.")
    if os_name not in _LIBRARY_OS_NAMES or arch not in _LIBRARY_ARCHES:
        raise ValueError("Unsupported target platform for library resolution.")
    libraries = version_metadata.get("libraries", [])
    if not isinstance(libraries, list):
        raise ValueError("Version libraries must be a list.")
    feature_flags = features or {}
    if not isinstance(feature_flags, dict):
        raise ValueError("Library feature flags must be an object.")

    resolved = {}
    for library in libraries:
        if not isinstance(library, dict):
            raise ValueError("Library entry must be an object.")
        if not _library_allowed(library, os_name, arch, feature_flags):
            continue
        downloads = library.get("downloads") or {}
        if not isinstance(downloads, dict):
            raise ValueError("Library downloads metadata must be an object.")
        artifact = downloads.get("artifact")
        if artifact is not None:
            item = _validated_library_artifact(artifact, "artifact")
            resolved[item["path"]] = item

        natives = library.get("natives") or {}
        classifiers = downloads.get("classifiers") or {}
        if not isinstance(natives, dict) or not isinstance(classifiers, dict):
            raise ValueError("Native library metadata must be objects.")
        native_key = natives.get(os_name)
        if native_key:
            if not isinstance(native_key, str):
                raise ValueError("Native classifier name must be a string.")
            classifier_name = native_key.replace("\u0024{arch}", _LIBRARY_ARCHES[arch])
            native_artifact = classifiers.get(classifier_name)
            if native_artifact is not None:
                item = _validated_library_artifact(native_artifact, "native")
                resolved[item["path"]] = item

    return [resolved[path] for path in sorted(resolved)]
