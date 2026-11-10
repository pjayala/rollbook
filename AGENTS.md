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
Optional native window: `python3.15 -m venv .venv && .venv/bin/pip install -r requirements-optional.txt`
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
requirements-optional.txt  pywebview, the one optional package (native app window)
tests/                  unittest suite (parsing, logic, importer, Strava sync, app, weather, laps, FTP, MCP, LAN/TLS/QR, end-to-end API); run in CI
tools/demo.py           synthetic demo athlete (export + DB + seeded caches), never real data
tools/screenshots.py    regenerates docs/screenshots/*.png from the demo, headless Chrome, --offline
CHANGELOG.md            release notes; `rollbook --version` = __version__ in bin/rollbook
samples/*.gpx           two hand-checked rides used as regression fixtures
web/server.py           `rollbook web`: wiring + routing (handler, start/serve), read-only DB
web/api.py              JSON API: GET views (`GET` table), POST actions (`POST` table), the stores
web/http_base.py        responses, `_read_json`, Host / same-origin / --lan token checks, CSP
web/analysis.py         derive() and its helpers (efforts, laps, power, tracks) + Metrics cache
web/conditions.py       weather + wind / slope split per out-and-back
web/lanaccess.py        --lan: token, addresses, TLS listener, banner
web/tiles.py            OSM tile proxy + disk cache
web/favorites.py        starred sessions / segments (data/favorites.json)
web/gear.py             gear, parts, defaults, per-session choices (data/gear.json) + totals
web/segments.py         auto-generated segments + effort matching
web/profile.py          athlete profile (data/profile.json) + per-sport calorie models
web/weather.py          `db weather`: Open-Meteo weather + air quality per session hour (cache, fetcher, weather table, reading)
web/geonames.py         segment place names (Nominatim reverse geocoding, cached)
web/importer.py         Import dialog backend: uploads + background db build / enrich
web/edits.py            session edits (data/edits.json) as a TEMP view overlay on the read-only DB
web/store.py            atomic 0600 JSON writes + object-only loads for the small stores in data/
web/lastimport.py       what the last import added (data/last_import.json), for the new-records summary
web/tls.py              self-signed cert for `web --lan` (openssl, else pure-Python RSA + DER)
web/qr.py               stdlib QR encoder (byte mode, level M, v1-10) for the --lan link
web/app.py              `rollbook app`: desktop window (pywebview / Chrome-Edge --app / browser)
web/mcp_server.py       `rollbook mcp`: read-only MCP tools over stdio JSON-RPC
web/strava_web.py       `rollbook strava`: session-cookie sync of new activities from strava.com
opencode.json           registers `./rollbook mcp` as the `rollbook` MCP server
web/static/             index.html / app.js / map.js / compare.js / gear.js / style.css, vanilla JS + SVG/canvas, no CDN
web/static/icon.svg     app icon master; favicon.svg is its 16-32 px simplification
web/static/inter-*.woff2 Inter variable font (Latin subset, OFL, Inter-OFL.txt), served locally
assets/                 rendered icons: rollbook-{128..1024}.png, rollbook.icns, rollbook.ico
NOTES.md                analysis findings so far
```

## Commands

| command | what it does |
|---|---|
| `db build <archive.zip\|dir>` | import an export; incremental by filename, `--fresh` to rebuild, `--no-samples` to skip per-second rows |
| `db weather [--missing] [--offline]` | hourly weather + air quality per outdoor session into the `weather` table (network, cached); `db build` runs `--missing` unless `--no-weather` |
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
  (`analysis.has_col`).

`splits` — per kilometre: `moving_s`, `pause_s`, `kmh`, `hr`.

`samples` — per second: `lat`, `lon`, `ele`, `hr`, `cad`, `dist_m`, `watts`
(NULL = not recorded, 0 = a real 0 W). 4.1M rows, indexed on `(activity_id, t)`.

`weather` — per outdoor session and UTC hour, Open-Meteo model values
(see Weather / conditions); `v_weather` averages them per session.

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
count changes or `METRICS_VERSION` in `web/analysis.py` is bumped; bump it after
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
* **Weather / conditions** (`web/weather.py`, `web/conditions.py`): stored in
  the DB by `db weather` (the last step of a CLI `db build` / `strava sync`
  unless `--no-weather`; the dashboard Import builds with `--no-weather` and
  then runs `db weather --missing` in the background via `OnDemand.backfill`,
  so new sessions never wait on Open-Meteo). `web --offline` or
  `ROLLBOOK_OFFLINE=1` = cache only (tests set the latter). `db weather`
  installs the edits overlay, so edited sports / hidden sessions count like on
  the dashboard; exit 2 when a `--id` got no rows, 3 when the run was cut
  short. Table `weather`:
  one row per outdoor session (has GPS, not Virtual / `NO_WEATHER`) and UTC
  hour from the hour at/before the start to the one at/after the end (≤ 48),
  each at the GPS fix nearest that hour (0.1° cell): temp, feels, rh, dew,
  precip / rain / snow (preceding hour), WMO code, cloud, pmsl, psurf, ws,
  wd (from), gust, and air quality pm25, pm10, o3, European aqi (CAMS, from
  2013, else NULL). No coordinates. `v_weather` = per-session averages.
  Sources per cell and UTC day: archive API (ERA5 / IFS reanalysis; hours it
  lacks in the last 7 days from the forecast API's `past_days`) and the
  air-quality API. Raw days are cached in `data/cache/weather.json`
  (`{"v": 2, "wx"|"aq": {cell: {date: {"at", "h"}}}}`; any other content,
  e.g. the old v1, is dropped), so `db build --fresh` refetches nothing. A
  day fetched within 7 days of itself is provisional: refetched once a day
  until it settles; its rows carry `final = 0` (also when an hour is
  missing) and `--missing` redoes them. A day Open-Meteo had nothing for (or
  answered 4xx / 5xx / garbage) is cached empty: asked again daily while
  recent, else weekly, and given up on 60 days after the day (so a session
  whose gaps are for good becomes `final`); values outside a plausible
  range per column (`BOUNDS`) are stored as NULL; if `edits.json` can't be
  read, bulk runs are skipped (exit 3) so deleted sessions never leak. A
  failed or partial answer never blanks a cached day
  (hours merge, a failure only sets `tried`). `--id` runs stop at a 240 s
  budget and store what they have; deleted sessions are only looked up by
  `--id` (their own page), never in bulk; the dashboard's queued `--id`
  runs go before a backfill. Requests: needed days merged into ranges (≤ 7-day
  gaps, ≤ 62 days), newest first, 1 s apart; 429 / 5xx wait (Retry-After,
  ≥ 30 s, ≤ 60 s for `--id`) and retry 3×; a 5xx range is then skipped; 3
  network errors in a row, a persistent 429 or 5 failed ranges in a row end
  the run (cache saved, the next run continues). Answers > 8 MiB or not
  plain JSON are skipped; cache entries of the wrong shape are dropped on
  load. A first backfill of ~1900 sessions is
  ~530 requests, server latency 0.5–30 s each. `db build` deletes a
  re-imported session's rows. The dashboard (`weather.load` → `summary` /
  `at`: linear between hours, wind direction as a vector, precipitation from
  the covering hour) and MCP (`weather` in `activity`) only read the table.
  Session page: one Weather card; clicking it opens `weatherDetails()` (an
  enlarged panel built for the modal, removed on close via `_onCollapse`)
  from `weather_hours` in `api_activity`: stats, charts on an hour axis with
  the session as the selection band, wind arrows per hour, hourly table.
  Opening a session with no rows (`weather_missing` in `api_activity`: has
  GPS, outdoor) makes the page `POST /api/weather {id}`: `weather.OnDemand`
  runs `rollbook db weather --id <id>…` in a subprocess (the server's DB is
  read-only), one run at a time with later ids batched, each id at most once
  per 10 min; refused with `--offline` and while an import runs (it fills
  them anyway); never from a read-only `--lan` phone. The POST answers
  `none` without a process for indoor / no-GPS sessions and `done` when rows
  exist. The enlarged view is a modal dialog (`role=dialog`, focus on close,
  Tab trapped, focus returns to what opened it). MCP reads `weather.psurf`
  as NULL and drops it from `activity` unless `--allow-gps` (surface vs
  sea-level pressure gives the area's altitude). The page polls
  `GET /api/weather?id=` and redraws once on `done` (once per page load).
  `fill()` commits before fetching (no write lock held during the network
  part), `db weather` waits up to 60 s for the lock, and `Cache.save()`
  merges with the file on disk, so an import and an on-demand run can overlap.
  For out-and-backs each leg gets its tailwind component (wind at the leg's
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
* HR zones are % of HRmax: 75/84/90/95%, **per session as of its date**
  (`profile.HrmaxAt`, `hrmax` / `hrmax_src` in `api_activities` /
  `api_activity`, MCP `hr_zones`): the profile's max HR in effect that day
  (`hr_max_history`: `Profile.set` dates every change, from that day on; a
  value saved before the history existed applies to every earlier date,
  `from: 0000-01-01`; a profile with no history applies `hr_max` everywhere),
  else the **held peak**: the highest bpm a session spent ≥ 30 s at or above
  (its `hr_hist`), best over the `hr_months` (default 12) up to that date,
  the session itself included; else the recorded peak in that window. The
  monthly zone chart labels zones in % since each session has its own max.
  "For all past sessions too" (`hr_max_all`) replaces the history with one
  `from: 0000-01-01` entry. Histograms come from the metrics cache only
  (`get_many(compute=False)`), so these never derive() per request; a cold
  cache falls back to the recorded peak until `warm()` is done.
  `/api/hrmax` is today's value (profile, else the held peak of the
  `hr_months` before the newest HR session) plus the suggestion. The raw
  `hr_max` is often a sensor spike (173 recorded vs 157 held for 10 s on one
  skate). The Profile page suggests it with the sessions behind it, the
  Tanaka age formula as a reference and a "Use" button; MCP uses the same
  choice (cached histograms only).
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
  replay map is fitted to both tracks and never pans or zooms by itself; it
  re-fits only when its size changes (enlarge / close / window resize); its rAF loop stops on leaving, on a hidden tab and on navigation.
  GET only, so it works read-only over `--lan`. Pure helpers are tested under
  node in `tests/test_frontend_compare.py`.
* **Page cleanup**: `route()` runs every `onLeave()` callback first (maps,
  replay loops) and bumps `routeSeq`; a view that awaited checks it so a late
  response never draws over the next page. The HTML carries a
  Content-Security-Policy (`CSP` in http_base.py; no inline scripts anywhere).
* **Gear** (#10; `web/gear.py`, `web/static/gear.js`, `#/gear`,
  `#/gear/<id>`): parts (`KINDS`: kind → label, group, typed specs) are
  fitted to gear by dated mounts `{part, from, to}` — a part counts for a
  session on day D when `from <= D < to` (swap day = the new part), and is on
  at most one gear at a time. A session's gear: its explicit choice
  (`sessions[id]`, null = none), else its (edited) sport's default — the
  `defaults[sport]` entry with the latest `since <= date`, unless that gear was
  retired by then. Totals over the visible sessions (the `activities` view):
  sessions, km, time, first/last per gear and part; `km_since_service` from
  the last service (a session on that day counts after it; never serviced =
  from install plus `km_offset`), due at `>= service_km`, never when retired;
  trainer (Virtual) km don't wear a tyre. Nothing goes into SQLite or the
  metrics cache; `api_activities` / `api_activity` add `gear_id`,
  `gear_name`, `gear_src` (explicit / default), `api_activity` also
  `gear_parts` and `gear_default` (what "Default" gives that day; Strava's own
  `gear` column is untouched). `POST /api/gear {op, rev, ...}` (ops in
  `gear.OPS`): each op runs on a copy, then the whole store is `validate()`d
  — the same check as on load — and written (previous version to
  `gear.json.bak`); a stale `rev` is 409, unknown ids 404, ids are made by
  the server. A `gear.json` that fails to load is never overwritten: writes
  503 until it is fixed or moved. Explicit choices keep `start` / `km` and
  follow a replaced session like edits (`edits.reattach`, importer
  `_remap_all`). The page uses `Map`s for anything keyed by id or sport;
  every user string goes through `esc()`; write controls carry `.gear-w`
  (hidden read-only). Reminder badge: `#gear-badge`. Gear page / detail
  views: `gearCompare()` (by gear for one sport: `best10min_kmh`, or
  `pw20m` for Virtual, never `kmh_moving`; per-heartbeat only with ≥ 3 HR
  sessions), `timelineRows()` (fittings as % bars over [first fitting,
  today]), monthly km from the server (`totals.gear[id].months`), first-run
  rows from `/api/types`; pure helpers are tested under node in
  `tests/test_frontend_gear.py`. Compare: `gearDiff()` in compare.js
  (parts by kind both sessions have, differing part or specs → a
  "Different wheels: …" note; unrecorded on one side ≠ different); the
  picker adds "same route, other gear".
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
  Edits follow a session that an import replaces under a new id (dedup:
  upload, then the export's copy): the dashboard importer calls
  `EDITS.remap({old: new})`; for CLI `db build` / `strava sync` each edit
  keeps the session's `start` and `km`, and when `main.activities` changes
  (`count, total(id), max(id)`), an edit whose id is gone moves to the single
  session starting within 120 s, with km within 5% when both are known (a
  hiding edit needs both), that has no edit (none if two qualify; never a
  `file_removed` one). In memory only, saved with the next edit or the
  importer's `remap`. The importer pairs old/new ids only 1:1. Keys in
  `edits.json` must be ASCII digits (≤ 18).
* **POST routing** (`api.POST`): every action is a `Post(fn, limit, what)`
  entry; `do_POST` alone applies the checks (LAN gate / `--lan-write`, then
  same origin + `X-Rollbook: 1`), reads the JSON body when `limit` is set
  (else validates `Content-Length` for raw actions like the upload), calls
  `fn(api.Req)` → `(status, json)` and maps ValueError/TypeError → 400,
  OSError → 500 "<what> not saved". Add an endpoint there, never a new
  branch in `do_POST`; `tests` iterate `api.POST` for the 403 checks.
  `/api/bye` (a beacon) is the one special case. GETs: `api.GET`.
* **JSON POST bodies** (`_read_json` in http_base.py, every JSON endpoint):
  `Content-Length` 0…limit (64 KiB, dismiss 4 KiB) else 413/400 through
  `_reject` (≤ 1 MiB drained, else the connection closed; nothing is read from
  a LAN client before its token cookie checks out) — never
  `read(min(n, cap))`, which read to EOF on a negative length. No
  `Content-Length` is 411, any `Transfer-Encoding` 501 (the server is
  HTTP/1.0). NaN/Infinity/1e999, nesting too deep and non-objects are 400.
  The upload refuses a negative length too. Sockets time out after 60 s
  (`REQUEST_TIMEOUT_S`). Favorites keep only a validated segment `meta`
  (type, start/end lat-lon, m) and at most `FAV_MAX` per kind; the profile
  file is re-validated against `FIELDS` on load (MCP uses the same check). The stores (`edits`, `profile`, `favorites`,
  `last_import`) write via `store.write_json` (unique O_EXCL/O_NOFOLLOW temp,
  0600, fsync, replace, one retry; ASCII with escapes, so any text
  round-trips) and load via `store.load_object` (anything but a JSON object =
  empty, never a startup crash). Memory changes only after the write: a
  failed save leaves the store as it was, and a deleted import's file is
  removed only once its edit is on disk.
* **Best efforts** (#6, `best_effort()` in analysis.py): the fastest *elapsed*
  time over 400 m, 1 km, 5 km, 10 km, half and full marathon anywhere in a
  session (no 400 m for rides, none for virtual rides), in the metrics cache as
  `be` {label: [s, start t, start m]} and in the list as `be<label>` seconds.
  Exact, not sampled: time as a function of distance is piecewise linear, so
  the minimum of T(x + D) − T(x) sits where x or x + D is a sample's distance;
  a pause starts the window when moving on and ends it at the first arrival.
  Equal timestamps collapse to the last sample; a window holding a step faster
  than 30 m/s (a spike) is skipped. Shown on the session page (rank all time,
  click to select the stretch with its exact time), in Personal records (lower
  is better, `low` in `RECORD_METRICS`, also for new records after an import),
  as sortable list columns (first click fastest first), as a Progress trend
  (per-session speed plus the record progression) and in compare. 400 m / 1 km
  read slower on 6 s recording than on 1 Hz.
* **Power** (`power_grid` etc. in analysis.py): readings go on a 1 s grid,
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
  cookie: `has_cookie` only stats the file, the server never opens it.
  `save_cookie` writes via `store.write_bytes` (unique O_EXCL|O_NOFOLLOW
  temp, 0600). The MCP server has no sync tool.
* Endpoints are undocumented: all response parsing is `parse_list()` /
  `sniff_ext()`, tested against a local fake in `tests/test_strava.py`.

## MCP server

`./rollbook mcp` (`web/mcp_server.py`) is MCP over stdio, newline-delimited
JSON-RPC, stdlib only; protocol versions 2025-06-18 / 2025-03-26 /
2024-11-05. Tools: `sports`, `schema`, `query`, `activities`, `activity`,
`compare`, `trend`, `records`, `gear`; the `initialize` instructions carry the
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
* `gear` tool (and `gear` in `activity`): `gear.json` loaded with the
  dashboard's own validation, reloaded on mtime, never written (no remap /
  commit is ever called). Totals use the edited `activities` view. Notes,
  service notes and purchase dates are left out (tool output goes to a model
  provider); `query` can't see gear (it isn't in SQLite).
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

## Adding a feature

* **Where it goes.** Parsing, import, CLI subcommands: `bin/rollbook`. What the
  dashboard or MCP reads: a module in `web/`, one concern per file
  (`weather.py`, `gear.py`). Routing only in `server.py`; JSON views in
  `api.GET`, actions as `Post(fn, limit, what)` in `api.POST`. UI in
  `web/static/` (vanilla JS, no CDN, no inline script). SPDX header on new
  files.
* **One predicate, one place.** A rule like "does session X get Y" (outdoor,
  visible, eligible) is one function in the owning module, called by the CLI,
  `api.py` and `mcp_server.py` alike; never re-spelt as SQL in one place and
  Python in another.
* **CLI and dashboard agree.** CLI work on sessions installs the edits overlay
  first (`edits.Edits(HOME).sync(db)`), so edited sports and hidden / deleted
  sessions count the same. An unreadable store (`Edits.error`) = refuse the
  bulk run, not "no edits".
* **Schema.** New columns go into `MIGRATIONS`; readers cope with the column
  missing (`analysis.has_col`) and NULL on older rows. New tables: `CREATE
  TABLE IF NOT EXISTS` where they are written; readers catch
  `sqlite3.OperationalError` on an older DB.
* **Derived metrics.** Changing `derive()` or what it calls: bump
  `METRICS_VERSION`. Request handlers read the metrics cache
  (`get_many(compute=False)` when many sessions are involved); never
  `derive()` or scan `samples` for many sessions per request.
* **Caches** (`data/cache/`) are versioned (`{"v": N, ...}`, other versions
  dropped) and shape-checked on load. A failed or partial fetch never
  overwrites good cached data (merge, note `tried`). `save()` merges with the
  file on disk when two processes may write.
* **Stores** (`data/*.json`, user state): `store.write_json` /
  `store.load_object` only; memory changes after the write succeeds; a file
  that fails validation is never overwritten.
* **Long work never blocks a user flow.** Network or bulk work runs in a
  subprocess / thread and the page polls; the server's DB stays read-only.
  An import is done before its weather starts.
* **State machines terminate.** Every retry / provisional / pending state has
  an end (attempt cap, back-off, give-up date, wall-clock budget). Write down
  what turns "provisional" into "final" when the data never arrives.
* **Exit codes.** A command exits non-zero when it didn't do what it was asked
  (nothing stored, cut short, refused); document each code.
* **Dates.** Stored instants are UTC ISO with offset; naive = UTC. Day math
  with `datetime` / `calendar`, never string surgery (no 31 Feb); catch
  `OverflowError` on odd inputs. "Today" is an injectable parameter. In JS,
  `YYYY-MM-DD` is parsed as a local date (`dayOf`), not `new Date(s)` (UTC).
* **Frontend.** Pure logic (math, formatting, HTML builders) as top-level
  functions testable under node. A modal, overlay or rAF loop registers its
  cleanup (`onLeave`, `_onCollapse`, `dropExpanded`): a re-render, resize or
  route change leaves no stale DOM, listener or focus trap. An awaited view
  checks `routeSeq` before drawing. Enlarged panels are dialogs (focus in,
  Tab trapped, focus back). Write controls hidden when `readonly`.
* **Look.** Colour on data only (`PAL`); main series orange, context grey,
  rose only for HR. Check new views at 1440, 900 and 390 px on the demo.
* **Docs.** User-visible behaviour: an AGENTS.md note + a `CHANGELOG.md` entry.

## Code standards

* Python stdlib only (pywebview optional behind an import guard). Supported:
  **3.15** (default) and **3.14** (`MIN_PYTHON` in `bin/rollbook` refuses
  older; the wrappers prefer `.venv`, then 3.15, then 3.14; `.python-version`
  pins pyenv to 3.15.2 in the repo, bump it with the Python). Anything in 3.14
  is fine (`match`, `X | Y`, `datetime.UTC`, `tomllib`, …); nothing 3.15-only
  until 3.14 is dropped. Raise both, CI and the docs together, once a year
  as versions leave upstream support.
* Cross-platform: `os.path`, `encoding="utf-8"` on text opens, `newline=""`
  for CSV, no POSIX-only calls without a Windows path.
* Comments say *why* (the data quirk, the measured number), not what.
* Untrusted input (exports, uploads, upstream answers, JSON files) is
  size-capped and type-checked; bad entries are dropped, never a crash.

## Security rules for new code

Threat model: one local user, sensitive data (GPS tracks show home; tool
output goes to a model provider). Attackers: other web pages (CSRF, DNS
rebinding to 127.0.0.1), LAN devices (`--lan`), hostile or compromised
upstreams (Strava, Open-Meteo, Nominatim, OSM tiles), tampered files in
`data/`. Fail closed; never crash on bad input.

* **Endpoints** only via `api.GET` / `api.POST`, never a branch in `do_GET` /
  `do_POST`: routing applies Host, `_lan_gate`, same origin, `X-Rollbook: 1`.
  JSON bodies through `_read_json` with the smallest limit that works (64 KiB,
  small actions 4 KiB). Writes, process spawns and store changes are POST
  only. Read-only is the `--lan` default: new write controls hide under
  `body.readonly` and the server refuses them anyway. Client ids:
  `ACTIVITY_ID` / `edits.ID_RE` `fullmatch`, `int()`, looked up before use;
  names in paths only through `safe_name`. Errors name what failed, not paths
  or tracebacks.
* **Frontend**: every string from the API, DB, `localStorage` or upstream
  goes through `esc()`, attributes included; numbers through `isNum` / `f0` /
  `f1` / `f2`, never raw `${v}`. No inline scripts, `on*=` attributes, `eval`
  or third-party URLs; the CSP (`http_base.CSP`) stays as it is.
* **Files in `data/`**: `store.write_json` (atomic, 0600, O_EXCL|O_NOFOLLOW)
  and `store.load_object(path, MAX)` with a size cap. Every loaded file,
  caches included, is untrusted: check each field's type, drop bad entries,
  fall back to empty; a bad file never crashes startup, a worker or a request.
* **Network**: only the hosts above. Every fetch honours `--offline` /
  `ROLLBOOK_OFFLINE`, has a timeout and the identifying User-Agent, caps the
  read (`r.read(MAX + 1)`), parses with `store.loads` and checks the shape, is
  rate-limited and cached. Coordinates rounded to the coarsest grid that
  works; never deleted / hidden sessions in bulk; if the edits overlay can't
  load, skip the bulk run.
* **Subprocesses**: `[sys.executable, TOOL, ...]` argv lists, never
  `shell=True`; arguments validated ints or constants; one run per kind,
  later requests batched and rate-limited; a `timeout`; a dead run leaves a
  clean state. Never with `--offline`, during an import doing the same work,
  or from a read-only LAN client.
* **SQL**: `?` placeholders for every value; identifiers only from constants
  or `sqlite_master`. Server and MCP open the DB `mode=ro`; writes only in
  `bin/rollbook` (sub)processes. Sessions read through the `activities`
  overlay (`activities_all` only where hidden rows are meant to show).
* **MCP**: no coordinates and nothing a place can be derived from: add
  location columns to `GPS_COLS` (e.g. `weather.psurf`) and derived fields to
  `DERIVED_DROP`. A new tool / table / field gets a privacy review ("what
  could a model provider work out from it?"). `query` stays under the
  authorizer. Output via `_r` / `clean` and `json.dumps(allow_nan=False)`;
  request ids that can't be echoed are rejected. MCP never writes files.
* **Secrets**: the Strava cookie lives in `data/strava_cookie` (0600) and the
  `strava` subprocess only: never in the server, logs, job logs, API answers,
  fixtures or chats. LAN token and TLS key 0600, compared with
  `hmac.compare_digest`.
* **Numbers** from a client, file or upstream: finite and within per-field
  bounds (`weather.BOUNDS`, `profile.FIELDS`) before arithmetic, `datetime`,
  `range` or allocation. Reject NaN, ±inf, 1e999, bools-as-ints, huge ints.

## Tests

`python3 -m unittest discover -s tests -t tests` (stdlib only, ~40 s, no
network; `tests/helpers.py` points `ROLLBOOK_HOME` at a temp dir and sets
`ROLLBOOK_OFFLINE=1`; import it first). CI:
`.github/workflows/tests.yml` on every push / PR, ubuntu/macos/windows ×
Python 3.14 and 3.15, plus `node --check` on the JS. `tests/helpers.py` also
writes synthetic FIT files (either endianness, optional sport/session
messages), so add a test there when a new device quirk turns up.

* Never touch `./data` or the network: network code takes injectable
  `urlopen` / `sleep` / `today` / clock, and tests also make
  `socket.connect` fail (`test_weather.Base`). Temp dirs via
  `helpers.tmpdir(self)`; fixed dates, no `now()` in asserts, no real sleeps.
* End to end: run `bin/rollbook` against a temp home (`test_end_to_end.run`)
  and assert the exit code as well as the output.
* Pure JS helpers run under node (`tests/test_frontend_*.py`, `skipUnless`
  node); every HTML builder gets an `EVIL` string.
* For every new cache / store / state: missing, corrupt, old-version and
  wrong-shape file; a failure after success (good data survives); the
  terminal state is reached (retry → give up → final); CLI, API and MCP agree
  on an edited session (sport changed, hidden, deleted).
* For every POST: the `api.POST` loops cover 403 (no header, foreign Origin,
  LAN read-only); add 400 on a bad body and the 404 / 409 / 503 paths.
* Assert intended behaviour: work expected values out by hand or by a second
  method, never copy them from a run of the code under test (a test that
  pins `2019-02-31` bakes a bug in).

Before a PR:

- [ ] tests pass; new code paths had a failing test first; `node --check web/static/*.js`
- [ ] runs on Python 3.14 and 3.15; Windows-safe paths / encodings; stdlib only; SPDX headers
- [ ] `METRICS_VERSION` bumped if `derive()` changed; `MIGRATIONS` + `has_col` for new columns
- [ ] network behind `--offline` / `ROLLBOOK_OFFLINE`, capped, cached, never blocking a request
- [ ] every retry / pending state ends; non-zero exit when nothing was done
- [ ] shared predicates used by CLI, API and MCP; edits overlay applied
- [ ] a failed fetch / save leaves caches and stores as they were
- [ ] `esc()` on every string; modal / loop cleanup on re-render and route change
- [ ] new MCP fields privacy-reviewed; AGENTS.md + CHANGELOG.md updated; screenshots only from `tools/demo.py`

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

Best efforts (elapsed): `Outdoor_cycle.gpx` 400 m 57.8 s, 1 km 2:27.0, 5 km
12:50.7, 10 km 27:25.0; `The_come_back.gpx` 54.3 s, 2:19.5, 12:26.0, 27:16.3.

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
