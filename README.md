# ROWING SAFETY DASHBOARD

A real-time weather and tide monitoring dashboard for Fulham Reach Boat Club, displaying critical river conditions, weather forecasts, water quality, and club events. Dashboard data (tides, weather, flags, calendar, water quality) is fetched server-side and rendered as HTML. Two things refresh independently client-side: the rain radar / wind-arrow map (Leaflet + RainViewer + `/api/wind`), and the closest-lightning line (`/api/lightning`), which is fed by a background Blitzortung websocket thread.

## Current State (2026-10-25)

Recently shipped:
- **Closest lightning** line (`/api/lightning`) from the public Blitzortung websocket — 30-minute rolling window, flashes red below 100 km.
- **Club Diary rework** — clock-first heading, events ended >1 h ago removed, event-driven switch to Tomorrow, current-window-only 🟥/🟨 markers.
- **Richmond / Kingston rework** — 12–24 h low-water readings, measurement-time freshness, BST/GMT labels; next-flag prediction and the PLA-JSON crosscheck removed.
- **Weather table** — Storm and "Met Office Warning" rows removed; section headings link to their source pages; NSWWS warning lines link to the Met Office warnings page.
- **Water quality** — each CSO outfall links to its Water Watch site page; spring/neap note shortened.

Open work is tracked in `PLAN.md` (local working note, not committed).

## Dashboard Overview

The dashboard displays real-time information in a three-column layout (landscape) or single column (portrait):

**Column 1 - River Conditions:**
- **Hammersmith Tides** - Current tide direction (FLOOD/EBB), time until next tide, upcoming tide schedule with heights
- **Bridge Tides** - Predicted times at Putney, Hammersmith, Chiswick and Richmond, derived as fixed minute offsets from the Hammersmith prediction
- **Spring/Neap** - short note under the tide table: `Moving to spring tides` / `Moving to neap tides`, or `Spring tide` / `Neap tide` when within 0.2 m of the window peak
- **PLA Ebb Flag** - Port of London Authority flag status with associated safety text. The flag image links to the PLA ebb tide flag warning page. Falls back to the Richmond-derived colour if the widget cannot be scraped
- **Richmond Low Tide** - every observed low water from 12 h before the current flag slot up to now (12–24 h window, 1–3 readings), colour-coded by PLA thresholds
- **Kingston Flow** - River flow at Kingston with threshold colouring and a freshness check on the measurement itself (stale if > 1 h old)
- **Water Quality** - Live CSO/sewage-spill status for the local waterways (Thames Water Open Data API) plus E. coli readings, with a grouped detail page at `/waterquality`; each outfall there links to its Water Watch site page

**Column 2 - Weather & Hazards:**
- **Weather Forecast** - Morning (0600-1200) and afternoon (1200-2000) windows. The heading is "WEATHER TODAY" by default and switches to "WEATHER TOMORROW" after 20:00 (showing tomorrow's same two windows, with tomorrow's sunrise/sunset), reverting at midnight. Showing:
  - Temperature range
  - Wind speed and gusts with direction
  - Rain probability
  - UV index
  - Fog indicator
  - Air + Water temperature sum (cold water risk)
- **Met Office Warnings** - NSWWS severe weather warnings by time period, plus a 7-day-lookahead list of warnings not yet active today. Each current/upcoming warning line links to the Met Office warnings page
- **Closest Lightning** - closest Blitzortung strike to Hammersmith in the last 30 min (e.g. `Closest lightning 1331.4 km at 11:57`); flashes red under 100 km. Links to maps.blitzortung.org
- **Rain Radar & Wind Map** - Leaflet map with a RainViewer radar overlay and a 4x4 wind-arrow grid, refreshed client-side (radar every 5 minutes, wind hourly)

**Column 3 - Club Diary:**
- **Club Diary** - today's club sessions, switching to tomorrow as soon as today's diary has finished (no timed session still to run; an all-day session keeps it on today). Heading shows the live clock first, then `TODAY`/`TOMORROW`
- Events that ended more than an hour ago are removed; those that ended within the last hour stay greyed, so the column shows current + remaining only
- Tide, sunrise and sunset markers follow the diary's day and expire the same way
- Auto-shrinking text, then auto-scrolling, in landscape mode when events overflow

Section headings (Tide, PLA, Water Quality, Weather, Radar) link to their source pages; the Club Diary heading does not.

## Data Sources and APIs

### Primary APIs

