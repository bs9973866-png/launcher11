from __future__ import annotations

import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QPlainTextEdit, QSlider, QStackedWidget,
    QVBoxLayout, QWidget, QFormLayout
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
QPushButton#Primary { background: #7b45dc; border: 1px solid #a77aff; color: white; font-weight: 700; padding: 14px 26px; }
QPushButton#Primary:hover { background: #925cf0; }
QComboBox, QLineEdit { background: #0b0915; border: 1px solid #30264a; border-radius: 7px; padding: 8px; min-height: 20px; }
QPlainTextEdit { background: #07060d; border: 1px solid #292044; border-radius: 8px; color: #b8f4d0; font-family: Consolas; }
QSlider::groove:horizontal { height: 5px; background: #30264a; border-radius: 2px; }
QSlider::handle:horizontal { background: #a77aff; width: 14px; margin: -5px 0; border-radius: 7px; }
"""

PROFILES = {
    "Low-end PC": {"ram": 2048, "renderer": "Compatibility", "jvm": "-XX:+UseG1GC -XX:MaxGCPauseMillis=100"},
    "Balanced": {"ram": 4096, "renderer": "Default", "jvm": "-XX:+UseG1GC"},
    "High-end PC": {"ram": 6144, "renderer": "Performance", "jvm": "-XX:+UseG1GC -XX:+ParallelRefProcEnabled"},
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VOIDEX Launcher")
        self.resize(1180, 740)
        self.setMinimumSize(900, 600)
        self._build_ui()
        self._apply_profile("Balanced")
        self.log("VOIDEX initialized. Idle mode: no animation or polling timers.")

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
        for index, name in enumerate(self.page_names):
            button = QPushButton(name)
            button.clicked.connect(lambda checked=False, i=index: self.pages.setCurrentIndex(i))
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
        self.pages.addWidget(self._placeholder_page("Minecraft versions", "Version catalog and installation manager will live here."))
        self.pages.addWidget(self._placeholder_page("Mod manager", "Modrinth and CurseForge integration will live here."))
        self.pages.addWidget(self._accounts_page())
        self.pages.addWidget(self._settings_page())
        self.pages.addWidget(self._console_page())

        outer.addWidget(sidebar)
        outer.addWidget(content, 1)
        self.setCentralWidget(root)

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
        hero, hero_layout = self._panel("READY FOR TAKEOFF", "A quiet, lightweight launcher that only does work when you ask it to.")
        self.profile_badge = QLabel("Performance preset: Balanced")
        self.profile_badge.setObjectName("Muted")
        hero_layout.addWidget(self.profile_badge)
        play = QPushButton("LAUNCH MINECRAFT")
        play.setObjectName("Primary")
        play.clicked.connect(lambda: self.log("Launch requested. Game installation and account checks are not connected yet."))
        hero_layout.addWidget(play)
        layout.addWidget(hero)
        account, account_layout = self._panel("PLAYER IDENTITY", "Connected account and skin UUID will be displayed here.")
        self.account_summary = QLabel("Source: Offline  |  Skin UUID: not loaded")
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
        panel, panel_layout = self._panel("COMING IN THE NEXT DEVELOPMENT STAGE", text)
        layout.addWidget(panel)
        layout.addStretch()
        return page

    def _accounts_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Accounts & skin identity")
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        panel, form = self._panel("ACCOUNT DETAILS", "The UUID field is read-only until a real account provider is connected.")
        self.account_source = QComboBox()
        self.account_source.addItems(["Offline", "Ely.by", "Microsoft"])
        self.player_name = QLineEdit()
        self.player_name.setPlaceholderText("Minecraft nickname")
        self.skin_uuid = QLineEdit()
        self.skin_uuid.setReadOnly(True)
        self.skin_uuid.setPlaceholderText("UUID will appear after authentication / skin lookup")
        form.addLayout(self._form_row("Account source", self.account_source))
        form.addLayout(self._form_row("Player name", self.player_name))
        form.addLayout(self._form_row("Skin UUID", self.skin_uuid))
        layout.addWidget(panel)
        layout.addStretch()
        return page

    def _form_row(self, label, widget):
        row = QFormLayout()
        row.addRow(label, widget)
        return row

    def _settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Performance & settings")
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        panel, form = self._panel("PERFORMANCE PRESET", "Presets provide safe starting points. Custom JVM arguments remain editable.")
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
