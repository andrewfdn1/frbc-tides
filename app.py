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

# ---------------------------------------------------------------------------

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
MO_SITE_KEY       = os.environ.get("METOFFICE_SITESPECIFIC", "")
MO_ATMO_KEY       = os.environ.get("METOFFICE_ATMOSPHERIC", "")
MO_OBS_KEY        = os.environ.get("METOFFICE_OBSERVATIONS", "")
LONDON_TZ         = ZoneInfo("Europe/London")
CAL_ID            = "info@fulhamreachboatclub.com"

# Hammersmith, London
LAT, LON = 51.488, -0.224

_cache           = {}
_cache_locks     = {}
_cache_locks_mu  = threading.Lock()
_cal_fail_until  = 0

# File-based backoff — survives process restarts and is shared across workers
_BACKOFF_FILE  = pathlib.Path(tempfile.gettempdir()) / "openmeteo_backoff.json"
# File-based morning weather store — survives process restarts
_MORNING_FILE  = pathlib.Path(tempfile.gettempdir()) / "morning_weather.json"


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
    global _nswws_last_error
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

# ---------------------------------------------------------------------------
# PLA Ebb Flag
# ---------------------------------------------------------------------------

_PLA_FLAG_EMBED_URL  = "https://pla.co.uk/pla-api-integration/ebb-tide-widget-embed"
_PLA_FLAG_JSON_URL   = "https://pla.co.uk/pla-proxy/five-minute?url=tides/ebb-flag"
_PLA_FLAG_IMAGE_BASE = "https://pla.co.uk/modules/custom/pla_api_integration/assets/flag_{colour}.png"

_PLA_LETTER_MAP = {"G": "green", "Y": "yellow", "R": "red", "B": "black"}


def _flag_result(colour, source):
    """Build a normalised flag result dict from a colour name (lowercase)."""
    return {"colour": colour, "image_url": _PLA_FLAG_IMAGE_BASE.format(colour=colour), "source": source}


def _pla_flag_from_embed():
    """Stage 1: scrape the PLA embed page and extract colour from the img src."""
    r = requests.get(_PLA_FLAG_EMBED_URL, timeout=5)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    img = soup.find('img')
    if not img:
        print("ERROR [pla_flag]: no <img> tag in embed page")
        return None
    src = img.get('src', '')
    for colour in ('green', 'yellow', 'red', 'black'):
        if f'flag_{colour}' in src:
            print(f"INFO [pla_flag]: embed → {colour}")
            return _flag_result(colour, 'embed')
    print(f"ERROR [pla_flag]: unrecognised img src '{src}'")
    return None


def _pla_flag_from_json():
    """Stage 2: fetch the PLA JSON endpoint and map letter code to colour."""
    r = requests.get(_PLA_FLAG_JSON_URL, timeout=5)
    r.raise_for_status()
    data = r.json()
    letter = data.get("flag_colour", "").strip().upper()
    colour = _PLA_LETTER_MAP.get(letter)
    if not colour:
        print(f"ERROR [pla_flag]: JSON returned unrecognised flag_colour '{letter}'")
        return None
    print(f"INFO [pla_flag]: JSON → {letter} → {colour}")
    return _flag_result(colour, 'json')


def _pla_flag_from_richmond():
    """Stage 3: derive flag colour from cached Richmond low tide height using PLA thresholds."""
    result = get_richmond_observed_low_tide()
    lw_data = result[0] if isinstance(result, tuple) else result
    if lw_data is None:
        return None
    metres = lw_data["metres"]
    if   metres >= 2.6: colour = "red"
    elif metres >= 1.7: colour = "yellow"
    elif metres >= 0:   colour = "green"
    else:               colour = "black"
    print(f"INFO [pla_flag]: Richmond fallback {metres}m → {colour}")
    return _flag_result(colour, 'richmond')


def get_pla_flag():
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
    if cached and cached.get('slot') == slot and cached['data'] is not None:
        return cached['data'], cached['fetched_at']

    # Work through the fallback chain
    # NOTE: _pla_flag_from_embed() removed — PLA embed page img src can serve a
    # stale/incorrect colour while the JSON endpoint stays correct.
    data = None
    for fn in (_pla_flag_from_json, _pla_flag_from_richmond):
        try:
            data = fn()
        except Exception as e:
            print(f"ERROR [pla_flag]: {fn.__name__} raised {e}")
            data = None
        if data is not None:
            break

    fetched_at = datetime.now(LONDON_TZ).strftime('%H:%M')
    if data is not None:
        _cache['pla_flag'] = {'data': data, 'fetched_at': fetched_at, 'slot': slot}
    elif cached:
        print("ERROR [pla_flag]: all sources failed, serving stale cache")
        return cached['data'], cached['fetched_at']

    return data, fetched_at


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

        if not isinstance(data, (list, dict)):
            raise ValueError(f"Unexpected Richmond chart response type: {type(data).__name__}")
        records = data if isinstance(data, list) else data.get("tpoints", [])
        if not isinstance(records, list):
            raise ValueError(f"Unexpected Richmond records type: {type(records).__name__}")
        for tp in records:
            if not isinstance(tp, dict):
                continue
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
# Met Office Weather DataHub — Site Specific (Global Spot)
# ---------------------------------------------------------------------------

