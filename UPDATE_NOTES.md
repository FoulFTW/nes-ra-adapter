# RA Bridge v0.55 - Update Notes

## Release Summary

**Version:** 0.55  
**Date:** February 2025  
**Repo:** Minimal build for Everdrive N8 Pro → PC → ESP32 adapter bridge

---

## What's New in v0.55

### 1. **Connect-First (Everdrive Mode)**
- Bridge must connect to Everdrive before sending game data
- ESP32 waits for bridge connection before requesting CRC
- Prevents race conditions when adapter boots before PC is ready

### 2. **Read-Before-Send**
- 1.5 second delay between reading game from Everdrive and sending to adapter
- Ensures Everdrive has fully loaded the game before bridge sends CRC
- Reduces "game not found" or wrong-game issues

### 3. **Achievement Images**
- Achievement popups now show badge images (fetched from RetroAchievements)
- ESP32 downloads and displays achievement artwork on TFT
- Bridge GUI shows achievement images in popup window

### 4. **Sidebar UI**
- Dark theme with blue accents
- Sidebar layout for cleaner navigation
- Game info panel with RA compatibility status

### 5. **Security Lockout (Everdrive-Only)**
- If bridge disconnects (USB unplugged or app closed) for 15 seconds:
  - Bus disabled
  - "Ah Ah Ah / You didn't say the magic word" screen
  - Pico commands drained to prevent crashes

---

## Project Structure

| Component | Location | Description |
|-----------|----------|-------------|
| Bridge GUI | `everdrive_bridge_gui.py` | Main app - connect Everdrive, send games |
| ESP32 Firmware | `nes-esp-firmware/` | WiFi, TFT, achievement display |
| Pico Firmware | `nes-pico-firmware/` | CRC, memory bus, rcheevos |
| Web Monitor | `misc/webapp/` | Browser-based status |
| Achievement Popup | `misc/achievement_alert.html` | Template for popup |

---

## How to Update

### ESP32
1. Open `nes-esp-firmware/nes-esp-firmware.ino` in Arduino IDE
2. Upload to ESP32-C3

### Pico
1. Open `nes-pico-firmware/` in VS Code with Pico SDK
2. CMake: Configure → Build
3. Copy generated `.uf2` to Pico (BOOTSEL mode)

### Bridge
1. Run `run_ra_bridge.bat` or `python everdrive_bridge_gui.py`
2. Or build EXE: `build_ra_bridge.bat`

---

## Requirements

- Python 3.x
- `pip install pyserial` (pygame optional for achievement sound)
- Pico SDK 1.5.1 (for Pico firmware)
- Arduino IDE + ESP32 board support (for ESP32 firmware)
