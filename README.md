# ROWING SAFETY DASHBOARD

A real-time weather and tide monitoring dashboard for Fulham Reach Boat Club, displaying critical river conditions, weather forecasts, and club events. All API calls happen server-side; the browser receives plain HTML.

## Dashboard Overview

The dashboard displays real-time information in a three-column layout (landscape) or single column (portrait):

**Column 1 - River Conditions:**
- **Hammersmith Tides** - Current tide direction (FLOOD/EBB), time until next tide, upcoming tide schedule with heights
- **PLA Ebb Flag** - Port of London Authority flag status image for rowing safety
- **Richmond Low Tide** - Observed low tide level with colour-coded flag (Red/Yellow/Green/Black)
- **Kingston Flow** - River flow rate at Kingston with threshold-based colour coding

**Column 2 - Weather & Hazards:**
- **Weather Forecast** - Morning (0600-1200) and afternoon (1200-2000) windows showing:
  - Temperature range
  - Wind speed and gusts with direction
  - Rain probability
  - UV index
  - Fog and storm indicators
  - Air + Water temperature sum (cold water risk)
- **Met Office Warnings** - NSWWS severe weather warnings by time period


**Column 3 - Club Diary:**
- **Calendar Events** - Today's (or tomorrow's after 22:00) club sessions with times
- Auto-scrolling in landscape mode when events overflow
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
| **Google Calendar API** | Club calendar events | `GOOGLE_CALENDAR_API_KEY` |

### Fallback APIs

| API | Purpose | Environment Variable |
|-----|---------|---------------------|
| **WeatherAPI.com** | Weather forecast fallback | `WEATHERAPI_KEY` |
| **Open-Meteo** | Weather fallback, lightning risk, sunrise/sunset | None (free) |

### Open Data APIs (No Key Required)

| API | Purpose |
|-----|---------|
| **Port of London Authority** | Ebb tide flag widget, Richmond observed low tide |
| **Environment Agency** | Kingston river flow, Thames water temperature |

## Data Logic and Processing

### Caching Strategy

All API responses are cached in memory with per-source TTL (time-to-live):

| Data Source | TTL | Rationale |
|-------------|-----|-----------|
| Tides | 2 hours | Predicted data changes slowly |
| Weather | 2 hours | Forecasts updated infrequently |
| PLA Flag | Time-slot based | Refreshes at key times (06:00, 18:00, etc.) |
| Calendar | 30 minutes | Events change infrequently |
| Kingston Flow | 15 minutes | River conditions change moderately |
| Thames Temp | 15 minutes | Water temperature changes slowly |
| NSWWS Warnings | 15 minutes | Warnings updated regularly |

A file-based backoff system (`openmeteo_backoff.json`) persists rate-limit state across process restarts for Open-Meteo.

### Parallel Fetching

All data sources are fetched concurrently using threads to minimise page load time. The `build_dashboard_data()` function spawns 8 threads for:
- Tides, Calendar, PLA Flag, Weather, Kingston Flow, Richmond LW, Thames Temp, NSWWS

### Weather Fallback Chain

Weather data follows a priority fallback chain:
1. **Met Office DataHub** (Site-Specific) - tries hourly, then three-hourly
2. **WeatherAPI.com** - if Met Office unavailable or unconfigured
3. **Open-Meteo** - final fallback with rate-limit backoff

All sources return normalised data with morning/afternoon windows.

### Tide Calculations

- **Direction**: Determined by next upcoming tide event (HighWater = FLOOD, LowWater = EBB)
- **Time until next**: Calculated from current UTC time to next tide event
- **BST Adjustment**: Times displayed in local time (BST/GMT) with +1 hour offset during BST

### Richmond Flag Logic

Observed low tide height determines flag colour:
- **Red**: ≥ 2.6m (dangerous fast water)
- **Yellow**: ≥ 1.7m (caution)
- **Green**: ≥ 0m (normal)
- **Black**: < 0m (extreme low)

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

### Calendar Logic

- Fetches events for current day
- After 22:00, switches to show tomorrow's events
- Displays time ranges or "All Day"
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
├── requirements.txt
├── render.yaml                     # Render.com deployment config
├── README.md
├── templates/
│   └── index.html                  # Jinja2 template
└── static/
    └── FRBC logo White on black.png   # Copy your logo here
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

## API Endpoints

- `GET /` - Main dashboard HTML page
- `GET /data` - JSON endpoint with all dashboard data (for AJAX updates)
- `GET /ping` - Health check endpoint
- `GET /api/nswws-status` - Diagnostic endpoint for NSWWS connectivity

## Key Features

- **Zero client-side JavaScript** for data fetching (all server-side)
- **Auto-refresh** every 10 minutes via lightweight fetch
- **Responsive design** - adapts to portrait/landscape orientations
- **Graceful degradation** - continues operating if individual APIs fail
- **Threaded fetching** - parallel API calls for fast page loads
- **Cache pre-warming** - populates cache on startup to reduce first-request latency
- **Rate-limit handling** - file-based backoff for Open-Meteo 429 responses
