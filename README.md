# RA Bridge v0.55 (minimal)

Everdrive N8 Pro → PC → ESP32 adapter bridge. Connect Everdrive to PC, send game to adapter via WiFi.

## Quick start

1. **Run:** `run_ra_bridge.bat` (or `python everdrive_bridge_gui.py`)
2. **Build EXE:** `build_ra_bridge.bat`

## Requirements

- Python 3.x
- `pip install pyserial` (pygame optional for achievement sound)

## Structure

- `everdrive_bridge_gui.py` — main app
- `nes-esp-firmware/` — ESP32 firmware
- `misc/webapp/` — web monitor
- `misc/achievement_alert.html` — achievement popup template
