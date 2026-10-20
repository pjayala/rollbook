# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""`rollbook web --lan`: token gate, cookie, Host check, read-only, TLS listener."""

import http.client
import io
import json
import os
import shutil
import socket
import ssl
import tempfile
import threading
import unittest

from test_end_to_end import make_export, run

import server

LAN_IP = "192.0.2.10"  # TEST-NET-1: never a real interface


class LanBase(unittest.TestCase):
    write = False
    https = True

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="rollbook-test-lan-")
        make_export(os.path.join(cls.home, "export"))
        r = run(cls.home, "db", "build", os.path.join(cls.home, "export"))
        assert r.returncode == 0, r.stdout + r.stderr
        cls.db = os.path.join(cls.home, "rollbook.db")
        cls.lan = server.lan_setup(cls.home, port=0, write=cls.write, https=cls.https, ips=[LAN_IP])
        cls.lan.bind = "127.0.0.1"  # tests stay on loopback; the Host header plays the phone
        cls.httpd, cls.M, cls.url = server.start(cls.db, os.path.join(cls.home, "cache", "web_metrics.json"),
                                                 0, offline=True, lan=cls.lan)
        for s in (cls.httpd, cls.httpd.lan_httpd):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.port = cls.httpd.lan_httpd.server_address[1]
        cls.host = f"{LAN_IP}:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.httpd.lan_httpd, cls.httpd):
            s.shutdown()
            s.server_close()
        shutil.rmtree(cls.home, True)

    def req(self, method, path, host=None, cookie=True, headers=None, body=None):
        if self.https:
            ctx = ssl.create_default_context(cafile=self.lan.cert["cert"])
            c = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=30, context=ctx)
        else:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or self.host}
        if cookie:
            h["Cookie"] = f"{server.LAN_COOKIE}={self.lan.token}"
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def post(self, path, obj, **kw):
        h = {"X-Rollbook": "1", "Content-Type": "application/json",
             "Origin": f"{self.lan.scheme}://{self.host}"}
        h.update(kw.pop("headers", {}))
        return self.req("POST", path, headers=h, body=json.dumps(obj).encode(), **kw)


