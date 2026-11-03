# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Gear (issue #10): parts, the setups they make, and which session used what.

Stored in $ROLLBOOK_HOME/gear.json (the DB is read-only and rebuilt from
exports, so it lives beside it like edits.json):

  parts     {id: {kind, name, brand, specs, bought, km_offset, service_km,
                  services: [{date, action, note}], retired, notes}}
  gear      {id: {name, sports, parts: [{part, from, to}], retired, notes}}
            a part counts for a session on day D when from <= D < to (to null:
            still on); one part is on at most one gear at a time
  defaults  {sport: [{gear | null, since}]}: a session of that sport with no
            choice of its own gets the gear of the latest `since` <= its date,
            unless that gear was retired by then
  sessions  {activity id: {gear | null, start, km}}: explicit choices (null =
            none); start / km re-attach a choice when an import replaces the
            session under another id, as edits do (edits.reattach)

The file is validated as a whole on load and before every write. A file that
fails is never overwritten: the store goes read-only (writes 503) until it is
fixed or moved away. Every write keeps the previous version as gear.json.bak
and needs the client's `rev`, so a stale tab gets 409 instead of undoing
another tab's change. Ids are made here (16 hex digits).
"""

import bisect
import copy
import json
import os
import re
import secrets
import stat
import sys
import threading
import unicodedata
from datetime import date as _date

import edits
import store

VERSION = 1
MAX_FILE = 2 << 20
MAX_PARTS, MAX_GEAR, MAX_MOUNTS, MAX_SERVICES, MAX_SERVICES_ALL = 500, 200, 50, 200, 5000
MAX_SESSIONS, MAX_ASSIGN, MAX_SPORTS, MAX_DEFAULTS = 100_000, 2000, 50, 200
NAME_MAX, NOTE_MAX, SERVICE_NOTE_MAX = 120, 2000, 500
ID = re.compile(r"[0-9a-f]{16}")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
BAD_KEYS = {"__proto__", "constructor", "prototype"}
ACTIONS = ("service", "rotate", "replace")

# kind: (label, group, specs). spec: (key, label, "num"|"int", unit, min, max)
# | (key, label, "text", max length) | (key, label, "choice", options)
KINDS = {
    "boots": ("Boots", "skate", [("size_eu", "Size (EU)", "num", None, 20, 52), ("model", "Model", "text", 60),
                                 ("shell", "Shell", "choice", ("hard", "soft", "carbon"))]),
    "frame": ("Frame", "skate", [("length_mm", "Length", "num", "mm", 150, 420),
                                 ("material", "Material", "choice",
                                  ("aluminium", "magnesium", "carbon", "composite", "plastic")),
                                 ("setup", "Setup", "text", 16),
                                 ("mount", "Mount", "choice", ("165 mm", "195 mm", "UFS", "Trinity", "other"))]),
    "wheels": ("Wheels", "skate", [("diameter_mm", "Diameter", "num", "mm", 40, 130),
                                   ("hardness_a", "Hardness", "num", "A", 70, 100),
                                   ("count", "Count", "int", None, 1, 12), ("profile", "Profile", "text", 30)]),
    "bearings": ("Bearings", "skate", [("size", "Size", "choice", ("608", "688", "other")),
                                       ("rating", "Rating", "text", 30), ("count", "Count", "int", None, 1, 24)]),
    "bike": ("Bike", "bike", [("type", "Type", "choice", ("road", "gravel", "mtb", "city", "trainer", "other")),
                              ("size", "Size", "text", 16)]),
    "tyre": ("Tyre", "bike", [("width_mm", "Width", "num", "mm", 18, 80), ("size", "Size", "text", 16),
                              ("position", "Position", "choice", ("front", "rear", "both"))]),
    "chain": ("Chain", "bike", [("speeds", "Speeds", "int", None, 1, 13)]),
    "cassette": ("Cassette", "bike", [("speeds", "Speeds", "int", None, 1, 13), ("range", "Range", "text", 16)]),
    "chainring": ("Chainring", "bike", [("teeth", "Teeth", "text", 16)]),
    "brake_pads": ("Brake pads", "bike", [("type", "Type", "text", 30)]),
    "shoes": ("Shoes", "run", [("size_eu", "Size (EU)", "num", None, 20, 52),
                               ("drop_mm", "Drop", "num", "mm", 0, 20)]),
    "other": ("Other", "other", []),
}
# which kinds the page offers first for a sport; anything else is "other"
SPORT_GROUP = {"Inline Skate": "skate", "Roller Ski": "skate", "Ride": "bike", "Virtual Ride": "bike",
               "E-Bike Ride": "bike", "Mountain Bike Ride": "bike", "Gravel Ride": "bike",
               "Run": "run", "Trail Run": "run", "Virtual Run": "run", "Walk": "run", "Hike": "run"}
NO_TRAINER = {"tyre"}  # a trainer session doesn't wear the tyre: it doesn't count for it at all
RECENT = 15  # sessions per gear listed on its page


def wears(kind, sport):
    return not (kind in NO_TRAINER and (sport or "").startswith("Virtual"))


class Conflict(Exception):
    """409: the store changed since the client read it, or a reference blocks the change."""


class Broken(Exception):
    """503: gear.json could not be loaded; nothing is written until it is fixed."""


class NotFound(LookupError):
    """404: no such gear, part, mount, service or session."""


# ------------------------------------------------------------------ validation

def _txt(v, limit, what, required=False):
    if isinstance(v, str):  # normalise first: NFC can lengthen a string, the limit is on what is stored
        v = unicodedata.normalize("NFC", v)
    v = edits._text(v, limit, what)
    if any(unicodedata.category(c) == "Cf" for c in v):  # bidi overrides, zero-width
        raise ValueError(f"{what} has invisible formatting characters")
    if required and not v:
        raise ValueError(f"{what} is required")
    return v


def _day(v, what, optional=True):
    if v is None or v == "":
        if optional:
            return None
        raise ValueError(f"{what} is required")
    if not isinstance(v, str) or not DATE.fullmatch(v):
        raise ValueError(f"{what} must be a date YYYY-MM-DD")
    try:
        _date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{what} is not a real date") from None
    return v


def _num(v, lo, hi, what, integer=False, optional=True):
    if v is None or v == "":
        if optional:
            return None
        raise ValueError(f"{what} is required")
    if isinstance(v, bool) or type(v) not in (int, float) or not lo <= v <= hi:  # NaN fails the range
        raise ValueError(f"{what} must be a number from {lo} to {hi}")
    if integer and v != int(v):
        raise ValueError(f"{what} must be a whole number")
    return int(v) if integer or v == int(v) else v


def _sport(v):
    v = _txt(v, edits.TYPE_MAX, "sport", True)
    if v in BAD_KEYS:
        raise ValueError("not a sport name")
    return v


def _id(v, what):
    if not isinstance(v, str) or not ID.fullmatch(v):
        raise ValueError(f"bad {what} id")
    return v


def _specs(kind, specs):
    if specs is None:
        return {}
    if not isinstance(specs, dict):
        raise ValueError("specs must be an object")
    known = {s[0]: s for s in KINDS[kind][2]}
    out = {}
    for k, v in specs.items():
        if k not in known:
            raise ValueError(f"{KINDS[kind][0].lower()} have no '{k}'")
        s = known[k]
        if v is None or v == "":
            continue
        if s[2] in ("num", "int"):
            out[k] = _num(v, s[4], s[5], s[1], s[2] == "int")
        elif s[2] == "text":
            v = _txt(v, s[3], s[1])
            if v:
                out[k] = v
        elif v in s[3]:
            out[k] = v
        else:
            raise ValueError(f"{s[1]} must be one of {', '.join(s[3])}")
    return out


def _part(p):
    if not isinstance(p, dict):
        raise ValueError("a part must be an object")
    kind = p.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("unknown part kind")
    services = p.get("services") or []
    if not isinstance(services, list) or len(services) > MAX_SERVICES:
        raise ValueError(f"at most {MAX_SERVICES} services per part")
    sv = []
    for s in services:
        if not isinstance(s, dict) or s.get("action") not in ACTIONS:
            raise ValueError(f"a service is one of {', '.join(ACTIONS)}")
        sv.append({"date": _day(s.get("date"), "service date", False), "action": s["action"],
                   "note": _txt(s.get("note"), SERVICE_NOTE_MAX, "note")})
    sv.sort(key=lambda s: s["date"])
    return {"kind": kind, "name": _txt(p.get("name"), NAME_MAX, "name", True),
            "brand": _txt(p.get("brand"), NAME_MAX, "brand"), "specs": _specs(kind, p.get("specs")),
            "bought": _day(p.get("bought"), "bought"),
            "km_offset": _num(p.get("km_offset"), 0, 1e6, "km already on it") or 0,
            "service_km": _num(p.get("service_km"), 1, 1e5, "service interval"),
            "services": sv, "retired": _day(p.get("retired"), "retired"),
            "notes": _txt(p.get("notes"), NOTE_MAX, "notes")}


def _gear(g, parts):
    if not isinstance(g, dict):
        raise ValueError("a gear must be an object")
    sports = g.get("sports") or []
    if not isinstance(sports, list) or len(sports) > 10:
        raise ValueError("at most 10 sports per gear")
    mounts = g.get("parts") or []
    if not isinstance(mounts, list) or len(mounts) > MAX_MOUNTS:
        raise ValueError(f"at most {MAX_MOUNTS} parts per gear")
    ms = []
    for m in mounts:
        if not isinstance(m, dict):
            raise ValueError("bad part on gear")
        pid = _id(m.get("part"), "part")
        if pid not in parts:
            raise ValueError("gear holds a part that doesn't exist")
        f, t = _day(m.get("from"), "installed", False), _day(m.get("to"), "removed")
        if t is not None and t <= f:
            raise ValueError("a part must be removed after it was installed")
        ms.append({"part": pid, "from": f, "to": t})
    ms.sort(key=lambda m: m["from"])
    return {"name": _txt(g.get("name"), NAME_MAX, "name", True),
            "sports": list(dict.fromkeys(_sport(s) for s in sports)), "parts": ms,
            "retired": _day(g.get("retired"), "retired"), "notes": _txt(g.get("notes"), NOTE_MAX, "notes")}


def validate(d):
    """A clean copy of a whole store; ValueError says what is wrong."""
    if not isinstance(d, dict) or d.get("v") != VERSION:
        raise ValueError(f"not a version {VERSION} gear file")
    rev = d.get("rev", 0)
    if type(rev) is not int or rev < 0:
        raise ValueError("bad rev")
    raw = {k: d.get(k) or {} for k in ("parts", "gear", "defaults", "sessions")}
    if not all(isinstance(v, dict) for v in raw.values()):
        raise ValueError("parts, gear, defaults and sessions must be objects")
    if len(raw["parts"]) > MAX_PARTS or len(raw["gear"]) > MAX_GEAR:
        raise ValueError(f"at most {MAX_PARTS} parts and {MAX_GEAR} gear")
    parts = {_id(k, "part"): _part(p) for k, p in raw["parts"].items()}
    gear = {_id(k, "gear"): _gear(g, parts) for k, g in raw["gear"].items()}
    on = {}  # part -> [(from, to)] over all gear: one place at a time
    for g in gear.values():
        for m in g["parts"]:
            on.setdefault(m["part"], []).append((m["from"], m["to"]))
    for pid, iv in on.items():
        iv.sort(key=lambda x: (x[0], x[1] or "9999-12-31"))
        for (_, t0), (f1, _) in zip(iv, iv[1:]):
            if t0 is None or f1 < t0:
                raise ValueError(f"{parts[pid]['name']} is on two gears at once")
    if len(raw["defaults"]) > MAX_SPORTS:
        raise ValueError(f"defaults for at most {MAX_SPORTS} sports")
    defaults = {}
    for sport, hist in raw["defaults"].items():
        sport = _sport(sport)
        if sport in defaults:
            raise ValueError("a sport is listed twice")
        if not isinstance(hist, list) or len(hist) > MAX_DEFAULTS:
            raise ValueError("bad default history")
        out = {}
        for e in hist:
            if not isinstance(e, dict):
                raise ValueError("bad default")
            gid = e.get("gear")
            if gid is not None and _id(gid, "gear") not in gear:
                raise ValueError("default gear doesn't exist")
            out[_day(e.get("since"), "since", False)] = gid
        hist = [{"gear": out[d], "since": d} for d in sorted(out)]  # one entry per day
        if hist:
            defaults[sport] = hist
    if sum(len(p["services"]) for p in parts.values()) > MAX_SERVICES_ALL:
        raise ValueError(f"at most {MAX_SERVICES_ALL} services in all")
    if len(raw["sessions"]) > MAX_SESSIONS:
        raise ValueError(f"at most {MAX_SESSIONS} sessions with a gear of their own")
    sessions = {}
    for k, e in raw["sessions"].items():
        if not isinstance(k, str) or not edits.ID_RE.fullmatch(k) or not isinstance(e, dict) or "gear" not in e:
            raise ValueError("bad session entry")
        gid = e["gear"]
        if gid is not None and _id(gid, "gear") not in gear:
            raise ValueError("session gear doesn't exist")
        x = {"gear": gid}
        if isinstance(e.get("start"), str) and len(e["start"]) <= 40:
            x["start"] = e["start"]
        if edits._finite(e.get("km")):
            x["km"] = e["km"]
        sessions[k] = x
    return {"v": VERSION, "rev": rev, "parts": parts, "gear": gear, "defaults": defaults, "sessions": sessions}


def empty():
    return {"v": VERSION, "rev": 0, "parts": {}, "gear": {}, "defaults": {}, "sessions": {}}


def public_kinds():
    out = {}
    for k, (label, group, specs) in KINDS.items():
        ss = []
        for s in specs:
            d = {"k": s[0], "label": s[1], "type": s[2]}
            if s[2] in ("num", "int"):
                d.update(unit=s[3], min=s[4], max=s[5])
            elif s[2] == "text":
                d["max"] = s[3]
            else:
                d["options"] = list(s[3])
            ss.append(d)
        out[k] = {"label": label, "group": group, "specs": ss}
    return out


# ------------------------------------------------------------------ resolving

class Resolver:
    """A frozen view of the store: which gear / parts a session used."""

    def __init__(self, data):
        self.d = data
        self.hist = {s: ([e["since"] for e in h], [e["gear"] for e in h]) for s, h in data["defaults"].items()}

    def of(self, aid, sport, day):
        """(gear id or None, "explicit" | "default" | None)."""
        e = self.d["sessions"].get(str(aid))
        if e is not None:
            return (e["gear"] if e["gear"] in self.d["gear"] else None), "explicit"
        return self.default_of(sport, day)

    def default_of(self, sport, day):
        """What the sport's default gives on that day: (gear id, "default") or (None, None)."""
        h = self.hist.get(sport)
        if not h or not isinstance(day, str):
            return None, None
        i = bisect.bisect_right(h[0], day[:10]) - 1
        gid = h[1][i] if i >= 0 else None
        g = self.d["gear"].get(gid)
        if g is None or (g["retired"] and g["retired"] <= day[:10]):
            return None, None
        return gid, "default"

    def parts(self, gid, day):
        g = self.d["gear"].get(gid)
        if g is None or not isinstance(day, str):
            return []
        d = day[:10]
        return list(dict.fromkeys(m["part"] for m in g["parts"] if m["from"] <= d and (m["to"] is None or d < m["to"])))

    def part_info(self, gid, day):
        return [{"id": p, **{k: self.d["parts"][p][k] for k in ("kind", "name", "brand", "specs")}}
                for p in self.parts(gid, day)]

    def name(self, gid):
        g = self.d["gear"].get(gid)
        return g["name"] if g else None


