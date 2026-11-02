# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""`rollbook web --lan`: the network listener's token, addresses, TLS and banner.

A second listener on 0.0.0.0 (HTTPS with a self-signed cert by default) for a
phone on the same network; the loopback listener is untouched. On the LAN
listener every request needs the token: `?t=<token>` once (sets an HttpOnly
cookie, then redirects the token out of the URL), the cookie afterwards. Host
must be one of this machine's names (DNS rebinding), and POSTs are refused
unless --lan-write. The checks themselves are in http_base.Base._lan_gate.
"""

import hmac
import ipaddress
import os
import re
import secrets
import socket
import ssl
import sys
from http.server import ThreadingHTTPServer

LAN_COOKIE = "rb_lan"
LAN_COOKIE_AGE = 365 * 86400
TLS_TIMEOUT_S = 60  # TLS handshake on the LAN listener
LAN_READONLY_MSG = "read-only from the network; restart with `rollbook web --lan-write` to allow changes"


class Lan:
    def __init__(self, token, write=False, https=True, port=8766, names=(), cert=None):
        self.token, self.write, self.https, self.cert = token, write, https, cert
        self.cert_args = None  # (tls_dir, host, ips) for tls.ensure_cert, set by lan_setup
        self.bind = "0.0.0.0"
        self.names = list(dict.fromkeys(names))  # host names / IPs, no port
        self.scheme = "https" if https else "http"
        self.set_port(port)

    def set_port(self, port):
        self.port = port
        self.hosts = {f"{n}:{port}" for n in self.names}
        if (self.https and port == 443) or (not self.https and port == 80):
            self.hosts |= set(self.names)

    def ok_token(self, value):
        return bool(value) and hmac.compare_digest(value.encode("utf-8", "replace"), self.token.encode())

    def urls(self, with_token=True):
        """Phone URLs, one per LAN address (not loopback / .local)."""
        out = []
        for n in self.names:
            if n in ("127.0.0.1", "localhost") or n.endswith(".local"):
                continue
            out.append(f"{self.scheme}://{n}:{self.port}/" + (f"?t={self.token}" if with_token else ""))
        return out


def lan_ips():
    """This machine's IPv4 addresses that a phone could reach (no loopback / link-local)."""
    ips = []
    for probe in ("10.254.254.254", "192.168.254.254"):
        try:  # UDP connect sends nothing; it just picks the outgoing interface
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                ips.append(s.getsockname()[0])
        except OSError:
            pass
    try:
        ips += [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    good = []
    for a in ips:
        ip = ipaddress.ip_address(a)
        if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified) and a not in good:
            good.append(a)
    return good


def lan_token(home, new=False):
    """Random token in $ROLLBOOK_HOME/lan_token, reused across restarts."""
    path = os.path.join(home, "lan_token")
    if not new:
        try:
            with open(path, encoding="ascii") as f:
                t = f.read().strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{22,}", t):
                return t
        except (OSError, UnicodeDecodeError):
            pass
    t = secrets.token_urlsafe(16)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(t + "\n")
    os.replace(tmp, path)
    return t


def lan_setup(home, port=8766, write=False, https=True, new_token=False, new_cert=False, ips=None):
    """Token, names and (for https) the self-signed cert for a --lan listener."""
    import tls

    ips = lan_ips() if ips is None else list(ips)
    host = tls.safe_host(socket.gethostname())
    cert_args = (os.path.join(home, "tls"), host, ips)
    cert = tls.ensure_cert(*cert_args, force=new_cert) if https else None
    names = ips + [f"{host}.local", "localhost", "127.0.0.1"]
    lan = Lan(lan_token(home, new_token), write, https, port, names, cert)
    lan.cert_args = cert_args
    return lan


class TLSServer(ThreadingHTTPServer):
    """HTTPS: the handshake runs in the request's own thread, not the accept loop,
    so a slow or plain-HTTP client can't stall the listener."""

    def __init__(self, addr, handler, ctx):
        self.ssl_ctx = ctx
        super().__init__(addr, handler)

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(TLS_TIMEOUT_S)
            request = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            self.shutdown_request(request)
            return
        super().process_request_thread(request, client_address)


def tls_context(lan):
    import tls

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(lan.cert["cert"], lan.cert["key"])
    except (ssl.SSLError, OSError):  # damaged / mismatched pair: make a new one
        lan.cert = tls.ensure_cert(*lan.cert_args, force=True)
        ctx.load_cert_chain(lan.cert["cert"], lan.cert["key"])
    return ctx


def lan_banner(lan, qr_mode="auto", out=None):
    """What to print on start: URLs, QR codes, the cert fingerprint, the warning."""
    import qr

    out = out or sys.stdout
    tty = hasattr(out, "isatty") and out.isatty()
    show_qr = qr_mode == "always" or (qr_mode == "auto" and tty)
    lines = [f"\nnetwork access ({'read-write' if lan.write else 'read-only'}), "
             f"{lan.scheme}://<this machine>:{lan.port}/ — open on your phone:"]
    urls = lan.urls()
    if not urls:
        lines.append("  no network address found (offline?); only https://localhost works")
    for u in urls:
        lines.append(f"  {u}")
        if show_qr:
            lines.append(qr.render(qr.encode(u), ansi=qr.can_render_blocks(out)))
    if lan.cert:
        lines += [f"certificate SHA-256 {lan.cert['fingerprint']}",
                  "  self-signed: the phone warns about it. Check this fingerprint in the browser's",
                  "  certificate details, then Advanced -> Proceed."]
    else:
        lines.append("WARNING: plain HTTP (--lan-http): the token and your data cross the network unencrypted.")
    lines.append(f"Anyone with this link on your network can {'view and change' if lan.write else 'view'} "
                 "your training data. Rotate it with --new-token.")
    print("\n".join(lines), file=out, flush=True)
