#!/usr/bin/env python3
"""
RA Layout Manager integration - write achievement unlocks to stream-labels folder.

Compatible with: https://github.com/Colossus-Gaming/retroachievements-layout-manager

When the Bridge receives an achievement from the ESP32, we can write to the same
stream-labels folder the RA Layout Manager uses. OBS and other tools that read
from that folder will display our real-hardware achievements.

Format matches RA Layout Manager's alerts/focus/last-five structure.
"""

import json
import os
import re


def _badge_number_from_url(url):
    """Extract badge number from URL, e.g. Badge/001234.png -> 001234."""
    if not url:
        return ""
    m = re.search(r"Badge[/\\](\d+)", str(url), re.I)
    return m.group(1) if m else ""


def write_achievement_to_stream_labels(achievement, stream_labels_path, game_id=None, game_title=None):
    """
    Write achievement to stream-labels folder in RA Layout Manager format.

    achievement: dict with id, title, description, points, url
    stream_labels_path: path to stream-labels folder (e.g. RA Layout Manager's output folder)
    game_id, game_title: optional - from current game if known
    """
    if not stream_labels_path or not os.path.isdir(stream_labels_path):
        return False
    ach = achievement or {}
    aid = ach.get("id") or 0
    title = str(ach.get("title") or "Achievement Unlocked")
    desc = str(ach.get("description") or "")
    points = ach.get("points")
    url = ach.get("url") or ""
    badge = _badge_number_from_url(url)
    gid = game_id if game_id is not None else 0
    gtitle = str(game_title or "")

    # RA Layout Manager format (from their README)
    data = {
        "id": aid,
        "gameId": gid,
        "gameTitle": gtitle,
        "title": title,
        "description": desc,
        "points": points if points is not None else 0,
        "trueRatio": ach.get("trueRatio", 0),
        "badgeNumber": badge or str(aid).zfill(6),
        "hardcoreAchieved": ach.get("hardcoreAchieved", True),
        "displayOrder": ach.get("displayOrder", 0),
    }
    try:
        for name in ("alerts", "focus", "last-five"):
            path = os.path.join(stream_labels_path, f"{name}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        return True
    except Exception:
        return False