def totals(data, rows):
    """rows: (id, date, type, km, moving_s, elapsed_s, name) of the visible sessions.
    Per gear and part: sessions, km, moving_s, first, last; per part also
    km_total (with km_offset), km_since_service, last_service, service_left,
    due; per gear its latest RECENT sessions and km / sessions per month. Returns (totals, due part ids)."""
    res = Resolver(data)
    blank = lambda: {"sessions": 0, "km": 0.0, "moving_s": 0, "first": None, "last": None}  # noqa: E731
    gt = {k: {**blank(), "recent": [], "months": {}} for k in data["gear"]}
    pt = {k: {**blank(), "km_since_service": 0.0} for k in data["parts"]}
    last_svc = {k: (p["services"][-1]["date"] if p["services"] else None) for k, p in data["parts"].items()}

    def add(t, day, km, sec):
        t["sessions"] += 1
        t["km"] += km
        t["moving_s"] += sec
        if isinstance(day, str):
            t["first"] = min(t["first"] or day, day)
            t["last"] = max(t["last"] or day, day)

    for aid, day, typ, km, mv, el, name in sorted(rows, key=lambda r: r[1] or "", reverse=True):
        gid, src = res.of(aid, typ, day)
        if gid is None:
            continue
        km, sec = km or 0.0, mv or el or 0
        add(gt[gid], day, km, sec)
        if isinstance(day, str):
            mo = gt[gid]["months"].setdefault(day[:7], [0.0, 0])  # "YYYY-MM": [km, sessions]
            mo[0] += km
            mo[1] += 1
        if len(gt[gid]["recent"]) < RECENT:
            gt[gid]["recent"].append({"id": aid, "date": day, "type": typ, "km": km, "name": name, "src": src})
        for p in res.parts(gid, day):
            if not wears(data["parts"][p]["kind"], typ):
                continue
            add(pt[p], day, km, sec)
            ls = last_svc[p]
            if ls is None or day[:10] >= ls:  # a session on the service day counts as after it
                pt[p]["km_since_service"] += km
    due = []
    for k, p in data["parts"].items():
        t = pt[k]
        if not p["services"]:  # km_offset: wear from before Rollbook saw it, until the first service
            t["km_since_service"] += p["km_offset"]
        t["km_total"] = t["km"] + p["km_offset"]
        t["last_service"] = last_svc[k]
        si = p["service_km"]
        t["service_left"] = None if si is None else si - t["km_since_service"]
        t["due"] = bool(si) and not p["retired"] and t["km_since_service"] >= si
        if t["due"]:
            due.append(k)
    return {"gear": gt, "parts": pt}, due


