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
from urllib.parse import urlencode, urlparse, quote

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


# Official Eclipse Temurin runtime installer (Windows x64).
TEMURIN_API_HOST = "api.adoptium.net"
TEMURIN_DOWNLOAD_HOSTS = {
    "github.com", "release-assets.githubusercontent.com",
    "objects.githubusercontent.com", "github-releases.githubusercontent.com",
}
MAX_TEMURIN_METADATA_BYTES = 4 * 1024 * 1024
MAX_TEMURIN_ARCHIVE_BYTES = 700 * 1024 * 1024
MAX_TEMURIN_ENTRIES = 100000
MAX_TEMURIN_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024


class TemurinRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow Temurin archives to redirect only to known GitHub release hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if (parsed.scheme != "https" or parsed.hostname not in TEMURIN_DOWNLOAD_HOSTS
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Temurin download redirected to an untrusted host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_temurin_asset(expected_major, opener=None):
    if (not isinstance(expected_major, int) or isinstance(expected_major, bool)
            or expected_major < 8 or expected_major > 99):
        raise ValueError("Unsupported Java major version requested.")
    url = (
        f"https://{TEMURIN_API_HOST}/v3/assets/latest/{expected_major}/hotspot"
        "?architecture=x64&image_type=jdk&os=windows&vendor=eclipse"
    )
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != TEMURIN_API_HOST:
        raise ValueError("Temurin metadata URL is not trusted.")
    request = urllib.request.Request(url, headers={"User-Agent": "VOIDEX-Launcher/0.5"})
    if opener is None:
        opener = urllib.request.build_opener()
    with opener.open(request, timeout=25) as response:
        payload = response.read(MAX_TEMURIN_METADATA_BYTES + 1)
    if len(payload) > MAX_TEMURIN_METADATA_BYTES:
        raise ValueError("Temurin metadata response is unexpectedly large.")
    try:
        assets = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Temurin returned invalid release metadata.") from exc
    if not isinstance(assets, list) or not assets:
        raise ValueError(f"Eclipse Temurin has no Windows x64 JDK for Java {expected_major}.")
    for release in assets:
        if not isinstance(release, dict):
            continue
        binary = release.get("binary")
        if not isinstance(binary, dict):
            continue
        package = binary.get("package")
        version = release.get("version") or {}
        if (binary.get("os") != "windows" or binary.get("architecture") != "x64"
                or binary.get("image_type") != "jdk" or not isinstance(package, dict)):
            continue
            link = package.get("link")
            parsed_link = urlparse(link if isinstance(link, str) else "")
            checksum = package.get("checksum")
            size = package.get("size")
            name = package.get("name")
            if (parsed_link.scheme != "https" or parsed_link.hostname != "github.com"
                    or parsed_link.username is not None or parsed_link.password is not None):
                continue
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
                continue
            if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_TEMURIN_ARCHIVE_BYTES:
                continue
            if not isinstance(name, str) or not name.lower().endswith(".zip") or Path(name).name != name:
                continue
            major = version.get("major") if isinstance(version, dict) else None
            if major is not None and major != expected_major:
                continue
            return {"url": link, "sha256": checksum.lower(), "size": size, "name": name}
    raise ValueError("Temurin metadata did not contain a valid Windows x64 JDK ZIP package.")


def safe_extract_temurin_zip(archive_path, destination):
    """Extract a Temurin ZIP while rejecting traversal, links and resource bombs."""
    archive_file = Path(archive_path)
    if archive_file.is_symlink() or not archive_file.is_file():
        raise ValueError("Temurin archive is missing or unsafe.")
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    seen = set()
    total_size = 0
    extracted = []
    with zipfile.ZipFile(archive_file, "r") as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_TEMURIN_ENTRIES:
            raise ValueError("Temurin archive has an invalid entry count.")
        for info in infos:
            raw_name = info.filename
            if not isinstance(raw_name, str) or not raw_name or "\\" in raw_name:
                raise ValueError("Temurin archive contains an invalid path.")
            relative = PurePosixPath(raw_name)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("Temurin archive path traversal was rejected.")
            if relative.parts and ":" in relative.parts[0]:
                raise ValueError("Temurin archive contains a drive-qualified path.")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError("Temurin archive contains a symbolic link.")
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted Temurin archive entries are not supported.")
            if info.is_dir():
                continue
            if info.file_size < 0 or info.file_size > 1024 * 1024 * 1024:
                raise ValueError("Temurin archive contains an oversized file.")
            total_size += info.file_size
            if total_size > MAX_TEMURIN_EXTRACTED_BYTES:
                raise ValueError("Temurin archive exceeds the extraction size limit.")
            key = relative.as_posix().casefold()
            if key in seen:
                raise ValueError("Temurin archive contains duplicate output paths.")
            seen.add(key)
            target = root.joinpath(*relative.parts)
            resolved = target.resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ValueError("Temurin extraction path escapes its destination.") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise ValueError("Temurin extraction refuses to overwrite a symbolic link.")
            temporary_path = None
            written = 0
            try:
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
                            if written > info.file_size:
                                raise ValueError("Temurin archive entry expanded beyond its declared size.")
                            temporary.write(chunk)
                if written != info.file_size:
                    raise ValueError("Temurin archive entry size does not match its ZIP metadata.")
                os.replace(temporary_path, target)
                temporary_path = None
                extracted.append(target)
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass
    java_files = [path for path in extracted if path.name.lower() == "java.exe"
                  and path.parent.name.lower() == "bin"]
    if len(java_files) != 1:
        raise ValueError("Temurin archive must contain exactly one bin/java.exe runtime.")
    return java_files[0]


def install_temurin_runtime(expected_major, runtimes_root, progress_callback=None,
                            metadata_opener=None, download_opener=None):
    """Download, SHA-256 verify, safely extract and validate an official Temurin JDK."""
    if os.name != "nt":
        raise ValueError("Automatic Temurin installation currently supports Windows x64 only.")
    runtime_root = Path(runtimes_root).expanduser().resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    target_home = runtime_root / f"temurin-{expected_major}-windows-x64"
    target_java = target_home / "bin" / "java.exe"
    if target_home.is_symlink():
        raise ValueError("Refusing to use a symbolic-link Java runtime directory.")
    if target_java.is_file() and not target_java.is_symlink():
        try:
            return validate_java_executable(target_java, expected_major)["path"]
        except ValueError:
            shutil.rmtree(target_home, ignore_errors=True)

    asset = _fetch_temurin_asset(expected_major, opener=metadata_opener)
    request = urllib.request.Request(
        asset["url"], headers={"User-Agent": "VOIDEX-Launcher/0.5"}
    )
    if download_opener is None:
        download_opener = urllib.request.build_opener(TemurinRedirectHandler())
    with tempfile.TemporaryDirectory(prefix="voidex-temurin-", dir=str(runtime_root)) as temporary:
        work = Path(temporary)
        archive_path = work / asset["name"]
        digest = hashlib.sha256()
        downloaded = 0
        try:
            with download_opener.open(request, timeout=45) as response:
                with archive_path.open("wb") as output:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > asset["size"] or downloaded > MAX_TEMURIN_ARCHIVE_BYTES:
                            raise ValueError("Temurin archive is larger than its published size.")
                        output.write(chunk)
                        digest.update(chunk)
                        if progress_callback:
                            progress_callback(downloaded, asset["size"], "Temurin archive")
        except Exception:
            raise
        if downloaded != asset["size"]:
            raise ValueError(
                f"Incomplete Temurin archive: expected {asset['size']} bytes, received {downloaded}."
            )
        if digest.hexdigest() != asset["sha256"]:
            raise ValueError("Temurin archive SHA-256 verification failed.")
        extracted_root = work / "extracted"
        java_path = safe_extract_temurin_zip(archive_path, extracted_root)
        java_home = java_path.parent.parent
        if not java_home.is_dir() or java_path.is_symlink():
            raise ValueError("Extracted Temurin runtime layout is invalid.")
        if target_home.exists():
            if target_home.is_symlink():
                raise ValueError("Refusing to replace a symbolic-link Java runtime directory.")
            shutil.rmtree(target_home)
        os.replace(java_home, target_home)
    installed_java = target_home / "bin" / "java.exe"
    validation = validate_java_executable(installed_java, expected_major)
    if progress_callback:
        progress_callback(asset["size"], asset["size"], "Java runtime verified")
    return validation["path"]


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
    pattern = re.compile(r"\$\{([^}]+)\}")
    expanded = pattern.sub(
        lambda match: str(placeholders[match.group(1)])
        if match.group(1) in placeholders else match.group(0),
        value,
    )
    unresolved = re.findall(r"\$\{([^}]+)\}", expanded)
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
        lexical_candidate = library_root / relative
        current = lexical_candidate
        while current != library_root:
            if current.is_symlink():
                raise ValueError("Library classpath refuses symbolic-link paths.")
            current = current.parent
        candidate = lexical_candidate.resolve()
        try:
            candidate.relative_to(library_root)
        except ValueError as exc:
            raise ValueError("Library classpath path escapes the libraries directory.") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"Required Minecraft library is missing: {artifact['path']}")
        expected_size = artifact.get("size")
        expected_sha1 = artifact.get("sha1")
        if expected_size is not None and candidate.stat().st_size != expected_size:
            raise ValueError(f"Minecraft library size mismatch: {artifact['path']}")
        if expected_sha1:
            digest = hashlib.sha1()
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != expected_sha1.lower():
                raise ValueError(f"Minecraft library SHA-1 mismatch: {artifact['path']}")
        classpath_items.append(str(candidate))

    client_source = Path(client_jar).expanduser()
    if client_source.is_symlink():
        raise ValueError("Minecraft client JAR must not be a symbolic link.")
    client = client_source.resolve()
    if not client.is_file():
        raise ValueError("Minecraft client JAR is missing.")
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
        optional_value = auth.get(optional_key, "")
        if not isinstance(optional_value, str):
            raise ValueError(f"Authentication field {optional_key} must be a string.")
        # These identifiers are legitimately empty for an offline profile.
        placeholders[optional_key] = optional_value

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


