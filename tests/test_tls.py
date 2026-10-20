# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""web/tls.py: the self-signed certificate behind `rollbook web --lan`."""

import datetime as dt
import os
import socket
import ssl
import threading
import unittest

from helpers import tmpdir

import tls

HOST = "Test-Laptop.local"
IPS = ["192.0.2.10", "10.0.0.7"]


def handshake(cert, key, server_hostname="127.0.0.1"):
    """Serve one TLS connection with (cert, key); a client verifies it against cert."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        c, _ = srv.accept()
        try:
            with ctx.wrap_socket(c, server_side=True) as s:
                s.sendall(b"ok")
        except (ssl.SSLError, OSError):
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        client = ssl.create_default_context(cafile=cert)
        with client.wrap_socket(socket.create_connection(srv.getsockname(), timeout=10),
                                server_hostname=server_hostname) as s:
            return s.recv(2)
    finally:
        t.join(10)
        srv.close()


class Primitives(unittest.TestCase):
    def test_der_integers_and_oids(self):
        self.assertEqual(tls._int(0), b"\x02\x01\x00")
        self.assertEqual(tls._int(127), b"\x02\x01\x7f")
        self.assertEqual(tls._int(128), b"\x02\x02\x00\x80")  # positive: leading zero
        self.assertEqual(tls._oid("1.2.840.113549.1.1.11"), bytes.fromhex("06092a864886f70d01010b"))
        self.assertEqual(tls._oid_str(bytes.fromhex("2a864886f70d01010b")), "1.2.840.113549.1.1.11")
        self.assertEqual(tls._len(300), b"\x82\x01\x2c")

    def test_primality(self):
        for p in (2, 3, 65537, 2 ** 61 - 1, 2 ** 127 - 1):
            self.assertTrue(tls.is_probable_prime(p), p)
        for c in (1, 561, 41041, 2 ** 61 + 1, (2 ** 61 - 1) * (2 ** 31 - 1)):  # incl. Carmichael numbers
            self.assertFalse(tls.is_probable_prime(c), c)

    def test_rsa_sign_verifies(self):
        n, e, d, p, q = tls.rsa_key(1024)
        self.assertEqual(n.bit_length(), 1024)
        self.assertEqual(p * q, n)
        sig = tls.rsa_sign(b"hello", n, d)
        em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(128, "big")
        self.assertTrue(em.startswith(b"\x00\x01\xff"))
        self.assertTrue(em.endswith(tls.hashlib.sha256(b"hello").digest()))

    def test_safe_host(self):
        self.assertEqual(tls.safe_host("Pedros-MacBook-Pro.local"), "pedros-macbook-pro")
        self.assertEqual(tls.safe_host("my host$x"), "my-host-x")
        self.assertEqual(tls.safe_host(""), "rollbook")


class Certificates(unittest.TestCase):
    def check_profile(self, info):
        self.assertEqual(info["cn"], "Rollbook (test-laptop)")
        self.assertEqual(sorted(info["dns"]), ["localhost", "test-laptop.local"])
        self.assertEqual(sorted(info["ips"]), sorted(["127.0.0.1", *IPS]))
        self.assertEqual(info["eku"], [tls.OID_SERVER_AUTH])
        self.assertIs(info["ca"], False)
        self.assertEqual(info["key_usage"], 0xA0)  # digitalSignature + keyEncipherment
        self.assertEqual(sorted(info["critical"]), sorted([tls.OID_BC, tls.OID_KU]))
        days = (info["not_after"] - dt.datetime.now(dt.timezone.utc)).days
        self.assertIn(days, (tls.DAYS - 1, tls.DAYS))
        self.assertRegex(info["fingerprint"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")

    def generate(self, generator):
        d = os.path.join(tmpdir(self), "tls")
        r = tls.ensure_cert(d, HOST, IPS, generator=generator)
        self.assertEqual(r["generator"], generator)
        return d, r

    def test_python_generator_handshake(self):
        d, r = self.generate("python")
        self.check_profile(tls.cert_info(r["cert"]))
        for name in ("127.0.0.1", "localhost"):
            self.assertEqual(handshake(r["cert"], r["key"], name), b"ok")
        with self.assertRaises(ssl.SSLCertVerificationError):
            handshake(r["cert"], r["key"], "example.com")
        if os.name == "posix":
            self.assertEqual(os.stat(r["key"]).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)

    @unittest.skipUnless(tls.openssl_binary(), "no openssl on PATH")
    def test_openssl_generator_same_profile(self):
        _, r = self.generate("openssl")
        self.check_profile(tls.cert_info(r["cert"]))
        self.assertEqual(handshake(r["cert"], r["key"]), b"ok")
        if os.name == "posix":
            self.assertEqual(os.stat(r["key"]).st_mode & 0o777, 0o600)

    def test_falls_back_to_python_without_openssl(self):
        d = tmpdir(self)
        self.assertFalse(tls.generate_openssl(os.path.join(d, "c"), os.path.join(d, "k"), "x", ["localhost"],
                                              ["127.0.0.1"], binary=os.path.join(d, "no-such-openssl")))
        orig = tls.openssl_binary
        tls.openssl_binary = lambda: None
        try:
            r = tls.ensure_cert(os.path.join(d, "tls"), HOST, IPS)
        finally:
            tls.openssl_binary = orig
        self.assertEqual(r["generator"], "python")

    def test_reused_until_names_change_or_expiry(self):
        d, r = self.generate("python")
        again = tls.ensure_cert(d, HOST, list(reversed(IPS)), generator="python")
        self.assertEqual((again["generator"], again["fingerprint"]), ("existing", r["fingerprint"]))
        moved = tls.ensure_cert(d, HOST, ["192.0.2.99"], generator="python")
        self.assertEqual(moved["generator"], "python")
        self.assertNotEqual(moved["fingerprint"], r["fingerprint"])
        forced = tls.ensure_cert(d, HOST, ["192.0.2.99"], force=True, generator="python")
        self.assertNotEqual(forced["fingerprint"], moved["fingerprint"])
        # close to expiry: renewed
        orig = tls.DAYS
        tls.DAYS = tls.RENEW_DAYS - 1
        try:
            short = tls.ensure_cert(d, HOST, IPS, force=True, generator="python")
        finally:
            tls.DAYS = orig
        renewed = tls.ensure_cert(d, HOST, IPS, generator="python")
        self.assertNotEqual(renewed["fingerprint"], short["fingerprint"])

    def test_unreadable_cert_is_replaced(self):
        d, _ = self.generate("python")
        with open(os.path.join(d, "cert.pem"), "w") as f:
            f.write("garbage")
        self.assertEqual(tls.ensure_cert(d, HOST, IPS, generator="python")["generator"], "python")


if __name__ == "__main__":
    unittest.main()
