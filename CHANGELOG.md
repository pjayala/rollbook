# Changelog

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
