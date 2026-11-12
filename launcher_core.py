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
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
import stat
import zipfile
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
    if os_rule is None:
        os_rule = {}
    if not isinstance(os_rule, dict):
        return False
    if os_rule.get("name") and os_rule["name"] != os_name:
        return False
    if os_rule.get("arch"):
        expected = os_rule["arch"]
        actual = {"x86_64": "amd64", "x86": "x86"}.get(arch, arch)
        if expected not in {actual, arch}:
            return False
    rule_features = rule.get("features") or {}
    if not isinstance(rule_features, dict):
        return False
    for feature, expected in rule_features.items():
        if not isinstance(expected, bool) or features.get(feature, False) != expected:
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
    if not isinstance(path, str) or not path or "\\" in path:
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


class SameHostLibraryRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow library downloads to redirect only over HTTPS to the original host."""

    def __init__(self, original_host):
        super().__init__()
        if not isinstance(original_host, str) or not original_host:
            raise ValueError("A library redirect host is required.")
        self.original_host = original_host.lower()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if (parsed.scheme != "https" or parsed.hostname is None
                or parsed.hostname.lower() != self.original_host
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Library download redirected outside its original HTTPS host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_verified_library(artifact, libraries_root, progress_callback=None, opener=None):
    """Download one resolved library artifact with path, host, size and SHA-1 checks.

    Native classifier JARs are downloaded as ordinary verified files; extraction is
    deliberately a separate step so archive paths can be validated independently.
    """
    if not isinstance(artifact, dict):
        raise ValueError("Invalid library artifact.")
    validated = _validated_library_artifact(
        {key: artifact.get(key) for key in ("path", "url", "sha1", "size")},
        artifact.get("source", "artifact"),
    )
    parsed = urlparse(validated["url"])
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Library download URL must not contain credentials.")

    root = Path(libraries_root).resolve()
    relative = Path(validated["path"])
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Library artifact path escapes the libraries directory.") from exc
    root.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Re-check after directory creation to catch symlinked parent directories.
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Library artifact path escapes the libraries directory.") from exc

    expected_size = validated["size"]
    expected_sha1 = validated["sha1"]

    def is_verified(path):
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha1()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected_sha1

    if is_verified(target):
        if progress_callback:
            progress_callback(expected_size, expected_size)
        return target

    request = urllib.request.Request(
        validated["url"], headers={"User-Agent": "VOIDEX-Launcher/0.5"}
    )
    if opener is None:
        opener = urllib.request.build_opener(
            SameHostLibraryRedirectHandler(parsed.hostname)
        )

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
                    if downloaded > expected_size:
                        raise ValueError("Downloaded library is larger than its declared size.")
                    temporary.write(chunk)
                    digest.update(chunk)
                    if progress_callback:
                        progress_callback(downloaded, expected_size)

        if downloaded != expected_size:
            raise ValueError(
                f"Incomplete library download: expected {expected_size} bytes, received {downloaded}."
            )
        if digest.hexdigest() != expected_sha1:
            raise ValueError("Library artifact SHA-1 verification failed.")
        os.replace(temporary_path, target)
        temporary_path = None
        return target
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def download_verified_libraries(artifacts, libraries_root, progress_callback=None, opener_factory=None):
    """Download a list of resolved artifacts sequentially with aggregate byte progress.

    progress_callback receives (completed_bytes, total_bytes, current_relative_path).
    The operation is intentionally sequential for predictable resource use and simple
    failure handling. Previously verified files are reused by download_verified_library.
    """
    if not isinstance(artifacts, (list, tuple)):
        raise ValueError("Library artifacts must be a list.")
    validated = []
    seen = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("Invalid library artifact in download list.")
        item = _validated_library_artifact(
            {key: artifact.get(key) for key in ("path", "url", "sha1", "size")},
            artifact.get("source", "artifact"),
        )
        previous = seen.get(item["path"])
        if previous is not None:
            if previous != (item["sha1"], item["size"], item["url"]):
                raise ValueError("Conflicting library artifacts use the same destination path.")
            continue
        seen[item["path"]] = (item["sha1"], item["size"], item["url"])
        validated.append({**item, "source": artifact.get("source", "artifact")})

    total = sum(item["size"] for item in validated)
    completed = 0
    results = []
    for item in validated:
        opener = opener_factory(item) if opener_factory else None

        def report(done, _item_total, path=item["path"]):
            if progress_callback:
                progress_callback(completed + done, total, path)

        result = download_verified_library(
            item, libraries_root, progress_callback=report, opener=opener
        )
        completed += item["size"]
        results.append(result)
        if progress_callback:
            progress_callback(completed, total, item["path"])
    return results

ASSET_DOWNLOAD_HOST = "resources.download.minecraft.net"

def resolve_asset_objects(asset_index):
    """Resolve legacy and modern asset-index objects to safe Mojang object downloads."""
    if not isinstance(asset_index, dict):
        raise ValueError("Asset index must be a JSON object.")
    objects = asset_index.get("objects")
    if not isinstance(objects, dict):
        raise ValueError("Asset index has no valid objects map.")
    resolved = {}
    for logical_name, metadata in objects.items():
        if not isinstance(logical_name, str) or not logical_name:
            raise ValueError("Asset index contains an invalid logical asset name.")
        if not isinstance(metadata, dict):
            raise ValueError("Asset index contains invalid object metadata.")
        digest = metadata.get("hash")
        if not isinstance(digest, str) or not _SHA1_RE.fullmatch(digest):
            raise ValueError("Asset index contains an invalid object hash.")
        digest = digest.lower()
        size = metadata.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError("Asset index contains an invalid object size.")
        path = f"{digest[:2]}/{digest}"
        url = f"https://{ASSET_DOWNLOAD_HOST}/{digest[:2]}/{digest}"
        item = {"path": path, "url": url, "sha1": digest, "size": size, "source": "asset"}
        previous = resolved.get(path)
        if previous is not None and previous != item:
            raise ValueError("Conflicting asset objects share the same content hash.")
        resolved[path] = item
    return [resolved[path] for path in sorted(resolved)]


MAX_NATIVE_ARCHIVE_ENTRIES = 20000
MAX_NATIVE_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
MAX_NATIVE_FILE_SIZE = 128 * 1024 * 1024


def _validated_native_member_name(name):
    """Return a safe POSIX archive member path or reject platform-specific escapes."""
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise ValueError("Native archive contains an invalid member path.")
    if name.startswith("/") or name.startswith("//"):
        raise ValueError("Native archive contains an absolute member path.")
    normalized = name[:-1] if name.endswith("/") else name
    parts = normalized.split("/")
    if (not normalized or any(part in {"", ".", ".."} for part in parts)
            or ":" in parts[0]):
        raise ValueError("Native archive member path is unsafe.")
    relative = PurePosixPath(normalized)
    if relative.is_absolute():
        raise ValueError("Native archive contains an absolute member path.")
    return relative


def extract_native_archives(archives, destination,
                            max_entries=MAX_NATIVE_ARCHIVE_ENTRIES,
                            max_total_size=MAX_NATIVE_TOTAL_UNCOMPRESSED,
                            max_file_size=MAX_NATIVE_FILE_SIZE):
    """Safely extract native JARs, rejecting traversal, links, collisions and zip bombs.

    META-INF entries are omitted. Each file is written to a temporary sibling and
    atomically replaced only after its uncompressed byte count matches the ZIP header.
    """
    if not isinstance(archives, (list, tuple)):
        raise ValueError("Native archives must be provided as a list.")
    for limit in (max_entries, max_total_size, max_file_size):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("Native extraction limits must be positive integers.")

    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    planned = []
    total_size = 0
    entry_count = 0
    seen_files = set()

    # Preflight every archive before writing anything.
    for archive_path in archives:
        archive_file = Path(archive_path)
        if not archive_file.is_file():
            raise ValueError(f"Native archive does not exist: {archive_file}")
        try:
            with zipfile.ZipFile(archive_file, "r") as archive:
                for info in archive.infolist():
                    entry_count += 1
                    if entry_count > max_entries:
                        raise ValueError("Native archives exceed the entry-count limit.")
                    name = info.filename
                    relative = _validated_native_member_name(name)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode):
                        raise ValueError("Native archive contains a symbolic link.")
                    if info.flag_bits & 0x1:
                        raise ValueError("Encrypted native archive entries are not supported.")
                    if relative.parts[0].upper() == "META-INF":
                        continue
                    if info.is_dir():
                        continue
                    if info.file_size < 0 or info.file_size > max_file_size:
                        raise ValueError("Native archive contains a file above the size limit.")
                    total_size += info.file_size
                    if total_size > max_total_size:
                        raise ValueError("Native archives exceed the total extraction size limit.")
                    key = relative.as_posix()
                    if key in seen_files:
                        raise ValueError("Native archives contain duplicate output paths.")
                    seen_files.add(key)
                    planned.append((archive_file, info, relative))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Invalid native archive: {archive_file}") from exc

    # Reject a file that would also need to act as a parent directory.
    for key in seen_files:
        parts = PurePosixPath(key).parts
        if any("/".join(parts[:index]) in seen_files for index in range(1, len(parts))):
            raise ValueError("Native archive file/directory path collision detected.")

    extracted = []
    for archive_file, info, relative in planned:
        target = root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve()
        try:
            resolved_parent.relative_to(root)
        except ValueError as exc:
            raise ValueError("Native extraction path escapes its destination.") from exc
        # Re-resolve after mkdir to detect pre-existing or raced symlink parents.
        target = resolved_parent / target.name
        resolved_target = target.resolve()
        try:
            resolved_target.relative_to(root)
        except ValueError as exc:
            raise ValueError("Native extraction target escapes its destination.") from exc
        if target.is_symlink():
            raise ValueError("Native extraction refuses to overwrite a symbolic link.")

        temporary_path = None
        written = 0
        try:
            with zipfile.ZipFile(archive_file, "r") as archive:
                with archive.open(info, "r") as source:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", prefix=target.name + ".", suffix=".part",
                        dir=str(target.parent), delete=False
                    ) as temporary:
                        temporary_path = Path(temporary.name)
                        while True:
                            chunk = source.read(DOWNLOAD_CHUNK_SIZE)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > info.file_size or written > max_file_size:
                                raise ValueError("Native archive entry expanded beyond its declared size.")
                            temporary.write(chunk)
            if written != info.file_size:
                raise ValueError("Native archive entry size does not match its ZIP metadata.")
            os.replace(temporary_path, target)
            temporary_path = None
            extracted.append(target)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    return extracted


_JAVA_VERSION_RE = re.compile(r'version "([^"]+)"')


def discover_java_candidates(extra_paths=None, environ=None, path_value=None, include_system=True):
    """Find likely Java executables without recursively scanning the whole disk."""
    env = os.environ if environ is None else environ
    search_path = env.get("PATH", "") if path_value is None else path_value
    is_windows = os.name == "nt"
    executable_name = "java.exe" if is_windows else "java"
    candidates = []

    def add(value):
        if not value:
            return
        candidate = Path(value).expanduser()
        if candidate.is_dir():
            candidate = candidate / "bin" / executable_name
        try:
            candidate = candidate.resolve()
        except OSError:
            return
        if candidate.is_file() and candidate.name.lower() == executable_name:
            if candidate not in candidates:
                candidates.append(candidate)

    if extra_paths:
        for item in extra_paths:
            add(item)
    java_home = env.get("JAVA_HOME")
    if java_home:
        add(Path(java_home) / "bin" / executable_name)
    add(shutil.which("java", path=search_path))

    if include_system and is_windows:
        roots = [Path(env[key]) for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if env.get(key)]
        vendors = ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "Azul",
                   "Amazon Corretto", "BellSoft", "AdoptOpenJDK")
        for root in roots:
            for vendor in vendors:
                vendor_root = root / vendor
                if vendor_root.is_dir():
                    try:
                        for home in vendor_root.iterdir():
                            add(home / "bin" / "java.exe")
                    except OSError:
                        continue
    elif include_system:
        for root in (Path("/usr/lib/jvm"), Path("/usr/java")):
            if root.is_dir():
                try:
                    for home in root.iterdir():
                        add(home / "bin" / "java")
                except OSError:
                    continue
    return candidates


def validate_java_executable(executable, expected_major=None, timeout=5):
    """Run java -version safely and report the detected major version."""
    path = Path(executable).expanduser()
    if not path.is_file():
        raise ValueError("Java executable does not exist or is not a file.")
    if path.name.lower() not in {"java", "java.exe"}:
        raise ValueError("Selected file is not named java or java.exe.")
    if expected_major is not None and (
        not isinstance(expected_major, int) or isinstance(expected_major, bool)
        or expected_major < 1
    ):
        raise ValueError("Expected Java major version must be a positive integer.")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            [str(path), "-version"], capture_output=True, text=True,
            timeout=timeout, check=False, creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Could not execute Java: {exc}") from exc
    output = (result.stderr or "") + "\n" + (result.stdout or "")
    match = _JAVA_VERSION_RE.search(output)
    if result.returncode != 0 or not match:
        raise ValueError("Java did not return a recognizable version.")
    version_text = match.group(1)
    parts = version_text.split(".")
    try:
        major = int(parts[1] if parts[0] == "1" and len(parts) > 1 else parts[0])
    except (ValueError, IndexError) as exc:
        raise ValueError("Java reported an unrecognized version number.") from exc
    if major < 1:
        raise ValueError("Java reported an invalid major version.")
    return {
        "path": str(path.resolve()), "version": version_text, "major": major,
        "compatible": expected_major is None or major == expected_major,
        "expected_major": expected_major,
    }


# Minecraft launch-plan construction. This stage creates a validated argv list only;
# it deliberately does not spawn a process.
import shlex
import sys


def _runtime_platform():
    if sys.platform.startswith("win"):
        return "windows", "x86_64" if sys.maxsize > 2**32 else "x86"
    if sys.platform == "darwin":
        machine = __import__("platform").machine().lower()
        return "osx", "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
    machine = __import__("platform").machine().lower()
    return "linux", "arm64" if machine in {"arm64", "aarch64"} else ("x86" if sys.maxsize <= 2**32 else "x86_64")


def _launch_rule_matches(rule, os_name, arch, features):
    if not isinstance(rule, dict):
        return False
    os_rule = rule.get("os") or {}
    if not isinstance(os_rule, dict):
        return False
    if os_rule.get("name") and os_rule["name"] != os_name:
        return False
    if os_rule.get("arch"):
        aliases = {"x86_64": "amd64", "x86": "x86"}
        if os_rule["arch"] not in {arch, aliases.get(arch)}:
            return False
    rule_features = rule.get("features") or {}
    if not isinstance(rule_features, dict):
        return False
    return all(isinstance(value, bool) and features.get(key, False) == value
               for key, value in rule_features.items())


def _launch_rules_allow(rules, os_name, arch, features):
    if rules is None:
        return True
    if not isinstance(rules, list):
        raise ValueError("Minecraft argument rules must be a list.")
    allowed = False
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("action") not in {"allow", "disallow"}:
            raise ValueError("Minecraft argument metadata contains an invalid rule.")
        if _launch_rule_matches(rule, os_name, arch, features):
            allowed = rule["action"] == "allow"
    return allowed


def _expand_launch_argument(value, placeholders):
    if not isinstance(value, str):
        raise ValueError("Minecraft launch arguments must be strings.")
    pattern = re.compile(r"\\$\\{([^}]+)\\}")
    expanded = pattern.sub(
        lambda match: str(placeholders[match.group(1)])
        if match.group(1) in placeholders else match.group(0),
        value,
    )
    unresolved = re.findall(r"\\$\\{([^}]+)\\}", expanded)
    if unresolved:
        raise ValueError("Unresolved Minecraft launch placeholder(s): " + ", ".join(sorted(set(unresolved))))
    return expanded


def _collect_argument_values(entries, os_name, arch, features, placeholders):
    if not isinstance(entries, list):
        raise ValueError("Minecraft argument entries must be a list.")
    result = []
    for entry in entries:
        if isinstance(entry, str):
            result.append(_expand_launch_argument(entry, placeholders))
            continue
        if not isinstance(entry, dict):
            raise ValueError("Minecraft argument entry has an unsupported format.")
        if not _launch_rules_allow(entry.get("rules"), os_name, arch, features):
            continue
        value = entry.get("value")
        values = value if isinstance(value, list) else [value]
        for item in values:
            result.append(_expand_launch_argument(item, placeholders))
    return result


def build_minecraft_classpath(version_metadata, client_jar, libraries_root,
                              os_name=None, arch=None, features=None,
                              path_separator=None):
    """Build a verified classpath from present, platform-compatible artifact JARs."""
    if not isinstance(version_metadata, dict):
        raise ValueError("Invalid Minecraft version metadata.")
    if not (os_name and arch):
        os_name, arch = _runtime_platform()
    if os_name not in _LIBRARY_OS_NAMES or arch not in _LIBRARY_ARCHES:
        raise ValueError("Unsupported target platform for Minecraft classpath.")
    library_root = Path(libraries_root).resolve()
    artifacts = resolve_library_artifacts(
        version_metadata, os_name=os_name, arch=arch, features=features or {}
    )
    classpath_items = []
    for artifact in artifacts:
        if artifact["source"] == "native":
            continue
        relative = Path(artifact["path"])
        candidate = (library_root / relative).resolve()
        try:
            candidate.relative_to(library_root)
        except ValueError as exc:
            raise ValueError("Library classpath path escapes the libraries directory.") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"Required Minecraft library is missing: {artifact['path']}")
        classpath_items.append(str(candidate))

    client = Path(client_jar).resolve()
    if not client.is_file():
        raise ValueError("Minecraft client JAR is missing.")
    if client.is_symlink():
        raise ValueError("Minecraft client JAR must not be a symbolic link.")
    classpath_items.append(str(client))
    separator = path_separator if path_separator is not None else (";" if os_name == "windows" else ":")
    if separator not in {";", ":"}:
        raise ValueError("Invalid classpath separator.")
    return separator.join(classpath_items)


def build_launch_plan(version_metadata, *, java_executable, client_jar,
                      libraries_root, natives_directory, game_directory,
                      assets_root, auth, java_validation=None, jvm_arguments=None,
                      resolution=(1280, 720), version_type="release",
                      launcher_name="VOIDEX", launcher_version="0.1",
                      os_name=None, arch=None, features=None):
    """Return a validated process argv/cwd plan; never executes Java.

    Auth must explicitly provide player_name, uuid, access_token and user_type.
    Offline callers pass their chosen offline token explicitly.
    """
    if not isinstance(version_metadata, dict):
        raise ValueError("Invalid Minecraft version metadata.")
    main_class = version_metadata.get("mainClass")
    if not isinstance(main_class, str) or not main_class.strip():
        raise ValueError("Minecraft version metadata has no mainClass.")
    required_auth = ("player_name", "uuid", "access_token", "user_type")
    if not isinstance(auth, dict) or any(not isinstance(auth.get(key), str) or not auth[key]
                                         for key in required_auth):
        raise ValueError("Account session is incomplete; authenticate or provide an explicit offline session.")

    java_major = (version_metadata.get("javaVersion") or {}).get("majorVersion")
    validation = java_validation or validate_java_executable(java_executable, java_major)
    if not validation.get("compatible", False):
        raise ValueError(f"Selected Java runtime does not match required Java {java_major}.")
    java_path = Path(validation.get("path", java_executable)).expanduser().resolve()
    if not java_path.is_file():
        raise ValueError("Validated Java executable is no longer available.")

    if not (os_name and arch):
        os_name, arch = _runtime_platform()
    feature_flags = dict(features or {})
    feature_flags.setdefault("is_demo_user", False)
    feature_flags.setdefault("has_custom_resolution", True)

    client_path = Path(client_jar).expanduser().resolve()
    libraries_path = Path(libraries_root).expanduser().resolve()
    natives_path = Path(natives_directory).expanduser().resolve()
    game_path = Path(game_directory).expanduser().resolve()
    assets_path = Path(assets_root).expanduser().resolve()
    for directory, label in ((natives_path, "Native library directory"),
                             (game_path, "Game directory"), (assets_path, "Assets directory")):
        if not directory.is_dir():
            raise ValueError(f"{label} does not exist: {directory}")

    classpath = build_minecraft_classpath(
        version_metadata, client_path, libraries_path,
        os_name=os_name, arch=arch, features=feature_flags,
    )
    asset_index = version_metadata.get("assetIndex") or {}
    asset_index_name = asset_index.get("id") if isinstance(asset_index, dict) else None
    if not asset_index_name:
        asset_index_name = "legacy"

    width, height = resolution
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (width, height)):
        raise ValueError("Resolution width and height must be positive integers.")
    placeholders = {
        "auth_player_name": auth["player_name"],
        "version_name": str(version_metadata.get("id") or ""),
        "game_directory": str(game_path),
        "assets_root": str(assets_path),
        "assets_index_name": str(asset_index_name),
        "auth_uuid": auth["uuid"].replace("-", ""),
        "auth_access_token": auth["access_token"],
        "user_type": auth["user_type"],
        "version_type": str(version_type),
        "natives_directory": str(natives_path),
        "launcher_name": launcher_name,
        "launcher_version": launcher_version,
        "classpath": classpath,
        "classpath_separator": ";" if os_name == "windows" else ":",
        "library_directory": str(libraries_path),
        "resolution_width": str(width),
        "resolution_height": str(height),
    }
    for optional_key in ("clientid", "auth_xuid"):
        if isinstance(auth.get(optional_key), str) and auth[optional_key]:
            placeholders[optional_key] = auth[optional_key]

    arguments = version_metadata.get("arguments")
    if isinstance(arguments, dict):
        jvm_args = _collect_argument_values(
            arguments.get("jvm", []), os_name, arch, feature_flags, placeholders
        )
        game_args = _collect_argument_values(
            arguments.get("game", []), os_name, arch, feature_flags, placeholders
        )
    else:
        legacy = version_metadata.get("minecraftArguments")
        if not isinstance(legacy, str) or not legacy.strip():
            raise ValueError("Minecraft metadata contains neither modern nor legacy game arguments.")
        try:
            legacy_tokens = shlex.split(legacy, posix=(os_name != "windows"))
        except ValueError as exc:
            raise ValueError(f"Could not parse legacy Minecraft arguments: {exc}") from exc
        game_args = [_expand_launch_argument(token, placeholders) for token in legacy_tokens]
        jvm_args = []

    extra_jvm = jvm_arguments or []
    if not isinstance(extra_jvm, (list, tuple)) or any(not isinstance(arg, str) for arg in extra_jvm):
        raise ValueError("Additional JVM arguments must be a list of strings.")
    jvm_args.extend(extra_jvm)
    if not any(arg.startswith("-Djava.library.path=") for arg in jvm_args):
        jvm_args.append(f"-Djava.library.path={natives_path}")
    if not any(arg in {"-cp", "-classpath"} for arg in jvm_args):
        jvm_args.extend(["-cp", classpath])
    argv = [str(java_path), *jvm_args, main_class, *game_args]
    return {
        "argv": argv,
        "cwd": str(game_path),
        "java": str(java_path),
        "main_class": main_class,
        "classpath": classpath,
        "jvm_arguments": jvm_args,
        "game_arguments": game_args,
        "natives_directory": str(natives_path),
        "assets_root": str(assets_path),
        "assets_index_name": str(asset_index_name),
    }
