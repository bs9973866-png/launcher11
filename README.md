# VOIDEX Launcher

A lightweight, customizable Minecraft launcher for Windows, built with Python and PySide6.

## Design goals

- Low idle overhead: no permanent animation timers, polling loops, or background network requests.
- Three performance presets: Low-end, Balanced, and High-end.
- Explicit JVM and rendering profiles; changing a preset never silently overwrites custom settings.
- Account identity panel designed to show the skin UUID and account source (Ely.by, Microsoft, or Offline).
- Built-in log console for launcher and game output.
- Cosmic neon visual design with effects that can be disabled.

## Requirements

- Python 3.11+
- Windows 10/11

## Development

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Build

```powershell
pip install pyinstaller
pyinstaller --noconfirm --windowed --name VOIDEX main.py
```

The first commit contains the lightweight UI foundation. Minecraft downloading, authentication, skin resolution, and process launching will be added as separate tested modules.