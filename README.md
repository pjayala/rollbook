<p align="center"><img src="assets/rollbook-256.png" width="128" alt="Rollbook icon"></p>

<h1 align="center">Rollbook</h1>

<p align="center"><a href="https://github.com/pjayala/rollbook/actions/workflows/tests.yml"><img src="https://github.com/pjayala/rollbook/actions/workflows/tests.yml/badge.svg" alt="tests"></a></p>

<p align="center">An offline training dashboard for your Strava data.<br>
Progress, heatmaps, laps, automatic segments with leaderboards, and wind-neutral speed. It runs on your own machine.</p>

<p align="center"><img src="docs/screenshots/progress.png" alt="Progress page: summary cards, calendar, form and volume charts"></p>

## Why

Strava's API and its deeper analysis features are subscription-only. The
account export, which you can request under GDPR, is free and contains every
activity file you ever recorded. Rollbook imports that export into a local
SQLite database and gives you a dashboard on top of it. Nothing is uploaded
anywhere.

It started as a way to track inline-skating progress honestly. On an
out-and-back route a tailwind makes one leg 10–25% faster, so raw "best
10 minutes" mostly measures the weather. Rollbook splits each session at its
turnaround and reports a **wind-neutral** speed. It works for any sport with
GPS: runs, rides, walks, skates, plus indoor rides from ROUVY or MyWhoosh.

## Features

- **Progress:** calendar heatmap, volume per month, form (best 10-min and
  wind-neutral speed), endurance (best 30/60-min), efficiency (speed ÷ heart
  rate), time in heart-rate zones, personal records and a monthly summary.
- **Sessions:** a sortable list with time, pace, best efforts, laps and heart
  rate. Each session has a page with:
  - a route map coloured by speed
  - speed, heart-rate and elevation charts that follow each other on hover
  - splits per km
  - out/back legs
  - automatic **lap detection**: the best lap, consistency and fade across
    laps
  - the segments you rode
  - other sessions on the same route

  Click a km, leg or lap, or drag across a chart, to highlight that stretch
  everywhere at once.
- **Map:** a heatmap of everywhere you've been, or one line per session
  coloured by speed, distance or date. Click a spot to list every session that
  passed through it.
- **Segments:** generated automatically from the roads and loops you use most,
  each direction separately (so wind shows up), and named after the streets
  and areas they cross using OpenStreetMap. Each segment has a leaderboard and
  a speed trend, and links to the opposite direction.
- **Weather and conditions:** temperature, wind and gusts for every outdoor
  session. On out-and-back routes, the gap between the two legs is split into
  wind, slope and "other", giving a conditions-adjusted speed. Plus speed vs
  temperature.
