"""
Firmware update server - ESP32 checks for update, user confirms, then app serves.
Advertises nes-ra-updater.local:8081.

Flow:
  1. User selects firmware in bridge, clicks "Ready for boot update" -> queue ready
  2. ESP32: GET /update-status -> {"update_ready": true/false}
  3. If true, ESP32: GET /firmware.bin
  4. App shows dialog: "ESP32 requesting update. Send? [OK] [Cancel]"
  5. If OK: serve file, clear queue. If Cancel: 404, ESP continues boot, clear queue
"""

import os
import socket
import socketserver
import threading

_firmware_path = None
_update_ready = False  # True when user has selected file and clicked Ready
_zeroconf = None
_zeroconf_info = None
_gui_request_callback = None  # Called when ESP requests file: cb(set_decision) where set_decision(True/False)
_gui_update_done_callback = None  # Called when update sent or cancelled: cb(sent: bool)
_pending_event = threading.Event()
_user_decision = None


def start_updater_mdns(ip, port=8081):
    """Advertise nes-ra-updater.local so ESP32 can find the update server."""
    global _zeroconf, _zeroconf_info
    try:
        from zeroconf import ServiceInfo, Zeroconf
        _zeroconf = Zeroconf()
        _zeroconf_info = ServiceInfo(
            "_http._tcp.local.",
            "nes-ra-updater._http._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=port,
            server="nes-ra-updater.local.",
        )
        _zeroconf.register_service(_zeroconf_info)
        return True
    except ImportError:
        return False
    except Exception:
        return False


def stop_updater_mdns():
    global _zeroconf, _zeroconf_info
    if _zeroconf and _zeroconf_info:
        try:
            _zeroconf.unregister_service(_zeroconf_info)
            _zeroconf.close()
        except Exception:
            pass
        _zeroconf = None
        _zeroconf_info = None


class FirmwareHTTPHandler(socketserver.BaseRequestHandler):
    """Serves GET /update-status and GET /firmware.bin with user confirmation."""

    def handle(self):
        global _firmware_path, _update_ready, _gui_request_callback, _gui_update_done_callback, _pending_event, _user_decision
        try:
            data = self.request.recv(4096).decode("ascii", "replace")
            if not data.strip():
                return
            lines = data.split("\r\n")
            first = lines[0] if lines else ""
            if not first.startswith("GET "):
                self._send(400, b"Bad request")
                return
            path = first.split(" ", 2)[1].split("?")[0]
            if path == "/update-status":
                body = b'{"update_ready":true}' if (_update_ready and _firmware_path and os.path.isfile(_firmware_path)) else b'{"update_ready":false}'
                self._send(200, body, "application/json")
            elif path in ("/", "/firmware.bin"):
                if not _firmware_path or not os.path.isfile(_firmware_path):
                    self._send(404, b"NO_UPDATE")
                    return
                _user_decision = None
                _pending_event.clear()
                if _gui_request_callback:
                    _gui_request_callback(lambda ok: self._set_decision(ok))
                    _pending_event.wait(60)
                else:
                    _user_decision = True
                if _user_decision:
                    with open(_firmware_path, "rb") as f:
                        body = f.read()
                    self._send(200, body, "application/octet-stream")
                    clear_update_queue()
                    if _gui_update_done_callback:
                        _gui_update_done_callback(True)
                else:
                    self._send(404, b"NO_UPDATE")
                    clear_update_queue()
                    if _gui_update_done_callback:
                        _gui_update_done_callback(False)
            else:
                self._send(404, b"Not found")
        except Exception:
            pass

    def _set_decision(self, ok):
        global _user_decision, _pending_event
        _user_decision = ok
        _pending_event.set()

    def _send(self, code, body, content_type="text/plain"):
        if isinstance(body, str):
            body = body.encode("ascii")
        status = "200 OK" if code == 200 else "404 Not Found" if code == 404 else "400 Bad Request"
        head = f"HTTP/1.0 {status}\r\n"
        head += f"Content-Type: {content_type}\r\n"
        head += f"Content-Length: {len(body)}\r\n"
        head += "Connection: close\r\n\r\n"
        try:
            self.request.sendall(head.encode("ascii") + body)
        except Exception:
            pass
        self.request.close()


class FirmwareHTTPServer(socketserver.TCPServer):
    allow_reuse_address = True


def set_firmware_path(path):
    global _firmware_path
    _firmware_path = path


def clear_firmware_path():
    global _firmware_path
    _firmware_path = None


def set_update_ready(ready):
    global _update_ready
    _update_ready = ready


def clear_update_queue():
    """Clear update state after send or cancel. Next ESP boot will see update_ready: false."""
    global _firmware_path, _update_ready
    _firmware_path = None
    _update_ready = False


def set_gui_request_callback(callback):
    """Call when ESP requests firmware - callback(set_decision) to show dialog."""
    global _gui_request_callback
    _gui_request_callback = callback


def set_gui_update_done_callback(callback):
    """Call when update sent or cancelled - callback(sent: bool)."""
    global _gui_update_done_callback
    _gui_update_done_callback = callback