_MO_SS_BASE  = "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/"
_MO_OBS_BASE = "https://data.hub.api.metoffice.gov.uk/observation-land/1/"
_MO_FOG_CODES = {5, 6}          # mist, fog
_MO_STORM_CODES = {28, 29, 30}  # thunder showers / thunder

# Geohash file — persists the nearest station geohash across process restarts
_GEOHASH_FILE = pathlib.Path(tempfile.gettempdir()) / "mo_obs_geohash.json"
# Calculated fallback for Hammersmith (51.488, -0.224) — used if nearest call fails
_MO_OBS_FALLBACK_GEOHASH = "gcpufv"

# In-process cache
_mo_obs_geohash      = None
_mo_obs_geohash_fail = 0   # timestamp after which we retry a failed nearest lookup


def _load_geohash():
    """Load cached geohash from file."""
    try:
        data = json.loads(_GEOHASH_FILE.read_text())
        return data.get("geohash")
    except Exception:
        return None


def _save_geohash(geohash):
    """Persist geohash to file."""
    try:
        _GEOHASH_FILE.write_text(json.dumps({"geohash": geohash}))
    except Exception as e:
        print(f"Could not persist geohash: {e}")


def _get_mo_obs_geohash():
    """
    Return the nearest observation station geohash for our lat/lon.
    Priority: in-process cache → file cache → API call → hardcoded fallback.
    """
    global _mo_obs_geohash, _mo_obs_geohash_fail

    # 1. In-process cache (fastest)
    if _mo_obs_geohash:
        return _mo_obs_geohash

    # 2. File cache (survives restarts)
    saved = _load_geohash()
    if saved:
        _mo_obs_geohash = saved
        return _mo_obs_geohash

    # 3. API call — skip if in backoff
    now = datetime.now(timezone.utc).timestamp()
    if now >= _mo_obs_geohash_fail:
        try:
            url = f"{_MO_OBS_BASE}nearest/{LAT}/{LON}"
            headers = {"apikey": MO_OBS_KEY, "accept": "application/json"}
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code in (401, 403):
                _mo_obs_geohash_fail = now + 3600
                raise Exception("Met Office Observations auth failed")
            if r.status_code == 429:
                _mo_obs_geohash_fail = now + 3600
                raise Exception("Met Office Observations rate limited")
            if not r.ok:
                _mo_obs_geohash_fail = now + 1800
                r.raise_for_status()
            data = r.json()
            if data and isinstance(data, list):
                geohash = data[0].get("geohash")
                if geohash:
                    _mo_obs_geohash = geohash
                    _save_geohash(geohash)
                    print(f"Met Office Observations geohash from API: {geohash}")
                    return _mo_obs_geohash
            _mo_obs_geohash_fail = now + 1800
        except Exception as e:
            print(f"Met Office Observations nearest failed, using fallback geohash: {e}")

    # 4. Hardcoded fallback — Hammersmith nearest station
    print(f"Met Office Observations using fallback geohash: {_MO_OBS_FALLBACK_GEOHASH}")
    return _MO_OBS_FALLBACK_GEOHASH


def _fetch_mo_observations():
    """
    Fetch ~48h of hourly observations for our nearest station.
    Returns a list of hourly records as in the sample data.
    """
    geohash = _get_mo_obs_geohash()
    url = _MO_OBS_BASE + geohash
    headers = {"apikey": MO_OBS_KEY, "accept": "application/json"}
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code in (401, 403):
        raise Exception("Met Office Observations auth failed")
    if r.status_code == 429:
        raise Exception("Met Office Observations rate limited")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise Exception("Met Office Observations: unexpected response format")
    return data