- **Power** for indoor rides (smart trainer, from the original files):
  average, weighted power, best 5 s to 60 min, zero-power time, power zones
  per session and per month, intensity and training load against your FTP
  (from the profile, from ROUVY's files, or estimated), cadence, watts per
  heartbeat, and a power chart per session.
- **Calories:** Rollbook's own per-sport estimate, from measured power for
  indoor rides, the ACSM equations for running and walking, and
  speed-based METs for skating and outdoor rides. Watches and Strava differ
  wildly on this. A **Profile** screen holds your weight, height, birth year
  and heart-rate limits.
- **Race predictor**, **favorites (☆)** for sessions and segments, and
  back/forward navigation.
- **Import** from the UI: drop in a full Strava export `.zip` or single
  `.fit/.gpx/.tcx` files ("Export Original"). Duplicates are detected, and
  different files with the same name (HUAWEI calls every export
  `Outdoor_cycle.fit`) are all kept.
- **New records after an import:** when an import or sync finishes, the
  dialog and the Progress page list what it changed: personal records (best
  5–60 min, wind-neutral, longest, power), segment PRs and best laps, as
  all-time records or "best at the time". Shown until you dismiss it.
- **Sync from Strava** (`rollbook strava sync` or a button in the Import
  dialog): fetch new activities straight from strava.com with your browser's
  login cookie, no export or API needed. See
  [Sync from Strava](#sync-from-strava).
- **Edit sessions** (✎ on a session): rename, fix the sport, hide from stats
  or delete. Edits are stored next to the database and survive rebuilds.
- **Desktop window** (`rollbook app`) or a normal browser tab
  (`rollbook web`).

<table>
<tr><td><img src="docs/screenshots/session.png" alt="Session page with laps"></td>
<td><img src="docs/screenshots/segments.png" alt="Segment leaderboard"></td></tr>
<tr><td align="center"><sub>A session: speed-coloured route, linked charts, laps</sub></td>
<td align="center"><sub>A segment: speed over time and its leaderboard</sub></td></tr>
</table>

<table>
<tr><td><img src="docs/screenshots/power.png" alt="Indoor ride with power"></td>
<td><img src="docs/screenshots/heatmap.png" alt="Heatmap"></td></tr>
<tr><td align="center"><sub>An indoor ride: power, intensity and load against FTP</sub></td>
<td align="center"><sub>Heatmap of every session</sub></td></tr>
</table>

<p align="center"><sub>The screenshots show a synthetic demo athlete (<code>tools/demo.py</code>), offline, so
there is no basemap. Normally you see OpenStreetMap underneath.</sub></p>

## Requirements

- **Python 3.9 or newer.** Rollbook itself uses only the standard library, so
  nothing else is needed for `rollbook web`.
- **Optional, for a native app window:**
  [pywebview](https://pywebview.flowrl.com/). If it isn't installed,
  `rollbook app` opens Chrome, Edge, Chromium or Brave in app mode instead.
- **A Strava account export:** Strava → Settings → My Account → *Download or
  Delete Your Account* → *Request your archive*. The email with the `.zip`
  usually arrives within a few hours.

## Install

### macOS

```sh
git clone https://github.com/pjayala/rollbook.git
cd rollbook
python3 --version                       # 3.9+; if missing: brew install python
# optional native window:
python3 -m venv .venv && .venv/bin/pip install pywebview
./rollbook app
```

### Linux

```sh
git clone https://github.com/pjayala/rollbook.git
cd rollbook
python3 --version                       # 3.9+ (Debian/Ubuntu: sudo apt install python3 python3-venv)
# optional native window, needs Qt or GTK bindings:
python3 -m venv .venv && .venv/bin/pip install "pywebview[qt]"
./rollbook app
```

Without pywebview, `./rollbook app` uses an installed Chrome, Chromium, Edge
or Brave. If none is found it opens a normal browser tab.

### Windows

Install Python 3 from [python.org](https://www.python.org/downloads/) or the
Microsoft Store, then in PowerShell or cmd:

```bat
git clone https://github.com/pjayala/rollbook.git
cd rollbook
rem optional native window (uses Edge WebView2, built into Windows 10/11):
python -m venv .venv
.venv\Scripts\pip install pywebview
rollbook.cmd app
```

Without pywebview, `rollbook.cmd app` uses Edge in app mode, which comes with
Windows. No download is needed.

## First run

1. Start Rollbook: `./rollbook app` (Windows: `rollbook.cmd app`), or
   `./rollbook web` for a browser tab at <http://127.0.0.1:8765/>.
2. Click **⇪ Import** (or drag files onto the window) and drop your
   `export_….zip`. A few thousand activities import in about a minute.
   Rollbook then computes its metrics in the background, which takes about
   another minute the first time.
3. Pick a sport at the top and explore.

The same from the command line:

```sh
./rollbook db build ~/Downloads/export_12345678.zip
./rollbook db enrich --missing
./rollbook web
```

Adding sessions later: run `./rollbook strava sync` (see
[Sync from Strava](#sync-from-strava)), import a newer export (only new
activities are added), or a single activity's **⋯ → Export Original** file.
For single files, pick the sport in the dialog, because watches often
mislabel it; HUAWEI saves skating as "cycling".

## Command line

| command | what it does |
|---|---|
| `rollbook app [--mode auto\|webview\|chrome\|browser]` | dashboard in its own window; the server stops when the window closes |
| `rollbook web [--port 8765] [--offline]` | dashboard on `127.0.0.1` |
| `rollbook web --lan [--lan-write]` | also serve it to your phone over the home network, see below |
| `rollbook db build <export.zip\|dir> [--fresh]` | import an export (incremental) |
| `rollbook db enrich [--missing]` | best 5/10/20-min efforts |
| `rollbook db query "SQL" [--csv]` | read-only SQL against your data |
| `rollbook analyze <file>` | one activity: pauses, splits, out/back legs, grade |
| `rollbook compare <a> <b> …` | side-by-side summary |
| `rollbook strava auth\|sync\|status\|logout` | download new activities from strava.com, see below |
| `rollbook mcp [--allow-gps]` | read-only MCP server on stdio, so an AI assistant can query your data |
| `rollbook --version` | print the version |

Use `./rollbook` on macOS/Linux and `rollbook.cmd` on Windows. All state lives
in `./data`, which is git-ignored.

### Sync from Strava

Instead of requesting a full export every time, Rollbook can fetch new
activities from strava.com the way the website does, with the session cookie
of a browser where you're logged in. No API subscription and no password.

1. Log into strava.com in your browser. Open the developer tools →
   *Application* (Chrome/Edge) or *Storage* (Firefox) → *Cookies* →
   `https://www.strava.com`, and copy the value of `_strava4_session`.
2. In a terminal: `./rollbook strava auth` and paste it (the input is
   hidden; a whole copied `Cookie:` header works too). It is checked against
   strava.com and saved in `data/strava_cookie`, readable only by you.
3. `./rollbook strava sync`, or **⇪ Import → ⟳ Sync from Strava** in the
   dashboard.

Sync walks your activity list from newest to oldest and stops at the first
page with nothing new. It downloads each new activity's original file
(what **Export Original** gives), with Strava's name and sport, then imports
and enriches them. Options: `--dry-run` (only list), `--since YYYY-MM-DD`,
`--all` (check every page), `--max N` (default 200 per run, oldest first; the
next run continues). Manual entries without a file are skipped and
remembered. `strava status` says whether the cookie still works; when it has
expired, run `strava auth` again.

Synced sessions lack the summary numbers only an export has (e.g. Strava's
elevation gain). The next full export replaces them with Strava's version.

Notes:

- **The cookie is a full login to your Strava account.** Never paste it into
  a chat, an issue or a shared file. `./rollbook strava logout` deletes the
  stored copy; logging out on strava.com ends the session for good.
- It's only sent to strava.com. The dashboard never reads it: the button
  runs the same CLI in a subprocess. It isn't available with `--offline`, or
  from the phone unless `--lan-write`.
- This is automated use of the website, which Strava's terms don't allow.
  Rollbook keeps it light (1.5 s between requests, backs off when asked), but
  use it at your own risk. The endpoints are undocumented and may change.

### Use from your phone

```sh
./rollbook web --lan --no-open        # phone can view
./rollbook web --lan-write --no-open  # phone can also edit, star and import
```

The dashboard stays at `http://127.0.0.1:8765/` on the computer. A second,
HTTPS listener on port 8766 serves the phone on the same Wi-Fi. The terminal
prints a link and a QR code for it.

1. On macOS, allow the "accept incoming connections" prompt the first time.
2. Scan the QR code with the phone's camera, or open the printed
   `https://<computer-ip>:8766/?t=…` link.
3. Chrome says *Your connection is not private*, because the certificate is
   self-signed. Optionally compare the SHA-256 fingerprint the terminal printed
   with the one under the address bar's certificate details. Then tap
   **Advanced → Proceed**.
4. The link signs that browser in with a cookie for a year. After that,
   `https://<computer-ip>:8766/` is enough, and you can use
   ⋮ → *Add to Home screen*.

How it is protected:

- **Access link.** Every request on the network port needs the random
  token from the link (128-bit, kept in `data/lan_token`). The token is
  turned into an HttpOnly cookie and removed from the address bar. Requests
  without it get 403. `--new-token` makes a new link and signs out every
  device.
- **Read-only by default.** Edits, favorites, profile changes and imports
  from the phone are refused (and hidden in the UI) unless you start with
  `--lan-write`.
- **Only this computer's own names and IPs** are accepted as the host,
  which guards against DNS rebinding. The desktop address does the same:
  only `127.0.0.1:<port>` and `localhost:<port>`.
- **HTTPS with a self-signed certificate** (`data/tls/`, valid 397 days,
  reused across restarts). It covers the computer's LAN IPs,
  `<hostname>.local`, `localhost` and `127.0.0.1`. Rollbook creates it with
  `openssl` when that's installed, otherwise in pure Python. It's renewed
  automatically when the IP changes or it nears expiry, and then the phone
  asks again once. `--new-cert` forces a new one.
- `--lan-http` uses plain HTTP instead. The token and your data then cross
  the network unencrypted, so only use it if HTTPS is impossible.

Only use this on a network you trust. Anyone holding the link can read your
data, including GPS tracks, which reveal where you live. The computer must
be awake, and if its IP changes, rescan the new QR code. Other options:
`--lan-port`, and `--qr always|never` (QR codes print automatically only
when output goes to a terminal).

The dashboard isn't optimised for phone screens yet: some wide panels need
sideways scrolling.

### AI assistants (MCP)

`rollbook mcp` exposes the database to AI assistants as Model Context
Protocol tools (stdio, stdlib only; protocol versions 2025-06-18,
2025-03-26 and 2024-11-05):

| tool | what it does |
|---|---|
| `sports` | per sport: sessions, km, hours, date range, best 10-min speed |
| `schema` | tables, views, columns, row counts |
| `query` | one SELECT; 200 rows by default (5000 max), 30 s timeout |
| `activities` | list sessions, filtered by sport / dates / name / min km, sorted by any key metric |
| `activity` | one session: summary, per-km splits, out/back legs and wind-neutral speed, laps, power, HR zones |
| `compare` | 2–10 sessions side by side |
| `trend` | per week / month / year: volume and form |
| `records` | personal bests: best 5–60 min speed, wind-neutral speed, distance, ascent, power |

On connect, the assistant also gets the rules for which metric to trust
(e.g. compare form with `best10min_kmh`, never compare Virtual Ride speeds
with outdoor ones).

**Setup.** The repo's `opencode.json` registers it for OpenCode as the
`rollbook` server (`opencode mcp list` should show it connected). For other
clients, configure a local stdio server that runs `./rollbook mcp` (use the
absolute path); on Windows the command is `rollbook.cmd mcp`.

**Read-only.** The database is opened read-only. `query` runs on its own
connection that accepts only SELECT and introspection PRAGMAs; writes,
`ATTACH`, temporary tables and PRAGMA changes are refused. Your dashboard
edits (renames, sport changes, hidden sessions) apply as in the dashboard;
`main.activities` is the unedited table. The dashboard's metrics cache is
read but never written.

**Privacy.** Whatever the tools return is sent to the assistant's model
provider. GPS coordinates are withheld (`samples.lat/lon` read as NULL, map
tracks and lap positions dropped) unless you pass `--allow-gps`.

**Limitations.** `records` only covers sessions already in the dashboard's
metrics cache and says how many it used; open `rollbook web` once to fill it.
The 30 s best speed is left out of the records because a single GPS glitch
can win it.

## Privacy

- Your activities, the database and caches stay in `./data` on your machine.
  The server listens on `127.0.0.1` only, unless you start it with `--lan`
  (see [Use from your phone](#use-from-your-phone)).
- Three things go over the network, all cached forever:
  - **map tiles** from OpenStreetMap
  - **reverse geocoding** of segment points (OpenStreetMap Nominatim, at most
    one request per second) to name segments
  - **historical weather** for outdoor sessions (Open-Meteo), per ~10 km
    area and day

  These requests reveal to those services the areas and dates involved. Start
  with `--offline` to use only what's cached.
- `rollbook mcp` itself makes no network requests, but what it returns to an
  AI assistant goes to that assistant's model provider (GPS withheld by
  default, see [AI assistants](#ai-assistants-mcp)).
- `rollbook strava sync` (only when you run it) talks to strava.com with your
  session cookie, see [Sync from Strava](#sync-from-strava).
- No accounts, no telemetry, no Strava API.

## How it works

`AGENTS.md` has the detailed notes: the database schema, which speed metric
to trust for what, how laps, segments and wind-neutral speed are computed,
and a list of data-quality traps in real-world GPX/TCX/FIT files that the
importer handles. Examples include big-endian FIT files, GPS lost under a
bridge, filler altitude readings and stuck odometers.

## Development

Tests use only the standard library and never touch the network or your
`./data`. Changes to `master` go through pull requests, and all CI jobs must pass before merging:

```sh
python3 -m unittest discover -s tests -t tests
```

They cover the parsers (GPX, gzipped files, synthetic little- and big-endian
FIT), the regression fixtures in `samples/`, GPS and elevation clean-up,
laps and circuits, segments, the wind and slope model, the weather cache, the
importer (uploads, duplicate detection, background build), Strava sync
(against a local fake strava.com), the `rollbook app`
launcher, FTP choice and power zones (run under Node when it's installed), and
an end-to-end build plus the dashboard API and its CSRF checks. GitHub Actions
runs them on every push and pull request on Linux, macOS and Windows with
Python 3.9 and 3.13.

Screenshots are regenerated from a synthetic athlete, never from real data:

```sh
python3 tools/screenshots.py      # builds the demo with tools/demo.py, needs Chrome
```

Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## License

[GPL-3.0-or-later](LICENSE). Map data © OpenStreetMap contributors (ODbL).
Rollbook is not affiliated with Strava.
