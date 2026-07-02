# ROWING SAFETY DASHBOARD

A real-time weather and tide monitoring dashboard for Fulham Reach Boat Club, displaying critical river conditions, weather forecasts, water quality, and club events. Dashboard data (tides, weather, flags, calendar, water quality) is fetched server-side and rendered as HTML. The rain radar and wind-arrow map overlay are the exception — those are fetched client-side (Leaflet + RainViewer + `/api/wind`) so the map can refresh independently of the page.

## Dashboard Overview

The dashboard displays real-time information in a three-column layout (landscape) or single column (portrait):

**Column 1 - River Conditions:**
- **Hammersmith Tides** - Current tide direction (FLOOD/EBB), time until next tide, upcoming tide schedule with heights
- **Bridge Tides** - Predicted times at Putney, Hammersmith, Chiswick and Richmond, derived as fixed minute offsets from the Hammersmith prediction
- **Spring/Neap Trend** - "Moving to Spring/Neap tides" indicator computed from the tidal range trend over the last 7 days
- **PLA Ebb Flag** - Port of London Authority flag status image with associated safety text, plus two crosscheck lines: PLA JSON endpoint result and Richmond low tide prior to the flag. A "sources disagree" warning is shown if the widget scrape, Richmond fallback and JSON crosscheck don't all agree
- **Richmond Low Tide** - Lowest observed tide in the 12 hours before the current flag slot, colour-coded by PLA thresholds; with a next-flag prediction if a low tide has been recorded since the current flag was set
- **Kingston Flow** - River flow rate at Kingston with threshold-based colour coding
- **Water Quality** - Combined CSO (Combined Sewer Overflow) discharge status and E. coli readings, with drill-down pages at `/waterquality` (banded discharge table) and `/csomap` (interactive monitor map)

**Column 2 - Weather & Hazards:**
- **Weather Forecast** - Morning (0600-1200) and afternoon (1200-2000) windows showing:
  - Temperature range
  - Wind speed and gusts with direction
  - Rain probability
  - UV index
  - Fog and storm indicators
  - Air + Water temperature sum (cold water risk)
  - Each window is backfilled with actual Met Office observations once it has fully passed (morning after 12:00, afternoon after 20:00), when `METOFFICE_OBSERVATIONS` is configured; a window still in progress or upcoming stays as forecast
- **Met Office Warnings** - NSWWS severe weather warnings by time period, plus a 7-day-lookahead list of warnings not yet active today
- **Rain Radar & Wind Map** - Leaflet map with a RainViewer radar overlay and a 4x4 wind-arrow grid, refreshed client-side (radar every 5 minutes, wind hourly)

**Column 3 - Club Diary:**
- **Calendar Events** - Today's (or tomorrow's after 22:00) club sessions with times, interleaved with tide/sunrise/sunset markers
- Live clock in the column header
- Auto-shrinking text, then auto-scrolling, in landscape mode when events overflow
- Past events dimmed

**Footer:**
- System status, timezone (BST/GMT), and last update timestamp

## Data Sources and APIs

### Primary APIs

| API | Purpose | Environment Variable |
|-----|---------|---------------------|
| **UK Hydrographic Office (Admiralty) Tidal API** | Tidal events for Hammersmith (Station 0115) | `TIDE_API_KEY` |
| **Met Office Weather DataHub (Site-Specific)** | Hourly/three-hourly weather forecasts | `METOFFICE_SITESPECIFIC` |
| **Met Office NSWWS** | National Severe Weather Warning Service | `METOFFICE_NSWWS` |
| **Met Office Observations** | Actual observations, used to backfill each weather window once it has fully passed | `METOFFICE_OBSERVATIONS` |
| **Google Calendar API** | Club calendar events | `GOOGLE_CALENDAR_API_KEY` |

### Fallback APIs

| API | Purpose | Environment Variable |
|-----|---------|---------------------|
| **WeatherAPI.com** | Weather forecast fallback, sunrise/sunset fallback | `WEATHERAPI_KEY` |
| **Open-Meteo** | Weather fallback, lightning risk, sunrise/sunset | None (free) |

### Open Data APIs (No Key Required)

| API | Purpose |
|-----|---------|
| **Port of London Authority** | Ebb tide flag (widget scrape + JSON endpoint crosscheck), Richmond observed low tide chart |
| **Environment Agency** | Kingston river flow, Thames water temperature |
| **Thames Water Open Data API v2** | CSO (Combined Sewer Overflow) discharge alerts and status, by monitor location |
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
| Met Office Observations | 1 hour | Backfills each weather window once it has fully passed |
| PLA Flag | Time-slot based | Refreshes at key times (06:00, 18:00, etc.) |
| PLA JSON (crosscheck) | 5 minutes | Independent check against the widget/Richmond-derived colour |
| Richmond Observed Low Tide | 1 minute | Needs to catch a new low tide reading quickly for next-flag prediction |
| Calendar | 30 minutes | Events change infrequently |
| Kingston Flow | 15 minutes | River conditions change moderately |
| Thames Temp | 15 minutes | Water temperature changes slowly |
| NSWWS Warnings | 15 minutes | Warnings updated regularly |
| CSO Discharge | 30 minutes | Discharge alerts change moderately |
| Water Quality (E. coli) | 6 hours | Sheet is updated infrequently |
| Wind Grid | 1 hour | Wind forecast changes slowly |

