# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""`rollbook mcp`: the training database as Model Context Protocol tools.

Stdlib only. Speaks JSON-RPC 2.0 over stdio (one message per line), the MCP
"stdio" transport, so an assistant such as OpenCode or Claude can start it as
a local server. Everything is read-only:

* the DB is opened with `mode=ro`, and free-form SQL (`query`) additionally
  runs under an authorizer that only allows SELECT, read-only PRAGMAs and
  functions (no ATTACH, no TEMP tables, no writes of any kind);
* session edits from the dashboard (data/edits.json) are applied with the same
  TEMP overlay as `rollbook web`, so names, sports and hidden sessions match
  the dashboard; `main.activities` is the raw, unedited table;
* the dashboard's metrics cache (data/cache/web_metrics.json) is read, never
  written: a session missing from it is derived in memory only;
* GPS coordinates never leave: `samples.lat` / `samples.lon` read as NULL in
  `query` unless the server was started with --allow-gps, and the per-session
  tools drop tracks and lap-gate positions. GPS tracks reveal home.
"""

import json
import os
import sqlite3
import sys
import time

import edits
import server as srv

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
MAX_ROWS = 5000
QUERY_TIMEOUT_S = 30
GPS_COLS = {("samples", "lat"), ("samples", "lon")}
READ_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
                "foreign_key_list"}
DERIVED_DROP = {"trk", "ll0", "hr_hist", "pw_hist", "n"}  # coordinates, bulky histograms
HR_ZONES = (0.75, 0.84, 0.90, 0.95)  # lower edges of Z2..Z5 as % of HRmax, like the dashboard
SORTABLE = ("date", "km", "moving_s", "elapsed_s", "ascent_m", "kmh_moving", "best30_kmh",
            "best5min_kmh", "best10min_kmh", "best20min_kmh", "hr_avg", "hr_max",
            "watts_avg", "f_np")
LIST_COLS = ("id", "date", "type", "name", "km", "moving_s", "pause_s", "kmh_moving",
             "best10min_kmh", "hr_avg", "ascent_m", "watts_avg")
# no best30_kmh: a 30 s window is short enough for one GPS glitch to win it
RECORD_COLS = ("best5min_kmh", "best10min_kmh", "best20min_kmh", "km",
               "moving_s", "ascent_m", "watts_avg", "f_np")
RECORD_DERIVED = {"best30min_kmh": "b30", "best45min_kmh": "b45", "best60min_kmh": "b60",
                  "wind_neutral_kmh": "wind_neutral", "weighted_power_w": "np"}
POWER_LABELS = {"5": "5s", "30": "30s", "60": "1m", "300": "5m", "1200": "20m",
                "1800": "30m", "3600": "60m"}

INSTRUCTIONS = """\
Rollbook: one athlete's training history (Strava export; HUAWEI WATCH GT 6 Pro,
ROUVY/MyWhoosh indoor rides). Mostly inline skating. Read-only.

Which metric to trust:
- best10min_kmh (also best5/best20) = best rolling-window speed. USE THIS to
  compare form over time; immune to stops and sample rate. NULL if never
  enriched.
- kmh_moving: fine within an era but NOT comparable across 2025-08 (sampling
  dropped ~1 Hz -> ~0.16 Hz, which inflates later moving speeds).
- kmh_elapsed: includes stops, measures the route, not fitness.
- hr_avg: only on recent skates (watch from 2026-09-08) and indoor rides.
  best10min_kmh / hr_avg is the efficiency metric when HR exists.
- Power (watts_avg, f_np, best power): indoor rides only, the most reliable
  fitness signal. Outdoor s_avg_watts is Strava's estimate, not measured.
- Never compare 'Virtual Ride' speeds with outdoor ones (simulated gradients).
- Best efforts (400 m … marathon): fastest elapsed time over that distance
  anywhere in a session, stops included, per sport; none for virtual rides.
  400 m / 1 km read slower on the 6 s recording era (since 2025-08).
- Wind-neutral speed (out-and-back routes) = mean of the best 10 min out and
  back; it cancels wind and slope.
- km / ascent_m prefer Strava's figure; *_calc are recomputed; s_* columns are
  Strava's own summary. Summed watch climbing is unreliable, use ascent_m.