def _parse_mo_observations_morning(obs_records, existing_morning):
    """
    Build a morning window dict (06:00–12:00 local) from observed hourly records.
    Preserves rain_min/rain_max and uv_max from the forecast (not available in obs).
    Wind speeds are in m/s in the observations API — same as site-specific forecast.
    Wind direction is already a cardinal string (e.g. "NNW") — take the mode.
    """
    today = datetime.now(LONDON_TZ).date()
    entries = []
    for rec in obs_records:
        try:
            dt = datetime.fromisoformat(rec["datetime"].replace("Z", "+00:00")).astimezone(LONDON_TZ)
        except Exception:
            continue
        if dt.date() == today and 6 <= dt.hour < 12:
            entries.append(rec)

    if not entries:
        return None

    temps  = [float(e["temperature"])  for e in entries if e.get("temperature")  is not None]
    winds  = [float(e["wind_speed"])   for e in entries if e.get("wind_speed")   is not None]
    gusts  = [float(e["wind_gust"])    for e in entries if e.get("wind_gust")    is not None]
    dirs   = [e["wind_direction"]      for e in entries if e.get("wind_direction")]
    codes  = [int(e["weather_code"])   for e in entries if e.get("weather_code") is not None]

    # Prevailing direction: most frequent cardinal string
    direction = max(set(dirs), key=dirs.count) if dirs else None

    result = {
        "temp_min":  round(min(temps))        if temps  else None,
        "temp_max":  round(max(temps))        if temps  else None,
        "wind_min":  _ms_to_kmh(min(winds))   if winds  else None,
        "wind_max":  _ms_to_kmh(max(winds))   if winds  else None,
        "gust_min":  _ms_to_kmh(min(gusts))   if gusts  else None,
        "gust_max":  _ms_to_kmh(max(gusts))   if gusts  else None,
        "direction": direction,
        "fog":       any(c in _MO_FOG_CODES   for c in codes),
        "storm":     any(c in _MO_STORM_CODES for c in codes),
        # Preserve forecast values for fields not available in observations
        "rain_min":  existing_morning.get("rain_min")  if existing_morning else None,
        "rain_max":  existing_morning.get("rain_max")  if existing_morning else None,
        "uv_max":    existing_morning.get("uv_max")    if existing_morning else None,
    }
    return result


def _ms_to_kmh(ms):
    return round(float(ms) * 3.6)


def _fetch_metoffice_timeseries(api_key, timestep="hourly"):
    """Fetch GeoJSON timeSeries from Met Office Global Spot API."""
    url = f"{_MO_SS_BASE}{timestep}"
    headers = {"apikey": api_key, "accept": "application/json"}
    params = {
        "latitude": LAT,
        "longitude": LON,
        "excludeParameterMetadata": "true",
        "includeLocationName": "false",
    }
    r = requests.get(url, headers=headers, params=params, timeout=15)
    if r.status_code in (401, 403):
        raise Exception("Met Office authentication failed — check API key")
    if r.status_code == 429:
        raise Exception("Met Office rate limited")
    r.raise_for_status()
    features = r.json().get("features", [])
    if not features:
        raise Exception("Met Office response has no features")
    ts = features[0].get("properties", {}).get("timeSeries", [])
    if not ts:
        raise Exception("Met Office empty timeSeries")
    return ts


def _metoffice_window_from_entries(entries):
    if not entries:
        return None

    temps = []
    for e in entries:
        for k in ("minScreenAirTemp", "maxScreenAirTemp", "screenTemperature"):
            if e.get(k) is not None:
                temps.append(float(e[k]))

    winds, gusts, dirs, rains, uvs, codes = [], [], [], [], [], []
    for e in entries:
        if e.get("windSpeed10m") is not None:
            winds.append(float(e["windSpeed10m"]))
        g = e.get("max10mWindGust")
        if g is None:
            g = e.get("windGustSpeed10m")
        if g is not None:
            gusts.append(float(g))
        if e.get("windDirectionFrom10m") is not None:
            dirs.append(float(e["windDirectionFrom10m"]))
        if e.get("probOfPrecipitation") is not None:
            rains.append(float(e["probOfPrecipitation"]))
        if e.get("uvIndex") is not None:
            uvs.append(float(e["uvIndex"]))
        if e.get("significantWeatherCode") is not None:
            codes.append(int(e["significantWeatherCode"]))

    sferics = any((e.get("probOfSferics") or 0) > 0 for e in entries)

    return {
        "temp_min":  round(min(temps)) if temps else None,
        "temp_max":  round(max(temps)) if temps else None,
        "wind_min":  _ms_to_kmh(min(winds)) if winds else None,
        "wind_max":  _ms_to_kmh(max(winds)) if winds else None,
        "gust_min":  _ms_to_kmh(min(gusts)) if gusts else None,
        "gust_max":  _ms_to_kmh(max(gusts)) if gusts else None,
        "direction": prevailing_direction(dirs),
        "rain_min":  round(min(rains)) if rains else None,
        "rain_max":  round(max(rains)) if rains else None,
        "uv_max":    round(max(uvs), 1) if uvs else None,
        "fog":       any(c in _MO_FOG_CODES for c in codes),
        "storm":     any(c in _MO_STORM_CODES for c in codes) or sferics,
    }


