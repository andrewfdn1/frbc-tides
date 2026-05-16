from flask import Flask, jsonify, render_template
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
import requests
import urllib3
import threading
import os
import json
import pathlib
import tempfile
import xml.etree.ElementTree as ET

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from shapely.geometry import Point, shape as shapely_shape
    _SHAPELY_OK = True
except ImportError:
    _SHAPELY_OK = False
    print("WARNING: shapely not installed — NSWWS point-in-polygon disabled. pip install shapely")

app = Flask(__name__)

TIDE_API_KEY      = os.environ.get("TIDE_API_KEY", "")
GOOGLE_API_KEY    = os.environ.get("GOOGLE_CALENDAR_API_KEY", "")
WEATHERAPI_KEY    = os.environ.get("WEATHERAPI_KEY", "")
NSWWS_API_KEY     = os.environ.get("METOFFICE_NSWWS", "")
LONDON_TZ         = ZoneInfo("Europe/London")
CAL_ID            = "info@fulhamreachboatclub.com"

# Hammersmith, London
LAT, LON = 51.488, -0.224

_cache           = {}
_cache_locks     = {}
_cache_locks_mu  = threading.Lock()
_cal_fail_until  = 0

# File-based backoff — survives process restarts and is shared across workers
_BACKOFF_FILE = pathlib.Path(tempfile.gettempdir()) / "openmeteo_backoff.json"


def _get_fail_until(key):
    try:
        data = json.loads(_BACKOFF_FILE.read_text())
        return data.get(key, 0)
    except Exception:
        return 0


def _set_fail_until(key, seconds):
    try:
        try:
            data = json.loads(_BACKOFF_FILE.read_text())
        except Exception:
            data = {}
        data[key] = datetime.now(timezone.utc).timestamp() + seconds
        _BACKOFF_FILE.write_text(json.dumps(data))
        print(f"Open-Meteo {key} 429 — backing off for {seconds}s")
    except Exception as e:
        print(f"Could not persist backoff state: {e}")


def _get_lock(key):
    with _cache_locks_mu:
        if key not in _cache_locks:
            _cache_locks[key] = threading.Lock()
        return _cache_locks[key]

def get_cached(key, fetch_fn, ttl_seconds):
    now = datetime.now(timezone.utc).timestamp()
    if key in _cache and now - _cache[key]['ts'] < ttl_seconds:
        return _cache[key]['data'], _cache[key]['fetched_at']
    with _get_lock(key):
        now = datetime.now(timezone.utc).timestamp()
        if key in _cache and now - _cache[key]['ts'] < ttl_seconds:
            return _cache[key]['data'], _cache[key]['fetched_at']
        try:
            data = fetch_fn()
            fetched_at = datetime.now(LONDON_TZ).strftime('%H:%M')
            _cache[key] = {'ts': now, 'data': data, 'fetched_at': fetched_at}
            return data, fetched_at
        except Exception as e:
            print(f"Error fetching {key}: {e}")
            if key == "nswws":
                global _nswws_last_error
                _nswws_last_error = str(e)
            if key in _cache:
                return _cache[key]['data'], _cache[key]['fetched_at']
            return None, ''


def get_tides():
    def fetch():
        r = requests.get(
            "https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/0115/TidalEvents",
            headers={"Ocp-Apim-Subscription-Key": TIDE_API_KEY},
            timeout=10
        )
        r.raise_for_status()
        return sorted([
            {
                'dt_utc': datetime.fromisoformat(e['DateTime'].replace('Z', '')).replace(tzinfo=timezone.utc),
                'EventType': e['EventType'],
                'Height': e['Height']
            }
            for e in r.json()
        ], key=lambda x: x['dt_utc'])
    return get_cached('tides', fetch, ttl_seconds=7200)


