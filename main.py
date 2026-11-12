from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from launcher_core import fetch_version_metadata, summarize_version_metadata, download_verified_client

from PySide6.QtCore import Qt, QSettings, QThread, Signal
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("VOIDEX", "Launcher")
        self.manifest_worker = None
        self.metadata_worker = None
        self.download_worker = None
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
        self.pages.addWidget(self._placeholder_page("Mod manager", "Modrinth and CurseForge integration is planned. No fake download controls are shown."))
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
        play = QPushButton("LAUNCH MINECRAFT")
        play.setObjectName("Primary")
        play.clicked.connect(lambda: self.log("Launch is not enabled yet: runtime installation, Java validation and account authentication must be completed first."))
        hero_layout.addWidget(play)
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
        bottom.addWidget(self.version_status, 1)
        bottom.addWidget(self.selected_version_label)
        bottom.addWidget(self.inspect_button)
        bottom.addWidget(self.download_button)
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
        self.version_status.setText(
            f"{version_id}: {java} • client JAR {size_mb:.1f} MiB • "
            f"{summary['libraries_count']} libraries • assets {summary['asset_index'] or 'n/a'}"
        )
        self.log(
            f"{version_id} metadata validated: {java}; SHA-1 {summary['client_sha1']}."
        )
        self.metadata_worker = None

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
