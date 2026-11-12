# VOIDEX Launcher

A lightweight, customizable Minecraft Java launcher for Windows, built with Python and PySide6.

## Current preview features

- Dark cosmic UI with Overview, Versions, Mods, Accounts, Settings and Console pages.
- On-demand connection to Mojang's official version manifest. The catalog supports searching and filtering releases, snapshots, old beta and old alpha entries.
- On-demand inspection of a selected version's official Mojang metadata, including Java major-version requirement, client JAR size/checksum, asset index and library count. Metadata URLs and client download hosts are restricted to Mojang HTTPS hosts.
- Optional download and caching of the selected version JSON, verified against the SHA-1 recorded in the official version manifest. The JSON is parsed before atomic replacement and cached under the version-specific VOIDEX directory.
- Platform-aware resolution of Mojang library artifacts and native classifiers from version JSON, including ordered allow/disallow rules and feature flags. Resolved paths, HTTPS URLs, SHA-1 values and sizes are validated before a download stage is allowed.
- Optional download of the selected client JAR into a local VOIDEX cache, with streamed progress, byte-count and SHA-1 verification, temporary-file cleanup and atomic replacement. A previously verified cached file is reused.
- Local Minecraft-compatible offline UUID generation from a player nickname.
- Low-end, Balanced and High-end starting presets, editable RAM allocation, rendering profile and JVM arguments.
- Settings and the selected version are persisted with Qt settings.
- No permanent animation timers, polling loops, or background network requests. The Mojang manifest is requested only when the Versions page is opened or Refresh is pressed.

## Not implemented yet

This is an active preview, not a finished launcher. The following are not connected yet:

- Full Minecraft installation: downloading resolved libraries and assets, Java executable discovery/validation and game process launch. The current pipeline resolves library metadata and caches verified version JSON and the verified client JAR, but still does not download the resolved libraries or assemble a complete game installation; it does not create an installed Minecraft version or launch the game.
- Ely.by and Microsoft authentication.
- Online skin lookup, skin upload and 3D skin preview.
- Modrinth / CurseForge downloads and mod-loader installation.
- Streaming Minecraft process output into the console.
- Signed Windows installer and portable package.

The Launch button intentionally reports that launching is not ready instead of pretending a game was started. The metadata inspector and client-JAR cache are preparatory steps, not a complete installer. Ely.by and Microsoft UUIDs are not fabricated.

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

# Run core validation and library-resolution tests
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