def _fetch_sunrise_sunset():
    """Sunrise/sunset — tries WeatherAPI first, falls back to Open-Meteo."""
    if WEATHERAPI_KEY:
        try:
            url = (
                f"https://api.weatherapi.com/v1/forecast.json"
                f"?key={WEATHERAPI_KEY}"
                f"&q={LAT},{LON}"
                f"&days=1&aqi=no&alerts=no"
            )
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            astro = r.json()['forecast']['forecastday'][0]['astro']
            def to_24h(t):
                return datetime.strptime(t, '%I:%M %p').strftime('%H:%M')
            return to_24h(astro['sunrise']), to_24h(astro['sunset'])
        except Exception as e:
            print(f"WeatherAPI sunrise/sunset failed, trying Open-Meteo: {e}")

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&daily=sunrise,sunset"
        "&timezone=Europe%2FLondon"
        "&forecast_days=1"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    daily = r.json()["daily"]
    return daily["sunrise"][0][-5:], daily["sunset"][0][-5:]


def _parse_metoffice_timeseries(time_series, source_label):
    today = datetime.now(LONDON_TZ).date()

    def bucket(start_h, end_h):
        entries = []
        for e in time_series:
            t = datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone(LONDON_TZ)
            if t.date() != today:
                continue
            if start_h <= t.hour < end_h:
                entries.append(e)
        return _metoffice_window_from_entries(entries)

    try:
        sunrise, sunset = _fetch_sunrise_sunset()
    except Exception as e:
        print(f"Sunrise/sunset fallback failed: {e}")
        sunrise, sunset = "", ""

    return {
        "morning":   bucket(6, 12),
        "afternoon": bucket(12, 20),
        "sunrise":   sunrise,
        "sunset":    sunset,
        "source":    source_label,
    }


def get_weather_metoffice():
    """
    Met Office Weather DataHub Global Spot (site-specific JSON API).
    Uses METOFFICE_SITESPECIFIC key only; tries hourly then three-hourly.
    Note: Atmospheric API returns GRIB2 format, not GeoJSON, so it's not compatible.
    """
    if not MO_SITE_KEY:
        raise Exception("No Met Office DataHub Site-Specific API key configured (METOFFICE_SITESPECIFIC)")

    last_err = None
    for timestep in ("hourly", "three-hourly"):
        try:
            ts = _fetch_metoffice_timeseries(MO_SITE_KEY, timestep)
            src = f"Met Office Site-Specific ({timestep})"
            return _parse_metoffice_timeseries(ts, src)
        except Exception as e:
            last_err = e
            print(f"Met Office Site-Specific {timestep} failed: {e}")
    raise last_err or Exception("Met Office weather unavailable")


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
                'rain_min':  round(min(rains)),
                'rain_max':  round(max(rains)),
                'uv_max':    round(max(uvs), 1) if uvs else None,
                'fog':       any(c in FOG_CODES   for c in codes),
                'storm':     any(c in STORM_CODES for c in codes),
            }

        return {
            'morning':   window(6,  12),
            'afternoon': window(12, 20),
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
# Weather: Met Office DataHub → Open-Meteo → WeatherAPI
# ---------------------------------------------------------------------------

def _fetch_openmeteo():
    wx_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,"
        "wind_gusts_10m,weather_code,precipitation_probability,uv_index"
        "&daily=sunrise,sunset"
        "&timezone=Europe%2FLondon"
        "&forecast_days=1"
        "&past_hours=12"
    )

    wx_res = requests.get(wx_url, timeout=10)
    if wx_res.status_code == 429:
        retry_after = int(wx_res.headers.get("Retry-After", 3600))
        _set_fail_until('weather_openmeteo', retry_after)
        raise Exception(f"Open-Meteo rate limited, retry after {retry_after}s")
    wx_res.raise_for_status()
    d = wx_res.json()

    hourly = d['hourly']
    daily  = d['daily']
    times  = hourly['time']

    def window(start_h, end_h):
        today_str = datetime.now(LONDON_TZ).strftime('%Y-%m-%d')
        indices = [i for i, t in enumerate(times) if t[:10] == today_str and start_h <= int(t[11:13]) < end_h]
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
        'afternoon': window(12, 20),
        'sunrise':   daily['sunrise'][0][-5:],
        'sunset':    daily['sunset'][0][-5:],
        'source':    'Open-Meteo',
    }