Start with `sports` or `schema`, then `activities` / `trend` / `records`;
`query` takes any SELECT (activities = edited view, main.activities = raw,
samples is per second and large: always filter by activity_id).
"""


def _r(v):
    """Round floats for compact output; recurse into containers."""
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, dict):
        return {k: _r(x) for k, x in v.items() if x is not None}
    if isinstance(v, (list, tuple)):
        return [_r(x) for x in v]
    return v


class ToolError(Exception):
    """Reported to the model as a tool result with isError, not a protocol error."""


class Rollbook:
    def __init__(self, db_path, cache_path, allow_gps=False):
        if not os.path.exists(db_path):
            raise FileNotFoundError(db_path)
        self.db_path, self.cache_path, self.allow_gps = db_path, cache_path, allow_gps
        self.home = os.path.dirname(os.path.abspath(db_path))
        uri = f"file:{db_path}?mode=ro"
        # trusted connection for the built-in tools (derive() reads coordinates)
        self.db = sqlite3.connect(uri, uri=True)
        self.db.row_factory = sqlite3.Row
        # free-form SQL gets its own connection with the authorizer always armed
        # once the overlay is installed. No statement cache: the authorizer runs
        # at prepare time, so a cached statement would skip it.
        self.qdb = sqlite3.connect(uri, uri=True, cached_statements=0)
        self.locked = False
        self.deadline = None
        self.qdb.set_authorizer(self._authorize)
        self.qdb.set_progress_handler(self._progress, 20000)
        self._edits, self._edits_mtime = None, None
        self._metrics, self._metrics_mtime = None, None

    def close(self):
        self.db.close()
        self.qdb.close()

    # ------------------------------------------------------------ plumbing

    def _authorize(self, action, a1, a2, dbname, source):
        if not self.locked:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if not self.allow_gps and (a1, a2) in GPS_COLS:
                return sqlite3.SQLITE_IGNORE  # reads as NULL, so SELECT * still works
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
                      getattr(sqlite3, "SQLITE_RECURSIVE", 33)):
            return sqlite3.SQLITE_OK
        # a2 is the pragma's argument, whether `PRAGMA x(arg)` or `PRAGMA x = arg`;
        # the allowed ones are pure introspection either way
        if action == sqlite3.SQLITE_PRAGMA and (a1 or "").lower() in READ_PRAGMAS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def _progress(self):
        return 1 if self.deadline and time.monotonic() > self.deadline else 0

    @staticmethod
    def _mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def sync(self):
        """Refresh the edits overlay and metrics cache if their files changed."""
        m = self._mtime(os.path.join(self.home, "edits.json"))
        if self._edits is None or m != self._edits_mtime:
            self._edits, self._edits_mtime = edits.Edits(self.home), m
        self._edits.sync(self.db)
        self._edits.sync(self.qdb)  # runs unlocked: called only between queries
        m = self._mtime(self.cache_path)
        if self._metrics is None or m != self._metrics_mtime:
            self._metrics, self._metrics_mtime = srv.Metrics(self.db_path, self.cache_path), m

    def cols(self):
        return {r[1] for r in self.db.execute("PRAGMA table_info(activities)")}

    def derived(self, rows, compute=True):
        """Metrics-cache entries for activity rows (dicts with id, samples).
        compute=False: only what is already cached and current."""
        cache = self._metrics.data if self._metrics else {}
        out = {}
        for a in rows:
            n = a["samples"] or 0
            m = cache.get(str(a["id"]))
            if m is None or m.get("n") != n:
                if not compute:
                    continue
                m = srv.derive(self.db, a["id"])  # in memory only, the cache file is the dashboard's
                m["n"] = n
                cache[str(a["id"])] = m
            out[a["id"]] = m
        return out

    @staticmethod
    def clean_derived(m):
        m = {k: v for k, v in m.items() if k not in DERIVED_DROP}
        if "pw" in m:
            m["best_power_w"] = {POWER_LABELS.get(k, k): v for k, v in m.pop("pw").items()}
        if "np" in m:
            m["weighted_power_w"] = m.pop("np")
        if "be" in m:  # best efforts by distance: elapsed seconds and where it started (no coordinates)
            m["best_efforts"] = {k: {"seconds": v[0], "start_s": v[1], "start_km": round(v[2] / 1000, 3)} for k, v in m.pop("be").items()}
        for k in ("b30", "b45", "b60"):
            if k in m:
                m[f"best{k[1:]}min_kmh"] = m.pop(k)
        if "laps" in m:
            L = m.pop("laps")
            laps = [{k: v for k, v in x.items() if k not in ("t0", "t1")} for x in L.get("laps", [])]
            counted = [x["elapsed"] for x in laps if x.get("counted")]
            m["laps"] = {"lap_km": L.get("lap_km"), "count": len(laps),
                         "best_s": min(counted) if counted else None,
                         "median_s": sorted(counted)[len(counted) // 2] if counted else None,
                         "laps": laps}
        return m

    def hrmax(self):
        try:
            with open(os.path.join(self.home, "profile.json")) as f:
                p = json.load(f)
            if p.get("hr_max"):
                return p["hr_max"]
        except (OSError, ValueError):
            pass
        return self.db.execute("SELECT MAX(hr_max) FROM activities "
                               "WHERE date >= date('now','-365 days') AND hr_max < 230").fetchone()[0]

    @staticmethod
    def sport_filter(args, where, params):
        if args.get("sport"):
            where.append("LOWER(type) = LOWER(?)")
            params.append(args["sport"])
        if args.get("since"):
            where.append("date >= ?")
            params.append(args["since"])
        if args.get("until"):
            where.append("date <= ?")
            params.append(args["until"])

    # ------------------------------------------------------------ tools

    def t_schema(self, args):
        out = []
        for name, kind in self.db.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall():
            cols = [f"{r[1]} {r[2]}".strip() for r in self.db.execute(f'PRAGMA main.table_info("{name}")')]
            ent = {"name": name, "kind": kind, "columns": cols}
            if name != "samples":  # 4M rows; COUNT(*) takes a while
                ent["rows"] = self.db.execute(f'SELECT COUNT(*) FROM main."{name}"').fetchone()[0]
            out.append(ent)
        out.append({"name": "activities", "kind": "temp view",
                    "note": "main.activities with dashboard edits (name/type overrides, hidden rows "
                            "left out); unqualified `activities` resolves to it. activities_all "
                            "keeps hidden rows plus hidden/deleted/orig_name/orig_type."})
        if not self.allow_gps:
            out.append({"note": "samples.lat / samples.lon read as NULL (privacy)"})
        return out

    def t_query(self, args):
        sql = (args.get("sql") or "").strip()
        if not sql:
            raise ToolError("sql is required")
        cap = max(1, min(int(args.get("max_rows") or 200), MAX_ROWS))
        self.locked, self.deadline = True, time.monotonic() + QUERY_TIMEOUT_S
        try:
            cur = self.qdb.execute(sql)
            rows = cur.fetchmany(cap + 1)
        except sqlite3.Warning:
            raise ToolError("one statement at a time")
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if msg == "interrupted":
                msg = f"query took longer than {QUERY_TIMEOUT_S} s; filter samples by activity_id"
            elif "not authorized" in msg:
                msg = "not authorized: read-only, SELECT only (no ATTACH, PRAGMA writes, TEMP objects)"
            raise ToolError(f"sql: {msg}")
        finally:
            self.locked, self.deadline = False, None
        if not cur.description:
            return {"columns": [], "rows": []}
        return {"columns": [d[0] for d in cur.description],
                "rows": [_r(list(r)) for r in rows[:cap]],
                "truncated": len(rows) > cap}

    def t_sports(self, args):
        cols = self.cols()
        b10 = "ROUND(MAX(best10min_kmh), 2)" if "best10min_kmh" in cols else "NULL"
        rows = self.db.execute(
            "SELECT type, COUNT(*) n, ROUND(SUM(km), 1) km, ROUND(SUM(moving_s) / 3600.0, 1) moving_h, "
            f"MIN(date) first, MAX(date) last, SUM(hr_avg > 0) with_hr, {b10} best10min_kmh "
            "FROM activities GROUP BY type ORDER BY n DESC").fetchall()
        return [_r(dict(r)) for r in rows]

    def t_activities(self, args):
        cols = self.cols()
        order = args.get("order_by") or "date"
        if order not in SORTABLE:
            raise ToolError(f"order_by must be one of {', '.join(SORTABLE)}")
        if order not in cols:
            raise ToolError(f"{order} is not in this database (run `rollbook db enrich`?)")
        where, params = [], []
        self.sport_filter(args, where, params)
        if args.get("search"):
            where.append("(name LIKE ? OR description LIKE ?)")
            params += [f"%{args['search']}%"] * 2
        if args.get("min_km") is not None:
            where.append("km >= ?")
            params.append(float(args["min_km"]))
        table = "activities_all" if args.get("include_hidden") else "activities"
        sel = [c for c in LIST_COLS if c in cols] + (["hidden"] if args.get("include_hidden") else [])
        limit = max(1, min(int(args.get("limit") or 50), 500))
        desc = "DESC" if args.get("descending", True) else "ASC"
        sql = (f"SELECT {', '.join(sel)} FROM {table}" + (" WHERE " + " AND ".join(where) if where else "")
               + f" ORDER BY {order} IS NULL, {order} {desc}, date DESC LIMIT ?")
        rows = self.db.execute(sql, params + [limit]).fetchall()
        total = self.db.execute(f"SELECT COUNT(*) FROM {table}" + (" WHERE " + " AND ".join(where) if where else ""),
                                params).fetchone()[0]
        return {"matching": total, "returned": len(rows), "columns": sel,
                "rows": [_r(list(r)) for r in rows]}

    def _activity_row(self, aid):
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            raise ToolError("id must be an activity id (integer)")
        row = self.db.execute("SELECT * FROM activities_all WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise ToolError(f"no activity {aid}")
        return dict(row)

    def t_activity(self, args):
        a = self._activity_row(args.get("id"))
        m = self.derived([a]).get(a["id"], {})
        out = {"activity": _r(a)}
        e = self._edits.get(a["id"]) if self._edits else None
        if e:
            out["edit"] = e
        out["derived"] = _r(self.clean_derived(m))
        hist, hrmax = m.get("hr_hist"), self.hrmax()
        if hist and hrmax:
            z = [0.0] * (len(HR_ZONES) + 1)
            for bpm, s in hist.items():
                z[sum(int(bpm) >= f * hrmax for f in HR_ZONES)] += s
            tot = sum(z) or 1
            names = [f"Z{i + 1}" for i in range(len(z))]
            out["hr_zones"] = {"hrmax": hrmax,
                               "seconds": {k: round(s) for k, s in zip(names, z)},
                               "pct": {k: round(s / tot * 100, 1) for k, s in zip(names, z)}}
        if args.get("splits", True):
            out["splits"] = [_r(dict(r)) for r in self.db.execute(
                "SELECT km, moving_s, elapsed_s, pause_s, kmh, hr FROM splits WHERE activity_id=? ORDER BY km",
                (a["id"],))]
        return out

    def t_compare(self, args):
        ids = args.get("ids") or []
        if not isinstance(ids, list) or not 2 <= len(ids) <= 10:
            raise ToolError("ids: a list of 2 to 10 activity ids")
        acts = [self._activity_row(i) for i in ids]
        der = self.derived(acts)
        keys = ("date", "type", "name", "km", "elapsed_s", "moving_s", "pause_s", "kmh_moving",
                "kmh_elapsed", "best30_kmh", "best5min_kmh", "best10min_kmh", "best20min_kmh",
                "hr_avg", "hr_max", "ascent_m", "watts_avg", "f_np", "cad_avg")
        out = []
        for a in acts:
            m = self.clean_derived(der.get(a["id"], {}))
            ent = {"id": a["id"], **{k: a.get(k) for k in keys}}
            if a.get("hr_avg") and a.get("best10min_kmh"):
                ent["best10min_kmh_per_bpm"] = a["best10min_kmh"] / a["hr_avg"]
            for k in ("route", "best30min_kmh", "best60min_kmh", "wind_neutral", "asym_pct",
                      "out_b10", "back_b10", "weighted_power_w", "best_power_w"):
                if k in m:
                    ent[k] = m[k]
            if "laps" in m:
                ent["laps"] = {k: m["laps"][k] for k in ("lap_km", "count", "best_s", "median_s")}
            out.append(_r(ent))
        return out

    def t_trend(self, args):
        by = args.get("by") or "month"
        key = {"week": "isoweek", "month": "month", "year": "year"}.get(by)
        if not key:
            raise ToolError("by must be week, month or year")
        cols = self.cols()
        where, params = [], []
        self.sport_filter(args, where, params)
        opt = lambda c, agg: f"ROUND({agg}({c}), 2)" if c in cols else "NULL"
        sql = (f"SELECT {key} period, COUNT(*) n, ROUND(SUM(km), 1) km, "
               "ROUND(SUM(moving_s) / 3600.0, 2) moving_h, ROUND(SUM(ascent_m)) ascent_m, "
               f"{opt('best10min_kmh', 'AVG')} best10min_avg, {opt('best10min_kmh', 'MAX')} best10min_max, "
               "ROUND(AVG(kmh_moving), 2) kmh_moving_avg, ROUND(AVG(NULLIF(hr_avg, 0))) hr_avg, "
               f"{opt('watts_avg', 'AVG')} watts_avg "
               "FROM activities" + (" WHERE " + " AND ".join(where) if where else "")
               + f" GROUP BY {key} ORDER BY {key}")
        rows = [_r(dict(r)) for r in self.db.execute(sql, params)]
        return {"by": by, "sport": args.get("sport") or "all", "periods": rows}

    def t_records(self, args):
        cols = self.cols()
        n = max(1, min(int(args.get("limit") or 5), 25))
        where, params = [], []
        self.sport_filter(args, where, params)
        w = " WHERE " + " AND ".join(where) if where else ""
        out = {}
        for c in RECORD_COLS:
            if c not in cols:
                continue
            rows = self.db.execute(
                f"SELECT id, date, type, name, {c} value FROM activities{w}{' AND' if w else ' WHERE'} "
                f"{c} IS NOT NULL AND {c} > 0 ORDER BY {c} DESC LIMIT ?", params + [n]).fetchall()
            if rows:
                out[c] = [_r(dict(r)) for r in rows]
        acts = [dict(r) for r in self.db.execute(f"SELECT id, date, type, name, samples FROM activities{w}", params)]
        der = self.derived(acts, compute=False)
        info = {a["id"]: a for a in acts}

        def top(get):
            vals = [(get(m), aid) for aid, m in der.items()]
            vals = sorted(((v, aid) for v, aid in vals if v), reverse=True)[:n]
            return [_r({**{k: info[aid][k] for k in ("id", "date", "type", "name")}, "value": v})
                    for v, aid in vals]

        for label, k in RECORD_DERIVED.items():
            t = top(lambda m, k=k: m.get(k))
            if t:
                out[label] = t
        for k, label in POWER_LABELS.items():
            t = top(lambda m, k=k: (m.get("pw") or {}).get(k))
            if t:
                out[f"best_power_{label}_w"] = t
        # best efforts by distance: fastest first, and per sport (a ride's 5 km is no run's)
        for label in ("400", "1k", "5k", "10k", "21k", "42k"):
            per = {}
            for aid, m in der.items():
                v = ((m.get("be") or {}).get(label) or [None])[0]
                if v:
                    per.setdefault(info[aid]["type"], []).append((v, aid))
            res = {typ: [_r({**{k: info[aid][k] for k in ("id", "date", "type", "name")}, "seconds": v})
                         for v, aid in sorted(vals)[:n]] for typ, vals in sorted(per.items())}
            if res:
                out[f"best_effort_{label}_s"] = next(iter(res.values())) if args.get("sport") else res
        if len(der) < len(acts):
            out["note"] = (f"derived records cover {len(der)} of {len(acts)} sessions "
                           "(open `rollbook web` once to fill the metrics cache)")
        return out


def _props(**kw):
    return {"type": "object", "properties": kw, "additionalProperties": False}


FILTERS = {
    "sport": {"type": "string", "description": "Exact activity type, e.g. 'Inline Skate', 'Ride', "
                                               "'Virtual Ride', 'Run' (see the sports tool)."},
    "since": {"type": "string", "description": "YYYY-MM-DD, inclusive."},
    "until": {"type": "string", "description": "YYYY-MM-DD, inclusive."},
}

TOOLS = [
    ("sports", "Sports in the history: sessions, km, hours, date range, sessions with HR, best 10-min speed.",
     _props(), Rollbook.t_sports),
    ("schema", "Tables, views and columns of the SQLite database, with row counts.",
     _props(), Rollbook.t_schema),
    ("query", "Run one read-only SQL SELECT on the SQLite database. `activities` has dashboard "
              "edits applied; `samples` (per second, millions of rows) must be filtered by activity_id.",
     {**_props(sql={"type": "string", "description": "A single SELECT statement (SQLite dialect)."},
               max_rows={"type": "integer", "minimum": 1, "maximum": MAX_ROWS,
                         "description": "Row cap, default 200."}), "required": ["sql"]},
     Rollbook.t_query),
    ("activities", "List / search sessions with key metrics, filtered and sorted.",
     _props(**FILTERS,
            search={"type": "string", "description": "Substring of name or description."},
            min_km={"type": "number"},
            order_by={"type": "string", "enum": list(SORTABLE), "description": "Default date."},
            descending={"type": "boolean", "description": "Default true."},
            limit={"type": "integer", "minimum": 1, "maximum": 500, "description": "Default 50."},
            include_hidden={"type": "boolean", "description": "Also sessions hidden in the dashboard."}),
     Rollbook.t_activities),
    ("activity", "Everything about one session: summary row, per-km splits, derived metrics "
                 "(best 30/45/60 min, route kind, out/back legs and wind-neutral speed, laps, best "
                 "power, weighted power) and time in HR zones.",
     {**_props(id={"type": "integer"},
               splits={"type": "boolean", "description": "Include per-km splits, default true."}),
      "required": ["id"]},
     Rollbook.t_activity),
    ("compare", "Side-by-side key metrics for 2-10 sessions.",
     {**_props(ids={"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 10}),
      "required": ["ids"]},
     Rollbook.t_compare),
    ("trend", "Totals and form per week / month / year: sessions, km, hours, ascent, best 10-min "
              "speed (avg and max), moving speed, HR, power.",
     _props(**FILTERS, by={"type": "string", "enum": ["week", "month", "year"], "description": "Default month."}),
     Rollbook.t_trend),
    ("records", "Personal bests: top sessions by best 5/10/20/30/45/60-min speed, wind-neutral speed, "
                "distance, duration, ascent and power, and best efforts by distance (fastest 400 m … marathon, per sport).",
     _props(**FILTERS, limit={"type": "integer", "minimum": 1, "maximum": 25, "description": "Per record, default 5."}),
     Rollbook.t_records),
]
TOOL_FN = {name: fn for name, _, _, fn in TOOLS}


class Server:
    def __init__(self, rb, version="0"):
        self.rb, self.version = rb, version

    def handle(self, msg):
        """One JSON-RPC message in, the response dict out (None for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return self._error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:  # notification: initialized, cancelled, ...
            return None
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "rollbook", "version": self.version},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": n, "description": d, "inputSchema": s,
                                     "annotations": {"readOnlyHint": True, "openWorldHint": False}}
                                    for n, d, s, _ in TOOLS]}
            elif method == "tools/call":
                result = self.call(params.get("name"), params.get("arguments") or {})
            else:
                return self._error(mid, -32601, f"method not found: {method}")
        except Exception as e:  # never kill the session over one request
            return self._error(mid, -32603, f"{type(e).__name__}: {e}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call(self, name, args):
        fn = TOOL_FN.get(name)
        if fn is None:
            return self._tool_text(f"unknown tool: {name}", True)
        if not isinstance(args, dict):
            return self._tool_text("arguments must be an object", True)
        try:
            self.rb.sync()
            res = fn(self.rb, args)
        except ToolError as e:
            return self._tool_text(str(e), True)
        except (ValueError, TypeError) as e:
            return self._tool_text(f"bad arguments: {e}", True)
        except sqlite3.Error as e:
            return self._tool_text(f"database: {e}", True)
        return self._tool_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), False)

    @staticmethod
    def _tool_text(text, err):
        return {"content": [{"type": "text", "text": text}], "isError": err}

    @staticmethod
    def _error(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    def serve(self, inp=None, out=None):
        """stdio transport: newline-delimited JSON-RPC until EOF."""
        inp = inp or sys.stdin.buffer
        out = out or sys.stdout.buffer
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                resp = self._error(None, -32700, "parse error")
            else:
                if isinstance(msg, list):  # batch (2025-03-26 only, harmless to accept)
                    resp = [r for r in (self.handle(m) for m in msg) if r is not None] or None
                else:
                    resp = self.handle(msg)
            if resp is not None:
                out.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                out.flush()


def run(db_path, cache_path, allow_gps=False, version="0"):
    Server(Rollbook(db_path, cache_path, allow_gps), version).serve()
