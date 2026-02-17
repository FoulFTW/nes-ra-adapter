#!/usr/bin/env python3
"""
Customizable achievement alert window with video - like RA Layout Manager.
Uses pywebview for video playback. Falls back to simple popup if pywebview unavailable.

Video: Same as https://github.com/Colossus-Gaming/retroachievements-layout-manager
       achievement-notification.webm
"""

import json
import os
import sys
import threading
import urllib.request

# Default video URL (RA Layout Manager)
VIDEO_URL = "https://github.com/Colossus-Gaming/retroachievements-layout-manager/raw/master/Retro%20Achievement%20Tracker/video/achievement-notification.webm"


def _get_base_dir():
    """Base dir for scripts/data. PyInstaller: use exe dir for writable, _MEIPASS for bundled."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_misc_dir():
    """Path to misc folder (template HTML). PyInstaller: bundled in _MEIPASS."""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "misc")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "misc")


def _get_video_path():
    """Return path to cached achievement-notification.webm (writable: next to exe)."""
    base = _get_base_dir()
    return os.path.join(base, "misc", "video", "achievement-notification.webm")


def _ensure_video_downloaded(callback=None):
    """Download video from RA Layout Manager if not present. Runs in thread."""
    path = _get_video_path()
    if os.path.isfile(path):
        if callback:
            callback(path)
        return path

    def do():
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            urllib.request.urlretrieve(VIDEO_URL, path)
            if callback:
                callback(path)
        except Exception:
            if callback:
                callback(None)

    threading.Thread(target=do, daemon=True).start()
    return None


def _make_html(achievement, video_path, duration_ms=8000):
    """Build HTML with achievement data and video path embedded."""
    misc_dir = _get_misc_dir()
    template = os.path.join(misc_dir, "achievement_alert.html")
    if not os.path.isfile(template):
        return None
    with open(template, "r", encoding="utf-8") as f:
        html = f.read()
    def esc(s):
        return s.replace("\\", "\\\\").replace("'", "\\'").replace("<", "&lt;").replace("\n", "\\n").replace("\r", "\\r")
    title = esc(str(achievement.get("title") or "Achievement Unlocked"))
    desc = esc(str(achievement.get("description") or ""))
    pts = achievement.get("points")
    pts_str = str(pts) + " pts" if pts is not None else ""
    url = esc(str(achievement.get("url") or ""))
    vid_src = ""
    if video_path and os.path.isfile(video_path):
        vid_src = esc("file:///" + video_path.replace("\\", "/"))
    # Inject init script before </body>
    init = f"""
<script>
document.addEventListener('DOMContentLoaded', function() {{
  show({{
    title: '{title}',
    description: '{desc}',
    points: {json.dumps(pts)},
    url: '{url}',
    videoPath: '{vid_src}'
  }});
  setTimeout(function() {{ try {{ if (typeof pywebview !== 'undefined' && pywebview.api && pywebview.api.destroy) pywebview.api.destroy(); }} catch(e) {{}} }}, {duration_ms});
}});
</script>
"""
    html = html.replace("</body>", init + "</body>")
    return html


def show_achievement_alert(achievement, x=100, y=100, width=400, height=300, video_path=None, duration_ms=8000):
    """
    Show customizable achievement alert window with video.
    achievement: dict with title, description, points, url
    x, y: window position
    width, height: window size
    video_path: path to .webm (default: download from RA Layout Manager)
    duration_ms: auto-close after this many ms
    """
    try:
        import webview
    except ImportError:
        return None  # Fallback to tkinter popup

    vp = video_path or _get_video_path()
    html = _make_html(achievement or {}, vp, duration_ms)
    if not html:
        return None

    class Api:
        def __init__(self):
            self._window = None

        def set_window(self, window):
            self._window = window

        def destroy(self):
            if self._window:
                try:
                    self._window.destroy()
                except Exception:
                    pass

    def run():
        api = Api()
        win = webview.create_window(
            "Achievement Unlocked!",
            html=html,
            x=x, y=y, width=width, height=height,
            resizable=True,
            frameless=False,
            on_top=True,
            js_api=api,
        )
        api.set_window(win)
        webview.start(debug=False)

    # Run in thread so we don't block
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return True


def show_achievement_alert_async(achievement, x=100, y=100, width=400, height=300, video_path=None, duration_ms=8000, on_video_ready=None):
    """
    Show alert, downloading video first if needed.
    on_video_ready: callback(path) when video is ready - can then call show_achievement_alert.
    Returns True if video alert will be shown (immediately or after download), False/None to fall back to tkinter popup.
    """
    try:
        import webview
    except ImportError:
        return None
    path = video_path or _get_video_path()
    if os.path.isfile(path):
        show_achievement_alert(achievement, x, y, width, height, path, duration_ms)
        return True

    def cb(p):
        if p and on_video_ready:
            on_video_ready(p)
        if p:
            show_achievement_alert(achievement, x, y, width, height, p, duration_ms)

    _ensure_video_downloaded(cb)
    return True  # Will show after download completes