def get_calendar_events():
    global _cal_fail_until
    now_ts = datetime.now(timezone.utc).timestamp()

    if now_ts < _cal_fail_until:
        if 'calendar' in _cache:
            return _cache['calendar']['data'], _cache['calendar']['fetched_at']
        return {"day_label": "TODAY", "list": []}, ''

    def fetch():
        global _cal_fail_until
        now = datetime.now(LONDON_TZ)
        display_date = now + timedelta(days=1) if now.hour >= 22 else now
        target_date  = display_date.date()

        day_start = display_date.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        day_end   = display_date.replace(hour=23, minute=59, second=59, microsecond=0)

        url = (
            f"https://www.googleapis.com/calendar/v3/calendars/"
            f"{requests.utils.quote(CAL_ID, safe='')}/events"
            f"?key={GOOGLE_API_KEY}"
            f"&timeMin={requests.utils.quote(day_start.isoformat())}"
            f"&timeMax={requests.utils.quote(day_end.isoformat())}"
            f"&singleEvents=true&orderBy=startTime&maxResults=20"
        )

        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
        except Exception:
            _cal_fail_until = datetime.now(timezone.utc).timestamp() + 600
            raise

        events_list = []
        for e in r.json().get('items', []):
            start = e.get('start', {})
            end   = e.get('end', {})
            summary = e.get('summary', '(no title)')
            if 'dateTime' in start:
                dt_s = datetime.fromisoformat(start['dateTime']).astimezone(LONDON_TZ)
                if dt_s.date() == target_date:
                    time_str = dt_s.strftime('%H:%M')
                    if 'dateTime' in end:
                        dt_e = datetime.fromisoformat(end['dateTime']).astimezone(LONDON_TZ)
                        time_str = f"{time_str}-{dt_e.strftime('%H:%M')}"
                    events_list.append({"summary": summary, "time": time_str})
            elif 'date' in start:
                ev_date = datetime.strptime(start['date'], '%Y-%m-%d').date()
                if ev_date == target_date:
                    events_list.append({"summary": summary, "time": "All Day"})

        return {
            "day_label": "TOMORROW" if now.hour >= 22 else "TODAY",
            "list": events_list
        }

    return get_cached('calendar', fetch, ttl_seconds=1800)


def get_pla_flag():
    def fetch():
        r = requests.get("https://pla.co.uk/pla-api-integration/ebb-tide-widget-embed", timeout=5)
        soup = BeautifulSoup(r.text, 'html.parser')
        img = soup.find('img')
        if img:
            src = img['src']
            return "https://pla.co.uk" + src if not src.startswith('http') else src
        return None

    now = datetime.now(LONDON_TZ)
    h, m = now.hour, now.minute

    if h < 6:
        slot = (now.date(), 'pre-dawn')
    elif h == 6 and m < 15:
        slot = (now.date(), 'am-early')       # 06:00–06:14 first fetch
    elif h == 6 and m < 30:
        slot = (now.date(), 'am-mid')         # 06:15–06:29 second fetch
    elif h < 7:
        slot = (now.date(), 'am-late')        # 06:30–06:59
    elif h == 7 and m < 15:
        slot = (now.date(), 'am-bst-catch')   # 07:00–07:14 BST safety fetch
    elif h < 18:
        slot = (now.date(), 'midday')         # 07:15–17:59 stable
    elif h == 18 and m < 15:
        slot = (now.date(), 'pm-early')       # 18:00–18:14 first fetch
    elif h == 18 and m < 30:
        slot = (now.date(), 'pm-mid')         # 18:15–18:29 second fetch
    elif h < 19:
        slot = (now.date(), 'pm-late')        # 18:30–18:59
    elif h == 19 and m < 15:
        slot = (now.date(), 'pm-bst-catch')   # 19:00–19:14 BST safety fetch
    else:
        slot = (now.date(), 'evening')        # 19:15 onward stable

    cached = _cache.get('pla_flag')
    if cached and cached.get('slot') == slot:
        return cached['data'], cached['fetched_at']

    try:
        data = fetch()
        fetched_at = datetime.now(LONDON_TZ).strftime('%H:%M')
        _cache['pla_flag'] = {
            'ts': datetime.now(timezone.utc).timestamp(),
            'data': data,
            'fetched_at': fetched_at,
            'slot': slot
        }
        return data, fetched_at
    except Exception as e:
        print(f"Error fetching pla_flag: {e}")
        if cached:
            return cached['data'], cached['fetched_at']
        return None, ''