class ReadOnly(LanBase):
    def test_loopback_listener_unchanged(self):
        self.assertTrue(self.url.startswith("http://127.0.0.1:"))
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")
        import urllib.request
        with urllib.request.urlopen(self.url + "api/status", timeout=30) as r:
            self.assertIs(json.loads(r.read())["readonly"], False)

    def test_everything_needs_the_token(self):
        for path in ("/", "/api/types", "/api/activity/1001", "/tiles/osm/1/0/0.png", "/app.js"):
            r, body = self.req("GET", path, cookie=False)
            self.assertEqual(r.status, 403, path)
            self.assertIn(b"rollbook web --lan", body)
        r, _ = self.req("GET", "/api/types", cookie=False, headers={"Cookie": f"{server.LAN_COOKIE}=nope"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("GET", "/?t=wrong", cookie=False)
        self.assertEqual(r.status, 403)

    def test_token_link_sets_cookie_and_strips_token(self):
        r, _ = self.req("GET", f"/?t={self.lan.token}", cookie=False)
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/")
        ck = r.getheader("Set-Cookie")
        self.assertIn(f"{server.LAN_COOKIE}={self.lan.token}", ck)
        for attr in ("HttpOnly", "SameSite=Lax", "Path=/", "Secure"):
            self.assertIn(attr, ck)
        r, _ = self.req("GET", f"/?x=1&t={self.lan.token}", cookie=False)
        self.assertEqual(r.getheader("Location"), "/?x=1")

    def test_cookie_grants_read_access(self):
        r, body = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"<html", body.lower())
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
        r, body = self.req("GET", "/api/types")
        self.assertEqual(json.loads(body)[0]["type"], "Inline Skate")
        r, body = self.req("GET", "/api/status")
        self.assertIs(json.loads(body)["readonly"], True)

    def test_foreign_host_refused(self):
        for host in ("evil.example:%d" % self.port, f"{LAN_IP}:1", "192.0.2.99:%d" % self.port):
            r, _ = self.req("GET", "/api/types", host=host)
            self.assertEqual(r.status, 403, host)
        for host in (f"localhost:{self.port}", f"127.0.0.1:{self.port}"):
            r, _ = self.req("GET", "/api/types", host=host)
            self.assertEqual(r.status, 200, host)

    def test_posts_refused_when_read_only(self):
        for path, body in (("/api/favorites", {"kind": "sessions", "id": "1001", "on": True}),
                           ("/api/edit", {"id": 1001, "name": "x"}), ("/api/profile", {"name": "x"}),
                           ("/api/import/run", {}), ("/api/bye", {})):
            r, out = self.post(path, body)
            self.assertEqual(r.status, 403, path)
            self.assertIn("lan-write", json.loads(out)["error"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "favorites.json")))
        r, _ = self.post(f"/api/favorites?t={self.lan.token}", {}, cookie=False)
        self.assertEqual(r.status, 403)  # the token is never accepted on a POST

    def test_plain_http_to_tls_port_does_not_stall(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                s.recv(100)
            except (ConnectionResetError, socket.timeout):
                pass
        idle = socket.create_connection(("127.0.0.1", self.port), timeout=10)  # never handshakes
        try:
            r, _ = self.req("GET", "/api/types")
            self.assertEqual(r.status, 200)
        finally:
            idle.close()

    def test_lan_port_in_use_refused(self):
        lan = server.Lan("x" * 22, port=self.httpd.server_address[1], names=["127.0.0.1"], https=False)
        with self.assertRaises(OSError) as cm:
            server.start(self.db, os.path.join(self.home, "cache", "web_metrics.json"), 0, offline=True, lan=lan)
        self.assertIn("in use", str(cm.exception))

    def test_banner(self):
        out = io.StringIO()
        server.lan_banner(self.lan, "always", out)
        text = out.getvalue()
        self.assertIn(f"https://{LAN_IP}:{self.port}/?t={self.lan.token}", text)
        self.assertIn(self.lan.cert["fingerprint"], text)
        self.assertIn("read-only", text)
        self.assertIn("##", text)  # a QR code (StringIO has no encoding: ASCII fallback)
        utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        server.lan_banner(self.lan, "always", utf8)
        utf8.seek(0)
        self.assertIn("\u2580", utf8.read())
        out = io.StringIO()
        server.lan_banner(self.lan, "auto", out)  # not a terminal: no QR
        self.assertNotIn("##", out.getvalue())


class ReadWrite(LanBase):
    write = True

    def test_posts_allowed_with_header_and_origin(self):
        fav = {"kind": "sessions", "id": "1001", "on": True}
        r, body = self.post("/api/favorites", fav)
        self.assertEqual(r.status, 200, body)
        r, _ = self.post("/api/favorites", fav, headers={"X-Rollbook": ""})
        self.assertEqual(r.status, 403)
        r, _ = self.post("/api/favorites", fav, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.post("/api/favorites", fav, headers={"Origin": f"http://{self.host}"})  # wrong scheme
        self.assertEqual(r.status, 403)
        r, _ = self.post("/api/favorites", fav, cookie=False)
        self.assertEqual(r.status, 403)
        r, _ = self.post("/api/bye", {})
        self.assertEqual(r.status, 403)  # only the desktop app window may stop the server
        r, body = self.req("GET", "/api/status")
        self.assertIs(json.loads(body)["readonly"], False)


class PlainHttp(LanBase):
    https = False

    def test_http_mode(self):
        self.assertIsNone(self.lan.cert)
        r, _ = self.req("GET", f"/?t={self.lan.token}", cookie=False)
        self.assertEqual(r.status, 302)
        self.assertNotIn("Secure", r.getheader("Set-Cookie"))
        r, _ = self.req("GET", "/api/types")
        self.assertEqual(r.status, 200)
        out = io.StringIO()
        server.lan_banner(self.lan, "never", out)
        self.assertIn("unencrypted", out.getvalue())


class Token(unittest.TestCase):
    def test_token_persists_and_rotates(self):
        d = tempfile.mkdtemp(prefix="rollbook-test-token-")
        self.addCleanup(shutil.rmtree, d, True)
        t = server.lan_token(d)
        self.assertGreaterEqual(len(t), 22)
        self.assertEqual(server.lan_token(d), t)
        if os.name == "posix":
            self.assertEqual(os.stat(os.path.join(d, "lan_token")).st_mode & 0o777, 0o600)
        t2 = server.lan_token(d, new=True)
        self.assertNotEqual(t2, t)
        self.assertEqual(server.lan_token(d), t2)
        with open(os.path.join(d, "lan_token"), "w") as f:
            f.write("short")
        self.assertNotEqual(server.lan_token(d), "short")

    def test_lan_ips_excludes_loopback(self):
        for ip in server.lan_ips():
            self.assertFalse(ip.startswith(("127.", "169.254.")), ip)

    def test_cli_flags(self):
        r = run(tempfile.gettempdir(), "web", "--help")
        for flag in ("--lan", "--lan-write", "--lan-port", "--lan-http", "--new-token", "--new-cert", "--qr"):
            self.assertIn(flag, r.stdout)


if __name__ == "__main__":
    unittest.main()