| API | Purpose | Environment Variable |
|-----|---------|---------------------|
| **UK Hydrographic Office (Admiralty) Tidal API** | Tidal events for Hammersmith (Station 0115) | `TIDE_API_KEY` |
| **Met Office Weather DataHub (Site-Specific)** | Hourly/three-hourly weather forecasts, and the source for the 14-day calendar daily summary | `METOFFICE_SITESPECIFIC` |
| **Met Office NSWWS (v1.1)** | National Severe Weather Warning Service - v1.1 on Weather DataHub | `METOFFICE_NSWWS` |
| **Google Calendar API** | Club calendar events | `GOOGLE_CALENDAR_API_KEY` |

### Fallback APIs

| API | Purpose | Environment Variable |
|-----|---------|---------------------|
| **WeatherAPI.com** | Weather forecast fallback, sunrise/sunset | `WEATHERAPI_KEY` |

> **Note:** Open-Meteo was previously used for weather/wind/sunrise fallbacks. It has been removed entirely; the weather chain is now Met Office → WeatherAPI only.

### Open Data APIs (No Key Required)

| API | Purpose |
|-----|---------|
| **Port of London Authority** | Ebb tide flag (widget scrape; Richmond fallback), Richmond observed low tide chart |
| **Environment Agency** | Kingston river flow, Thames water temperature |
| **Blitzortung.org (public WSS)** | Global lightning strikes feeding the closest-lightning line (server-side websocket, no key) |
| **Thames Water Open Data API v2** | Live CSO/EDM discharge status (`discharge/status?limit=1000`, one call) — open, no key |
| **Google Sheets (CSV export)** | E. coli water-quality readings for FRBC and PTRC monitoring sites |
| **RainViewer** | Rain radar tile overlay (fetched client-side) |
| **CartoDB / OpenStreetMap** | Basemap tiles for the radar/wind map (fetched client-side) |

## Data Logic and Processing

### Caching Strategy

All API responses are cached in memory with per-source TTL (time-to-live):

| Data Source | TTL | Rationale |
|-------------|-----|-----------|
| Tides | 2 hours | Predicted data changes slowly |
| Weather | 2 hours | Forecasts updated infrequently |
| Daily weather (14-day calendar) | 2 hours | Forecasts updated infrequently |
| Calendar (today) | 30 minutes | Events change infrequently |
| Calendar (14-day agenda) | 30 minutes | Events change infrequently |
| PLA Flag | 15-minute window | Re-scrapes at most once per 15-minute slot, all day |
| Richmond Observed Low Tide | 1 minute | Needs to pick up a new low-water reading quickly |
| Kingston Flow | 15 minutes | River conditions change moderately; staleness is judged on the reading's own timestamp |
| Lightning | live (rolling 30 min) | Background Blitzortung websocket thread; the dashboard reads an in-memory snapshot |
| Thames Temp | 15 minutes | Water temperature changes slowly |
| NSWWS Warnings | 15 minutes | Warnings updated regularly |
| CSO discharge hours | 15 minutes | Source updates ~every 30 min; status + 48h alerts pull |
| Water Quality (E. coli) | 6 hours | Sheet is updated infrequently |
| Wind Grid | 1 hour | Wind forecast changes slowly |

### Parallel Fetching

All data sources are fetched concurrently using threads to minimise page load time. The `build_dashboard_data()` function spawns 10 threads for:
- Tides, Calendar, PLA Flag, Weather, Kingston Flow, Richmond LW, Thames Temp, NSWWS, Water Quality, CSO Status

### Weather Fallback Chain

Weather data follows a priority fallback chain:
1. **Met Office DataHub** (Site-Specific) - tries hourly, then three-hourly
2. **WeatherAPI.com** - if Met Office unavailable or unconfigured

Both sources return normalised morning/afternoon windows. Sunrise/sunset is fetched from WeatherAPI independently of which source served the main forecast; if it fails, the sun markers are simply omitted.

The `/calendar` page's 14-day daily summary is built from the Met Office site-specific timeseries (three-hourly, falling back to hourly), aggregated per local day (max temperature, max rain probability, max wind, max gust). It covers however many days the Met Office response returns — later calendar days without data simply show no weather entry.

### Tide Calculations

- **Direction**: Determined by next upcoming tide event (HighWater = FLOOD, LowWater = EBB)
- **Time until next**: Calculated from current UTC time to next tide event
- **BST Adjustment**: Times displayed in local time (BST/GMT) with +1 hour offset during BST

### PLA Ebb Flag Logic

The flag colour is determined by a fallback chain, attempted when the 15-minute cache slot expires:

1. **PLA widget scrape** (primary) — the app scrapes the PLA's own ebb-tide-flag widget embed page for the current flag colour. The heading/body text is preferred over the image filename when they disagree.
2. **Richmond gauge fallback** — if the widget scrape fails, the colour is derived from the lowest Richmond observed low tide in the 12 hours before the current flag slot (06:00 or 18:00). This replicates what the PLA would have seen when setting the flag.
3. **Error state** — if both sources fail and there is no stale cache, a warning message is shown in place of the flag.

