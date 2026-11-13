#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Open every dashboard page of the synthetic demo athlete in headless Chrome and
fail on a script error: an uncaught exception or console error, or a page that
route() replaced by "Error: …".

    python3 tools/smoke.py            # builds the demo in $TMPDIR/rollbook-demo if missing

The node tests run pure helpers and the scripts' top level; this is the check that
the views themselves still draw. Needs Chrome / Chromium; no real data, no network.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import screenshots as sh  # noqa: E402  same demo home, server and wait

PORT = sh.PORT = 8792
ERR = re.compile(r"Uncaught|CONSOLE.*\b(?:Error|TypeError|ReferenceError|SyntaxError)\b|\"(?:Error|TypeError|ReferenceError)\b")


def visit(chrome, profile, route, w):
    p = subprocess.run([chrome, "--headless=new", f"--user-data-dir={profile}", f"--window-size={w},1400",
                        "--enable-logging=stderr", "--v=0", "--virtual-time-budget=20000", "--dump-dom",
                        f"http://127.0.0.1:{PORT}/{route}"], capture_output=True, text=True, timeout=120)
    bad = [ln for ln in p.stderr.splitlines() if "CONSOLE" in ln and ERR.search(ln)]
    m = re.search(r'<p class="down">Error: ([^<]*)', p.stdout)
    if m:
        bad.append("page: Error: " + m.group(1))
    if 'id="app"' not in p.stdout or "Loading session…" in p.stdout:
        bad.append("page did not finish drawing")
    return bad


def main():
    chrome = sh.find_chromium()
    if not chrome:
        sys.exit("needs Chrome / Chromium / Edge / Brave")
    if not os.path.exists(os.path.join(sh.HOME, "rollbook.db")):
        subprocess.run([sys.executable, os.path.join(sh.ROOT, "tools", "demo.py"), sh.HOME], check=True)
    env = dict(os.environ, ROLLBOOK_HOME=sh.HOME)
    srv = subprocess.Popen([sys.executable, os.path.join(sh.ROOT, "bin", "rollbook"), "web", "--no-open",
                            "--offline", "--port", str(PORT)], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    profile = tempfile.mkdtemp(prefix="rollbook-smoke-")
    failed = 0
    try:
        sh.wait_ready()
        acts = sh.api("activities")
        skate = [a for a in acts if a["type"] == "Inline Skate"]
        laps = [a for a in skate if a.get("laps_n")][-1]
        ob = [a for a in skate if a.get("route") == "out-back" and a["id"] != laps["id"]]
        if not ob:
            sys.exit("the demo has no out-and-back skate: rebuild it (tools/demo.py)")
        virt = [a for a in acts if a["type"] == "Virtual Ride"][-1]
        seg = sh.api("segments?type=Inline%20Skate")["segments"][0]
        routes = ["#/", "#/progress", "#/sessions", "#/map", "#/segments", f"#/segment/{seg['id']}", "#/profile",
                  "#/gear", f"#/session/{laps['id']}", f"#/session/{ob[-1]['id']}", f"#/session/{virt['id']}",
                  f"#/session/{skate[0]['id']}", f"#/compare/{laps['id']}/{skate[-2]['id']}"]
        routes = list(dict.fromkeys(routes))  # the same session twice tests nothing more
        for w in (1440, 390):
            for r in routes:
                bad = visit(chrome, profile, r, w)
                print(("FAIL" if bad else "ok  "), w, r)
                for b in bad:
                    print("     ", b[:300])
                failed += bool(bad)
    finally:
        srv.terminate()
        srv.wait(10)
        shutil.rmtree(profile, True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