# ---------------------------------------------------------------------------
# PLA Richmond observed low tide
# ---------------------------------------------------------------------------

_PLA_RICHMOND_CHART_URL = (
    "https://pla.co.uk/pla-proxy/one-minute?url=tides/chart/14541"
)

def get_richmond_observed_low_tide():
    def fetch():
        r = requests.get(
            _PLA_RICHMOND_CHART_URL,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://pla.co.uk/"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        now_utc = datetime.now(timezone.utc)
        best = None

        for tp in data.get("tpoints", []):
            if tp.get("tidal_state") != 2:
                continue
            observed = tp.get("observed")
            if observed is None:
                continue
            tstamp = tp.get("tstamp", "")
            if not tstamp:
                continue

            dt_utc    = datetime.fromisoformat(tstamp[:19]).replace(tzinfo=timezone.utc)
            dt_london = dt_utc.astimezone(LONDON_TZ)

            if dt_utc > now_utc + timedelta(minutes=30):
                continue

            if best is None or dt_utc > best["dt_utc"]:
                best = {
                    "dt_utc":    dt_utc,
                    "dt_london": dt_london,
                    "metres":    float(observed),
                }

        if best is None:
            return None

        d      = best["dt_london"].day
        suffix = "th" if 11 <= d % 100 <= 13 else {1:"st",2:"nd",3:"rd"}.get(d % 10, "th")
        metres = best["metres"]

        if   metres >= 2.6: flag, flag_word = "RED",    "Red"
        elif metres >= 1.7: flag, flag_word = "YELLOW", "Yellow"
        elif metres >= 0:   flag, flag_word = "GREEN",  "Green"
        else:               flag, flag_word = "BLACK",  "Black"

        return {
            "time":      best["dt_london"].strftime(f"%H:%M {d}{suffix} %b"),
            "metres":    metres,
            "flag":      flag,
            "flag_word": flag_word,
        }

    return get_cached("richmond_observed_lw", fetch, ttl_seconds=60)


def get_cardinal_direction(degree):
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[int((degree + 11.25) / 22.5) % 16]


def prevailing_direction(degrees_list):
    if not degrees_list:
        return "N/A"
    cardinals = [get_cardinal_direction(d) for d in degrees_list]
    return max(set(cardinals), key=cardinals.count)


# ---------------------------------------------------------------------------
# WeatherAPI.com fallback
# ---------------------------------------------------------------------------

def _parse_weatherapi(data):
    """Map WeatherAPI.com forecast response to the same shape as get_weather()."""
    try:
        day = data['forecast']['forecastday'][0]
        sunrise = day['astro']['sunrise']   # e.g. "06:12 AM"
        sunset  = day['astro']['sunset']

        # Normalise to HH:MM 24-hour
        def to_24h(t):
            return datetime.strptime(t, '%I:%M %p').strftime('%H:%M')

        def window(start_h, end_h):
            hours = [
                h for h in day['hour']
                if start_h <= int(h['time'][11:13]) < end_h
            ]
            if not hours:
                return None

            temps  = [h['temp_c']       for h in hours]
            winds  = [h['wind_kph']     for h in hours]
            gusts  = [h['gust_kph']     for h in hours]
            dirs   = [h['wind_degree']  for h in hours]
            rains  = [h.get('chance_of_rain', h.get('chance_of_snow', 0)) for h in hours]
            uvs    = [h.get('uv', h.get('uv_index', 0)) for h in hours]
            codes  = [h['condition']['code'] for h in hours]

            # WeatherAPI condition codes: fog=248/260, storm=1273/1276/1279/1282
            FOG_CODES   = {248, 260}
            STORM_CODES = {1273, 1276, 1279, 1282}

            return {
                'temp_min':  round(min(temps)),
                'temp_max':  round(max(temps)),
                'wind_min':  round(min(winds)),
                'wind_max':  round(max(winds)),
                'gust_min':  round(min(gusts)),
                'gust_max':  round(max(gusts)),
                'direction': prevailing_direction(dirs),
                'rain_min':  round(max(rains)),
                'rain_max':  round(max(rains)),
                'uv_max':    round(max(uvs), 1) if uvs else None,
                'fog':       any(c in FOG_CODES   for c in codes),
                'storm':     any(c in STORM_CODES for c in codes),
            }

        return {
            'morning':   window(6,  12),
            'afternoon': window(12, 21),
            'sunrise':   to_24h(sunrise),
            'sunset':    to_24h(sunset),
            'source':    'WeatherAPI',
        }
    except Exception as e:
        raise Exception(f"WeatherAPI parse error: {e}")


def get_weather_weatherapi():
    """Fetch from WeatherAPI.com and return data in the same shape as get_weather()."""
    if not WEATHERAPI_KEY:
        raise Exception("No WEATHERAPI_KEY configured")
    url = (
        f"https://api.weatherapi.com/v1/forecast.json"
        f"?key={WEATHERAPI_KEY}"
        f"&q={LAT},{LON}"
        f"&days=1"
        f"&aqi=no"
        f"&alerts=no"
    )
    r = requests.get(url, timeout=10)
    if r.status_code == 429:
        raise Exception("WeatherAPI rate limited")
    r.raise_for_status()
    return _parse_weatherapi(r.json())


# ---------------------------------------------------------------------------
# Primary weather: Open-Meteo, fallback: WeatherAPI
# ---------------------------------------------------------------------------

def get_weather():
    now_ts = datetime.now(timezone.utc).timestamp()

    # Honour Open-Meteo backoff (file-persisted, survives restarts)
    if now_ts < _get_fail_until('weather'):
        if 'weather' in _cache:
            return _cache['weather']['data'], _cache['weather']['fetched_at']
        # Backoff active but no cache — try fallback immediately
        print("Open-Meteo in backoff, trying WeatherAPI fallback")
        return _get_weather_fallback()

    def fetch():
        wx_url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={LAT}&longitude={LON}"
            "&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,"
            "wind_gusts_10m,weather_code,precipitation_probability,uv_index"
            "&daily=sunrise,sunset"
            "&timezone=Europe%2FLondon"
            "&forecast_days=1"
        )

        wx_res = requests.get(wx_url, timeout=10)
        if wx_res.status_code == 429:
            retry_after = int(wx_res.headers.get("Retry-After", 3600))
            _set_fail_until('weather', retry_after)
            raise Exception(f"Open-Meteo rate limited, retry after {retry_after}s")
        wx_res.raise_for_status()
        d = wx_res.json()

        hourly = d['hourly']
        daily  = d['daily']
        times  = hourly['time']

        def window(start_h, end_h):
            indices = [i for i, t in enumerate(times) if start_h <= int(t[11:13]) < end_h]
            if not indices:
                return None

            def vals(key):
                return [hourly[key][i] for i in indices if hourly[key][i] is not None]

            return {
                'temp_min':  round(min(vals('temperature_2m'))) if vals('temperature_2m') else None,
                'temp_max':  round(max(vals('temperature_2m'))) if vals('temperature_2m') else None,
                'wind_min':  round(min(vals('wind_speed_10m'))) if vals('wind_speed_10m') else None,
                'wind_max':  round(max(vals('wind_speed_10m'))) if vals('wind_speed_10m') else None,
                'gust_min':  round(min(vals('wind_gusts_10m'))) if vals('wind_gusts_10m') else None,
                'gust_max':  round(max(vals('wind_gusts_10m'))) if vals('wind_gusts_10m') else None,
                'direction': prevailing_direction(vals('wind_direction_10m')),
                'rain_min':  round(min(vals('precipitation_probability'))) if vals('precipitation_probability') else None,
                'rain_max':  round(max(vals('precipitation_probability'))) if vals('precipitation_probability') else None,
                'uv_max':    round(max(vals('uv_index')), 1) if vals('uv_index') else None,
                'fog':       any(c in [45, 48] for c in vals('weather_code')),
                'storm':     any(c >= 95 for c in vals('weather_code')),
            }

        return {
            'morning':   window(6,  12),
            'afternoon': window(12, 21),
            'sunrise':   daily['sunrise'][0][-5:],
            'sunset':    daily['sunset'][0][-5:],
            'source':    'Open-Meteo',
        }

    # Try Open-Meteo via the normal cache wrapper
    result, fetched_at = get_cached('weather', fetch, ttl_seconds=7200)

    # If Open-Meteo failed and returned None, try fallback
    if result is None:
        return _get_weather_fallback()

    return result, fetched_at