def _fetch_weather_with_fallbacks():
    """Try Met Office DataHub, then WeatherAPI, then Open-Meteo."""
    if MO_SITE_KEY:
        try:
            return get_weather_metoffice()
        except Exception as e:
            print(f"Met Office weather failed, trying fallbacks: {e}")

    if WEATHERAPI_KEY:
        try:
            return get_weather_weatherapi()
        except Exception as e:
            print(f"WeatherAPI failed, trying Open-Meteo: {e}")

    now_ts = datetime.now(timezone.utc).timestamp()
    if now_ts >= _get_fail_until('weather_openmeteo'):
        try:
            return _fetch_openmeteo()
        except Exception as e:
            print(f"Open-Meteo failed: {e}")
    else:
        print("Open-Meteo in backoff")

    raise Exception("All weather sources failed")


def _load_morning_store():
    """Load morning weather data from file, returns {date_str: morning_dict}."""
    try:
        return json.loads(_MORNING_FILE.read_text())
    except Exception:
        return {}


def _save_morning_store(store):
    """Persist morning weather data to file."""
    try:
        _MORNING_FILE.write_text(json.dumps(store))
    except Exception as e:
        print(f"Could not persist morning store: {e}")


def _fetch_weather_with_observations():
    """
    Fetch weather, persist morning data to file, and after midday replace
    morning forecast with actual observations if available.
    Called inside get_cached so it only runs when the cache expires.
    """
    result = _fetch_weather_with_fallbacks()
    today_str = datetime.now(LONDON_TZ).date().isoformat()
    now_lon   = datetime.now(LONDON_TZ)
    store     = _load_morning_store()

    # Purge old dates
    store = {k: v for k, v in store.items() if k == today_str}

    # Save morning forecast data if we have it
    if result.get('morning') is not None:
        store[today_str] = result['morning']
        _save_morning_store(store)

    # After midday: try to replace with actual observations (own cache — refreshes hourly)
    if now_lon.hour >= 12 and MO_OBS_KEY:
        try:
            obs, _ = get_cached('mo_observations', _fetch_mo_observations, ttl_seconds=3600)
            if obs:
                obs_morning = _parse_mo_observations_morning(obs, store.get(today_str))
                if obs_morning:
                    store[today_str] = obs_morning
                    _save_morning_store(store)
                    print("Met Office Observations: morning data updated")
        except Exception as e:
            print(f"Met Office Observations morning fetch failed, keeping forecast: {e}")

    # Restore morning from file (covers both: no morning in result, and always-prefer-observed)
    if today_str in store:
        result = dict(result)
        result['morning'] = store[today_str]

    return result


def get_weather():
    result, fetched_at = get_cached('weather', _fetch_weather_with_observations, ttl_seconds=7200)
    if result is None:
        raise Exception("Weather unavailable")
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
_nswws_last_error = ""

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


def _nswws_headline_lines(morning, afternoon):
    """
    Human-readable warning text for below the hazards table.
    period: 'All day', 'AM (0600–1200)', or 'PM (1200–2000)'.
    """
    m_h = (morning or {}).get("headline", "").strip() if morning else ""
    a_h = (afternoon or {}).get("headline", "").strip() if afternoon else ""
    if not m_h and not a_h:
        return []

    if m_h and a_h and m_h == a_h:
        level = (morning or afternoon).get("level", "")
        return [{"period": "All day", "headline": m_h, "level": level}]

    lines = []
    if m_h:
        lines.append({
            "period": "AM (0600–1200)",
            "headline": m_h,
            "level": morning.get("level", ""),
        })
    if a_h:
        lines.append({
            "period": "PM (1200–2000)",
            "headline": a_h,
            "level": afternoon.get("level", ""),
        })
    return lines


def _nswws_upcoming_lines(warnings):
    """
    Return warning lines for warnings that start in the next 7 days but are
    not active today. Each line includes a human-readable day-range label.
    """
    now_local   = datetime.now(LONDON_TZ)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    lookahead   = today_start + timedelta(days=7)

    lines = []
    seen_headlines = set()
    for w in warnings:
        try:
            vf = datetime.fromisoformat(w["valid_from"].replace("Z", "+00:00")).astimezone(LONDON_TZ) if w["valid_from"] else None
            vt = datetime.fromisoformat(w["valid_to"].replace("Z", "+00:00")).astimezone(LONDON_TZ)   if w["valid_to"]   else None
        except Exception:
            vf, vt = None, None

        # Skip if active today (covered by nswws_headlines)
        active_today = (vf is None or vf < today_end) and (vt is None or vt > today_start)
        if active_today:
            continue

        # Only include if starts within lookahead
        if vf is None or not (today_end <= vf <= lookahead):
            continue

        headline = w.get("headline", "").strip()
        if not headline or headline in seen_headlines:
            continue
        seen_headlines.add(headline)

        # Format day range: "Wed 25 Jun" or "Wed 25 Jun – Fri 27 Jun"
        from_label = vf.strftime("%-d %b")
        from_day   = vf.strftime("%a")
        if vt:
            to_label = vt.strftime("%-d %b")
            to_day   = vt.strftime("%a")
            if from_label == to_label:
                period = f"{from_day} {from_label}"
            else:
                period = f"{from_day} {from_label} \u2013 {to_day} {to_label}"
        else:
            period = f"From {from_day} {from_label}"

        lines.append({
            "period":   period,
            "headline": headline,
            "level":    w["level"],
        })

    return lines


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

