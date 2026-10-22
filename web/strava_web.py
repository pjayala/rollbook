# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Sync new activities straight from strava.com, with the browser session cookie.

Strava's API is subscriber-only, so this uses what the website itself uses:

  GET /athlete/training_activities?page=N   JSON list behind "My Activities"
  GET /activities/<id>/export_original      the file as the device recorded it

authenticated by the `_strava4_session` cookie copied once from a logged-in
browser (`rollbook strava auth`). No password is ever handled. The cookie is
stored in data/strava_cookie (0600) and only ever sent to the Strava host:
it is attached with add_unredirected_header and re-attached on a redirect
only when the redirect stays on that host.

Each new activity is stored as data/imports/activities/strava-<id>.<ext> with
a data/imports/activities.csv row carrying Strava's id, name and sport, so the
normal `db build data/imports` gives it its Strava id (a later full export of
the same activity then replaces it, with Strava's summary columns) and the
right sport (HUAWEI labels skating "cycling").

Both endpoints are undocumented: everything that reads their responses is in
parse_list() / sniff_ext(), covered by tests on canned responses. This is
automated access to the website, which Strava's terms don't allow; requests
are spaced (DELAY), 429s back off, and paging stops at the first page with
nothing new.
"""

import csv
import gzip
import io
import json
import os
import re
import sqlite3
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.strava.com"
# tests point this at a local fake; never anywhere else, the cookie goes there
_b = os.environ.get("ROLLBOOK_STRAVA_BASE", "")
if re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+", _b):
    BASE = _b
COOKIE_NAME = "_strava4_session"
DELAY = 1.5  # seconds between requests
MAX_FILE = 64 * 1024 ** 2
UA = "Rollbook (personal training log; export_original sync)"
CSV_HEAD = ["Activity ID", "Activity Date", "Activity Name", "Activity Type", "Filename"]

# exit codes of `rollbook strava sync`, read by web/importer.py
EXIT_AUTH = 3
EXIT_NET = 4


class SyncError(Exception):
    pass


class AuthExpired(SyncError):
    """No cookie, or Strava no longer accepts it."""


class FormatChanged(SyncError):
    """A response that doesn't look like what this module knows."""


# ------------------------------------------------------------------ cookie


def cookie_path(home):
    return os.path.join(home, "strava_cookie")


def state_path(home):
    return os.path.join(home, "strava_sync.json")


def parse_cookie(text):
    """The session value from what the user pasted: the bare value, `name=value`,
    or a whole `Cookie:` header copied from the browser's dev tools."""
    t = (text or "").strip()
    t = re.sub(r"^cookie:\s*", "", t, flags=re.I)
    m = re.search(re.escape(COOKIE_NAME) + r"=([^;\s]+)", t)
    v = m.group(1) if m else t
    if not re.fullmatch(r"[A-Za-z0-9%._~+/=-]{16,4096}", v):
        raise ValueError(f"that doesn't look like a {COOKIE_NAME} value")
    return v


def save_cookie(home, value):
    os.makedirs(home, exist_ok=True)
    p = cookie_path(home)
    fd = os.open(p + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    os.replace(p + ".part", p)
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load_cookie(home):
    try:
        with open(cookie_path(home)) as f:
            v = f.read().strip()
    except FileNotFoundError:
        return None
    return v or None


def forget_cookie(home):
    try:
        os.remove(cookie_path(home))
        return True
    except FileNotFoundError:
        return False


def load_state(home):
    try:
        with open(state_path(home)) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_state(home, s):
    p = state_path(home)
    with open(p + ".part", "w") as f:
        json.dump(s, f, indent=1)
    os.replace(p + ".part", p)


def status(home):
    """For the dashboard: never includes the cookie."""
    s = load_state(home)
    return {"configured": load_cookie(home) is not None, "last_sync": s.get("last_sync"),
            "last_result": s.get("last_result"), "auth_expired": bool(s.get("auth_expired"))}


# ------------------------------------------------------------------ HTTP


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin, cookie):
        self.origin, self.cookie = origin, cookie

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) == self.origin:
            new.add_unredirected_header("Cookie", self.cookie())
        return new


def _origin(url):
    u = urllib.parse.urlsplit(url)
    return (u.scheme, u.netloc.lower())


def _set_cookie_value(headers):
    for h in headers.get_all("Set-Cookie") or []:
        m = re.match(re.escape(COOKIE_NAME) + r"=([^;]*)", h.strip())
        if m:
            return m.group(1)
    return None