def _get_weather_fallback():
    """Try WeatherAPI.com and cache the result under the same 'weather' key."""
    def fetch_fallback():
        return get_weather_weatherapi()

    result, fetched_at = get_cached('weather_fallback', fetch_fallback, ttl_seconds=7200)
    return result, fetched_at



# ---------------------------------------------------------------------------
# Met Office NSWWS weather warnings
# ---------------------------------------------------------------------------

_NSWWS_FEED_URL  = os.environ.get(
    "METOFFICE_NSWWS_FEED_URL",
    "https://prd.nswws.api.metoffice.gov.uk/v1.0/objects/feed",
)
_NSWWS_ATOM_NS   = "{http://www.w3.org/2005/Atom}"
_LEVEL_ORDER     = {"RED": 3, "AMBER": 2, "YELLOW": 1}
_nsws_last_error = ""

# London bounding box for a quick pre-filter before shapely
_LON_BBOX = (-0.51, 51.28, 0.33, 51.70)   # (min_lon, min_lat, max_lon, max_lat)


def _point_in_geojson(geometry, lat, lon):
    """Return True if (lat, lon) falls inside the GeoJSON MultiPolygon geometry."""
    if not _SHAPELY_OK:
        return True   # can't filter, assume it applies
    try:
        return shapely_shape(geometry).contains(Point(lon, lat))
    except Exception as e:
        print(f"NSWWS shapely error: {e}")
        return False


