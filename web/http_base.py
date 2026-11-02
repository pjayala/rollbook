# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""HTTP plumbing shared by both listeners: responses, the request-body reader,
and the checks every request passes (Host / same origin, the --lan token).

Handler classes from server.make_handler mix this in and set `lan` (None for
the loopback listener, a lanaccess.Lan for the network one).
"""

import gzip
import json
import os
import sys
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler

import store
from lanaccess import LAN_COOKIE, LAN_COOKIE_AGE

REQUEST_TIMEOUT_S = 60  # any socket read / write once a request is being handled
REJECT_DRAIN = 1 << 20  # a refused body up to this is read and dropped, else the connection closes
# no inline scripts anywhere; templates use style="" attributes, so inline styles stay allowed
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
       "style-src 'self' 'unsafe-inline'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")


class Base(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_S  # per socket operation: a stalled client frees its thread
    server_version = "rollbook"
    lan = None

    def log_message(self, format, *args):
        if os.environ.get("ROLLBOOK_WEB_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def send(self, code, body, ctype="application/json", cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        gz = (len(body) > 4096 and not ctype.startswith("image/")
              and "gzip" in self.headers.get("Accept-Encoding", ""))
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "max-age=86400" if ctype in ("image/svg+xml", "image/png", "image/x-icon", "font/woff2") else cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        if self.lan:
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, separators=(",", ":"), allow_nan=False,
                                   default=str))

    # ---------------------------------------------------- --lan gate

    def _lan_cookie(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return None
        return c[LAN_COOKIE].value if LAN_COOKIE in c else None

    def _lan_gate(self, u, q):
        """LAN listener only. True = request answered here (refused or redirected)."""
        if self.lan is None:
            return False
        if self.headers.get("Host") not in self.lan.hosts:
            self._reject(403, "unknown host name: open one of the addresses printed by "
                              "`rollbook web --lan`\n", "text/plain", drain=False)
            return True
        if "t" in q and self.command == "GET":
            if not self.lan.ok_token(q["t"]):
                self.send(403, "wrong or old link: use the one printed by `rollbook web --lan`\n", "text/plain")
                return True
            rest = urllib.parse.urlencode({k: v for k, v in q.items() if k != "t"})
            self.send_response(302)
            self.send_header("Location", u.path + (f"?{rest}" if rest else ""))
            self.send_header("Set-Cookie", f"{LAN_COOKIE}={self.lan.token}; Path=/; Max-Age={LAN_COOKIE_AGE}; "
                                           "HttpOnly; SameSite=Lax" + ("; Secure" if self.lan.https else ""))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if self.lan.ok_token(self._lan_cookie()):
            return False
        self._reject(403, "not signed in: open the link (or scan the QR code) printed by "
                          "`rollbook web --lan` once on this device\n", "text/plain", drain=False)
        return True

    def _local_hosts(self):
        port = self.server.server_address[1]
        return (f"127.0.0.1:{port}", f"localhost:{port}")

    def _same_origin(self):
        """Reject other websites (CSRF) and DNS-rebinding hosts."""
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        if self.lan is not None:
            return host in self.lan.hosts and (origin is None or origin == f"{self.lan.scheme}://{host}")
        ours = self._local_hosts()
        if host not in ours:
            return False
        return origin is None or origin in tuple("http://" + o for o in ours)

    def _reject(self, code, body, ctype="application/json", drain=True):
        """Refuse a POST without breaking the connection: read (and drop) a
        small request body first, otherwise close instead of keeping alive."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1  # unparseable: can't drain it, so close
        if self.headers.get("Transfer-Encoding"):
            n = -1  # chunked: length unknown, close
        if drain and 0 < n <= REJECT_DRAIN:
            while n > 0:
                n -= len(self.rfile.read(min(n, 1 << 20)) or b"x" * n)
        else:
            self.close_connection = True
        return self.send(code, body, ctype)

    def _not_saved(self, what, e):
        """A failed store write: the details (paths) go to the log, not the client."""
        self.log_error("%s not saved: %r", what, e)
        return self.json({"error": f"{what} not saved: {e.strerror or type(e).__name__}"}, 500)

    def _read_json(self, limit=65536):
        """The request body as a JSON object, or None after answering 400/413.
        The body is read whole or not at all (a refused one is drained, or the
        connection closed: the client then sees the answer, not a reset)."""
        if self.headers.get("Content-Length") is None:
            self.close_connection = True
            self.json({"error": "Content-Length required"}, 411)
            return None
        try:
            n = int(self.headers["Content-Length"])
        except ValueError:
            n = -1
        if not 0 <= n <= limit:
            self._reject(413 if n > limit else 400, json.dumps({"error": f"body must be 0-{limit} bytes"}))
            return None
        try:
            b = store.loads(self.rfile.read(n) or b"{}")
        except (ValueError, RecursionError) as e:  # bad JSON / UTF-8, NaN, [[[[...
            self.json({"error": f"bad JSON: {type(e).__name__}"}, 400)
            return None
        if not isinstance(b, dict):
            self.json({"error": "expected a JSON object"}, 400)
            return None
        return b