A file-based backoff system (`openmeteo_backoff.json`) persists rate-limit state across process restarts for Open-Meteo. A separate file-based store (`_MORNING_FILE`) persists each day's captured forecast windows across process restarts, so the observations backfill has rain/UV values to borrow once a window has passed. This file lives in ephemeral per-deploy storage, so a Render redeploy clears it — if that happens between roughly 06:00 and 20:00, that day's rain/UV values for the backfilled window(s) may be unavailable until the next day's forecast is captured fresh.

### Parallel Fetching

All data sources are fetched concurrently using threads to minimise page load time. The `build_dashboard_data()` function spawns 11 threads for:
- Tides, Calendar, PLA Flag, PLA JSON (crosscheck), Weather, Kingston Flow, CSO Discharge, Richmond LW, Thames Temp, NSWWS, Water Quality

### Weather Fallback Chain

Weather data follows a priority fallback chain:
1. **Met Office DataHub** (Site-Specific) - tries hourly, then three-hourly
2. **WeatherAPI.com** - if Met Office unavailable or unconfigured
3. **Open-Meteo** - final fallback with rate-limit backoff
4. **Met Office Observations** - if `METOFFICE_OBSERVATIONS` is configured, each window (whichever source supplied it) is replaced with actual observations once it has fully passed: morning after 12:00 local, afternoon after 20:00 local

All sources return normalised data with morning/afternoon windows.

Sunrise/sunset is fetched via its own independent fallback chain (WeatherAPI.com, then Open-Meteo), regardless of which source served the main forecast.

### Tide Calculations

- **Direction**: Determined by next upcoming tide event (HighWater = FLOOD, LowWater = EBB)
- **Time until next**: Calculated from current UTC time to next tide event
- **BST Adjustment**: Times displayed in local time (BST/GMT) with +1 hour offset during BST

### PLA Ebb Flag Logic

The flag image and colour are determined by a fallback chain, attempted in order when the cache slot expires:

1. **PLA widget scrape** (primary) — the app scrapes the PLA's own ebb-tide-flag widget embed page for the current flag colour. The flag image URL is then constructed using the PLA's fixed pattern `flag_{colour}.png`.

2. **Richmond gauge fallback** — if the widget scrape fails, the colour is derived from the Richmond observed low tide (see below) that applies to the current flag slot time (06:00 or 18:00). This replicates what the PLA would have seen when setting the flag. A "double check with PLA" warning is shown when this source is used.

3. **Error state** — if both sources fail and there is no stale cache, a warning message is shown in place of the flag image.

The flag image is always a PLA-hosted PNG; the app determines which colour to put in the filename.

The PLA JSON endpoint (`pla.co.uk/pla-proxy/five-minute?url=tides/ebb-flag`) is **not** part of this fallback chain — it's fetched independently as a crosscheck (see below) and compared against the widget/Richmond-derived colour. If the sources disagree, a blinking "sources disagree" warning is shown.

Two crosscheck lines are displayed beneath the flag image:
- **PLA JSON** — the raw result from the JSON endpoint, fetched and cached independently of the primary flag colour
- **Richmond low tide prior to flag** — the time and height of the observed low tide the PLA used when setting the current flag

### Richmond Low Tide Display and Next-Flag Prediction

A single API call to `pla.co.uk/pla-proxy/one-minute?url=tides/chart/14541` returns all Richmond observed tidal records. These are split into two buckets relative to the current flag slot time (06:00 or 18:00):

- **before_flag** — the **lowest** low tide reading in the 12 hours before the flag slot (the PLA sets the flag from the lowest reading in that window, not simply the most recent one). Displayed in the Richmond Low Tide section as what the PLA saw, and used by the Richmond fallback if the widget scrape fails.
- **after_flag** — the most recent low tide after the flag slot, if any. This is new data the PLA has not yet acted on and is used to predict the next flag colour and slot time (6am or 6pm). If no low tide has occurred since the flag was set, no prediction is shown.

### Richmond Flag Colour Thresholds

| Height | Flag |
|--------|------|
| ≥ 2.6m | Red |
| ≥ 1.7m | Yellow |
| ≥ 0m | Green |
| < 0m | Black |

### Kingston Flow Thresholds

River flow colour coding:
- **Red**: > 120 m³/s (dangerous)
- **Yellow**: ≥ 80 m³/s (caution)
- **White**: < 80 m³/s (normal)

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

### Water Quality / CSO Logic

