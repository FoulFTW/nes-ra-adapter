#!/usr/bin/env python3
"""Bump VERSION in everdrive_bridge_gui.py by 0.01."""
import re
with open('everdrive_bridge_gui.py', 'r', encoding='utf-8') as f:
    c = f.read()
m = re.search(r'VERSION = "([\d.]+)"', c)
if m:
    v = float(m.group(1))
    v = round(v + 0.01, 2)
    new = '%.2f' % v
    c = re.sub(r'VERSION = "[\d.]+"', 'VERSION = "' + new + '"', c)
    with open('everdrive_bridge_gui.py', 'w', encoding='utf-8') as f:
        f.write(c)
    print('Version: ' + new)