There is **no** separate crosscheck and no "sources disagree" banner (removed Oct 2026). The only flag/feed warnings are the blocked/blank ones described below.

### Richmond Low Tide Display

A single API call to `pla.co.uk/pla-proxy/one-minute?url=tides/chart/14541` returns all Richmond observed tidal records.

The dashboard lists **every observed low water** from 12 hours before the current flag slot time (06:00 or 18:00) up to now. That window is therefore 12–24 hours long and normally contains 1–3 readings, oldest first. Each reading is coloured with the thresholds below (PLA "black" is rendered grey so it is visible on the black background). Timestamps carry a BST/GMT label, and a reading from the previous day is marked as such.

If the feed is blocked or returns no readings, the row shows **"Richmond low tide data unavailable — check data with PLA"**. There are no discrepancy warnings.

### Richmond Flag Colour Thresholds

| Height | Flag |
|--------|------|
| ≥ 2.6m | Red |
| ≥ 1.7m | Yellow |
| ≥ 0m | Green |
| < 0m | Black |

### Kingston Flow

Source: Environment Agency measure `3400TH-flow-water-i-15_min-m3_s` (`?_sorted&_limit=1`), rounded to whole m³/s. The dashboard shows the **measurement time** (BST/GMT), not the fetch time, and marks the reading stale if it is more than an hour old (or has no usable timestamp) — the EA feed keeps serving the last value when a gauge goes offline.

Colour coding:
- **Red**: > 120 m³/s (dangerous), or stale
- **Yellow**: ≥ 80 m³/s (caution)
- **White**: < 80 m³/s (normal)

If the feed is blocked or blank, the row shows **"Kingston Flow unavailable — check data with PLA"**.

### NSWWS Warning Processing

1. Fetches Atom feed to get issued-warnings GeoJSON URL
2. Fetches GeoJSON with polygon geometries
3. Filters by:
   - Warning level (RED/AMBER/YELLOW)
   - Status (excludes EXPIRED/CANCELLED)
   - Location (point-in-polygon check using shapely, or London bbox fallback)
   - Time window (overlaps with morning 0600-1200 or afternoon 1200-2000)
4. Sorts by severity (RED > AMBER > YELLOW)

### Cold Water Risk

Air temperature + water temperature sum displayed with red warning if < 14°C.

### Water Quality Logic

- **CSO discharge hours** — `get_cso_status()` (cached 15 min) does a `GET .../discharge/status?limit=1000` (all ~570 national permits, for the monitor list/positions) plus `GET .../discharge/alerts` Start+Stop over a 14-day lookback, pairs each Start with its next Stop, clips the intervals to the **last 48 hours** and sums seconds per permit. It keeps only the waterways near the club (River Thames, River Brent, River Wandle & Mitchell Brook, Beverley Brook, and the smaller NW brooks: Graveney, Dollis, Wealdstone, Wembley, Hanwell) and groups them into three reaches by BNG easting: **Upstream of Teddington** (x < 517550), **Tideway to Putney** (517550–524075, Teddington Lock to Putney Bridge) and **Downstream** (x > 524075). Each zone shows its **total discharge hours in the last 48h**, and each outfall shows its own 48h hours plus the most recent discharge start/stop. Each outfall keeps its waterway label. Tunnel-captured permits (`receivingWaterCourse` contains "via the Tideway tunnel") are excluded — they discharge nothing to the river. No API key, no database, no scheduler.
- **E. coli readings** — `get_water_quality()` reads FRBC and PTRC monitoring-site CSV exports from Google Sheets and derives a risk colour per reading. Shown in the dashboard's Water Quality tile and on `/waterquality`.
- The homepage Water Quality tile shows each reach's **total discharge hours in the last 48h** (e.g. "1h 30m") beside the E. coli readings; `/waterquality` shows the full grouped tables with per-outfall 48h hours.

### Club Diary Logic

- Fetches today's events, and tomorrow's once today's diary has finished — i.e. no timed event is still to run. Any all-day event keeps it on today; a day with no events at all moves to tomorrow.
- Heading shows the live clock first, then `TODAY`/`TOMORROW`.
- Events that ended more than an hour ago are dropped; events that ended within the last hour stay greyed. Running and upcoming events are normal.
- Tide, sunrise and sunset markers follow the diary's day and expire the same way.
- Hazard markers (🟥/🟨) use only the current flag window.

## File Structure

