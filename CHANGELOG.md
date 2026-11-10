# Changelog

## 0.10.0 (2026-11-10)

### Python 3.15

- **Breaking:** Rollbook now needs Python 3.14 or 3.15 (3.15 is the default;
  3.9 to 3.13 are refused with a message). `./rollbook` and `rollbook.cmd`
  pick 3.15, then 3.14, unless a `.venv` exists: recreate it with
  `python3.15 -m venv .venv` if it was made with an older Python.
- CI runs on Python 3.14 and 3.15. A `.python-version` (3.15.2) makes pyenv
  pick it inside the repo. `requirements-optional.txt` lists the one
  optional package (pywebview, for `rollbook app`'s native window).

## 0.9.0 (2026-11-10)

### Hardening

- The dashboard no longer reads your Strava cookie to show whether one is
  stored (it only checks the file is there), and the cookie is written
  without a fixed temp file a planted link could redirect.
- A damaged metrics, segments or place-name cache no longer stops
  `rollbook web` from starting: it is ignored and rebuilt. An odd answer from
  OpenStreetMap's place-name service no longer stops segment naming until a
  restart; that cache is now private (0600).

### Max heart rate from your sessions

- The Profile page suggests a max HR: the highest heart rate you held for
  30 s in any session of the last 3 to 36 months (you pick), with the
  sessions it came from, the age formula for reference and a one-click "Use".
  A recorded peak that lasted only seconds (a sensor spike) no longer sets
  your zones.
- Each session's heart-rate zones use the max HR that applied on its date:
  changing it in the profile counts from that day on (the profile keeps a
  dated history), so older sessions don't move; tick "for all past sessions
  too" to correct it everywhere. Where no max HR was set, a
  session uses the held 30 s peak of the months up to its date, so zones
  follow your fitness. The headings say which max HR a session used.

### Weather in the database

- Weather is now **stored in the database** at import, one row per outdoor
  session and hour (`weather` table, `v_weather` per session), instead of
  being fetched by the dashboard while you browse. New: feels-like
  temperature, humidity, dew point, rain / snow, cloud cover, sky (weather
  code), pressure, and **air quality** (PM2.5, PM10, ozone, European AQI,
  from 2013). Each hour uses the position you were at then.
- `rollbook db weather [--missing] [--offline] [--id N]`; `db build` and
  `strava sync` run it at the end (`--no-weather` / `web --offline` skip the
  network). The dashboard Import fetches it in the background once the
  import is done, so new sessions show right away. Recent days are
  provisional and are refreshed by later imports until the reanalysis
  settles; days Open-Meteo had nothing for are asked again weekly.
- Enlarged panels are proper dialogs (focus, Tab, Esc), keep their close
  button in reach while scrolling, and survive a window resize.
- MCP: surface pressure is withheld unless `--allow-gps` (with sea-level
  pressure it gives your area's altitude).
- Opening a session that has no weather yet (e.g. right after upgrading)
  fetches and stores it in the background and redraws the page; `db weather
  --id <id>` does the same from a terminal. Not with `--offline`.
- Session page: one Weather card (temperature, sky, wind, rain, AQI); click
  it for the details: summary, hour-by-hour charts of temperature / feels /
  dew point, wind and gusts with direction, rain and cloud, humidity and
  pressure, air quality, with the session shaded, and an hourly table. Rain
  and AQI columns in the session list. MCP: `weather` in
  `activity`, and the tables in `query`.
- Fixed: a malformed Open-Meteo answer stopped all weather lookups until a
  restart; a session with a missing hour showed "loading" forever; last
  week's values were never replaced by the final reanalysis.
- Fixed: enlarging the compare page's Replay (or any sticky panel) only
  dimmed the page; it now fills the window, with both tracks fitted to it.
- The weather cache moves to a new format; the first `db weather` fetches
  again (~530 requests for ~1900 sessions, a few minutes to half an hour).

## 0.8.0 (2026-11-02)

### Gear in compare and MCP (#10, part 3)

- Comparing two sessions names their gear, and says which parts differed
  (e.g. other wheels or frame, with their specs): part of the gap may be the
  gear. The picker suggests "same route, other gear" and tags rows on other
  gear.
- MCP: a read-only `gear` tool (gear, fitted parts, km, hours, service state,
  defaults) and the session's gear in `activity`. Notes and purchase dates
  are never sent.

### Gear pages (#10, part 2)

- A gear's page shows its **parts over time** (when each was fitted and
  removed, with service, rotation and replacement marks), km per month, its
  best 10 min (best 20 min power indoors) against your other gear of that
  sport, and its service log.
- The Gear page compares the selected sport's gear side by side: sessions,
  km, median and best best-10-min, and speed per heartbeat where there is
  heart rate.
- No gear yet? The page lists your sports with their sessions and km; one
  click creates gear for a sport as its default from the first session.

### Gear (#10, part 1)

- A **Gear** page: your skates, bikes and shoes, made of parts (wheels,
  frame, boots, bearings, tyres, chain, shoes…) with their specs (diameter,
  hardness, frame length and setup, shoe size…). Km, time and sessions per
  gear and per part; a part counts only for the days it was fitted, so
  swapping or rotating wheels keeps the history right.
- A default gear per sport from a date: new sessions (dashboard import,
  Strava sync, `db build`) get it, older ones keep theirs. Change a single
  session's gear in its **Edit** dialog.
- Service reminders: set an interval in km per part, log a service, rotation
  or replacement; parts near or past it are flagged on the page and in the
  sidebar.
- The session page shows its gear (and the parts on it that day); the
  session list gets a gear column and filter.
- Kept in `data/gear.json`, which a rebuild never touches. A file that can't
  be read is left as it is and gear goes read-only until it's fixed.

## 0.7.0 (2026-10-30)

### Best efforts by distance (#6)

- The fastest time over 400 m, 1 km, 5 km, 10 km, half and full marathon
  anywhere in each session, exact (not sampled), elapsed time with stops,
  like Strava's best efforts. None for virtual rides; no 400 m on a bike.
- On the session page with its all-time rank (click a row to see the stretch
  on the map and charts), in Personal records, as sortable columns in the
  session list, in compare, as new records after an import, and as a trend on
  the Progress page with the record progression.
- The metrics cache is rebuilt once on the first start (about a minute).
- The MCP `records` tool lists best efforts per sport, fastest first.

## 0.6.0 (2026-10-28)

### Compare two sessions (#16)

- **Compare…** on a session (suggests the last time on the same route and the
  route best), a **Compare** link in "Same route, other sessions", or tick two
  sessions in the list.
- The time gap by distance leads: who was ahead where, the final and largest
  gap, lead changes and, on the elapsed clock, the stops. Speed and heart rate
  overlaid, a map replay with a ghost (10× / 30× / 60×, a scrubber synced with
  the charts), per-km (or per-lap) splits that add up to the gap, shared
  segments and every metric side by side.
- Moving or elapsed clock; the moving clock comes from the server at full
  resolution. Far-apart starts are lined up on the first shared segment;
  circuits can be lined up on the lap gate.

### Fixes and hardening

- Summary strips never leave a gap in their last row; the session page's map
  is released when you leave it.
- The dashboard sends a Content-Security-Policy; activity ids are checked
  strictly; the tile proxy fetches each tile once and at most two at a time,
  with a contact URL in its User-Agent.

## 0.5.0 (2026-10-26)

### A new look for the dashboard

- Dark, flat design on a neutral near-black: panels one shade up, no borders
  or shadows; the Inter typeface, bundled so it stays offline (OFL,
  `web/static/Inter-OFL.txt`), with tabular figures everywhere.
- A sidebar with Progress, Sessions, Map, Segments and your profile replaces
  the top navigation. It folds to an icon rail on a tablet and a bottom bar on
  a phone, and can be hidden with the button left of back / forward or
  ⌘\ / Ctrl+\ (remembered).
- Colour only on data: the wheel's orange for the main series, white for its
  trend, greys for context, rose for heart rate. Speed, heart-rate and power
  zones, the calendar and the heatmap are shades of those instead of rainbows.
- Summary cards are one divided strip that never leaves an orphan card; rows of
  panels no longer stretch over empty space; indoor sessions drop the empty
  route panel; the splits chart stays in view beside its table.
- One outline icon set replaces the ☆ ✎ ⤢ ✕ ⇪ symbols; icon buttons have
  accessible names; a skip link; the current page is marked for screen readers.

### New records after an import (#17)

- When an import or Strava sync finishes, the Import dialog lists the
  records it set, with links to the sessions and segments; the Progress page
  shows the same panel until you dismiss it.
- Covered: best 5/10/20/30/60 min, wind-neutral 10 min, longest, power
  (best 5 s – 60 min, weighted), segment PRs and best laps per circuit.
  "record" = best of that sport now; "best at the time" = faster than every
  earlier session (only for sessions near your latest, so an old backfill
  stays quiet).
- No claims without history: at least 5 earlier sessions with the value
  (3 for a circuit, 5 other sessions on a segment); a first import only
  counts sessions. Virtual rides get power records only. A session an
  export copy replaced isn't announced again.
- Stored in `data/last_import.json` (0600, validated on load); an import
  before you dismiss adds to the summary rather than replacing it. Read-only
  over `--lan`; the MCP server doesn't read it.

## 0.4.1 (2026-10-23)

### Security

- The desktop listener (`http://127.0.0.1:8765/`) now refuses any request
  whose `Host` isn't `127.0.0.1:<port>` or `localhost:<port>`, GETs included.
  Before, only POSTs were checked, so a website using DNS rebinding could
  read the dashboard's data, GPS tracks included.

## 0.4.0 (2026-10-22)

### Sync from Strava (`rollbook strava sync`)

- Downloads new activities' original files from strava.com and imports them,
  no full account export needed. CLI and a **Sync from Strava** button in
  the Import dialog.
- Uses your browser's `_strava4_session` cookie, stored once with
  `rollbook strava auth` in `data/strava_cookie` (0600); no password.
  `strava status` / `strava logout`.
- Synced activities keep Strava's id, name and sport; the next full export
  replaces them with Strava's summary, and they never overwrite it afterwards.
- Polite: requests spaced 1.5 s, backs off on 429, stops paging at the first
  page with nothing new. Automated website access is against Strava's terms.

## 0.3.0 (2026-10-20)

### Use from your phone (`rollbook web --lan`)

- Serves the dashboard to a phone on the same network, on a second HTTPS
  listener (port 8766). The desktop address `http://127.0.0.1:8765/` is
  unchanged.
- Access needs a random link, printed with a QR code in the terminal. It
  signs the browser in with a cookie; `--new-token` signs every device out.
- Read-only from the phone unless started with `--lan-write`.
- Self-signed certificate made with `openssl` or, without it, in pure Python.
  Its fingerprint is printed so you can check it before accepting the
  browser warning. `--lan-http` drops TLS.

## 0.2.0 (2026-10-18)

### MCP server (`rollbook mcp`)

- Read-only Model Context Protocol server on stdio, so AI assistants can
  work with your training data. Tools: `sports`, `schema`, `query` (one
  SELECT), `activities`, `activity`, `compare`, `trend`, `records`.
- Tells the assistant which metrics to trust (best 10-min speed for form,
  no Virtual Ride vs outdoor speed comparisons, ...).
- SQL is limited to SELECT and introspection: no writes, `ATTACH`,
  temporary tables or PRAGMA changes; 30 s timeout and a row cap.
- Applies your dashboard edits; never writes the dashboard's caches.
- GPS coordinates are withheld unless started with `--allow-gps`.
- Registered for OpenCode in `opencode.json`.

## 0.1.0 (2026-10-16)

First tagged release.

### Dashboard (`rollbook web` / `rollbook app`)

- Progress page: calendar heatmap, volume per month, form (best 10-min and
  wind-neutral speed), endurance, efficiency, heart-rate zones, records,
  energy per month.
- Sessions list and session page: speed-coloured route, linked speed / heart
  rate / power / elevation charts, splits, out/back legs, automatic lap
  detection, segments ridden, same-route sessions.
- Map: heatmap or per-session lines; click a spot to list sessions.
- Segments generated from your most-used roads, per direction, named from
  OpenStreetMap, with leaderboards and speed trends.
- Historical weather per outdoor session; out/back gap split into wind,
  slope and other, with a conditions-adjusted speed.
- Power for indoor rides: best 5 s to 60 min, weighted power, zones,
  intensity and load against your FTP, cadence, power per heartbeat.
- Rollbook's own per-sport calorie estimate and a Profile screen.
- Import from the UI (Strava export `.zip` or single `.fit/.gpx/.tcx(.gz)`),
  session edits (rename, sport, hide, delete), favorites, back/forward.
- `rollbook app`: own window via pywebview, or Chrome / Edge / Chromium /
  Brave in app mode; the server stops when the window closes.

### Command line

- `db build` / `db enrich` / `db query`, `analyze`, `compare`, `streams`,
  `list`, `trend`, and `--version`.

### Fixes in this release

- The dashboard no longer fails on a database that was never enriched.
- A second uploaded file with the same name but different content (HUAWEI's
  "Export Original" is always `Outdoor_cycle.fit`) is kept as `-2`, `-3`…
  instead of being dropped.
- Closing a second `rollbook app` window that reuses the server no longer
  stops the first one.
- A session replaced by its export copy (duplicate detection) is now
  enriched.

### Known issues

- An `f_ftp` of 0 in a FIT file stops the fallback to the 95 %-of-best-20-min
  FTP estimate.
- Power-zone labels round the edge, so a reading on a half-watt edge can be
  binned in the zone above its label.
- A malformed weather response stops the weather thread until restart.
- Uploading an export whose name matches an old, already-extracted archive
  reuses the old extraction.

### Development

- 217 stdlib-only tests (parsers, regression fixtures, laps, segments, wind
  model, importer, app launcher, weather cache, FTP and power zones), run on
  Linux, macOS and Windows with Python 3.9 and 3.13.
- `tools/demo.py` builds a synthetic athlete; `tools/screenshots.py`
  regenerates `docs/screenshots/` from it.
