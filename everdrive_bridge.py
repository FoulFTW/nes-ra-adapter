#!/usr/bin/env python3
"""
Everdrive N8 Pro → PC → ESP32 Adapter Bridge

Reads game CRC from Everdrive via USB (Edio protocol), looks up MD5 in everdrive_games.txt,
and sends SELECT_GAME=md5 to the NES RA Adapter. Works with everdrive-sources/edlink-n8
protocol.

Usage:
  python everdrive_bridge.py                    # Auto-detect ports, run once
  python everdrive_bridge.py --everdrive COM9 --adapter COM6
  python everdrive_bridge.py --watch           # Poll Everdrive periodically
"""

import argparse
import struct
import time
import os
import sys

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("Install pyserial: pip install pyserial")
    sys.exit(1)

# Edio protocol constants (from Edio.cs)
ADDR_PRG = 0x0000000
ADDR_CFG = 0x1800000
CMD_STATUS = 0x10
CMD_GET_MODE = 0x11
CMD_MEM_RD = 0x19
CMD_MEM_WR = 0x1A
CMD_MEM_CRC = 0x1D
CMD_RUN_APP = 0xF1
CFG_BASE = 32
CFG_SIZE = 48  # MapConfig binary size
# MapConfig Ctrl bits (everdrive.h)
CTRL_RST_DELAY = 0x01  # reset delay
CTRL_SS_ON = 0x02   # savestates / vblank hook for in-game menu
CTRL_GG_ON = 0x04   # cheats engine
CTRL_SS_BTN = 0x08  # savestate button
CTRL_UNLOCK = 0x80  # must preserve when writing

# OS settings for boot behavior (experimental - may need adjustment)
# Boot last game is typically an OS-level setting, not game-specific MapConfig
# This may require writing to a different address (system config, not game config)
CTRL_BOOT_LAST = 0x10  # Placeholder bit for "boot last game automatically"


