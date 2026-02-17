#!/usr/bin/env python3
"""
RetroAchievements API client for game images.
Requires RA username + API key from https://retroachievements.org/controlpanel.php
"""

import json
import os
import threading
import urllib.request

# NES console ID on RetroAchievements
NES_CONSOLE_ID = 7

# Cache: MD5 -> game_id (persists for session)
_game_cache = {}
_cache_lock = threading.Lock()


def game_id(md5, username=None, api_key=None):
    """
    Get RA game_id from MD5 hash. Returns (game_id, error) or (None, error_msg).
    game_id is zero-padded string for image URL, e.g. "052570".
    """
    md5_lower = (md5 or "").strip().lower()
    if not md5_lower:
        return None, "No MD5"

    with _cache_lock:
        if md5_lower in _game_cache:
            return _game_cache[md5_lower], None

    # Try env vars if not passed
    username = username or os.environ.get("RA_USERNAME", "").strip()
    api_key = api_key or os.environ.get("RA_API_KEY", "").strip()
    if not username or not api_key:
        return None, "RA credentials needed (RA_USERNAME, RA_API_KEY or Achievements tab)"

    try:
        url = (
            f"https://retroachievements.org/API/API_GetGameList.php"
            f"?i={NES_CONSOLE_ID}&h=1&y={api_key}"
        )
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "RA-Bridge/1.0")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # Response: array of {ID, ImageIcon, Hashes: [...]}
        games = data if isinstance(data, list) else list(data.values()) if isinstance(data, dict) else []
        for game in games:
            hashes = game.get("Hashes") or game.get("hashes") or []
            for h in hashes:
                if (h or "").lower() == md5_lower:
                    icon = game.get("ImageIcon") or game.get("imageIcon") or ""
                    if "/" in icon:
                        base = icon.split("/")[-1].replace(".png", "")
                        gid_str = base.zfill(6) if base.isdigit() else base
                    else:
                        gid_str = str(game.get("ID") or game.get("id") or "").zfill(6)
                    with _cache_lock:
                        _game_cache[md5_lower] = gid_str
                    return gid_str, None

        return None, "Game not found"
    except Exception as e:
        return None, str(e)


def game_image_url(game_id_str):
    """Full URL for game icon image."""
    if not game_id_str:
        return None
    gid = str(game_id_str).zfill(6) if str(game_id_str).isdigit() else game_id_str
    return f"https://media.retroachievements.org/Images/{gid}.png"
