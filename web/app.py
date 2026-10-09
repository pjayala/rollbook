# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Rollbook desktop window: `rollbook app`.

Runs the same server as `rollbook web` and shows it in its own window, no
browser tabs or address bar. Two backends, tried in this order by default:

  webview  pywebview: a native window (WKWebView on macOS, WebView2 on
           Windows, GTK/Qt WebKit on Linux). Optional dependency:
           `pip install pywebview` (Linux also needs GTK or Qt bindings).
  chrome   an installed Chromium-family browser in --app mode (Chrome,
           Edge, Chromium, Brave) with its own profile under
           $ROLLBOOK_HOME/cache/app-profile. Edge ships with Windows 10+, so
           this works there with no extra install.
  browser  last resort: a tab in the default browser.

The server stops when the window closes: pywebview's start() returns, the
browser process exits, or the page sends /api/bye on pagehide and doesn't
come back within BYE_GRACE_S (reloads do come back).
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import server

TITLE = "Rollbook"
SIZE = (1440, 960)
BYE_GRACE_S = 4.0
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
ICON = os.path.abspath(os.path.join(ASSETS, "rollbook.ico" if os.name == "nt" else "rollbook-512.png"))


def _already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def find_chromium():
    """Path to a Chromium-family browser, or None."""
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Vivaldi"):
            p = f"/Applications/{app}.app/Contents/MacOS/{app}"
            for base in ("", os.path.expanduser("~")):
                if os.path.exists(base + p):
                    return base + p
        return None
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData")]
        rels = (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
                r"Chromium\Application\chrome.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe")
        for rel in rels:
            for root in filter(None, roots):
                p = os.path.join(root, rel)
                if os.path.exists(p):
                    return p
        return shutil.which("chrome") or shutil.which("msedge")
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "microsoft-edge", "microsoft-edge-stable", "brave-browser", "vivaldi"):
        p = shutil.which(name)
        if p:
            return p
    return None


def have_webview():
    try:
        import webview  # noqa: F401
        return True
    except Exception:  # ImportError, or a GUI toolkit failing to load on Linux
        return False


def run(db_path, cache_path, port=8765, offline=False, mode="auto"):
    if mode == "auto":
        mode = "webview" if have_webview() else "chrome" if find_chromium() else "browser"
    elif mode == "webview" and not have_webview():
        sys.exit("error: pywebview not installed. pip install pywebview  (or use --mode chrome)")
    elif mode == "chrome" and not find_chromium():
        sys.exit("error: no Chrome / Edge / Chromium / Brave found  (or use --mode browser)")

    reuse = _already_running(port)
    httpd = M = None
    if reuse:
        url = f"http://127.0.0.1:{port}/"
        print(f"dashboard already running at {url}, opening a window on it")
    else:
        try:
            httpd, M, url = server.start(db_path, cache_path, port, offline)
        except OSError:
            # port taken by something else; any free port works, but browser
            # storage (segment names, settings) is per port, so warn
            httpd, M, url = server.start(db_path, cache_path, 0, offline)
            print(f"note: port {port} is busy, using {url} (saved names/settings are per port)")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app_url = url + "?app=1"
    print(f"opening {mode} window")

    try:
        if mode == "webview":
            _webview(app_url)
        elif mode == "chrome":
            _chrome(app_url, httpd, cache_path)
        else:
            webbrowser.open(url)
            if httpd:
                print("ctrl-c to stop")
                threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        if httpd:
            M.save()
            httpd.shutdown()
            httpd.server_close()


def _webview(url):
    import webview

    webview.create_window(TITLE, url, width=SIZE[0], height=SIZE[1], min_size=(900, 600),
                          text_select=True)
    # private_mode=False keeps localStorage (segment names, settings) between runs
    # icon: used by the GTK/Qt (Linux) and WinForms backends; macOS takes the
    # dock icon from the app bundle, so a plain `python` process shows Python's
    try:
        webview.start(private_mode=False, icon=ICON if os.path.exists(ICON) else None)
    except TypeError:  # pywebview < 5 has no icon argument
        webview.start(private_mode=False)


def _chrome(url, httpd, cache_path):
    profile = os.path.join(os.path.dirname(cache_path), "app-profile")
    os.makedirs(profile, exist_ok=True)
    proc = subprocess.Popen(
        [find_chromium(), f"--app={url}", f"--user-data-dir={profile}",
         f"--window-size={SIZE[0]},{SIZE[1]}", "--no-first-run", "--no-default-browser-check",
         "--disable-features=Translate"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if httpd is None:  # reusing another server: nothing to shut down
        return
    # The dedicated profile makes this its own browser process, which exits
    # with its window on Windows/Linux. On macOS it keeps running windowless,
    # so the page's pagehide beacon is the real "closed" signal.
    closed = threading.Event()
    state = {"bye": None}

    def on_bye():
        state["bye"] = time.monotonic()

    httpd.on_bye = on_bye
    while not closed.is_set():
        if proc.poll() is not None:
            break
        b = state["bye"]
        if b is not None and time.monotonic() - b > BYE_GRACE_S:
            # no page came back: if a request arrived after the bye, it was a reload
            if getattr(httpd, "last_request", 0) > b:
                state["bye"] = None
                continue
            break
        closed.wait(0.5)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