_thames_temp_fail_until = 0

def get_thames_temperature():
    global _thames_temp_fail_until
    now_ts = datetime.now(timezone.utc).timestamp()
    if now_ts < _thames_temp_fail_until:
        if 'thames_temp' in _cache:
            return _cache['thames_temp']['data'], _cache['thames_temp']['fetched_at']
        return None, ''

    def fetch():
        global _thames_temp_fail_until
        url = (
            "https://environment.data.gov.uk/hydrology/id/measures/"
            "GPRSD8A-temp-i-subdaily-C/readings?latest"
        )
        try:
            r = requests.get(url, timeout=5)
            r.raise_for_status()
        except Exception as e:
            _thames_temp_fail_until = datetime.now(timezone.utc).timestamp() + 900
            print(f"thames_temp backing off 900s: {e}")
            raise
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

        # Bridge tides table — apply fixed offsets (minutes) from Hammersmith reference
        # Offsets are approximate and based on standard Thames tidal progression
        _BRIDGES = [
            {"name": "Putney",        "hw_off": -5,  "lw_off": -8},
            {"name": "Hammersmith",   "hw_off":  0,  "lw_off":  0},
            {"name": "Chiswick",      "hw_off": +8,  "lw_off": +10},
            {"name": "Richmond",      "hw_off": +25, "lw_off": +30},
        ]
        # next tide event (fut[0]) determines whether "next" is HW or LW
        _next_is_hw = fut[0]['EventType'] == 'HighWater'
        # Gather up to 4 future events to find next HW and next LW at Hammersmith
        _next_hw_utc = next((t['dt_utc'] for t in fut if t['EventType'] == 'HighWater'), None)
        _next_lw_utc = next((t['dt_utc'] for t in fut if t['EventType'] == 'LowWater'),  None)
        _next_hw_h   = next((t['Height'] for t in fut if t['EventType'] == 'HighWater'), None)
        _next_lw_h   = next((t['Height'] for t in fut if t['EventType'] == 'LowWater'),  None)

        def _fmt_bridge_time(base_utc, offset_mins):
            if base_utc is None:
                return None
            adjusted = base_utc + timedelta(minutes=offset_mins) + off
            return adjusted.strftime('%H:%M')

        _bridge_rows = []
        for _b in _BRIDGES:
            _bridge_rows.append({
                "name":     _b["name"],
                "hw_time":  _fmt_bridge_time(_next_hw_utc, _b["hw_off"]),
                "lw_time":  _fmt_bridge_time(_next_lw_utc, _b["lw_off"]),
                "hw_height": f"{_next_hw_h:.1f}m" if _next_hw_h is not None else None,
                "lw_height": f"{_next_lw_h:.1f}m" if _next_lw_h is not None else None,
                "next_is_hw": _next_is_hw,
            })
        t_data["bridge_tides"] = _bridge_rows
        t_data["next_hw_utc_iso"] = _next_hw_utc.isoformat() if _next_hw_utc else None
        t_data["next_lw_utc_iso"] = _next_lw_utc.isoformat() if _next_lw_utc else None

        # Spring/Neap indicator — use all API data (7 days) to compute daily ranges
        # and derive both current type and multi-day trend
        _tidal_range_info = None
        if _next_hw_h is not None and _next_lw_h is not None:
            _cur_range = round(_next_hw_h - _next_lw_h, 1)

            # Thresholds (approximate Hammersmith values): spring >5.5m, neap <4.0m
            _SPRING_THRESHOLD = 5.5
            _NEAP_THRESHOLD   = 4.0
            if _cur_range >= _SPRING_THRESHOLD:
                _tide_type = "Spring"
            elif _cur_range <= _NEAP_THRESHOLD:
                _tide_type = "Neap"
            else:
                _tide_type = "Moderate"

            # Build daily tidal ranges from the full dataset (all tides, past and future)
            from collections import defaultdict as _dd
            _daily_heights = _dd(list)
            for _t in tides:
                _day = (_t['dt_utc'] + off).strftime('%Y-%m-%d')
                _daily_heights[_day].append(_t['Height'])
            _daily_ranges = {
                _d: round(max(_h) - min(_h), 2)
                for _d, _h in _daily_heights.items()
                if len(_h) >= 2   # need at least one HW + one LW
            }

            # Trend: look at the last 3 days of range data and fit a direction.
            # We use a simple sign-of-slope on the sorted daily ranges.
            _today_str = now_lon.strftime('%Y-%m-%d')
            _sorted_days = sorted(_daily_ranges.keys())
            # Use days up to and including today + the next 2 for a short window
            _window = [_d for _d in _sorted_days if _d <= _today_str][-2:] + \
                      [_d for _d in _sorted_days if _d > _today_str][:2]
            _window = sorted(set(_window))

            _trend = None
            if len(_window) >= 2:
                _range_vals = [_daily_ranges[_d] for _d in _window]
                # Count increasing vs decreasing steps
                _up   = sum(1 for i in range(len(_range_vals)-1) if _range_vals[i+1] > _range_vals[i])
                _down = sum(1 for i in range(len(_range_vals)-1) if _range_vals[i+1] < _range_vals[i])
                if _up > _down:
                    _trend = "Spring"
                elif _down > _up:
                    _trend = "Neap"
                # tie → _trend stays None (transitioning / at peak)

            _tidal_range_info = {
                "range":     _cur_range,
                "hw":        round(_next_hw_h, 1),
                "lw":        round(_next_lw_h, 1),
                "tide_type": _tide_type,
                "trend":     _trend,   # "Spring", "Neap", or None
            }
        t_data["tidal_range"] = _tidal_range_info

        # Today's tides for the calendar column — HH:MM only, today's date only
        today_local = now_lon.date()
        t_data["today_tides"] = [
            {
                "label":  "High" if t['EventType'] == 'HighWater' else "Low",
                "time":   (t['dt_utc'] + off).strftime('%H:%M'),
                "height": f"{t['Height']:.1f}m",
            }
            for t in tides
            if (t['dt_utc'] + off).date() == today_local
        ]
            
    # Calendar
    cal_data, cal_up = results.get('calendar', (None, ''))

    # Weather
    w_res, w_up = results.get('weather', (None, ''))
    weather = {"error": True, "updated": w_up}

    if w_res:
        m = w_res.get('morning')
        a = w_res.get('afternoon')

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
    nswws_afternoon = _warning_for_window(nswws_all, 12, 20)
    nswws_headlines = _nswws_headline_lines(nswws_morning, nswws_afternoon)
    nswws_upcoming  = _nswws_upcoming_lines(nswws_all)

    # Pre-sorted marker list for the TODAY calendar column
    # Combines tides + sunrise + sunset into a single time-ordered list
    _markers = []
    for _t in t_data.get("today_tides", []):
        _markers.append({"type": "tide", "time": _t["time"], "label": _t["label"], "height": _t["height"]})
    if weather.get("sunrise"):
        _markers.append({"type": "sunrise", "time": weather["sunrise"]})
    if weather.get("sunset"):
        _markers.append({"type": "sunset", "time": weather["sunset"]})
    _markers.sort(key=lambda x: x["time"])

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
        "today_markers":       _markers,
        "thames_temp":         thames_temp_data,
        "thames_temp_updated": thames_temp_up,
        "nswws_morning":       nswws_morning,
        "nswws_afternoon":     nswws_afternoon,
        "nswws_headlines":     nswws_headlines,
        "nswws_upcoming":      nswws_upcoming,
        "nswws_updated":       nswws_up,
        "nswws_status":        nswws_status,
        "nswws_count":         len(nswws_all),
        "nswws_error":         _nswws_last_error,
    }