class Client:
    def __init__(self, session, base=BASE, delay=DELAY, timeout=60, sleep=time.sleep):
        self.session, self.base, self.delay, self.timeout, self.sleep = session, base.rstrip("/"), delay, timeout, sleep
        self.rotated = False  # Strava sent a new session value: persist it
        self._last = 0.0
        self.origin = _origin(self.base)
        self.opener = urllib.request.build_opener(_Redirect(self.origin, self._cookie))

    def _cookie(self):
        return f"{COOKIE_NAME}={self.session}"

    def _get(self, path, accept, xhr=False):
        if not self.session:
            raise AuthExpired("no Strava session")
        for attempt in range(5):
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(self.base + path, headers={"User-Agent": UA, "Accept": accept})
            if xhr:
                req.add_header("X-Requested-With", "XMLHttpRequest")
            req.add_unredirected_header("Cookie", self._cookie())
            try:
                r = self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                code, hdrs = e.code, e.headers
                if e.fp is not None:
                    e.close()
                if code in (401, 403):
                    raise AuthExpired(f"Strava answered {code}")
                if code == 429 or code >= 500:
                    if attempt == 4:
                        raise SyncError(f"Strava answered {code} repeatedly, try again later")
                    ra = hdrs.get("Retry-After") if hdrs else None
                    self.sleep(min(900, int(ra) if ra and ra.isdigit() else 30 * 2 ** attempt))
                    continue
                if code == 404:
                    return None, None, None
                raise SyncError(f"{path}: HTTP {code}")
            except (urllib.error.URLError, OSError) as e:
                raise SyncError(f"can't reach Strava: {getattr(e, 'reason', e)}")
            with r:
                final = r.geturl()
                body = r.read(MAX_FILE + 1)
                if len(body) > MAX_FILE:
                    raise SyncError(f"{path}: response larger than {MAX_FILE >> 20} MB")
                v = _set_cookie_value(r.headers)
                if v and v != self.session and v.lower() not in ("", "deleted"):
                    self.session, self.rotated = v, True
                if _origin(final) == self.origin and urllib.parse.urlsplit(final).path.rstrip("/") in (
                        "/login", "/session", "/oauth/authorize", "/register"):
                    raise AuthExpired("Strava redirected to its login page")
                return body, r.headers, final
        raise SyncError("unreachable")

    def list_page(self, page, per_page=20):
        body, _, _ = self._get(f"/athlete/training_activities?page={page}&per_page={per_page}",
                               "application/json, text/javascript, */*; q=0.01", xhr=True)
        if body is None:
            raise FormatChanged("activity list endpoint not found (404)")
        return parse_list(body)

    def download(self, aid):
        """(bytes, file name from Content-Disposition) or (None, None) when Strava
        has no original file (manual entries)."""
        body, headers, _ = self._get(f"/activities/{int(aid)}/export_original", "*/*")
        if body is None or headers is None:
            return None, None
        cd = headers.get("Content-Disposition") or ""
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, flags=re.I)
        return body, urllib.parse.unquote(m.group(1)) if m else None


# ------------------------------------------------------------------ parsing


def _type_name(m):
    for k in ("display_type", "activity_type_display_name", "sport_type_display_name"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    v = m.get("sport_type") or m.get("type") or ""
    # "InlineSkate" -> "Inline Skate", as activities.csv spells it
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(v)).strip()


def _start(m):
    """ISO date-time string of the start (UTC or local, whatever is there), or None."""
    for k in ("start_time", "start_date_local", "start_date"):
        v = m.get(k)
        if isinstance(v, str) and re.match(r"\d{4}-\d\d-\d\d", v):
            return v
    v = m.get("start_date_local_raw")
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(v, timezone.utc).isoformat()
    return None


def parse_list(body):
    """-> (activities, total or None). activities: [{id, name, type, start}]."""
    try:
        obj = json.loads(body)
    except ValueError:
        if body.lstrip()[:1] == b"<":  # an HTML page instead of JSON: not logged in
            raise AuthExpired("Strava sent a web page instead of the activity list")
        raise FormatChanged("activity list is not JSON")
    models = obj.get("models") if isinstance(obj, dict) else obj
    if not isinstance(models, list):
        raise FormatChanged("activity list has no 'models'")
    out = []
    for m in models:
        if not isinstance(m, dict) or not str(m.get("id", "")).isdigit():
            raise FormatChanged("activity without a numeric id")
        out.append({"id": int(m["id"]), "name": str(m.get("name") or "").strip()[:200],
                    "type": _type_name(m)[:40], "start": _start(m)})
    total = obj.get("total") if isinstance(obj, dict) else None
    return out, total if isinstance(total, int) else None


