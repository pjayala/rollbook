# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Sync sessions from intervals.icu, which the HUAWEI watch uploads to even
when Strava refuses one. Official API, personal API key:

  GET /api/v1/athlete/0                          the key's athlete (auth check)
  GET /api/v1/athlete/0/activities?oldest&newest  list, one answer per range
  GET /api/v1/activity/<id>/file                 the session as a FIT file

Basic auth `API_KEY:<key>` (intervals.icu Settings -> Developer). The key is
stored in data/intervals_key (0600), sent only to the intervals.icu host
(unredirected header, re-added on a redirect only when it stays there), and
the dashboard process never reads it (has_key only stats the file).

Strava stays the preferred source: its copy is the file the watch recorded,
while intervals.icu rebuilds HUAWEI sessions (its distance channel is the
total spread evenly over the time; `db build` falls back to GPS). So a sync
downloads only sessions Rollbook doesn't have yet: no id, Strava id, stored
file or session within 2 min / 5 % distance (the build's own dedup rule).
If Strava's copy turns up later, the build replaces the intervals.icu one.

HUAWEI labels skating "Ride". The sport of a new session is the user's
mapping (--sport Ride="Inline Skate", kept in data/intervals_sync.json), else
what the overlap teaches: the sport Rollbook has for the sessions that are in
both places (>= 3 of them, >= 60 % agreeing), else intervals.icu's type.
"""

import base64
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import edits
import store
from strava_web import CSV_HEAD, _append_row, sniff_ext  # noqa: F401  one index format for every source

BASE = "https://intervals.icu"
_b = os.environ.get("ROLLBOOK_INTERVALS_BASE", "")  # tests: a local fake, never anywhere else
if re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+", _b):
    BASE = _b
DELAY = 0.5  # seconds between requests
MAX_FILE = 64 * 1024 ** 2
MAX_LIST = 32 * 1024 ** 2
UA = "Rollbook (personal training log; intervals.icu sync)"
# Rollbook id of intervals.icu activity i<n>: far above Strava's ids (~2e10)
# and path-hash ids (< 1e12), 15 digits like api.ACTIVITY_ID allows
ID_BASE = 9 * 10 ** 14
FILE_RE = re.compile(r"intervals-(\d{1,12})\.")
LEARN_MIN, LEARN_SHARE = 3, 0.6
RETRY_AFTER_MAX = 120  # seconds; a longer ask ends the run sooner, see Client._get
RUN_BUDGET_S = 30 * 60  # one sync run, wall clock: the dashboard job waits on it
STATE_V = 1
CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

EXIT_AUTH = 3
EXIT_NET = 4
EXIT_PARTIAL = 5  # cut short by the time budget: what it stored is kept, the next run continues


class SyncError(Exception):
    pass


class AuthExpired(SyncError):
    """No key, or intervals.icu no longer accepts it."""


class FormatChanged(SyncError):
    pass


# ------------------------------------------------------------------ key / state


def key_path(home):
    return os.path.join(home, "intervals_key")


def state_path(home):
    return os.path.join(home, "intervals_sync.json")


def parse_key(text):
    """The key from what was pasted: the bare key or `API_KEY:<key>`."""
    t = re.sub(r"^\s*API_KEY\s*:", "", (text or "").strip())
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", t.strip()):
        raise ValueError("that doesn't look like an intervals.icu API key")
    return t.strip()


def save_key(home, value):
    os.makedirs(home, exist_ok=True)
    store.write_bytes(key_path(home), (value + "\n").encode("ascii"))


def has_key(home):
    try:
        return os.path.getsize(key_path(home)) > 0
    except OSError:
        return False


def load_key(home):
    try:
        with open(key_path(home), encoding="ascii", errors="replace") as f:
            v = f.read().strip()
    except FileNotFoundError:
        return None
    return v or None


def forget_key(home):
    try:
        os.remove(key_path(home))
        return True
    except FileNotFoundError:
        return False


STATE_MAX = 4 << 20


def _sport_map(v):
    if not isinstance(v, dict):
        return {}
    return {k: s for k, s in v.items() if isinstance(k, str) and isinstance(s, str)
            and 0 < len(k) <= 40 and 0 < len(s) <= 40}


def load_state(home):
    """data/intervals_sync.json, untrusted: every field type-checked."""
    s = store.load_object(state_path(home), STATE_MAX) or {}
    if s.get("v", STATE_V) != STATE_V:
        s = {}
    s["v"] = STATE_V
    nf = s.get("no_file")
    s["no_file"] = sorted({x for x in nf if isinstance(x, int) and not isinstance(x, bool) and 0 < x < 10 ** 12}
                          if isinstance(nf, list) else set())
    s["sport_map"], s["learned"] = _sport_map(s.get("sport_map")), _sport_map(s.get("learned"))
    return s


def save_state(home, s):
    store.write_json(state_path(home), s)


def status(home):
    """For the dashboard: never the key, and never read it."""
    s = load_state(home)
    last = s.get("last_sync")
    res = s.get("last_result")
    res = {k: v for k, v in res.items() if isinstance(k, str) and isinstance(v, (int, float, str, bool)) and
           not (isinstance(v, str) and len(v) > 200)} if isinstance(res, dict) else None
    return {"configured": has_key(home), "last_sync": last if isinstance(last, str) and len(last) <= 40 else None,
            "last_result": res, "auth_expired": s.get("auth_expired") is True,
            "sports": {**s["learned"], **s["sport_map"]}}


# ------------------------------------------------------------------ HTTP


def _origin(url):
    u = urllib.parse.urlsplit(url)
    return (u.scheme, u.netloc.lower())


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin, auth):
        self.origin, self.auth = origin, auth

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) == self.origin:
            new.add_unredirected_header("Authorization", self.auth)
        return new


class Client:
    def __init__(self, key, base=BASE, delay=DELAY, timeout=60, sleep=time.sleep):
        self.key, self.base, self.delay, self.timeout, self.sleep = key, base.rstrip("/"), delay, timeout, sleep
        self._last = 0.0
        self.origin = _origin(self.base)
        self.auth = "Basic " + base64.b64encode(f"API_KEY:{key or ''}".encode()).decode()
        self.opener = urllib.request.build_opener(_Redirect(self.origin, self.auth))

    def _get(self, path, cap, missing_ok=False):
        if not self.key:
            raise AuthExpired("no intervals.icu API key")
        for attempt in range(5):
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(self.base + path, headers={"User-Agent": UA, "Accept": "*/*"})
            req.add_unredirected_header("Authorization", self.auth)
            try:
                r = self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                code, hdrs = e.code, e.headers
                if e.fp is not None:
                    e.close()
                if code in (401, 403):
                    raise AuthExpired(f"intervals.icu answered {code}")
                if code == 429 or code >= 500:
                    if attempt == 4:
                        raise SyncError(f"intervals.icu answered {code} repeatedly, try again later")
                    self.sleep(retry_after(hdrs, attempt))
                    continue
                if missing_ok and code in (404, 410, 422):
                    return None
                raise SyncError(f"{path.split('?')[0]}: HTTP {code}")
            except (urllib.error.URLError, OSError) as e:
                raise SyncError(f"can't reach intervals.icu: {getattr(e, 'reason', e)}")
            with r:
                body = r.read(cap + 1)
            if len(body) > cap:
                raise SyncError(f"{path.split('?')[0]}: response larger than {cap >> 20} MB")
            return body
        raise SyncError("unreachable")

    def athlete(self):
        """{id, name} of the key's athlete."""
        obj = _json(self._get("/api/v1/athlete/0", 1 << 20))
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
            raise FormatChanged("athlete answer has no id")
        return {"id": clean_text(obj["id"], 40), "name": clean_text(obj.get("name"), 80)}

    def activities(self, oldest, newest):
        q = urllib.parse.urlencode({"oldest": oldest, "newest": newest})
        return parse_list(self._get(f"/api/v1/athlete/0/activities?{q}", MAX_LIST))

    def download(self, num):
        """FIT bytes of activity i<num>, or None if it has no file."""
        return self._get(f"/api/v1/activity/i{int(num)}/file", MAX_FILE, missing_ok=True)


def retry_after(hdrs, attempt):
    """Seconds to wait after a 429 / 5xx: the server's Retry-After (ASCII digits
    only; "²".isdigit() is True) capped at RETRY_AFTER_MAX, else 30 s doubling."""
    ra = (hdrs.get("Retry-After") or "").strip() if hdrs else ""
    if re.fullmatch(r"[0-9]{1,6}", ra):
        return min(RETRY_AFTER_MAX, int(ra))
    return min(RETRY_AFTER_MAX, 30 * 2 ** attempt)


def _json(body):
    try:
        return store.loads(body)
    except (ValueError, RecursionError):  # garbage, or nesting deep enough to blow the parser's stack
        raise FormatChanged("answer is not JSON")


def clean_text(v, n):
    """An upstream string for logs, the CSV and the dashboard: no control
    characters (terminal escapes, fake log lines), trimmed, at most n chars."""
    return CTRL.sub(" ", v).strip()[:n] if isinstance(v, str) else ""


# ------------------------------------------------------------------ parsing


def _utc(v):
    """Aware UTC datetime from an ISO string with an offset or Z; None otherwise
    (start_date_local has no offset and can't be placed in time)."""
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:?\d\d)", v):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
    return dt if 1990 <= dt.year <= 2100 else None  # day math around it stays in range


