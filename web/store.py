# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Small JSON files in $ROLLBOOK_HOME (edits, profile, favorites, last import).

write_json: atomic, 0600, fsynced. The temp file gets a unique name and is
created with O_EXCL (and O_NOFOLLOW where it exists), so a file or symlink
planted at that path is never written through. Windows can't replace a file
another process has open, so a failed replace is retried once.

load_object: the parsed file if it is a JSON object, else None (missing,
unreadable, not UTF-8, not JSON, a non-finite number, too big, or e.g. a list).
The files are written ASCII-only (non-ASCII as JSON escapes), so any str round-trips.
"""

import json
import os
import secrets
import threading
import time


def _no_constant(name):
    raise ValueError(f"{name} is not allowed")


def _finite(s):
    f = float(s)
    if f != f or f in (float("inf"), float("-inf")):  # 1e999
        raise ValueError(f"{s} is not a finite number")
    return f


def loads(raw):
    """json.loads without NaN / Infinity / 1e999 (they can't be sent back out)."""
    return json.loads(raw, parse_constant=_no_constant, parse_float=_finite)


def load_object(path, max_bytes=None):
    try:
        if max_bytes is not None and os.path.getsize(path) > max_bytes:
            return None
        with open(path, encoding="utf-8") as f:
            d = loads(f.read())
    except (OSError, ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def write_json(path, data, indent=1):
    """Replace `path` with `data` as JSON; raises OSError if both attempts fail."""
    body = json.dumps(data, indent=indent, allow_nan=False).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    err = OSError(f"could not write {path}")
    for attempt in (0, 1):
        tmp = _tmp_name(path)
        try:
            fd = os.open(tmp, flags, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(body)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            _fsync_dir(os.path.dirname(os.path.abspath(path)))
            return
        except OSError as e:  # Windows: replace fails while another process reads it
            err = e
            if attempt == 0:
                time.sleep(0.05)
    raise err


def _tmp_name(path):
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"


def _fsync_dir(d):
    if os.name != "posix":
        return
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