def _nswws_issued_url_from_feed(feed_xml):
    """Get the GeoJSON issued-warnings URL from the Atom feed (link rel=related)."""
    root = ET.fromstring(feed_xml)
    for link in root.findall(f"{_NSWWS_ATOM_NS}link"):
        if link.get("rel") == "related":
            href = link.get("href")
            if href:
                return href
    return None


def _nswws_request_headers():
    return {
        "x-api-key": NSWWS_API_KEY,
        "Accept": "application/json, application/vnd.geo+json;q=0.9, */*;q=0.8",
        "User-Agent": "frbc-tides/1.0",
    }


def _nswws_read_json(response, label):
    """Parse a Met Office NSWWS JSON body; tolerate empty issued-warning collections."""
    body = (response.content or b"").strip()
    if not body:
        print(
            f"NSWWS: {label} returned empty body "
            f"(HTTP {response.status_code}) {response.url}"
        )
        return {"type": "FeatureCollection", "features": []}
    ctype = (response.headers.get("Content-Type") or "").lower()
    if "json" not in ctype and not body.startswith((b"{", b"[")):
        snippet = body[:160].decode("utf-8", errors="replace")
        raise ValueError(
            f"NSWWS {label}: expected JSON, got {ctype or 'unknown'} — {snippet!r}"
        )
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        snippet = body[:160].decode("utf-8", errors="replace")
        raise ValueError(f"NSWWS {label}: invalid JSON ({e}) — {snippet!r}") from e