def _num(v, lo, hi):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else None


def parse_list(body):
    """-> [{num, name, type, start (aware UTC or None), km, source, strava_id}]."""
    obj = _json(body)
    if not isinstance(obj, list):
        raise FormatChanged("activity list is not a list")
    out = []
    for a in obj:
        if not isinstance(a, dict):
            continue
        m = re.fullmatch(r"i(\d{1,12})", str(a.get("id", "")))
        if not m:
            continue  # not an activity of this athlete's own (planned workouts have other ids)
        sid = a.get("strava_id")
        sid = int(sid) if isinstance(sid, (int, str)) and not isinstance(sid, bool) and re.fullmatch(r"\d{1,15}", str(sid)) else None
        dist = _num(a.get("distance"), 0, 5e6)
        out.append({"num": int(m.group(1)), "name": clean_text(a.get("name"), 200), "type": clean_text(a.get("type"), 40),
                    "start": _utc(a.get("start_date")), "km": dist / 1000 if dist is not None else None,
                    "source": clean_text(str(a.get("source") or ""), 20).upper(), "strava_id": sid})
    return out


def type_label(t):
    """'VirtualRide' -> 'Virtual Ride', as activities.csv spells sports."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t).strip()


# ------------------------------------------------------------------ dedup


def db_sessions(db_path, home):
    """[(id, start, km, sport)] of every session Rollbook has: the raw DB,
    hidden and deleted included, with the dashboard's sport edits applied when
    edits.json reads, plus deleted sessions whose row is gone (a deleted
    upload after `db build --fresh`: its edit keeps start / km), so a deleted
    session never comes back from here. No DB yet = []; a DB that is there
    but can't be read raises SyncError (else everything would look new)."""
    if not os.path.exists(db_path):
        return []
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise SyncError(f"can't read the database: {e}")
    E = edits.Edits(home)
    try:
        src = "main.activities"
        if not E.error:
            try:
                E.sync(db)
                src = "activities_all"
            except sqlite3.Error:
                pass
        rows = db.execute(f"SELECT id, datetime, COALESCE(km_calc, km), type FROM {src} "
                          f"WHERE parse_error IS NULL").fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):  # no table = an aborted first build: no rows
            raise SyncError(f"can't read the database: {e}")
        rows = []
    except sqlite3.Error as e:
        raise SyncError(f"can't read the database: {e}")
    finally:
        db.close()
    out = [(aid, _start(dt), km, typ) for aid, dt, km, typ in rows]
    have = {r[0] for r in rows}
    for k, e in (E.data.get("sessions") or {}).items():
        if e.get("deleted") and int(k) not in have:
            out.append((int(k), _start(e.get("start")), e.get("km") if edits._finite(e.get("km")) else None,
                        e.get("type")))
    return out