```
frbc-tides/
├── app.py                          # Flask app + all API logic
├── requirements.txt
├── .gitignore
├── README.md
├── templates/
│   ├── index.html                   # Main dashboard
│   ├── calendar.html                # 14-day diary agenda
│   ├── links.html                   # Links page
│   └── distances.html               # FRBC rowing distances
└── static/
    ├── FRBC logo White on black.png
    ├── favicon.ico / favicon.svg / favicon-96x96.png
    ├── apple-touch-icon.png
    ├── site.webmanifest
    └── web-app-manifest-192x192.png / web-app-manifest-512x512.png
```

Working notes kept beside the code but **excluded from git** (`.git/info/exclude`), so they do not travel with a clone:
- `AGENTS.md` — agent/contributor workflow, approval rule, auth, deploy notes
- `PLAN.md` — open items

## Environment Variables

Required for full functionality:

```bash
TIDE_API_KEY=your_ukho_key
GOOGLE_CALENDAR_API_KEY=your_google_key
WEATHERAPI_KEY=your_weatherapi_key
METOFFICE_NSWWS=your_metoffice_nsws_key
METOFFICE_SITESPECIFIC=your_metoffice_site_key
```

Optional:
- `METOFFICE_NSWWS_FEED_URL` - Custom NSWWS v1.1 feed URL (defaults to `https://data.hub.api.metoffice.gov.uk/nswws/v1.1/objects/feed`)
- `ENABLE_PREWARM` - set to `1`/`true`/`yes` to warm the cache in a background thread at startup. **Leave unset on Render's free tier** (see below)
- `FLASK_DEBUG` - set to `1` to run local `python app.py` with Flask debug mode on (defaults off)

## Local Development

```bash
pip install -r requirements.txt
python app.py
# Visit http://localhost:5000
```

Optional: Install `shapely` for precise NSWWS location filtering (it is already in `requirements.txt`; without it, the app falls back to a London bounding-box check).

## Deploying to Render (free)

This service is **dashboard-managed** on Render (there is no `render.yaml`):

1. Push this repository to GitHub.
2. Create/serve the Web Service in the Render dashboard and connect the repo.
3. Set the environment variables above in the Render dashboard.
4. Start command:
   ```
   gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1
   ```
5. Health Check Path: `/healthz` (cheap liveness probe; returns `ok` immediately).
6. Render auto-deploys on each push to `main`.

### Free-tier notes

- Render's free tier spins down after ~15 minutes of inactivity; the first request after spin-down is slower because the cache is cold.
- **Cache pre-warming is opt-in and off by default.** At startup the worker binds its port immediately and the first request warms the cache. On the free tier this avoids the memory spike (OOM/SIGKILL) and port-detection flapping caused by fetching every source in the background during boot. If you want pre-warming, set `ENABLE_PREWARM=1` — but note it competes with the first request on the single worker.
- `/healthz` deliberately does **not** trigger pre-warming; Render's health checks therefore stay cheap.

## API Endpoints

- `GET /` - Main dashboard HTML page
- `GET /data` - JSON endpoint with all dashboard data (for AJAX updates)
- `GET /healthz` - Lightweight liveness check (returns `ok`)
- `GET /ping` - Simple health check endpoint (returns `ok`)
- `GET /links` - Links page
- `GET /calendar` - 14-day diary agenda (tides, weather, club events)
- `GET /distances` - FRBC rowing distances
- `GET /api/nswws-status` - Diagnostic endpoint for NSWWS connectivity
- `GET /api/wind` - JSON wind-grid data for the radar map's wind-arrow overlay
- `GET /api/lightning` - Closest lightning strike to Hammersmith in the last 30 min (JSON: message, distance, time, danger flag)
- `GET /api/overlay` - Compact JSON summary (PLA flag colour, next tide label/time) for external/embedded consumers
- `GET /waterquality` - CSO spill status grouped by reach (Upstream of Teddington / Tideway to Putney / Downstream) plus FRBC/PTRC E. coli readings

## Key Features

- **Mostly server-side rendering** - dashboard data (tides, weather, flags, calendar, water quality) is fetched and assembled server-side; the rain radar and wind-arrow map overlay are fetched client-side via Leaflet/RainViewer/`/api/wind`
- **Auto-refresh** every 10 minutes via lightweight fetch
- **Responsive design** - adapts to portrait/landscape orientations
- **Graceful degradation** - continues operating if individual APIs fail
- **Threaded fetching** - 10 parallel API calls for fast page loads
- **Opt-in cache pre-warming** - off by default; enable with `ENABLE_PREWARM=1`
- **Weather chain** - Met Office DataHub → WeatherAPI (no Open-Meteo)
- **Water quality** - live CSO spill status for the local waterways (single Thames Water API call, no key) plus FRBC/PTRC E. coli readings; outfalls link to Water Watch
- **Closest lightning** - Blitzortung websocket, rolling 30-min window, red below 100 km
- **Linked sources** - section headings, NSWWS warning lines and CSO outfalls link to their source pages
