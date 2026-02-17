#!/usr/bin/env python3
"""
Simple HTTP server for RA Bridge - WiFi delivery mode.
ESP32 polls GET /game to receive SELECT_GAME_CRC data. No serial = no reset.
"""

import json
import os
import socket
import socketserver
import threading
import time

_zeroconf = None
_zeroconf_info = None


def start_mdns(ip, port=8080):
    """Advertise everdrive-bridge.local via mDNS so ESP32 can resolve without config."""
    global _zeroconf, _zeroconf_info
    try:
        from zeroconf import ServiceInfo, Zeroconf
        _zeroconf = Zeroconf()
        _zeroconf_info = ServiceInfo(
            "_http._tcp.local.",
            "everdrive-bridge._http._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=port,
            server="everdrive-bridge.local.",
        )
        _zeroconf.register_service(_zeroconf_info)
        return True
    except ImportError:
        return False
    except Exception:
        return False


def stop_mdns():
    global _zeroconf, _zeroconf_info
    if _zeroconf and _zeroconf_info:
        try:
            _zeroconf.unregister_service(_zeroconf_info)
            _zeroconf.close()
        except Exception:
            pass
        _zeroconf = None
        _zeroconf_info = None


class BridgeHTTPHandler(socketserver.BaseRequestHandler):
    """Serves GET /game with buffered CRC data. Clears buffer after first successful fetch."""

    def handle(self):
        try:
            # Prevent one bad/half-open client from blocking the whole server.
            self.request.settimeout(1.0)
            data = self.request.recv(4096).decode("ascii", "replace")
            if not data.strip():
                return
            lines = data.split("\r\n")
            first = lines[0] if lines else ""
            if not first.startswith("GET "):
                self._send(400, "Bad request")
                return
            raw_path = first.split(" ", 2)[1]
            path = raw_path.split("?")[0]
            method = first.split(" ", 2)[0] if first else ""
            client_addr = self.client_address[0] if self.client_address else "?"
            if method == "OPTIONS":
                self._send(200, "", allow_cors=True)
                return
            if path == "/" or path == "/game":
                buf = self.server.get_current_game()
                if buf:
                    self.server.log_request("GET /game", client_addr, 200, buf)
                    self._send(200, buf, "text/plain; charset=ascii")
                else:
                    self.server.log_request("GET /game", client_addr, 404, "No game")
                    self._send(404, "No game")
            elif path == "/status":
                body = self.server.get_status_json()
                self.server.log_request("GET /status", client_addr, 200, body[:80] + "..." if len(body) > 80 else body)
                self._send(200, body, "application/json; charset=utf-8", allow_cors=True)
            elif path == "/lockout_ack":
                # ESP informs us it entered lockout; clear sticky lockout on bridge side
                try:
                    self.server.clear_lockout()
                except Exception:
                    pass
                self.server.log_request("GET /lockout_ack", client_addr, 200, "cleared")
                self._send(200, "ok", "text/plain; charset=ascii", allow_cors=True)
            elif path == "/game_ack":
                # ESP informs us it accepted the current game; clear it so we don't keep serving it forever.
                try:
                    self.server.clear_current_game()
                except Exception:
                    pass
                self.server.log_request("GET /game_ack", client_addr, 200, "cleared_game")
                self._send(200, "ok", "text/plain; charset=ascii", allow_cors=True)
            elif path == "/log":
                body = self.server.get_request_log_json()
                self._send(200, body, "application/json; charset=utf-8", allow_cors=True)
            else:
                self._send(404, "Not found")
        except Exception:
            pass

    def _send(self, code, body, content_type="text/plain", allow_cors=False):
        if isinstance(body, str):
            body = body.encode("ascii")
        status = "200 OK" if code == 200 else "404 Not Found" if code == 404 else "400 Bad Request"
        head = f"HTTP/1.0 {status}\r\n"
        head += f"Content-Type: {content_type}\r\n"
        head += f"Content-Length: {len(body)}\r\n"
        if allow_cors:
            head += "Access-Control-Allow-Origin: *\r\n"
            head += "Access-Control-Allow-Methods: GET, OPTIONS\r\n"
        head += "Connection: close\r\n\r\n"
        try:
            self.request.sendall(head.encode("ascii") + body)
        except Exception:
            pass
        self.request.close()


class BridgeHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    HEARTBEAT_INTERVAL = 5  # seconds - app alive indicator for ESP
    LOCKOUT_AFTER_UNPLUG_SECONDS = 15  # Everdrive unplug grace before lockout (bridge-side)

    def __init__(self, host="0.0.0.0", port=8080, persist_path=None):
        # Server is source of truth for current game CRC. ESP polls /game. Cleared only when user scans new game.
        self._persist_path = persist_path
        self._current_game_crc = None
        self._game_lock = threading.Lock()
        self._game_sent = False  # True after store_game - only then does everdrive_connected matter
        self._status = {"cheats_ok": True, "everdrive_connected": True, "last_check": 0}
        self._status_lock = threading.Lock()
        # Best-effort: what the bridge currently sees running on the Everdrive.
        # Populated by GUI "Watch" (CRC polling). Optional; may be None.
        self._active_crc_pair = None  # "XXXXXXXX,YYYYYYYY"
        self._active_crc_in_menu = None  # bool or None
        self._active_crc_at = 0  # int(time.time())
        self._active_crc_lock = threading.Lock()
        # Bridge-driven lockout (ESP enforces bus-off). This lets us communicate reasons for diagnostics.
        self._lockout = {"lockout": False, "lockout_reason": "", "lockout_since": 0}
        self._lockout_lock = threading.Lock()
        # Unplug timer state (only meaningful after a game has been sent at least once)
        self._everdrive_offline_since = None  # time.time() when everdrive_connected became False
        self._everdrive_seen_online_since_game = False  # must be True before unplug timer can trigger
        self._heartbeat = int(time.time())  # Unix timestamp, updated every HEARTBEAT_INTERVAL
        self._heartbeat_stop = False
        self._heartbeat_thread = None
        self._request_log = []
        self._request_log_lock = threading.Lock()
        self._request_log_max = 500
        self._last_logged_status = None
        self._last_request_time = 0.0  # time.time() of last RECV
        self._last_request_client = ""
        self._tick_stop = False
        super().__init__((host, port), BridgeHTTPHandler)
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._tick_thread.start()
        # Clear any persisted game on boot - wait for user to Read Game + Send (avoids auto-load on Everdrive)
        self._clear_persisted_on_startup()
        self.log_app_sent("server_started", "everdrive_connected=True (default until Connect)")

    def set_lockout(self, reason: str):
        """Set lockout flag and reason. ESP should lock out immediately when it polls /status."""
        now = int(time.time())
        with self._lockout_lock:
            self._lockout = {"lockout": True, "lockout_reason": str(reason or ""), "lockout_since": now}
        self.log_app_sent("set_lockout", f"reason={reason}")

    def clear_lockout(self):
        """Clear lockout (typically when user is starting a new game)."""
        with self._lockout_lock:
            self._lockout = {"lockout": False, "lockout_reason": "", "lockout_since": 0}
        self.log_app_sent("clear_lockout")

    def log_request(self, path, client, code, body_preview):
        """Log HTTP request for Server tab display."""
        self._last_request_time = time.time()
        self._last_request_client = client
        with self._request_log_lock:
            ts = time.strftime('%H:%M:%S') + f".{int(time.time() * 1000) % 1000:03d}"
            entry = f"[{ts}] RECV {path} from {client} -> {code}"
            if body_preview:
                entry += f" | {body_preview}"
            self._request_log.append(entry)
            if len(self._request_log) > self._request_log_max:
                self._request_log.pop(0)

    def log_app_sent(self, what, detail=""):
        """Log what the app sent to server (store_game, set_status, etc). Always logs (no de-dup)."""
        with self._request_log_lock:
            ts = time.strftime('%H:%M:%S') + f".{int(time.time() * 1000) % 1000:03d}"
            entry = f"[{ts}] APP SENT {what}"
            if detail:
                entry += f" | {detail}"
            self._request_log.append(entry)
            if len(self._request_log) > self._request_log_max:
                self._request_log.pop(0)

    def get_request_log(self):
        with self._request_log_lock:
            return list(self._request_log)

    def get_server_status(self):
        """Return (seconds_ago, client) of last request. Use to confirm server is still alive."""
        if self._last_request_time <= 0:
            return None, ""
        return time.time() - self._last_request_time, self._last_request_client

    def _heartbeat_loop(self):
        """Update heartbeat every N seconds - ESP: same value twice = app disconnected."""
        while not self._heartbeat_stop:
            time.sleep(self.HEARTBEAT_INTERVAL)
            if self._heartbeat_stop:
                break
            with self._status_lock:
                self._heartbeat = int(time.time())

    def _tick_loop(self):
        """Log server tick every 2 seconds - proves server is running (shows in activity log)."""
        while not self._tick_stop:
            time.sleep(2)
            if self._tick_stop:
                break
            with self._request_log_lock:
                ts = time.strftime('%H:%M:%S') + f".{int(time.time() * 1000) % 1000:03d}"
                entry = f"[{ts}] SERVER tick | online"
                self._request_log.append(entry)
                if len(self._request_log) > self._request_log_max:
                    self._request_log.pop(0)

    def server_close(self):
        self._heartbeat_stop = True
        self._tick_stop = True
        super().server_close()

    def get_current_game(self):
        """Return current game CRC. Server keeps it until user scans a new game - ESP polls /game as needed."""
        with self._game_lock:
            return self._current_game_crc

    def get_current_game_peek(self):
        """Return current game for display (Server tab). Thread-safe."""
        with self._game_lock:
            return self._current_game_crc

    def get_status_snapshot(self):
        """Return copy of status dict (heartbeat, everdrive_connected, cheats_ok, last_check, game, lockout...). Thread-safe."""
        with self._status_lock:
            s = {
                "heartbeat": self._heartbeat,
                "everdrive_connected": self._status["everdrive_connected"],
                "cheats_ok": self._status["cheats_ok"],
                "last_check": self._status["last_check"],
            }
        with self._active_crc_lock:
            s["active_crc"] = self._active_crc_pair
            s["active_crc_in_menu"] = self._active_crc_in_menu
            s["active_crc_at"] = self._active_crc_at
        with self._lockout_lock:
            s["lockout"] = self._lockout["lockout"]
            s["lockout_reason"] = self._lockout["lockout_reason"]
            s["lockout_since"] = self._lockout["lockout_since"]
        with self._game_lock:
            s["game"] = self._current_game_crc
        return s

    def _clear_persisted_on_startup(self):
        """Delete persisted game on boot so Everdrive waits for user to Read Game + Send."""
        with self._game_lock:
            self._current_game_crc = None
            self._game_sent = False
        if self._persist_path and os.path.isfile(self._persist_path):
            try:
                os.remove(self._persist_path)
                self.log_app_sent("cleared_persisted_on_boot", "(wait for user to Send)")
            except Exception:
                pass

    def _load_persisted_game(self):
        """Load last game from disk at startup - survives bridge restart. NOT called on boot (see _clear_persisted_on_startup)."""
        if not self._persist_path:
            return
        try:
            if os.path.isfile(self._persist_path):
                with open(self._persist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                line = data.get("game")
                if line and line.startswith("SELECT_GAME_CRC="):
                    with self._game_lock:
                        self._current_game_crc = line
                        self._game_sent = True
                    self.log_app_sent("loaded_persisted", line)
        except Exception:
            pass

    def _save_persisted_game(self):
        """Save current game to disk so it survives bridge restart."""
        if not self._persist_path:
            return
        try:
            with self._game_lock:
                line = self._current_game_crc
            if line:
                with open(self._persist_path, "w", encoding="utf-8") as f:
                    json.dump({"game": line}, f)
            elif os.path.isfile(self._persist_path):
                os.remove(self._persist_path)
        except Exception:
            pass

    def store_game(self, crc_begin, crc_end):
        """Store game CRC on server. ESP polls /game to get it. Overwrites previous game."""
        line = f"SELECT_GAME_CRC={crc_begin},{crc_end}"
        with self._game_lock:
            self._current_game_crc = line
            self._game_sent = True  # Game loaded - everdrive_connected now matters for disconnect check
        # New game = clear lockout state and reset unplug timer
        self.clear_lockout()
        self._everdrive_offline_since = None
        self._everdrive_seen_online_since_game = False
        self._save_persisted_game()
        self.log_app_sent("store_game", line)

    def clear_current_game(self):
        """Clear stored game - called when user scans a new game (Read Game from Everdrive)."""
        with self._game_lock:
            self._current_game_crc = None
            self._game_sent = False  # Reset - next store_game will set it again
        self._everdrive_offline_since = None
        self._everdrive_seen_online_since_game = False
        self._save_persisted_game()

    def set_status(self, cheats_ok, everdrive_connected):
        with self._status_lock:
            self._status = {"cheats_ok": cheats_ok, "everdrive_connected": everdrive_connected, "last_check": int(time.time())}
        self.log_app_sent("set_status", f"everdrive_connected={everdrive_connected}, cheats_ok={cheats_ok}")
        # Bridge-side lockout decision (ESP still enforces as backup).
        # Only activate after a game was sent and we saw everdrive_connected==True at least once after that.
        with self._game_lock:
            game_sent = self._game_sent
        if not game_sent:
            return
        # IMPORTANT: Do NOT set lockout purely because cheats_ok==False here.
        # Some call sites may temporarily report cheats_ok False when the Everdrive status check fails.
        # Cheats/savestates lockout must only be triggered by a *confirmed* breach
        # (e.g. GUI detects cheats/savestates still ON after disable attempt) via set_lockout().
        now = time.time()
        if everdrive_connected:
            self._everdrive_seen_online_since_game = True
            self._everdrive_offline_since = None
        else:
            if not self._everdrive_seen_online_since_game:
                return
            if self._everdrive_offline_since is None:
                self._everdrive_offline_since = now
            if (now - self._everdrive_offline_since) >= self.LOCKOUT_AFTER_UNPLUG_SECONDS:
                self.set_lockout("unplug_15s")

    def set_active_crc(self, crc_begin: str, crc_end: str, in_menu: bool = None):
        """Update active CRC pair observed from Everdrive (best-effort, optional)."""
        try:
            if not crc_begin or not crc_end:
                pair = None
            else:
                pair = f"{str(crc_begin).strip().upper()},{str(crc_end).strip().upper()}"
            with self._active_crc_lock:
                self._active_crc_pair = pair
                self._active_crc_in_menu = (bool(in_menu) if in_menu is not None else None)
                self._active_crc_at = int(time.time()) if pair else 0
        except Exception:
            return

    def get_status_json(self):
        """heartbeat, everdrive_connected, cheats_ok, last_check, game, lockout... ESP uses one poll for handshake."""
        import json
        with self._status_lock:
            s = dict(self._status)
            s["heartbeat"] = self._heartbeat
        with self._active_crc_lock:
            s["active_crc"] = self._active_crc_pair
            s["active_crc_in_menu"] = self._active_crc_in_menu
            s["active_crc_at"] = self._active_crc_at
        with self._lockout_lock:
            s["lockout"] = self._lockout["lockout"]
            s["lockout_reason"] = self._lockout["lockout_reason"]
            s["lockout_since"] = self._lockout["lockout_since"]
        with self._game_lock:
            s["game"] = self._current_game_crc
        return json.dumps(s)

    def get_request_log_json(self):
        """Return request log as JSON for web app monitoring."""
        import json
        with self._request_log_lock:
            log = list(self._request_log)
        return json.dumps({"entries": log})


def get_local_ip():
    """Get this machine's local IP (for display to user)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


def try_start_mdns(ip, port=8080):
    """Start mDNS if zeroconf available. Returns True if everdrive-bridge.local is advertised."""
    return start_mdns(ip, port)
