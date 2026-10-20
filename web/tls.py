# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Self-signed TLS certificate for `rollbook web --lan`.

Python's ssl module can serve TLS but not create certificates, so:

1. `openssl req -x509` when an openssl binary is on PATH (macOS, Linux, Git for
   Windows), with a config file written here so the profile doesn't depend on
   the system openssl.cnf;
2. otherwise a pure-Python fallback: RSA key generation (Miller-Rabin),
   PKCS#1 v1.5 / SHA-256 signature and hand-written DER.

Both produce the same profile: RSA 2048, SHA-256, CN "Rollbook (<host>)",
basicConstraints CA:FALSE, keyUsage digitalSignature+keyEncipherment,
extendedKeyUsage serverAuth, subjectAltName = the LAN IPs + <host>.local +
localhost + 127.0.0.1, valid 397 days. The certificate only protects the
phone <-> laptop hop on a home network; the browser will warn about it (it is
self-signed), and its SHA-256 fingerprint is printed so it can be checked.

The cert is reused across restarts (so the phone's "proceed anyway" sticks)
until its names change or fewer than RENEW_DAYS remain.
"""

import base64
import datetime as dt
import hashlib
import ipaddress
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile

DAYS = 397
RENEW_DAYS = 30
BITS = 2048
E = 65537

OID_CN = "2.5.4.3"
OID_RSA = "1.2.840.113549.1.1.1"
OID_SHA256_RSA = "1.2.840.113549.1.1.11"
OID_SKI = "2.5.29.14"
OID_KU = "2.5.29.15"
OID_SAN = "2.5.29.17"
OID_BC = "2.5.29.19"
OID_EKU = "2.5.29.37"
OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"
DIGESTINFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


def safe_host(name):
    """A DNS label usable in the cert and the openssl config (no `$`, spaces...)."""
    label = re.sub(r"[^A-Za-z0-9-]", "-", (name or "").split(".")[0]).strip("-").lower()
    return label[:63] or "rollbook"


# ---------------------------------------------------------------- DER writer


def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, content):
    return bytes([tag]) + _len(len(content)) + content


def _seq(*items):
    return _tlv(0x30, b"".join(items))


def _int(v):
    return _tlv(0x02, v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big"))


def _oid(dotted):
    parts = [int(x) for x in dotted.split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, bytes(out))


def _bits(b, unused=0):
    return _tlv(0x03, bytes([unused]) + b)


def _time(t):
    if 1950 <= t.year < 2050:
        return _tlv(0x17, t.strftime("%y%m%d%H%M%SZ").encode())
    return _tlv(0x18, t.strftime("%Y%m%d%H%M%SZ").encode())


def _name(cn):
    return _seq(_tlv(0x31, _seq(_oid(OID_CN), _tlv(0x0C, cn.encode("utf-8")))))


def _ext(oid, value, critical=False):
    return _seq(_oid(oid), *([_tlv(0x01, b"\xff")] if critical else []), _tlv(0x04, value))


def _pem(kind, der):
    b = base64.b64encode(der).decode()
    return f"-----BEGIN {kind}-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + f"\n-----END {kind}-----\n"


# ---------------------------------------------------------------- RSA


def _small_primes(limit=2000):
    sieve = bytearray([1]) * limit
    sieve[:2] = b"\0\0"
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i::i] = bytearray(len(sieve[i * i::i]))
    return [i for i in range(limit) if sieve[i]]


SMALL_PRIMES = _small_primes()


def is_probable_prime(n, rounds=40):
    if n < 2:
        return False
    for p in SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        # top two bits set so p*q has exactly 2*bits bits; odd
        c = secrets.randbits(bits) | (0b11 << (bits - 2)) | 1
        if c % E != 1 and is_probable_prime(c):  # e must be invertible mod p-1
            return c


def rsa_key(bits=BITS):
    """(n, e, d, p, q) from the OS CSPRNG (`secrets`)."""
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        d = pow(E, -1, math.lcm(p - 1, q - 1))
        return n, E, d, p, q


def rsa_sign(msg, n, d):
    """RSASSA-PKCS1-v1_5 with SHA-256."""
    k = (n.bit_length() + 7) // 8
    t = DIGESTINFO_SHA256 + hashlib.sha256(msg).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def generate_python(cert_path, key_path, cn, dns, ips, days=DAYS):
    n, e, d, p, q = rsa_key()
    pub = _seq(_int(n), _int(e))
    spki = _seq(_seq(_oid(OID_RSA), b"\x05\x00"), _bits(pub))
    alg = _seq(_oid(OID_SHA256_RSA), b"\x05\x00")
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    san = b"".join([_tlv(0x82, h.encode("ascii")) for h in dns]
                   + [_tlv(0x87, ipaddress.ip_address(i).packed) for i in ips])
    exts = _seq(
        _ext(OID_BC, _seq(), critical=True),
        _ext(OID_KU, _bits(b"\xa0", 5), critical=True),  # digitalSignature, keyEncipherment
        _ext(OID_EKU, _seq(_oid(OID_SERVER_AUTH))),
        _ext(OID_SAN, _seq(san)),
        _ext(OID_SKI, _tlv(0x04, hashlib.sha1(pub).digest())),
    )
    tbs = _seq(
        _tlv(0xA0, _int(2)),  # v3
        _int(secrets.randbits(63) | 1 << 62),
        alg, _name(cn),
        _seq(_time(now - dt.timedelta(hours=1)), _time(now + dt.timedelta(days=days))),
        _name(cn), spki,
        _tlv(0xA3, exts),
    )
    cert = _seq(tbs, alg, _bits(rsa_sign(tbs, n, d)))
    key = _seq(_int(0), _int(n), _int(e), _int(d), _int(p), _int(q),
               _int(d % (p - 1)), _int(d % (q - 1)), _int(pow(q, -1, p)))
    _write(key_path, _pem("RSA PRIVATE KEY", key), private=True)
    _write(cert_path, _pem("CERTIFICATE", cert))


# ---------------------------------------------------------------- openssl


def openssl_binary():
    return shutil.which("openssl")


def generate_openssl(cert_path, key_path, cn, dns, ips, days=DAYS, binary=None):
    """True when openssl produced both files; False to fall back to Python."""
    binary = binary or openssl_binary()
    if not binary:
        return False
    san = [f"DNS.{i} = {h}" for i, h in enumerate(dns, 1)] + [f"IP.{i} = {a}" for i, a in enumerate(ips, 1)]
    cfg = "\n".join([
        "[req]", "distinguished_name = dn", "x509_extensions = ext", "prompt = no", "",
        "[dn]", f"CN = {cn}", "",
        "[ext]", "basicConstraints = critical, CA:FALSE",
        "keyUsage = critical, digitalSignature, keyEncipherment",
        "extendedKeyUsage = serverAuth", "subjectAltName = @san", "subjectKeyIdentifier = hash", "",
        "[san]", *san, ""])
    tmp = tempfile.mkdtemp(prefix="rollbook-tls-")
    try:
        conf, kp, cp = (os.path.join(tmp, x) for x in ("req.cnf", "key.pem", "cert.pem"))
        with open(conf, "w", encoding="ascii") as f:
            f.write(cfg)
        r = subprocess.run([binary, "req", "-x509", "-new", "-newkey", f"rsa:{BITS}", "-nodes", "-sha256",
                            "-days", str(days), "-keyout", kp, "-out", cp, "-config", conf],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        if r.returncode != 0 or not (os.path.exists(kp) and os.path.exists(cp)):
            return False
        with open(kp, "rb") as f:
            key = f.read()
        with open(cp, "rb") as f:
            cert = f.read()
        cert_info(cert)  # parseable and ours, or fall back
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    finally:
        shutil.rmtree(tmp, True)
    _write(key_path, key, private=True)
    _write(cert_path, cert)
    return True


def _write(path, data, private=False):
    if isinstance(data, str):
        data = data.encode("ascii")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    if private and os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------- DER reader


def _read(b, i=0):
    tag, ln = b[i], b[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(b[i:i + k], "big")
        i += k
    return tag, b[i:i + ln], i + ln


def _items(b):
    i, out = 0, []
    while i < len(b):
        tag, val, i = _read(b, i)
        out.append((tag, val))
    return out


def _oid_str(b):
    parts = [b[0] // 40, b[0] % 40]
    v = 0
    for x in b[1:]:
        v = v << 7 | (x & 0x7F)
        if not x & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))


def _parse_time(tag, val):
    s = val.decode()
    t = dt.datetime.strptime(s, "%y%m%d%H%M%SZ" if tag == 0x17 else "%Y%m%d%H%M%SZ")
    return t.replace(tzinfo=dt.timezone.utc)


def pem_to_der(pem):
    if isinstance(pem, str):
        pem = pem.encode()
    m = re.search(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", pem, re.S)
    if not m:
        raise ValueError("no PEM certificate")
    return base64.b64decode(b"".join(m.group(1).split()))


def cert_info(pem):
    """The fields Rollbook cares about, from a PEM certificate (bytes / str / path)."""
    if isinstance(pem, str) and os.path.exists(pem):
        with open(pem, "rb") as f:
            pem = f.read()
    der = pem_to_der(pem)
    try:
        tag, cert, _ = _read(der)
        tbs = _items(_items(cert)[0][1])
        if tbs[0][0] == 0xA0:
            tbs = tbs[1:]
        validity = _items(tbs[3][1])
        cn = None
        for _, rdn in _items(tbs[4][1]):
            for _, atv in _items(rdn):
                (_, oid), (_, val) = _items(atv)[:2]
                if _oid_str(oid) == OID_CN:
                    cn = val.decode("utf-8")
        info = {"cn": cn, "not_before": _parse_time(*validity[0]), "not_after": _parse_time(*validity[1]),
                "dns": [], "ips": [], "eku": [], "ca": None, "key_usage": None, "critical": [],
                "fingerprint": ":".join(f"{x:02X}" for x in hashlib.sha256(der).digest())}
        for t, val in tbs[6:]:
            if t != 0xA3:
                continue
            for _, ext in _items(_items(val)[0][1]):
                parts = _items(ext)
                oid = _oid_str(parts[0][1])
                if len(parts) == 3 and parts[1][1] == b"\xff":
                    info["critical"].append(oid)
                body = parts[-1][1]
                if oid == OID_SAN:
                    for gt, gv in _items(_items(body)[0][1]):
                        if gt == 0x82:
                            info["dns"].append(gv.decode("ascii"))
                        elif gt == 0x87:
                            info["ips"].append(str(ipaddress.ip_address(gv)))
                elif oid == OID_EKU:
                    info["eku"] = [_oid_str(v) for _, v in _items(_items(body)[0][1])]
                elif oid == OID_BC:
                    inner = _items(_items(body)[0][1])
                    info["ca"] = bool(inner and inner[0][0] == 0x01 and inner[0][1] == b"\xff")
                elif oid == OID_KU:
                    bits = _items(body)[0][1]
                    info["key_usage"] = bits[1] if len(bits) > 1 else 0
        return info
    except (IndexError, ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"unreadable certificate: {e}")


# ---------------------------------------------------------------- entry point


def ensure_cert(tls_dir, host, ips, force=False, generator=None):
    """Return {cert, key, fingerprint, not_after, generator} for these names.

    generator: None = openssl if available else python; "python" / "openssl" to force.
    """
    os.makedirs(tls_dir, exist_ok=True)
    if os.name == "posix":
        os.chmod(tls_dir, 0o700)
    cert, key = os.path.join(tls_dir, "cert.pem"), os.path.join(tls_dir, "key.pem")
    label = safe_host(host)
    dns = sorted({f"{label}.local", "localhost"})
    ips = sorted({"127.0.0.1", *ips}, key=lambda a: ipaddress.ip_address(a).packed)
    used = "existing"
    info = None
    if not force and os.path.exists(cert) and os.path.exists(key):
        try:
            info = cert_info(cert)
        except (OSError, ValueError):
            info = None
        now = dt.datetime.now(dt.timezone.utc)
        if info and (sorted(info["dns"]) != dns or sorted(info["ips"], key=lambda a: ipaddress.ip_address(a).packed) != ips
                     or info["not_after"] - now < dt.timedelta(days=RENEW_DAYS)):
            info = None
    if info is None:
        cn = f"Rollbook ({label})"
        if generator != "python" and generate_openssl(cert, key, cn, dns, ips, days=DAYS):
            used = "openssl"
        elif generator == "openssl":
            raise RuntimeError("openssl could not create the certificate")
        else:
            generate_python(cert, key, cn, dns, ips, days=DAYS)
            used = "python"
        info = cert_info(cert)
    return {"cert": cert, "key": key, "fingerprint": info["fingerprint"],
            "not_after": info["not_after"], "generator": used, "dns": dns, "ips": ips}