class Edio:
    """Minimal Edio protocol client for Everdrive N8 Pro."""
    
    def __init__(self, port, baud=9600):
        self.port = port
        self.ser = serial.Serial(port=port, baudrate=baud, timeout=1.0, write_timeout=1.0)
        self.ser.dtr = False
        self.ser.rts = False
        time.sleep(0.1)
        self.ser.reset_input_buffer()
    
    def _tx_cmd(self, cmd):
        self.ser.write(bytes([0x2B, 0xD4, cmd, cmd ^ 0xFF]))
    
    def _tx32(self, val):
        self.ser.write(struct.pack("<I", val & 0xFFFFFFFF))
    
    def _tx8(self, val):
        self.ser.write(bytes([val & 0xFF]))
    
    def _rx8(self):
        b = self.ser.read(1)
        if not b:
            raise TimeoutError("Serial read timeout (no data)")
        return ord(b)
    
    def _rx16(self):
        data = self.ser.read(2)
        if len(data) < 2:
            raise TimeoutError("Serial read timeout")
        return struct.unpack("<H", data)[0]
    
    def _rx32(self):
        data = self.ser.read(4)
        if len(data) < 4:
            raise TimeoutError("Serial read timeout")
        return struct.unpack("<I", data)[0]
    
    def _rx_data(self, n):
        data = b""
        while len(data) < n:
            chunk = self.ser.read(n - len(data))
            if not chunk:
                raise TimeoutError("Serial read timeout")
            data += chunk
        return data
    
    def get_status(self):
        self._tx_cmd(CMD_STATUS)
        resp = self._rx16()
        if (resp & 0xFF00) != 0xA500:
            raise IOError(f"Everdrive bad status: 0x{resp:04X}")
        return resp & 0xFF
    
    def exit_service_mode(self):
        """Boot cart if in service mode (USB-only power)."""
        mode = self._get_mode()
        if mode == 0xA1:  # service mode
            self._tx_cmd(CMD_RUN_APP)
            self._rx8()
            time.sleep(0.3)
            for _ in range(15):
                try:
                    time.sleep(0.15)
                    self.ser.close()
                    time.sleep(0.1)
                    self.ser.open()
                    self.get_status()
                    return
                except Exception:
                    pass
            raise IOError("Everdrive boot timeout")
    
    def _get_mode(self):
        self._tx_cmd(CMD_GET_MODE)
        return self._rx8()
    
    def mem_rd(self, addr, length):
        self._tx_cmd(CMD_MEM_RD)
        self._tx32(addr)
        self._tx32(length)
        self._tx8(0)
        return self._rx_data(length)
    
    def mem_wr(self, addr, data):
        """Write data to Everdrive memory."""
        if not data:
            return
        self._tx_cmd(CMD_MEM_WR)
        self._tx32(addr)
        self._tx32(len(data))
        self._tx8(0)
        self.ser.write(data)
        self.ser.flush()
    
    def mem_crc(self, addr, length):
        self._tx_cmd(CMD_MEM_CRC)
        self._tx32(addr)
        self._tx32(length)
        self._tx32(0)
        self._tx8(0)
        return self._rx32()
    
    def get_config(self):
        """Read MapConfig, return PrgSize in bytes."""
        cfg = self.mem_rd(ADDR_CFG, CFG_SIZE)
        prg_mask = cfg[CFG_BASE + 1] & 0x0F
        prg_size = 8192 << prg_mask
        return prg_size

    def get_map_idx(self):
        """Read MapConfig and return mapper index (map_idx).

        In the official edlink-n8 tool, the Everdrive menu uses map_idx=255 (0xFF).
        This is a lightweight read (no mem_crc) and can be used as a "menu vs game" hint.
        """
        cfg = self.mem_rd(ADDR_CFG, CFG_SIZE)
        # MapConfig.cs: map_idx = cfg[base+0] | ((cfg[base+2] & 0xF0) << 4)
        lo = cfg[CFG_BASE + 0]
        hi = (cfg[CFG_BASE + 2] & 0xF0) << 4
        return int(lo | hi)
    
    def get_cheat_savestate_status(self):
        """Read config and return (cheats_on, savestates_on)."""
        cfg = self.mem_rd(ADDR_CFG, CFG_SIZE)
        ctrl = cfg[CFG_BASE + 7]
        cheats_on = bool(ctrl & CTRL_GG_ON)
        savestates_on = bool(ctrl & CTRL_SS_ON)
        return cheats_on, savestates_on
    
    def get_boot_last_game_status(self):
        """Read config and check if boot last game is enabled."""
        cfg = self.mem_rd(ADDR_CFG, CFG_SIZE)
        ctrl = cfg[CFG_BASE + 7]
        # Boot last game might be a different bit or different address
        # This is experimental - exact bit location needs confirmation
        boot_last_enabled = bool(ctrl & CTRL_BOOT_LAST)
        return boot_last_enabled
    
    def set_cheats_savestates_off(self):
        """Write config to force cheats and savestates off. Returns True on success."""
        cfg = bytearray(self.mem_rd(ADDR_CFG, CFG_SIZE))
        ctrl = cfg[CFG_BASE + 7]
        cfg[CFG_BASE + 7] = ctrl & ~(CTRL_GG_ON | CTRL_SS_ON)  # clear bits, preserve rest
        self.mem_wr(ADDR_CFG, bytes(cfg))
        return True
    
    def set_boot_last_game_on(self):
        """Enable boot last game automatically.
        
        NOTE: This is experimental. The exact bit/address for "boot last game" 
        may vary. MapConfig is game-specific; boot behavior might be OS-level.
        If this doesn't work, the setting may be at a different memory address.
        """
        cfg = bytearray(self.mem_rd(ADDR_CFG, CFG_SIZE))
        ctrl = cfg[CFG_BASE + 7]
        cfg[CFG_BASE + 7] = ctrl | CTRL_BOOT_LAST  # set bit, preserve rest
        self.mem_wr(ADDR_CFG, bytes(cfg))
        return True
    
    def get_rom_crcs(self):
        """Get begin and end CRC (same format as Pico/ESP games.txt)."""
        try:
            prg_size = self.get_config()
        except Exception:
            prg_size = 0x40000  # fallback 256K
        if prg_size < 8192:
            raise ValueError("PRG too small")
        # games.txt uses first 512 of last 8K (NES 0xE000 region) - same as crc-md5-mapper
        last_bank_offset = prg_size - 8192
        crc_begin = self.mem_crc(ADDR_PRG, 512)
        crc_end = self.mem_crc(ADDR_PRG + last_bank_offset, 512)
        return f"{crc_begin:08X}", f"{crc_end:08X}"
    
    def close(self):
        self.ser.close()


def load_games_txt(path):
    """Load CRC1,CRC2=MD5 mapping. Skips lines starting with #."""
    table = {}
    if not os.path.exists(path):
        return table
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line or "," not in line:
                continue
            crc_part, md5 = line.split("=", 1)
            crc1, crc2 = crc_part.split(",", 1)
            key = (crc1.upper().strip(), crc2.upper().strip())
            table[key] = md5.strip().lower()
    return table


def load_everdrive_games(script_dir):
    """Load everdrive_games.txt only - separate from games.txt (physical cartridges)."""
    data_dir = os.path.join(script_dir, "nes-esp-firmware", "data")
    everdrive_path = os.path.join(data_dir, "everdrive_games.txt")
    table = load_games_txt(everdrive_path)
    return table, everdrive_path


def load_everdrive_and_games(script_dir):
    """Load everdrive_games.txt + games.txt for fallback lookup."""
    data_dir = os.path.join(script_dir, "nes-esp-firmware", "data")
    ed_table = load_games_txt(os.path.join(data_dir, "everdrive_games.txt"))
    games_table = load_games_txt(os.path.join(data_dir, "games.txt"))
    return ed_table, games_table