# ------------------------------------------------------------------ store

LOAD_ERROR = "gear.json could not be loaded; it is left untouched and gear is read-only until it is fixed or moved away"


class Gear:
    def __init__(self, home):
        self.path = os.path.join(home, "gear.json") if home else None
        self.lock = threading.Lock()
        self.error = self.error_detail = None
        self.data = empty()
        if self.path and os.path.lexists(self.path):
            try:
                self.data = validate(self._read())
            except Exception as e:  # noqa: BLE001  untrusted file: never a startup crash
                self.error, self.error_detail = LOAD_ERROR, f"{type(e).__name__}: {e}"
                print(f"rollbook: {LOAD_ERROR} ({self.error_detail})", file=sys.stderr)
                self.data = empty()
        self.follow = edits.Follower(self.lock, lambda: self.data["sessions"], self._put_moved)

    def _read(self):
        fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("not a regular file")
            if st.st_size > MAX_FILE:
                raise ValueError("too big")
            raw = store.loads(f.read(MAX_FILE + 1).decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        return raw

    def _put_moved(self, sess):
        self.data = {**self.data, "sessions": sess}

    # ---- read. self.data is never changed in place (every change builds a new
    # dict and swaps it in), so a reference taken under the lock stays consistent.

    def resolver(self, db=None):
        if db is not None:
            self.follow.reconcile(db)
        with self.lock:
            return Resolver(self.data)

    def view(self, db):
        """Everything the gear page shows, with totals over the visible (edited) sessions."""
        self.follow.reconcile(db)
        rows = db.execute("SELECT id, date, type, km, moving_s, elapsed_s, name FROM activities "
                          "WHERE parse_error IS NULL").fetchall()
        with self.lock:
            d = self.data
        tot, due = totals(d, [tuple(r) for r in rows])
        out = {"rev": d["rev"], "parts": d["parts"], "gear": d["gear"], "defaults": d["defaults"],
               "totals": tot, "due": due, "kinds": public_kinds(), "actions": list(ACTIONS),
               "sport_group": SPORT_GROUP, "readonly_store": self.error is not None}
        if self.error:
            out["error"] = self.error
        return out

    # ---- write

    def remap(self, mapping):
        """{old id: new id} from the importer; also saves what reconcile moved."""
        if self.error:
            return
        with self.lock:
            sess = self.follow.remap(mapping)
            if sess is not None:
                self._commit({**self.data, "sessions": sess}, bump=False)

    def _commit(self, new, bump=True):
        """Validate, back up the current file, write, then make it current. Caller holds the lock."""
        new = validate({**new, "rev": self.data["rev"] + (1 if bump else 0)})
        size = len(json.dumps(new, indent=1, allow_nan=False).encode("ascii"))
        if size > MAX_FILE:  # it would not load again
            raise ValueError("the gear store is full: remove some notes or service entries")
        if self.path:
            if os.path.exists(self.path) and self.data["rev"]:
                store.write_json(self.path + ".bak", self.data)
            store.write_json(self.path, new)
        self.data = new
        self.follow.dirty = False

    def apply(self, db, body):
        """One change from the page. body: {op, rev, ...}. Raises ValueError (400),
        NotFound (404), Conflict (409), Broken (503)."""
        if self.error:
            raise Broken(self.error)
        op = OPS.get(body.get("op")) if isinstance(body.get("op"), str) else None
        if op is None:
            raise ValueError("unknown op")
        pre = op.pre(db, body) if op.pre else None  # DB reads before taking the lock
        self.follow.reconcile(db)
        with self.lock:
            if type(body.get("rev")) is not int or body["rev"] != self.data["rev"]:
                raise Conflict("gear changed in another window: reload")
            d = copy.deepcopy(self.data)
            res = op.fn(d, body, pre)
            self._commit(d)
            return res


def _new_id(d, bucket):
    while True:
        i = secrets.token_hex(8)
        if i not in d[bucket]:
            return i


def _get(d, bucket, i):
    if not isinstance(i, str) or i not in d[bucket]:
        raise NotFound(i)
    return d[bucket][i]


FIELDS_PART = ("kind", "name", "brand", "specs", "bought", "km_offset", "service_km", "notes")
FIELDS_GEAR = ("name", "sports", "notes")


def op_part_save(d, b, _):
    gid = b.get("mount_on")  # a new part fitted straight away; existing ones use mount.add
    if b.get("id") is None:
        if len(d["parts"]) >= MAX_PARTS:
            raise ValueError(f"at most {MAX_PARTS} parts")
        pid = _new_id(d, "parts")
        d["parts"][pid] = {**{k: b.get(k) for k in FIELDS_PART}, "services": [], "retired": None}
    else:
        if gid is not None:
            raise ValueError("fit an existing part from its gear's page")
        pid = b["id"]
        p = _get(d, "parts", pid)
        if "kind" in b and b["kind"] != p["kind"] and "specs" not in b:
            p["specs"] = {}  # another kind has other specs
        p.update({k: b[k] for k in FIELDS_PART if k in b})
    if gid is not None:
        _get(d, "gear", gid)["parts"].append({"part": pid, "from": _day(b.get("installed"), "installed", False),
                                              "to": None})
    return {"id": pid}


def op_part_retire(d, b, _):
    p = _get(d, "parts", b.get("id"))
    if b.get("undo"):
        p["retired"] = None
        return {"id": b["id"]}
    day = _day(b.get("date"), "date", False)
    p["retired"] = day
    if day:
        _close_mounts(d, lambda g, m: m["part"] == b["id"], day)
    return {"id": b["id"]}


def _close_mounts(d, which, day):
    """Take matching parts off on `day` (a mount that starts later can't be cut there)."""
    for g in d["gear"].values():
        for m in g["parts"]:
            if which(g, m) and (m["to"] is None or m["to"] > day):
                if m["from"] >= day:
                    raise ValueError(f"a part is fitted on {g['name']} only from {m['from']}: "
                                     "change those dates first")
                m["to"] = day


def op_part_service(d, b, _):
    p = _get(d, "parts", b.get("id"))
    if b.get("delete") is not None:
        i = b["delete"]
        if type(i) is not int or not 0 <= i < len(p["services"]):
            raise NotFound(i)
        v = p["services"][i]
        if any(k in b and b[k] != v[k] for k in ("date", "action")):  # the page's row is not this entry
            raise Conflict("the service list changed in another window: reload")
        p["services"].pop(i)
    else:
        p["services"].append({"date": b.get("date"), "action": b.get("action"), "note": b.get("note")})
    return {"id": b["id"]}


def op_part_delete(d, b, _):
    _get(d, "parts", b.get("id"))
    if any(m["part"] == b["id"] for g in d["gear"].values() for m in g["parts"]):
        raise Conflict("this part is on a gear (now or in the past): remove it there first, or retire it")
    del d["parts"][b["id"]]
    return {"id": b["id"]}


def op_gear_save(d, b, _):
    if b.get("id") is None:
        if len(d["gear"]) >= MAX_GEAR:
            raise ValueError(f"at most {MAX_GEAR} gear")
        gid = _new_id(d, "gear")
        d["gear"][gid] = {**{k: b.get(k) for k in FIELDS_GEAR}, "parts": [], "retired": None}
    else:
        gid = b["id"]
        _get(d, "gear", gid).update({k: b[k] for k in FIELDS_GEAR if k in b})
    if b.get("default_since"):  # "make it the default for its sports from this day"
        sports = d["gear"][gid].get("sports")
        if not isinstance(sports, list) or not sports:
            raise ValueError("pick a sport to make it the default")
        for s in sports:
            op_default_set(d, {"sport": s, "gear": gid, "since": b["default_since"]}, None)
    return {"id": gid}


def op_gear_retire(d, b, _):
    g = _get(d, "gear", b.get("id"))
    if b.get("undo"):
        g["retired"] = None
        return {"id": b["id"]}
    day = _day(b.get("date"), "date", False)
    g["retired"] = day
    _close_mounts(d, lambda gg, m: gg is g, day)  # its parts come off (spare again)
    return {"id": b["id"]}


def op_gear_delete(d, b, existing):
    _get(d, "gear", b.get("id"))
    gid = b["id"]
    if any(e["gear"] == gid for h in d["defaults"].values() for e in h):
        raise Conflict("this gear is (or was) a default: retire it instead")
    if any(e["gear"] == gid for k, e in d["sessions"].items() if int(k) in existing):
        raise Conflict("sessions are assigned to this gear: retire it instead")
    d["sessions"] = {k: e for k, e in d["sessions"].items() if e["gear"] != gid}
    del d["gear"][gid]
    return {"id": gid}


def _mount(g, b):
    i = b.get("index")
    if type(i) is not int or not 0 <= i < len(g["parts"]):
        raise NotFound(i)
    if "part" in b and b["part"] != g["parts"][i]["part"]:  # the page's row is not this fitting
        raise Conflict("the gear's parts changed in another window: reload")
    return i


def op_mount_add(d, b, _):
    g = _get(d, "gear", b.get("gear"))
    _get(d, "parts", b.get("part"))
    g["parts"].append({"part": b["part"], "from": b.get("from"), "to": b.get("to")})
    return {"id": b["gear"]}


def op_mount_set(d, b, _):
    g = _get(d, "gear", b.get("gear"))
    m = g["parts"][_mount(g, b)]
    m["from"], m["to"] = b.get("from", m["from"]), b.get("to", m["to"])
    return {"id": b["gear"]}


def op_mount_delete(d, b, _):
    g = _get(d, "gear", b.get("gear"))
    g["parts"].pop(_mount(g, b))
    return {"id": b["gear"]}


def op_default_set(d, b, _):
    sport, since, gid = _sport(b.get("sport")), _day(b.get("since"), "since", False), b.get("gear")
    if gid is not None:
        _get(d, "gear", gid)
    hist = [e for e in d["defaults"].get(sport, []) if e["since"] != since]
    if b.get("delete"):
        if len(hist) == len(d["defaults"].get(sport, [])):
            raise NotFound(since)
    else:
        hist.append({"gear": gid, "since": since})
    d["defaults"][sport] = hist
    if not hist:
        del d["defaults"][sport]
    return {"sport": sport}


def _assign_rows(db, b):
    ids = b.get("ids")
    if not isinstance(ids, list) or not 0 < len(ids) <= MAX_ASSIGN:
        raise ValueError(f"ids: 1 to {MAX_ASSIGN} sessions")
    if not all(type(i) is int and 0 < i < 2 ** 53 for i in ids):
        raise ValueError("bad session id")
    found = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in db.execute(f"SELECT id, datetime, km FROM main.activities WHERE id IN ({q})", chunk):
            found[r[0]] = (r[1], r[2])
    missing = [i for i in ids if i not in found]
    if missing:
        raise NotFound(missing[0])
    return found


def op_assign(d, b, rows):
    if "gear" not in b or not (b["gear"] is None or isinstance(b["gear"], str)):
        raise ValueError("gear: an id, null (none) or \"default\"")
    gid = b["gear"]
    if gid not in (None, "default"):
        _get(d, "gear", gid)
    for aid, (start, km) in rows.items():
        if gid == "default":
            d["sessions"].pop(str(aid), None)
        else:
            e = {"gear": gid}
            if isinstance(start, str):
                e["start"] = start
            if edits._finite(km):
                e["km"] = km
            d["sessions"][str(aid)] = e
    if len(d["sessions"]) > MAX_SESSIONS:
        raise ValueError("too many sessions with a gear of their own")
    return {"n": len(rows)}


class Op:
    """fn(store copy, body, pre) -> result, applied under the lock; pre(db, body): DB reads before it."""

    def __init__(self, fn, pre=None):
        self.fn, self.pre = fn, pre


def _existing_ids(db, _):
    return {r[0] for r in db.execute("SELECT id FROM main.activities")}


OPS = {"part.save": Op(op_part_save), "part.retire": Op(op_part_retire), "part.service": Op(op_part_service),
       "part.delete": Op(op_part_delete), "gear.save": Op(op_gear_save), "gear.retire": Op(op_gear_retire),
       "gear.delete": Op(op_gear_delete, _existing_ids), "mount.add": Op(op_mount_add),
       "mount.set": Op(op_mount_set), "mount.delete": Op(op_mount_delete), "default.set": Op(op_default_set),
       "assign": Op(op_assign, _assign_rows)}
