# VOIDEX Launcher

A lightweight, customizable Minecraft Java launcher for Windows, built with Python and PySide6.

## Current preview features

- Dark cosmic UI with Overview, Versions, Mods, Accounts, Settings and Console pages.
- On-demand connection to Mojang's official version manifest. The catalog supports searching and filtering releases, snapshots, old beta and old alpha entries.
- On-demand inspection of a selected version's official Mojang metadata, including Java major-version requirement, client JAR size/checksum, asset index and library count. Metadata URLs and client download hosts are restricted to Mojang HTTPS hosts.
- Optional download and caching of the selected version JSON, verified against the SHA-1 recorded in the official version manifest. The JSON is parsed before atomic replacement and cached under the version-specific VOIDEX directory.
- Platform-aware resolution of Mojang library artifacts and native classifiers from version JSON, including ordered allow/disallow rules and feature flags. Resolved paths, HTTPS URLs, SHA-1 values and sizes are validated before a download stage is allowed.
- Core native JAR extraction helper with ZIP path traversal checks, symlink rejection, META-INF filtering, duplicate-path detection, bounded entry/file/total sizes, destination containment checks, and atomic per-file writes. The Versions page now has a **Prepare natives** action that reuses verified native JAR downloads and extracts them in a background worker to `.voidex/versions/<version>/natives`.
- Asset-index object resolution maps each logical asset to its content-addressed SHA-1 path and the fixed Mojang asset host. The Versions page now has an **Install assets** action: it verifies/caches the version JSON, downloads and verifies its asset index, then downloads all required object files in a background worker to `.voidex/assets/objects`; the index is cached under `.voidex/assets/indexes`.
- Core library artifact downloader for resolved Maven entries: same-host HTTPS redirects only, cache verification, streamed size/SHA-1 checks, atomic replacement and temporary-file cleanup. Native classifier JARs are downloaded but not extracted. A sequential batch helper downloads multiple resolved artifacts and reports aggregate byte progress; verified cache entries are reused and conflicting destination paths are rejected before network access. The Versions page now has an **Install libraries** action: it first caches the selected version JSON with manifest SHA-1 verification, resolves Windows x64 library artifacts, then downloads them sequentially in a background worker to `.voidex/libraries`.
- Optional download of the selected client JAR into a local VOIDEX cache, with streamed progress, byte-count and SHA-1 verification, temporary-file cleanup and atomic replacement. A previously verified cached file is reused.
- Local Minecraft-compatible offline UUID generation from a player nickname.
- Low-end, Balanced and High-end starting presets, editable RAM allocation, rendering profile and JVM arguments.
- Settings and the selected version are persisted with Qt settings.
- No permanent animation timers, polling loops, or background network requests. The Mojang manifest is requested only when the Versions page is opened or Refresh is pressed.

## Not implemented yet

This is an active preview, not a finished launcher. The following are not connected yet:

- Remaining full Minecraft installation work: Java executable discovery/validation, building the classpath and game arguments, and launching/monitoring the game process. Library and asset actions cache verified files but do not yet create a playable installation or launch Minecraft.
- Ely.by and Microsoft authentication.
- Online skin lookup, skin upload and 3D skin preview.
- Modrinth / CurseForge downloads and mod-loader installation.
- Streaming Minecraft process output into the console.
- Signed Windows installer and portable package.

The Launch button intentionally reports that launching is not ready instead of pretending a game was started. The metadata inspector, client-JAR cache and library cache are preparatory stages, not a complete installer. Ely.by and Microsoft UUIDs are not fabricated.

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