def verify_cached_asset_objects(asset_index_path, objects_root):
    """Verify every content-addressed asset object referenced by an index."""
    index_file = Path(asset_index_path).expanduser()
    if index_file.is_symlink() or not index_file.is_file():
        raise ValueError("Minecraft asset index is missing or unsafe.")
    try:
        with index_file.open("r", encoding="utf-8") as stream:
            index = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read Minecraft asset index: {exc}") from exc
    artifacts = resolve_asset_objects(index)
    root = Path(objects_root).expanduser().resolve()
    for artifact in artifacts:
        relative = Path(artifact["path"])
        candidate_source = root / relative
        current = candidate_source
        while current != root:
            if current.is_symlink():
                raise ValueError("Asset cache refuses symbolic-link paths.")
            current = current.parent
        candidate = candidate_source.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("Asset object path escapes the asset cache.") from exc
        if not candidate.is_file():
            raise ValueError(f"Required Minecraft asset is missing: {artifact['path']}")
        if candidate.stat().st_size != artifact["size"]:
            raise ValueError(f"Minecraft asset size mismatch: {artifact['path']}")
        digest = hashlib.sha1()
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != artifact["sha1"]:
            raise ValueError(f"Minecraft asset SHA-1 mismatch: {artifact['path']}")
    return {"count": len(artifacts), "index": index}


