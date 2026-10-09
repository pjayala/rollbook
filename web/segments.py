# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Auto-generated segments: popular stretches of road and loops, with every
session's time on each.

Generation (per sport type), from the ~15 m map tracks in the metrics cache:
  1. Count distinct sessions per CELL_M grid cell (3x3-smoothed, so GPS
     scatter across a cell edge doesn't split a road's count).
  2. Walk tracks from most to least "popular". Each track is cut into runs of
     popular points, split at pen-ups, at U-turns and when the track closes a
     loop on itself (same spot, same heading, >= LOOP_MIN_M later).
  3. Points already covered by an earlier segment in the same direction are
     dropped, so overlapping sessions never produce offset duplicates. Runs
     are cut into ~SEG_TARGET_M pieces; a long run also yields one "full"
     segment covering all of it.
Direction matters: the out and back legs of a route are separate segments,
which is the point when there is wind.

Matching: an effort starts where the session crosses the start gate (line
perpendicular to the segment's start heading, within GATE_R, heading-aligned)
and ends at the next end-gate crossing 80-130% of the segment length later,
provided the session stays within FOLLOW_R of a checkpoint every CHECK_M.
Times are gate-to-gate, interpolated on the 10 m resampled track.
"""

import hashlib
import json
import math
import os
import threading

import server as srv

SEG_VERSION = 4
CELL_M = 40.0
POP_MIN = 5  # distinct sessions through a cell for it to be "usual"
GAP_PTS = 3  # unpopular points tolerated inside a run (~45 m)
SEG_MIN_M = 800.0
LOOP_MIN_M = 500.0
SEG_TARGET_M = 2500.0
SEG_FULL_MAX_M = 16000.0
CHAIN_R = 60.0  # piece end -> next piece start, for chaining into a full leg
MAX_SEGS = 40
COVER_R = 35.0
GATE_R = 30.0
FOLLOW_R = 40.0
CHECK_M = 100.0
REVERSE_COS = -0.5  # heading change > 120 deg = U-turn
ALIGN_COS = 0.6


def decode(flat):
    """Inverse of server.compact_track: [(lat, lon) | None for pen-up]."""
    pts, la, lo = [], 0, 0
    for i in range(0, len(flat), 2):
        if i and flat[i] == 0 and flat[i + 1] == 0:
            pts.append(None)
            continue
        la += flat[i]
        lo += flat[i + 1]
        pts.append((la / srv.TRACK_Q, lo / srv.TRACK_Q))
    return pts


def cell(lat, lon, size=CELL_M):
    return (math.floor(lat * 110540 / size),
            math.floor(lon * 111320 * math.cos(math.radians(lat)) / size))


def metres(a, b):
    dy = (b[0] - a[0]) * 110540
    dx = (b[1] - a[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def headings(pts, w=2):
    """Unit (east, north) heading per point over +-w points, None at pen-ups."""
    out = [None] * len(pts)
    for j, p in enumerate(pts):
        if p is None:
            continue
        a = b = j
        while a > j - w and a > 0 and pts[a - 1] is not None:
            a -= 1
        while b < j + w and b < len(pts) - 1 and pts[b + 1] is not None:
            b += 1
        if a == b:
            continue
        pa, pb = pts[a], pts[b]
        hx = (pb[1] - pa[1]) * 111320 * math.cos(math.radians(pa[0]))
        hy = (pb[0] - pa[0]) * 110540
        L = math.hypot(hx, hy)
        if L > 5:
            out[j] = (hx / L, hy / L)
    return out


class Cover:
    """Spatial index of points already belonging to a segment, with heading."""

    def __init__(self):
        self.g = {}

    def add(self, pts, head):
        for p, h in zip(pts, head):
            if p is not None and h is not None:
                self.g.setdefault(cell(*p), []).append((p, h))

    def covered(self, p, h):
        cy, cx = cell(*p)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for q, hq in self.g.get((cy + dy, cx + dx), ()):
                    if hq[0] * h[0] + hq[1] * h[1] >= ALIGN_COS and metres(p, q) <= COVER_R:
                        return True
        return False


def _runs(pts, head, pop):
    """Candidate runs of popular points: [(indices, closed_loop)]."""
    s = [0.0] * len(pts)
    for j in range(1, len(pts)):
        if pts[j] is not None and pts[j - 1] is not None:
            s[j] = s[j - 1] + metres(pts[j - 1], pts[j])
        else:
            s[j] = s[j - 1]
    out, cur, pend, grid = [], [], [], {}

    def close(closed=False):
        if len(cur) >= 2:
            out.append((list(cur), closed))
        cur.clear()
        pend.clear()
        grid.clear()

    for j, p in enumerate(pts):
        h = head[j]
        if p is None or h is None:
            close()
            continue
        if not pop[j]:
            if cur:
                pend.append(j)
                if len(pend) > GAP_PTS:
                    close()
            continue
        if cur:
            hp = head[cur[-1]]
            if hp[0] * h[0] + hp[1] * h[1] < REVERSE_COS:
                close()
        if cur:
            cur.extend(pend)
            pend.clear()
            # loop closure: back at an earlier point of this run, same heading
            cy, cx = cell(*p, 25.0)
            hit = any(s[j] - s[k] >= LOOP_MIN_M and metres(p, pts[k]) <= 25
                      and head[k][0] * h[0] + head[k][1] * h[1] >= ALIGN_COS
                      for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                      for k in grid.get((cy + dy, cx + dx), ()))
            if hit:
                cur.append(j)
                close(True)
        cur.append(j)
        grid.setdefault(cell(*p, 25.0), []).append(j)
    close()
    return out, s


def _seg(pts, kind, full=False):
    L = sum(metres(a, b) for a, b in zip(pts, pts[1:]))
    a, b = pts[0], pts[min(len(pts) - 1, 3)] if kind == "loop" else pts[-1]
    key = f"{kind}:{a[0]:.4f},{a[1]:.4f}:{pts[-1][0]:.4f},{pts[-1][1]:.4f}:{L:.0f}"
    return {
        "id": hashlib.sha1(key.encode()).hexdigest()[:10],
        "kind": kind, "full": full, "m": L,
        "bearing": srv.bearing(a, b),
        "pts": [[round(p[0], 5), round(p[1], 5)] for p in pts],
    }


def generate(tracks):
    """tracks: [(aid, [(lat, lon) | None])] -> list of segment dicts."""
    counts = {}
    for _, pts in tracks:
        seen = set()
        for p in pts:
            if p is not None:
                cy, cx = cell(*p)
                seen.update((cy + dy, cx + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        for c in seen:
            counts[c] = counts.get(c, 0) + 1

    def popular(p):
        return counts.get(cell(*p), 0) >= POP_MIN

    pops = {aid: [p is not None and popular(p) for p in pts] for aid, pts in tracks}
    order = sorted(tracks, key=lambda tr: -sum(pops[tr[0]]))
    segs, cover = [], Cover()
    for aid, pts in order:
        if len(segs) >= MAX_SEGS or not any(pops[aid]):
            break
        head = headings(pts)
        runs, s = _runs(pts, head, pops[aid])
        for idx, closed in runs:
            # drop already-covered points, keep the uncovered sub-runs
            subs, cur = [], []
            for j in idx:
                if cover.covered(pts[j], head[j]):
                    if cur:
                        subs.append(cur)
                    cur = []
                else:
                    cur.append(j)
            if cur:
                subs.append(cur)
            for sub in subs:
                L = s[sub[-1]] - s[sub[0]]
                is_loop = closed and len(subs) == 1 and len(sub) == len(idx)
                new = []
                if is_loop:
                    if L >= LOOP_MIN_M:
                        new.append(_seg([pts[j] for j in sub], "loop"))
                elif L >= SEG_MIN_M:
                    n = max(1, round(L / SEG_TARGET_M))
                    cuts = [s[sub[0]] + L * k / n for k in range(n + 1)]
                    for k in range(n):
                        piece = [j for j in sub if cuts[k] - 1e-6 <= s[j] <= cuts[k + 1] + 1e-6]
                        if len(piece) >= 2:
                            new.append(_seg([pts[j] for j in piece], "line"))
                    if n > 1 and L <= SEG_FULL_MAX_M:
                        new.append(_seg([pts[j] for j in sub], "line", full=True))
                for sg in new:
                    if len(segs) < MAX_SEGS:
                        segs.append(sg)
                cover.add([pts[j] for j in sub], [head[j] for j in sub])
    return segs + _chains(segs)


def _chains(segs):
    """Join pieces that continue each other (same direction) into one segment.

    Pieces come from different source sessions, so a whole out leg is often
    several pieces; this gives it back as one "full" segment.
    """
    lines = [s for s in segs if s["kind"] == "line" and not s["full"]]

    def nxt(a):
        h = headings(a["pts"])
        ha = next(x for x in reversed(h) if x)
        for b in lines:
            hb = next(x for x in headings(b["pts"]) if x)
            if b is not a and metres(a["pts"][-1], b["pts"][0]) <= CHAIN_R and ha[0] * hb[0] + ha[1] * hb[1] >= ALIGN_COS:
                return b
        return None

    succ = {a["id"]: nxt(a) for a in lines}
    has_pred = {b["id"] for b in succ.values() if b}
    out, have = [], {(tuple(s["pts"][0]), tuple(s["pts"][-1])) for s in segs if s["full"]}
    for a in lines:
        if a["id"] in has_pred:
            continue
        chain, seen = [a], {a["id"]}
        while succ[chain[-1]["id"]] and succ[chain[-1]["id"]]["id"] not in seen:
            chain.append(succ[chain[-1]["id"]])
            seen.add(chain[-1]["id"])
        if len(chain) < 2:
            continue
        # every contiguous run of >= 2 pieces: sessions turn around at
        # different points, so "home -> turnaround" and "turnaround -> home"
        # each start or end mid-chain
        for i in range(len(chain)):
            pts = [tuple(p) for p in chain[i]["pts"]]
            for k in range(i + 1, len(chain)):
                pts += [tuple(p) for p in chain[k]["pts"][1:]]
                if sum(x["m"] for x in chain[i:k + 1]) > SEG_FULL_MAX_M:
                    break
                if (pts[0], pts[-1]) in have:
                    continue
                have.add((pts[0], pts[-1]))
                sg = _seg(list(pts), "line", full=True)
                sg["parts"] = [x["id"] for x in chain[i:k + 1]]
                out.append(sg)
    return out


# ------------------------------------------------------------------ matching


def _prep(seg, geo):
    lat0, lon0, kx, ky = geo
    P = [((p[1] - lon0) * kx, (p[0] - lat0) * ky) for p in seg["pts"]]

    def unit(a, b):
        hx, hy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(hx, hy) or 1
        return hx / L, hy / L

    k = min(2, len(P) - 1)
    checks, acc, last = [], 0.0, None
    for i, p in enumerate(P):
        if i:
            acc += math.hypot(p[0] - P[i - 1][0], p[1] - P[i - 1][1])
        if last is None or acc - last >= CHECK_M:
            checks.append(p)
            last = acc
    return {"g0": P[0], "h0": unit(P[0], P[k]), "g1": P[-1], "h1": unit(P[-1 - k], P[-1]),
            "checks": checks}


def _crossings(R, head, grid, g, h):
    gx, gy = g
    ix, iy = int(gx // GATE_R), int(gy // GATE_R)
    near = sorted(j for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                  for j in grid.get((ix + dx, iy + dy), ())
                  if head[j] and head[j][0] * h[0] + head[j][1] * h[1] >= 0.7
                  and math.hypot(R[j][0] - gx, R[j][1] - gy) <= GATE_R)
    if not near:
        return []
    groups, cur = [], [near[0]]
    for j in near[1:]:
        if j - cur[-1] <= 3:
            cur.append(j)
        else:
            groups.append(cur)
            cur = [j]
    groups.append(cur)
    out = []
    for grp in groups:
        for j in range(max(0, grp[0] - 1), min(len(R) - 1, grp[-1] + 1)):
            p, q = R[j], R[j + 1]
            ap = (p[0] - gx) * h[0] + (p[1] - gy) * h[1]
            aq = (q[0] - gx) * h[0] + (q[1] - gy) * h[1]
            if ap < 0 <= aq:
                f = -ap / (aq - ap) if aq != ap else 0.0
                out.append((p[2] + (q[2] - p[2]) * f, p[4] + (q[4] - p[4]) * f, j))
                break
    return out


def match(rows, t, d, R, geo, segs):
    """All efforts of one session on the given segments: {seg_id: [effort]}."""
    head = []
    n = len(R)
    for j in range(n):
        a, b = R[max(0, j - 2)], R[min(n - 1, j + 2)]
        hx, hy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(hx, hy)
        head.append((hx / L, hy / L) if L > 5 else None)
    grid, fgrid = {}, {}
    for j, p in enumerate(R):
        grid.setdefault((int(p[0] // GATE_R), int(p[1] // GATE_R)), []).append(j)
        fgrid.setdefault((int(p[0] // FOLLOW_R), int(p[1] // FOLLOW_R)), []).append(j)

    def follows(cp, j0, j1):
        ix, iy = int(cp[0] // FOLLOW_R), int(cp[1] // FOLLOW_R)
        return any(j0 - 1 <= j <= j1 + 1 and math.hypot(R[j][0] - cp[0], R[j][1] - cp[1]) <= FOLLOW_R
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in fgrid.get((ix + dx, iy + dy), ()))

    out = {}
    for seg in segs:
        g = _prep(seg, geo)
        L = seg["m"]
        starts = _crossings(R, head, grid, g["g0"], g["h0"])
        if not starts:
            continue
        ends = starts if seg["kind"] == "loop" else _crossings(R, head, grid, g["g1"], g["h1"])
        last_s, effs = -1e18, []
        for t0, s0, j0 in starts:
            if s0 < last_s - 1:
                continue
            for t1, s1, j1 in ends:
                ds = s1 - s0
                if ds < 0.8 * L:
                    continue
                if ds > 1.3 * L:
                    break
                if all(follows(cp, j0, j1) for cp in g["checks"]):
                    mv, hr, _ = srv._seg_stats(rows, t, d, t0, t1)
                    effs.append([round(t0, 1), round(t1 - t0, 1), round(mv, 1),
                                 round(hr) if hr else None])
                    last_s = s1
                break
        if effs:
            out[seg["id"]] = effs
    return out


def _near_cells(p):
    cy, cx = cell(*p)
    return {(cy + dy, cx + dx) for dy in (-2, -1, 0, 1, 2) for dx in (-2, -1, 0, 1, 2)}


# ------------------------------------------------------------------ store


class SegmentStore:
    def __init__(self, db_path, metrics, path):
        self.db_path, self.M, self.path = db_path, metrics, path
        self.lock = threading.Lock()
        self.data, self.jobs = {}, {}
        try:
            with open(path) as f:
                blob = json.load(f)
            if blob.get("version") == SEG_VERSION:
                self.data = blob["types"]
        except (OSError, ValueError, KeyError):
            pass

    def _key(self, db, typ):
        ids = [tuple(r) for r in db.execute(
            "SELECT id, samples FROM activities WHERE type=? AND has_gps "
            "AND parse_error IS NULL ORDER BY id", (typ,))]
        h = hashlib.sha1(f"{SEG_VERSION}:{srv.METRICS_VERSION}:{ids}".encode()).hexdigest()
        return h, ids

    def get(self, db, typ):
        key, _ = self._key(db, typ)
        cur = self.data.get(typ)
        if cur and cur.get("key") == key:
            return {"status": "ready", "segments": cur["segments"]}
        with self.lock:
            job = self.jobs.get(typ)
            if not job or not job["running"]:
                job = self.jobs[typ] = {"running": True, "phase": "starting", "done": 0, "total": 0}
                threading.Thread(target=self._compute, args=(typ, job), daemon=True).start()
        return {"status": "computing", **{k: job[k] for k in ("phase", "done", "total")}}

    def _compute(self, typ, job):
        db = srv.connect(self.db_path)
        try:
            key, ids = self._key(db, typ)
            job["phase"] = "tracks"
            ms = self.M.get_many(db, ids)
            tracks = [(aid, decode(ms[aid]["trk"])) for aid, _ in ids if ms.get(aid, {}).get("trk")]
            job["phase"] = "finding segments"
            segs = generate(tracks)
            for sg in segs:
                sg["efforts"] = []
                sg["_c0"], sg["_c1"] = _near_cells(sg["pts"][0]), _near_cells(sg["pts"][-1])
            job.update(phase="matching sessions", total=len(tracks))
            for i, (aid, pts) in enumerate(tracks):
                job["done"] = i
                cells = {cell(*p) for p in pts if p is not None}
                cand = [sg for sg in segs if cells & sg["_c0"] and cells & sg["_c1"]]
                if not cand:
                    continue
                rows, t, d = srv.load_track(db, aid)
                R, geo = srv._resample(rows, t, d)
                if not R:
                    continue
                for sid, effs in match(rows, t, d, R, geo, cand).items():
                    sg = next(s for s in cand if s["id"] == sid)
                    sg["efforts"] += [[aid, *e] for e in effs]
            for sg in segs:
                del sg["_c0"], sg["_c1"]
            # segments nobody but their source session rides aren't useful
            segs = [sg for sg in segs if len({e[0] for e in sg["efforts"]}) >= 3]
            with self.lock:
                self.data[typ] = {"key": key, "segments": segs}
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"version": SEG_VERSION, "types": self.data}, f)
                os.replace(tmp, self.path)
        finally:
            job["running"] = False
            db.close()