# ---------------------------------------------------------------------------
# Wind grid data for radar overlay — Open-Meteo with caching
# ---------------------------------------------------------------------------

def get_wind_grid():
    """
    Fetch wind data for a grid around the map center.
    Returns a sparse grid of points with wind speed/direction for arrow overlay.
    Cached for 1 hour to minimize API calls.
    """
    def fetch():
        now_ts = datetime.now(timezone.utc).timestamp()
        
        # Honour backoff
        if now_ts < _get_fail_until('wind_grid'):
            raise Exception("Wind grid in backoff")
        
        # Grid: | `grid_size = 2` | 4 | Very sparse
        # `grid_size = 3` | 9 arrows | Few
        # `grid_size = 4` | 16 arrows | Moderate
        # `grid_size = 5` | 25 arrows | Dense
        # Centered on Hammersmith, spanning ~0.5 degrees
        lat_min, lat_max = 51.1, 51.9   
        lon_min, lon_max = -3.2, 2.74
        grid_size = 4
        
        lats = [lat_min + i * (lat_max - lat_min) / (grid_size - 1) for i in range(grid_size)]
        lons = [lon_min + i * (lon_max - lon_min) / (grid_size - 1) for i in range(grid_size)]
        
        points = []
        for lat in lats:
            for lon in lons:
                points.append((round(lat, 3), round(lon, 3)))
        
        # Batch request - Open-Meteo supports multiple locations
        lat_str = ",".join(str(p[0]) for p in points)
        lon_str = ",".join(str(p[1]) for p in points)
        
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat_str}&longitude={lon_str}"
            "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
            "&timezone=Europe%2FLondon"
        )
        
        r = requests.get(url, timeout=15)
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", 3600))
            _set_fail_until('wind_grid', retry_after)
            raise Exception(f"Open-Meteo rate limited, retry after {retry_after}s")
        r.raise_for_status()
        
        data = r.json()
        
        # Handle both single-location and multi-location responses
        if isinstance(data, list):
            locations = data
        else:
            locations = [data]
        
        wind_data = []
        for i, loc in enumerate(locations):
            current = loc.get("current", {})
            wind_data.append({
                "lat": points[i][0],
                "lon": points[i][1],
                "speed": current.get("wind_speed_10m"),  # km/h
                "direction": current.get("wind_direction_10m"),  # degrees
                "gusts": current.get("wind_gusts_10m"),
            })
        
        return {
            "points": wind_data,
            "generated_at": datetime.now(LONDON_TZ).isoformat(),
        }
    
    return get_cached('wind_grid', fetch, ttl_seconds=3600)  # 1 hour cache