def _start(v):
    try:
        t = datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None
    if t is not None and t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)  # naive = UTC
    return t


def stored_nums(imports):
    acts = os.path.join(imports, "activities")
    out = set()
    if os.path.isdir(acts):
        for n in os.listdir(acts):
            m = FILE_RE.match(n)
            if m:
                out.add(int(m.group(1)))
    return out


def same_session(a, start, km):
    """intervals.icu activity a vs a session Rollbook has: edits.same_session,
    the rule `db build` dedup and edit / gear re-attachment use."""
    if a["start"] is None or start is None:
        return False
    return edits.same_session(abs((a["start"] - start).total_seconds()), a["km"], km)


def classify(acts, sessions, stored, no_file):
    """Split the list into (new, matched pairs [(activity, sport in Rollbook)],
    counts). A session counts as present by id, by its Strava id, by a stored
    file, or by start + distance (same_session). Items intervals.icu got from
    Strava are left to `rollbook strava sync` (the API won't give their file)."""
    ids = {s[0] for s in sessions}
    by_day = {}  # proleptic ordinal: day +-1 never overflows
    for s in sessions:
        if s[1] is not None:
            by_day.setdefault(s[1].toordinal(), []).append(s)
    new, pairs, n = [], [], Counter()
    for a in acts:
        if ID_BASE + a["num"] in ids or a["num"] in stored:
            n["stored"] += 1
            continue
        if a["num"] in no_file:
            n["no_file"] += 1
            continue
        if a["strava_id"] and a["strava_id"] in ids:
            n["matched"] += 1
            continue
        if a["source"] == "STRAVA":
            n["strava"] += 1
            continue
        hit = None
        if a["start"] is not None:
            d = a["start"].toordinal()
            near = by_day.get(d, []) + by_day.get(d - 1, []) + by_day.get(d + 1, [])
            hit = next((s for s in near if same_session(a, s[1], s[2])), None)
        if hit:
            n["matched"] += 1
            pairs.append((a, hit[3]))
            continue
        new.append(a)
    return new, pairs, n


