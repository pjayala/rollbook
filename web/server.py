# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Rollbook dashboard: localhost UI for the rollbook.db built by `rollbook db build`.

Stdlib only. Started via `./rollbook web`; binds to 127.0.0.1 and opens the
database read-only. Static files live in web/static, JSON under /api.

This module wires things together and routes requests; the pieces live in
  analysis.py    per-activity metrics (derive) and their cache (Metrics)
  api.py         the JSON API: GET views, POST actions, the stores
  conditions.py  weather (from the DB's weather table) and the wind / slope split
  favorites.py   starred sessions and segments
  http_base.py   responses, body reader, Host / origin / --lan token checks
  lanaccess.py   the --lan listener: token, addresses, TLS, banner
  tiles.py       basemap tile proxy + cache
"""

import errno
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import ThreadingHTTPServer

import analysis
import api
import edits
import gear
import geonames
import profile as athlete
import weather
from favorites import Favorites
from http_base import Base
from lanaccess import LAN_READONLY_MSG, TLSServer, lan_banner, tls_context
from tiles import TILE_MAXZ, TILE_SOURCES, TileCache

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2"}


def make_handler(db_path, M, tiles, lan=None):
    """Request handler class. lan=None: the loopback listener (trusted, as always);
    a Lan: the `--lan` listener, where every request needs the token cookie."""
    local = threading.local()

    def dbc():
        if not hasattr(local, "db"):
            local.db = api.connect(db_path)
        api.EDITS.sync(local.db)  # picks up edits made since this thread's last request
        return local.db

    class H(Base):
        def do_GET(self):
            now = time.monotonic()
            self.server.last_request = now
            u = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            p = u.path
            if self._lan_gate(u, q):
                return
            if lan is None and self.headers.get("Host") not in self._local_hosts():
                # DNS rebinding: a website whose name now resolves to 127.0.0.1 would
                # otherwise read every response (GPS tracks included) as same-origin
                return self.send(403, "unknown host name: open http://127.0.0.1:"
                                      f"{self.server.server_address[1]}/\n", "text/plain")
            if lan is None and "app" in q and not p.startswith(("/api/", "/tiles/")):
                # app launcher: a page load of window <token> after its bye = a reload
                self.server.app_pages[q["app"]] = now
            try:
                if p.startswith("/api/"):
                    return self.route_api(p[5:], q)
                if p.startswith("/tiles/"):
                    return self.tile(p[7:])
                return self.static(p)
            except BrokenPipeError:
                pass
            except Exception as e:  # keep the server alive, report to client
                self.json({"error": f"{type(e).__name__}: {e}"}, 500)

        def route_api(self, p, q):
            db = dbc()
            fn = api.GET.get(p)
            if fn is not None:
                r = api.clean(fn(db, q, M))
                if p == "status":
                    r["readonly"] = lan is not None and not lan.write
                return self.json(r)
            if p.startswith("activity/"):
                tail = p[len("activity/"):]
                if not api.ACTIVITY_ID.fullmatch(tail):
                    return self.json({"error": "bad id"}, 400)
                a = api.api_activity(db, q, M, int(tail))
                if a is None:
                    return self.json({"error": "not found"}, 404)
                return self.json(api.clean(a))
            return self.json({"error": "unknown endpoint"}, 404)

        def do_POST(self):
            if self.headers.get("Transfer-Encoding"):  # every client here sends Content-Length
                self.close_connection = True
                return self.send(501, '{"error":"Transfer-Encoding not supported"}')
            u = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            if lan is not None:
                q.pop("t", None)  # the token is only accepted on a GET (link / QR code)
                if self._lan_gate(u, q):
                    return
                if not lan.write or u.path == "/api/bye":  # bye: only the desktop app window
                    return self._reject(403, json.dumps({"error": LAN_READONLY_MSG}))
            if u.path == "/api/bye":
                return self.bye(q)
            action = api.POST.get(u.path)
            if action is None:
                return self._reject(404, "not found", "text/plain")
            # custom header: a cross-site page can't send it without a CORS
            # preflight, which this server never approves
            if not self._same_origin() or self.headers.get("X-Rollbook") != "1":
                return self._reject(403, '{"error":"forbidden"}')
            length = 0
            if action.limit is not None:
                body = self._read_json(action.limit)
                if body is None:
                    return
            else:  # reads the body itself (upload) or takes none
                body = None
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0:  # rfile.read(-1) would read to EOF
                    return self._reject(400, '{"error":"bad Content-Length"}')
            req = api.Req(body, q, dbc, M, self.rfile, length)
            try:
                code, out = action.fn(req)
            except (ValueError, TypeError) as e:
                return self.json({"error": str(e)}, 400)
            except OSError as e:
                return self._not_saved(action.what, e)
            if code >= 500:
                self.log_error("%s %s: %s", u.path, code, out.get("error") if isinstance(out, dict) else out)
            return self.json(api.clean(out), code)

        def bye(self, q):
            # the app window's page sends it from pagehide (a beacon, so no
            # custom header possible; Origin is always set for beacons)
            if not self._same_origin() or not self.headers.get("Origin"):
                return self._reject(403, "forbidden", "text/plain")
            cb = getattr(self.server, "on_bye", None)
            if cb:  # before answering (it only stamps a time): a 204 means it was seen
                cb(q.get("app"))  # which window: a reused server also serves other launchers' windows
            return self.send(204, b"", "text/plain")

        def tile(self, p):
            parts = p.removesuffix(".png").split("/")
            if (len(parts) != 4 or parts[0] not in TILE_SOURCES
                    or not all(x.isdigit() for x in parts[1:])):
                return self.send(400, "bad tile", "text/plain")
            src, z, x, y = parts[0], *map(int, parts[1:])
            if z > TILE_MAXZ or x >= 2 ** z or y >= 2 ** z:
                return self.send(400, "bad tile", "text/plain")
            body = tiles.get(src, z, x, y)
            if body is None:
                return self.send(404, "tile unavailable", "text/plain")
            self.send(200, body, "image/png", "max-age=604800")

        def static(self, p):
            name = {"/": "index.html", "": "index.html"}.get(p, p.lstrip("/"))
            if "/" in name or name.startswith(".") or name not in os.listdir(STATIC):
                return self.send(404, "not found", "text/plain")
            ext = os.path.splitext(name)[1]
            with open(os.path.join(STATIC, name), "rb") as f:
                self.send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    H.lan = lan
    return H


def _port_answers(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _remap_all(mapping):
    """The importer replaced sessions: edits and gear choices follow (each on its own)."""
    errs = []
    for what, fn in (("edits", api.EDITS.remap), ("gear", api.GEAR.remap)):
        try:
            fn(mapping)
        except Exception as e:  # noqa: BLE001  the other store must still move
            errs.append(f"{what}: {e}")
    if errs:
        raise OSError("; ".join(errs))


def start(db_path, cache_path, port=8765, offline=False, lan=None):
    """Bind and return (httpd, metrics, url); caller runs httpd.serve_forever().
    With lan (a Lan from lan_setup), httpd.lan_httpd is the network listener,
    bound but not yet serving."""
    import importer
    import lastimport
    import segments  # imports api / analysis; must come after they are fully loaded

    home = os.path.dirname(os.path.abspath(db_path))
    cache = os.path.dirname(cache_path)
    api.EDITS = edits.Edits(home)
    M = analysis.Metrics(db_path, cache_path, api.connect)
    api.SEGS = segments.SegmentStore(db_path, M, os.path.join(cache, "web_segments.json"))
    imp = api.IMPORTER = importer.Importer(db_path, M)
    imp.on_store = api.EDITS.forget_removed
    api.GEAR = gear.Gear(home)
    imp.on_remap = _remap_all
    imp.offline = offline
    imp.last = api.LAST = lastimport.LastImport(os.path.join(home, "last_import.json"))
    api.PROFILE = athlete.Profile(home)
    api.FAVS = Favorites(os.path.join(home, "favorites.json"))
    threading.Thread(target=M.warm, daemon=True).start()
    tiles = TileCache(os.path.join(cache, "tiles"), offline)
    api.GEO = geonames.Geonames(os.path.join(cache, "geonames.json"), offline)
    api.WX_JOB = weather.OnDemand(home, importer.TOOL, offline,
                                  busy=lambda: imp.job.get("state") == "running")
    imp.after_import = api.WX_JOB.backfill
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path, M, tiles))
    httpd.daemon_threads = True
    httpd.on_bye = None  # set by the app launcher: called with the window token when a page closes
    httpd.app_pages = {}  # window token -> monotonic time of its last page load
    httpd.lan_httpd = None
    if lan is not None:
        try:
            if lan.port and _port_answers(lan.port):
                # macOS lets 0.0.0.0:p bind (SO_REUSEADDR) while another process
                # holds 127.0.0.1:p; the two would then split the traffic
                raise OSError(errno.EADDRINUSE, f"port {lan.port} is already in use")
            h = make_handler(db_path, M, tiles, lan)
            addr = (lan.bind, lan.port)
            srv = TLSServer(addr, h, tls_context(lan)) if lan.https else ThreadingHTTPServer(addr, h)
        except BaseException:
            httpd.server_close()
            raise
        srv.daemon_threads = True
        srv.app_pages = {}
        lan.set_port(srv.server_address[1])
        httpd.lan_httpd = srv
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"serving {url}  (db {db_path}, tiles {'cache only' if offline else 'online + cache'})")
    return httpd, M, url


def serve(db_path, cache_path, port=8765, open_browser=True, offline=False, lan=None, qr="auto"):
    httpd, M, url = start(db_path, cache_path, port, offline, lan)
    if httpd.lan_httpd:
        threading.Thread(target=httpd.lan_httpd.serve_forever, daemon=True).start()
        lan_banner(lan, qr)
    print("ctrl-c to stop", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        M.save()
        if httpd.lan_httpd:
            httpd.lan_httpd.shutdown()
            httpd.lan_httpd.server_close()
        httpd.server_close()


if __name__ == "__main__":
    home = os.path.abspath(os.path.expanduser(os.environ.get("ROLLBOOK_HOME") or "~/.local/share/rollbook"))
    serve(os.path.join(home, "rollbook.db"), os.path.join(home, "cache", "web_metrics.json"),
          int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
