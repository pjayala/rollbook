# Rollbook

Local analysis of my training history, exported from Strava (which in turn
receives it from a HUAWEI WATCH GT 6 Pro, plus ROUVY/MyWhoosh for indoor
cycling).

Everything is offline and stdlib-only (pywebview for `rollbook app` is the one
optional dependency, kept in `./.venv`). There is no API access — see
[Why no API](#why-no-api). The network exceptions are OSM basemap tiles,
OSM Nominatim reverse geocoding for segment names and Open-Meteo historical
weather, all cached forever in `data/cache/` (`./rollbook web --offline`
turns them off), plus the opt-in `rollbook strava sync` (strava.com with your
browser session cookie, see [Strava sync](#strava-sync)).

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
tests/                  unittest suite (parsing, logic, importer, Strava sync, app, weather, laps, FTP, MCP, LAN/TLS/QR, end-to-end API); run in CI
tools/demo.py           synthetic demo athlete (export + DB + seeded caches), never real data
tools/screenshots.py    regenerates docs/screenshots/*.png from the demo, headless Chrome, --offline
CHANGELOG.md            release notes; `rollbook --version` = __version__ in bin/rollbook
samples/*.gpx           two hand-checked rides used as regression fixtures
web/server.py           `rollbook web`: stdlib HTTP server + JSON API, read-only DB
web/segments.py         auto-generated segments + effort matching
web/profile.py          athlete profile (data/profile.json) + per-sport calorie models
web/weather.py          historical weather per outdoor session (Open-Meteo, cached)
web/geonames.py         segment place names (Nominatim reverse geocoding, cached)
web/importer.py         Import dialog backend: uploads + background db build / enrich
web/edits.py            session edits (data/edits.json) as a TEMP view overlay on the read-only DB
web/lastimport.py       what the last import added (data/last_import.json), for the new-records summary
web/tls.py              self-signed cert for `web --lan` (openssl, else pure-Python RSA + DER)
web/qr.py               stdlib QR encoder (byte mode, level M, v1-10) for the --lan link
web/app.py              `rollbook app`: desktop window (pywebview / Chrome-Edge --app / browser)
web/mcp_server.py       `rollbook mcp`: read-only MCP tools over stdio JSON-RPC
web/strava_web.py       `rollbook strava`: session-cookie sync of new activities from strava.com
opencode.json           registers `./rollbook mcp` as the `rollbook` MCP server
web/static/             index.html / app.js / map.js / compare.js / style.css, vanilla JS + SVG/canvas, no CDN
web/static/icon.svg     app icon master; favicon.svg is its 16-32 px simplification
web/static/inter-*.woff2 Inter variable font (Latin subset, OFL, Inter-OFL.txt), served locally
assets/                 rendered icons: rollbook-{128..1024}.png, rollbook.icns, rollbook.ico
NOTES.md                analysis findings so far
```

## Commands

| command | what it does |
|---|---|
| `db build <archive.zip\|dir>` | import an export; incremental by filename, `--fresh` to rebuild, `--no-samples` to skip per-second rows |
| `db enrich [--windows 5,10,20] [--missing]` | compute `best<N>min_kmh` from the samples table. **Run after every build**; `--missing` only does activities that lack them |
| `web [--port 8765] [--no-open] [--offline]` | localhost dashboard: progress, calendar, circuits, session list, map + heatmap, segments + leaderboards, per-session map/splits/laps/segments/HR zones, race predictor |
| `web --lan [--lan-write] [--lan-port 8766] [--lan-http] [--new-token] [--new-cert] [--qr auto\|always\|never]` | also serve it to a phone on the network: HTTPS (self-signed), token link / QR, read-only unless `--lan-write` |
| `app [--mode auto\|webview\|chrome\|browser] [--port] [--offline]` | dashboard in its own window; server stops when the window closes |
| `db query "SQL"` | read-only query, `--csv` for machine output |
| `db schema` | tables, views, row counts |
| `analyze <file\|id>` | one activity: pauses, per-km splits, out/back legs, grade |
| `compare <a> <b> ...` | side-by-side summary table |
| `mcp [--allow-gps]` | read-only MCP server on stdio (`web/mcp_server.py`), registered in `opencode.json` |
| `list <archive>` / `trend <archive>` | bulk views straight off an archive, `-t type --since --until --by week\|month\|year` |
| `streams <file>` | which data channels a file actually carries |
| `strava auth\|sync\|status\|logout` | sync new activities from strava.com with the browser session cookie, see [Strava sync](#strava-sync) |
| `login` / `whoami` / `activities` | Strava API mode — unusable, see below |

Accepts `.gpx`, `.tcx`, `.fit`, each optionally `.gz`, or a numeric Strava
activity id. Format is detected from the extension, not guessed.

## Importing a new export

1. Strava → Settings → My Account → *Download or Delete Your Account* →
   "Request your archive". Email arrives within a few hours.
2. `mv ~/Downloads/export_*.zip exports/export_<athleteid>_$(date +%F).zip`
3. `./rollbook db build exports/<file>.zip && ./rollbook db enrich --missing`

Between exports, `./rollbook strava sync` fetches just the new activities
(see [Strava sync](#strava-sync)).

Single activities: on Strava, an activity's ⋯ menu → **Export Original**
gives the file as the watch recorded it (HUAWEI: `Outdoor_cycle.fit`, no
`sport` message — the sport comes from the `session` message, and HUAWEI
labels skating "cycling", so the Import dialog's sport choice wins). A zip of
such files is accepted too. `db build` skips an activity whose start is
within 2 min and distance within 5% of one already imported; if the newcomer
has `activities.csv` metadata and the existing copy doesn't, it replaces it.

Or from the dashboard: **Import** (or drag files onto the page). It takes the
export `.zip` (stored in `exports/`, renamed `-2`, `-3`… if a different file
has the same name, since archives are reused by stem) and/or single
`.fit/.gpx/.tcx(.gz)` files (stored in `data/imports/activities/`, likewise
`-2`, `-3`… for same name / other content — HUAWEI names every
"Export Original" `Outdoor_cycle.fit` — with a row
in `data/imports/activities.csv` holding the sport picked in the dialog and a
name from the file name; `data/imports` is then built like an export). The
build and `enrich --missing` run as a subprocess (enrich whenever the set of
activity ids changed, so a dedup replacement is enriched too); the page polls
`GET /api/import` and reloads its data when done. The dashboard also works on
a DB that was never enriched (`best<N>min_kmh` are optional columns, NULL).
Upload/run are POSTs that
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
* `watts_avg` / `watts_max` — power recorded in the file (FIT record field 7,
  TCX `ns3:Watts`, GPX `<power>`), mean of the readings with 0 W (coasting)
  included. On the 87 Virtual Rides: 81 within 1 W of Strava's
  `s_avg_watts`, all within 3 W. Strava's `s_max_watts` is always empty.
* `f_avg_watts`, `f_max_watts`, `f_np`, `f_if`, `f_tss` — the FIT session
  message's own power summary (ROUVY writes it; fields 20/21/34/36/35).
  The file doesn't hold the FTP the app used, so `f_ftp = f_np / f_if`
  (ROUVY: 147 W in May → 195 in July → 185–188 W since September).
* `cad_avg` / `cad_max` (0 rpm left out), `kmh_max` (device speed channel).
* Columns added later (`MIGRATIONS` in `bin/rollbook`) are `ALTER TABLE`d into
  an older DB by `db build`; rows imported before stay NULL until
  `db build --fresh`. Code reading them must cope with the column missing
  (`server.has_col`).

`splits` — per kilometre: `moving_s`, `pause_s`, `kmh`, `hr`.

`samples` — per second: `lat`, `lon`, `ele`, `hr`, `cad`, `dist_m`, `watts`
(NULL = not recorded, 0 = a real 0 W). 4.1M rows, indexed on `(activity_id, t)`.

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
* **Power** (`watts_avg`, best 5 s … 60 min, NP) — indoor cycling only (smart
  trainer), and the single most reliable fitness signal in the dataset. On
  outdoor rides `s_avg_watts` is Strava's *estimate*, not a measurement.

Never compare `Virtual Ride` speeds to outdoor: ROUVY/MyWhoosh simulate
gradients, so `best10min_kmh` of 47–58 km/h are simulated descents.

## Web dashboard

Look: dark, one neutral ground (#0a0a0a) with panels one lightness step up, no
borders or shadows at rest; Inter with tabular figures; a sidebar (icon rail
≤ 1100 px, bottom bar ≤ 720 px). Colour is on data only: `PAL` in app.js / the
tokens in style.css. One hue (the wheel's orange) for the main series, white for
its trend, greys for context, rose (`PAL.hr`) only for heart rate; speed, zones
and the heatmap are ramps of those hues, not rainbows.

Icons: edit `web/static/icon.svg` / `favicon.svg`, then re-render `assets/`
(PNGs via headless Chrome, `iconutil` for `.icns`, `.ico` = PNG entries with
favicon art at 16-64 px) and copy `icon-256.png`, `favicon-32.png`,
`favicon.ico` into `web/static/`.

`./rollbook web` binds to 127.0.0.1 only and opens the DB read-only. Every
request on that listener, GET included, needs `Host: 127.0.0.1:<port>` or
`localhost:<port>` (else 403): a DNS-rebinding page would otherwise read the
whole API, GPS tracks included, as same-origin. Metrics the
DB doesn't hold — best 30/45/60 min, out/back legs, **wind-neutral** speed,
route kind, HR histogram — are derived from `samples` in a background thread
on first start (~1 min for all activities) and cached in
`data/cache/web_metrics.json`. Entries invalidate when an activity's sample
count changes or `METRICS_VERSION` in `web/server.py` is bumped; bump it after
changing `derive()`.

* **`web --lan` / `--lan-write`** (phone access): the loopback listener is
  unchanged. A second one on `0.0.0.0:8766` (`--lan-port`) runs the same
  handler with `make_handler(..., lan=Lan)`. It is HTTPS by default
  (`TLSServer`: handshake in the request thread, 60 s timeout, so a stalled
  or plain-HTTP client can't block accept). On that listener:
  * Every request passes `_lan_gate`. Host must be in `lan.hosts` (LAN IPs
    from `lan_ips()`, `<host>.local`, localhost, 127.0.0.1, with the port).
  * `GET ?t=<token>` sets the `rb_lan` cookie (HttpOnly, SameSite=Lax,
    Secure) and 302s to the URL without `t`. Otherwise the cookie must match.
    Comparisons use `hmac.compare_digest`. The token is never accepted on a
    POST.
  * POSTs are 403 unless `--lan-write`; `/api/bye` is always 403.
    `_same_origin` compares Origin with `<scheme>://<Host>`.
  * `/api/status` carries `readonly`; `app.js` then hides import / edit /
    stars / profile save (`body.readonly`).
  * The token is in `data/lan_token` (0600), `--new-token` rotates it.
  * Cert (`web/tls.py`): self-signed leaf, no CA, in `data/tls/`
    (dir 0700, key 0600). It's made with `openssl req -x509` and a generated
    config, else pure Python: Miller–Rabin keygen from `secrets`, PKCS#1 v1.5
    SHA-256, hand-written DER. Both have the same profile, enforced by
    `tests/test_tls.py` via a real verified handshake. It's reused until the
    SAN set changes or < 30 days remain; `--new-cert` forces a new one.
  * The banner prints per-IP URLs, the SHA-256 fingerprint and a QR code
    (`web/qr.py`, module-for-module equal to segno 1.6.6 at full capacity,
    golden hashes in `tests/test_qr.py`; `--qr auto` only on a TTY, `##`
    fallback when stdout can't encode `▀▄█`). `rollbook app` never uses
    `--lan`.

* **`rollbook app`** runs the same server in a thread and opens a window: pywebview
  if importable, else a Chromium-family browser (Chrome, Edge, Chromium, Brave)
  in `--app` mode with its own profile in `data/cache/app-profile`, else a
  tab. It stops when the window closes: `webview.start()` returns, the browser
  process exits (Windows/Linux), or — macOS, where Chrome outlives its window
  — the page's `pagehide` beacon (`POST /api/bye?app=<token>`, same-origin
  `Origin` header) isn't followed by a reload within 4 s. The token is random
  per launch (`?app=<token>` on the window URL): only that launcher's beacon
  counts, and only a page load carrying its token is a reload, so a second
  `rollbook app` reusing the server can close its window safely. If the
  port already answers `/api/status`, it just opens a window on that server.
  Window storage (segment names, settings) is per engine/profile, so it is
  not shared with your normal browser.
* **Wind-neutral** = mean of the best 10 min on the out leg and the back leg,
  split at the point farthest from start. Only for `out-back` routes
  (turnaround ≥ 50% of half the distance, finish within 1 km of start).
  `asym_pct` = out vs back moving speed; the home route's −0.15% grade alone
  gives 0–5%, more than that is wind.
* **Calories** (`web/profile.py`): Rollbook's own estimate everywhere;
  Strava's/the watch's `s_calories` is shown only as a reference (without HR,
  Strava's skate figure is ~2× the measured rate). Per sport: indoor rides
  `avg watts × time ÷ 24%`, recorded `watts_avg` for any ride, else
  Strava's `s_avg_watts` indoors (outdoor rides' `s_avg_watts` is Strava's
  *estimate*, not used); run/walk/hike ACSM equations (speed + uphill grade
  from `ascent_m`); inline skate / outdoor ride Compendium MET by speed;
  others a fixed MET; Keytel HR formula as a cross-check when birth year
  and sex are set. Weight is the current profile weight for every session.
  The **Profile** screen (`#/profile`) edits name, sex, birth year, height,
  weight, resting/max HR; first prefilled from the export's `profile.csv`.
  A profile max HR overrides the recorded one for zones. BMR is
  Mifflin–St Jeor.
* **Weather / conditions** (`web/weather.py`, `conditions()` in server.py):
  Open-Meteo hourly temperature, 10 m wind and gusts per 0.1° cell and UTC
  day (archive API, forecast API `past_days` for the last week), cached in
  `data/cache/weather.json`; never for indoor/Virtual sessions. For
  out-and-backs each leg gets its tailwind component (wind at the leg's
  mid-time along its heading) and its grade (height gain to the turnaround
  as `((turn−start) − (end−turn))/2`, which cancels barometric drift). Slope
  effect is physics at constant power (skate 75 kg, Crr 0.008, CdA 0.35;
  ride 85/0.005/0.32; run 3%/1% grade); wind effect = k × tailwind, with k
  least-squares fitted per sport on the user's own out-and-backs (gap minus
  slope ~ a + k·Δtailwind; skate k ≈ 0.46 %/km/h, physics with model 10 m
  wind would overstate it ~5×). "Adjusted" = each leg's best 10 min ÷
  (1 + wind% + slope%), averaged. Measured on 169 skates: the out/back gap
  (median +11.5%) splits into ~+4.7% wind, ~+6.2% slope (home route
  −0.07…−0.10% out), ~+2% other. Because averaging the legs already cancels
  most of both, adjusted ≈ wind-neutral; the value is the breakdown and
  the per-leg numbers.
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
  being viewed to the OSM tile server. `TileCache` fetches each tile once
  (other requests wait for it) and at most `UPSTREAM` (2) at a time, per the
  OSM tile policy; the User-Agent carries the project URL.
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
* **Enlarge** (corner icon) on every panel with a chart or map: the panel goes
  full-window (Esc / close / backdrop closes) and its charts are redrawn taller;
  `chart()` keeps its options on the host (`_redraw`) and returns a facade
  that delegates to the latest render, so references like `speedChart` and an
  active selection band survive the redraw. Maps just follow the new size.
* **Back / forward** (‹ › in the top bar, Alt+←/→, ⌘[ / ⌘], mouse side
  buttons): pages are `#/…` hash routes, so navigation is normal browser
  history; each entry is numbered in `history.state` to enable/disable the
  buttons (the app window has no browser chrome) and stores the scroll
  position, restored on back/forward.
* **Sidebar** (`sideInit`): the button left of back/forward (or ⌘\ / Ctrl+\)
  hides it, `body.side-hidden`, saved in `localStorage.sideHidden`; charts
  redraw at the new width. Below 720 px it is the bottom bar and never hides.
* **Compare** (`web/static/compare.js`, #16): `#/compare/<subject>/<reference>`,
  reached from **Compare…** on a session (a picker: suggestions, same route via
  `sameRoute()`, then the sport), the **Compare** action in "Same route", or two
  ticks in the Sessions list (the newer is the subject; `sessionStorage.cmpPick`,
  ids only). The subject is orange, the reference white (heart rate rose / a
  muted rose); every change is subject − reference, coloured only where better
  is clear. Lead block: the time gap by distance, `g = t_ref(x) − t_sub(x)`
  (positive = subject ahead), on the **moving** clock (the series' `mv` column:
  cumulative moving s at full resolution, so downsampling doesn't move it) or
  **elapsed**. Distance is made non-decreasing and "time at x km" is the first
  arrival. Per-km splits come from rounded cumulative times, so they add up to
  the headline gap. Starts > 150 m apart: auto-aligned at the first shared
  segment's start (`alignAt`); circuits offer the lap gate. Different virtual
  routes get no gap or splits (simulated speed), best power instead. The
  replay map is fitted once to both tracks and never pans or zooms (no new
  tiles); its rAF loop stops on leaving, on a hidden tab and on navigation.
  GET only, so it works read-only over `--lan`. Pure helpers are tested under
  node in `tests/test_frontend_compare.py`.
* **Page cleanup**: `route()` runs every `onLeave()` callback first (maps,
  replay loops) and bumps `routeSeq`; a view that awaited checks it so a late
  response never draws over the next page. The HTML carries a
  Content-Security-Policy (`CSP` in server.py; no inline scripts anywhere).
* **Favorites** (star toggle on sessions and segments, "favorites only" filters,
  a Favorites panel on the progress page) live in `data/favorites.json`,
  server-side so the app window and browser tabs share them. Starred
  segments store sport/start/end/length; when regenerated segments get new
  ids, `loadSegs` re-attaches the star (and a custom name) to the segment
  whose ends are within 80 m and length within 10%.
* **New records after an import** (#17; `web/lastimport.py`, `newRecords()`
  in app.js): `Importer._work` diffs raw-DB ids before/after and stores the
  new ones, oldest first, in `data/last_import.json`. A new id whose start is
  within 2 min of a removed id's start is a dedup replacement: it is counted
  in `replaced`, not announced. The job dict carries only `new_total` /
  `replaced` / `changed`, never the ids. A first import (empty DB) stores no
  ids (`first`); others store the newest 500. On an error after rows
  committed, the file gets `partial`, and no records are compared.
  Undismissed summaries merge with the next import.
  * Store: lock, in memory (a GET never opens the file), 0600 atomic write
    + fsync, one retry, never raises. On load it's untrusted: ≤ 256 KiB and
    every field type-checked, else ignored.
  * `GET /api/last_import` returns `new:[{id,type}]` through the edits
    overlay.
  * `POST /api/last_import/dismiss {finished}`: the usual
    X-Rollbook/Host/Origin checks, body ≤ 4 KiB. It returns 409 unless
    `finished` matches (a stale tab can't hide a newer import). There is no
    GET form. The MCP server never reads it.
  * The page computes the records from loaded sessions and segments, so
    they follow edits:
    * "all": best of the sport now, strictly above the older best after
      display rounding.
    * "then": beats everything earlier by `datetime` (not `_t`, which is
      noon). Only within 30 days of the newest older session.
    * At least 5 older sessions with the value (laps 3, segments 5 other
      sessions). Virtual sports: power only. Never `kmh_moving`,
      `ascent_m`, `moving_s`.
    * At most 2 segment PRs per session (pieces before Full); the list is
      capped at 10.
  * `RECORD_METRICS` is shared with the records table; `groupCircuits()`
    with the Circuits panel.
  * Every name is `esc()`d; ids select only among API-returned rows.
* **Session edits** (**Edit** on the session page; `web/edits.py`): name,
  sport, hidden, deleted, stored as overrides in `data/edits.json` keyed by
  activity id (Strava's, or the SHA-1-of-path id of an import — both stable,
  so edits survive `db build --fresh`). Each server connection gets a TEMP
  table and a TEMP view named `activities` that **shadows**
  `main.activities` (name/type overridden, hidden rows dropped), so every
  query — lists, sport counts, tracks, segments, HR max, wind fit — sees the
  edits with no further changes; `activities_all` keeps hidden rows plus
  `hidden`/`deleted`/`orig_name`/`orig_type` for the session page and
  `GET /api/activities?hidden=1` ("show hidden"). `EDITS.sync()` refreshes a
  connection's overlay when the edit version changes (once per request).
  `POST /api/edit` needs `X-Rollbook: 1` + same origin like the other
  POSTs. Delete = hide; for a single-file import it can also remove the file
  from `data/imports/activities/` and its `activities.csv` row (the DB row
  stays, restorable, until a fresh build); re-uploading that file un-deletes.
  A sport change drops the session's metrics cache entry. The CLI
  (`db query`, `v_activities`) shows the raw, unedited DB.
* **Power** (`power_grid` etc. in server.py): readings go on a 1 s grid,
  each held until the next one for at most 10 s (a paused trainer stops
  recording, so longer gaps are 0 W). From it: best mean power over 5 s,
  1/5/20/60 min (`pw` in the metrics cache, `pw5s`…`pw60m` in the list) and
  Coggan normalized power (30 s rolling mean, 4th-power mean). The session
  series gets a centred 30 s mean (`watts` column). Also cached: best 30 s /
  30 min, `pw_zero_s` (time at a *recorded* 0 W — the 0 W power_grid puts in
  gaps doesn't count) and `pw_hist` (seconds per 1 W; coarser bins moved
  zone shares by up to 3 points). In the browser, `ftpAt()`: profile `ftp` >
  the latest `f_ftp` up to that ride (a ride's own wins) > 95% of the best
  20 min in the previous 180 days. Power zones are Coggan's 7 levels as % of
  that FTP. The UI says "weighted power / intensity / load" (NP, IF and TSS
  are TrainingPeaks trademarks; named only in notes). Lago di Garda vs
  ROUVY: zones 3/20/38/29/9/1/0 vs 3/19/38/30/9/1/0 %, every best-power
  window equal, zero power 26 vs 25 s, cadence 97/110 equal.
* **Circuits** (progress page) group sessions whose lap *centre* is within
  max(60 m, 25% of lap length) and lap length within 12% — the gate itself
  moves between sessions, the centre doesn't.

## Strava sync

`./rollbook strava sync` (or **Sync from Strava** in the Import dialog)
fetches activities newer than the DB without a full export. No API, no
password: it uses the website's own endpoints with the `_strava4_session`
cookie of a logged-in browser, stored once by `./rollbook strava auth`
(hidden prompt or stdin, accepts the bare value or a whole `Cookie:` header,
checked against strava.com, saved in `data/strava_cookie` 0600). **Never
paste the cookie into a chat**: it is a full login. `strava logout` deletes
it (log out on strava.com to kill the session itself). This is automated use
of the website, which Strava's terms don't allow; it is kept polite.

* `GET /athlete/training_activities?page=N` (XHR JSON, `models` / `total`),
  newest first. Known = ids in the raw DB (hidden ones too) ∪
  `data/imports/activities/strava-<id>.*` ∪ `no_file` in
  `data/strava_sync.json`. Stops at the first page with nothing new (`--all`
  walks every page, `--since YYYY-MM-DD` stops at older ones). Downloads
  oldest first, `--max` (200) per run, so a capped/interrupted run is
  continued by the next one. `--dry-run` only lists.
* `GET /activities/<id>/export_original`, format sniffed from the bytes
  (FIT header / `<gpx` / TrainingCenterDatabase, `.gz` kept). An HTML page or
  404 = no original (manual entry) → remembered in `no_file`.
* Each file goes to `data/imports/activities/strava-<id>.<ext>` with an
  `activities.csv` row (Strava id, start, name, sport — `display_type`, else
  the CamelCase `type` split), so the build gives it **Strava's id** and the
  right sport. Then `db build data/imports` + `enrich --missing`.
* No `s_*` summary columns for synced rows. The next full export has the same
  id and replaces the row (`INSERT OR REPLACE`); afterwards `db build` skips
  any file whose CSV id already has a row with `s_distance_m` under another
  filename ("skipped, already imported from an export"), so rebuilding
  `data/imports` never brings the bare copy back.
* HTTP (`Client`): 1.5 s between requests, 429/5xx back off (Retry-After),
  401/403 or a redirect to `/login` → `AuthExpired` (exit 3; network errors
  exit 4). The cookie is an unredirected header, re-added on a redirect only
  to the same origin. A rotated `_strava4_session` in `Set-Cookie` is saved.
  `ROLLBOOK_STRAVA_BASE` (tests) is honoured only for `http://127.0.0.1|localhost:port`.
* Dashboard: `POST /api/strava/sync` (same `X-Rollbook` / Host / Origin
  checks; refused with `--offline`, without a cookie, and on `--lan` unless
  `--lan-write`) runs `rollbook strava sync --no-build` as a subprocess, so the
  server process never reads the cookie, then the normal import build; the
  page polls `GET /api/import` (`source: "strava"`, `fetched`, `auth`).
  `GET /api/strava` = configured / last sync / auth_expired, never the
  cookie. The MCP server has no sync tool.
* Endpoints are undocumented: all response parsing is `parse_list()` /
  `sniff_ext()`, tested against a local fake in `tests/test_strava.py`.

## MCP server

`./rollbook mcp` (`web/mcp_server.py`) is MCP over stdio, newline-delimited
JSON-RPC, stdlib only; protocol versions 2025-06-18 / 2025-03-26 /
2024-11-05. Tools: `sports`, `schema`, `query`, `activities`, `activity`,
`compare`, `trend`, `records`; the `initialize` instructions carry the
"which metric to trust" rules above. Read-only throughout:

* two `mode=ro` connections. Tools use the trusted one; `query` uses the
  other, under an authorizer allowing only SELECT / functions / recursive
  CTEs / introspection PRAGMAs (no ATTACH, TEMP objects, PRAGMA writes), a
  30 s progress-handler timeout and a row cap. That connection has
  `cached_statements=0` — the authorizer runs at prepare time, so a cached
  statement would skip it.
* `samples.lat` / `lon` read as NULL in `query` (authorizer `SQLITE_IGNORE`)
  and `trk` / `ll0` / lap gate positions are dropped from tool output,
  unless `--allow-gps`. Tool output goes to a model provider.
* dashboard edits (`edits.json`) are applied with the same TEMP overlay as
  `rollbook web`, reloaded when the file's mtime changes.
* `web_metrics.json` is read, never written: a missing/stale entry is
  `derive()`d in memory. `records` uses only cached entries (says how many).

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
| GT 6 Pro `-1 m` filler altitude between real readings | a single "Export Original" file (no Strava ascent to fall back on) got 4332 m of climbing on a flat 26 km skate. `clean_ele` drops the filler and interpolates; the fallback ascent is a 5 m-hysteresis climb of the smoothed profile (median 1.09× Strava's on 133 skates) |
| FIT raw barometric altitude | +799 m of "climbing" on a flat 18 km skate. Hence `ascent_m` prefers Strava's terrain-model value |

Accuracy achieved: recomputed distance is within 1% of Strava's own figure for
**96%** of activities. The residual outliers are 3 genuinely bad tracks (a pool
swim, two 30-second-sampling skates) — find them via `km_diff_pct`.

## Tests

`python3 -m unittest discover -s tests -t tests` (stdlib only, ~10 s, no
network; `tests/helpers.py` points `ROLLBOOK_HOME` at a temp dir). CI:
`.github/workflows/tests.yml` on every push / PR, ubuntu/macos/windows ×
Python 3.9 and 3.13, plus `node --check` on the JS. `tests/helpers.py` also
writes synthetic FIT files (either endianness, optional sport/session
messages), so add a test there when a new device quirk turns up.

Screenshots: `python3 tools/screenshots.py` (needs Chrome) builds the demo
athlete in `$TMPDIR/rollbook-demo` if missing (`tools/demo.py`, ~20 s,
deterministic) and shoots progress / session / segment / map / power pages
at 1440 px wide. Delete that directory after changing `demo.py`. Never
screenshot real data: GPS tracks reveal home.

Releases: bump `__version__` in `bin/rollbook`, add a `## x.y.z (date)`
section to `CHANGELOG.md` (a test checks it exists), tag `vx.y.z` on master.

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

(The website itself is still usable with a session cookie, see
[Strava sync](#strava-sync).) Both remote options are paywalled for this account, verified by probing:

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