def sniff_ext(data):
    """.fit / .gpx / .tcx (+ .gz) from the content, None if it isn't an activity file."""
    gz = data[:2] == b"\x1f\x8b"
    head = data
    if gz:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
                head = g.read(8192)
        except (OSError, EOFError):
            return None
    head = head[:8192]
    low = head.lower()
    if len(head) >= 12 and head[8:12] == b".FIT":
        ext = ".fit"
    elif b"<gpx" in low:
        ext = ".gpx"
    elif b"trainingcenterdatabase" in low:
        ext = ".tcx"
    else:
        return None
    return ext + (".gz" if gz else "")


# ------------------------------------------------------------------ sync


def db_ids(db_path):
    """Every activity id in the raw DB (hidden / deleted sessions included)."""
    if not os.path.exists(db_path):
        return set()
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {r[0] for r in db.execute("SELECT id FROM activities")}
    except sqlite3.Error:
        return set()
    finally:
        db.close()


def stored_ids(imports):
    acts = os.path.join(imports, "activities")
    out = set()
    if os.path.isdir(acts):
        for n in os.listdir(acts):
            m = re.match(r"strava-(\d+)\.", n)
            if m:
                out.add(int(m.group(1)))
    return out


def _append_row(imports, row):
    index = os.path.join(imports, "activities.csv")
    new = not os.path.exists(index)
    with open(index, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(CSV_HEAD)
        w.writerow(row)


def find_new(client, known, since=None, all_pages=False, max_new=None, log=print):
    """Walk the list newest first. Without all_pages, stop at the first page with
    nothing new (or past `since`, a YYYY-MM-DD). Returns new activities, oldest first."""
    new, page, seen = [], 1, 0
    while True:
        acts, total = client.list_page(page)
        seen += len(acts)
        log(f"page {page}: {len(acts)} activities")
        if not acts:
            break
        fresh = [a for a in acts if a["id"] not in known and a["id"] not in {x["id"] for x in new}
                 and not (since and a["start"] and a["start"][:10] < since)]
        new += fresh
        older = since and any(a["start"] and a["start"][:10] < since for a in acts)
        # max_new doesn't stop the walk: the oldest new ones are downloaded first,
        # so a capped run leaves the newest for the next one, whose page 1 has them
        if older or (not fresh and not all_pages):
            break
        if total is not None and seen >= total:
            break
        page += 1
    new.reverse()
    if max_new and len(new) > max_new:
        new = new[:max_new]  # the oldest ones: the next run continues where this one stopped
    return new


def sync(home, db_path, client, since=None, all_pages=False, max_new=200, dry_run=False, log=print):
    """Download what's new into data/imports. Returns a summary dict."""
    imports = os.path.join(home, "imports")
    acts_dir = os.path.join(imports, "activities")
    st = load_state(home)
    no_file = set(st.get("no_file") or [])
    known = db_ids(db_path) | stored_ids(imports) | no_file
    result = {"new": 0, "downloaded": 0, "no_file": 0, "dry_run": dry_run}
    try:
        new = find_new(client, known, since, all_pages, max_new, log)
        st.pop("auth_expired", None)
        result["new"] = len(new)
        log(f"{len(new)} new on Strava" + (" (dry run, nothing downloaded)" if dry_run and new else ""))
        if dry_run:
            for a in new:
                log(f"  {a['start'] or '?'}  {a['type']:<14} {a['name']}  (id {a['id']})")
            return result
        os.makedirs(acts_dir, exist_ok=True)
        for k, a in enumerate(new, 1):
            data, fname = client.download(a["id"])
            ext = sniff_ext(data) if data else None
            label = f"{(a['start'] or '')[:10]} {a['type']} {a['name']}"
            if not ext:
                # 404, or the activity's web page: a manual entry has no original file
                log(f"  {k}/{len(new)}  {label}: no original file on Strava (manual entry?), skipped")
                no_file.add(a["id"])
                result["no_file"] += 1
                continue
            name = f"strava-{a['id']}{ext}"
            part = os.path.join(acts_dir, f".{name}.part")
            with open(part, "wb") as f:
                f.write(data)
            # the CSV row first: a file without its row would be built with a
            # path-hash id and the device's sport
            _append_row(imports, [str(a["id"]), a["start"] or "", a["name"], a["type"], f"activities/{name}"])
            os.replace(part, os.path.join(acts_dir, name))
            result["downloaded"] += 1
            log(f"  {k}/{len(new)}  {label}  ({fname or name}, {len(data) // 1024} kB)")
        return result
    except AuthExpired:
        st["auth_expired"] = True
        raise
    finally:
        if client.rotated:
            save_cookie(home, client.session)
        if not dry_run:
            st["no_file"] = sorted(no_file)
            st["last_sync"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            st["last_result"] = result
        save_state(home, st)
