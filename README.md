# VOIDEX Launcher

A lightweight, customizable Minecraft Java launcher for Windows, built with Python and PySide6.

## Current preview features

- Dark cosmic UI with Overview, Versions, a working Modrinth mod browser, Accounts, Settings and Console pages.
- On-demand connection to Mojang's official version manifest. The catalog supports searching and filtering releases, snapshots, old beta and old alpha entries.
- On-demand inspection of a selected version's official Mojang metadata, including Java major-version requirement, client JAR size/checksum, asset index and library count. Metadata URLs and client download hosts are restricted to Mojang HTTPS hosts.
- Optional download and caching of the selected version JSON, verified against the SHA-1 recorded in the official version manifest. The JSON is parsed before atomic replacement and cached under the version-specific VOIDEX directory.
- Platform-aware resolution of Mojang library artifacts and native classifiers from version JSON, including ordered allow/disallow rules and feature flags. Resolved paths, HTTPS URLs, SHA-1 values and sizes are validated before a download stage is allowed.
- Core native JAR extraction helper with ZIP path traversal checks, symlink rejection, META-INF filtering, duplicate-path detection, bounded entry/file/total sizes, destination containment checks, and atomic per-file writes. The Versions page now has a **Prepare natives** action that reuses verified native JAR downloads and extracts them in a background worker to `.voidex/versions/<version>/natives`.
- Asset-index object resolution maps each logical asset to its content-addressed SHA-1 path and the fixed Mojang asset host. The Versions page now has an **Install assets** action: it verifies/caches the version JSON, downloads and verifies its asset index, then downloads all required object files in a background worker to `.voidex/assets/objects`; the index is cached under `.voidex/assets/indexes`.
- Core library artifact downloader for resolved Maven entries: same-host HTTPS redirects only, cache verification, streamed size/SHA-1 checks, atomic replacement and temporary-file cleanup. Native classifier JARs are downloaded but not extracted. A sequential batch helper downloads multiple resolved artifacts and reports aggregate byte progress; verified cache entries are reused and conflicting destination paths are rejected before network access. The Versions page now has an **Install libraries** action: it first caches the selected version JSON with manifest SHA-1 verification, resolves Windows x64 library artifacts, then downloads them sequentially in a background worker to `.voidex/libraries`.
- One-button **Install selected version** pipeline: verifies and caches version JSON and client JAR, resolves and downloads Windows x64 libraries, safely extracts native archives, downloads the asset index and every referenced asset object, verifies the complete asset cache, and creates a per-version game directory. All network and extraction work runs in a background thread with stage/progress reporting; existing verified cache files are reused.
- Optional standalone download of the selected client JAR, with streamed progress, byte-count and SHA-1 verification, temporary-file cleanup and atomic replacement. A previously verified cached file is reused.
- Local Minecraft-compatible offline UUID generation from a player nickname.
- Java discovery from JAVA_HOME, PATH and common Windows JDK vendor folders. Inspecting a Minecraft version automatically checks the configured or discovered runtime against its required Java major.
- Explicit **Install required Java** action in Settings downloads the matching Windows x64 Eclipse Temurin JDK from the official Adoptium API, checks the published SHA-256 and byte size, safely extracts the ZIP with traversal/link/resource limits, runs `java.exe -version`, and saves the verified runtime path under `.voidex/runtimes`. It never downloads Java without the user's click.
- Modrinth browser: search real mod projects for a concrete Minecraft version and Fabric / Forge / NeoForge / Quilt filter, fetch a compatible project version, and install its primary JAR into `.voidex/instances/<version>/mods`. Downloads are restricted to the official Modrinth CDN and verified against published SHA-1 or SHA-512, size-checked, written to a temporary file, and atomically installed. Search and installation run in background workers.
- A tested launch-plan builder that checks Java compatibility, required library/client files, builds a platform-specific classpath without native classifier JARs, applies Mojang OS/feature rules, expands modern or legacy game arguments, and returns a shell-free argv/cwd plan. Account credentials must be explicitly supplied; the planner does not fabricate online sessions.
- Low-end, Balanced and High-end starting presets, editable RAM allocation, rendering profile and JVM arguments.
- Settings and the selected version are persisted with Qt settings.
- No permanent animation timers, polling loops, or background network requests. The Mojang manifest is requested only when the Versions page is opened or Refresh is pressed.

## Not implemented yet

This is an active preview, not a finished launcher. The following are not connected yet:

- Full virtual/legacy asset materialization for older releases and authenticated Ely.by/Microsoft sessions. One-button installation currently supports modern indexed assets and Windows x64 library resolution. The current launch preflight verifies the selected version JSON, client JAR, library hashes, asset index and all asset objects, native directory, and Java major before starting a Java process. Offline profiles can launch with a local nickname; online account providers remain blocked until their authentication flows are implemented.
- Ely.by and Microsoft authentication.
- Online skin lookup, skin upload and 3D skin preview.
- CurseForge integration, Quilt loader installation, and full modpack import/management. NeoForge builds can be discovered from the official Maven metadata and installed with the official client installer; launch-profile merging is the next step. Forge and NeoForge profiles can be installed and merged into the launch metadata when their Minecraft version matches; added Maven libraries must already exist locally and pass path, repository, size and checksum checks. Fabric can be installed through its official CLI installer, and the generated profile is merged into Mojang metadata for launch.
- Signed Windows installer and portable package.

The Launch button performs a background preflight and starts a real Java process only when required files pass integrity checks. It currently supports explicit offline nickname sessions only; Ely.by and Microsoft authentication are not fabricated or bypassed. Use **Install selected version** on the Versions page to prepare the client JAR, libraries, assets and natives in one background operation. If a compatible Java runtime is missing, inspect the version and use **Install required Java** in Settings; the matching Temurin runtime is then installed and validated locally. In the Mods page, select a concrete Minecraft version and loader to search and install compatible Modrinth JARs. Fabric builds can be loaded from Fabric Meta and installed with the official Fabric CLI installer; VOIDEX verifies the installer JAR against its Maven SHA-1 sidecar, validates the generated profile, and merges its libraries and launch arguments into the Mojang profile at launch. The installed-mod list shows JAR filenames and sizes, and removal requires confirmation; symbolic links and paths outside the instance mods folder are rejected. Forge build discovery and official installer execution are available from the Mods page. Installer JARs are fetched from the Forge Maven host, checked against the published SHA-1 sidecar, and the generated client profile is validated. When the selected Minecraft version matches the installed Forge profile, VOIDEX merges its main class, launch arguments and Maven libraries into the launch plan; missing or untrusted libraries block launch. NeoForge catalog lookup, verified client installer execution, and launch-profile merging are available. VOIDEX validates the generated profile, merges its launch arguments and main class, and requires every added Maven library to exist locally with matching size/checksum metadata. Quilt remains unfinished.

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
