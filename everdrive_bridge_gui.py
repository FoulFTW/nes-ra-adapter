#!/usr/bin/env python3
"""
Everdrive N8 Pro → PC → ESP32 Adapter Bridge (GUI)

Connect Everdrive (COM9) to PC. Read game from Everdrive via USB, look up MD5.
Send to adapter via:
  - WiFi (recommended): Bridge serves game data over HTTP; ESP32 polls. No serial = no reset.
  - Serial: Connect to adapter COM port (legacy).
"""

import io
import os
import struct
import sys
import threading
import time
import urllib.request
import webbrowser

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("Install pyserial: pip install pyserial")
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox, filedialog
except ImportError:
    print("tkinter not available")
    sys.exit(1)

# Import bridge logic
from everdrive_bridge import Edio, load_everdrive_and_games, lookup_md5_with_fallback, send_to_adapter_crc
from bridge_http_server import BridgeHTTPServer, get_local_ip, start_mdns, stop_mdns
from firmware_update import (
    FirmwareHTTPServer, FirmwareHTTPHandler, set_firmware_path, clear_firmware_path,
    set_update_ready, set_gui_request_callback, set_gui_update_done_callback,
    start_updater_mdns, stop_updater_mdns
)
try:
    from ra_client import game_id as ra_game_id, game_image_url as ra_game_image_url
except ImportError:
    ra_game_id = None
    ra_game_image_url = (lambda x: None)
# RA compatibility comes from games.txt - if MD5 is in games.txt, it's compatible. No internet.
try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Sound playback: pygame for volume control + MP3, else winsound (Windows .wav only)
# pygame init is done lazily in _play_achievement_sound to avoid startup crashes
try:
    import pygame
    HAS_PYGAME = True
except Exception:
    HAS_PYGAME = False
    pygame = None
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# Auto-incremented by 0.01 on each build (build_ra_bridge.bat)
VERSION = "0.55"

# Everdrive mode: Read before Send - delay between read and send (ms)
SEND_READ_DELAY_MS = 1500

# Dark theme (futuristic, blue accents - like RA Bridge reference)
BG_DARK = "#0d1117"
BG_CARD = "#161b22"
BG_SIDEBAR = "#010409"
FG_LIGHT = "#e6edf3"
FG_MUTED = "#8b949e"
ACCENT = "#58a6ff"
ACCENT_GLOW = "#388bfd"
BORDER = "#30363d"
SUCCESS = "#3fb950"
ERROR_COLOR = "#f85149"


