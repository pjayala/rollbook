# Rollbook

Local analysis of my training history, exported from Strava (which in turn
receives it from a HUAWEI WATCH GT 6 Pro, plus ROUVY/MyWhoosh for indoor
cycling).

Everything is offline and stdlib-only (pywebview for `rollbook app` is the one
optional dependency, kept in `./.venv`). There is no API access — see
[Why no API](#why-no-api). The network exceptions are OSM basemap tiles and
OSM Nominatim reverse geocoding for segment names, both cached forever in
`data/cache/` (`./rollbook web --offline` turns both off).

## Quick start

```sh
./rollbook db query "SELECT * FROM v_activities ORDER BY date DESC LIMIT 10"
./rollbook db schema
./rollbook analyze samples/Outdoor_cycle.gpx
./rollbook web                      # dashboard on http://127.0.0.1:8765/
./rollbook app                      # same dashboard in its own window
```

Windows: `rollbook.cmd` instead of `./rollbook` (same behaviour, state in `.\data`).
Optional native window: `python3 -m venv .venv && .venv/bin/pip install pywebview`
(Windows: `.venv\Scripts\pip`; Linux also needs GTK or Qt bindings, e.g.
`pip install pywebview[qt]`). Both wrappers use `.venv`'s Python when it exists.

`./rollbook` is a wrapper that pins all generated state to `./data`. Use it
rather than `bin/rollbook` directly, otherwise state goes to
`~/.local/share/rollbook` (override with `ROLLBOOK_HOME`).

## License

GPL-3.0-or-later, full text in `LICENSE`. Every source file carries an
`SPDX-License-Identifier: GPL-3.0-or-later` header; add it to new files. Map
tiles are © OpenStreetMap contributors (ODbL) and are not part of the repo.

## Layout

```
rollbook / rollbook.cmd  wrappers (sh / Windows): ROLLBOOK_HOME=./data, .venv python if present
bin/rollbook              the whole tool, ~1500 lines, python3 stdlib only
data/rollbook.db          SQLite: 1866 activities, 4.07M samples   (gitignored)
data/cache/archives/    extracted Strava export                  (gitignored)
exports/*.zip           raw Strava account exports, by request date (gitignored)
LICENSE                 GPL-3.0 text
samples/*.gpx           two hand-checked rides used as regression fixtures
web/server.py           `rollbook web`: stdlib HTTP server + JSON API, read-only DB
web/segments.py         auto-generated segments + effort matching
web/geonames.py         segment place names (Nominatim reverse geocoding, cached)
web/importer.py         Import dialog backend: uploads + background db build / enrich
web/app.py              `rollbook app`: desktop window (pywebview / Chrome-Edge --app / browser)
web/static/             index.html / app.js / map.js / style.css, vanilla JS + SVG/canvas, no CDN
web/static/icon.svg     app icon master; favicon.svg is its 16-32 px simplification
assets/                 rendered icons: rollbook-{128..1024}.png, rollbook.icns, rollbook.ico
NOTES.md                analysis findings so far
```

## Commands

| command | what it does |
|---|---|
| `db build <archive.zip\|dir>` | import an export; incremental by filename, `--fresh` to rebuild, `--no-samples` to skip per-second rows |
| `db enrich [--windows 5,10,20] [--missing]` | compute `best<N>min_kmh` from the samples table. **Run after every build**; `--missing` only does activities that lack them |
| `web [--port 8765] [--no-open] [--offline]` | localhost dashboard: progress, calendar, circuits, session list, map + heatmap, segments + leaderboards, per-session map/splits/laps/segments/HR zones, race predictor |
| `app [--mode auto\|webview\|chrome\|browser] [--port] [--offline]` | dashboard in its own window; server stops when the window closes |
| `db query "SQL"` | read-only query, `--csv` for machine output |
| `db schema` | tables, views, row counts |
| `analyze <file\|id>` | one activity: pauses, per-km splits, out/back legs, grade |
| `compare <a> <b> ...` | side-by-side summary table |
| `list <archive>` / `trend <archive>` | bulk views straight off an archive, `-t type --since --until --by week\|month\|year` |
| `streams <file>` | which data channels a file actually carries |
| `login` / `whoami` / `activities` | Strava API mode — unusable, see below |

Accepts `.gpx`, `.tcx`, `.fit`, each optionally `.gz`, or a numeric Strava
activity id. Format is detected from the extension, not guessed.

## Importing a new export

1. Strava → Settings → My Account → *Download or Delete Your Account* →
   "Request your archive". Email arrives within a few hours.
2. `mv ~/Downloads/export_*.zip exports/export_<athleteid>_$(date +%F).zip`
3. `./rollbook db build exports/<file>.zip && ./rollbook db enrich --missing`

Single activities: on Strava, an activity's ⋯ menu → **Export Original**
gives the file as the watch recorded it (HUAWEI: `Outdoor_cycle.fit`, no
`sport` message — the sport comes from the `session` message, and HUAWEI
labels skating "cycling", so the Import dialog's sport choice wins). A zip of
such files is accepted too. `db build` skips an activity whose start is
within 2 min and distance within 5% of one already imported; if the newcomer
has `activities.csv` metadata and the existing copy doesn't, it replaces it.

Or from the dashboard: **⇪ Import** (or drag files onto the page). It takes the
export `.zip` (stored in `exports/`, renamed `-2`, `-3`… if a different file
has the same name, since archives are reused by stem) and/or single
`.fit/.gpx/.tcx(.gz)` files (stored in `data/imports/activities/`, with a row
in `data/imports/activities.csv` holding the sport picked in the dialog and a
name from the file name; `data/imports` is then built like an export). The
build and `enrich --missing` run as a subprocess; the page polls
`GET /api/import` and reloads its data when done. Upload/run are POSTs that
need an `X-Rollbook: 1` header, a 127.0.0.1/localhost `Host` and no foreign
`Origin` (CSRF + DNS rebinding). Files without a CSV id get a stable id from
a SHA-1 of their path (it used to be Python's salted `hash()`).

`db build` refuses a directory without `activities.csv`: building from a
parent folder once re-imported all 1866 activities a second time under a
longer path, typed by FIT sport name (`inline_skating`, `running`, `generic`…)
and with no Strava metadata.

Builds are incremental: already-imported `filename`s are skipped, so a fresh
export only costs the new activities. Full rebuild of 1866 activities is ~60 s.

Archives extract to `data/cache/archives/<zip stem>`. Keep the stem stable or
you pay a 310 MB re-extract.

## Database

`activities` — one row per activity.

* `s_*` columns are Strava's own summary, parsed from `activities.csv`
  verbatim. Note that CSV has **duplicate headers**; the parser deliberately
  takes the *last* occurrence, which is the detailed block (metres and m/s,
  not kilometres and rounded).
* `km` and `ascent_m` prefer Strava's figure and fall back to the track.
* `km_calc` / `ascent_calc` are always recomputed here; `km_diff_pct` is the
  disagreement between the two. `ORDER BY ABS(km_diff_pct) DESC` surfaces bad
  tracks.
* `moving_s`, `pause_s`, `best30_kmh`, `best<N>min_kmh` and everything in
  `splits` exist **only** here — Strava's export does not contain them.

`splits` — per kilometre: `moving_s`, `pause_s`, `kmh`, `hr`.

`samples` — per second: `lat`, `lon`, `ele`, `hr`, `cad`, `dist_m`.
4.07M rows, indexed on `(activity_id, t)`.

`v_activities` — convenience view with pre-formatted km / moving / kmh /
`kmh_per_bpm`.

## Metrics: which to trust

This matters more than anything else here. Pick the wrong speed column and you
will measure the equipment, not the athlete.

* **`best10min_kmh` (and `best5`/`best20`) — use this for comparing form over
  time.** Peak distance in any rolling N-minute window. Immune to how much the
  athlete stopped *and* to sample spacing, because it reads cumulative
  distance over a fixed time span. Requires `db enrich`.
* **`kmh_moving`** — `km / moving_s`, pauses excluded at a 0.5 m/s threshold.
  Fine within an era, **not comparable across 2025-08**: sample rate dropped
  from ~1 Hz to ~0.16 Hz then and measured pause fraction jumped 7.6% → 16.4%,
  which inflates later speeds. This is also why this number differs slightly
  from the Strava app's — different pause definition, neither is "wrong".
* **`kmh_elapsed`** — no pause logic at all, but it tracks real-world stopping
  (traffic lights, photos), so it measures the route, not fitness.
* **`hr_avg`** — only present on 16 skates (the GT 6 Pro arrived 2026-09-08)
  and on the indoor rides. There is no long-run HR trend for skating.
  `best10min_kmh / hr_avg` is the efficiency metric of choice when HR exists.
* **`s_avg_watts`** — indoor cycling only, and the single most reliable
  fitness signal in the dataset.

Never compare `Virtual Ride` speeds to outdoor: ROUVY/MyWhoosh simulate
gradients, so `best10min_kmh` of 47–58 km/h are simulated descents.

## Web dashboard

Icons: edit `web/static/icon.svg` / `favicon.svg`, then re-render `assets/`
(PNGs via headless Chrome, `iconutil` for `.icns`, `.ico` = PNG entries with
favicon art at 16-64 px) and copy `icon-256.png`, `favicon-32.png`,
`favicon.ico` into `web/static/`.

`./rollbook web` binds to 127.0.0.1 only and opens the DB read-only. Metrics the
DB doesn't hold — best 30/45/60 min, out/back legs, **wind-neutral** speed,
route kind, HR histogram — are derived from `samples` in a background thread
on first start (~1 min for all activities) and cached in
`data/cache/web_metrics.json`. Entries invalidate when an activity's sample
count changes or `METRICS_VERSION` in `web/server.py` is bumped; bump it after
changing `derive()`.

* **`rollbook app`** runs the same server in a thread and opens a window: pywebview
  if importable, else a Chromium-family browser (Chrome, Edge, Chromium, Brave)
  in `--app` mode with its own profile in `data/cache/app-profile`, else a
  tab. It stops when the window closes: `webview.start()` returns, the browser
  process exits (Windows/Linux), or — macOS, where Chrome outlives its window
  — the page's `pagehide` beacon (`POST /api/bye`, only with `?app=1` and a
  same-origin `Origin` header) isn't followed by a reload within 4 s. If the
  port already answers `/api/status`, it just opens a window on that server.
  Window storage (segment names, settings) is per engine/profile, so it is
  not shared with your normal browser.
* **Wind-neutral** = mean of the best 10 min on the out leg and the back leg,
  split at the point farthest from start. Only for `out-back` routes
  (turnaround ≥ 50% of half the distance, finish within 1 km of start).
  `asym_pct` = out vs back moving speed; the home route's −0.15% grade alone
  gives 0–5%, more than that is wind.
* **Elevation** on the session page: samples with no GPS fix and `ele == 0`
  are placeholders and dropped (`load_track`). The GT 6 Pro writes a real
  altitude only every ~5 s and **-1 m** on every other record (~85% of
  samples, 26 activities); `_ele_placeholder` detects such a filler value
  (-1 or 0, ≥ 20% of samples, the rest > 10 m away) and drops it, and the
  browser interpolates gaps up to 3 min. The profile is a 7-sample
  median then a ±15 s mean. Summed watch climbing runs 2–10× over Strava's
  terrain-model ascent outdoors even after smoothing (measured over ~300
  sessions; only Virtual Ride is close), so the total shown is `ascent_m`,
  and splits / selections show net change and grade, which drift barely
  affects.
* HR is held for 30 s between readings: GT 6 Pro FIT files put HR on its own
  sparse records, so most samples have none.
* HR zones are % of HRmax (max `hr_max` over the last 365 days): 75/84/90/95%.
* `Virtual Ride` is tagged `virtual` and never gets wind metrics or a map track.
* **Maps** (`web/static/map.js`) are a small canvas slippy map, no library.
  Tiles are OSM raster, fetched by the server through `/tiles/osm/z/x/y.png`
  with an identifying User-Agent and cached forever in `data/cache/tiles/`;
  the browser never talks to a third party. "Dark" is the same tiles through
  a CSS filter. CARTO basemaps now need an API key — don't switch back.
  `--offline` serves only cached tiles. Tile requests do reveal which area is
  being viewed to the OSM tile server.
* **Heatmap**: each track adds once per pixel (one `stroke()` per track, so an
  out-and-back doesn't count twice), accumulated in the alpha channel, then
  colour-mapped. Tracks are downsampled to ~15 m and delta-encoded in the
  metrics cache (`trk`); a `(0,0)` delta is a pen-up before a >500 m jump.
* **Laps** (`find_laps`): candidate gates every 80 m along the track; a pass
  is a crossing of the line through the gate, perpendicular to its heading,
  within 25 m and heading-aligned (so out-and-back return legs never count).
  Needs ≥ 3 passes; the gate with the most regular laps wins. A lap is
  *regular* if its GPS length and its odometer km are within ~15–25% of the
  median; *counted* (used for best/median/consistency) if also not dominated
  by a stop. Typical circuits: a ~1.4 km road loop and a
  ~190 m rink. Lap timing is gate-to-gate, so with 6 s
  sampling on a 190 m rink a single lap is only good to a couple of seconds.
* **Segments** (`web/segments.py`, cached in `data/cache/web_segments.json`,
  recomputed per sport when its set of activities changes, ~2–4 s): cells
  (40 m, 3×3-smoothed) that ≥ 5 sessions pass are "usual"; tracks are walked
  most-popular first, cut into runs at U-turns and loop closures, and any
  stretch already covered in the same direction is skipped, so overlapping
  sessions never create offset duplicates. Runs become ~2.5 km pieces;
  consecutive pieces are chained into "Full" segments (every contiguous run of
  ≥ 2 pieces, ≤ 16 km), because sessions turn around at different points.
  Direction is kept: out and back legs are separate segments. An effort is
  start-gate → end-gate (same gate logic as laps) at 80–130% of the segment
  length while staying within 40 m of a checkpoint every 100 m. Ranking is by
  elapsed time, best per session. Segments with < 3 sessions are dropped.
  Segment ids hash their geometry, so they can change when new data
  regenerates them; custom names live in browser `localStorage`.
  **Names**: `web/geonames.py` reverse-geocodes each segment's start, end and
  every ~1 km (≤ 8 points) via Nominatim, 1 req/s, cached in
  `data/cache/geonames.json` on a ~55 m grid. Lines are named "from area →
  to area", loops "<area> loop (<road>)", with the roads listed underneath;
  duplicates get the length appended. The first run for a sport takes a
  couple of minutes (~100 lookups for skating); the page re-polls while
  `names_pending > 0`. Custom names still win.
  "Opposite direction" pairs are matched in the browser by ≥ 70% path overlap
  and a heading difference > 130°.
* **Back / forward** (← → left of the logo, Alt+←/→, ⌘[ / ⌘], mouse side
  buttons): pages are `#/…` hash routes, so navigation is normal browser
  history; each entry is numbered in `history.state` to enable/disable the
  buttons (the app window has no browser chrome) and stores the scroll
  position, restored on back/forward.
* **Favorites** (☆/★ on sessions and segments, "favorites only" filters,
  a Favorites panel on the progress page) live in `data/favorites.json`,
  server-side so the app window and browser tabs share them. Starred
  segments store sport/start/end/length; when regenerated segments get new
  ids, `loadSegs` re-attaches the star (and a custom name) to the segment
  whose ends are within 80 m and length within 10%.
* **Circuits** (progress page) group sessions whose lap *centre* is within
  max(60 m, 25% of lap length) and lap length within 12% — the gate itself
  moves between sessions, the centre doesn't.

## Data-quality landmines

All of these are already handled in `bin/rollbook`; they are recorded because
they were each silently losing or corrupting data, and a rewrite would
reintroduce them.

| issue | effect if unhandled |
|---|---|
| TCX with whitespace/BOM before `<?xml` | 85 files unparseable |
| GPX `trkpt` with no `lat`/`lon` attrs | 30 files crash the parser |
| Samples before first GPS fix — FIT writes `0x7FFFFFFF`, TCX omits `<Position>` | the *entire* GPS channel gets dropped if you require all samples to have a fix: 103 activities wrongly "no distance" |
| FIT files are sometimes **big-endian** | garbage coordinates; endianness is per-definition-message |
| GPS fix lost mid-session (e.g. 134 s under a bridge) | all the missed distance landed on one sample: a 149 km/h step and a best-30 s of 39 km/h on a skate. `cumulative_gps` now spreads it over the no-fix samples |
| Recording gaps | a 65-min gap (London→Cambridge) added 76 km to six 2015-16 runs. Filtered by rejecting >`GAP_MAX_MS` sustained over >`GAP_S` |
| GPS spikes | rejected above `MAX_MS`; null-island `(0,0)` dropped |
| Stuck device odometers | odometer is preferred (96% within 1% of Strava vs 91% for GPS) *unless* it reads <50% of the GPS track |
| Indoor/HR-only workouts | 20 treadmill sessions rejected outright if you demand a distance channel |
| FIT raw barometric altitude | +799 m of "climbing" on a flat 18 km skate. Hence `ascent_m` prefers Strava's terrain-model value |

Accuracy achieved: recomputed distance is within 1% of Strava's own figure for
**96%** of activities. The residual outliers are 3 genuinely bad tracks (a pool
swim, two 30-second-sampling skates) — find them via `km_diff_pct`.

## Regression fixtures

`samples/Outdoor_cycle.gpx` and `samples/The_come_back.gpx` are hand-verified.
Any change to parsing or distance logic must keep these exact:

```
Outdoor_cycle.gpx   18.206 km | elapsed 55:02 | moving 51:08 | pause 3:54 | HR 145 | best30 25.42
The_come_back.gpx   14.040 km | elapsed 40:40 | moving 38:42 | pause 1:58 | HR 154 | best30 27.04
```

Out/back split for `Outdoor_cycle.gpx`: OUT 8.761 km @ 22.53, BACK 9.445 km @ 20.39.

The same ride also exists in the archive as a big-endian FIT
(`activities/21363091415.fit.gz`) and must yield 18.205 km — cross-format
agreement is the strongest check available:

```sh
./rollbook analyze data/cache/archives/export_<athleteid>_<date>/activities/21363091415.fit.gz
```

## Why no API

Both remote options are paywalled for this account, verified by probing:

* `mcp.strava.com/mcp` → `401`, and the issuer's `registration_endpoint`
  only accepts a `client_name` containing "claude", always returning the same
  public `client_id 248572`. Subscriber-only regardless.
* Strava REST API v3 → subscriber-only as of 2026.

So the account-data export (a GDPR right, not paywalled) is the only pipe.
`bin/rollbook` keeps the API code paths (`login`, `whoami`, `activities`, and
numeric-id resolution) wired up in case the account is ever upgraded; they are
dead code today.

Huawei Health alternatives, if ever needed: its GDPR export carries sleep /
SpO2 / resting-HR that Strava never receives, in an undocumented JSON schema
(`Motion path detail data & description.json`). Huawei Health Kit REST needs a
business-verified developer account and is not realistic for personal use.
