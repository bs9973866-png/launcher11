from __future__ import annotations

import hashlib
import json
import shlex
import sys
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from launcher_core import (fetch_version_metadata, summarize_version_metadata, download_verified_client,
                            download_verified_version_json, resolve_library_artifacts,
                            download_verified_libraries, resolve_asset_objects, extract_native_archives,
                            discover_java_candidates, validate_java_executable,
                            build_launch_plan, verify_cached_asset_objects,
                            install_temurin_runtime, search_modrinth_mods,
                            fetch_modrinth_compatible_version, download_verified_modrinth_file, list_installed_mods, remove_installed_mod,
                            fetch_fabric_loader_versions, install_fabric_loader, merge_fabric_profile_metadata,
                            fetch_forge_loader_versions)

from PySide6.QtCore import Qt, QSettings, QThread, Signal, QProcess, QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QPlainTextEdit, QStackedWidget, QVBoxLayout,
    QWidget, QFormLayout, QTableWidget, QTableWidgetItem, QMessageBox
)


APP_STYLE = """
QWidget { background: #090713; color: #e9e5f5; font-family: Segoe UI; font-size: 10pt; }
QFrame#Sidebar { background: #0e0b1b; border-right: 1px solid #241b3b; }
QLabel#Brand { color: #f5efff; font-size: 23pt; font-weight: 800; letter-spacing: 3px; }
QLabel#Muted { color: #938aa9; }
QLabel#Heading { color: #ffffff; font-size: 22pt; font-weight: 700; }
QFrame#Panel { background: #121023; border: 1px solid #292044; border-radius: 13px; }
QPushButton { background: #1b1530; border: 1px solid #38275d; border-radius: 9px; padding: 10px 14px; text-align: left; }
QPushButton:hover { background: #282044; border-color: #9b6cff; }
QPushButton:disabled { color: #655d75; border-color: #211b30; }
QPushButton#Primary { background: #7b45dc; border: 1px solid #a77aff; color: white; font-weight: 700; padding: 14px 26px; }
QPushButton#Primary:hover { background: #925cf0; }
QComboBox, QLineEdit { background: #0b0915; border: 1px solid #30264a; border-radius: 7px; padding: 8px; min-height: 20px; }
QPlainTextEdit, QTableWidget { background: #07060d; border: 1px solid #292044; border-radius: 8px; color: #d9d1eb; }
QHeaderView::section { background: #171128; color: #bba9dc; border: 0; padding: 7px; }
QTableWidget::item:selected { background: #493078; }
"""

PROFILES = {
    "Low-end PC": {"ram": 2048, "renderer": "Compatibility", "jvm": "-XX:+UseG1GC -XX:MaxGCPauseMillis=100"},
    "Balanced": {"ram": 4096, "renderer": "Default", "jvm": "-XX:+UseG1GC"},
    "High-end PC": {"ram": 6144, "renderer": "Performance", "jvm": "-XX:+UseG1GC -XX:+ParallelRefProcEnabled"},
}
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def offline_uuid(username: str) -> str:
    """Minecraft-compatible offline profile UUID (UUID v3 / MD5)."""
    digest = bytearray(hashlib.md5(("OfflinePlayer:" + username).encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


class ModrinthSearchWorker(QThread):
    loaded = Signal(list)
    failed = Signal(str)

    def __init__(self, query, game_version, loader, parent=None):
        super().__init__(parent)
        self.query = query
        self.game_version = game_version
        self.loader = loader

    def run(self):
        try:
            self.loaded.emit(search_modrinth_mods(
                self.query, self.game_version, self.loader
            ))
        except Exception as exc:
            self.failed.emit(str(exc))


class ModrinthInstallWorker(QThread):
    progress = Signal(int, int)
    completed = Signal(str, str)
    failed = Signal(str)

    def __init__(self, project, game_version, loader, mods_directory, parent=None):
        super().__init__(parent)
        self.project = project
        self.game_version = game_version
        self.loader = loader
        self.mods_directory = Path(mods_directory)

    def run(self):
        try:
            version = fetch_modrinth_compatible_version(
                self.project["project_id"], self.game_version, self.loader
            )
            path = download_verified_modrinth_file(
                version, self.mods_directory,
                progress_callback=lambda done, total: self.progress.emit(done, total),
            )
            self.completed.emit(str(path), version["version_name"])
        except Exception as exc:
            self.failed.emit(str(exc))


class FabricVersionsWorker(QThread):
    loaded = Signal(list)
    failed = Signal(str)

    def __init__(self, game_version, parent=None):
        super().__init__(parent)
        self.game_version = game_version

    def run(self):
        try:
            self.loaded.emit(fetch_fabric_loader_versions(self.game_version))
        except Exception as exc:
            self.failed.emit(str(exc))



class ForgeVersionsWorker(QThread):
    loaded = Signal(list)
    failed = Signal(str)

    def __init__(self, game_version, parent=None):
        super().__init__(parent)
        self.game_version = game_version

    def run(self):
        try:
            self.loaded.emit(fetch_forge_loader_versions(self.game_version))
        except Exception as exc:
            self.failed.emit(str(exc))


class FabricInstallWorker(QThread):
    stage = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, game_version, loader_version, java_executable, voidex_root, parent=None):
        super().__init__(parent)
        self.game_version = game_version
        self.loader_version = loader_version
        self.java_executable = java_executable
        self.voidex_root = voidex_root

    def run(self):
        try:
            result = install_fabric_loader(
                self.game_version, self.loader_version, self.java_executable,
                self.voidex_root, progress_callback=self.stage.emit,
            )
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class ManifestWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            request = urllib.request.Request(
                MANIFEST_URL,
                headers={"User-Agent": "VOIDEX-Launcher/0.2"}
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.loaded.emit(data)
        except Exception as exc:
            self.failed.emit(str(exc))


class MetadataWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, metadata_url, parent=None):
        super().__init__(parent)
        self.metadata_url = metadata_url

    def run(self):
        try:
            metadata = fetch_version_metadata(self.metadata_url)
            self.loaded.emit(summarize_version_metadata(metadata))
        except Exception as exc:
            self.failed.emit(str(exc))


class ClientDownloadWorker(QThread):
    progress = Signal(int, int)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, summary, destination, parent=None):
        super().__init__(parent)
        self.summary = summary
        self.destination = destination

    def run(self):
        try:
            path = download_verified_client(
                self.summary, self.destination,
                progress_callback=lambda done, total: self.progress.emit(done, total),
            )
            self.completed.emit(str(path))
        except Exception as exc:
            self.failed.emit(str(exc))


class VersionJsonWorker(QThread):
    progress = Signal(int, int)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, metadata_url, expected_sha1, destination, parent=None):
        super().__init__(parent)
        self.metadata_url = metadata_url
        self.expected_sha1 = expected_sha1
        self.destination = destination

    def run(self):
        try:
            path = download_verified_version_json(
                self.metadata_url, self.expected_sha1, self.destination,
                progress_callback=lambda done, total: self.progress.emit(done, total),
            )
            self.completed.emit(str(path))
        except Exception as exc:
            self.failed.emit(str(exc))


class LibraryInstallWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, metadata_url, expected_sha1, version_json_path, libraries_root, parent=None):
        super().__init__(parent)
        self.metadata_url = metadata_url
        self.expected_sha1 = expected_sha1
        self.version_json_path = version_json_path
        self.libraries_root = libraries_root

    def run(self):
        try:
            json_path = download_verified_version_json(
                self.metadata_url, self.expected_sha1, self.version_json_path,
                progress_callback=lambda done, total: self.progress.emit(done, total, "version JSON"),
            )
            with Path(json_path).open("r", encoding="utf-8") as stream:
                version_metadata = json.load(stream)
            artifacts = resolve_library_artifacts(version_metadata, os_name="windows", arch="x86_64")
            if not artifacts:
                self.completed.emit([])
                return
            paths = download_verified_libraries(
                artifacts, self.libraries_root,
                progress_callback=lambda done, total, path: self.progress.emit(done, total, path),
            )
            self.completed.emit([str(path) for path in paths])
        except Exception as exc:
            self.failed.emit(str(exc))


class NativeInstallWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, metadata_url, expected_sha1, version_json_path,
                 libraries_root, natives_root, parent=None):
        super().__init__(parent)
        self.metadata_url = metadata_url
        self.expected_sha1 = expected_sha1
        self.version_json_path = version_json_path
        self.libraries_root = libraries_root
        self.natives_root = natives_root

    def run(self):
        try:
            json_path = download_verified_version_json(
                self.metadata_url, self.expected_sha1, self.version_json_path,
                progress_callback=lambda done, total: self.progress.emit(done, total, "version JSON"),
            )
            with Path(json_path).open("r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            artifacts = resolve_library_artifacts(metadata, os_name="windows", arch="x86_64")
            native_artifacts = [item for item in artifacts if item.get("source") == "native"]
            if not native_artifacts:
                self.completed.emit({"archives": 0, "files": 0, "directory": str(self.natives_root)})
                return
            archives = download_verified_libraries(
                native_artifacts, self.libraries_root,
                progress_callback=lambda done, total, path: self.progress.emit(done, total, path),
            )
            extracted = extract_native_archives(archives, self.natives_root)
            self.completed.emit({
                "archives": len(archives),
                "files": len(extracted),
                "directory": str(self.natives_root),
            })
        except Exception as exc:
            self.failed.emit(str(exc))


class AssetInstallWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, version_metadata_url, version_sha1, version_json_path, assets_root, parent=None):
        super().__init__(parent)
        self.version_metadata_url = version_metadata_url
        self.version_sha1 = version_sha1
        self.version_json_path = version_json_path
        self.assets_root = assets_root

    def run(self):
        try:
            version_path = download_verified_version_json(
                self.version_metadata_url, self.version_sha1, self.version_json_path,
                progress_callback=lambda done, total: self.progress.emit(done, total, "version JSON"),
            )
            with Path(version_path).open("r", encoding="utf-8") as stream:
                version_metadata = json.load(stream)
            asset_ref = version_metadata.get("assetIndex")
            if not isinstance(asset_ref, dict):
                raise ValueError("Selected Minecraft version has no asset index metadata.")
            index_id = asset_ref.get("id")
            if (not isinstance(index_id, str) or not index_id
                    or any(not (char.isalnum() or char in "._-") for char in index_id)):
                raise ValueError("Asset index ID is invalid.")
            index_url = asset_ref.get("url")
            index_sha1 = asset_ref.get("sha1")
            index_path = self.assets_root / "indexes" / f"{index_id}.json"
            self.progress.emit(0, 0, "asset index")
            cached_index = download_verified_version_json(
                index_url, index_sha1, index_path,
                progress_callback=lambda done, total: self.progress.emit(done, total, "asset index"),
            )
            with Path(cached_index).open("r", encoding="utf-8") as stream:
                asset_index = json.load(stream)
            artifacts = resolve_asset_objects(asset_index)
            objects_root = self.assets_root / "objects"
            paths = download_verified_libraries(
                artifacts, objects_root,
                progress_callback=lambda done, total, path: self.progress.emit(done, total, path),
            )
            self.completed.emit({"index": str(cached_index), "count": len(paths)})
        except Exception as exc:
            self.failed.emit(str(exc))