def lookup_md5(crc_begin, crc_end, games_table):
    key = (crc_begin.upper(), crc_end.upper())
    return games_table.get(key)


def lookup_md5_with_fallback(crc_begin, crc_end, everdrive_table, games_table):
    """Try everdrive_games.txt first, then games.txt (physical cart CRCs)."""
    md5 = lookup_md5(crc_begin, crc_end, everdrive_table)
    if md5:
        return md5, "everdrive"
    md5 = lookup_md5(crc_begin, crc_end, games_table)
    if md5:
        return md5, "games"
    return None, None


def send_to_adapter(port, md5, baud=9600):
    """Send SELECT_GAME=md5 to adapter (fallback if ESP32 doesn't support CRC)."""
    ser = serial.Serial(port=port, baudrate=baud, timeout=0.5)
    cmd = f"SELECT_GAME={md5}\r\n"
    ser.write(cmd.encode())
    ser.flush()
    ser.close()


def send_to_adapter_crc(port, crc_begin, crc_end, baud=9600):
    """Send EVERDRIVE_READY then SELECT_GAME_CRC. ESP32 skips Pico bus, uses bridge CRC."""
    ser = serial.Serial(port=port, baudrate=baud, timeout=0.5)
    ser.dtr = False
    ser.rts = False
    time.sleep(0.05)
    ser.write(b"EVERDRIVE_READY\r\n")
    ser.write(f"SELECT_GAME_CRC={crc_begin},{crc_end}\r\n".encode())
    ser.flush()
    ser.close()


def find_everdrive_port():
    """Try to find Everdrive by scanning ports."""
    for port in serial.tools.list_ports.comports():
        try:
            ed = Edio(port.device)
            ed.get_status()
            ed.close()
            return port.device
        except Exception:
            pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Everdrive→PC→Adapter bridge")
    parser.add_argument("--everdrive", "-e", help="Everdrive COM port (e.g. COM9)")
    parser.add_argument("--adapter", "-a", help="Adapter COM port (e.g. COM6)")
    parser.add_argument("--games", "-g", default=None,
                        help="Path to everdrive_games.txt (default: nes-esp-firmware/data/everdrive_games.txt)")
    parser.add_argument("--watch", "-w", action="store_true",
                        help="Poll Everdrive every 5s (detect game changes)")
    parser.add_argument("--no-adapter", action="store_true", help="Only read Everdrive, don't send to adapter")
    args = parser.parse_args()
    
    everdrive_port = args.everdrive
    adapter_port = args.adapter
    
    if not everdrive_port:
        everdrive_port = find_everdrive_port()
        if not everdrive_port:
            print("Everdrive not found. Connect Everdrive via USB and specify --everdrive COMx")
            sys.exit(1)
        print(f"Everdrive found at {everdrive_port}")
    
    if not adapter_port and not args.no_adapter:
        # Guess adapter: often different from Everdrive
        all_ports = [p.device for p in serial.tools.list_ports.comports()]
        adapter_port = next((p for p in all_ports if p != everdrive_port), None)
        if not adapter_port:
            print("Adapter not found. Specify --adapter COMx or use --no-adapter")
            sys.exit(1)
        print(f"Adapter at {adapter_port} (ESP32, 9600 baud)")
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if args.games:
        everdrive_table = load_games_txt(args.games)
        games_table = {}
        games_path = args.games
    else:
        everdrive_table, _ = load_everdrive_games(script_dir)
        games_table = load_games_txt(os.path.join(script_dir, "nes-esp-firmware", "data", "games.txt"))
        games_path = "everdrive_games.txt + games.txt fallback"
    print(f"Loaded everdrive: {len(everdrive_table)}, games.txt: {len(games_table)}")
    
    last_crcs = None
    
    while True:
        try:
            ed = Edio(everdrive_port)
            ed.exit_service_mode()
            crc_begin, crc_end = ed.get_rom_crcs()
            ed.close()
            
            print(f"\nCRC: {crc_begin},{crc_end}")
            
            md5, source = lookup_md5_with_fallback(crc_begin, crc_end, everdrive_table, games_table)
            if md5:
                src = "(everdrive)" if source == "everdrive" else "(games.txt)"
                print(f"Game MD5: {md5} {src}")
            if adapter_port and not args.no_adapter and crc_begin != crc_end:
                send_to_adapter_crc(adapter_port, crc_begin, crc_end)
                print(f"Sent SELECT_GAME_CRC={crc_begin},{crc_end} to adapter ({adapter_port})")
            elif crc_begin == crc_end:
                print("Everdrive menu (no game loaded) - boot a game to title screen, then read again")
            else:
                print("Game not found - add to everdrive_games.txt with add_everdrive_crc.py")
            
            if not args.watch:
                break
            
            time.sleep(5)
            
        except Exception as e:
            print(f"Error: {e}")
            if not args.watch:
                sys.exit(1)
            time.sleep(2)


if __name__ == "__main__":
    main()
