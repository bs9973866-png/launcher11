# VOIDEX Launcher

A lightweight, customizable Minecraft Java launcher for Windows, built with Python and PySide6.

## Current preview features

- Dark cosmic UI with Overview, Versions, Mods, Accounts, Settings and Console pages.
- On-demand connection to Mojang's official version manifest. The catalog supports searching and filtering releases, snapshots, old beta and old alpha entries.
- On-demand inspection of a selected version's official Mojang metadata, including Java major-version requirement, client JAR size/checksum, asset index and library count. Metadata URLs and client download hosts are restricted to Mojang HTTPS hosts.
- Local Minecraft-compatible offline UUID generation from a player nickname.
- Low-end, Balanced and High-end starting presets, editable RAM allocation, rendering profile and JVM arguments.
- Settings and the selected version are persisted with Qt settings.
- No permanent animation timers, polling loops, or background network requests. The Mojang manifest is requested only when the Versions page is opened or Refresh is pressed.

## Not implemented yet

This is an active preview, not a finished launcher. The following are not connected yet:

- Actual Minecraft installation: downloading libraries/assets/client JAR, checksum verification on disk, Java executable discovery/validation and game process launch. The current metadata inspector validates the official metadata only; it does not download or launch the game.
- Ely.by and Microsoft authentication.
- Online skin lookup, skin upload and 3D skin preview.
- Modrinth / CurseForge downloads and mod-loader installation.
- Streaming Minecraft process output into the console.
- Signed Windows installer and portable package.

The Launch button intentionally reports that launching is not ready instead of pretending a game was started. The version metadata inspector is a preparatory step, not an installer. Ely.by and Microsoft UUIDs are not fabricated.

## Requirements

- Python 3.11+
- Windows 10/11
- Internet connection for the official Mojang version catalog

## Development

```powershell
py -m venv .venv
.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
python main.py

# Run core metadata validation tests
python -m unittest discover -s tests -v
```

## Build a local preview executable

```powershell
pip install pyinstaller
pyinstaller --noconfirm --windowed --name VOIDEX main.py
```

The executable will be placed in `dist\\VOIDEX\\` (or `dist\\VOIDEX.exe`, depending on the PyInstaller mode). This command has not been run in CI yet; no prebuilt executable is currently included in the repository.

## Performance note

The interface avoids continuous animation and idle polling to keep background overhead low. Actual CPU/RAM usage depends on Windows, Qt, drivers, display scaling and other running software; a fixed 2–5% idle usage cannot be guaranteed.