def _fetch_nswws():
    """
    Fetch Met Office NSWWS warnings for Hammersmith (LAT, LON).

    Step 1: GET /v1.0/objects/feed (Atom XML) with X-Api-Key.
    Step 2: GET the link[@rel=related] URL for issued warnings (GeoJSON).

    Returns a list sorted highest severity first. Each item:
      { level, weather_types, headline, area, valid_from, valid_to }
    """
    global _nswws_last_error
    _nswws_last_error = ""

    if not NSWWS_API_KEY:
        print("NSWWS: METOFFICE_NSWWS not set")
        return []

    session = requests.Session()
    session.headers.update(_nswws_request_headers())

    r = session.get(_NSWWS_FEED_URL, timeout=15)
    if r.status_code in (401, 403):
        _nswws_last_error = "authentication failed on Atom feed"
        print("NSWWS: authentication failed — check METOFFICE_NSWWS API key")
        return []
    r.raise_for_status()

    issued_url = _nswws_issued_url_from_feed(r.content)
    if not issued_url:
        _nswws_last_error = "no rel=related link in Atom feed"
        print("NSWWS: no rel=related link in Atom feed")
        return []

    data = None
    for attempt in range(2):
        r2 = session.get(issued_url, timeout=15)
        if r2.status_code == 404 and attempt == 0:
            print("NSWWS: issued URL expired (404), refreshing Atom feed")
            r = session.get(_NSWWS_FEED_URL, timeout=15)
            r.raise_for_status()
            issued_url = _nswws_issued_url_from_feed(r.content)
            if not issued_url:
                _nswws_last_error = "issued URL 404 and feed had no replacement link"
                return []
            continue
        if r2.status_code in (401, 403):
            _nswws_last_error = "authentication failed on issued warnings"
            print("NSWWS: authentication failed on issued warnings URL")
            return []
        r2.raise_for_status()
        data = _nswws_read_json(r2, "issued warnings")
        break

    if data is None:
        _nswws_last_error = "could not load issued warnings"
        return []

    warnings_out = []
    for feature in data.get("features", []):
        props    = feature.get("properties", {})
        level    = props.get("warningLevel", "").upper()
        status   = props.get("warningStatus", "")

        if level not in _LEVEL_ORDER:
            continue
        if status in ("EXPIRED", "CANCELLED"):
            continue

        # Quick bbox pre-filter, then precise polygon check
        geometry = feature.get("geometry")
        if geometry and not _point_in_geojson(geometry, LAT, LON):
            continue

        # Build area string from affectedAreas list
        # e.g. [{"regionName": "London", "subRegions": ["Greater London"]}]
        affected = props.get("affectedAreas", [])
        if affected:
            area_parts = []
            for a in affected[:3]:
                region = a.get("regionName", "")
                subs   = a.get("subRegions", [])
                if subs:
                    area_parts.append(f"{region} ({', '.join(subs[:2])})")
                elif region:
                    area_parts.append(region)
            area = "; ".join(area_parts) if area_parts else "your area"
        else:
            area = "your area"

        weather_types = props.get("weatherType", [])
        wtype = ", ".join(str(t).title() for t in weather_types) if weather_types else ""

        warnings_out.append({
            "level":         level,
            "weather_types": wtype,
            "headline":      props.get("warningHeadline", ""),
            "area":          area,
            "valid_from":    props.get("validFromDate", ""),
            "valid_to":      props.get("validToDate", ""),
        })

    warnings_out.sort(key=lambda w: _LEVEL_ORDER.get(w["level"], 0), reverse=True)
    return warnings_out


def _warning_for_window(warnings, window_start_h, window_end_h):
    """
    Return the highest-severity warning active during the given local-time
    window today, or None. window_start_h/end_h are integers (e.g. 6, 12).
    """
    now_local    = datetime.now(LONDON_TZ)
    window_start = now_local.replace(hour=window_start_h, minute=0, second=0, microsecond=0)
    window_end   = now_local.replace(hour=window_end_h,   minute=0, second=0, microsecond=0)

    for w in warnings:   # already sorted highest-first
        try:
            vf = datetime.fromisoformat(w["valid_from"].replace("Z", "+00:00")).astimezone(LONDON_TZ) if w["valid_from"] else None
            vt = datetime.fromisoformat(w["valid_to"].replace("Z", "+00:00")).astimezone(LONDON_TZ)   if w["valid_to"]   else None
        except Exception:
            vf, vt = None, None

        starts_before_end = (vf is None) or (vf < window_end)
        ends_after_start  = (vt is None) or (vt > window_start)
        if starts_before_end and ends_after_start:
            return w
    return None


def get_nswws_warnings():
    """Cached wrapper — refreshes every 15 minutes."""
    return get_cached("nswws", _fetch_nswws, ttl_seconds=900)

