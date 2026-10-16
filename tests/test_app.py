# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""`rollbook app` launcher (web/app.py): backend choice, server reuse, /api/bye shutdown.

No real window, browser, network or sleep: imports, which/exists, Popen and the
clock are faked. Stand-ins replace names inside app/server only (never
threading.Event or time.monotonic globally, the HTTP server's threads use them).
"""

import contextlib
import io
import ntpath
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import ROOT, tmpdir

import app
import server


def module_copy(mod, **overrides):
    """A copy of `mod` with some attributes replaced, to patch one module's view of it."""
    m = types.ModuleType(mod.__name__)
    m.__dict__.update(mod.__dict__)
    m.__dict__.update(overrides)
    return m


class Clock:
    """Fake monotonic clock. Only FakeEvent.wait advances it; scripted actions
    run once it reaches their time."""

    LIMIT = 120.0

    def __init__(self):
        self.t = 0.0
        self.actions = []
        self.waits = []

    def __call__(self):
        return self.t

    def at(self, t, fn):
        self.actions.append((t, fn))

    def advance(self, dt):
        self.waits.append(dt)
        self.t += dt
        if self.t > self.LIMIT:
            raise AssertionError("launcher never stopped")
        due = sorted((a for a in self.actions if a[0] <= self.t), key=lambda a: a[0])
        self.actions = [a for a in self.actions if a[0] > self.t]
        for _, fn in due:
            fn()


def fake_event_cls(clock):
    class FakeEvent:
        def __init__(self):
            self.flag = False

        def is_set(self):
            return self.flag

        def set(self):
            self.flag = True

        def wait(self, timeout=None):
            if timeout is None:
                raise AssertionError("would block forever")
            clock.advance(timeout)
            return self.flag
    return FakeEvent


class FakeProc:
    """subprocess.Popen stand-in for the browser."""

    def __init__(self, clock, exits_at=None, hangs=False):
        self.clock, self.exits_at, self.hangs = clock, exits_at, hangs
        self.returncode = None
        self.terminated_at = None
        self.killed = False

    def poll(self):
        if self.returncode is None and self.exits_at is not None and self.clock() >= self.exits_at:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated_at = self.clock()
        if not self.hangs:
            self.returncode = -15

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("chrome", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


# ------------------------------------------------------------ backend discovery

MAC = "/Applications/{0}.app/Contents/MacOS/{0}"
WIN_ENV = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
           "LocalAppData": r"C:\Users\me\AppData\Local"}


class FindChromium(unittest.TestCase):
    def find(self, platform, osname, existing=(), env=None, which=None, join=os.path.join):
        which = which or {}
        existing = set(existing)
        fake_os = types.SimpleNamespace(
            name=osname, environ=env or {},
            path=types.SimpleNamespace(join=join, exists=lambda p: p in existing,
                                       expanduser=lambda p: "/Users/me" if p == "~" else p))
        with mock.patch.object(app, "sys", types.SimpleNamespace(platform=platform)), \
                mock.patch.object(app, "os", fake_os), \
                mock.patch.object(app.shutil, "which", side_effect=lambda n: which.get(n)) as w:
            return app.find_chromium(), w

    def mac(self, *existing):
        return self.find("darwin", "posix", existing)

    def test_macos_browser_order(self):
        names = ("Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser")
        self.assertEqual(self.mac(*(MAC.format(n) for n in names))[0], MAC.format("Google Chrome"))
        self.assertEqual(self.mac(MAC.format("Brave Browser"), MAC.format("Microsoft Edge"))[0],
                         MAC.format("Microsoft Edge"))
        self.assertEqual(self.mac(MAC.format("Brave Browser"))[0], MAC.format("Brave Browser"))

    def test_macos_user_applications_and_order_beats_location(self):
        user_chrome = "/Users/me" + MAC.format("Google Chrome")
        self.assertEqual(self.mac(user_chrome)[0], user_chrome)
        self.assertEqual(self.mac(MAC.format("Microsoft Edge"), user_chrome)[0], user_chrome)

    def test_macos_none(self):
        found, which = self.mac()
        self.assertIsNone(found)
        which.assert_not_called()

    def win(self, existing=(), env=WIN_ENV, which=None):
        return self.find("win32", "nt", existing, env, which, join=ntpath.join)[0]

    def test_windows_any_root(self):
        edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        self.assertEqual(self.win([edge]), edge)
        brave = r"C:\Users\me\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"
        self.assertEqual(self.win([brave]), brave)

    def test_windows_browser_order_beats_root(self):
        chrome = r"C:\Users\me\AppData\Local\Google\Chrome\Application\chrome.exe"
        edge = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        self.assertEqual(self.win([edge, chrome]), chrome)

    def test_windows_missing_env_vars(self):
        chrome = r"C:\Users\me\AppData\Local\Google\Chrome\Application\chrome.exe"
        self.assertEqual(self.win([chrome], env={"LocalAppData": WIN_ENV["LocalAppData"]}), chrome)

    def test_windows_path_fallback(self):
        self.assertEqual(self.win(which={"chrome": "C:/x/chrome.exe", "msedge": "C:/x/msedge.exe"}), "C:/x/chrome.exe")
        self.assertEqual(self.win(which={"msedge": "C:/x/msedge.exe"}), "C:/x/msedge.exe")
        self.assertIsNone(self.win())

    def test_linux_path_order(self):
        def lin(which):
            return self.find("linux", "posix", which=which)[0]
        self.assertEqual(lin({"chromium": "/usr/bin/chromium", "brave-browser": "/usr/bin/brave-browser"}),
                         "/usr/bin/chromium")
        self.assertEqual(lin({"microsoft-edge-stable": "/e", "brave-browser": "/b"}), "/e")
        self.assertEqual(lin({"google-chrome-stable": "/g"}), "/g")
        self.assertIsNone(lin({}))


class HaveWebview(unittest.TestCase):
    def test_importable(self):
        with mock.patch.dict(sys.modules, {"webview": types.ModuleType("webview")}):
            self.assertTrue(app.have_webview())

    def test_not_installed(self):
        with mock.patch.dict(sys.modules, {"webview": None}):
            self.assertFalse(app.have_webview())

    def test_gui_toolkit_fails_to_load(self):
        real = __import__

        def imp(name, *a, **kw):
            if name == "webview":
                raise RuntimeError("no GTK or Qt")
            return real(name, *a, **kw)
        with mock.patch("builtins.__import__", side_effect=imp):
            self.assertFalse(app.have_webview())


class WebviewWindow(unittest.TestCase):
    URL = "http://127.0.0.1:1/?app=1"

    def open(self, icon, old=False):
        wv = types.ModuleType("webview")
        wv.create_window = mock.Mock()
        calls = []

        def start(**kw):
            calls.append(kw)
            if old and "icon" in kw:
                raise TypeError("start() got an unexpected keyword argument 'icon'")
        wv.start = start
        with mock.patch.dict(sys.modules, {"webview": wv}), mock.patch.object(app, "ICON", icon):
            app._webview(self.URL)
        return wv, calls

    def test_window_keeps_storage(self):
        icon = os.path.join(tmpdir(self), "icon.png")
        with open(icon, "wb"):
            pass
        wv, calls = self.open(icon)
        args, kw = wv.create_window.call_args
        self.assertEqual(args, (app.TITLE, self.URL))
        self.assertEqual((kw["width"], kw["height"]), app.SIZE)
        self.assertEqual(calls, [{"private_mode": False, "icon": icon}])

    def test_missing_icon(self):
        _, calls = self.open(os.path.join(tmpdir(self), "nope.png"))
        self.assertEqual(calls, [{"private_mode": False, "icon": None}])

    def test_old_pywebview_without_icon_argument(self):
        _, calls = self.open(os.path.join(tmpdir(self), "nope.png"), old=True)
        self.assertEqual(calls, [{"private_mode": False, "icon": None}, {"private_mode": False}])


class AlreadyRunning(unittest.TestCase):
    def test_answering_server(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/api/status" else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.assertTrue(app._already_running(httpd.server_address[1]))

    def check(self, **urlopen):
        with mock.patch.object(app.urllib.request, "urlopen", **urlopen) as u:
            result = app._already_running(8765)
        u.assert_called_once_with("http://127.0.0.1:8765/api/status", timeout=1)
        return result

    def test_nothing_listening(self):
        self.assertFalse(self.check(side_effect=ConnectionRefusedError()))

    def test_error_status(self):
        err = urllib.error.HTTPError("http://127.0.0.1:8765/api/status", 500, "boom", {}, None)
        self.assertFalse(self.check(side_effect=err))

    def test_timeout(self):
        self.assertFalse(self.check(side_effect=socket.timeout()))

    def test_non_200(self):
        cm = mock.MagicMock()
        cm.__enter__.return_value.status = 204
        self.assertFalse(self.check(return_value=cm))


# ------------------------------------------------------------ run(): mode + server

class Run(unittest.TestCase):
    PORT = 8765

    def setUp(self):
        home = tmpdir(self)
        self.db = os.path.join(home, "rollbook.db")
        self.cache = os.path.join(home, "cache", "web_metrics.json")
        self.url = f"http://127.0.0.1:{self.PORT}/"
        self.httpd, self.M = mock.Mock(), mock.Mock()
        self.threads, self.event_waits = [], 0
        test = self

        class FakeThread:
            def __init__(self, target=None, daemon=None, **kw):
                test.threads.append((target, daemon))

            def start(self):
                pass

        class FakeEvent:
            def wait(self, timeout=None):
                test.event_waits += 1
                raise KeyboardInterrupt  # ctrl-c while a plain browser tab is open
        self.m = {}
        for k, p in {
            "have_webview": mock.patch.object(app, "have_webview", return_value=False),
            "find_chromium": mock.patch.object(app, "find_chromium", return_value=None),
            "running": mock.patch.object(app, "_already_running", return_value=False),
            "webview": mock.patch.object(app, "_webview"),
            "chrome": mock.patch.object(app, "_chrome"),
            "start": mock.patch.object(app.server, "start", return_value=(self.httpd, self.M, self.url)),
            "open": mock.patch.object(app.webbrowser, "open"),
            "threading": mock.patch.object(app, "threading", module_copy(threading, Thread=FakeThread, Event=FakeEvent)),
            "token": mock.patch.object(app.secrets, "token_hex", return_value="tok"),
        }.items():
            self.m[k] = p.start()
            self.addCleanup(p.stop)

    def launch(self, mode="auto", offline=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.run(self.db, self.cache, self.PORT, offline, mode)
        return out.getvalue()

    def reset(self):
        for k, m in self.m.items():
            if isinstance(m, mock.Mock):
                m.reset_mock()
        self.httpd.reset_mock()
        self.M.reset_mock()
        self.threads.clear()
        self.event_waits = 0

    def assert_cleaned_up(self):
        self.M.save.assert_called_once_with()
        self.httpd.shutdown.assert_called_once_with()
        self.httpd.server_close.assert_called_once_with()

    def test_auto_prefers_webview(self):
        self.m["have_webview"].return_value = True
        self.m["find_chromium"].return_value = "/opt/chrome"
        self.assertIn("opening webview window", self.launch())
        self.m["webview"].assert_called_once_with(self.url + "?app=tok")
        self.m["chrome"].assert_not_called()
        self.m["open"].assert_not_called()
        self.m["find_chromium"].assert_not_called()
        self.assert_cleaned_up()

    def test_auto_falls_back_to_chromium(self):
        self.m["find_chromium"].return_value = "/opt/chrome"
        self.assertIn("opening chrome window", self.launch())
        self.m["chrome"].assert_called_once_with(self.url + "?app=tok", self.httpd, self.cache, "tok")
        self.m["webview"].assert_not_called()
        self.m["open"].assert_not_called()

    def test_auto_last_resort_is_a_tab_without_app_flag(self):
        self.assertIn("opening browser window", self.launch())
        self.m["open"].assert_called_once_with(self.url)  # no ?app=: a tab never sends /api/bye
        self.assertEqual(self.event_waits, 1)  # serves until ctrl-c
        self.assert_cleaned_up()

    def test_explicit_mode_honoured(self):
        self.m["have_webview"].return_value = True
        self.m["find_chromium"].return_value = "/opt/chrome"
        for mode, used in (("webview", "webview"), ("chrome", "chrome"), ("browser", "open")):
            with self.subTest(mode=mode):
                self.reset()
                self.launch(mode)
                for k in ("webview", "chrome", "open"):
                    self.assertEqual(self.m[k].called, k == used, k)
                if mode != "webview":
                    self.m["have_webview"].assert_not_called()
                if mode == "browser":
                    self.m["find_chromium"].assert_not_called()

    def test_explicit_unavailable_mode_exits_without_fallback(self):
        self.m["find_chromium"].return_value = "/opt/chrome"
        with self.assertRaises(SystemExit) as cm:
            self.launch("webview")
        self.assertIn("pywebview", str(cm.exception.code))
        self.m["find_chromium"].return_value = None
        self.m["have_webview"].return_value = True
        with self.assertRaises(SystemExit) as cm:
            self.launch("chrome")
        self.assertIn("Chrome", str(cm.exception.code))
        self.m["start"].assert_not_called()
        self.m["webview"].assert_not_called()
        self.m["chrome"].assert_not_called()

    def test_starts_server_thread_and_cleans_up(self):
        self.m["have_webview"].return_value = True
        self.launch(offline=True)
        self.m["start"].assert_called_once_with(self.db, self.cache, self.PORT, True)
        self.assertEqual(self.threads, [(self.httpd.serve_forever, True)])
        self.assert_cleaned_up()

    def test_cleans_up_when_window_fails(self):
        self.m["have_webview"].return_value = True
        self.m["webview"].side_effect = RuntimeError("no display")
        with self.assertRaises(RuntimeError):
            self.launch()
        self.assert_cleaned_up()

    def test_ctrl_c_is_a_clean_exit(self):
        self.m["find_chromium"].return_value = "/opt/chrome"
        self.m["chrome"].side_effect = KeyboardInterrupt
        self.launch()
        self.assert_cleaned_up()

    def test_reuses_running_server(self):
        self.m["running"].return_value = True
        self.m["have_webview"].return_value = True
        self.m["find_chromium"].return_value = "/opt/chrome"
        for mode in ("webview", "chrome", "browser"):
            with self.subTest(mode=mode):
                self.reset()
                self.assertIn("already running", self.launch(mode))
                self.m["running"].assert_called_once_with(self.PORT)
                self.m["start"].assert_not_called()
                self.assertEqual(self.threads, [])
                self.httpd.shutdown.assert_not_called()
                self.M.save.assert_not_called()
                if mode == "webview":
                    self.m["webview"].assert_called_once_with(self.url + "?app=tok")
                elif mode == "chrome":
                    self.m["chrome"].assert_called_once_with(self.url + "?app=tok", None, self.cache, "tok")
                else:
                    self.m["open"].assert_called_once_with(self.url)
                    self.assertEqual(self.event_waits, 0)  # nothing of ours to keep alive

    def test_busy_port_uses_a_free_one(self):
        other = "http://127.0.0.1:50123/"
        self.m["have_webview"].return_value = True
        self.m["start"].side_effect = [OSError("address in use"), (self.httpd, self.M, other)]
        out = self.launch()
        self.assertEqual([c.args[2] for c in self.m["start"].call_args_list], [self.PORT, 0])
        self.m["webview"].assert_called_once_with(other + "?app=tok")
        self.assertIn(f"port {self.PORT} is busy", out)
        self.assert_cleaned_up()


# ------------------------------------------------------------ _chrome(): close detection

class ChromeWindow(unittest.TestCase):
    TOKEN = "tok"
    URL = "http://127.0.0.1:8765/?app=tok"

    def setUp(self):
        self.home = tmpdir(self)
        self.cache = os.path.join(self.home, "cache", "web_metrics.json")
        self.clock = Clock()
        self.httpd = types.SimpleNamespace(app_pages={})
        self.popen = mock.Mock()
        for p in (mock.patch.object(app, "time", module_copy(time, monotonic=self.clock)),
                  mock.patch.object(app, "threading", module_copy(threading, Event=fake_event_cls(self.clock))),
                  mock.patch.object(app, "subprocess", module_copy(subprocess, Popen=self.popen)),
                  mock.patch.object(app, "find_chromium", return_value="/opt/chrome")):
            p.start()
            self.addCleanup(p.stop)

    def launch(self, proc, *script, httpd="server"):
        for t, fn in script:
            self.clock.at(t, fn)
        self.popen.return_value = proc
        app._chrome(self.URL, self.httpd if httpd == "server" else httpd, self.cache, self.TOKEN)
        return proc

    def bye(self, token=TOKEN):
        self.httpd.on_bye(token)

    def reload(self, token=TOKEN):
        self.httpd.app_pages[token] = self.clock()

    def test_grace_is_four_seconds(self):
        self.assertEqual(app.BYE_GRACE_S, 4.0)

    def test_command_line_and_profile(self):
        self.launch(FakeProc(self.clock, exits_at=0.5))
        (cmd,), kw = self.popen.call_args
        profile = os.path.join(self.home, "cache", "app-profile")
        self.assertEqual(cmd[0], "/opt/chrome")
        for arg in (f"--app={self.URL}", f"--user-data-dir={profile}", "--no-first-run",
                    f"--window-size={app.SIZE[0]},{app.SIZE[1]}"):
            self.assertIn(arg, cmd)
        self.assertTrue(os.path.isdir(profile))
        self.assertEqual((kw["stdout"], kw["stderr"]), (subprocess.DEVNULL, subprocess.DEVNULL))

    def test_reused_server_only_opens_window(self):
        proc = self.launch(FakeProc(self.clock), httpd=None)
        self.popen.assert_called_once()
        self.assertEqual(self.clock.waits, [])
        self.assertIsNone(proc.terminated_at)

    def test_browser_exit_closes(self):
        proc = self.launch(FakeProc(self.clock, exits_at=2.0))
        self.assertEqual(self.clock.t, 2.0)
        self.assertIsNone(proc.terminated_at)

    def test_bye_without_reload_stops_after_grace(self):
        proc = self.launch(FakeProc(self.clock), (1.0, self.bye))
        self.assertEqual(proc.terminated_at, 5.5)  # strictly > 4 s: still waiting at 5.0
        self.assertTrue(all(w <= 0.5 for w in self.clock.waits))

    def test_reload_within_grace_cancels_shutdown(self):
        proc = self.launch(FakeProc(self.clock), (1.0, self.bye), (1.5, self.reload), (10.0, self.bye))
        self.assertEqual(proc.terminated_at, 14.5)  # only the second, real close

    def test_reload_at_end_of_grace_still_counts(self):
        proc = self.launch(FakeProc(self.clock, exits_at=8.0), (1.0, self.bye), (5.0, self.reload))
        self.assertIsNone(proc.terminated_at)
        self.assertEqual(self.clock.t, 8.0)

    def test_request_before_bye_is_not_a_reload(self):
        proc = self.launch(FakeProc(self.clock), (0.5, self.reload), (1.0, self.bye))
        self.assertEqual(proc.terminated_at, 5.5)

    def test_latest_bye_wins(self):
        proc = self.launch(FakeProc(self.clock), (1.0, self.bye), (3.0, self.bye))
        self.assertEqual(proc.terminated_at, 7.5)

    def test_other_windows_bye_is_ignored(self):
        # a second `rollbook app` reusing this server: closing its window must not stop this one
        proc = self.launch(FakeProc(self.clock, exits_at=8.0), (1.0, lambda: self.bye("other")),
                           (1.2, lambda: self.bye(None)))
        self.assertIsNone(proc.terminated_at)
        self.assertEqual(self.clock.t, 8.0)

    def test_other_windows_page_load_is_not_a_reload(self):
        proc = self.launch(FakeProc(self.clock), (1.0, self.bye), (1.5, lambda: self.reload("other")))
        self.assertEqual(proc.terminated_at, 5.5)

    def test_stubborn_browser_is_killed(self):
        proc = self.launch(FakeProc(self.clock, hangs=True), (1.0, self.bye))
        self.assertEqual(proc.terminated_at, 5.5)
        self.assertTrue(proc.killed)


# ------------------------------------------------------------ /api/bye over HTTP

class ByeEndpoint(unittest.TestCase):
    """A real server.start() (no DB: /api/bye and static pages never open it)."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="rollbook-test-app-")
        cls.cache = os.path.join(cls.home, "cache", "web_metrics.json")
        with mock.patch.object(server.Metrics, "warm", lambda self: None), \
                contextlib.redirect_stdout(io.StringIO()):
            cls.httpd, cls.M, cls.url = server.start(os.path.join(cls.home, "rollbook.db"), cls.cache, 0, offline=True)
        threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        cls.port = cls.httpd.server_address[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.home, True)

    def setUp(self):
        self.byes = []
        self.httpd.on_bye = lambda app=None: self.byes.append(app)
        self.httpd.last_request = 0
        self.httpd.app_pages.clear()
        self.addCleanup(setattr, self.httpd, "on_bye", None)

    def post(self, path, headers=None):
        req = urllib.request.Request(self.url.rstrip("/") + path, data=b"", method="POST", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def get(self, path):
        with urllib.request.urlopen(self.url.rstrip("/") + path, timeout=10) as r:
            r.read()
            return r.status

    def hit(self, path):
        """GET, any status (API calls 500 here: this server has no DB)."""
        try:
            return self.get(path)
        except urllib.error.HTTPError as e:
            return e.code

    def test_rejects_foreign_or_missing_origin(self):
        for h in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"},
                  {"Origin": f"https://127.0.0.1:{self.port}"},
                  {"Origin": f"http://127.0.0.1:{self.port + 1}"},
                  {"Origin": self.origin, "Host": "evil.example"}):
            with self.subTest(headers=h):
                self.assertEqual(self.post("/api/bye?app=1", h), 403)
        self.assertEqual(self.byes, [])

    def test_same_origin_beacon_notifies_launcher(self):
        self.assertEqual(self.post("/api/bye?app=1", {"Origin": self.origin}), 204)
        self.assertEqual(self.post("/api/bye", {"Origin": f"http://localhost:{self.port}",
                                                "Host": f"localhost:{self.port}"}), 204)
        # the handler answers 204 before calling on_bye: wait for the callbacks
        end = time.monotonic() + 5
        while len(self.byes) < 2 and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(self.byes, ["1", None])  # the window token is passed through

    def test_without_launcher_is_harmless(self):
        self.httpd.on_bye = None
        self.assertEqual(self.post("/api/bye?app=1", {"Origin": self.origin}), 204)

    def test_only_exact_path(self):
        for p in ("/api/bye/", "/api/byebye", "/api/profile"):
            self.post(p, {"Origin": self.origin})
        self.assertEqual(self.byes, [])

    def test_get_marks_activity_post_does_not(self):
        with mock.patch.object(server, "time", module_copy(time, monotonic=lambda: 42.0)):
            self.assertEqual(self.get("/"), 200)
            self.assertEqual(self.httpd.last_request, 42.0)
        self.post("/api/bye", {"Origin": self.origin})
        self.assertEqual(self.httpd.last_request, 42.0)

    def test_page_loads_recorded_per_window(self):
        with mock.patch.object(server, "time", module_copy(time, monotonic=lambda: 7.0)):
            self.get("/?app=w1")
            self.hit("/api/status?app=w2")  # API / tile requests are not page loads
            self.get("/")
        self.assertEqual(self.httpd.app_pages, {"w1": 7.0})

    def test_only_the_app_window_sends_the_beacon(self):
        with open(os.path.join(ROOT, "web", "static", "app.js"), encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        i = next(i for i, ln in enumerate(lines) if "sendBeacon(" in ln)
        self.assertIn('has("app")', lines[i - 1])
        self.assertIn('"/api/bye?app=" + encodeURIComponent(', lines[i + 1])  # echoes the window token

    # ---- the launcher loop against this server, on a fake clock shared by both

    def chrome(self, *script, exits_at=None):
        clock = Clock()
        ft = module_copy(time, monotonic=clock)
        proc = FakeProc(clock, exits_at=exits_at)
        for t, fn in script:
            clock.at(t, fn)
        with mock.patch.object(app, "time", ft), mock.patch.object(server, "time", ft), \
                mock.patch.object(app, "threading", module_copy(threading, Event=fake_event_cls(clock))), \
                mock.patch.object(app, "subprocess", module_copy(subprocess, Popen=mock.Mock(return_value=proc))), \
                mock.patch.object(app, "find_chromium", return_value="/opt/chrome"):
            app._chrome(self.url + "?app=t1", self.httpd, self.cache, "t1")
        return proc, clock

    def beacon(self, origin=None, token="t1"):
        """POST /api/bye and wait until the handler has called on_bye (it
        answers 204 first), so the bye is stamped at this fake time."""
        cb, landed = self.httpd.on_bye, threading.Event()

        def wrapped(app=None):
            cb(app)
            landed.set()
        self.httpd.on_bye = wrapped
        code = self.post(f"/api/bye?app={token}", {"Origin": origin or self.origin})
        if code == 204:
            self.assertTrue(landed.wait(5))
        return code

    def reload(self, token="t1"):
        self.assertEqual(self.get(f"/?app={token}"), 200)

    def test_e2e_closed_window_stops(self):
        proc, _ = self.chrome((1.0, self.beacon))
        self.assertEqual(proc.terminated_at, 5.5)

    def test_e2e_reload_keeps_running(self):
        proc, _ = self.chrome((1.0, self.beacon), (1.5, self.reload), (10.0, self.beacon))
        self.assertEqual(proc.terminated_at, 14.5)

    def test_e2e_foreign_bye_ignored(self):
        def evil():
            self.assertEqual(self.beacon("https://evil.example"), 403)
        proc, clock = self.chrome((1.0, evil), exits_at=8.0)
        self.assertIsNone(proc.terminated_at)
        self.assertEqual(clock.t, 8.0)

    def test_e2e_second_window_closing_keeps_first_running(self):
        def other_window():
            self.reload("t2")
            self.assertEqual(self.beacon(token="t2"), 204)
        proc, clock = self.chrome((1.0, other_window), exits_at=8.0)
        self.assertIsNone(proc.terminated_at)
        self.assertEqual(clock.t, 8.0)

    def test_e2e_api_poll_after_bye_is_not_a_reload(self):
        proc, _ = self.chrome((1.0, self.beacon), (1.5, lambda: self.hit("/api/status")))
        self.assertEqual(proc.terminated_at, 5.5)

    def test_e2e_request_before_bye_is_not_a_reload(self):
        proc, _ = self.chrome((0.5, self.reload), (1.0, self.beacon))
        self.assertEqual(proc.terminated_at, 5.5)


if __name__ == "__main__":
    unittest.main()