def learn_sports(pairs):
    """{intervals type: sport} from sessions in both places: >= LEARN_MIN
    pairs and >= LEARN_SHARE of them agreeing."""
    by = {}
    for a, sport in pairs:
        if a["type"] and isinstance(sport, str) and sport:
            by.setdefault(a["type"], Counter())[sport] += 1
    out = {}
    for t, c in by.items():
        sport, k = c.most_common(1)[0]
        if k >= LEARN_MIN and k >= LEARN_SHARE * sum(c.values()):
            out[t] = sport
    return out


def sport_for(a, user, learned):
    t = a["type"]
    return (user.get(t) or learned.get(t) or type_label(t) or "Workout")[:40]


# ------------------------------------------------------------------ sync


def window(today, since=None, all_=False, days=365):
    newest = (today + timedelta(days=1)).isoformat()
    if all_:
        return "1990-01-01", newest
    return since or (today - timedelta(days=days)).isoformat(), newest


def sync(home, db_path, client, since=None, all_=False, max_new=200, dry_run=False, sports=None,
         today=None, now=None, clock=time.monotonic, budget_s=RUN_BUDGET_S, log=print):
    """Download the sessions Rollbook doesn't have into data/imports.
    sports: {intervals type: sport or ""} to remember ("" forgets one).
    Stops after budget_s (result "cut_short"); what it stored stays and the
    next run continues, oldest first."""
    imports = os.path.join(home, "imports")
    acts_dir = os.path.join(imports, "activities")
    st = load_state(home)
    for t, s in (sports or {}).items():
        if s:
            st["sport_map"][t] = s
        else:
            st["sport_map"].pop(t, None)
    no_file = set(st["no_file"])
    result = {"new": 0, "downloaded": 0, "no_file": 0, "matched": 0, "skipped": 0, "dry_run": dry_run}
    deadline = clock() + budget_s
    try:
        oldest, newest = window(today or date.today(), since, all_)
        acts = client.activities(oldest, newest)
        st.pop("auth_expired", None)
        new, pairs, n = classify(acts, db_sessions(db_path, home), stored_nums(imports), no_file)
        learned = learn_sports(pairs)
        if learned:
            st["learned"] = {**st["learned"], **learned}
        result.update(new=len(new), matched=n["matched"] + n["stored"])
        new.sort(key=lambda a: (a["start"] or datetime.max.replace(tzinfo=timezone.utc), a["num"]))
        if max_new and len(new) > max_new:
            new = new[:max_new]  # oldest first: the next run continues
        log(f"{len(acts)} on intervals.icu since {oldest}: {result['new']} new, "
            f"{result['matched']} already in Rollbook"
            + (f", {n['strava']} from Strava (left to `rollbook strava sync`)" if n["strava"] else ""))
        for t, s in sorted({**st["learned"], **st["sport_map"]}.items()):
            log(f"  sport: intervals.icu {t} -> {s}" + ("" if t in st["sport_map"] else " (learned from overlap)"))
        if dry_run:
            for a in new:
                log(f"  {a['start'].isoformat() if a['start'] else '?'}  {sport_for(a, st['sport_map'], st['learned']):<14}"
                    f" {a['name']}  ({a['km'] or 0:.2f} km, i{a['num']})")
            return result
        os.makedirs(acts_dir, exist_ok=True)
        for k, a in enumerate(new, 1):
            if clock() > deadline:
                result["cut_short"] = True
                log(f"time budget used after {k - 1} of {len(new)}: the next run continues")
                break
            data = client.download(a["num"])
            sport = sport_for(a, st["sport_map"], st["learned"])
            label = f"{a['start'].date().isoformat() if a['start'] else ''} {sport} {a['name']}"
            if data is None:  # 404 / 410 / 422: there is no file, for good
                log(f"  {k}/{len(new)}  {label}: no activity file on intervals.icu, skipped")
                no_file.add(a["num"])
                result["no_file"] += 1
                continue
            ext = sniff_ext(data)
            if not ext:  # a 200 that isn't a track (a maintenance page?): try again next run
                log(f"  {k}/{len(new)}  {label}: intervals.icu sent something that isn't an activity file, "
                    f"skipped this time")
                result["skipped"] += 1
                continue
            name = f"intervals-{a['num']}{ext}"
            # the CSV row first: a file without its row would be built with a
            # path-hash id and the device's sport; a row without its file is ignored
            _append_row(imports, [str(ID_BASE + a["num"]), a["start"].isoformat() if a["start"] else "",
                                  a["name"], sport, f"activities/{name}"])
            store.write_bytes(os.path.join(acts_dir, name), data)  # unique O_EXCL|O_NOFOLLOW temp, 0600
            result["downloaded"] += 1
            log(f"  {k}/{len(new)}  {label}  ({len(data) // 1024} kB)")
        return result
    except AuthExpired:
        st["auth_expired"] = True
        raise
    finally:
        if not dry_run:
            st["no_file"] = sorted(no_file)
            st["last_sync"] = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
            st["last_result"] = result
        try:
            save_state(home, st)
        except OSError as e:  # never hide the run's own error behind this one
            log(f"sync state not saved: {e}")