def get_kingston_flow():
    def fetch():
        url = (
            "https://environment.data.gov.uk/flood-monitoring/id/measures/"
            "3400TH-flow-water-i-15_min-m3_s/readings?_sorted&_limit=1"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        items = r.json().get('items', [])
        if items:
            val = items[0].get('value')
            if val is not None:
                flow = round(float(val))
                return {"flow": str(flow), "unit": "m\u00b3/s", "raw": flow}
        return None
    return get_cached('kingston_flow', fetch, ttl_seconds=900)

def get_thames_temperature():
    def fetch():
        url = (
            "https://environment.data.gov.uk/hydrology/id/measures/"
            "GPRSD8A-temp-i-subdaily-C/readings?latest"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        items = r.json().get('items', [])
        if items:
            reading = items[0]
            val = reading.get('value')
            if val is not None:
                return {
                    "temperature_c": round(float(val), 1),
                    "datetime": reading.get('dateTime', ''),
                }
        return None
    return get_cached('thames_temp', fetch, ttl_seconds=900)
    
def tide_direction_at(tides, check_utc):
    fut = [t for t in tides if t['dt_utc'] > check_utc]
    if fut:
        return "FLOOD TIDE" if fut[0]['EventType'] == "HighWater" else "EBB TIDE"
    return ""


def build_dashboard_data():
    now_utc = datetime.now(timezone.utc)
    now_lon = datetime.now(LONDON_TZ)
    is_bst  = now_lon.dst() != timedelta(0)
    off     = timedelta(hours=1) if is_bst else timedelta(0)

    results = {}

    def run(key, fn):
        try:
            results[key] = fn()
        except Exception as e:
            print(f"Thread error {key}: {e}")

    threads = [
        threading.Thread(target=run, args=('tides',         get_tides)),
        threading.Thread(target=run, args=('calendar',      get_calendar_events)),
        threading.Thread(target=run, args=('pla_flag',      get_pla_flag)),
        threading.Thread(target=run, args=('weather',       get_weather)),
        threading.Thread(target=run, args=('kingston_flow', get_kingston_flow)),
        threading.Thread(target=run, args=('richmond_lw',   get_richmond_observed_low_tide)),
        threading.Thread(target=run, args=('thames_temp', get_thames_temperature)),
        threading.Thread(target=run, args=('nswws',       get_nswws_warnings)),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=15)

    # Tides
    tides, t_up = results.get('tides', (None, ''))
    t_data = {"upcoming": [], "direction": "", "until": "", "launch_warning": "", "updated": t_up, "last_tide": None, "next_tide": None}

    if tides:
        fut = [t for t in tides if t['dt_utc'] > now_utc]
        pst = [t for t in tides if t['dt_utc'] <= now_utc]
        if fut:
            t_data["direction"] = "FLOOD TIDE" if fut[0]['EventType'] == "HighWater" else "EBB TIDE"
            t_data["until"] = (fut[0]['dt_utc'] + off).strftime('%H:%M')
            # Extract the height string for the current imminent tide target
            t_data["current_target_height"] = f"{fut[0]['Height']:.1f}m"
            for t in fut[:5]:
                t_data["upcoming"].append({
                    "label":  "HI" if t['EventType'] == 'HighWater' else "LO",
                    "time":   (t['dt_utc'] + off).strftime('%a %H:%M'),
                    "height": f"{t['Height']:.1f}m",
                    "type":   t['EventType']
                })
            if len(fut) >= 2:
                nt = fut[1]
                t_data["next_tide"] = {
                    "label":  "High" if nt['EventType'] == 'HighWater' else "Low",
                    "time":   (nt['dt_utc'] + off).strftime('%a %H:%M'),
                    "height": f"{nt['Height']:.1f}m",
                    "type":   nt['EventType']
                }
        if pst:
            lt = pst[-1]
            t_data["last_tide"] = {
                "label":  "High" if lt['EventType'] == 'HighWater' else "Low",
                "time":   (lt['dt_utc'] + off).strftime('%a %H:%M'),
                "height": f"{lt['Height']:.1f}m",
                "type":   lt['EventType']
            }
            
    # Calendar
    cal_data, cal_up = results.get('calendar', (None, ''))

    # Weather
    w_res, w_up = results.get('weather', (None, ''))
    weather = {"error": True, "updated": w_up}

    if w_res:
        m = w_res.get('morning')
        a = w_res.get('afternoon')

        def wvt(window_data, hour):
            if not window_data or not tides:
                return False
            check = now_utc.replace(hour=hour, minute=0, second=0, microsecond=0)
            dirn  = tide_direction_at(tides, check)
            wd    = window_data['direction']
            spd   = window_data['wind_max'] or 0
            return spd > 10 and (
                (dirn == "EBB TIDE"   and wd in ["S", "SE", "SW"]) or
                (dirn == "FLOOD TIDE" and wd in ["N", "NE", "NW"])
            )

        weather.update({
            "error":     False,
            "updated":   w_up,
            "source":    w_res.get('source', ''),
            "sunrise":   w_res['sunrise'],
            "sunset":    w_res['sunset'],
            "morning":   m,
            "afternoon": a,
        })


    # PLA Flag
    pla_f, pla_u = results.get('pla_flag', (None, ''))

    # Richmond observed low tide
    lw_data, lw_up = results.get('richmond_lw', (None, ''))

    # Kingston Flow
    flow_data, flow_up = results.get('kingston_flow', (None, ''))

    # Thames water temperature
    thames_temp_data, thames_temp_up = results.get('thames_temp', (None, ''))

    # Met Office NSWWS weather warnings
    if not NSWWS_API_KEY:
        nswws_status = "no_key"
        nswws_all, nswws_up = [], ""
    elif "nswws" not in results:
        nswws_status = "error"
        nswws_all, nswws_up = [], ""
    else:
        nswws_all, nswws_up = results["nswws"]
        if nswws_all is None:
            nswws_status = "error"
            nswws_all = []
        else:
            nswws_status = "ok"
    nswws_morning   = _warning_for_window(nswws_all, 6,  12)
    nswws_afternoon = _warning_for_window(nswws_all, 12, 21)

    return {
        "tides":               t_data,
        "pla_flag":            pla_f,
        "pla_updated":         pla_u,
        "richmond_lw":         lw_data,
        "richmond_lw_updated": lw_up,
        "weather":             weather,
        "cal": {
            **(cal_data or {"day_label": "TODAY", "list": []}),
            "updated": cal_up
        },
        "cal_updated":         cal_up,
        "kingston_flow":       flow_data,
        "flow_updated":        flow_up,
        "last_updated":        now_lon.strftime('%H:%M:%S'),
        "tz_label":            "BST" if is_bst else "GMT",
        "thames_temp":         thames_temp_data,
        "thames_temp_updated": thames_temp_up,
        "nswws_morning":       nswws_morning,
        "nswws_afternoon":     nswws_afternoon,
        "nswws_updated":       nswws_up,
        "nswws_status":        nswws_status,
        "nswws_count":         len(nswws_all),
        "nswws_error":         _nswws_last_error,
    }


@app.route("/")
def index():
    return render_template("index.html", d=build_dashboard_data())


@app.route("/data")
def data_endpoint():
    return jsonify(build_dashboard_data())

@app.route("/ping")
def ping():
    return "ok", 200


@app.route("/api/nswws-status")
def nswws_status_endpoint():
    """Lightweight diagnostic — hit this URL to verify Met Office NSWWS from Render."""
    if not NSWWS_API_KEY:
        return jsonify({"status": "no_key", "error": "METOFFICE_NSWWS not set"}), 200
    try:
        warnings = _fetch_nswws()
        return jsonify({
            "status": "ok",
            "count": len(warnings),
            "warnings": warnings[:3],
            "feed_url": _NSWWS_FEED_URL,
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "feed_url": _NSWWS_FEED_URL}), 500


def _prewarm():
    import time
    print("Pre-warming cache on startup...")
    for fn in (get_tides, get_kingston_flow, get_pla_flag, get_calendar_events, get_nswws_warnings):
        try:
            fn()
        except Exception as e:
            print(f"Pre-warm error: {e}")
    time.sleep(2)
    try:
        get_weather()
    except Exception as e:
        print(f"Pre-warm weather error: {e}")


if __name__ == "__main__":
    threading.Thread(target=_prewarm, daemon=True).start()
    app.run(debug=True)
