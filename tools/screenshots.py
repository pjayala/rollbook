#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Regenerate docs/screenshots/*.png from the synthetic demo athlete.

    python3 tools/screenshots.py            # builds /tmp/rollbook-demo if missing

Needs Google Chrome / Chromium (headless). Runs `rollbook web --offline` on the
demo home only: no real data, no network, so maps have no basemap.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "web"))
from app import find_chromium  # noqa: E402

HOME = os.environ.get("ROLLBOOK_DEMO", os.path.join(tempfile.gettempdir(), "rollbook-demo"))
OUT = os.path.join(ROOT, "docs", "screenshots")
PORT = 8791


def api(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/{path}", timeout=120) as r:
        return json.load(r)


def wait_ready():
    for _ in range(300):
        try:
            if not api("status")["running"]:
                break
        except OSError:
            pass
        time.sleep(1)
    for _ in range(300):  # segments are computed on first request
        if "segments" in api("segments?type=Inline%20Skate"):
            return
        time.sleep(1)


def shoot(chrome, profile, name, route, w, h):
    # `type` in the URL sets the sport (localStorage) before the app boots
    url = f"http://127.0.0.1:{PORT}/{route}"
    path = os.path.join(OUT, name)
    subprocess.run([chrome, "--headless=new", "--hide-scrollbars", f"--user-data-dir={profile}",
                    f"--window-size={w},{h}", "--force-device-scale-factor=1",
                    "--virtual-time-budget=20000", f"--screenshot={path}", url],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    print("wrote", os.path.relpath(path, ROOT))


def main():
    chrome = find_chromium()
    if not chrome:
        sys.exit("needs Chrome / Chromium / Edge / Brave")
    if not os.path.exists(os.path.join(HOME, "rollbook.db")):
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "demo.py"), HOME], check=True)
    env = dict(os.environ, ROLLBOOK_HOME=HOME)
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "rollbook"), "web", "--no-open",
                            "--offline", "--port", str(PORT)], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    profile = tempfile.mkdtemp(prefix="rollbook-shots-")
    try:
        wait_ready()
        acts = api("activities")
        skate = [a for a in acts if a["type"] == "Inline Skate"]
        laps = [a for a in skate if a.get("laps_n")][-1]
        virt = [a for a in acts if a["type"] == "Virtual Ride"][-1]
        segs = api("segments?type=Inline%20Skate")["segments"]
        seg = max((s for s in segs if s["full"]), key=lambda s: (len(s["efforts"]), s["m"]))
        os.makedirs(OUT, exist_ok=True)
        shoot(chrome, profile, "progress.png", "#/", 1440, 1400)
        shoot(chrome, profile, "session.png", f"#/session/{laps['id']}", 1440, 1440)
        shoot(chrome, profile, "segments.png", f"#/segment/{seg['id']}", 1440, 1250)
        shoot(chrome, profile, "heatmap.png", "#/map", 1440, 900)
        shoot(chrome, profile, "power.png", f"#/session/{virt['id']}", 1440, 1440)
    finally:
        srv.terminate()
        srv.wait(10)
        shutil.rmtree(profile, True)


if __name__ == "__main__":
    main()