# Modrinth project browser and verified mod downloads.
MODRINTH_API_HOST = "api.modrinth.com"
MODRINTH_CDN_HOSTS = {"cdn.modrinth.com"}
MAX_MODRINTH_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MODRINTH_MOD_BYTES = 250 * 1024 * 1024
_MODRINTH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_MODRINTH_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_MODRINTH_SHA512_RE = re.compile(r"^[0-9a-fA-F]{128}$")


def _modrinth_api_json(path, params=None, opener=None):
    if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/"):
        raise ValueError("Invalid Modrinth API path.")
    query = urlencode(params or {})
    url = f"https://{MODRINTH_API_HOST}{path}" + (f"?{query}" if query else "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != MODRINTH_API_HOST:
        raise ValueError("Modrinth API URL is not trusted.")
    request = urllib.request.Request(
        url, headers={"User-Agent": "VOIDEX-Launcher/0.6 (mod browser)"}
    )
    if opener is None:
        opener = urllib.request.build_opener()
    with opener.open(request, timeout=25) as response:
        payload = response.read(MAX_MODRINTH_RESPONSE_BYTES + 1)
    if len(payload) > MAX_MODRINTH_RESPONSE_BYTES:
        raise ValueError("Modrinth API response is unexpectedly large.")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Modrinth returned invalid JSON.") from exc


def search_modrinth_mods(query, game_version, loader="fabric", limit=30, opener=None):
    """Search Modrinth mod projects compatible with a selected game version and loader."""
    if not isinstance(query, str) or len(query.strip()) > 120:
        raise ValueError("Search text must be at most 120 characters.")
    if not isinstance(game_version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", game_version):
        raise ValueError("Choose a concrete Minecraft release before searching for mods.")
    if loader not in {"fabric", "forge", "neoforge", "quilt"}:
        raise ValueError("Unsupported mod loader filter.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("Modrinth search limit must be between 1 and 50.")
    facets = json.dumps([["project_type:mod"], [f"versions:{game_version}"], [f"categories:{loader}"]])
    payload = _modrinth_api_json(
        "/v2/search",
        {"query": query.strip(), "facets": facets, "limit": str(limit), "index": "relevance"},
        opener=opener,
    )
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        raise ValueError("Modrinth search response did not contain a project list.")
    results = []
    for hit in hits:
        if not isinstance(hit, dict) or hit.get("project_type") != "mod":
            continue
        project_id = hit.get("project_id")
        slug = hit.get("slug")
        title = hit.get("title")
        if not isinstance(project_id, str) or not _MODRINTH_ID_RE.fullmatch(project_id):
            continue
        if not isinstance(slug, str) or not slug or len(slug) > 120:
            continue
        if not isinstance(title, str) or not title or len(title) > 160:
            continue
        versions = hit.get("versions", [])
        if not isinstance(versions, list) or game_version not in versions:
            continue
        results.append({
            "project_id": project_id,
            "slug": slug,
            "title": title,
            "description": str(hit.get("description", ""))[:500],
            "downloads": hit.get("downloads", 0) if isinstance(hit.get("downloads", 0), int) else 0,
            "icon_url": hit.get("icon_url") if isinstance(hit.get("icon_url"), str) else "",
            "game_version": game_version,
            "loader": loader,
        })
    return results


def fetch_modrinth_compatible_version(project_id, game_version, loader="fabric", opener=None):
    if not isinstance(project_id, str) or not _MODRINTH_ID_RE.fullmatch(project_id):
        raise ValueError("Invalid Modrinth project ID.")
    if not isinstance(game_version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", game_version):
        raise ValueError("Choose a concrete Minecraft release.")
    if loader not in {"fabric", "forge", "neoforge", "quilt"}:
        raise ValueError("Unsupported mod loader.")
    payload = _modrinth_api_json(
        f"/v2/project/{project_id}/version",
        {"game_versions": json.dumps([game_version]), "loaders": json.dumps([loader])},
        opener=opener,
    )
    if not isinstance(payload, list):
        raise ValueError("Modrinth version response is invalid.")
    for version in payload:
        if not isinstance(version, dict):
            continue
        if game_version not in version.get("game_versions", []):
            continue
        if loader not in version.get("loaders", []):
            continue
        files = version.get("files")
        if not isinstance(files, list):
            continue
        candidates = []
        for item in files:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            url = item.get("url")
            parsed = urlparse(url if isinstance(url, str) else "")
            size = item.get("size")
            hashes = item.get("hashes")
            if (not isinstance(filename, str) or Path(filename).name != filename
                    or not filename.lower().endswith(".jar")
                    or parsed.scheme != "https" or parsed.hostname not in MODRINTH_CDN_HOSTS
                    or parsed.username is not None or parsed.password is not None
                    or not isinstance(size, int) or isinstance(size, bool)
                    or size <= 0 or size > MAX_MODRINTH_MOD_BYTES
                    or not isinstance(hashes, dict)):
                continue
            sha1 = hashes.get("sha1")
            sha512 = hashes.get("sha512")
            if not ((isinstance(sha1, str) and _MODRINTH_SHA1_RE.fullmatch(sha1))
                    or (isinstance(sha512, str) and _MODRINTH_SHA512_RE.fullmatch(sha512))):
                continue
            candidates.append({
                "filename": filename, "url": url, "size": size,
                "sha1": sha1.lower() if isinstance(sha1, str) else None,
                "sha512": sha512.lower() if isinstance(sha512, str) else None,
                "primary": bool(item.get("primary")),
            })
        if candidates:
            candidates.sort(key=lambda item: not item["primary"])
            return {
                "version_id": version.get("id", ""),
                "version_name": version.get("name", "Mod version"),
                "project_id": project_id,
                "game_version": game_version,
                "loader": loader,
                "file": candidates[0],
            }
    raise ValueError(f"No {loader} build of this mod is available for Minecraft {game_version}.")


FABRIC_META_HOST = "meta.fabricmc.net"
FABRIC_MAVEN_HOST = "maven.fabricmc.net"
MAX_FABRIC_INSTALLER_BYTES = 50 * 1024 * 1024


def _fabric_json(path, opener=None):
    parsed = urlparse("https://" + FABRIC_META_HOST + path)
    if parsed.scheme != "https" or parsed.hostname != FABRIC_META_HOST:
        raise ValueError("Fabric metadata URL is not trusted.")
    request = urllib.request.Request(
        parsed.geturl(), headers={"User-Agent": "VOIDEX-Launcher/0.7"}
    )
    opener = opener or urllib.request.build_opener(SameHostLibraryRedirectHandler(FABRIC_META_HOST))
    with opener.open(request, timeout=25) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Fabric metadata response is too large.")
    payload = json.loads(raw.decode("utf-8"))
    return payload


def fetch_fabric_loader_versions(game_version, opener=None):
    """Return validated Fabric loader builds supported by the selected Minecraft version."""
    if not isinstance(game_version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", game_version):
        raise ValueError("Choose a concrete Minecraft release for Fabric.")
    payload = _fabric_json(
        "/v2/versions/loader/" + quote(game_version, safe=""), opener=opener
    )
    if not isinstance(payload, list):
        raise ValueError("Fabric returned an invalid loader version list.")
    results = []
    for item in payload[:100]:
        loader = item.get("loader") if isinstance(item, dict) else None
        version = loader.get("version") if isinstance(loader, dict) else None
        if (isinstance(version, str) and len(version) <= 64
                and re.fullmatch(r"[0-9A-Za-z.+_-]+", version)):
            results.append({
                "version": version,
                "stable": bool(loader.get("stable", False)),
            })
    if not results:
        raise ValueError(f"Fabric has no compatible loader builds for Minecraft {game_version}.")
    results.sort(key=lambda item: not item["stable"])
    return results




FORGE_PROMOTIONS_URL = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
_FORGE_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


def fetch_forge_loader_versions(game_version, opener=None):
    """Return Forge's official recommended/latest builds for one Minecraft release."""
    if not isinstance(game_version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", game_version):
        raise ValueError("Choose a concrete Minecraft release for Forge.")
    parsed = urlparse(FORGE_PROMOTIONS_URL)
    if parsed.scheme != "https" or parsed.hostname != "files.minecraftforge.net":
        raise ValueError("Forge promotions URL is not trusted.")
    request = urllib.request.Request(
        FORGE_PROMOTIONS_URL, headers={"User-Agent": "VOIDEX-Launcher/0.8"}
    )
    opener = opener or urllib.request.build_opener(
        SameHostLibraryRedirectHandler("files.minecraftforge.net")
    )
    with opener.open(request, timeout=25) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Forge promotions response is too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Forge returned invalid promotions metadata.") from exc
    promotions = payload.get("promos") if isinstance(payload, dict) else None
    if not isinstance(promotions, dict):
        raise ValueError("Forge promotions metadata has no promotions map.")
    results = []
    for suffix, label in (("recommended", "Recommended"), ("latest", "Latest")):
        version = promotions.get(f"{game_version}-{suffix}")
        if not isinstance(version, str) or not _FORGE_VERSION_RE.fullmatch(version):
            continue
        results.append({"version": version, "channel": label})
    if not results:
        raise ValueError(f"Forge has no published recommended/latest build for Minecraft {game_version}.")
    return results


def _download_fabric_installer(version, destination, opener=None, progress_callback=None):
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Fabric installer version is invalid.")
    root = Path(destination).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("Fabric installer cache must not be a symbolic link.")
    root = root.resolve()
    filename = f"fabric-installer-{version}.jar"
    url = f"https://{FABRIC_MAVEN_HOST}/net/fabricmc/fabric-installer/{version}/{filename}"
    opener = opener or urllib.request.build_opener(SameHostLibraryRedirectHandler(FABRIC_MAVEN_HOST))
    checksum_request = urllib.request.Request(url + ".sha1", headers={"User-Agent": "VOIDEX-Launcher/0.7"})
    with opener.open(checksum_request, timeout=25) as response:
        checksum_text = response.read(256).decode("ascii", errors="strict").strip().split()[0]
    if not _SHA1_RE.fullmatch(checksum_text):
        raise ValueError("Fabric installer SHA-1 metadata is invalid.")
    target = root / filename
    if target.is_symlink():
        raise ValueError("Refusing to overwrite a symbolic-link Fabric installer.")
    def valid(path):
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_FABRIC_INSTALLER_BYTES:
            return False
        digest = hashlib.sha1()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest() == checksum_text.lower()
    if valid(target):
        if progress_callback:
            progress_callback(target.stat().st_size, target.stat().st_size)
        return target
    request = urllib.request.Request(url, headers={"User-Agent": "VOIDEX-Launcher/0.7"})
    temporary = None
    digest = hashlib.sha1()
    received = 0
    try:
        with opener.open(request, timeout=45) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=filename + ".", suffix=".part",
                dir=str(root), delete=False
            ) as stream:
                temporary = Path(stream.name)
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_FABRIC_INSTALLER_BYTES:
                        raise ValueError("Fabric installer exceeds the allowed download size.")
                    stream.write(chunk)
                    digest.update(chunk)
                    if progress_callback:
                        progress_callback(received, 0)
        if received == 0 or digest.hexdigest() != checksum_text.lower():
            raise ValueError("Fabric installer SHA-1 verification failed.")
        os.replace(temporary, target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def install_fabric_loader(game_version, loader_version, java_executable, voidex_root,
                          progress_callback=None, opener=None, runner=None):
    """Run the official Fabric client installer and validate its generated launch profile."""
    if not isinstance(game_version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", game_version):
        raise ValueError("Minecraft version is invalid.")
    if not isinstance(loader_version, str) or not re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", loader_version):
        raise ValueError("Fabric loader version is invalid.")
    java = Path(java_executable).expanduser()
    if java.is_symlink() or not java.is_file():
        raise ValueError("Select a valid Java executable before installing Fabric.")
    java_validation = validate_java_executable(java)
    java = Path(java_validation["path"])
    root = Path(voidex_root).expanduser()
    if root.is_symlink():
        raise ValueError("VOIDEX data directory must not be a symbolic link.")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    versions = fetch_fabric_loader_versions(game_version, opener=opener)
    if loader_version not in {item["version"] for item in versions}:
        raise ValueError("The selected Fabric loader build is not available for this Minecraft version.")
    installers = _fabric_json("/v2/versions/installer", opener=opener)
    if not isinstance(installers, list):
        raise ValueError("Fabric installer catalog is invalid.")
    installer_version = next((
        item.get("version") for item in installers
        if isinstance(item, dict) and item.get("stable") is True
        and isinstance(item.get("version"), str)
        and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", item["version"])
    ), None)
    if not installer_version:
        raise ValueError("Fabric did not provide a stable installer release.")
    if progress_callback:
        progress_callback("Downloading official Fabric installer")
    installer = _download_fabric_installer(
        installer_version, root / "cache" / "installers",
        opener=opener,
    )
    if progress_callback:
        progress_callback("Installing Fabric loader")
    command = [
        str(java.resolve()), "-jar", str(installer), "client",
        "-dir", str(root), "-mcversion", game_version, "-loader", loader_version,
    ]
    execute = runner or subprocess.run
    try:
        completed = execute(
            command, cwd=str(root), capture_output=True, text=True,
            timeout=900, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Fabric installer timed out after 15 minutes.") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "No installer output").strip()
        raise ValueError("Fabric installer failed: " + detail[-1800:])
    versions_root = root / "versions"
    if versions_root.is_symlink() or not versions_root.is_dir():
        raise ValueError("Fabric installer did not create a versions directory.")
    expected_profile_id = f"fabric-loader-{loader_version}-{game_version}"
    candidates = []
    for folder in versions_root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or folder.name != expected_profile_id:
            continue
        profile_path = folder / (folder.name + ".json")
        if profile_path.is_symlink() or not profile_path.is_file():
            continue
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(profile, dict) and profile.get("inheritsFrom") == game_version
                and profile.get("id") == folder.name
                and isinstance(profile.get("mainClass"), str)
                and isinstance(profile.get("libraries"), list)):
            candidates.append((folder.name, profile_path))
    if len(candidates) != 1:
        raise ValueError("Fabric installation did not produce one valid profile for the selected Minecraft version.")
    profile_id, profile_path = candidates[0]
    return {
        "game_version": game_version,
        "loader_version": loader_version,
        "profile_id": profile_id,
        "profile_path": str(profile_path),
        "installer_version": installer_version,
        "output": (completed.stdout or "")[-2000:],
    }


def merge_fabric_profile_metadata(base_metadata, profile, libraries_root):
    """Merge an installed Fabric profile into Mojang metadata and verify its local libraries."""
    if not isinstance(base_metadata, dict) or not isinstance(profile, dict):
        raise ValueError("Invalid Minecraft or Fabric profile metadata.")
    base_id = base_metadata.get("id")
    if not isinstance(base_id, str) or profile.get("inheritsFrom") != base_id:
        raise ValueError("Fabric profile does not inherit from the selected Minecraft version.")
    profile_id = profile.get("id")
    if not isinstance(profile_id, str) or not profile_id.startswith("fabric-loader-"):
        raise ValueError("Fabric profile ID is invalid.")
    main_class = profile.get("mainClass")
    if not isinstance(main_class, str) or not main_class.strip():
        raise ValueError("Fabric profile has no client main class.")
    libraries_root = Path(libraries_root).expanduser().resolve()
    base_libraries = base_metadata.get("libraries", [])
    profile_libraries = profile.get("libraries", [])
    if not isinstance(base_libraries, list) or not isinstance(profile_libraries, list):
        raise ValueError("Minecraft or Fabric library metadata is invalid.")
    merged = dict(base_metadata)
    by_name = {
        item.get("name"): dict(item) for item in base_libraries
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for raw in profile_libraries:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise ValueError("Fabric profile contains an invalid library entry.")
        item = dict(raw)
        if not isinstance(item.get("downloads"), dict) or not isinstance(
            item["downloads"].get("artifact"), dict
        ):
            coordinates = item["name"].split("@", 1)
            coordinate = coordinates[0].split(":")
            extension = coordinates[1] if len(coordinates) == 2 else "jar"
            if len(coordinate) not in (3, 4) or not all(coordinate[:3]):
                raise ValueError(f"Unsupported Fabric Maven coordinate: {item['name']}")
            group, artifact, version = coordinate[:3]
            classifier = coordinate[3] if len(coordinate) == 4 else None
            if (not re.fullmatch(r"[A-Za-z0-9_.-]+", group)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+", artifact)
                    or not re.fullmatch(r"[A-Za-z0-9_.+-]+", version)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+", extension)
                    or (classifier and not re.fullmatch(r"[A-Za-z0-9_.-]+", classifier))):
                raise ValueError("Fabric Maven coordinate contains unsafe characters.")
            filename = f"{artifact}-{version}" + (f"-{classifier}" if classifier else "") + f".{extension}"
            relative = Path(*group.split(".")) / artifact / version / filename
            candidate = libraries_root / relative
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"Fabric library is missing; reinstall the loader: {relative.as_posix()}")
            digest = hashlib.sha1()
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_SIZE), b""):
                    digest.update(chunk)
            repository = item.get("url") or f"https://{FABRIC_MAVEN_HOST}/"
            parsed = urlparse(repository)
            if (parsed.scheme != "https" or parsed.hostname not in {
                FABRIC_MAVEN_HOST, "libraries.minecraft.net", "repo1.maven.org",
                "repo.maven.apache.org"
            }):
                raise ValueError("Fabric library repository is not trusted.")
            url = repository.rstrip("/") + "/" + relative.as_posix()
            item["downloads"] = {"artifact": {
                "path": relative.as_posix(), "url": url,
                "sha1": digest.hexdigest(), "size": candidate.stat().st_size,
            }}
        by_name[item["name"]] = item
    merged["id"] = profile_id
    merged["inheritsFrom"] = base_id
    merged["mainClass"] = main_class
    merged["libraries"] = list(by_name.values())
    base_arguments = base_metadata.get("arguments")
    profile_arguments = profile.get("arguments")
    if isinstance(base_arguments, dict) or isinstance(profile_arguments, dict):
        result = {}
        for key in ("jvm", "game"):
            base_values = (base_arguments or {}).get(key, []) if isinstance(base_arguments, dict) else []
            profile_values = (profile_arguments or {}).get(key, []) if isinstance(profile_arguments, dict) else []
            if not isinstance(base_values, list) or not isinstance(profile_values, list):
                raise ValueError("Fabric launch arguments are invalid.")
            result[key] = list(base_values) + list(profile_values)
        merged["arguments"] = result
    return merged




MAX_FORGE_INSTALLER_BYTES = 100 * 1024 * 1024
FORGE_MAVEN_HOST = "maven.minecraftforge.net"


def _download_forge_installer(game_version, forge_version, destination, opener=None,
                              progress_callback=None):
    if (not isinstance(game_version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", game_version)
            or not isinstance(forge_version, str) or not _FORGE_VERSION_RE.fullmatch(forge_version)):
        raise ValueError("Forge installer version is invalid.")
    root = Path(destination).expanduser()
    if root.is_symlink():
        raise ValueError("Forge installer cache must not be a symbolic link.")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    artifact_version = f"{game_version}-{forge_version}"
    filename = f"forge-{artifact_version}-installer.jar"
    url = f"https://{FORGE_MAVEN_HOST}/net/minecraftforge/forge/{artifact_version}/{filename}"
    opener = opener or urllib.request.build_opener(SameHostLibraryRedirectHandler(FORGE_MAVEN_HOST))
    request_headers = {"User-Agent": "VOIDEX-Launcher/0.8"}
    checksum_request = urllib.request.Request(url + ".sha1", headers=request_headers)
    with opener.open(checksum_request, timeout=25) as response:
        checksum_text = response.read(256).decode("ascii", errors="strict").strip().split()[0]
    if not _SHA1_RE.fullmatch(checksum_text):
        raise ValueError("Forge installer SHA-1 metadata is invalid.")
    target = root / filename
    if target.is_symlink():
        raise ValueError("Refusing to overwrite a symbolic-link Forge installer.")

    def valid(path):
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_FORGE_INSTALLER_BYTES:
            return False
        digest = hashlib.sha1()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest() == checksum_text.lower()

    if valid(target):
        if progress_callback:
            progress_callback(target.stat().st_size, target.stat().st_size)
        return target
    temporary = None
    digest = hashlib.sha1()
    received = 0
    try:
        request = urllib.request.Request(url, headers=request_headers)
        with opener.open(request, timeout=45) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=filename + ".", suffix=".part",
                dir=str(root), delete=False
            ) as stream:
                temporary = Path(stream.name)
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_FORGE_INSTALLER_BYTES:
                        raise ValueError("Forge installer exceeds the allowed download size.")
                    stream.write(chunk)
                    digest.update(chunk)
                    if progress_callback:
                        progress_callback(received, 0)
        if received == 0 or digest.hexdigest() != checksum_text.lower():
            raise ValueError("Forge installer SHA-1 verification failed.")
        os.replace(temporary, target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def install_forge_loader(game_version, forge_version, java_executable, voidex_root,
                         progress_callback=None, opener=None, runner=None):
    """Download and run the official Forge client installer, then validate its profile."""
    builds = fetch_forge_loader_versions(game_version, opener=opener)
    if forge_version not in {item["version"] for item in builds}:
        raise ValueError("The selected Forge build is not available for this Minecraft version.")
    java = Path(java_executable).expanduser()
    if java.is_symlink() or not java.is_file():
        raise ValueError("Select a valid Java executable before installing Forge.")
    java_validation = validate_java_executable(java)
    java = Path(java_validation["path"])
    root = Path(voidex_root).expanduser()
    if root.is_symlink():
        raise ValueError("VOIDEX data directory must not be a symbolic link.")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    if progress_callback:
        progress_callback("Downloading official Forge installer")
    installer = _download_forge_installer(
        game_version, forge_version, root / "cache" / "installers",
        opener=opener,
    )
    if progress_callback:
        progress_callback("Installing Forge client profile")
    command = [
        str(java.resolve()), "-jar", str(installer), "--installClient", str(root)
    ]
    execute = runner or subprocess.run
    try:
        completed = execute(
            command, cwd=str(root), capture_output=True, text=True,
            timeout=900, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Forge installer timed out after 15 minutes.") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "No installer output").strip()
        raise ValueError("Forge installer failed: " + detail[-1800:])
    versions_root = root / "versions"
    if versions_root.is_symlink() or not versions_root.is_dir():
        raise ValueError("Forge installer did not create a versions directory.")
    candidates = []
    for folder in versions_root.iterdir():
        if folder.is_symlink() or not folder.is_dir():
            continue
        profile_path = folder / (folder.name + ".json")
        if profile_path.is_symlink() or not profile_path.is_file():
            continue
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(profile, dict) and profile.get("inheritsFrom") == game_version
                and profile.get("id") == folder.name
                and isinstance(profile.get("mainClass"), str)
                and isinstance(profile.get("libraries"), list)
                and folder.name.startswith(f"{game_version}-forge-")):
            candidates.append((folder.name, profile_path))
    if len(candidates) != 1:
        raise ValueError("Forge installation did not produce one valid profile for the selected Minecraft version.")
    profile_id, profile_path = candidates[0]
    return {
        "game_version": game_version, "loader_version": forge_version,
        "profile_id": profile_id, "profile_path": str(profile_path),
        "output": (completed.stdout or "")[-2000:],
    }


def list_installed_mods(mods_directory):
    """List regular JAR files in an instance mods directory without following links."""
    root = Path(mods_directory).expanduser()
    if root.is_symlink():
        raise ValueError("Refusing to inspect a symbolic-link mods directory.")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("Mods path is not a directory.")
    root = root.resolve()
    results = []
    for item in root.iterdir():
        if item.suffix.lower() != ".jar" or item.is_symlink() or not item.is_file():
            continue
        results.append({"name": item.name, "size": item.stat().st_size})
    return sorted(results, key=lambda entry: entry["name"].casefold())


def remove_installed_mod(mods_directory, filename):
    """Remove one regular JAR by its exact filename; never follow symlinks or paths."""
    if (not isinstance(filename, str) or not filename or Path(filename).name != filename
            or "/" in filename or "\\" in filename
            or not filename.lower().endswith(".jar")):
        raise ValueError("Choose a valid mod JAR filename.")
    root = Path(mods_directory).expanduser()
    if root.is_symlink():
        raise ValueError("Refusing to modify a symbolic-link mods directory.")
    if not root.exists() or not root.is_dir():
        raise ValueError("The instance mods directory does not exist.")
    root = root.resolve()
    target = root / filename
    if target.is_symlink():
        raise ValueError("Refusing to remove a symbolic-link mod file.")
    if target.parent.resolve() != root or not target.is_file():
        raise ValueError("The selected mod file no longer exists.")
    target.unlink()
    return target


class ModrinthRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if (parsed.scheme != "https" or parsed.hostname not in MODRINTH_CDN_HOSTS
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Modrinth download redirected to an untrusted host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_verified_modrinth_file(version_info, mods_directory, progress_callback=None, opener=None):
    if not isinstance(version_info, dict) or not isinstance(version_info.get("file"), dict):
        raise ValueError("Invalid Modrinth mod version metadata.")
    item = version_info["file"]
    filename = item.get("filename")
    url = item.get("url")
    parsed = urlparse(url if isinstance(url, str) else "")
    size = item.get("size")
    sha1 = item.get("sha1")
    sha512 = item.get("sha512")
    if (not isinstance(filename, str) or Path(filename).name != filename
            or not filename.lower().endswith(".jar")):
        raise ValueError("Modrinth mod filename is unsafe.")
    if parsed.scheme != "https" or parsed.hostname not in MODRINTH_CDN_HOSTS:
        raise ValueError("Modrinth mod URL is not trusted.")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_MODRINTH_MOD_BYTES:
        raise ValueError("Modrinth mod size is invalid.")
    if not ((isinstance(sha1, str) and _MODRINTH_SHA1_RE.fullmatch(sha1))
            or (isinstance(sha512, str) and _MODRINTH_SHA512_RE.fullmatch(sha512))):
        raise ValueError("Modrinth mod checksum is invalid.")

    root = Path(mods_directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / filename
    if target.is_symlink():
        raise ValueError("Refusing to overwrite a symbolic-link mod file.")

    def matches(path):
        if not path.is_file() or path.stat().st_size != size:
            return False
        if sha512:
            digest = hashlib.sha512()
        else:
            digest = hashlib.sha1()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_SIZE), b""):
                digest.update(chunk)
        expected = sha512 or sha1
        return digest.hexdigest() == expected.lower()

    if matches(target):
        if progress_callback:
            progress_callback(size, size)
        return target

    request = urllib.request.Request(url, headers={"User-Agent": "VOIDEX-Launcher/0.6"})
    if opener is None:
        opener = urllib.request.build_opener(ModrinthRedirectHandler())
    temporary_path = None
    sha1_digest = hashlib.sha1() if sha1 else None
    sha512_digest = hashlib.sha512() if sha512 else None
    downloaded = 0
    try:
        with opener.open(request, timeout=35) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=filename + ".", suffix=".part",
                dir=str(root), delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > size:
                        raise ValueError("Downloaded Modrinth file exceeds its published size.")
                    temporary.write(chunk)
                    if sha1_digest:
                        sha1_digest.update(chunk)
                    if sha512_digest:
                        sha512_digest.update(chunk)
                    if progress_callback:
                        progress_callback(downloaded, size)
        if downloaded != size:
            raise ValueError(f"Incomplete Modrinth download: expected {size}, received {downloaded} bytes.")
        if sha1_digest and sha1_digest.hexdigest() != sha1.lower():
            raise ValueError("Modrinth mod SHA-1 verification failed.")
        if sha512_digest and sha512_digest.hexdigest() != sha512.lower():
            raise ValueError("Modrinth mod SHA-512 verification failed.")
        os.replace(temporary_path, target)
        temporary_path = None
        return target
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