@app.route("/")
def index():
    return render_template("index.html", d=build_dashboard_data())

@app.route('/radar')
def radar():
    return render_template("index2.html", d=build_dashboard_data())

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

@app.route("/api/wind")
def wind_endpoint():
    """Wind grid data for radar map overlay."""
    try:
        data, fetched_at = get_wind_grid()
        if data is None:
            return jsonify({"error": "Wind data unavailable", "points": []}), 503
        return jsonify({
            **data,
            "fetched_at": fetched_at,
            "cache_ttl": 3600,
        })
    except Exception as e:
        print(f"Wind endpoint error: {e}")
        return jsonify({"error": str(e), "points": []}), 500

@app.route("/api/overlay")
def api_overlay():
    now = datetime.now(timezone.utc)

    # PLA flag
    flag_colour = "UNKNOWN"
    try:
        flag_data, _ = get_pla_flag()
        flag_colour = flag_data.get('colour', 'UNKNOWN').upper()
    except Exception:
        pass

    # Next tide — compare next HW and LW UTC times, take whichever is sooner
    next_tide_label = None
    next_tide_time = None
    try:
        tides, _ = get_tides()
        lw_iso = None
        # Find next HW and LW
        for e in tides:
            if e['dt_utc'] > now:
                if "High" in e['EventType'] and hw_iso is None:
                    hw_iso = e['dt_utc']
                if "Low" in e['EventType'] and lw_iso is None:
                    lw_iso = e['dt_utc']
                if hw_iso and lw_iso:
                    break
        if hw_iso and lw_iso:
            if hw_iso < lw_iso:
                next_tide_label = "High"
                next_tide_time = hw_iso.astimezone(LONDON_TZ).strftime("%H:%M")
            else:
                next_tide_label = "Low"
                next_tide_time = lw_iso.astimezone(LONDON_TZ).strftime("%H:%M")
        elif hw_iso:
            next_tide_label = "High"
            next_tide_time = hw_iso.astimezone(LONDON_TZ).strftime("%H:%M")
        elif lw_iso:
            next_tide_label = "Low"
            next_tide_time = lw_iso.astimezone(LONDON_TZ).strftime("%H:%M")
    except Exception:
        pass

    # Pontoon warning — within 60 mins after low tide
    pontoon_warning = False
    try:
        tides, _ = get_tides()
        past_lows = [e for e in tides if "Low" in e['EventType'] and e['dt_utc'] < now]
        if past_lows:
            diff = (now - past_lows[-1]['dt_utc']).total_seconds()
            pontoon_warning = 0 <= diff <= 3600
    except Exception:
        pass

    return jsonify({
        "flag":            flag_colour,
        "next_tide_label": next_tide_label,
        "next_tide_time":  next_tide_time,
        "pontoon_warning": pontoon_warning,
    })
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
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