- **CSO discharge** — `get_cso_discharge()` polls the Thames Water Open Data API v2 (`discharge/alerts` and `discharge/status`) for every monitor listed in `cso_monitors.json` (the source of truth for monitor IDs, zones, and grid references). British National Grid references are converted to WGS84 for map display (`/csomap`).
- **E. coli readings** — `get_water_quality()` reads FRBC and PTRC monitoring-site CSV exports from Google Sheets and derives a risk colour per reading.
- The combined summary appears in the main dashboard's Water Quality tile; `/waterquality` shows a detailed table banded by geography (downstream of Putney / Hammersmith-Putney / upstream), and `/csomap` shows every tracked monitor on a Leaflet map colour-coded by zone.

### Calendar Logic

- Fetches events for current day
- After 22:00, switches to show tomorrow's events
- Displays time ranges or "All Day"
- Interleaves tide, sunrise and sunset markers among the day's events
- Past events dimmed based on current time

### PLA Flag Time Slots

Flag refreshes at specific times to catch flag changes:
- Pre-dawn (< 06:00)
- AM early (06:00-06:14)
- AM mid (06:15-06:29)
- AM late (06:30-06:59)
- AM BST catch (07:00-07:14) - safety fetch during BST
- Midday (07:15-17:59)
- PM early (18:00-18:14)
- PM mid (18:15-18:29)
- PM late (18:30-18:59)
- PM BST catch (19:00-19:14)
- Evening (19:15+)

## File Structure

```
frbc-tides/
├── app.py                          # Flask app + all API logic
├── cso_monitors.json                # CSO monitor/zone/grid-ref config (source of truth for /csomap, /waterquality)
├── requirements.txt
├── render.yaml                     # Render.com deployment config
├── gunicorn.conf.py                 # Gunicorn server config
├── site.webmanifest
├── README.md
├── templates/
│   └── index.html                  # Jinja2 template
└── static/
    ├── FRBC logo White on black.png   # Copy your logo here
    ├── favicon.ico / favicon.svg / favicon-96x96.png
    ├── apple-touch-icon.png
    ├── site.webmanifest
    └── web-app-manifest-192x192.png / web-app-manifest-512x512.png
```

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
- `METOFFICE_OBSERVATIONS` - Met Office Observations key, used to backfill each weather window with actuals once it has fully passed (morning after 12:00, afternoon after 20:00). Falls back gracefully (feature is simply skipped) if unset
- `FLASK_DEBUG` - set to `1` to run local `python app.py` with Flask debug mode on (defaults off)

Optional (not currently used):
- `METOFFICE_ATMOSPHERIC` - Atmospheric API key (returns GRIB2, not compatible)
- `METOFFICE_NSWWS_FEED_URL` - Custom NSWWS feed URL (defaults to official)

## Local Development

```bash
pip install -r requirements.txt
python app.py
# Visit http://localhost:5000
```

Optional: Install shapely for precise NSWWS location filtering:
```bash
pip install shapely
```

## Deploying to Render (free)

1. Push this repository to GitHub
2. Go to https://render.com and sign in with GitHub
3. Click **New → Web Service**
4. Select your repository
5. Add all environment variables from the Render dashboard
6. Render will detect `render.yaml` and configure automatically
7. Click **Deploy** — your app will be live at `yourapp.onrender.com`

Note: Render free tier spins down after 15 minutes inactivity. The first request after spin-down may be slower due to cache pre-warming.

**Important deploy sequencing**: always upload `app.py` before `index.html`. The template references data keys produced by `app.py`; deploying `index.html` first against an old `app.py` can cause template rendering errors.

## API Endpoints

- `GET /` - Main dashboard HTML page
- `GET /data` - JSON endpoint with all dashboard data (for AJAX updates)
- `GET /ping` - Health check endpoint
- `GET /api/nswws-status` - Diagnostic endpoint for NSWWS connectivity
- `GET /api/wind` - JSON wind-grid data for the radar map's wind-arrow overlay
- `GET /api/overlay` - Compact JSON summary (PLA flag colour, next tide label/time, pontoon warning) for external/embedded consumers
- `GET /csomap` - Leaflet map of all tracked CSO monitors, colour-coded by zone
- `GET /waterquality` - Detailed CSO discharge table banded by geographic zone

## Key Features

- **Mostly server-side rendering** - dashboard data (tides, weather, flags, calendar, water quality) is fetched and assembled server-side; the rain radar and wind-arrow map overlay are fetched client-side via Leaflet/RainViewer/`/api/wind`
- **Auto-refresh** every 10 minutes via lightweight fetch
- **Responsive design** - adapts to portrait/landscape orientations
- **Graceful degradation** - continues operating if individual APIs fail
- **Threaded fetching** - 11 parallel API calls for fast page loads
- **Cache pre-warming** - starts at import time (not just under `if __name__ == "__main__"`), so it also runs under gunicorn/Render to reduce first-request latency
- **Rate-limit handling** - file-based backoff for Open-Meteo 429 responses
- **Water quality tracking** - combines Thames Water CSO discharge data with E. coli readings, with map and table drill-downs
- **Source-disagreement warnings** - flags when the PLA widget scrape, Richmond fallback, and PLA JSON crosscheck don't agree on the current flag colour