class FullInstallWorker(QThread):
    stage = Signal(str)
    progress = Signal(int, int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, version_id, manifest_entry, parent=None):
        super().__init__(parent)
        self.version_id = version_id
        self.manifest_entry = dict(manifest_entry)

    def run(self):
        try:
            version_id = self.version_id
            home = Path.home() / ".voidex"
            version_json_path = home / "cache" / "versions" / version_id / f"{version_id}.json"
            libraries_root = home / "libraries"
            assets_root = home / "assets"
            client_path = home / "cache" / "clients" / f"{version_id}.jar"
            natives_root = home / "versions" / version_id / "natives"
            game_root = home / "instances" / version_id

            self.stage.emit("Downloading and verifying version metadata")
            json_path = download_verified_version_json(
                self.manifest_entry.get("url"),
                self.manifest_entry.get("sha1"),
                version_json_path,
                progress_callback=lambda done, total: self.progress.emit(done, total, "version JSON"),
            )
            with Path(json_path).open("r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            if metadata.get("id") != version_id:
                raise ValueError("Downloaded version metadata ID does not match the selected version.")

            asset_ref = metadata.get("assetIndex")
            if not isinstance(asset_ref, dict) or not asset_ref.get("id"):
                raise ValueError("This Minecraft version has no supported asset index.")
            if asset_ref.get("virtual") or metadata.get("assets") == "legacy":
                raise ValueError(
                    "This older version requires virtual/legacy asset materialization, which is not supported yet."
                )

            self.stage.emit("Downloading and verifying Minecraft client")
            summary = summarize_version_metadata(metadata)
            client = download_verified_client(
                summary, client_path,
                progress_callback=lambda done, total: self.progress.emit(done, total, "client JAR"),
            )

            self.stage.emit("Resolving and downloading libraries")
            artifacts = resolve_library_artifacts(metadata, os_name="windows", arch="x86_64")
            library_paths = download_verified_libraries(
                artifacts, libraries_root,
                progress_callback=lambda done, total, path: self.progress.emit(done, total, path),
            )

            self.stage.emit("Extracting native libraries")
            native_artifacts = [item for item in artifacts if item.get("source") == "native"]
            native_paths = [libraries_root / item["path"] for item in native_artifacts]
            natives_root.mkdir(parents=True, exist_ok=True)
            extracted = extract_native_archives(native_paths, natives_root) if native_paths else []
            if not any(natives_root.iterdir()):
                (natives_root / ".voidex-ready").write_text("no native archives required", encoding="utf-8")

            self.stage.emit("Downloading and verifying Minecraft assets")
            index_id = asset_ref.get("id")
            if (not isinstance(index_id, str) or not index_id
                    or any(not (char.isalnum() or char in "._-") for char in index_id)):
                raise ValueError("Minecraft asset index ID is invalid.")
            index_path = assets_root / "indexes" / f"{index_id}.json"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            cached_index = download_verified_version_json(
                asset_ref.get("url"), asset_ref.get("sha1"), index_path,
                progress_callback=lambda done, total: self.progress.emit(done, total, "asset index"),
            )
            with Path(cached_index).open("r", encoding="utf-8") as stream:
                asset_index = json.load(stream)
            asset_artifacts = resolve_asset_objects(asset_index)
            asset_paths = download_verified_libraries(
                asset_artifacts, assets_root / "objects",
                progress_callback=lambda done, total, path: self.progress.emit(done, total, path),
            )
            verification = verify_cached_asset_objects(cached_index, assets_root / "objects")

            game_root.mkdir(parents=True, exist_ok=True)
            self.completed.emit({
                "version": version_id,
                "client": str(client),
                "libraries": len(library_paths),
                "native_archives": len(native_paths),
                "native_files": len(extracted),
                "assets": len(asset_paths),
                "verified_assets": verification["count"],
                "game_directory": str(game_root),
                "natives_directory": str(natives_root),
            })
        except Exception as exc:
            self.failed.emit(str(exc))


class JavaCheckWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, executable=None, expected_major=None, parent=None):
        super().__init__(parent)
        self.executable = executable
        self.expected_major = expected_major

    def run(self):
        try:
            candidates = [Path(self.executable)] if self.executable else discover_java_candidates()
            results = []
            for candidate in candidates:
                try:
                    results.append(validate_java_executable(candidate, self.expected_major))
                except ValueError:
                    continue
            if not results:
                raise ValueError("No working Java executable was found. Install Java or enter its full path.")
            selected = next((item for item in results if item["compatible"]), None)
            if selected is None:
                found = ", ".join("Java {} at {}".format(item["version"], item["path"]) for item in results[:3])
                raise ValueError(
                    f"No detected Java runtime matches Minecraft Java {self.expected_major}. "
                    f"Found: {found}. Install the required Java major or choose its java.exe."
                )
            self.completed.emit(selected)
        except Exception as exc:
            self.failed.emit(str(exc))




class TemurinInstallWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, expected_major, destination, parent=None):
        super().__init__(parent)
        self.expected_major = expected_major
        self.destination = Path(destination)

    def run(self):
        try:
            path = install_temurin_runtime(
                self.expected_major, self.destination,
                progress_callback=lambda done, total, stage: self.progress.emit(done, total, stage),
            )
            self.completed.emit(path)
        except Exception as exc:
            self.failed.emit(str(exc))


class LaunchCheckWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, version_id, manifest_entry, java_executable, ram_mb,
                 extra_jvm, account_source, nickname, fabric_profile_id=None, parent=None):
        super().__init__(parent)
        self.version_id = version_id
        self.fabric_profile_id = fabric_profile_id
        self.manifest_entry = dict(manifest_entry)
        self.java_executable = java_executable
        self.ram_mb = ram_mb
        self.extra_jvm = list(extra_jvm)
        self.account_source = account_source
        self.nickname = nickname

    def run(self):
        try:
            if self.account_source != "Offline":
                raise ValueError(
                    f"{self.account_source} login is not implemented yet. "
                    "Switch to Offline or wait for account integration."
                )
            if not self.nickname or len(self.nickname) > 16:
                raise ValueError("Enter a valid offline nickname (1–16 characters).")
            if not all(ch.isalnum() or ch == "_" for ch in self.nickname):
                raise ValueError("Offline nickname may contain only letters, numbers and underscore.")

            home = Path.home() / ".voidex"
            version_json = home / "cache" / "versions" / self.version_id / f"{self.version_id}.json"
            if version_json.is_symlink() or not version_json.is_file():
                raise ValueError("Version JSON is not cached. Use Cache version JSON first.")
            with version_json.open("r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            if metadata.get("id") != self.version_id:
                raise ValueError("Cached version JSON does not match the selected version.")
            if self.fabric_profile_id:
                profile_path = home / "versions" / self.fabric_profile_id / f"{self.fabric_profile_id}.json"
                if profile_path.is_symlink() or not profile_path.is_file():
                    raise ValueError("Installed Fabric profile is missing. Reinstall Fabric from the Mods page.")
                with profile_path.open("r", encoding="utf-8") as stream:
                    fabric_profile = json.load(stream)
                metadata = merge_fabric_profile_metadata(metadata, fabric_profile, home / "libraries")

            client = home / "cache" / "clients" / f"{self.version_id}.jar"
            client_ref = (metadata.get("downloads") or {}).get("client") or {}
            if not client.is_file() or client.is_symlink():
                raise ValueError("Client JAR is missing. Download the client JAR first.")
            if client_ref.get("size") is not None and client.stat().st_size != client_ref["size"]:
                raise ValueError("Client JAR size mismatch; download it again.")
            digest = hashlib.sha1()
            with client.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != str(client_ref.get("sha1", "")).lower():
                raise ValueError("Client JAR SHA-1 mismatch; download it again.")

            libraries = home / "libraries"
            natives = home / "versions" / self.version_id / "natives"
            if natives.is_symlink() or not natives.is_dir() or not any(natives.iterdir()):
                raise ValueError("Native libraries are not prepared. Press Prepare natives first.")

            asset_ref = metadata.get("assetIndex") or {}
            if not isinstance(asset_ref, dict) or not asset_ref.get("id"):
                raise ValueError("This version has no supported asset index.")
            if asset_ref.get("virtual") or metadata.get("assets") == "legacy":
                raise ValueError(
                    "This older version requires virtual/legacy asset materialization, "
                    "which is not implemented yet. Launch is blocked to avoid missing game resources."
                )
            asset_index_path = home / "assets" / "indexes" / f"{asset_ref['id']}.json"
            objects_root = home / "assets" / "objects"
            if asset_ref.get("sha1"):
                index_digest = hashlib.sha1()
                with asset_index_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        index_digest.update(chunk)
                if index_digest.hexdigest().lower() != asset_ref["sha1"].lower():
                    raise ValueError("Asset index SHA-1 mismatch; install assets again.")
            verify_cached_asset_objects(asset_index_path, objects_root)

            expected_java = (metadata.get("javaVersion") or {}).get("majorVersion")
            java_candidates = [Path(self.java_executable)] if self.java_executable else discover_java_candidates()
            java_result = None
            errors = []
            for candidate in java_candidates:
                try:
                    check = validate_java_executable(candidate, expected_java)
                    if check["compatible"]:
                        java_result = check
                        break
                    errors.append(f"{candidate}: Java {check['major']} (requires {expected_java})")
                except ValueError as exc:
                    errors.append(str(exc))
            if java_result is None:
                detail = "; ".join(errors[:3])
                raise ValueError(
                    f"No compatible Java runtime found (requires Java {expected_java}). "
                    + detail
                )

            game_directory = home / "instances" / self.version_id
            game_directory.mkdir(parents=True, exist_ok=True)
            auth = {
                "player_name": self.nickname,
                "uuid": offline_uuid(self.nickname),
                # Offline mode has no online bearer credential. The conventional
                # local placeholder is explicit and never reused for online providers.
                "access_token": "0",
                "user_type": "legacy",
            }
            plan = build_launch_plan(
                metadata,
                java_executable=java_result["path"],
                java_validation=java_result,
                client_jar=client,
                libraries_root=libraries,
                natives_directory=natives,
                game_directory=game_directory,
                assets_root=home / "assets",
                auth=auth,
                jvm_arguments=[f"-Xms512M", f"-Xmx{self.ram_mb}M", *self.extra_jvm],
                version_type=self.manifest_entry.get("type", "release"),
                launcher_name="VOIDEX",
                launcher_version="0.3",
            )
            self.ready.emit(plan)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("VOIDEX", "Launcher")
        self.manifest_worker = None
        self.metadata_worker = None
        self.download_worker = None
        self.version_json_worker = None
        self.library_worker = None
        self.asset_worker = None
        self.native_worker = None
        self.full_install_worker = None
        self.java_worker = None
        self.launch_worker = None
        self.minecraft_process = None
        self.fabric_versions_worker = None
        self.forge_versions_worker = None
        self.fabric_install_worker = None
        self.fabric_profile_id = self.settings.value("fabric_profile_id", "")
        self.fabric_profile_game_version = self.settings.value("fabric_profile_game_version", "")
        self.selected_metadata = None
        self.selected_metadata_version = None
        self.versions = []
        self.selected_version = self.settings.value("selected_version", "latest-release")
        self.setWindowTitle("VOIDEX Launcher")
        self.resize(1180, 740)
        self.setMinimumSize(900, 600)
        self._build_ui()
        self._restore_settings()
        self._refresh_identity()
        self.log("VOIDEX initialized. Background polling and idle animation timers are disabled.")
        self.log("Version catalog connects to Mojang only when you open Versions or press Refresh.")

    def _build_ui(self):
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(215)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 24, 18, 20)
        brand = QLabel("VOIDEX")
        brand.setObjectName("Brand")
        side.addWidget(brand)
        tagline = QLabel("ENTER THE VOID")
        tagline.setObjectName("Muted")
        side.addWidget(tagline)
        side.addSpacing(35)

        self.pages = QStackedWidget()
        self.page_names = ["Overview", "Versions", "Mods", "Accounts", "Settings", "Console"]
        self.nav_buttons = []
        for index, name in enumerate(self.page_names):
            button = QPushButton(name)
            button.clicked.connect(lambda checked=False, i=index: self._navigate(i))
            self.nav_buttons.append(button)
            side.addWidget(button)
        side.addStretch()
        version_label = QLabel("LIGHTWEIGHT BUILD  •  PREVIEW")
        version_label.setObjectName("Muted")
        side.addWidget(version_label)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 24, 28, 24)
        content_layout.addWidget(self.pages)

        self.pages.addWidget(self._overview_page())
        self.pages.addWidget(self._versions_page())
        self.pages.addWidget(self._modrinth_page())
        self.pages.addWidget(self._accounts_page())
        self.pages.addWidget(self._settings_page())
        self.pages.addWidget(self._console_page())

        outer.addWidget(sidebar)
        outer.addWidget(content, 1)
        self.setCentralWidget(root)

    def _navigate(self, index):
        self.pages.setCurrentIndex(index)
        if index == 1 and not self.versions:
            self.refresh_versions()
        if index == 2:
            if __import__("re").fullmatch(r"\d+\.\d+(?:\.\d+)?", str(self.selected_version)):
                self.modrinth_game_version.setText(str(self.selected_version))
            self.refresh_installed_mods()

    def _panel(self, title: str, subtitle: str = ""):
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 18, 20, 18)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 14pt; font-weight: 700;")
        layout.addWidget(heading)
        if subtitle:
            note = QLabel(subtitle)
            note.setObjectName("Muted")
            note.setWordWrap(True)
            layout.addWidget(note)
        return panel, layout

    def _overview_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("Welcome to the VOID")
        title.setObjectName("Heading")
        layout.addWidget(title)
        subtitle = QLabel("Your Minecraft. Your universe. Your performance.")
        subtitle.setObjectName("Muted")
        layout.addWidget(subtitle)
        layout.addSpacing(18)
        hero, hero_layout = self._panel("READY FOR TAKEOFF", "A quiet launcher that only performs network and game work when requested.")
        self.profile_badge = QLabel("Performance preset: Balanced")
        self.profile_badge.setObjectName("Muted")
        hero_layout.addWidget(self.profile_badge)
        self.home_version = QLabel(f"Selected version: {self.selected_version}")
        self.home_version.setObjectName("Muted")
        hero_layout.addWidget(self.home_version)
        self.launch_button = QPushButton("LAUNCH MINECRAFT")
        self.launch_button.setObjectName("Primary")
        self.launch_button.clicked.connect(self.launch_selected_game)
        hero_layout.addWidget(self.launch_button)
        self.stop_button = QPushButton("STOP MINECRAFT")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_minecraft)
        hero_layout.addWidget(self.stop_button)
        self.launch_status = QLabel("Select a version, install its files, then launch.")
        self.launch_status.setObjectName("Muted")
        self.launch_status.setWordWrap(True)
        hero_layout.addWidget(self.launch_status)
        layout.addWidget(hero)
        account, account_layout = self._panel("PLAYER IDENTITY", "Offline profile UUID is generated locally. Online account providers are not connected yet.")
        self.account_summary = QLabel("Source: Offline  |  UUID: pending nickname")
        self.account_summary.setObjectName("Muted")
        account_layout.addWidget(self.account_summary)
        layout.addWidget(account)
        layout.addStretch()
        return page

    def _placeholder_page(self, title, text):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel(title)
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        panel, panel_layout = self._panel("DEVELOPMENT STATUS", text)
        layout.addWidget(panel)
        layout.addStretch()
        return page

    def _modrinth_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Modrinth mod browser")
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        note = QLabel(
            "Search real Modrinth projects and install checksum-verified JAR files into the selected instance."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        controls = QHBoxLayout()
        self.modrinth_search = QLineEdit()
        self.modrinth_search.setPlaceholderText("Search mods, e.g. Sodium")
        self.modrinth_game_version = QLineEdit()
        self.modrinth_game_version.setPlaceholderText("Minecraft version, e.g. 1.21.4")
        self.modrinth_game_version.setText(
            self.selected_version if __import__("re").fullmatch(
                r"\d+\.\d+(?:\.\d+)?", str(self.selected_version)
            ) else ""
        )
        self.modrinth_loader = QComboBox()
        self.modrinth_loader.addItem("Fabric", "fabric")
        self.modrinth_loader.addItem("Forge", "forge")
        self.modrinth_loader.addItem("NeoForge", "neoforge")
        self.modrinth_loader.addItem("Quilt", "quilt")
        fabric_row = QHBoxLayout()
        self.fabric_loader_version = QComboBox()
        self.fabric_loader_version.setMinimumWidth(150)
        self.fabric_versions_button = QPushButton("Load Fabric builds")
        self.fabric_versions_button.clicked.connect(self.load_fabric_versions)
        self.fabric_install_button = QPushButton("Install Fabric loader")
        self.fabric_install_button.clicked.connect(self.install_selected_fabric)
        self.fabric_status = QLabel("Fabric loader can be installed here.")
        self.fabric_status.setObjectName("Muted")
        self.fabric_status.setWordWrap(True)
        fabric_row.addWidget(QLabel("Fabric build"))
        fabric_row.addWidget(self.fabric_loader_version)
        fabric_row.addWidget(self.fabric_versions_button)
        fabric_row.addWidget(self.fabric_install_button)
        layout.addLayout(fabric_row)
        layout.addWidget(self.fabric_status)
        self.modrinth_search_button = QPushButton("Search Modrinth")
        self.modrinth_search_button.clicked.connect(self.search_modrinth)
        controls.addWidget(self.modrinth_search, 2)
        controls.addWidget(self.modrinth_game_version)
        controls.addWidget(self.modrinth_loader)
        controls.addWidget(self.modrinth_search_button)
        layout.addLayout(controls)

        self.modrinth_table = QTableWidget(0, 3)
        self.modrinth_table.setHorizontalHeaderLabels(["Project", "Downloads", "Description"])
        self.modrinth_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.modrinth_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.modrinth_table.horizontalHeader().setStretchLastSection(True)
        self.modrinth_table.setColumnWidth(0, 220)
        self.modrinth_table.setColumnWidth(1, 100)
        layout.addWidget(self.modrinth_table, 1)

        actions = QHBoxLayout()
        self.modrinth_status = QLabel("Choose a Minecraft version and loader, then search.")
        self.modrinth_status.setObjectName("Muted")
        self.modrinth_status.setWordWrap(True)
        self.modrinth_install_button = QPushButton("Install selected mod")
        self.modrinth_install_button.setEnabled(False)
        self.modrinth_install_button.clicked.connect(self.install_selected_modrinth)
        actions.addWidget(self.modrinth_status, 1)
        actions.addWidget(self.modrinth_install_button)
        self.modrinth_refresh_installed_button = QPushButton("Refresh installed")
        self.modrinth_refresh_installed_button.clicked.connect(self.refresh_installed_mods)
        self.modrinth_remove_button = QPushButton("Remove selected")
        self.modrinth_remove_button.setEnabled(False)
        self.modrinth_remove_button.clicked.connect(self.remove_selected_installed_mod)
        actions.addWidget(self.modrinth_refresh_installed_button)
        actions.addWidget(self.modrinth_remove_button)
        layout.addLayout(actions)

        installed_heading = QLabel("Installed mods in this instance")
        installed_heading.setStyleSheet("font-size: 13pt; font-weight: 700;")
        layout.addWidget(installed_heading)
        self.installed_mods_table = QTableWidget(0, 2)
        self.installed_mods_table.setHorizontalHeaderLabels(["File", "Size"])
        self.installed_mods_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.installed_mods_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.installed_mods_table.horizontalHeader().setStretchLastSection(True)
        self.installed_mods_table.itemSelectionChanged.connect(
            lambda: self.modrinth_remove_button.setEnabled(self.installed_mods_table.currentRow() >= 0)
        )
        layout.addWidget(self.installed_mods_table, 1)

        warning = QLabel(
            "Fabric installation is supported; official Forge build discovery is available. Forge installation, NeoForge and Quilt are not available yet. "
            "Mods must match the selected Minecraft version and installed loader."
        )
        warning.setObjectName("Muted")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        return page


    def load_fabric_versions(self):
        if self.fabric_versions_worker and self.fabric_versions_worker.isRunning():
            return
        game_version = self.modrinth_game_version.text().strip()
        self.fabric_versions_button.setEnabled(False)
        self.fabric_status.setText(f"Loading Fabric builds for Minecraft {game_version}...")
        self.fabric_versions_worker = FabricVersionsWorker(game_version, self)
        self.fabric_versions_worker.loaded.connect(self._fabric_versions_loaded)
        self.fabric_versions_worker.failed.connect(self._fabric_versions_failed)
        self.fabric_versions_worker.finished.connect(self._fabric_versions_finished)
        self.fabric_versions_worker.start()

    def _fabric_versions_loaded(self, versions):
        self.fabric_loader_version.clear()
        for item in versions:
            label = item["version"] + ("" if item["stable"] else " (unstable)")
            self.fabric_loader_version.addItem(label, item["version"])
        self.fabric_status.setText(
            f"Found {len(versions)} Fabric build(s). Stable builds are listed first by Fabric."
        )

    def _fabric_versions_failed(self, error):
        self.fabric_status.setText(f"Fabric version lookup failed: {error}")
        self.log(f"Fabric version lookup failed: {error}")

    def _fabric_versions_finished(self):
        self.fabric_versions_button.setEnabled(True)
        self.fabric_versions_worker = None


    def load_forge_versions(self):
        if self.forge_versions_worker and self.forge_versions_worker.isRunning():
            return
        game_version = self.modrinth_game_version.text().strip()
        self.forge_versions_button.setEnabled(False)
        self.forge_status.setText(f"Loading official Forge builds for Minecraft {game_version}...")
        self.forge_versions_worker = ForgeVersionsWorker(game_version, self)
        self.forge_versions_worker.loaded.connect(self._forge_versions_loaded)
        self.forge_versions_worker.failed.connect(self._forge_versions_failed)
        self.forge_versions_worker.finished.connect(self._forge_versions_finished)
        self.forge_versions_worker.start()

    def _forge_versions_loaded(self, versions):
        self.forge_loader_version.clear()
        for item in versions:
            self.forge_loader_version.addItem(
                f"{item['version']} ({item['channel']})", item["version"]
            )
        self.forge_status.setText(
            f"Found {len(versions)} official Forge build(s). Recommended builds are listed first."
        )

    def _forge_versions_failed(self, error):
        self.forge_status.setText(f"Forge catalog lookup failed: {error}")
        self.log(f"Forge catalog lookup failed: {error}")

    def _forge_versions_finished(self):
        self.forge_versions_button.setEnabled(True)
        self.forge_versions_worker = None

    def install_selected_fabric(self):
        if self.fabric_install_worker and self.fabric_install_worker.isRunning():
            return
        game_version = self.modrinth_game_version.text().strip()
        loader_version = self.fabric_loader_version.currentData()
        java_executable = self.java_path.text().strip()
        if not loader_version:
            self.fabric_status.setText("Load and select a Fabric build first.")
            return
        if not java_executable:
            self.fabric_status.setText("Set a Java executable in Settings before installing Fabric.")
            self._navigate(4)
            return
        if game_version != self.selected_version:
            QMessageBox.information(
                self, "VOIDEX",
                "The Mods page version must match the selected Minecraft version. Select it on the Versions page first."
            )
            return
        self.fabric_install_button.setEnabled(False)
        self.fabric_versions_button.setEnabled(False)
        self.fabric_status.setText("Preparing official Fabric installer...")
        self.fabric_install_worker = FabricInstallWorker(
            game_version, loader_version, java_executable,
            Path.home() / ".voidex", self,
        )
        self.fabric_install_worker.stage.connect(self._fabric_install_stage)
        self.fabric_install_worker.completed.connect(self._fabric_install_completed)
        self.fabric_install_worker.failed.connect(self._fabric_install_failed)
        self.fabric_install_worker.finished.connect(self._fabric_install_finished)
        self.fabric_install_worker.start()
        self.log(f"Installing Fabric {loader_version} for Minecraft {game_version}.")

    def _fabric_install_stage(self, stage):
        self.fabric_status.setText(stage + "...")
        self.log(stage + ".")

    def _fabric_install_completed(self, result):
        self.fabric_profile_id = result["profile_id"]
        self.fabric_profile_game_version = result["game_version"]
        self.settings.setValue("fabric_profile_id", self.fabric_profile_id)
        self.settings.setValue("fabric_profile_game_version", self.fabric_profile_game_version)
        self.fabric_status.setText(
            f"Fabric {result['loader_version']} installed for Minecraft {result['game_version']}."
        )
        self.log(
            f"Fabric profile ready: {result['profile_id']} (installer {result['installer_version']})."
        )
        self.launch_status.setText(
            f"Fabric profile installed for {result['game_version']}. Launch uses it when that version is selected."
        )

    def _fabric_install_failed(self, error):
        self.fabric_status.setText(f"Fabric installation failed: {error}")
        self.log(f"Fabric installation failed: {error}")

    def _fabric_install_finished(self):
        self.fabric_install_button.setEnabled(True)
        self.fabric_versions_button.setEnabled(True)
        self.fabric_install_worker = None

    def refresh_installed_mods(self):
        version = self.modrinth_game_version.text().strip()
        if not __import__("re").fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            self.modrinth_status.setText("Enter a valid Minecraft version to inspect its mods folder.")
            return
        directory = Path.home() / ".voidex" / "instances" / version / "mods"
        try:
            installed = list_installed_mods(directory)
        except (OSError, ValueError) as exc:
            self.modrinth_status.setText(f"Could not inspect installed mods: {exc}")
            self.log(f"Installed-mod scan failed: {exc}")
            return
        self.installed_mods_table.setRowCount(len(installed))
        for row, item in enumerate(installed):
            name = QTableWidgetItem(item["name"])
            name.setData(Qt.ItemDataRole.UserRole, item["name"])
            self.installed_mods_table.setItem(row, 0, name)
            size = item["size"]
            label = f"{size / (1024 * 1024):.2f} MiB" if size >= 1024 * 1024 else f"{size / 1024:.1f} KiB"
            self.installed_mods_table.setItem(row, 1, QTableWidgetItem(label))
        self.installed_mods_table.resizeColumnsToContents()
        self.modrinth_remove_button.setEnabled(False)
        self.modrinth_status.setText(
            f"{len(installed)} installed mod JAR(s) in Minecraft {version}."
        )

    def remove_selected_installed_mod(self):
        row = self.installed_mods_table.currentRow()
        if row < 0:
            return
        item = self.installed_mods_table.item(row, 0)
        filename = item.data(Qt.ItemDataRole.UserRole) if item else None
        version = self.modrinth_game_version.text().strip()
        if not __import__("re").fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            self.modrinth_status.setText("Enter a valid Minecraft version first.")
            return
        answer = QMessageBox.question(
            self, "Remove mod",
            f"Remove {filename} from Minecraft {version}? This only deletes the selected JAR.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        directory = Path.home() / ".voidex" / "instances" / version / "mods"
        try:
            remove_installed_mod(directory, filename)
        except (OSError, ValueError) as exc:
            self.modrinth_status.setText(f"Could not remove mod: {exc}")
            self.log(f"Mod removal failed: {exc}")
            return
        self.log(f"Removed installed mod {filename} from Minecraft {version}.")
        self.refresh_installed_mods()

    def search_modrinth(self):
        if self.modrinth_search_worker and self.modrinth_search_worker.isRunning():
            return
        game_version = self.modrinth_game_version.text().strip()
        query = self.modrinth_search.text().strip()
        loader = self.modrinth_loader.currentData()
        self.modrinth_search_button.setEnabled(False)
        self.modrinth_install_button.setEnabled(False)
        self.modrinth_status.setText("Searching Modrinth...")
        self.modrinth_search_worker = ModrinthSearchWorker(query, game_version, loader, self)
        self.modrinth_search_worker.loaded.connect(self._modrinth_search_loaded)
        self.modrinth_search_worker.failed.connect(self._modrinth_search_failed)
        self.modrinth_search_worker.finished.connect(self._modrinth_search_finished)
        self.modrinth_search_worker.start()
        self.log(f"Searching Modrinth mods: {query or 'popular'} / Minecraft {game_version} / {loader}.")

    def _modrinth_search_loaded(self, projects):
        self.modrinth_projects = projects
        self.modrinth_table.setRowCount(len(projects))
        for row, project in enumerate(projects):
            title = QTableWidgetItem(project["title"])
            title.setData(Qt.ItemDataRole.UserRole, project["project_id"])
            self.modrinth_table.setItem(row, 0, title)
            self.modrinth_table.setItem(row, 1, QTableWidgetItem(f"{project['downloads']:,}"))
            self.modrinth_table.setItem(row, 2, QTableWidgetItem(project["description"]))
        self.modrinth_table.resizeRowsToContents()
        self.modrinth_status.setText(
            f"Found {len(projects)} compatible Modrinth project(s). Select one to install."
        )
        self.modrinth_install_button.setEnabled(bool(projects))
        self.log(f"Modrinth search returned {len(projects)} compatible projects.")

    def _modrinth_search_failed(self, error):
        self.modrinth_status.setText(f"Modrinth search failed: {error}")
        self.log(f"Modrinth search error: {error}")

    def _modrinth_search_finished(self):
        self.modrinth_search_button.setEnabled(True)
        self.modrinth_search_worker = None

    def install_selected_modrinth(self):
        if self.modrinth_install_worker and self.modrinth_install_worker.isRunning():
            return
        row = self.modrinth_table.currentRow()
        if row < 0 or row >= len(self.modrinth_projects):
            self.modrinth_status.setText("Select a Modrinth project first.")
            return
        game_version = self.modrinth_game_version.text().strip()
        project = self.modrinth_projects[row]
        loader = self.modrinth_loader.currentData()
        mods_directory = Path.home() / ".voidex" / "instances" / game_version / "mods"
        self.modrinth_install_button.setEnabled(False)
        self.modrinth_search_button.setEnabled(False)
        self.modrinth_status.setText(f"Finding a compatible {loader} build of {project['title']}...")
        self.modrinth_install_worker = ModrinthInstallWorker(
            project, game_version, loader, mods_directory, self
        )
        self.modrinth_install_worker.progress.connect(self._modrinth_install_progress)
        self.modrinth_install_worker.completed.connect(self._modrinth_install_completed)
        self.modrinth_install_worker.failed.connect(self._modrinth_install_failed)
        self.modrinth_install_worker.finished.connect(self._modrinth_install_finished)
        self.modrinth_install_worker.start()
        self.log(f"Installing Modrinth project {project['title']} into {mods_directory}.")

    def _modrinth_install_progress(self, completed, total):
        self.modrinth_status.setText(
            f"Downloading mod: {completed / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MiB"
        )

    def _modrinth_install_completed(self, path, version_name):
        self.modrinth_status.setText(f"Installed and checksum-verified: {Path(path).name} ({version_name})")
        self.log(f"Modrinth mod installed and verified: {path}")
        self.refresh_installed_mods()

    def _modrinth_install_failed(self, error):
        self.modrinth_status.setText(f"Mod install failed: {error}")
        self.log(f"Modrinth install error: {error}")

    def _modrinth_install_finished(self):
        self.modrinth_install_button.setEnabled(bool(self.modrinth_projects))
        self.modrinth_search_button.setEnabled(True)
        self.modrinth_install_worker = None

    def _versions_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Minecraft versions")
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        subtitle = QLabel("Official Mojang version manifest • loaded on demand")
        subtitle.setObjectName("Muted")
        layout.addWidget(subtitle)

        controls = QHBoxLayout()
        self.version_search = QLineEdit()
        self.version_search.setPlaceholderText("Search versions, e.g. 1.21.4")
        self.version_search.textChanged.connect(self._filter_versions)
        self.version_type = QComboBox()
        self.version_type.addItem("All versions", "all")
        self.version_type.addItem("Releases", "release")
        self.version_type.addItem("Snapshots", "snapshot")
        self.version_type.addItem("Old beta", "old_beta")
        self.version_type.addItem("Old alpha", "old_alpha")
        self.version_type.currentIndexChanged.connect(self._filter_versions)
        self.refresh_button = QPushButton("Refresh catalog")
        self.refresh_button.clicked.connect(self.refresh_versions)
        controls.addWidget(self.version_search, 2)
        controls.addWidget(self.version_type)
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)

        self.version_table = QTableWidget(0, 3)
        self.version_table.setHorizontalHeaderLabels(["Version", "Type", "Released"])
        self.version_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.version_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.version_table.horizontalHeader().setStretchLastSection(True)
        self.version_table.cellDoubleClicked.connect(self._choose_table_version)
        layout.addWidget(self.version_table, 1)

        bottom = QHBoxLayout()
        self.version_status = QLabel("Catalog not loaded.")
        self.version_status.setObjectName("Muted")
        self.selected_version_label = QLabel(f"Selected: {self.selected_version}")
        self.selected_version_label.setObjectName("Muted")
        choose = QPushButton("Use selected version")
        choose.clicked.connect(self._choose_table_version)
        self.inspect_button = QPushButton("Inspect selected")
        self.inspect_button.clicked.connect(self.inspect_selected_version)
        self.download_button = QPushButton("Download client JAR")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_selected_client)
        self.version_json_button = QPushButton("Cache version JSON")
        self.version_json_button.setEnabled(False)
        self.version_json_button.clicked.connect(self.cache_selected_version_json)
        self.library_button = QPushButton("Install libraries")
        self.library_button.setEnabled(False)
        self.library_button.clicked.connect(self.install_selected_libraries)
        self.asset_button = QPushButton("Install assets")
        self.asset_button.setEnabled(False)
        self.asset_button.clicked.connect(self.install_selected_assets)
        self.full_install_button = QPushButton("Install selected version")
        self.full_install_button.setEnabled(False)
        self.full_install_button.clicked.connect(self.install_selected_version_all)
        self.native_button = QPushButton("Prepare natives")
        self.native_button.setEnabled(False)
        self.native_button.clicked.connect(self.prepare_selected_natives)
        bottom.addWidget(self.version_status, 1)
        bottom.addWidget(self.selected_version_label)
        bottom.addWidget(self.inspect_button)
        bottom.addWidget(self.download_button)
        bottom.addWidget(self.version_json_button)
        bottom.addWidget(self.library_button)
        bottom.addWidget(self.asset_button)
        bottom.addWidget(self.native_button)
        bottom.addWidget(self.full_install_button)
        bottom.addWidget(choose)
        layout.addLayout(bottom)
        return page

    def refresh_versions(self):
        if self.manifest_worker and self.manifest_worker.isRunning():
            return
        self.refresh_button.setEnabled(False)
        self.version_status.setText("Connecting to Mojang...")
        self.log("Fetching official Minecraft version manifest.")
        self.manifest_worker = ManifestWorker(self)
        self.manifest_worker.loaded.connect(self._manifest_loaded)
        self.manifest_worker.failed.connect(self._manifest_failed)
        self.manifest_worker.finished.connect(lambda: self.refresh_button.setEnabled(True))
        self.manifest_worker.start()

    def _manifest_loaded(self, manifest):
        self.versions = manifest.get("versions", [])
        latest = manifest.get("latest", {})
        self.version_status.setText(
            f"{len(self.versions)} versions loaded • latest release {latest.get('release', 'unknown')}"
        )
        self._filter_versions()
        self.log(f"Mojang manifest loaded: {len(self.versions)} entries.")
        self.manifest_worker = None

    def _manifest_failed(self, error):
        self.version_status.setText("Could not load catalog. Check your internet connection.")
        self.log(f"Version catalog error: {error}")
        self.manifest_worker = None

    def _filter_versions(self, *_):
        if not hasattr(self, "version_table"):
            return
        query = self.version_search.text().strip().lower()
        kind = self.version_type.currentData()
        filtered = [
            item for item in self.versions
            if (kind == "all" or item.get("type") == kind)
            and query in item.get("id", "").lower()
        ]
        self.version_table.setRowCount(len(filtered))
        for row, item in enumerate(filtered):
            released = item.get("releaseTime", "")
            if released:
                try:
                    released = datetime.fromisoformat(released.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                except ValueError:
                    pass
            values = [item.get("id", ""), item.get("type", ""), released]
            for col, value in enumerate(values):
                self.version_table.setItem(row, col, QTableWidgetItem(value))
        self.version_table.resizeColumnsToContents()

    def inspect_selected_version(self):
        row = self.version_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "VOIDEX", "Select a version in the catalog first.")
            return
        version_id = self.version_table.item(row, 0).text()
        self._choose_table_version()
        entry = next((v for v in self.versions if v.get("id") == version_id), None)
        if not entry or not entry.get("url"):
            self.log(f"No metadata URL found for {version_id}.")
            return
        if self.metadata_worker and self.metadata_worker.isRunning():
            return
        self.inspect_button.setEnabled(False)
        self.version_status.setText(f"Loading metadata for {version_id}...")
        self.metadata_worker = MetadataWorker(entry["url"], self)
        self.metadata_worker.loaded.connect(
            lambda summary, version=version_id: self._metadata_loaded(version, summary)
        )
        self.metadata_worker.failed.connect(self._metadata_failed)
        self.metadata_worker.finished.connect(lambda: self.inspect_button.setEnabled(True))
        self.metadata_worker.start()
        self.log(f"Inspecting Mojang metadata for {version_id}.")

    def _metadata_loaded(self, version_id, summary):
        java = f"Java {summary['java_major']}" if summary["java_major"] else "Java requirement not specified"
        size_mb = summary["client_size"] / (1024 * 1024)
        self.selected_metadata = summary
        self.selected_metadata_version = version_id
        self.download_button.setEnabled(True)
        self.version_json_button.setEnabled(True)
        self.library_button.setEnabled(True)
        self.asset_button.setEnabled(bool(summary["asset_index"]))
        self.native_button.setEnabled(True)
        self.full_install_button.setEnabled(True)
        self.version_status.setText(
            f"{version_id}: {java} • client JAR {size_mb:.1f} MiB • "
            f"{summary['libraries_count']} libraries • assets {summary['asset_index'] or 'n/a'}"
        )
        self.log(
            f"{version_id} metadata validated: {java}; SHA-1 {summary['client_sha1']}."
        )
        self.metadata_worker = None
        if not (self.java_worker and self.java_worker.isRunning()):
            configured_java = self.java_path.text().strip()
            self._start_java_check(configured_java or None)


    def install_selected_version_all(self):
        if self.full_install_worker and self.full_install_worker.isRunning():
            return
        if self.selected_metadata_version != self.selected_version:
            QMessageBox.information(self, "VOIDEX", "Inspect the selected version first.")
            return
        entry = next((item for item in self.versions
                      if item.get("id") == self.selected_version), None)
        if not entry or not entry.get("url") or not entry.get("sha1"):
            QMessageBox.information(self, "VOIDEX", "The selected version has no verified Mojang metadata entry.")
            return
        other_workers = (
            self.download_worker, self.version_json_worker, self.library_worker,
            self.asset_worker, self.native_worker
        )
        if any(worker and worker.isRunning() for worker in other_workers):
            QMessageBox.information(self, "VOIDEX", "Wait for the current download stage to finish first.")
            return

        version_id = self.selected_version
        self.full_install_button.setEnabled(False)
        for button in (self.download_button, self.version_json_button, self.library_button,
                       self.asset_button, self.native_button):
            button.setEnabled(False)
        self.version_status.setText(f"Installing all required files for {version_id}...")
        self.full_install_worker = FullInstallWorker(version_id, entry, self)
        self.full_install_worker.stage.connect(self._full_install_stage)
        self.full_install_worker.progress.connect(self._full_install_progress)
        self.full_install_worker.completed.connect(self._full_install_completed)
        self.full_install_worker.failed.connect(self._full_install_failed)
        self.full_install_worker.finished.connect(self._full_install_finished)
        self.full_install_worker.start()
        self.log(f"Started one-button verified installation for Minecraft {version_id}.")

    def _full_install_stage(self, stage):
        self.version_status.setText(stage + "...")
        self.log(stage + ".")

    def _full_install_progress(self, completed, total, current_path):
        if total:
            self.version_status.setText(
                f"{current_path}: {completed / (1024 * 1024):.1f} / "
                f"{total / (1024 * 1024):.1f} MiB"
            )
        else:
            self.version_status.setText(f"Preparing {current_path}...")

    def _full_install_completed(self, result):
        self.version_status.setText(
            f"{result['version']} installed: {result['libraries']} libraries, "
            f"{result['native_archives']} native archives, {result['verified_assets']} assets."
        )
        self.launch_status.setText(
            f"Files for {result['version']} are installed and verified. Press Launch; VOIDEX will validate a compatible Java runtime."
        )
        self.log(
            f"Installation complete for {result['version']}: client verified; "
            f"{result['libraries']} libraries; {result['native_files']} native files; "
            f"{result['verified_assets']} verified asset objects. Game directory: {result['game_directory']}"
        )
        self.full_install_worker = None

    def _full_install_failed(self, error):
        self.version_status.setText("Installation stopped; failed files were not accepted.")
        self.log(f"Full installation error: {error}")
        self.full_install_worker = None

    def _full_install_finished(self):
        self.full_install_button.setEnabled(True)
        for button in (self.download_button, self.version_json_button, self.library_button,
                       self.asset_button, self.native_button):
            button.setEnabled(True)


    def install_selected_libraries(self):
        if self.library_worker and self.library_worker.isRunning():
            return
        if self.selected_metadata_version != self.selected_version:
            QMessageBox.information(self, "VOIDEX", "Inspect the selected version first.")
            return
        entry = next((item for item in self.versions
                      if item.get("id") == self.selected_version), None)
        if not entry or not entry.get("url") or not entry.get("sha1"):
            self.log("Selected version has no verified JSON metadata URL or SHA-1.")
            return

        version_id = self.selected_version
        cache_root = Path.home() / ".voidex" / "cache" / "versions" / version_id
        json_path = cache_root / f"{version_id}.json"
        libraries_root = Path.home() / ".voidex" / "libraries"
        self.library_button.setEnabled(False)
        self.version_status.setText(f"Preparing libraries for {version_id}...")
        self.library_worker = LibraryInstallWorker(
            entry["url"], entry["sha1"], json_path, libraries_root, self
        )
        self.library_worker.progress.connect(self._library_install_progress)
        self.library_worker.completed.connect(self._library_install_completed)
        self.library_worker.failed.connect(self._library_install_failed)
        self.library_worker.finished.connect(lambda: self.library_button.setEnabled(True))
        self.library_worker.start()
        self.log(f"Resolving and downloading verified Windows x64 libraries for {version_id}.")

    def _library_install_progress(self, completed, total, current_path):
        stage = "Version JSON" if current_path == "version JSON" else "Libraries"
        if total:
            self.version_status.setText(
                f"{stage}: {completed / (1024 * 1024):.1f} / "
                f"{total / (1024 * 1024):.1f} MiB • {current_path}"
            )
        else:
            self.version_status.setText(f"Preparing {current_path}...")

    def _library_install_completed(self, paths):
        count = len(paths)
        self.version_status.setText(
            f"Verified libraries cached: {count}. Assets, natives and game launch are still pending."
        )
        self.log(f"Library stage complete: {count} verified artifacts cached under .voidex/libraries.")
        self.library_worker = None

    def _library_install_failed(self, error):
        self.version_status.setText("Library installation failed; unverified files were not accepted.")
        self.log(f"Library installation error: {error}")
        self.library_worker = None


    def prepare_selected_natives(self):
        if self.native_worker and self.native_worker.isRunning():
            return
        if self.selected_metadata_version != self.selected_version:
            QMessageBox.information(self, "VOIDEX", "Inspect the selected version first.")
            return
        entry = next((item for item in self.versions
                      if item.get("id") == self.selected_version), None)
        if not entry or not entry.get("url") or not entry.get("sha1"):
            self.log("Selected version has no version JSON URL or SHA-1.")
            return
        version_id = self.selected_version
        cache_root = Path.home() / ".voidex" / "cache" / "versions" / version_id
        version_json_path = cache_root / f"{version_id}.json"
        libraries_root = Path.home() / ".voidex" / "libraries"
        natives_root = Path.home() / ".voidex" / "versions" / version_id / "natives"
        self.native_button.setEnabled(False)
        self.version_status.setText(f"Preparing native libraries for {version_id}...")
        self.native_worker = NativeInstallWorker(
            entry["url"], entry["sha1"], version_json_path,
            libraries_root, natives_root, self
        )
        self.native_worker.progress.connect(self._native_install_progress)
        self.native_worker.completed.connect(self._native_install_completed)
        self.native_worker.failed.connect(self._native_install_failed)
        self.native_worker.finished.connect(lambda: self.native_button.setEnabled(True))
        self.native_worker.start()
        self.log(f"Downloading verified native classifiers and safely extracting them for {version_id}.")

    def _native_install_progress(self, completed, total, current_path):
        if total:
            self.version_status.setText(
                f"Native download: {completed / (1024 * 1024):.1f} / "
                f"{total / (1024 * 1024):.1f} MiB • {current_path}"
            )
        else:
            self.version_status.setText("Preparing version metadata for native libraries...")

    def _native_install_completed(self, result):
        self.version_status.setText(
            f"Native preparation complete: {result['archives']} archives, "
            f"{result['files']} files extracted."
        )
        self.log(f"Native files prepared in {result['directory']}.")
        self.native_worker = None

    def _native_install_failed(self, error):
        self.version_status.setText("Native preparation failed; unsafe archive entries were rejected.")
        self.log(f"Native extraction error: {error}")
        self.native_worker = None

    def install_selected_assets(self):
        if self.asset_worker and self.asset_worker.isRunning():
            return
        if self.selected_metadata_version != self.selected_version:
            QMessageBox.information(self, "VOIDEX", "Inspect the selected version first.")
            return
        entry = next((item for item in self.versions
                      if item.get("id") == self.selected_version), None)
        if not entry or not entry.get("url") or not entry.get("sha1"):
            self.log("Selected version has no version JSON URL or SHA-1 in the manifest.")
            return
        version_id = self.selected_version
        cache_root = Path.home() / ".voidex" / "cache" / "versions" / version_id
        version_json_path = cache_root / f"{version_id}.json"
        assets_root = Path.home() / ".voidex" / "assets"
        self.asset_button.setEnabled(False)
        self.version_status.setText(f"Preparing Minecraft assets for {version_id}...")
        self.asset_worker = AssetInstallWorker(
            entry["url"], entry["sha1"], version_json_path, assets_root, self
        )
        self.asset_worker.progress.connect(self._asset_install_progress)
        self.asset_worker.completed.connect(self._asset_install_completed)
        self.asset_worker.failed.connect(self._asset_install_failed)
        self.asset_worker.finished.connect(lambda: self.asset_button.setEnabled(True))
        self.asset_worker.start()
        self.log(f"Resolving and downloading verified asset objects for {version_id}.")

    def _asset_install_progress(self, completed, total, current_path):
        stage = "Asset index" if current_path == "asset index" else (
            "Version JSON" if current_path == "version JSON" else "Assets"
        )
        if total:
            self.version_status.setText(
                f"{stage}: {completed / (1024 * 1024):.1f} / "
                f"{total / (1024 * 1024):.1f} MiB • {current_path}"
            )
        else:
            self.version_status.setText(f"Preparing {current_path}...")

    def _asset_install_completed(self, result):
        count = result["count"]
        self.version_status.setText(
            f"Verified asset objects cached: {count}. Native extraction and game launch are still pending."
        )
        self.log(f"Asset stage complete: {count} verified objects cached under .voidex/assets/objects.")
        self.asset_worker = None

    def _asset_install_failed(self, error):
        self.version_status.setText("Asset installation failed; unverified objects were not accepted.")
        self.log(f"Asset installation error: {error}")
        self.asset_worker = None

    def cache_selected_version_json(self):
        if self.version_json_worker and self.version_json_worker.isRunning():
            return
        if self.selected_metadata_version != self.selected_version:
            QMessageBox.information(self, "VOIDEX", "Inspect the selected version first.")
            return
        entry = next((item for item in self.versions
                      if item.get("id") == self.selected_version), None)
        if not entry or not entry.get("url") or not entry.get("sha1"):
            self.log("Selected manifest entry has no version JSON URL or SHA-1.")
            return
        destination = (Path.home() / ".voidex" / "cache" / "versions"
                       / self.selected_version / f"{self.selected_version}.json")
        self.version_json_button.setEnabled(False)
        self.version_status.setText(f"Caching verified version JSON for {self.selected_version}...")
        self.version_json_worker = VersionJsonWorker(
            entry["url"], entry["sha1"], destination, self
        )
        self.version_json_worker.progress.connect(self._version_json_progress)
        self.version_json_worker.completed.connect(self._version_json_completed)
        self.version_json_worker.failed.connect(self._version_json_failed)
        self.version_json_worker.finished.connect(
            lambda: self.version_json_button.setEnabled(True)
        )
        self.version_json_worker.start()
        self.log(f"Downloading version JSON for {self.selected_version} with manifest SHA-1 verification.")

    def _version_json_progress(self, downloaded, total):
        if total:
            self.version_status.setText(
                f"Version JSON: {downloaded / 1024:.1f} / {total / 1024:.1f} KiB"
            )
        else:
            self.version_status.setText(
                f"Version JSON: {downloaded / 1024:.1f} KiB received"
            )

    def _version_json_completed(self, path):
        self.version_status.setText(f"Version JSON cached and verified: {path}")
        self.log(f"Verified version JSON cached at: {path}")
        self.version_json_worker = None

    def _version_json_failed(self, error):
        self.version_status.setText("Version JSON caching failed; invalid data was not saved.")
        self.log(f"Version JSON error: {error}")
        self.version_json_worker = None

    def download_selected_client(self):
        if not self.selected_metadata or self.selected_metadata_version != self.selected_version:
            QMessageBox.information(self, "VOIDEX", "Inspect the selected version metadata first.")
            return
        if self.download_worker and self.download_worker.isRunning():
            return
        version_id = self.selected_metadata_version
        destination = Path.home() / ".voidex" / "cache" / "clients" / f"{version_id}.jar"
        self.download_button.setEnabled(False)
        self.version_status.setText(f"Downloading verified client JAR for {version_id}...")
        self.download_worker = ClientDownloadWorker(self.selected_metadata, destination, self)
        self.download_worker.progress.connect(self._client_download_progress)
        self.download_worker.completed.connect(self._client_download_completed)
        self.download_worker.failed.connect(self._client_download_failed)
        self.download_worker.finished.connect(lambda: self.download_button.setEnabled(True))
        self.download_worker.start()
        self.log(f"Downloading Mojang client JAR for {version_id} into the local cache.")

    def _client_download_progress(self, downloaded, total):
        self.version_status.setText(
            f"Client JAR download: {downloaded / (1024 * 1024):.1f} / "
            f"{total / (1024 * 1024):.1f} MiB"
        )

    def _client_download_completed(self, path):
        self.version_status.setText(f"Client JAR downloaded and SHA-1 verified: {path}")
        self.log(f"Verified client JAR cached at: {path}")
        self.download_worker = None

    def _client_download_failed(self, error):
        self.version_status.setText("Client JAR download failed; no unverified file was installed.")
        self.log(f"Client JAR download error: {error}")
        self.download_worker = None

    def _metadata_failed(self, error):
        self.selected_metadata = None
        self.selected_metadata_version = None
        self.download_button.setEnabled(False)
        self.version_json_button.setEnabled(False)
        self.library_button.setEnabled(False)
        self.asset_button.setEnabled(False)
        self.native_button.setEnabled(False)
        self.full_install_button.setEnabled(False)
        self.version_status.setText("Could not inspect version metadata.")
        self.log(f"Version metadata error: {error}")
        self.metadata_worker = None

    def _choose_table_version(self, *_):
        row = self.version_table.currentRow()
        if row < 0:
            return
        item = self.version_table.item(row, 0)
        if not item:
            return
        self.selected_version = item.text()
        if self.selected_metadata_version != self.selected_version:
            self.selected_metadata = None
            self.selected_metadata_version = None
            self.download_button.setEnabled(False)
            self.version_json_button.setEnabled(False)
            self.library_button.setEnabled(False)
            self.asset_button.setEnabled(False)
            self.native_button.setEnabled(False)
        self.selected_version_label.setText(f"Selected: {self.selected_version}")
        self.home_version.setText(f"Selected version: {self.selected_version}")
        self.settings.setValue("selected_version", self.selected_version)
        self.log(f"Selected Minecraft version: {self.selected_version}")

    def _accounts_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Accounts & skin identity")
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        panel, form = self._panel("ACCOUNT DETAILS", "Offline UUIDs are calculated locally. Ely.by and Microsoft login are not implemented yet.")
        self.account_source = QComboBox()
        self.account_source.addItems(["Offline", "Ely.by", "Microsoft"])
        self.account_source.currentTextChanged.connect(self._refresh_identity)
        self.player_name = QLineEdit()
        self.player_name.setPlaceholderText("Minecraft nickname")
        self.player_name.textChanged.connect(self._refresh_identity)
        self.skin_uuid = QLineEdit()
        self.skin_uuid.setReadOnly(True)
        self.skin_uuid.setPlaceholderText("UUID")
        self.account_status = QLabel("Offline profile is ready.")
        self.account_status.setObjectName("Muted")
        form.addLayout(self._form_row("Account source", self.account_source))
        form.addLayout(self._form_row("Player name", self.player_name))
        form.addLayout(self._form_row("Profile UUID", self.skin_uuid))
        form.addWidget(self.account_status)
        layout.addWidget(panel)
        layout.addStretch()
        return page

    def _form_row(self, label, widget):
        row = QFormLayout()
        row.addRow(label, widget)
        return row

    def _refresh_identity(self, *_):
        if not hasattr(self, "account_source"):
            return
        source = self.account_source.currentText()
        nickname = self.player_name.text().strip()
        if source == "Offline":
            if nickname:
                profile_id = offline_uuid(nickname)
                self.skin_uuid.setText(profile_id)
                self.account_status.setText("Offline UUID generated locally. This is not a premium/Mojang account UUID.")
                self.account_summary.setText(f"Source: Offline  |  UUID: {profile_id}")
            else:
                self.skin_uuid.clear()
                self.account_status.setText("Enter a nickname to generate its offline UUID.")
                self.account_summary.setText("Source: Offline  |  UUID: nickname required")
        else:
            self.skin_uuid.clear()
            self.account_status.setText(f"{source} authentication is not connected yet; no UUID is being fabricated.")
            self.account_summary.setText(f"Source: {source}  |  Authentication not connected")

    def _settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Performance & settings")
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        panel, form = self._panel("PERFORMANCE PRESET", "Safe starting points. RAM values are recommendations, not guaranteed FPS boosts.")
        self.preset = QComboBox()
        self.preset.addItems(PROFILES.keys())
        self.preset.currentTextChanged.connect(self._apply_profile)
        self.ram = QComboBox()
        self.ram.addItems(["2048", "3072", "4096", "5120", "6144", "8192", "10240"])
        self.renderer = QComboBox()
        self.renderer.addItems(["Compatibility", "Default", "Performance"])
        self.jvm = QLineEdit()
        self.jvm.setPlaceholderText("Additional JVM arguments")
        self.java_path = QLineEdit()
        self.java_path.setPlaceholderText("Full path to java.exe (optional)")
        self.java_status = QLabel("Java has not been checked yet.")
        self.java_status.setObjectName("Muted")
        java_actions = QHBoxLayout()
        self.detect_java_button = QPushButton("Detect Java")
        self.detect_java_button.clicked.connect(self.detect_java)
        self.validate_java_button = QPushButton("Validate path")
        self.validate_java_button.clicked.connect(self.validate_selected_java)
        self.install_java_button = QPushButton("Install required Java")
        self.install_java_button.clicked.connect(self.install_required_java)
        java_actions.addWidget(self.detect_java_button)
        java_actions.addWidget(self.validate_java_button)
        java_actions.addWidget(self.install_java_button)
        form.addLayout(self._form_row("Java executable", self.java_path))
        form.addRow("", java_actions)
        form.addRow("", self.java_status)
        form.addLayout(self._form_row("PC profile", self.preset))
        form.addLayout(self._form_row("Memory allocation (MB)", self.ram))
        form.addLayout(self._form_row("Rendering profile", self.renderer))
        form.addLayout(self._form_row("JVM arguments", self.jvm))
        layout.addWidget(panel)
        effects, effects_layout = self._panel("VISUAL EFFECTS", "Effects are intentionally off by default to reduce idle GPU usage.")
        self.effects = QComboBox()
        self.effects.addItems(["Disabled (recommended)", "Reduced", "Full"])
        effects_layout.addWidget(self.effects)
        layout.addWidget(effects)
        save = QPushButton("Save settings")
        save.clicked.connect(self._save_settings)
        layout.addWidget(save)
        layout.addStretch()
        return page


    def _required_java_major(self):
        if self.selected_metadata and self.selected_metadata_version == self.selected_version:
            return self.selected_metadata.get("java_major")
        return None

    def detect_java(self):
        if self.java_worker and self.java_worker.isRunning():
            return
        self._start_java_check(None)

    def validate_selected_java(self):
        if self.java_worker and self.java_worker.isRunning():
            return
        executable = self.java_path.text().strip()
        if not executable:
            self.java_status.setText("Enter the full path to java.exe or use Detect Java.")
            return
        self._start_java_check(executable)

    def install_required_java(self):
        if self.java_install_worker and self.java_install_worker.isRunning():
            return
        expected_major = self._required_java_major()
        if expected_major is None:
            self.java_status.setText("Inspect a Minecraft version first to determine its Java requirement.")
            return
        if self.java_worker and self.java_worker.isRunning():
            self.java_status.setText("Wait for Java detection to finish first.")
            return
        self.install_java_button.setEnabled(False)
        self.detect_java_button.setEnabled(False)
        self.validate_java_button.setEnabled(False)
        self.java_status.setText(f"Preparing official Eclipse Temurin Java {expected_major}...")
        self.log(f"Starting explicit Temurin Java {expected_major} installation.")
        runtime_root = Path.home() / ".voidex" / "runtimes"
        self.java_install_worker = TemurinInstallWorker(expected_major, runtime_root, self)
        self.java_install_worker.progress.connect(self._java_install_progress)
        self.java_install_worker.completed.connect(self._java_install_completed)
        self.java_install_worker.failed.connect(self._java_install_failed)
        self.java_install_worker.finished.connect(self._java_install_finished)
        self.java_install_worker.start()

    def _java_install_progress(self, completed, total, stage):
        if total:
            self.java_status.setText(
                f"{stage}: {completed / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MiB"
            )
        else:
            self.java_status.setText(f"Preparing {stage}...")

    def _java_install_completed(self, executable):
        self.java_path.setText(executable)
        self.settings.setValue("java_path", executable)
        self.java_status.setText(f"Java runtime installed and verified: {executable}")
        self.log(f"Temurin runtime installed and verified: {executable}")

    def _java_install_failed(self, error):
        self.java_status.setText(f"Java installation failed: {error}")
        self.log(f"Temurin installation error: {error}")

    def _java_install_finished(self):
        self.install_java_button.setEnabled(True)
        self.detect_java_button.setEnabled(True)
        self.validate_java_button.setEnabled(True)
        self.java_install_worker = None

    def _start_java_check(self, executable):
        self.detect_java_button.setEnabled(False)
        self.validate_java_button.setEnabled(False)
        self.java_status.setText("Checking Java runtime...")
        self.java_worker = JavaCheckWorker(executable, self._required_java_major(), self)
        self.java_worker.completed.connect(self._java_check_completed)
        self.java_worker.failed.connect(self._java_check_failed)
        self.java_worker.finished.connect(self._java_check_finished)
        self.java_worker.start()

    def _java_check_completed(self, result):
        self.java_path.setText(result["path"])
        if result["expected_major"] is None:
            message = f"Java {result['version']} detected (major {result['major']})."
        elif result["compatible"]:
            message = (
                f"Java {result['version']} matches the selected version requirement "
                f"(requires Java {result['expected_major']} exactly)."
            )
        else:
            message = (
                f"Java {result['version']} does not match the selected Minecraft version requirement "
                f"(requires {result['expected_major']}+)."
            )
        self.java_status.setText(message)
        self.log(message)

    def _java_check_failed(self, error):
        self.java_status.setText(error)
        self.log(f"Java detection error: {error}")

    def _java_check_finished(self):
        self.detect_java_button.setEnabled(True)
        self.validate_java_button.setEnabled(True)
        self.java_worker = None



    def launch_selected_game(self):
        if self.minecraft_process and self.minecraft_process.state() != QProcess.ProcessState.NotRunning:
            self.log("Minecraft is already running.")
            return
        if self.launch_worker and self.launch_worker.isRunning():
            return
        if self.selected_metadata_version != self.selected_version:
            QMessageBox.information(self, "VOIDEX", "Select and inspect a Minecraft version first.")
            self._navigate(1)
            return
        entry = next((item for item in self.versions if item.get("id") == self.selected_version), None)
        if not entry:
            QMessageBox.information(self, "VOIDEX", "The selected version is not in the loaded Mojang catalog.")
            return
        try:
            extra_jvm = shlex.split(self.jvm.text().strip()) if self.jvm.text().strip() else []
        except ValueError as exc:
            QMessageBox.warning(self, "VOIDEX", f"Invalid JVM arguments: {exc}")
            return
        self.launch_button.setEnabled(False)
        self.launch_status.setText("Checking installation, assets and Java runtime...")
        self.log(f"Preparing launch for Minecraft {self.selected_version}.")
        self.launch_worker = LaunchCheckWorker(
            self.selected_version, entry, self.java_path.text().strip(),
            int(self.ram.currentText()), extra_jvm,
            self.account_source.currentText(), self.player_name.text().strip(),
            self.fabric_profile_id if self.fabric_profile_game_version == self.selected_version else None, self,
        )
        self.launch_worker.ready.connect(self._launch_plan_ready)
        self.launch_worker.failed.connect(self._launch_plan_failed)
        self.launch_worker.finished.connect(self._launch_check_finished)
        self.launch_worker.start()

    def _launch_check_finished(self):
        self.launch_worker = None
        if not (self.minecraft_process and self.minecraft_process.state() != QProcess.ProcessState.NotRunning):
            self.launch_button.setEnabled(True)

    def _launch_plan_failed(self, error):
        self.launch_status.setText(f"Launch blocked: {error}")
        self.log(f"Launch preflight failed: {error}")
        self.launch_button.setEnabled(True)

    def _launch_plan_ready(self, plan):
        self.log("All required files passed preflight checks. Starting Java process.")
        self.launch_status.setText("Starting Minecraft...")
        process = QProcess(self)
        self.minecraft_process = process
        process.setProgram(plan["argv"][0])
        process.setArguments(plan["argv"][1:])
        process.setWorkingDirectory(plan["cwd"])
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._minecraft_stdout)
        process.readyReadStandardError.connect(self._minecraft_stderr)
        process.errorOccurred.connect(self._minecraft_process_error)
        process.finished.connect(self._minecraft_process_finished)
        process.started.connect(self._minecraft_process_started)
        self.launch_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        process.start()

    def _minecraft_process_started(self):
        self.launch_status.setText("Minecraft process is running.")
        self.log("Minecraft Java process started.")

    def _minecraft_stdout(self):
        if not self.minecraft_process:
            return
        data = bytes(self.minecraft_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in data.splitlines():
            self.log(f"[MC] {line}")

    def _minecraft_stderr(self):
        if not self.minecraft_process:
            return
        data = bytes(self.minecraft_process.readAllStandardError()).decode("utf-8", errors="replace")
        for line in data.splitlines():
            self.log(f"[MC:ERR] {line}")

    def _minecraft_process_error(self, error):
        if not self.minecraft_process:
            return
        self.launch_status.setText(f"Minecraft process error: {self.minecraft_process.errorString()}")
        self.log(f"Java process error: {self.minecraft_process.errorString()}")
        if self.minecraft_process.state() == QProcess.ProcessState.NotRunning:
            self.stop_button.setEnabled(False)
            self.launch_button.setEnabled(True)

    def _minecraft_process_finished(self, exit_code, exit_status):
        self._minecraft_stdout()
        self._minecraft_stderr()
        self.stop_button.setEnabled(False)
        self.launch_button.setEnabled(True)
        self.launch_status.setText(f"Minecraft exited with code {exit_code}.")
        self.log(f"Minecraft process finished: exit code {exit_code}, status {exit_status}.")
        self.minecraft_process = None

    def stop_minecraft(self):
        process = self.minecraft_process
        if not process or process.state() == QProcess.ProcessState.NotRunning:
            return
        self.log("Sending terminate signal to Minecraft.")
        process.terminate()
        QTimer.singleShot(
            3000,
            lambda target=process: (
                self.log("Minecraft did not stop gracefully; sending kill signal."),
                target.kill()
            ) if target.state() != QProcess.ProcessState.NotRunning else None
        )

    def _console_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Console")
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(1500)
        layout.addWidget(self.console)
        return page

    def _apply_profile(self, name):
        if not hasattr(self, "ram"):
            return
        profile = PROFILES.get(name, PROFILES["Balanced"])
        self.ram.setCurrentText(str(profile["ram"]))
        self.renderer.setCurrentText(profile["renderer"])
        self.jvm.setText(profile["jvm"])
        if hasattr(self, "profile_badge"):
            self.profile_badge.setText(f"Performance preset: {name}")
        self.log(f"Performance profile selected: {name}")

    def _save_settings(self):
        self.settings.setValue("preset", self.preset.currentText())
        self.settings.setValue("ram", self.ram.currentText())
        self.settings.setValue("renderer", self.renderer.currentText())
        self.settings.setValue("jvm", self.jvm.text())
        self.settings.setValue("java_path", self.java_path.text().strip())
        self.settings.setValue("effects", self.effects.currentText())
        self.settings.setValue("account_source", self.account_source.currentText())
        self.settings.setValue("nickname", self.player_name.text())
        self.log("Settings saved.")
        QMessageBox.information(self, "VOIDEX", "Settings saved successfully.")

    def _restore_settings(self):
        self.preset.setCurrentText(self.settings.value("preset", "Balanced"))
        self.ram.setCurrentText(self.settings.value("ram", "4096"))
        self.renderer.setCurrentText(self.settings.value("renderer", "Default"))
        self.jvm.setText(self.settings.value("jvm", "-XX:+UseG1GC"))
        self.java_path.setText(self.settings.value("java_path", ""))
        self.effects.setCurrentText(self.settings.value("effects", "Disabled (recommended)"))
        self.account_source.setCurrentText(self.settings.value("account_source", "Offline"))
        self.player_name.setText(self.settings.value("nickname", ""))

    def log(self, message):
        if hasattr(self, "console"):
            self.console.appendPlainText(f"[VOIDEX] {message}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("VOIDEX Launcher")
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
