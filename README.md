# VOIDEX Launcher

A lightweight, customizable Minecraft Java launcher for Windows, built with Python and PySide6.

## Current preview features

- Dark cosmic UI with Overview, Versions, Mods, Accounts, Settings and Console pages.
- On-demand connection to Mojang's official version manifest. The catalog supports searching and filtering releases, snapshots, old beta and old alpha entries.
- Local Minecraft-compatible offline UUID generation from a player nickname.
- Low-end, Balanced and High-end starting presets, editable RAM allocation, rendering profile and JVM arguments.
- Settings and the selected version are persisted with Qt settings.
- No permanent animation timers, polling loops, or background network requests. The Mojang manifest is requested only when the Versions page is opened or Refresh is pressed.

## Not implemented yet

This is an active preview, not a finished launcher. The following are not connected yet:

- Minecraft version installation, libraries/assets download and game process launch.
- Ely.by and Microsoft authentication.
- Online skin lookup, skin upload and 3D skin preview.
- Modrinth / CurseForge downloads and mod-loader installation.
- Streaming Minecraft process output into the console.
- Signed Windows installer and portable package.

The Launch button intentionally reports that launching is not ready instead of pretending a game was started. Ely.by and Microsoft UUIDs are not fabricated.

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
```

## Build a local preview executable

```powershell
pip install pyinstaller
pyinstaller --noconfirm --windowed --name VOIDEX main.py
```

The executable will be placed in `dist\\VOIDEX\\` (or `dist\\VOIDEX.exe`, depending on the PyInstaller mode). This command has not been run in CI yet; no prebuilt executable is currently included in the repository.

## Performance note

The interface avoids continuous animation and idle polling to keep background overhead low. Actual CPU/RAM usage depends on Windows, Qt, drivers, display scaling and other running software; a fixed 2–5% idle usage cannot be guaranteed.