class EverdriveBridgeGUI:
    def __init__(self, root):
        self.root = root
        self.root.title(f"RA Bridge v{VERSION}")
        self.root.geometry("800x580")
        self.root.minsize(600, 480)
        self._closing_in_progress = False
        # Track menu/game transitions even without "Watch" enabled.
        # Used to clear stored game when user returns to Everdrive menu (CRC begin=end),
        # preventing "hold reset -> enable cheats -> relaunch without re-send".
        self._last_in_everdrive_menu = None
        self._last_crc_pair = None
        self._crc_stable_reads = 0
        self._last_auto_sent_crc_pair = None
        self._last_known_cheats_ok = True
        self._menu_hint = None  # bool or None (best-effort, from map_idx)
        # For server-side logging (avoid spam)
        self._last_server_log_crc_pair = None
        self._last_server_log_in_menu = None
        self._last_server_log_cheats = None
        self._last_server_log_boot_last = None
        # Optional: auto-detect Everdrive COM port and connect (no custom Windows driver needed)
        self._auto_connect_after_id = None
        self._auto_connect_in_progress = False
        self._auto_connect_fail_until = {}  # port -> monotonic seconds until retry
        self._auto_send_after_id = None
        
        self.ed_table = {}
        self.games_table = {}
        self.load_games()
        
        self.create_widgets()
        self.refresh_ports()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self._start_http_server()
        # No auto cheat check on startup - opening Everdrive port causes USB disconnect
        self._set_default_games_txt_path()
        # Start auto-connect loop (disabled by default in UI; loop is lightweight until enabled)
        self._schedule_auto_connect_tick()
        # Allow launcher (.bat/.exe) to preselect port + auto-connect
        self._apply_startup_defaults()
    
    def load_games(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.ed_table, self.games_table = load_everdrive_and_games(script_dir)
    
    def _apply_dark_theme(self):
        """Apply dark theme to root and ttk widgets."""
        self.root.configure(bg=BG_DARK)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=BG_DARK, foreground=FG_LIGHT, fieldbackground=BG_CARD)
        style.configure("TFrame", background=BG_DARK)
        style.configure("TLabel", background=BG_DARK, foreground=FG_LIGHT)
        style.configure("TLabelframe", background=BG_DARK, foreground=FG_LIGHT)
        style.configure("TLabelframe.Label", background=BG_DARK, foreground=FG_LIGHT)
        style.configure("TButton", background=BORDER, foreground=FG_LIGHT)
        style.map("TButton", background=[("active", ACCENT), ("pressed", ACCENT)])
        style.configure("TCheckbutton", background=BG_DARK, foreground=FG_LIGHT)
        style.map("TCheckbutton", background=[("active", BG_DARK)])
        style.configure("TCombobox", fieldbackground=BG_CARD, background=BORDER, foreground=FG_LIGHT)
        style.map("TCombobox", fieldbackground=[("readonly", BG_CARD)])
        style.configure("TNotebook", background=BG_DARK)
        style.configure("TNotebook.Tab", background=BORDER, foreground=FG_LIGHT)
        style.map("TNotebook.Tab", background=[("selected", BG_CARD)])

    def create_widgets(self):
        self._apply_dark_theme()
        self.root.configure(bg=BG_SIDEBAR)
        main = tk.Frame(self.root, bg=BG_DARK, padx=0, pady=0)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # Left sidebar (design from reference)
        sidebar = tk.Frame(main, bg=BG_SIDEBAR, width=140, padx=8, pady=12)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        tk.Label(sidebar, text="RA Bridge", font=("Arial", 11, "bold"), fg=ACCENT, bg=BG_SIDEBAR).pack(pady=(0, 12))
        self._sidebar_btns = []
        for i, text in enumerate(["Everdrive", "Serial Monitor", "Achievements", "Server", "Firmware"]):
            btn = tk.Button(sidebar, text=text, font=("Arial", 9), fg=FG_LIGHT, bg=BG_SIDEBAR, activebackground=BG_CARD,
                            activeforeground=ACCENT, relief=tk.FLAT, cursor="hand2", anchor="w", padx=8, pady=6,
                            command=lambda idx=i: self.notebook.select(idx))
            btn.pack(fill=tk.X, pady=2)
            btn.bind("<Enter>", lambda e, b=btn: b.config(bg=BORDER, fg=ACCENT))
            btn.bind("<Leave>", lambda e, b=btn: b.config(bg=BG_SIDEBAR, fg=FG_LIGHT))
            self._sidebar_btns.append(btn)

        content = tk.Frame(main, bg=BG_DARK, padx=10, pady=10)
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(content)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.notebook = notebook  # keep ref for tab switching

        def _on_tab_changed(event):
            try:
                idx = notebook.index(notebook.select())
                for i, btn in enumerate(self._sidebar_btns):
                    if i == idx:
                        btn.config(bg=BG_CARD, fg=ACCENT)
                    else:
                        btn.config(bg=BG_SIDEBAR, fg=FG_LIGHT)
            except Exception:
                pass
        notebook.bind("<<NotebookTabChanged>>", _on_tab_changed)
        self.root.after(50, lambda: _on_tab_changed(None))  # Initial highlight

        # --- Tab 1: Everdrive ---
        tab_everdrive = ttk.Frame(notebook, padding="5")
        notebook.add(tab_everdrive, text="Everdrive")
        tab_everdrive.columnconfigure(0, weight=1)
        tab_everdrive.rowconfigure(6, weight=1)
        
        ttk.Label(tab_everdrive, text="Everdrive N8 Pro → PC → Adapter Bridge", font=("Arial", 12, "bold")).grid(
            row=0, column=0, pady=(0, 5)
        )
        tab_row1 = ttk.Frame(tab_everdrive)
        tab_row1.grid(row=1, column=0, sticky="w", pady=(0, 10))
        ttk.Label(tab_row1, text=f"Everdrive: {len(self.ed_table)} mappings | games.txt fallback: {len(self.games_table)}", foreground=FG_MUTED).pack(side=tk.LEFT)
        ttk.Label(tab_row1, text="  |  ", foreground=FG_MUTED).pack(side=tk.LEFT)
        fw_link = tk.Label(tab_row1, text="Firmware update →", fg=ACCENT, bg=BG_DARK, cursor="hand2")
        fw_link.pack(side=tk.LEFT)
        fw_link.bind("<Button-1>", lambda e: self.notebook.select(4))
        
        # Ports
        ports_frame = ttk.LabelFrame(tab_everdrive, text="Connection", padding="10")
        ports_frame.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        ports_frame.columnconfigure(1, weight=1)
        
        ttk.Label(ports_frame, text="Everdrive (COM9):").grid(row=0, column=0, padx=(0, 5))
        self.ed_port_var = tk.StringVar(value="COM9")
        self.ed_combo = ttk.Combobox(ports_frame, textvariable=self.ed_port_var, width=12)
        self.ed_combo.grid(row=0, column=1, padx=(0, 10))
        
        ttk.Label(ports_frame, text="Adapter (COM6):").grid(row=1, column=0, padx=(0, 5))
        self.adapter_port_var = tk.StringVar(value="COM6")
        self.adapter_combo = ttk.Combobox(ports_frame, textvariable=self.adapter_port_var, width=12)
        self.adapter_combo.grid(row=1, column=1, padx=(0, 10))
        
        ttk.Button(ports_frame, text="Refresh", command=self.refresh_ports, width=8).grid(row=0, column=2, rowspan=2)
        ttk.Button(ports_frame, text="Connect to Everdrive", command=lambda: self._connect_to_everdrive(silent=False), width=18).grid(row=0, column=3, padx=(8, 0))
        ttk.Label(ports_frame, text="Required first — enables Read Game / Send (prevents cheats in menu)", foreground=FG_MUTED, font=("Arial", 8)).grid(row=1, column=3, sticky="w", padx=(8, 0))
        self.auto_connect_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ports_frame, text="Auto-connect Everdrive", variable=self.auto_connect_var).grid(row=1, column=3, sticky="w", padx=(8, 0))
        self.connection_status_label = tk.Label(ports_frame, text="—", font=("Segoe UI", 16, "bold"), fg=FG_MUTED, bg=BG_DARK)
        self.connection_status_label.grid(row=0, column=4, rowspan=2, padx=(15, 0))
        self.use_wifi_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ports_frame, text="Use WiFi (ESP32 polls - no reset)", variable=self.use_wifi_var,
                       command=self._on_wifi_toggle).grid(row=2, column=0, columnspan=2, sticky="w", pady=(5,0))
        self.everdrive_mode_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ports_frame, text="Everdrive mode (Connect first required)", variable=self.everdrive_mode_var,
                       command=self._on_everdrive_mode_toggle).grid(row=2, column=2, columnspan=2, sticky="w", padx=(15,0))
        self.bridge_url_label = ttk.Label(ports_frame, text="", foreground=SUCCESS)
        self.bridge_url_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2,0))
        self.connect_btn = ttk.Button(ports_frame, text="Connect to Adapter (Serial)", command=self._toggle_adapter_connect, width=22)
        self.connect_btn.grid(row=4, column=0, columnspan=2, sticky="w", pady=(2,0))
        ttk.Label(ports_frame, text="Connect for serial monitor (REQ=, A=, RESP=) — works with WiFi", foreground=FG_MUTED, font=("Arial", 8)).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0,0))
        self.monitor_after_send_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ports_frame, text="Show ESP32 output after send", variable=self.monitor_after_send_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=(2,0))
        
        # Everdrive mode: Connect-first only when checked (cartridge mode = unchecked, no Connect required)
        self.everdrive_mode_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ports_frame, text="Everdrive mode (Connect required before Read/Send)", variable=self.everdrive_mode_var,
                       command=self._on_everdrive_mode_toggle).grid(row=7, column=0, columnspan=2, sticky="w", pady=(2,0))
        # Cheat/savestate status (RA requires both off)
        cheat_frame = ttk.LabelFrame(tab_everdrive, text="Everdrive status (RA requires cheats & savestates OFF)", padding="5")
        cheat_frame.grid(row=3, column=0, sticky="ew", pady=(0, 5))
        cheat_frame.columnconfigure(1, weight=1)
        self.cheat_status_label = ttk.Label(cheat_frame, text="Cheats: — | Savestates: — (Connect Everdrive first)")
        self.cheat_status_label.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.force_cheats_off_btn = ttk.Button(cheat_frame, text="Turn off (while in Everdrive menu)", command=self.force_cheats_off, width=28)
        self.force_cheats_off_btn.grid(row=0, column=1, padx=(0, 0))
        ttk.Label(cheat_frame, text="Connect Everdrive first. RESET to menu, then Turn off. Must be in menu (no game running).", foreground=FG_MUTED, font=("Arial", 8)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2,0))
        # Auto-send (CRC polling) is expensive and can freeze the Everdrive menu / kick to menu on game start.
        self.auto_send_var = tk.BooleanVar(value=False)
        self.auto_send_check = ttk.Checkbutton(cheat_frame, text="Auto-detect game + auto-send (EXPERIMENTAL)", variable=self.auto_send_var)
        self.auto_send_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        cheat_btns = ttk.Frame(cheat_frame)
        cheat_btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.check_cheats_btn = ttk.Button(cheat_btns, text="Check cheats/savestates (test)", command=self.check_cheats_now, width=28)
        self.check_cheats_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.disable_cheats_btn = ttk.Button(cheat_btns, text="Disable cheats/savestates NOW (test)", command=self.disable_cheats_now, width=34)
        self.disable_cheats_btn.pack(side=tk.LEFT)
        self.force_cheats_off_btn.config(state=tk.DISABLED)
        self.auto_send_check.config(state=tk.DISABLED)
        self.check_cheats_btn.config(state=tk.DISABLED)
        self.disable_cheats_btn.config(state=tk.DISABLED)
        
        # Buttons (Read Game + Send to Adapter require Connect first - prevents cheats in menu)
        btn_frame = ttk.Frame(tab_everdrive)
        btn_frame.grid(row=4, column=0, sticky="ew", pady=(0, 5))
        btn_frame.columnconfigure(0, weight=1)
        
        self.read_game_btn = ttk.Button(btn_frame, text="Read Game from Everdrive", command=self.read_game, width=24)
        self.read_game_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.send_to_adapter_btn = ttk.Button(btn_frame, text="Send to Adapter", command=self.send_to_adapter_click, width=16)
        self.send_to_adapter_btn.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Clear CRC", command=self.clear_crc_click, width=10).pack(side=tk.LEFT, padx=(0, 5))
        self.watch_var = tk.BooleanVar()
        self.watch_check = ttk.Checkbutton(btn_frame, text="Watch (poll every 10s)", variable=self.watch_var, command=self.toggle_watch)
        self.watch_check.pack(side=tk.LEFT)
        self.read_game_btn.config(state=tk.DISABLED)
        self.send_to_adapter_btn.config(state=tk.DISABLED)
        self.watch_check.config(state=tk.DISABLED)
        
        # Current game (image + MD5 + RA status)
        game_frame = ttk.LabelFrame(tab_everdrive, text="Current game", padding="5")
        game_frame.grid(row=5, column=0, sticky="ew", pady=(0, 5))
        game_frame.columnconfigure(1, weight=1)
        self.game_image_label = ttk.Label(game_frame, text="(no game)", width=12)
        self.game_image_label.grid(row=0, column=0, padx=(0, 10), sticky="nw")
        self.game_info_label = ttk.Label(game_frame, text="Read game from Everdrive", foreground=FG_MUTED)
        self.game_info_label.grid(row=0, column=1, sticky="w")
        self.ra_status_label = ttk.Label(game_frame, text="RA: —", foreground=FG_MUTED)
        self.ra_status_label.grid(row=1, column=1, sticky="w", pady=(2, 0))
        self.game_photo = None
        
        # Log + ESP32 Monitor split
        log_frame = ttk.LabelFrame(tab_everdrive, text="Log (Everdrive / bridge)", padding="5")
        log_frame.grid(row=6, column=0, sticky="nsew", pady=(0, 5))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        tab_everdrive.rowconfigure(6, weight=2)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, font=("Consolas", 9), bg=BG_CARD, fg=FG_LIGHT, insertbackground=FG_LIGHT)
        self.log_text.grid(row=0, column=0, sticky="nsew")

        # --- Tab 2: Serial Monitor ---
        tab_serial = ttk.Frame(notebook, padding="5")
        notebook.add(tab_serial, text="Serial Monitor")
        tab_serial.columnconfigure(0, weight=1)
        tab_serial.rowconfigure(1, weight=1)
        ttk.Label(tab_serial, text="ESP32 adapter output (REQ=, A=, RESP=) — connect adapter on Everdrive tab", font=("Arial", 12, "bold")).grid(
            row=0, column=0, pady=(0, 8)
        )
        esp32_frame = ttk.Frame(tab_serial)
        esp32_frame.grid(row=1, column=0, sticky="nsew")
        esp32_frame.columnconfigure(0, weight=1)
        esp32_frame.rowconfigure(1, weight=1)
        self.esp32_status_var = tk.StringVar(value="Connect adapter on Everdrive tab to see output")
        ttk.Label(esp32_frame, textvariable=self.esp32_status_var).grid(row=0, column=0, sticky="w")
        ttk.Button(esp32_frame, text="Stop", command=self._stop_monitor, width=6).grid(row=0, column=1, padx=(8,0))
        self.esp32_text = scrolledtext.ScrolledText(esp32_frame, height=20, font=("Consolas", 9), bg=BG_CARD, fg=FG_LIGHT, insertbackground=FG_LIGHT)
        self.esp32_text.grid(row=1, column=0, columnspan=2, sticky="nsew")

        # --- Tab 3: Achievements ---
        tab_achievements = ttk.Frame(notebook, padding="10")
        notebook.add(tab_achievements, text="Achievements")
        tab_achievements.columnconfigure(0, weight=1)
        tab_achievements.rowconfigure(3, weight=1)
        ttk.Label(tab_achievements, text="Achievement alerts — game & achievement images pop up when you unlock", font=("Arial", 12, "bold")).grid(
            row=0, column=0, pady=(0, 8)
        )
        # RA credentials (for game images)
        ach_ra_frame = ttk.LabelFrame(tab_achievements, text="RetroAchievements (for game images)", padding="8")
        ach_ra_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ach_ra_frame.columnconfigure(1, weight=1)
        ttk.Label(ach_ra_frame, text="RA Username:").grid(row=0, column=0, padx=(0, 5), sticky="w")
        self.ach_ra_user_var = tk.StringVar(value=os.environ.get("RA_USERNAME", ""))
        ttk.Entry(ach_ra_frame, textvariable=self.ach_ra_user_var, width=20).grid(row=0, column=1, sticky="w", padx=(0, 10))
        ttk.Label(ach_ra_frame, text="API Key:").grid(row=1, column=0, padx=(0, 5), sticky="w")
        self.ach_ra_key_var = tk.StringVar(value=os.environ.get("RA_API_KEY", ""))
        ttk.Entry(ach_ra_frame, textvariable=self.ach_ra_key_var, width=32, show="*").grid(row=1, column=1, sticky="w", padx=(0, 10))
        ttk.Label(ach_ra_frame, text="From https://retroachievements.org/controlpanel.php", foreground=FG_MUTED, font=("Arial", 8)).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        # Sound settings
        ach_sound_frame = ttk.LabelFrame(tab_achievements, text="Sound", padding="8")
        ach_sound_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        ach_sound_frame.columnconfigure(1, weight=1)
        self.ach_sound_enable_var = tk.BooleanVar(value=True)
        self.ach_popup_var = tk.BooleanVar(value=True)
        self.ach_video_alert_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ach_sound_frame, text="Play sound on achievement", variable=self.ach_sound_enable_var).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(ach_sound_frame, text="Show popup with game + achievement images", variable=self.ach_popup_var).grid(row=0, column=2, columnspan=2, sticky="w", padx=(15, 0))
        try:
            import webview
            ttk.Checkbutton(ach_sound_frame, text="Use video alert (RA Layout Manager style)", variable=self.ach_video_alert_var).grid(row=0, column=4, columnspan=2, sticky="w", padx=(15, 0))
        except ImportError:
            pass
        ttk.Label(ach_sound_frame, text="Sound file:").grid(row=1, column=0, padx=(0, 5), sticky="w")
        self.ach_sound_path_var = tk.StringVar(value="(none)")
        ttk.Label(ach_sound_frame, textvariable=self.ach_sound_path_var, foreground=FG_MUTED).grid(row=1, column=1, sticky="w")
        ttk.Button(ach_sound_frame, text="Browse...", command=self._browse_achievement_sound).grid(row=1, column=2, padx=(8, 0))
        ttk.Label(ach_sound_frame, text="Volume:").grid(row=2, column=0, padx=(0, 5), sticky="w")
        self.ach_volume_var = tk.DoubleVar(value=0.8)
        self.ach_volume_scale = ttk.Scale(ach_sound_frame, from_=0, to=1, variable=self.ach_volume_var, orient=tk.HORIZONTAL, length=150)
        self.ach_volume_scale.grid(row=2, column=1, sticky="w", pady=(2, 0))
        ttk.Label(ach_sound_frame, text="0 = mute, 1 = full", foreground=FG_MUTED, font=("Arial", 8)).grid(row=3, column=1, sticky="w")
        ttk.Button(ach_sound_frame, text="Test sound", command=self._test_achievement_sound, width=10).grid(row=2, column=2, padx=(8, 0))
        if not HAS_PYGAME:
            ttk.Label(ach_sound_frame, text="Install pygame for volume control and MP3: pip install pygame", foreground=FG_MUTED, font=("Arial", 8)).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        # Achievement display box (game + achievement images)
        ach_display_frame = ttk.LabelFrame(tab_achievements, text="Latest achievement", padding="10")
        ach_display_frame.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        ach_display_frame.columnconfigure(1, weight=1)
        ach_display_frame.rowconfigure(1, weight=1)
        ach_images_row = ttk.Frame(ach_display_frame)
        ach_images_row.grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.ach_game_image_label = tk.Label(ach_images_row, text="(game)", font=("Arial", 9), fg=FG_MUTED, bg=BG_CARD, width=8)
        self.ach_game_image_label.pack(side=tk.LEFT, padx=(0, 12))
        self.ach_image_label = tk.Label(ach_images_row, text="", bg=BG_CARD)
        self.ach_image_label.pack(side=tk.LEFT)
        self.ach_photo = None
        self.ach_game_photo = None
        self.ach_title_label = tk.Label(ach_display_frame, text="(no achievement yet)", font=("Arial", 14, "bold"), fg=FG_LIGHT, bg=BG_CARD, wraplength=400)
        self.ach_title_label.grid(row=1, column=0, sticky="w", pady=(0, 4))
        ttk.Label(ach_display_frame, text="Connect adapter on Everdrive tab. Add RA credentials for game images.", foreground=FG_MUTED, font=("Arial", 9)).grid(row=2, column=0, sticky="w", pady=(4, 0))

        # --- Tab 4: Server (what ESP32 sees) ---
        tab_server = ttk.Frame(notebook, padding="10")
        notebook.add(tab_server, text="Server")
        tab_server.columnconfigure(0, weight=1)
        tab_server.rowconfigure(2, weight=1)
        ttk.Label(tab_server, text="Values served to ESP32 (GET /game, GET /status)", font=("Arial", 12, "bold")).grid(
            row=0, column=0, pady=(0, 10)
        )
        tab_server_row1 = ttk.Frame(tab_server)
        tab_server_row1.grid(row=1, column=0, sticky="w", pady=(0, 8))
        ttk.Label(tab_server_row1, text="Refreshes every 2 seconds. This is what the adapter sees when it polls.", foreground=FG_MUTED).pack(side=tk.LEFT)
        ttk.Button(tab_server_row1, text="Open Web App (monitor)", command=lambda: webbrowser.open("http://localhost:9000/"), width=22).pack(side=tk.LEFT, padx=(15, 0))
        ttk.Button(tab_server_row1, text="Copy all", command=self._copy_server_log, width=10).pack(side=tk.LEFT, padx=(5, 0))
        self.server_values_text = scrolledtext.ScrolledText(tab_server, height=28, font=("Consolas", 9), wrap=tk.WORD, bg=BG_CARD, fg=FG_LIGHT, insertbackground=FG_LIGHT)
        self.server_values_text.grid(row=2, column=0, sticky="nsew")
        self.server_values_text.insert(tk.END, "Starting server...")
        self._server_values_after_id = None

        # --- Tab 5: Firmware / Update ---
        tab_firmware = ttk.Frame(notebook, padding="15")
        notebook.add(tab_firmware, text="Firmware update")
        tab_firmware.columnconfigure(0, weight=1)
        tab_firmware.rowconfigure(6, weight=1)

        ttk.Label(tab_firmware, text="Update adapter via WiFi", font=("Arial", 12, "bold")).grid(
            row=0, column=0, pady=(0, 5)
        )
        ttk.Label(tab_firmware, text="Select file, then click Upload. Adapter must be on and connected to WiFi.", foreground=FG_MUTED).grid(
            row=1, column=0, sticky="w", pady=(0, 15)
        )

        # Section 1: ESP32 Firmware
        fw_frame = ttk.LabelFrame(tab_firmware, text="1. ESP32 Firmware", padding="10")
        fw_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        fw_frame.columnconfigure(1, weight=1)
        ttk.Label(fw_frame, text="Firmware (.bin):").grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.firmware_path_var = tk.StringVar(value="(none)")
        ttk.Label(fw_frame, textvariable=self.firmware_path_var, foreground=FG_MUTED).grid(row=0, column=1, sticky="w")
        ttk.Button(fw_frame, text="Browse...", command=self._browse_firmware).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(fw_frame, text="Build: nes-esp-firmware/build/.../nes-esp-firmware.ino.bin", foreground=FG_MUTED, font=("Arial", 8)).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(3, 0)
        )
        ttk.Button(fw_frame, text="Upload firmware to adapter", command=self._upload_firmware_to_adapter, width=24).grid(
            row=2, column=0, columnspan=3, pady=(8, 0)
        )

        # Section 2: games.txt (LittleFS)
        games_frame = ttk.LabelFrame(tab_firmware, text="2. games.txt (LittleFS) — upload to running adapter", padding="10")
        games_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        games_frame.columnconfigure(1, weight=1)
        ttk.Label(games_frame, text="games.txt:").grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.games_txt_path_var = tk.StringVar(value="(none)")
        ttk.Label(games_frame, textvariable=self.games_txt_path_var, foreground=FG_MUTED).grid(row=0, column=1, sticky="w")
        ttk.Button(games_frame, text="Browse...", command=self._browse_games_txt).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(games_frame, text="CRC→MD5 mapping. Default: nes-esp-firmware/data/games.txt", foreground=FG_MUTED, font=("Arial", 8)).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(3, 0)
        )

        # Section 3: Web app files (optional - for future)
        web_frame = ttk.LabelFrame(tab_firmware, text="3. Web app files (optional)", padding="10")
        web_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        web_frame.columnconfigure(1, weight=1)
        ttk.Label(web_frame, text="index.html, sw.js, snd.mp3 — coming soon", foreground=FG_MUTED).grid(row=0, column=0, columnspan=2, sticky="w")

        # Action buttons
        btn_fw_frame = ttk.Frame(tab_firmware)
        btn_fw_frame.grid(row=5, column=0, sticky="ew", pady=(10, 5))
        btn_fw_frame.columnconfigure(0, weight=1)
        self.send_to_adapter_fw_btn = ttk.Button(btn_fw_frame, text="Ready for boot update (adapter fetches on power-on)", command=self._toggle_firmware_server, width=38)
        self.send_to_adapter_fw_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.ready_next_fw_btn = ttk.Button(btn_fw_frame, text="Ready for next update", command=self._ready_for_next_update, width=18)
        self.ready_next_fw_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.ready_next_fw_btn.config(state=tk.DISABLED)
        ttk.Button(btn_fw_frame, text="Upload games.txt", command=self._upload_games_txt, width=18).pack(side=tk.LEFT, padx=(0, 8))
        self.firmware_status_label = ttk.Label(btn_fw_frame, text="", foreground=SUCCESS)
        self.firmware_status_label.pack(side=tk.LEFT, padx=(8, 0))

        fw_log_frame = ttk.LabelFrame(tab_firmware, text="Log", padding="8")
        fw_log_frame.grid(row=6, column=0, sticky="nsew", pady=(0, 5))
        fw_log_frame.columnconfigure(0, weight=1)
        fw_log_frame.rowconfigure(0, weight=1)
        self.firmware_log_text = tk.Text(fw_log_frame, height=8, font=("Consolas", 9), wrap=tk.WORD, bg=BG_CARD, fg=FG_LIGHT, insertbackground=FG_LIGHT)
        self.firmware_log_text.grid(row=0, column=0, sticky="nsew")
        
        self.last_md5 = None
        self.firmware_path = None
        self.games_txt_path = None
        self.firmware_server = None
        self.firmware_server_thread = None
        self.last_crcs = None
        self.last_cheats_on = None
        self.last_savestates_on = None
        self.watch_thread = None
        self.watch_stop = False
        self.adapter_ser = None
        self.monitor_ser = None
        self.monitor_stop = False
        self.monitor_thread = None
        self.http_server = None
        self.http_thread = None
        self.webapp_server = None
        self.webapp_thread = None
        self.cheat_check_stop = False
        self.cheat_check_thread = None
        self._everdrive_check_fail_count = 0  # Require 2 consecutive failures before marking disconnected
        self._cheats_still_on_strikes = 0     # Require multiple confirmations before lockout
        self._port_gone_count = 0  # Require 2 consecutive "port gone" before reporting to ESP (avoids USB hiccups)
        self._ed_connection = None  # Persistent connection - never open/close during game (avoids false disconnect)
        self._ed_lock = threading.Lock()
        self._port_check_after_id = None  # root.after id for 5-second port check
        self._server_values_after_id = None
        self._start_port_check_loop()
    
    def _schedule_server_values_refresh(self):
        """Refresh Server tab every 2 seconds."""
        def refresh():
            if self._server_values_after_id is None:
                return
            try:
                if not self.http_server:
                    msg = "Server not ready — HTTP server may have failed to start."
                else:
                    game_buf = self.http_server.get_current_game_peek()
                    status = self.http_server.get_status_snapshot()
                    req_log = self.http_server.get_request_log()
                    sec_ago, last_client = self.http_server.get_server_status()
                    server_status = "Server: Running"
                    if sec_ago is not None and last_client:
                        server_status += f" | Last request: {int(sec_ago)}s ago from {last_client}"
                    else:
                        server_status += " | (no requests yet)"
                    lines = [
                        f"=== {server_status} ===",
                        "(Keep web app open to poll — confirms server alive when ESP disconnects)",
                        "",
                        "=== CURRENT VALUE (what ESP32 would get) ===",
                        "GET /game:",
                        "  " + (game_buf if game_buf else "(empty / no game)"),
                        "",
                        "GET /status (JSON):",
                        f"  heartbeat: {status['heartbeat']}",
                        f"  everdrive_connected: {status['everdrive_connected']}",
                        f"  cheats_ok: {status['cheats_ok']}",
                        f"  last_check: {status['last_check']}",
                        f"  active_crc: {status.get('active_crc') or '(none)'}",
                        f"  active_crc_in_menu: {status.get('active_crc_in_menu') if status.get('active_crc_in_menu') is not None else '(unknown)'}",
                        f"  active_crc_at: {status.get('active_crc_at') or 0}",
                        f"  game: {status.get('game') or '(none)'}",
                        "",
                        "=== ALL ACTIVITY (RECV=from ESP, APP SENT=bridge, SERVER tick=alive every 2s) ===",
                    ]
                    if req_log:
                        lines.extend(req_log)
                        lines.append("")
                        lines.append(f"  — {len(req_log)} entries (newest at bottom)")
                    else:
                        lines.append("  (none yet — connect Everdrive, Read Game, Send to Adapter)")
                    msg = "\n".join(lines)
                # Capture scroll state before replace; restore if user scrolled up (reading older entries)
                y1, y2 = self.server_values_text.yview()
                was_at_bottom = y2 >= 0.99
                self.server_values_text.delete(1.0, tk.END)
                self.server_values_text.insert(tk.END, msg)
                if was_at_bottom:
                    self.server_values_text.see(tk.END)
                else:
                    self.server_values_text.yview_moveto(y1)
            except Exception as e:
                self.server_values_text.delete(1.0, tk.END)
                self.server_values_text.insert(tk.END, f"Error refreshing: {e}")
                self.server_values_text.see(tk.END)
            self._server_values_after_id = self.root.after(2000, refresh)
        self._server_values_after_id = self.root.after(500, refresh)  # First refresh in 500ms

    def _copy_server_log(self):
        """Copy full Server tab content to clipboard."""
        try:
            text = self.server_values_text.get(1.0, tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()  # Keep clipboard after window closes
            self.log("[Server] Copied to clipboard")
        except Exception as e:
            self.log(f"[Server] Copy failed: {e}")

    def _port_exists(self):
        """True if selected Everdrive port exists (no open/close - avoids USB signals)."""
        port = self.ed_port_var.get().strip()
        if not port:
            return False
        try:
            available = [p.device for p in serial.tools.list_ports.comports()]
            return any(p.upper() == port.upper() for p in available)
        except Exception:
            return False

    def _start_port_check_loop(self):
        """Check Everdrive port every 3 seconds - update status for ESP and GUI."""
        PORT_CHECK_MS = 3000
        def check():
            if self._port_check_after_id is None:
                return
            port = self.ed_port_var.get().strip()
            if not port:
                self._port_check_after_id = self.root.after(PORT_CHECK_MS, check)
                return
            exists = self._port_exists()
            with self._ed_lock:
                ed = self._ed_connection
            if ed and not exists:
                self._port_gone_count += 1
                if self._port_gone_count >= 1:  # Detect unplug quickly (~3s)
                    self._port_gone_count = 0
                    with self._ed_lock:
                        self._close_everdrive_connection()
                    if self.http_server:
                        cheats_ok = not (self.last_cheats_on or self.last_savestates_on) if self.last_cheats_on is not None else True
                        self.http_server.set_status(cheats_ok, False)
                    self._update_connection_status_display(False)
                    self.log("[Everdrive] Port gone - disconnected")
            elif ed and exists:
                self._port_gone_count = 0  # Port back - reset
                if self.http_server:
                    cheats_ok = not (self.last_cheats_on or self.last_savestates_on) if self.last_cheats_on is not None else True
                    self.http_server.set_status(cheats_ok, True)
                self._update_connection_status_display(True)
            else:
                self._port_gone_count = 0  # No active connection - reset
                # IMPORTANT: For ESP32 anti-cheat, everdrive_connected should mean
                # "USB device present" (port exists), not "service-mode session currently open".
                # The persistent connection can temporarily drop during resets/brief USB hiccups.
                self._update_connection_status_display(False)
                if self.http_server:
                    cheats_ok = not (self.last_cheats_on or self.last_savestates_on) if self.last_cheats_on is not None else True
                    self.http_server.set_status(cheats_ok, exists)
            self._port_check_after_id = self.root.after(PORT_CHECK_MS, check)
        self._port_check_after_id = self.root.after(PORT_CHECK_MS, check)

    def _update_connection_status_display(self, connected):
        """Update the big Connected! / Disconnected label."""
        if connected:
            self.connection_status_label.config(text="Connected!", fg=SUCCESS)
        else:
            self.connection_status_label.config(text="Disconnected", fg=ERROR_COLOR)
        # Everdrive mode: require Connect. Non-Everdrive: buttons always enabled.
        everdrive_mode = getattr(self, "everdrive_mode_var", None) and self.everdrive_mode_var.get()
        effective = connected or not everdrive_mode
        self._update_everdrive_dependent_buttons(effective)

    def _on_everdrive_mode_toggle(self):
        """Re-apply button states when Everdrive mode checkbox changes."""
        with getattr(self, "_ed_lock", threading.Lock()):
            connected = bool(getattr(self, "_ed_connection", None))
        self._update_everdrive_dependent_buttons(connected)

    def _update_everdrive_dependent_buttons(self, connected):
        """Enable/disable Read Game, Send to Adapter, etc. - require Connect first only in Everdrive mode."""
        everdrive_mode = getattr(self, "everdrive_mode_var", None) and self.everdrive_mode_var.get()
        state = tk.NORMAL if (not everdrive_mode or connected) else tk.DISABLED
        for btn in (getattr(self, "read_game_btn", None), getattr(self, "send_to_adapter_btn", None),
                    getattr(self, "force_cheats_off_btn", None), getattr(self, "check_cheats_btn", None),
                    getattr(self, "disable_cheats_btn", None)):
            if btn:
                try:
                    btn.config(state=state)
                except Exception:
                    pass
        for w in (getattr(self, "watch_check", None), getattr(self, "auto_send_check", None)):
            if w:
                try:
                    w.config(state=state)
                except Exception:
                    pass
        if not connected and getattr(self, "cheat_status_label", None):
            try:
                self.cheat_status_label.config(text="Cheats: — | Savestates: — (Connect Everdrive first)", foreground=FG_MUTED)
            except Exception:
                pass

    def _close_everdrive_connection(self):
        """Close persistent Everdrive connection. Call with _ed_lock held."""
        if self._ed_connection:
            try:
                self._ed_connection.close()
            except Exception:
                pass
            self._ed_connection = None
    
    def _do_everdrive_status_check(self):
        """Use persistent connection - no open/close (avoids USB disconnect signals).
        
        Security for Everdrive mode:
        - Force-disable cheats/savestates periodically (every 10s)
        - If cheats/savestates REMAIN ON after forced disable → SECURITY ALERT
        """
        if not self.http_server:
            return False
        with self._ed_lock:
            ed = self._ed_connection
        if not ed:
            return False
        try:
            ed.exit_service_mode()
            # Best-effort menu detection without CRC polling (no mem_crc):
            # The Everdrive menu typically uses map_idx=255 (0xFF) in MapConfig.
            try:
                map_idx = ed.get_map_idx()
                self._menu_hint = (map_idx == 0xFF)
            except Exception:
                self._menu_hint = None

            # CRC polling is EXPENSIVE (mem_crc) and can freeze Everdrive menu / kick to menu on game start.
            # Keep it opt-in only.
            in_menu = None
            crc_begin = None
            crc_end = None
            if getattr(self, "auto_send_var", None) and self.auto_send_var.get():
                try:
                    crc_begin, crc_end = ed.get_rom_crcs()
                    in_menu = (crc_begin == crc_end)
                except Exception:
                    in_menu = None
                # Publish best-effort active CRC to server for ESP-side game-change detection.
                try:
                    if self.http_server and in_menu is not None and crc_begin and crc_end:
                        self.http_server.set_active_crc(crc_begin, crc_end, in_menu)
                except Exception:
                    pass

                if in_menu is True:
                    if self._last_in_everdrive_menu is not True:
                        self.root.after(0, lambda: self.log("[Everdrive] Menu detected -> cleared stored game; re-send required"))
                    self._last_in_everdrive_menu = True
                    self._last_crc_pair = None
                    self._crc_stable_reads = 0
                    self._last_auto_sent_crc_pair = None
                    try:
                        self.http_server.clear_current_game()
                    except Exception:
                        pass
                    self.root.after(0, self._clear_last_game)
                elif in_menu is False:
                    self._last_in_everdrive_menu = False
                    pair = (crc_begin, crc_end)
                    if self._last_crc_pair == pair:
                        self._crc_stable_reads += 1
                    else:
                        self._last_crc_pair = pair
                        self._crc_stable_reads = 1

                # Server log for CRC only when CRC polling enabled
                try:
                    if self.http_server:
                        pair = (crc_begin, crc_end) if in_menu is not None else None
                        if pair != self._last_server_log_crc_pair or in_menu != self._last_server_log_in_menu:
                            self._last_server_log_crc_pair = pair
                            self._last_server_log_in_menu = in_menu
                            if pair:
                                self.http_server.log_app_sent("everdrive_crc", f"{pair[0]},{pair[1]} menu={in_menu}")
                            else:
                                self.http_server.log_app_sent("everdrive_crc", "unavailable")
                except Exception:
                    pass

            # If CRC polling is OFF, still allow menu-aware behavior using map_idx hint.
            # Only act on it when we have a confident value.
            if in_menu is None and self._menu_hint is not None:
                in_menu = self._menu_hint

            # If we detect menu, clear stored game and require re-send (even without CRC polling).
            if in_menu is True:
                if self._last_in_everdrive_menu is not True:
                    self.root.after(0, lambda: self.log("[Everdrive] Menu detected (map_idx) -> cleared stored game; re-send required"))
                self._last_in_everdrive_menu = True
                self._last_crc_pair = None
                self._crc_stable_reads = 0
                self._last_auto_sent_crc_pair = None
                try:
                    if self.http_server:
                        self.http_server.clear_current_game()
                except Exception:
                    pass
                self.root.after(0, self._clear_last_game)
            elif in_menu is False:
                self._last_in_everdrive_menu = False

            # SECURITY:
            # - In menu: scan frequently and force-disable cheats/savestates (safe time to write config).
            # - In game: do NOT write (can disrupt). Only read; if ON -> lockout.
            if in_menu is True:
                try:
                    ed.set_cheats_savestates_off()
                except Exception:
                    pass

            # Read AFTER write for visibility + enforcement.
            cheats_on, savestates_on = ed.get_cheat_savestate_status()
            try:
                if self.http_server:
                    cpair = (bool(cheats_on), bool(savestates_on))
                    if cpair != self._last_server_log_cheats:
                        self._last_server_log_cheats = cpair
                        self.http_server.log_app_sent("everdrive_cheats", f"cheats={cpair[0]} savestates={cpair[1]}")
            except Exception:
                pass

            if cheats_on or savestates_on:
                # USER REQUEST: 1 strike only. Detect cheats -> lockout (when a game is active).
                try:
                    game_line = self.http_server.get_current_game_peek() if self.http_server else None
                except Exception:
                    game_line = None
                self.root.after(0, lambda: self.log(
                    f"[Security] 🚫 Cheats/Savestates ON after forced disable "
                    f"(cheats={cheats_on}, savestates={savestates_on})"
                ))
                if game_line:
                    try:
                        self.http_server.set_lockout("cheats_or_savestates")
                    except Exception:
                        pass
                    self.http_server.set_status(False, self._port_exists())
                    return False
                # Pre-game: just report cheats not OK (no lockout yet)
                if self.http_server:
                    self.http_server.set_status(False, self._port_exists())
            else:
                self._cheats_still_on_strikes = 0
            
            # NOTE: Boot-last-game enforcement was removed.
            # The bit/address is experimental and can corrupt a real Everdrive setting / freeze the menu.
            
            cheats_ok = not (cheats_on or savestates_on)
            self._last_known_cheats_ok = cheats_ok
            self._everdrive_check_fail_count = 0
            if self.http_server:
                self.http_server.set_status(cheats_ok, True)
            self.root.after(0, lambda c=cheats_on, s=savestates_on: self._update_cheat_status_display(c, s))

            # Auto-send is opt-in only (see CRC polling above)
            if (getattr(self, "auto_send_var", None) and self.auto_send_var.get() and
                    in_menu is False and self._crc_stable_reads >= 2 and
                    crc_begin is not None and crc_end is not None and crc_begin != crc_end):
                pair = (crc_begin, crc_end)
                if self._last_auto_sent_crc_pair != pair:
                    if not cheats_ok:
                        self.root.after(0, lambda: self.log("[Auto] Launch detected but cheats/savestates not OK -> not sending"))
                    else:
                        md5, _ = lookup_md5_with_fallback(crc_begin, crc_end, self.ed_table, self.games_table)
                        if self.use_wifi_var.get():
                            self._store_game_wifi(crc_begin, crc_end)
                            self.root.after(0, lambda: self.log(f"[Auto] Sent CRC to adapter (WiFi): {crc_begin},{crc_end}"))
                        elif self.adapter_ser and self.adapter_ser.is_open and md5:
                            try:
                                self.adapter_ser.write(f"SELECT_GAME={md5}\r\n".encode())
                                self.adapter_ser.flush()
                                self.root.after(0, lambda: self.log(f"[Auto] Sent MD5 to adapter (Serial): {md5}"))
                            except Exception:
                                pass
                        else:
                            self.root.after(0, lambda: self.log("[Auto] Launch detected but adapter not connected (enable WiFi or connect serial)"))
                        self._last_auto_sent_crc_pair = pair
            return True
        except Exception:
            self._everdrive_check_fail_count += 1
            if self._everdrive_check_fail_count >= 2:
                with self._ed_lock:
                    self._close_everdrive_connection()
                if self.http_server:
                    # Status check failed (communication), not a confirmed cheat breach.
                    # Keep cheats_ok=True (unknown treated as OK) and use port existence for connection status.
                    self.http_server.set_status(True, self._port_exists())
                self._last_known_cheats_ok = True
                self.root.after(0, lambda: self._update_cheat_status_display(None, None))
                self.root.after(0, lambda: self._update_connection_status_display(False))
            return False

    def _cheat_check_loop(self):
        """Background: cheat/savestate check every 1 second."""
        time.sleep(1)  # Brief delay after connect
        while not self.cheat_check_stop:
            try:
                self._do_everdrive_status_check()
            except Exception:
                pass
            interval_s = 1.0
            steps = int(interval_s / 0.1)
            if steps < 1:
                steps = 1
            for _ in range(steps):
                if self.cheat_check_stop:
                    return
                time.sleep(0.1)
            # Note: set_status is called inside _do_everdrive_status_check(), no need to call again
    
    def _start_cheat_check_if_needed(self):
        """Start cheat check only after user has connected. Never on app startup."""
        if self.cheat_check_thread and self.cheat_check_thread.is_alive():
            return
        self.cheat_check_stop = False
        self.cheat_check_thread = threading.Thread(target=self._cheat_check_loop, daemon=True)
        self.cheat_check_thread.start()
        self.log("[Check] Cheat/savestate scan: every 1s")

    def _start_http_server(self):
        """Start HTTP server for WiFi delivery. ESP32 polls GET /game."""
        try:
            if getattr(sys, 'frozen', False):
                app_dir = os.path.dirname(sys.executable)
            else:
                app_dir = os.path.dirname(os.path.abspath(__file__))
            persist_path = os.path.join(app_dir, "last_game.json")
            self.http_server = BridgeHTTPServer(host="0.0.0.0", port=8080, persist_path=persist_path)
            self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
            self.http_thread.start()
            ip = get_local_ip()
            if start_mdns(ip, 8080):
                self.bridge_url_label.config(text=f"WiFi: http://everdrive-bridge.local:8080/game (or {ip}:8080)")
            else:
                self.bridge_url_label.config(text=f"WiFi: http://{ip}:8080/game (install zeroconf for .local)")
            self.log("[WiFi] Bridge HTTP server on port 8080")
            self._start_webapp_server()
        except OSError as e:
            self.bridge_url_label.config(text="Could not start HTTP server (port 8080 in use?)", foreground=ERROR_COLOR)
            self.log(f"[WiFi] HTTP server failed: {e}")
        self._schedule_server_values_refresh()

    def _stop_http_server(self):
        stop_mdns()
        self._stop_webapp_server()
        if self.http_server:
            try:
                self.http_server.shutdown()
                self.http_server.server_close()
            except Exception:
                pass
            self.http_server = None
        self.http_thread = None

    def _start_webapp_server(self):
        """Start web app server on port 9000 for monitoring (device logs, status)."""
        try:
            import http.server
            import socketserver
            webapp_dir = self._get_webapp_dir()
            if not webapp_dir or not os.path.isfile(os.path.join(webapp_dir, "index.html")):
                self.log("[Web app] index.html not found — skipping")
                return

            class WebAppHandler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    kwargs['directory'] = webapp_dir
                    super().__init__(*args, **kwargs)

            WebAppHandler.extensions_map['.js'] = 'application/javascript'
            self.webapp_server = socketserver.TCPServer(("", 9000), WebAppHandler)
            self.webapp_server.allow_reuse_address = True
            self.webapp_thread = threading.Thread(target=self.webapp_server.serve_forever, daemon=True)
            self.webapp_thread.start()
            self.log("[Web app] http://localhost:9000/ (Open Monitor for device logs)")
            self.root.after(500, lambda: webbrowser.open("http://localhost:9000/"))
        except OSError as e:
            self.log(f"[Web app] Port 9000 in use or failed: {e}")
        except Exception as e:
            self.log(f"[Web app] Failed: {e}")

    def _get_webapp_dir(self):
        if getattr(sys, 'frozen', False):
            return os.path.join(sys._MEIPASS, 'webapp')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'misc', 'webapp')

    def _stop_webapp_server(self):
        if self.webapp_server:
            try:
                self.webapp_server.shutdown()
                self.webapp_server.server_close()
            except Exception:
                pass
            self.webapp_server = None
        self.webapp_thread = None

    def _on_wifi_toggle(self):
        self._update_bridge_url_display()

    def _on_everdrive_mode_toggle(self):
        """Everdrive mode: when True, require Connect first. When False, enable all Everdrive buttons."""
        with self._ed_lock:
            ed = self._ed_connection
        connected = not self.everdrive_mode_var.get() or bool(ed)
        self._update_everdrive_dependent_buttons(connected)

    def _update_bridge_url_display(self):
        if self.use_wifi_var.get() and self.http_server:
            ip = get_local_ip()
            self.bridge_url_label.config(text=f"WiFi: ESP32 polls http://{ip}:8080/game", foreground=SUCCESS)
        elif self.use_wifi_var.get():
            self.bridge_url_label.config(text="WiFi: server starting...", foreground=FG_MUTED)
        else:
            self.bridge_url_label.config(text="Using Serial - connect to adapter port above", foreground=FG_MUTED)

    def _connect_to_everdrive(self, silent: bool = False):
        """Connect to Everdrive and keep port open - cheat check uses same connection."""
        port = self.ed_port_var.get().strip()
        if not port:
            if not silent:
                messagebox.showwarning("No port", "Select Everdrive port first.")
            return
        if not self.http_server:
            if not silent:
                messagebox.showwarning("Not ready", "Bridge server not running.")
            return
        for attempt in range(3):
            ed = None
            try:
                if attempt > 0:
                    self.log(f"[Everdrive] Retry {attempt + 1}/3 in 5s...")
                    time.sleep(5)
                with self._ed_lock:
                    self._close_everdrive_connection()
                    ed = Edio(port)
                    ed.exit_service_mode()
                    cheats_on, savestates_on = ed.get_cheat_savestate_status()
                    self._ed_connection = ed  # Keep open - no close
                    ed = None
                self._everdrive_check_fail_count = 0
                if self.http_server:
                    self.http_server.set_status(not (cheats_on or savestates_on), True)
                self._update_cheat_status_display(cheats_on, savestates_on)
                self.root.after(0, lambda: self._update_connection_status_display(True))
                self.log("[Everdrive] Connected (port stays open)")
                self._start_cheat_check_if_needed()
                if not silent:
                    messagebox.showinfo("Connected", "Everdrive connected. Port stays open - security checks are active.")
                return
            except Exception as e:
                self.log(f"[Everdrive] Connect failed: {e}")
                with self._ed_lock:
                    self._close_everdrive_connection()
                if ed:
                    try:
                        ed.close()
                    except Exception:
                        pass
        if not silent:
            messagebox.showerror("Connect failed", "Could not connect after 3 attempts.\n\nIf Everdrive just powered on, wait 10s for USB to stabilize, then try again.")

    def _store_game_wifi(self, crc_begin, crc_end):
        """Store game in HTTP buffer for ESP32 to fetch."""
        if self.http_server:
            self.http_server.store_game(crc_begin, crc_end)
        # Treat "Send to Adapter" as a launch moment: immediately re-check cheats/savestates.
        # (User might have toggled options right before starting a game.)
        try:
            self._do_everdrive_status_check()
        except Exception:
            pass

    def _update_cheat_status_display(self, cheats_on, savestates_on):
        if cheats_on is None:
            self.cheat_status_label.config(text="Cheats: — | Savestates: —", foreground=FG_MUTED)
            return
        c = "ON" if cheats_on else "OFF"
        s = "ON" if savestates_on else "OFF"
        color = "red" if (cheats_on or savestates_on) else "green"
        color_val = SUCCESS if color == "green" else ERROR_COLOR if color == "red" else FG_MUTED
        self.cheat_status_label.config(text=f"Cheats: {c} | Savestates: {s} (auto-check every 1s)", foreground=color_val)

    def force_cheats_off(self):
        """Write config to turn cheats/savestates off. Must be in Everdrive menu (RESET, no game running)."""
        with self._ed_lock:
            ed = self._ed_connection
        if not ed:
            messagebox.showwarning("Connect first", "Connect to Everdrive first.")
            return
        port = self.ed_port_var.get().strip()
        if not port:
            messagebox.showwarning("No port", "Select Everdrive port first.")
            return
        try:
            ed.exit_service_mode()
            cheats_on, savestates_on = ed.get_cheat_savestate_status()
            if not cheats_on and not savestates_on:
                self.log("[Everdrive] Cheats and savestates already OFF")
                messagebox.showinfo("Already off", "Cheats and savestates are already disabled.")
                return
            ed.set_cheats_savestates_off()
            time.sleep(0.2)
            cheats_on, savestates_on = ed.get_cheat_savestate_status()
            self.last_cheats_on = cheats_on
            self.last_savestates_on = savestates_on
            self._update_cheat_status_display(cheats_on, savestates_on)
            self.log("[Everdrive] Turn off sent (while in menu)")
            if cheats_on or savestates_on:
                self.log("  Still ON - were you in Everdrive menu? RESET to menu first, then try again.")
                messagebox.showinfo("Still enabled",
                    "Still ON. This only works when you're in the Everdrive menu (no game running).\n\n"
                    "RESET the NES to return to Everdrive menu, then click 'Turn off' again.")
            else:
                messagebox.showinfo("Done", "Cheats and savestates off. Select your game and they'll stay off.")
        except Exception as e:
            self.log(f"Turn off error: {e}")
            messagebox.showerror("Error", str(e))

    def disable_cheats_now(self):
        """TEST button: attempt to disable cheats/savestates immediately (may disrupt gameplay)."""
        with self._ed_lock:
            ed = self._ed_connection
        if not ed:
            messagebox.showwarning("Connect first", "Connect to Everdrive first.")
            return
        port = self.ed_port_var.get().strip()
        if not port:
            messagebox.showwarning("No port", "Select Everdrive port first.")
            return
        try:
            ed.exit_service_mode()
            cheats_on, savestates_on = ed.get_cheat_savestate_status()
            self.log(f"[Test] Before disable: cheats={cheats_on} savestates={savestates_on}")
            ed.set_cheats_savestates_off()
            time.sleep(0.2)
            cheats_on2, savestates_on2 = ed.get_cheat_savestate_status()
            self.log(f"[Test] After disable: cheats={cheats_on2} savestates={savestates_on2}")
            self.last_cheats_on = cheats_on2
            self.last_savestates_on = savestates_on2
            self._update_cheat_status_display(cheats_on2, savestates_on2)
            if self.http_server:
                self.http_server.set_status(not (cheats_on2 or savestates_on2), True)
            messagebox.showinfo("Test complete",
                                "Disable attempt sent.\n\nIf this crashes or kicks to menu, it confirms Everdrive USB writes are not safe during gameplay.")
        except Exception as e:
            self.log(f"[Test] Disable now error: {e}")
            messagebox.showerror("Error", str(e))

    def check_cheats_now(self):
        """TEST button: read-only check of cheats/savestates right now."""
        with self._ed_lock:
            ed = self._ed_connection
        if not ed:
            messagebox.showwarning("Connect first", "Connect to Everdrive first.")
            return
        port = self.ed_port_var.get().strip()
        if not port:
            messagebox.showwarning("No port", "Select Everdrive port first.")
            return
        try:
            ed.exit_service_mode()
            cheats_on, savestates_on = ed.get_cheat_savestate_status()
            self.last_cheats_on = cheats_on
            self.last_savestates_on = savestates_on
            self._update_cheat_status_display(cheats_on, savestates_on)
            if self.http_server:
                self.http_server.set_status(not (cheats_on or savestates_on), True)
            self.log(f"[Test] Check: cheats={cheats_on} savestates={savestates_on}")
        except Exception as e:
            self.log(f"[Test] Check error: {e}")
            messagebox.showerror("Error", str(e))

    def clear_crc_click(self):
        """Clear game buffer and selection so you can switch games."""
        self._clear_last_game()
        self.log("[Clear] CRC cleared - ready to switch games")

    def _games_txt_md5_set(self):
        """Set of MD5s in games.txt - these are RA compatible. No internet lookup."""
        return set(v.lower() for v in self.games_table.values())

    def _update_game_display(self):
        """Update the Current game panel (image + info + RA status)."""
        self.game_photo = None
        if self.last_md5:
            self.game_info_label.config(text=f"MD5: {self.last_md5}", foreground=FG_LIGHT)
            # RA status from games.txt only - if MD5 is in games.txt, it's compatible
            md5_lower = self.last_md5.lower()
            if md5_lower in self._games_txt_md5_set():
                self.ra_status_label.config(text="RA: Compatible", foreground=SUCCESS)
            else:
                self.ra_status_label.config(text="RA: Not in RA", foreground=ERROR_COLOR)
            self._load_game_image_async(self.last_md5)
        else:
            self.game_image_label.config(image="", text="(no game)")
            self.game_info_label.config(text="Read game from Everdrive", foreground=FG_MUTED)
            self.ra_status_label.config(text="RA: —", foreground=FG_MUTED)

    def _load_game_image_async(self, md5):
        """Optionally fetch game image from RA in background (only when Compatible)."""
        md5_lower = md5.lower()
        if md5_lower not in self._games_txt_md5_set():
            return  # Not in RA, no image
        if not ra_game_id or not HAS_PIL:
            return
        def load():
            try:
                gid, err = ra_game_id(md5)
                if gid is not None:
                    url = f"https://media.retroachievements.org/Images/{gid}.png"
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        data = resp.read()
                    img = Image.open(io.BytesIO(data)).resize((96, 96), Image.Resize.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    self.root.after(0, lambda: self._show_game_image(photo, gid))
            except Exception:
                pass  # Image fetch failed; RA status already shows Compatible from games.txt
        threading.Thread(target=load, daemon=True).start()

    def _show_game_image(self, photo, game_id=None):
        """Show game image on main thread."""
        self.game_photo = photo
        self.game_image_label.config(image=photo, text="")
        if game_id:
            self.game_info_label.config(text=f"MD5: {self.last_md5[:16]}... (RA ID: {game_id})", foreground=FG_LIGHT)

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.ed_combo["values"] = ports
        self.adapter_combo["values"] = ports
        if ports and not self.ed_port_var.get() in ports:
            self.ed_port_var.set(ports[0] if ports else "")
        if ports and not self.adapter_port_var.get() in ports:
            idx = 1 if len(ports) > 1 else 0
            self.adapter_port_var.set(ports[idx] if ports else "")

    def _apply_startup_defaults(self):
        """Allow .bat/.exe launcher to preselect COM port and auto-connect."""
        # Optional fixed port (e.g. COM9)
        try:
            port = os.environ.get("RA_BRIDGE_EVERDRIVE_PORT", "").strip()
            if port:
                self.ed_port_var.set(port)
        except Exception:
            pass
        # Optional: enable auto-connect checkbox
        try:
            ac = os.environ.get("RA_BRIDGE_AUTO_CONNECT", "").strip().lower()
            if getattr(self, "auto_connect_var", None) and ac in ("1", "true", "yes", "on"):
                self.auto_connect_var.set(True)
        except Exception:
            pass
        # Optional: connect immediately (silent)
        try:
            connect_now = os.environ.get("RA_BRIDGE_CONNECT_ON_STARTUP", "").strip().lower()
            if connect_now in ("1", "true", "yes", "on"):
                self.root.after(250, lambda: self._connect_to_everdrive(silent=True))
        except Exception:
            pass
        # Optional: enable auto-send (CRC polling) - EXPERIMENTAL
        try:
            asend = os.environ.get("RA_BRIDGE_AUTO_SEND", "").strip().lower()
            if getattr(self, "auto_send_var", None) and asend in ("1", "true", "yes", "on"):
                self.auto_send_var.set(True)
        except Exception:
            pass

        # Optional: enable Watch mode (CRC polling) on startup
        # NOTE: CRC polling is expensive; use sparingly.
        try:
            watch = os.environ.get("RA_BRIDGE_WATCH", "").strip().lower()
            if getattr(self, "watch_var", None) and watch in ("1", "true", "yes", "on"):
                self.watch_var.set(True)
                # Start watch after the UI has settled
                self.root.after(350, self.toggle_watch)
        except Exception:
            pass

    def _schedule_auto_connect_tick(self):
        if self._auto_connect_after_id:
            try:
                self.root.after_cancel(self._auto_connect_after_id)
            except Exception:
                pass
        self._auto_connect_after_id = self.root.after(1000, self._auto_connect_tick)

    def _auto_connect_tick(self):
        # Keep scheduling regardless; loop is lightweight when disabled.
        self._schedule_auto_connect_tick()

        if getattr(self, "_closing_in_progress", False):
            return
        if not getattr(self, "auto_connect_var", None) or not self.auto_connect_var.get():
            return
        # Already connected
        with getattr(self, "_ed_lock", threading.Lock()):
            if getattr(self, "_ed_connection", None):
                return
        if self._auto_connect_in_progress:
            return
        # Scan ports and try quick handshake on candidates
        now = time.monotonic()
        ports = list(serial.tools.list_ports.comports())
        # Prefer ports that look like USB serial (have VID/PID), but still try all.
        ports.sort(key=lambda p: (0 if (p.vid is not None and p.pid is not None) else 1, p.device))
        for p in ports:
            dev = p.device
            until = self._auto_connect_fail_until.get(dev, 0.0)
            if now < until:
                continue
            # Don't fight user selection if they chose a specific port
            if self.ed_port_var.get().strip() and self.ed_port_var.get().strip() != dev and len(ports) == 1:
                pass
            self._auto_connect_in_progress = True
            threading.Thread(target=self._try_auto_connect_port, args=(dev,), daemon=True).start()
            return

    def _try_auto_connect_port(self, dev: str):
        """Background: probe port quickly. If it behaves like Everdrive, connect silently."""
        ok = False
        ed = None
        try:
            ed = Edio(dev)
            ed.exit_service_mode()
            # If we can read CRCs, it's very likely the Everdrive.
            try:
                ed.get_rom_crcs()
            except Exception:
                pass
            ok = True
        except Exception:
            ok = False
        finally:
            try:
                if ed:
                    ed.close()
            except Exception:
                pass

        def finish():
            self._auto_connect_in_progress = False
            if ok:
                # Set port and connect silently (no popups)
                self.ed_port_var.set(dev)
                self.log(f"[AutoConnect] Detected Everdrive on {dev} -> connecting")
                self._connect_to_everdrive(silent=True)
            else:
                # Backoff so we don't spam-open random serial devices
                self._auto_connect_fail_until[dev] = time.monotonic() + 10.0

        try:
            self.root.after(0, finish)
        except Exception:
            # App closing
            self._auto_connect_in_progress = False
    
    def log(self, msg):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)

    def _firmware_log(self, msg):
        self.firmware_log_text.insert(tk.END, msg + "\n")
        self.firmware_log_text.see(tk.END)

    def _browse_firmware(self):
        path = filedialog.askopenfilename(
            title="Select ESP32 firmware .bin",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")]
        )
        if path:
            self.firmware_path = path
            name = os.path.basename(path)
            self.firmware_path_var.set(name if len(name) < 45 else name[:42] + "...")
            self._firmware_log(f"Selected firmware: {path}")

    def _set_default_games_txt_path(self):
        """Pre-fill games.txt with default path if it exists."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        default = os.path.join(script_dir, "nes-esp-firmware", "data", "games.txt")
        if os.path.isfile(default):
            self.games_txt_path = default
            self.games_txt_path_var.set("games.txt (default)")

    def _browse_games_txt(self):
        path = filedialog.askopenfilename(
            title="Select games.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if path:
            self.games_txt_path = path
            name = os.path.basename(path)
            self.games_txt_path_var.set(name if len(name) < 45 else name[:42] + "...")
            self._firmware_log(f"Selected games.txt: {path}")

    def _upload_firmware_to_adapter(self):
        """Push firmware .bin directly to running ESP32 via POST /ota."""
        if not self.firmware_path or not os.path.isfile(self.firmware_path):
            messagebox.showwarning("No file", "Select firmware (.bin) first.")
            return
        try:
            boundary = b"----NESRAAdapterOTA"
            with open(self.firmware_path, "rb") as f:
                filedata = f.read()
            body = b"--" + boundary + b"\r\n"
            body += b'Content-Disposition: form-data; name="file"; filename="firmware.bin"\r\n'
            body += b"Content-Type: application/octet-stream\r\n\r\n"
            body += filedata
            body += b"\r\n--" + boundary + b"--\r\n"
            req = urllib.request.Request("http://nes-ra-adapter.local/ota", data=body, method="POST")
            req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary.decode())
            self._firmware_log("Uploading firmware to adapter...")
            with urllib.request.urlopen(req, timeout=120) as resp:
                pass
            self._firmware_log("Firmware uploaded. Adapter rebooting.")
            messagebox.showinfo("Done", "Firmware uploaded. Adapter will reboot with new firmware.")
        except Exception as e:
            self._firmware_log(f"Upload failed: {e}")
            messagebox.showerror("Upload failed", str(e) + "\n\nEnsure adapter is on, connected to WiFi, and firmware supports /ota.")

    def _upload_games_txt(self):
        if not self.games_txt_path or not os.path.isfile(self.games_txt_path):
            messagebox.showwarning("No file", "Select games.txt first.")
            return
        try:
            boundary = b"----NESRAAdapterUpload"
            with open(self.games_txt_path, "rb") as f:
                filedata = f.read()
            body = b"--" + boundary + b"\r\n"
            body += b'Content-Disposition: form-data; name="file"; filename="games.txt"\r\n'
            body += b"Content-Type: text/plain\r\n\r\n"
            body += filedata
            body += b"\r\n--" + boundary + b"--\r\n"
            req = urllib.request.Request("http://nes-ra-adapter.local/upload", data=body, method="POST")
            req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary.decode())
            with urllib.request.urlopen(req, timeout=60) as resp:
                pass
            self._firmware_log("games.txt uploaded successfully. Adapter rebooting.")
            messagebox.showinfo("Done", "games.txt uploaded. Adapter will reboot to apply.")
        except Exception as e:
            self._firmware_log(f"Upload failed: {e}")
            messagebox.showerror("Upload failed", str(e) + "\n\nEnsure adapter is on, connected to WiFi, and firmware supports /upload.")

    def _toggle_firmware_server(self):
        if self.firmware_server:
            self._stop_firmware_server()
            return
        if not self.firmware_path or not os.path.isfile(self.firmware_path):
            messagebox.showwarning("No firmware", "Select ESP32 firmware (.bin) first.")
            return
        set_firmware_path(self.firmware_path)
        set_update_ready(True)
        set_gui_request_callback(self._on_esp_requesting_firmware)
        set_gui_update_done_callback(self._on_update_done)
        try:
            self.firmware_server = FirmwareHTTPServer(("0.0.0.0", 8081), FirmwareHTTPHandler)
            self.firmware_server_thread = threading.Thread(target=self.firmware_server.serve_forever, daemon=True)
            self.firmware_server_thread.start()
            ip = get_local_ip()
            if start_updater_mdns(ip, 8081):
                status = f"http://everdrive-bridge.local:8081 (or {ip}:8081)"
            else:
                status = f"http://{ip}:8081"
            self.firmware_status_label.config(text="Update ready - power on ESP", foreground="green")
            self.send_to_adapter_fw_btn.config(text="Stop server")
            self.ready_next_fw_btn.config(state=tk.NORMAL)
            self._firmware_log("Update ready. Power on or RESET adapter - ESP will show 'Ready for update', then OK to send.")
            messagebox.showinfo("Update ready", "Update ready.\n\nPower on or RESET the adapter. ESP will check on boot, show 'Ready for update', then you'll see OK/Cancel to send.")
        except OSError as e:
            messagebox.showerror("Error", f"Could not start server: {e}")
            self._firmware_log(f"Server failed: {e}")

    def _on_esp_requesting_firmware(self, set_decision):
        """Called from server thread when ESP requests firmware. Show dialog on main thread."""
        def do_dialog():
            ok = messagebox.askyesno("Firmware update", "ESP32 shows 'Ready for update' and is requesting.\n\nSend update?", icon="question")
            set_decision(ok)
        self.root.after(0, do_dialog)

    def _on_update_done(self, sent):
        """Called when update was sent or cancelled. Log and clear queue done in firmware_update."""
        def do_log():
            msg = "Update sent. Adapter will reboot." if sent else "Update cancelled. Adapter continues normal boot. Queue cleared."
            self._firmware_log(msg)
        self.root.after(0, do_log)

    def _ready_for_next_update(self):
        """Re-queue firmware for next adapter (server already running)."""
        if not self.firmware_path or not os.path.isfile(self.firmware_path):
            messagebox.showwarning("No file", "Select firmware (.bin) first.")
            return
        set_firmware_path(self.firmware_path)
        set_update_ready(True)
        self._firmware_log("Re-queued firmware for next adapter.")

    def _stop_firmware_server(self):
        stop_updater_mdns()
        set_update_ready(False)
        set_gui_request_callback(None)
        set_gui_update_done_callback(None)
        clear_firmware_path()
        if self.firmware_server:
            try:
                self.firmware_server.shutdown()
                self.firmware_server.server_close()
            except Exception:
                pass
            self.firmware_server = None
        self.firmware_server_thread = None
        self.firmware_status_label.config(text="", foreground="")
        self.send_to_adapter_fw_btn.config(text="Ready for boot update (adapter fetches on power-on)")
        self.ready_next_fw_btn.config(state=tk.DISABLED)
        self._firmware_log("Server stopped.")
    
    def _toggle_adapter_connect(self):
        """Connect to adapter (open port once, keep open - avoids ESP32 reset on open/close)."""
        if self.adapter_ser and self.adapter_ser.is_open:
            self._disconnect_adapter()
        else:
            port = self.adapter_port_var.get().strip()
            if not port:
                messagebox.showwarning("No port", "Select Adapter port first.")
                return
            try:
                self.adapter_ser = serial.Serial(port=port, baudrate=9600, timeout=0.1)
                self.adapter_ser.dtr = False
                self.adapter_ser.rts = False
                time.sleep(0.1)
                self.connect_btn.config(text="Disconnect")
                self.log("[Adapter] Connected (port stays open to avoid reset)")
                if self.monitor_after_send_var.get():
                    self.monitor_ser = self.adapter_ser
                    self._start_monitor()
            except Exception as e:
                self.log(f"Connect error: {e}")
                messagebox.showerror("Error", str(e))
    
    def _on_closing(self):
        if self._closing_in_progress:
            return
        self._closing_in_progress = True
        self.watch_stop = True
        self.cheat_check_stop = True
        if self._port_check_after_id is not None:
            self.root.after_cancel(self._port_check_after_id)
            self._port_check_after_id = None
        if self._server_values_after_id is not None:
            self.root.after_cancel(self._server_values_after_id)
            self._server_values_after_id = None
        with self._ed_lock:
            self._close_everdrive_connection()
        self._disconnect_adapter()
        # Close immediately. No lockout-on-quit behavior.
        self._stop_http_server()
        self._stop_firmware_server()
        self.root.destroy()
    
    def _disconnect_adapter(self):
        """Disconnect and close adapter port."""
        self.monitor_stop = True
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1.0)
            self.monitor_thread = None
        self.monitor_ser = None
        if self.adapter_ser:
            try:
                self.adapter_ser.close()
            except Exception:
                pass
            self.adapter_ser = None
        self.connect_btn.config(text="Connect to Adapter")
        self.log("[Adapter] Disconnected")
    
    def _start_monitor(self):
        """Start streaming ESP32 output (uses adapter_ser, does not close)."""
        if self.adapter_ser and self.adapter_ser.is_open:
            self.monitor_ser = self.adapter_ser
            self.monitor_stop = False
            self.esp32_text.delete(1.0, tk.END)
            self.esp32_text.insert(tk.END, "[Live] ESP32 output - updates as data arrives\n")
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
            self.log("[Monitor] ESP32 output started")
    
    def _stop_monitor(self):
        """Stop monitor read thread only - does NOT close port."""
        self.monitor_stop = True
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1.0)
            self.monitor_thread = None
        self.monitor_ser = None
        self.log("[Monitor] Stopped")
    
    def _esp32_append(self, line):
        self.esp32_text.insert(tk.END, line + "\n")
        self.esp32_text.see(tk.END)
        # Parse A=id;title;url (achievement display from ESP32)
        if line.startswith("A=") and ";" in line:
            parts = line[2:].split(";", 2)
            if len(parts) >= 3:
                ach_id, title, url = parts[0], parts[1], parts[2]
                if title and url.startswith("http"):
                    try:
                        self.root.after(0, lambda aid=ach_id, t=title, u=url: self._on_achievement_received(aid, t, u))
                    except Exception:
                        pass
        # Achievement-related lines -> also log to main Log so user can't miss them
        ach_markers = ("REQ=", "REQ:", ">>> REQ RECEIVED", "A=", "RESP=", "HTTP_ERR", "ERROR ON RESPONSE",
                      "ACHIEVEMENT_ADDED", "FIFO_FULL")
        if any(m in line for m in ach_markers):
            try:
                self.log(f"[Achievement] {line[:120]}")
            except Exception:
                pass
        # Update status to show we're receiving
        try:
            self.esp32_status_var.set("ESP32 output — receiving")
        except Exception:
            pass

    def _on_achievement_received(self, ach_id, title, url):
        """Update achievement display, play sound, fetch images, show popup. Called on main thread."""
        self.ach_title_label.config(text=title)
        if self.ach_sound_enable_var.get() and self.ach_sound_path_var.get() != "(none)":
            self._play_achievement_sound()
        # Fetch images and update display; show popup when ready
        show_popup = self.ach_popup_var.get()
        threading.Thread(
            target=self._fetch_achievement_and_game_images,
            args=(url, title, ach_id, show_popup),
            daemon=True,
        ).start()

    def _fetch_achievement_and_game_images(self, badge_url, title, ach_id, show_popup):
        """Fetch achievement badge + game image in background, then update UI and show popup."""
        ach_photo = None
        game_photo = None
        if HAS_PIL and badge_url:
            try:
                with urllib.request.urlopen(badge_url, timeout=10) as resp:
                    data = resp.read()
                img = Image.open(io.BytesIO(data)).resize((64, 64), Image.Resize.LANCZOS)
                ach_photo = ImageTk.PhotoImage(img)
            except Exception:
                pass
        md5 = getattr(self, "last_md5", None)
        if md5 and ra_game_id and HAS_PIL:
            user = (self.ach_ra_user_var.get() or "").strip()
            key = (self.ach_ra_key_var.get() or "").strip()
            if user and key:
                try:
                    gid, _ = ra_game_id(md5, username=user, api_key=key)
                    if gid and ra_game_image_url:
                        img_url = ra_game_image_url(gid)
                        if img_url:
                            with urllib.request.urlopen(img_url, timeout=10) as resp:
                                data = resp.read()
                            img = Image.open(io.BytesIO(data)).resize((64, 64), Image.Resize.LANCZOS)
                            game_photo = ImageTk.PhotoImage(img)
                except Exception:
                    pass

        def update_ui():
            if ach_photo:
                self.ach_photo = ach_photo
                self.ach_image_label.config(image=ach_photo, text="")
            if game_photo:
                self.ach_game_photo = game_photo
                self.ach_game_image_label.config(image=game_photo, text="")
            if show_popup:
                if getattr(self, "ach_video_alert_var", None) and self.ach_video_alert_var.get():
                    try:
                        from achievement_alert_window import show_achievement_alert_async
                        show_achievement_alert_async({"title": title, "url": badge_url}, duration_ms=6000)
                    except Exception:
                        self._show_achievement_popup(title, ach_photo, game_photo)
                else:
                    self._show_achievement_popup(title, ach_photo, game_photo)

        self.root.after(0, update_ui)

    def _show_achievement_popup(self, title, ach_photo=None, game_photo=None):
        """Show popup window with game + achievement images (like RA Layout Manager alert)."""
        pop = tk.Toplevel(self.root)
        pop.title("Achievement Unlocked!")
        pop.configure(bg=BG_CARD)
        pop.attributes("-topmost", True)
        pop.resizable(False, False)
        w, h = 380, 140
        try:
            x = self.root.winfo_screenwidth() - w - 20
        except Exception:
            x = 100
        pop.geometry(f"{w}x{h}+{max(0, x)}+20")
        outer = tk.Frame(pop, bg=ACCENT, padx=2, pady=2)
        outer.pack(fill=tk.BOTH, expand=True)
        inner = tk.Frame(outer, bg=BG_CARD, padx=12, pady=12)
        inner.pack(fill=tk.BOTH, expand=True)
        row = tk.Frame(inner, bg=BG_CARD)
        row.pack(fill=tk.X)
        if game_photo:
            tk.Label(row, image=game_photo, bg=BG_CARD).pack(side=tk.LEFT, padx=(0, 12))
        if ach_photo:
            tk.Label(row, image=ach_photo, bg=BG_CARD).pack(side=tk.LEFT, padx=(0, 12))
        tk.Label(row, text=title, font=("Arial", 14, "bold"), fg=FG_LIGHT, bg=BG_CARD, wraplength=220).pack(side=tk.LEFT, fill=tk.X, expand=True)
        pop.after(6000, pop.destroy)
        pop._game_photo = game_photo
        pop._ach_photo = ach_photo

    def _play_achievement_sound(self):
        """Play achievement sound with volume. Runs on main thread."""
        path = self.ach_sound_path_var.get()
        if not path or path == "(none)":
            return
        if not os.path.isfile(path):
            return
        vol = max(0, min(1, self.ach_volume_var.get()))
        def do_play():
            err = None
            try:
                if HAS_PYGAME and pygame:
                    if not pygame.mixer.get_init():
                        pygame.mixer.init(frequency=22050, size=-16, channels=2, buffer=512)
                    ext = os.path.splitext(path)[1].lower()
                    if ext == ".mp3":
                        pygame.mixer.music.load(path)
                        pygame.mixer.music.set_volume(vol)
                        pygame.mixer.music.play()
                    else:
                        snd = pygame.mixer.Sound(path)
                        snd.set_volume(vol)
                        snd.play()
                elif HAS_WINSOUND and path.lower().endswith(".wav"):
                    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                else:
                    err = "WAV files need no extra install. MP3 needs: pip install pygame"
            except Exception as e:
                err = str(e)
            if err:
                self.root.after(0, lambda: messagebox.showerror("Sound error", err))
        threading.Thread(target=do_play, daemon=True).start()

    def _browse_achievement_sound(self):
        path = filedialog.askopenfilename(
            title="Select achievement sound",
            filetypes=[("WAV files", "*.wav"), ("MP3 files", "*.mp3"), ("All files", "*.*")]
        )
        if path:
            self.ach_sound_path_var.set(path)


    def _test_achievement_sound(self):
        path = self.ach_sound_path_var.get()
        if not path or path == "(none)":
            messagebox.showinfo("Test sound", "Select a sound file first (click Browse...).\n\nWAV works without extra install. MP3 needs: pip install pygame")
            return
        if not os.path.isfile(path):
            messagebox.showwarning("Test sound", f"File not found:\n{path}")
            return
        if path.lower().endswith(".mp3") and not HAS_PYGAME:
            messagebox.showinfo("Test sound", "MP3 requires pygame. Use a WAV file, or run: pip install pygame")
            return
        self._play_achievement_sound()
    
    def _monitor_loop(self):
        buf = ""
        while not self.monitor_stop and self.monitor_ser:
            try:
                if self.monitor_ser.in_waiting:
                    buf += self.monitor_ser.read(self.monitor_ser.in_waiting).decode("ascii", "replace")
                # Flush complete lines to ESP32 panel immediately
                while "\n" in buf or "\r" in buf:
                    i = buf.find("\n")
                    r = buf.find("\r")
                    if r >= 0 and (i < 0 or r < i):
                        i = r
                    if i >= 0:
                        line = buf[:i].replace("\r", "").replace("\n", "").strip()
                        buf = buf[i + 1:]
                        if line:
                            self.root.after(0, lambda l=line: self._esp32_append(l))
                    else:
                        break
            except Exception:
                break
            time.sleep(0.005)
    
    def _clear_last_game(self):
        """Clear last game and buffer when starting a new scan."""
        if self.http_server:
            self.http_server.clear_current_game()
        self.last_crcs = None
        self.last_md5 = None
        self._update_game_display()

    def read_game(self):
        everdrive_mode = getattr(self, "everdrive_mode_var", None) and self.everdrive_mode_var.get()
        with self._ed_lock:
            ed = self._ed_connection
        if everdrive_mode and not ed:
            messagebox.showwarning("Connect first", "Connect to Everdrive first (prevents cheats in menu).")
            return
        port = self.ed_port_var.get().strip()
        if not port:
            messagebox.showerror("Error", "Select Everdrive port.")
            return
        self._clear_last_game()
        for attempt in range(3):
            try:
                if attempt > 0:
                    self.log(f"Retry {attempt + 1}/3 in 5s...")
                    time.sleep(5)
                self.log("Reading game from Everdrive...")
                with self._ed_lock:
                    ed = self._ed_connection
                if not ed:
                    messagebox.showwarning("Disconnected", "Everdrive disconnected. Connect again.")
                    return
                ed.exit_service_mode()
                crc_begin, crc_end = ed.get_rom_crcs()
                try:
                    cheats_on, savestates_on = ed.get_cheat_savestate_status()
                except Exception:
                    cheats_on, savestates_on = None, None
                self.last_crcs = (crc_begin, crc_end)
                self.last_cheats_on = cheats_on
                self.last_savestates_on = savestates_on
                if cheats_on is not None:
                    self._update_cheat_status_display(cheats_on, savestates_on)
                self._everdrive_check_fail_count = 0
                if self.http_server:
                    cheats_ok = not (cheats_on or savestates_on) if cheats_on is not None else True
                    self.http_server.set_status(cheats_ok, True)  # Start monitoring now that we've connected
                self.log(f"CRC: {crc_begin},{crc_end}")
                md5, source = lookup_md5_with_fallback(crc_begin, crc_end, self.ed_table, self.games_table)
                if md5:
                    self.last_md5 = md5
                    src = "(everdrive)" if source == "everdrive" else "(games.txt)"
                    self.log(f"Game MD5: {md5} {src}")
                elif crc_begin == crc_end:
                    self.last_md5 = None
                    self.log("Everdrive menu (no game loaded) - boot a game to title screen, then Read again")
                else:
                    self.last_md5 = None
                    self.log("Game not found - add to everdrive_games.txt with add_everdrive_crc.py")
                self._update_game_display()
                return  # Success
            except Exception as e:
                self.log(f"Error: {e}")
                self.last_md5 = None
                self._update_game_display()
                if attempt == 2:
                    messagebox.showerror("Error", f"{e}\n\nIf Everdrive just powered on, wait 10s for USB to stabilize, then try again.")
    
    def send_to_adapter_click(self):
        with self._ed_lock:
            ed = self._ed_connection
        if not ed:
            messagebox.showwarning("Connect first", "Connect to Everdrive first (prevents cheats in menu).")
            return
        if not self.last_crcs or self.last_crcs[0] == self.last_crcs[1]:
            messagebox.showwarning("No Game", "Read game from Everdrive first (boot to title screen).")
            return
        # Re-read cheat status (user may have enabled after Read)
        try:
            ed.exit_service_mode()
            cheats_on, savestates_on = ed.get_cheat_savestate_status()
            self.last_cheats_on = cheats_on
            self.last_savestates_on = savestates_on
            self._update_cheat_status_display(cheats_on, savestates_on)
            if cheats_on or savestates_on:
                messagebox.showwarning("Cheats/Savestates ON",
                    "RA requires cheats and savestates OFF. Click 'Force cheats/savestates off' or disable in Everdrive menu.")
                return
        except Exception as e:
            self.log(f"Could not check Everdrive status: {e}")
            messagebox.showwarning("Check failed", "Could not read Everdrive. Keep USB connected.")
            return
        crc_begin, crc_end = self.last_crcs[0], self.last_crcs[1]
        # Look up MD5 from CRC
        md5, _ = lookup_md5_with_fallback(crc_begin, crc_end, self.ed_table, self.games_table)
        if not md5:
            messagebox.showwarning("MD5 not found", f"Could not find MD5 for CRC {crc_begin},{crc_end}. Game not in database.")
            return
        if self.use_wifi_var.get():
            self._store_game_wifi(crc_begin, crc_end)
            self.log(f"[WiFi] Stored SELECT_GAME_CRC={crc_begin},{crc_end} - ESP32 will poll for it")
            messagebox.showinfo("Done", "Game ready for ESP32. It will poll every few seconds. Press RESET on NES.")
        else:
            if not self.adapter_ser or not self.adapter_ser.is_open:
                messagebox.showwarning("Not connected", "Click 'Connect to Adapter (Serial)' first.")
                return
            try:
                self.adapter_ser.write(f"SELECT_GAME={md5}\r\n".encode())
                self.adapter_ser.flush()
                self.log(f"Sent SELECT_GAME={md5} to adapter (CRC: {crc_begin},{crc_end})")
                if self.monitor_after_send_var.get() and not self.monitor_thread:
                    self._start_monitor()
                messagebox.showinfo("Done", "Sent to adapter. Press RESET on NES.")
            except Exception as e:
                self.log(f"Error: {e}")
                messagebox.showerror("Error", str(e))
    
    def watch_loop(self):
        while not self.watch_stop:
            try:
                with self._ed_lock:
                    ed = self._ed_connection
                if not ed:
                    # Require Connect first - don't create connection here
                    time.sleep(0.1)
                    continue
                port = self.ed_port_var.get().strip()
                if port:
                    ed.exit_service_mode()
                    crc_begin, crc_end = ed.get_rom_crcs()
                    cheats_on, savestates_on = ed.get_cheat_savestate_status()
                    # Publish best-effort active CRC to server for ESP-side game-change detection.
                    try:
                        if self.http_server and crc_begin and crc_end:
                            self.http_server.set_active_crc(crc_begin, crc_end, (crc_begin == crc_end))
                    except Exception:
                        pass
                    # In menu: clear last game, auto-disable cheats/savestates if on
                    if crc_begin == crc_end:
                        self.root.after(0, self._clear_last_game)
                        if cheats_on or savestates_on:
                            ed.set_cheats_savestates_off()
                            time.sleep(0.15)
                            cheats_on, savestates_on = ed.get_cheat_savestate_status()
                            self.root.after(0, lambda c=cheats_on, s=savestates_on: self._update_cheat_status_display(c, s))
                            self.root.after(0, lambda: self.log("Auto-disabled cheats/savestates (in menu)"))
                    # Don't close - keep connection for cheat check
                    if crc_begin != crc_end:
                        game_changed = self.last_crcs != (crc_begin, crc_end)
                        md5, _ = lookup_md5_with_fallback(crc_begin, crc_end, self.ed_table, self.games_table)
                        if md5:
                            self.last_md5 = md5
                        self.last_crcs = (crc_begin, crc_end)
                        self.root.after(0, lambda c=cheats_on, s=savestates_on: self._update_cheat_status_display(c, s))
                        msg = f"Detected: {crc_begin},{crc_end}"
                        if md5:
                            msg += f" -> {md5}"
                        self.root.after(0, lambda m=msg: self.log(m))
                        if game_changed and not (cheats_on or savestates_on):
                            if self.use_wifi_var.get():
                                self._store_game_wifi(crc_begin, crc_end)
                                self.root.after(0, lambda: self.log("Auto-sent to adapter (WiFi)"))
                            elif self.adapter_ser and self.adapter_ser.is_open:
                                try:
                                    if md5:
                                        self.adapter_ser.write(f"SELECT_GAME={md5}\r\n".encode())
                                        self.adapter_ser.flush()
                                        self.root.after(0, lambda: self.log(f"Auto-sent MD5 to adapter: {md5}"))
                                    else:
                                        self.root.after(0, lambda: self.log("No MD5 found for game, cannot send to adapter"))
                                except Exception as e:
                                    self.root.after(0, lambda err=str(e): self.log(f"Error sending to adapter: {err}"))
                                    pass
                        elif game_changed and (cheats_on or savestates_on):
                            self.root.after(0, lambda: self.log("Auto-send skipped: cheats/savestates ON"))
            except Exception:
                pass
            for _ in range(100):
                if self.watch_stop:
                    break
                time.sleep(0.1)
    
    def toggle_watch(self):
        if self.watch_var.get():
            self.watch_stop = False
            self.watch_thread = threading.Thread(target=self.watch_loop, daemon=True)
            self.watch_thread.start()
            self.log("Watch started (polling every 10s)")
        else:
            self.watch_stop = True
            self.log("Watch stopped")


def main():
    root = tk.Tk()
    EverdriveBridgeGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
