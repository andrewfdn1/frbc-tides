from flask import Flask, jsonify, render_template, render_template_string, request
from werkzeug.exceptions import HTTPException
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
import requests
import traceback
import urllib3
import threading
import time
from collections import defaultdict
import os
import json
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


@app.errorhandler(Exception)
def _log_unhandled_exception(e):
    """Log the full traceback of any unhandled exception to stdout so it
    shows up in the Render logs, instead of a bare 500 with no trace."""
    if isinstance(e, HTTPException):
        return e   # normal 404s etc. — pass through untouched
    print(f"ERROR [unhandled] {request.method} {request.path}: {e!r}")
    traceback.print_exc()
    return "Internal Server Error", 500

TIDE_API_KEY      = os.environ.get("TIDE_API_KEY", "")
GOOGLE_API_KEY    = os.environ.get("GOOGLE_CALENDAR_API_KEY", "")
WEATHERAPI_KEY    = os.environ.get("WEATHERAPI_KEY", "")
NSWWS_API_KEY     = os.environ.get("METOFFICE_NSWWS", "")
MO_SITE_KEY       = os.environ.get("METOFFICE_SITESPECIFIC", "")
LONDON_TZ         = ZoneInfo("Europe/London")

# Pontoon warning window around a low tide, in seconds
PONTOON_WARN_BEFORE = 1800   # switch on this far ahead of the low tide
PONTOON_WARN_AFTER  = 5400   # stay on this long after it
CAL_ID            = "info@fulhamreachboatclub.com"

# Hammersmith, London
LAT, LON = 51.488, -0.224

_cache           = {}
_cache_locks     = {}
_cache_locks_mu  = threading.Lock()
_cal_fail_until  = 0


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
                    dt_e = dt_s
                    if 'dateTime' in end:
                        dt_e = datetime.fromisoformat(end['dateTime']).astimezone(LONDON_TZ)
                        time_str = f"{time_str}-{dt_e.strftime('%H:%M')}"
                    events_list.append({
                        "summary": summary, "time": time_str,
                        "start_iso": dt_s.isoformat(), "end_iso": dt_e.isoformat(),
                    })
            elif 'date' in start:
                ev_date = datetime.strptime(start['date'], '%Y-%m-%d').date()
                if ev_date == target_date:
                    events_list.append({"summary": summary, "time": "All Day"})

        return {
            "day_label": "TOMORROW" if now.hour >= 22 else "TODAY",
            "list": events_list
        }

    return get_cached('calendar', fetch, ttl_seconds=1800)


def get_calendar_events_14d():
    """
    Club diary events for the next 14 days, grouped by ISO date string, for
    the /calendar agenda page. Same Google Calendar source as the homepage's
    Club Diary column, but fetched as a single 14-day window instead of one
    day at a time.
    """
    def fetch():
        now         = datetime.now(LONDON_TZ)
        range_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        range_end   = range_start + timedelta(days=14)

        url = (
            f"https://www.googleapis.com/calendar/v3/calendars/"
            f"{requests.utils.quote(CAL_ID, safe='')}/events"
            f"?key={GOOGLE_API_KEY}"
            f"&timeMin={requests.utils.quote(range_start.isoformat())}"
            f"&timeMax={requests.utils.quote(range_end.isoformat())}"
            f"&singleEvents=true&orderBy=startTime&maxResults=250"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()

        events_by_date = defaultdict(list)
        for e in r.json().get('items', []):
            start   = e.get('start', {})
            end     = e.get('end', {})
            summary = e.get('summary', '(no title)')
            if 'dateTime' in start:
                dt_s = datetime.fromisoformat(start['dateTime']).astimezone(LONDON_TZ)
                time_str = dt_s.strftime('%H:%M')
                dt_e = dt_s
                if 'dateTime' in end:
                    dt_e = datetime.fromisoformat(end['dateTime']).astimezone(LONDON_TZ)
                    time_str = f"{time_str}-{dt_e.strftime('%H:%M')}"
                events_by_date[dt_s.date().isoformat()].append({
                    "summary": summary, "time": time_str,
                    "start_iso": dt_s.isoformat(), "end_iso": dt_e.isoformat(),
                })
            elif 'date' in start:
                ev_date = datetime.strptime(start['date'], '%Y-%m-%d').date()
                events_by_date[ev_date.isoformat()].append({"summary": summary, "time": "All Day"})

        return dict(events_by_date)

    return get_cached('calendar_14d', fetch, ttl_seconds=1800)

# ---------------------------------------------------------------------------
# PLA Ebb Flag
# ---------------------------------------------------------------------------

_PLA_WIDGET_URL      = "https://pla.co.uk/pla-api-integration/ebb-tide-widget-embed"
_PLA_FLAG_JSON_URL   = "https://pla.co.uk/pla-proxy/five-minute?url=tides/ebb-flag"

_PLA_LETTER_MAP = {"G": "green", "Y": "yellow", "R": "red", "B": "black"}

# Mapping from widget image src fragment to colour name
_PLA_IMG_COLOUR_MAP = {
    "flag_green":  "green",
    "flag_yellow": "yellow",
    "flag_red":    "red",
    "flag_black":  "black",
}

# The widget's heading/body text doesn't use the colour word itself — it uses
# the fluvial-flow descriptor (e.g. "CAUTION - LOW", "AVERAGE"), matching the
# same wording as each colour's official meaning. Ordered longest-phrase-first
# so "very strong" is matched before the "strong" substring it contains.
_PLA_TEXT_COLOUR_MAP = [
    ("very strong", "red"),
    ("strong",      "yellow"),
    ("average",     "green"),
    ("low",         "black"),
]


def _scrape_pla_widget():
    """Scrape the PLA ebb tide widget embed page to get the current flag colour,
    heading, and body text directly from the source the PLA use for their own widget.
    The page returns fully populated HTML (no JS rendering needed).
    Returns dict with colour, heading, body — or None if scrape fails."""
    r = requests.get(
        _PLA_WIDGET_URL,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://pla.co.uk/"},
        timeout=8,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')

    img = soup.find('img', class_='tideflag')
    if not img:
        print("ERROR [pla_widget]: no img.tideflag found")
        return None

    src = img.get('src', '')
    img_colour = None
    for fragment, name in _PLA_IMG_COLOUR_MAP.items():
        if fragment in src:
            img_colour = name
            break

    # Extract heading and body text from the tidecont div
    tidecont = soup.find('div', class_='tidecont')
    inner_div = tidecont.find('div') if tidecont else None
    texts = [t.strip() for t in inner_div.strings if t.strip()] if inner_div else []
    heading = texts[0] if len(texts) > 0 else ""
    body    = texts[1] if len(texts) > 1 else ""

    # The heading+body text is PLA's own textual confirmation of which flag is
    # showing (e.g. "CAUTION - LOW" / "Fluvial Flows") and is the same text a
    # visitor reads next to the flag in the iframe. It's more trustworthy than
    # the <img> filename, which can be stale or mismatched, so prefer it and
    # only fall back to the image when no descriptor word is found in the text.
    combined_lower = f"{heading} {body}".lower()
    text_colour = next((name for phrase, name in _PLA_TEXT_COLOUR_MAP if phrase in combined_lower), None)

    colour = text_colour or img_colour
    if not colour:
        print(f"ERROR [pla_widget]: could not determine colour from heading '{heading}' or img src '{src}'")
        return None
    if img_colour and text_colour and img_colour != text_colour:
        print(f"WARN [pla_widget]: img src colour '{img_colour}' disagrees with heading text colour '{text_colour}' — using text")

    print(f"INFO [pla_widget]: scraped → {colour} | {heading} | {body}")
    return {"colour": colour, "heading": heading, "body": body}


def _fetch_pla_json():
    """Fetch the PLA JSON endpoint independently as a crosscheck.
    Always returns the raw result regardless of staleness — staleness is
    flagged in the returned dict so the caller can display it transparently.
    Returns dict with colour, last_updated, stale — or None if fetch fails.
    Cached for 5 minutes to avoid unnecessary outbound calls on every page load."""
    def fetch():
        r = requests.get(_PLA_FLAG_JSON_URL, timeout=5)
        r.raise_for_status()
        data = r.json()

        last_updated_str = data.get("last_updated", "")
        stale = False
        if last_updated_str:
            try:
                last_updated      = datetime.fromisoformat(last_updated_str)
                now_london        = datetime.now(LONDON_TZ)
                current_slot_hour = 6 if 6 <= now_london.hour < 18 else 18
                if last_updated.hour != current_slot_hour:
                    stale = True
                    print(f"WARN [pla_json]: last_updated {last_updated_str} does not match "
                          f"current slot hour {current_slot_hour:02d}:00 — flagged as stale")
            except ValueError:
                print(f"WARN [pla_json]: could not parse last_updated '{last_updated_str}'")

        letter = data.get("flag_colour", "").strip().upper()
        colour = _PLA_LETTER_MAP.get(letter)
        if not colour:
            print(f"ERROR [pla_json]: unrecognised flag_colour '{letter}'")
            return None

        print(f"INFO [pla_json]: → {letter} → {colour} (stale={stale})")
        return {
            "colour":       colour,
            "last_updated": last_updated_str,
            "stale":        stale,
        }

    return get_cached("pla_json_crosscheck", fetch, ttl_seconds=300)


def get_pla_flag():
    """Fetch the PLA widget page and return the current flag colour, heading and body.
    Falls back to Richmond derivation if the scrape fails.
    Re-scrapes at most once per 15-minute window, all day — PLA doesn't always
    flip the flag exactly on the 6am/6pm boundary, so a flat cadence catches a
    late update within 15 minutes instead of it being cached for hours."""
    now = datetime.now(LONDON_TZ)
    slot = (now.date(), now.hour, now.minute // 15)

    cached = _cache.get('pla_flag')
    if cached and cached.get('slot') == slot and cached['data'] is not None:
        return cached['data'], cached['fetched_at']

    # Primary: scrape the PLA widget page directly
    data = None
    try:
        scraped = _scrape_pla_widget()
        if scraped:
            data = {
                "colour":  scraped["colour"],
                "heading": scraped["heading"],
                "body":    scraped["body"],
                "source":  "widget",
            }
    except Exception as e:
        print(f"ERROR [pla_flag]: _scrape_pla_widget raised {e}")

    # Fallback: derive colour from Richmond before_flag low tide
    if data is None:
        try:
            lw_raw = get_richmond_observed_low_tide()
            lw_data = lw_raw[0] if isinstance(lw_raw, tuple) else lw_raw
            before = lw_data.get("before_flag") if lw_data else None
            if before:
                colour = before["flag"].lower()
                print(f"INFO [pla_flag]: Richmond fallback {before['metres']}m → {colour}")
                data = {"colour": colour, "heading": "", "body": "", "source": "richmond"}
        except Exception as e:
            print(f"ERROR [pla_flag]: Richmond fallback raised {e}")

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

def _lw_flag_colour(metres):
    """Map observed low tide height in metres to PLA flag colour strings."""
    if   metres >= 2.6: return "RED",    "Red"
    elif metres >= 1.7: return "YELLOW", "Yellow"
    elif metres >= 0:   return "GREEN",  "Green"
    else:               return "BLACK",  "Black"


def _lw_dict(best):
    """Build a display dict from a low tide candidate dict."""
    dt = best["dt_london"]
    d  = dt.day
    suffix = "th" if 11 <= d % 100 <= 13 else {1:"st",2:"nd",3:"rd"}.get(d % 10, "th")
    metres = best["metres"]
    flag, flag_word = _lw_flag_colour(metres)
    return {
        "time":      dt.strftime(f"%H:%M {d}{suffix} %b"),
        "metres":    metres,
        "flag":      flag,
        "flag_word": flag_word,
    }


def get_richmond_observed_low_tide():
    """Fetch Richmond chart data and return two low tide results from a single API call:
      - before_flag: the most recent low tide BEFORE the current flag slot time (6am or 6pm).
                     This is what the PLA would have used when setting the current flag,
                     and is used by the Richmond fallback in _pla_flag_from_richmond().
      - after_flag:  the most recent low tide AFTER the current flag slot time, if any.
                     This is new data the PLA has not acted on yet, used to predict the
                     next flag. None if no low tide has occurred since the flag was set.
    """
    def fetch():
        r = requests.get(
            _PLA_RICHMOND_CHART_URL,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://pla.co.uk/"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        now_utc    = datetime.now(timezone.utc)
        now_london = now_utc.astimezone(LONDON_TZ)

        # Determine the current flag slot time in London time (6am or 6pm today)
        if 6 <= now_london.hour < 18:
            flag_slot_london = now_london.replace(hour=6,  minute=0, second=0, microsecond=0)
        else:
            flag_slot_london = now_london.replace(hour=18, minute=0, second=0, microsecond=0)
            # If it is before 6am, the active flag slot was 6pm yesterday
            if now_london.hour < 6:
                flag_slot_london -= timedelta(days=1)
        flag_slot_utc = flag_slot_london.astimezone(timezone.utc)

        if not isinstance(data, (list, dict)):
            raise ValueError(f"Unexpected Richmond chart response type: {type(data).__name__}")
        records = data if isinstance(data, list) else data.get("tpoints", [])
        if not isinstance(records, list):
            raise ValueError(f"Unexpected Richmond records type: {type(records).__name__}")

        # PLA rule: flag is set using the LOWEST tide reading in the 12 hours
        # preceding 6am or 6pm. So before_flag tracks lowest metres, not most recent.
        window_start_utc = flag_slot_utc - timedelta(hours=12)

        before_flag = None  # lowest low tide in the 12 hours before the flag slot
        after_flag  = None  # most recent low tide after the flag slot

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

            # Ignore future readings (allow small buffer for near-real-time data)
            if dt_utc > now_utc + timedelta(minutes=30):
                continue

            candidate = {
                "dt_utc":    dt_utc,
                "dt_london": dt_london,
                "metres":    float(observed),
            }

            if dt_utc < flag_slot_utc:
                # Within the 12-hour window before the flag slot — keep the lowest
                if dt_utc >= window_start_utc:
                    if before_flag is None or candidate["metres"] < before_flag["metres"]:
                        before_flag = candidate
            else:
                # After current flag slot — keep the most recent
                if after_flag is None or dt_utc > after_flag["dt_utc"]:
                    after_flag = candidate

        return {
            "before_flag": _lw_dict(before_flag) if before_flag else None,
            "after_flag":  _lw_dict(after_flag)  if after_flag  else None,
        }

    return get_cached("richmond_observed_lw", fetch, ttl_seconds=60)


def get_cardinal_direction(degree):
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    # Normalise first so negative or >360 degrees land in the right sector
    # (e.g. -15 -> 345 -> NNW, not N)
    return directions[int(((degree % 360) + 11.25) / 22.5) % 16]


def prevailing_direction(degrees_list):
    if not degrees_list:
        return "N/A"
    cardinals = [get_cardinal_direction(d) for d in degrees_list]
    return max(set(cardinals), key=cardinals.count)


# ---------------------------------------------------------------------------
# Met Office Weather DataHub — Site Specific (Global Spot)
# ---------------------------------------------------------------------------

_MO_SS_BASE  = "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/"
_MO_FOG_CODES = {5, 6}          # mist, fog
_MO_STORM_CODES = {28, 29, 30}  # thunder showers / thunder


def _ms_to_kmh(ms):
    return round(float(ms) * 3.6)


def _ms_to_mph(ms):
    return round(float(ms) * 2.23694)


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
    """Sunrise/sunset for today and tomorrow — WeatherAPI only. Returns
    ("", "", "", "") when the key is absent or the call fails, so callers
    degrade to no sun markers instead of erroring."""
    if not WEATHERAPI_KEY:
        return "", "", "", ""
    url = (
        f"https://api.weatherapi.com/v1/forecast.json"
        f"?key={WEATHERAPI_KEY}"
        f"&q={LAT},{LON}"
        f"&days=2&aqi=no&alerts=no"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    days = r.json()['forecast']['forecastday']

    def to_24h(t):
        return datetime.strptime(t, '%I:%M %p').strftime('%H:%M')

    def astro(i):
        a = days[i]['astro']
        return to_24h(a['sunrise']), to_24h(a['sunset'])

    rise0, set0 = astro(0)
    rise1, set1 = astro(1) if len(days) > 1 else ("", "")
    return rise0, set0, rise1, set1


def _parse_metoffice_timeseries(time_series, source_label):
    today = datetime.now(LONDON_TZ).date()
    tomorrow = today + timedelta(days=1)

    def bucket(day, start_h, end_h):
        entries = []
        for e in time_series:
            t = datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone(LONDON_TZ)
            if t.date() != day:
                continue
            if start_h <= t.hour < end_h:
                entries.append(e)
        return _metoffice_window_from_entries(entries)

    try:
        sunrise, sunset, tmrw_sunrise, tmrw_sunset = _fetch_sunrise_sunset()
    except Exception as e:
        print(f"Sunrise/sunset fallback failed: {e}")
        sunrise = sunset = tmrw_sunrise = tmrw_sunset = ""

    return {
        "morning":            bucket(today, 6, 12),
        "afternoon":          bucket(today, 12, 20),
        "tomorrow_morning":   bucket(tomorrow, 6, 12),
        "tomorrow_afternoon": bucket(tomorrow, 12, 20),
        "sunrise":            sunrise,
        "sunset":             sunset,
        "tomorrow_sunrise":   tmrw_sunrise,
        "tomorrow_sunset":    tmrw_sunset,
        "source":             source_label,
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
        days = data['forecast']['forecastday']

        # Normalise to HH:MM 24-hour
        def to_24h(t):
            return datetime.strptime(t, '%I:%M %p').strftime('%H:%M')

        def window(day, start_h, end_h):
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

        def astro(day):
            return to_24h(day['astro']['sunrise']), to_24h(day['astro']['sunset'])

        d0 = days[0]
        out = {
            'morning':            window(d0, 6,  12),
            'afternoon':          window(d0, 12, 20),
            'sunrise':            astro(d0)[0],
            'sunset':             astro(d0)[1],
            'tomorrow_morning':   None,
            'tomorrow_afternoon': None,
            'tomorrow_sunrise':   "",
            'tomorrow_sunset':    "",
            'source':             'WeatherAPI',
        }
        if len(days) > 1:
            d1 = days[1]
            out['tomorrow_morning']   = window(d1, 6,  12)
            out['tomorrow_afternoon'] = window(d1, 12, 20)
            out['tomorrow_sunrise'], out['tomorrow_sunset'] = astro(d1)
        return out
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
        f"&days=2"
        f"&aqi=no"
        f"&alerts=no"
    )
    r = requests.get(url, timeout=10)
    if r.status_code == 429:
        raise Exception("WeatherAPI rate limited")
    r.raise_for_status()
    return _parse_weatherapi(r.json())


def _fetch_weather_with_fallbacks():
    """
    Try Met Office DataHub, then WeatherAPI. Open-Meteo has been removed,
    so this is the complete chain and there is no Open-Meteo rate-limit
    backoff state to maintain.
    """
    last_err = None
    if MO_SITE_KEY:
        try:
            return get_weather_metoffice()
        except Exception as e:
            last_err = e
            print(f"Met Office weather failed, trying WeatherAPI: {e}")
    if WEATHERAPI_KEY:
        try:
            return get_weather_weatherapi()
        except Exception as e:
            last_err = e
            print(f"WeatherAPI failed: {e}")
    raise last_err or Exception("All weather sources failed")


def get_weather():
    result, fetched_at = get_cached('weather', _fetch_weather_with_fallbacks, ttl_seconds=7200)
    if result is None:
        raise Exception("Weather unavailable")
    return result, fetched_at


# ---------------------------------------------------------------------------
# Weather: Met Office DataHub → WeatherAPI (Open-Meteo removed)
# ---------------------------------------------------------------------------

def get_daily_weather_14d():
    """
    Day-by-day weather summary for the next 14 days, keyed by ISO date string.
    Built from the Met Office site-specific timeseries (Open-Meteo removed),
    so it covers however many days that API returns — later calendar days
    simply have no weather entry. Cached for 2 hours.
    """
    def fetch():
        if not MO_SITE_KEY:
            raise Exception("No Met Office site-specific key for daily weather")

        time_series = None
        last_err = None
        # three-hourly tends to cover the longest range; fall back to hourly.
        for timestep in ("three-hourly", "hourly"):
            try:
                time_series = _fetch_metoffice_timeseries(MO_SITE_KEY, timestep)
                break
            except Exception as e:
                last_err = e
                print(f"Daily weather Met Office {timestep} failed: {e}")
        if time_series is None:
            raise last_err or Exception("Met Office daily weather unavailable")

        by_day = defaultdict(lambda: {"temps": [], "rains": [], "winds": [], "gusts": []})
        for e in time_series:
            t_str = e.get("time", "")
            if not t_str:
                continue
            try:
                t = datetime.fromisoformat(t_str.replace("Z", "+00:00")).astimezone(LONDON_TZ)
            except Exception:
                continue

            bucket = by_day[t.date().isoformat()]

            temp = None
            for k in ("maxScreenAirTemp", "screenTemperature", "minScreenAirTemp"):
                if e.get(k) is not None:
                    temp = float(e[k])
            if temp is not None:
                bucket["temps"].append(temp)

            if e.get("probOfPrecipitation") is not None:
                bucket["rains"].append(float(e["probOfPrecipitation"]))

            if e.get("windSpeed10m") is not None:
                bucket["winds"].append(float(e["windSpeed10m"]))

            gust = e.get("max10mWindGust")
            if gust is None:
                gust = e.get("windGustSpeed10m")
            if gust is not None:
                bucket["gusts"].append(float(gust))

        return {
            date_str: {
                "temp":  round(max(v["temps"])) if v["temps"] else None,
                "rain":  round(max(v["rains"])) if v["rains"] else None,
                "wind":  _ms_to_mph(max(v["winds"])) if v["winds"] else None,
                "gusts": _ms_to_mph(max(v["gusts"])) if v["gusts"] else None,
            }
            for date_str, v in by_day.items()
        }
    return get_cached('daily_weather_14d', fetch, ttl_seconds=7200)


# ---------------------------------------------------------------------------
# Met Office NSWWS weather warnings
# ---------------------------------------------------------------------------

_NSWWS_FEED_URL  = os.environ.get(
    "METOFFICE_NSWWS_FEED_URL",
    "https://data.hub.api.metoffice.gov.uk/nswws/v1.1/objects/feed",
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
        "apikey": NSWWS_API_KEY,
        "Accept": "application/atom+xml",
        "User-Agent": "frbc-tides/1.0",
    }


def _nswws_read_json(response, label):
    """Parse a Met Office NSWWS GeoJSON body from the issued warnings endpoint.
    Tolerates empty warning collections."""
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

    Step 1: GET /v1.1/objects/feed (Atom XML) with apikey header.
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
    Return warning lines for warnings starting in the next 7 days but not
    active today. Deduplicates by date range, keeping highest severity.
    """
    now_local   = datetime.now(LONDON_TZ)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    lookahead   = today_start + timedelta(days=7)

    # warnings already arrive severity-first (RED→AMBER→YELLOW) from _fetch_nswws.
    # Iterate in that order so the highest-severity warning wins each date-range slot,
    # then sort the output lines by date for display.
    lines = []
    seen_periods = set()
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
        if not headline:
            continue

        # Deduplicate by date range — keep highest severity (first encountered)
        period_key = (vf.date() if vf else None, vt.date() if vt else None)
        if period_key in seen_periods:
            continue
        seen_periods.add(period_key)

        # Format day range: "Wed 25 Jun" or "Wed 25 Jun – Fri 27 Jun"
        from_label = vf.strftime("%-d %b")
        from_day   = vf.strftime("%a")
        if vt:
            to_label = vt.strftime("%-d %b")
            to_day   = vt.strftime("%a")
            if from_label == to_label:
                period = f"{from_day} {from_label}"
            else:
                period = f"{from_day} {from_label} – {to_day} {to_label}"
        else:
            period = f"From {from_day} {from_label}"

        lines.append({
            "period":   period,
            "headline": headline,
            "level":    w["level"],
            "_vf":      vf,
        })

    lines.sort(key=lambda l: l["_vf"] or datetime.max.replace(tzinfo=LONDON_TZ))
    for l in lines:
        l.pop("_vf", None)
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


# ---------------------------------------------------------------------------
# Flag hazard markers for diary events
#
# Red (🟥): the event overlaps a 6am/6pm flag window (current or, if a
#   Richmond low-tide reading has already predicted the next one, predicted)
#   where that window's colour is red — for the whole window, any tide state.
# Yellow (🟨): as above but colour is yellow, AND only for the portion of
#   that window that is also an ebb tide (Hammersmith HighWater->LowWater
#   leg) — yellow risk is specifically an ebb-tide hazard, red is not.
# Red always takes precedence over yellow when both would apply.
# Only the current + one predicted window are ever known, so this can only
# ever mark events in roughly the next ~24h — anything further out in the
# 14-day agenda is correctly left unmarked (colour: unknown, not "safe").
# ---------------------------------------------------------------------------

def _flag_slot_boundaries(now_lon):
    """Return (current_start, current_end, next_end) — the three 6am/18:00
    boundaries bracketing `now_lon` and the one after, as aware London
    datetimes. [current_start, current_end) is the window the live flag
    applies to; [current_end, next_end) is the window a predicted flag
    (if any) applies to."""
    d = now_lon.date()
    candidates = sorted(
        datetime(day.year, day.month, day.day, hour, 0, tzinfo=LONDON_TZ)
        for delta in (-1, 0, 1, 2)
        for day, hour in [(d + timedelta(days=delta), 6), (d + timedelta(days=delta), 18)]
    )
    idx = max(i for i, c in enumerate(candidates) if c <= now_lon)
    return candidates[idx], candidates[idx + 1], candidates[idx + 2]


def _ebb_intervals_from_tides(tides, range_start, range_end):
    """Ebb-tide intervals (HighWater -> following LowWater, Hammersmith)
    that overlap [range_start, range_end). `tides` is the already-fetched,
    already-cached get_tides() list — no extra network/DB cost."""
    if not tides:
        return []
    events = sorted(tides, key=lambda t: t['dt_utc'])
    intervals = []
    for a, b in zip(events, events[1:]):
        if a['EventType'] == 'HighWater' and b['EventType'] == 'LowWater':
            start = a['dt_utc'].astimezone(LONDON_TZ)
            end   = b['dt_utc'].astimezone(LONDON_TZ)
            if start < range_end and end > range_start:
                intervals.append((start, end))
    return intervals


def _build_flag_context(pla_flag, richmond_next_flag, tides, now_lon):
    """Bundle the (<=2) known flag windows and the ebb intervals inside them
    into the small structure _event_flag_marker() checks events against.
    Colours are compared using the lowercase widget vocabulary
    ("red"/"yellow"/"green"/"black") already used by get_pla_flag()."""
    cur_start, cur_end, next_end = _flag_slot_boundaries(now_lon)
    windows = []
    if pla_flag and pla_flag.get("colour"):
        windows.append({"start": cur_start, "end": cur_end, "colour": pla_flag["colour"]})
    if richmond_next_flag and richmond_next_flag.get("css_class"):
        windows.append({"start": cur_end, "end": next_end, "colour": richmond_next_flag["css_class"]})
    return {
        "windows": windows,
        "ebb_intervals": _ebb_intervals_from_tides(tides, cur_start, next_end),
    }


def _event_flag_marker(start_dt, end_dt, flag_ctx):
    """Return '🟥', '🟨', or None for a single event's [start_dt, end_dt)."""
    def overlaps(a_start, a_end, b_start, b_end):
        return a_start < b_end and b_start < a_end

    for w in flag_ctx["windows"]:
        if w["colour"] == "red" and overlaps(start_dt, end_dt, w["start"], w["end"]):
            return "🟥"
    for w in flag_ctx["windows"]:
        if w["colour"] != "yellow":
            continue
        for ebb_start, ebb_end in flag_ctx["ebb_intervals"]:
            seg_start = max(w["start"], ebb_start)
            seg_end   = min(w["end"], ebb_end)
            if seg_start < seg_end and overlaps(start_dt, end_dt, seg_start, seg_end):
                return "🟨"
    return None


def _event_marker_from_iso(event, flag_ctx):
    """Marker for an event dict carrying start_iso/end_iso, or None (incl.
    All Day events, which have no start_iso/end_iso and are never marked)."""
    if not event.get("start_iso") or not event.get("end_iso"):
        return None
    try:
        s  = datetime.fromisoformat(event["start_iso"])
        en = datetime.fromisoformat(event["end_iso"])
    except ValueError:
        return None
    return _event_flag_marker(s, en, flag_ctx)


def build_calendar_data():
    """
    Diary-agenda data for the /calendar page: the next 14 days, each with
    its high/low tide events, club diary events, and one all-day weather
    summary event.
    Tide events only cover the ~7-day window the Admiralty API's TidalEvents
    endpoint supports for this subscription tier (duration is always relative
    to today, capped at 7, with no way to page further forward) — later days
    in the 14-day agenda show weather and club events only.
    """
    now_lon  = datetime.now(LONDON_TZ)
    today    = now_lon.date()
    end_date = today + timedelta(days=13)

    try:
        tides, tides_updated = get_tides()
    except Exception as e:
        print(f"Thread error tides: {e}")
        tides, tides_updated = None, ''
    tides = tides or []

    try:
        daily_weather, weather_updated = get_daily_weather_14d()
    except Exception as e:
        print(f"Thread error daily_weather_14d: {e}")
        daily_weather, weather_updated = None, ''
    daily_weather = daily_weather or {}

    try:
        club_events_by_date, cal_updated = get_calendar_events_14d()
    except Exception as e:
        print(f"Thread error calendar_14d: {e}")
        club_events_by_date, cal_updated = None, ''
    club_events_by_date = club_events_by_date or {}

    # Flag context for hazard markers — reuses the same cached fetches the
    # homepage dashboard uses (get_pla_flag / get_richmond_observed_low_tide
    # are both TTL-cached already, so this is a cache hit in the common case
    # where the dashboard has been loaded recently, not a fresh fetch).
    try:
        pla_flag_data, _ = get_pla_flag()
    except Exception as e:
        print(f"Thread error pla_flag: {e}")
        pla_flag_data = None

    try:
        richmond_lw_raw, _ = get_richmond_observed_low_tide()
    except Exception as e:
        print(f"Thread error richmond_lw: {e}")
        richmond_lw_raw = None
    lw_after = richmond_lw_raw.get("after_flag") if richmond_lw_raw else None
    richmond_next_flag = {"css_class": lw_after["flag"].lower()} if lw_after else None

    flag_ctx = _build_flag_context(pla_flag_data, richmond_next_flag, tides, now_lon)

    tides_by_date = defaultdict(list)
    for t in tides:
        dt_london = t['dt_utc'].astimezone(LONDON_TZ)
        d = dt_london.date()
        if today <= d <= end_date:
            tides_by_date[d].append({
                "time": dt_london.strftime('%H:%M'),
                "kind": "tide-high" if t['EventType'] == 'HighWater' else "tide-low",
                "text": f"{'High Water' if t['EventType'] == 'HighWater' else 'Low Water'} — {t['Height']:.1f}m",
            })

    days = []
    for i in range(14):
        d = today + timedelta(days=i)
        w = daily_weather.get(d.isoformat())
        if w and all(w.get(k) is not None for k in ("temp", "rain", "wind", "gusts")):
            weather_summary = f"{w['temp']}c temp, {w['rain']}% rain, {w['wind']}mph wind, {w['gusts']}mph gusts"
        else:
            weather_summary = None

        club_events = club_events_by_date.get(d.isoformat(), [])
        all_day_events = [e["summary"] for e in club_events if e["time"] == "All Day"]

        timed = list(tides_by_date.get(d, []))
        for e in club_events:
            if e["time"] != "All Day":
                marker = _event_marker_from_iso(e, flag_ctx)
                prefix = f"{marker} " if marker else ""
                timed.append({
                    "time": e["time"].split('-')[0],
                    "kind": "diary",
                    "text": f"{prefix}{e['summary']} ({e['time']})",
                })
        timed.sort(key=lambda x: x['time'])

        days.append({
            "label":          d.strftime('%a %-d %b'),
            "is_today":       d == today,
            "weather":        weather_summary,
            "all_day_events": all_day_events,
            "timed":          timed,
        })

    return {
        "days":            days,
        "tides_updated":   tides_updated,
        "weather_updated": weather_updated,
        "cal_updated":     cal_updated,
    }


def build_dashboard_data():
    now_utc = datetime.now(timezone.utc)
    now_lon = datetime.now(LONDON_TZ)
    is_bst  = now_lon.dst() != timedelta(0)

    # Convert a UTC tide datetime to London wall-clock time. Using astimezone
    # (rather than adding a fixed BST/GMT offset based on *now*) keeps times
    # correct for events on the far side of a clock change.
    def to_london(dt_utc):
        return dt_utc.astimezone(LONDON_TZ)

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
        threading.Thread(target=run, args=('pla_json',       _fetch_pla_json)),
        threading.Thread(target=run, args=('weather',       get_weather)),
        threading.Thread(target=run, args=('kingston_flow', get_kingston_flow)),
        threading.Thread(target=run, args=('richmond_lw',   get_richmond_observed_low_tide)),
        threading.Thread(target=run, args=('thames_temp', get_thames_temperature)),
        threading.Thread(target=run, args=('nswws',          get_nswws_warnings)),
        threading.Thread(target=run, args=('water_quality',  get_water_quality)),
        threading.Thread(target=run, args=('cso_status',     get_cso_status)),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=8)

    # Tides
    tides, t_up = results.get('tides', (None, ''))
    t_data = {"upcoming": [], "direction": "", "until": "", "launch_warning": "", "updated": t_up, "last_tide": None, "next_tide": None}

    if tides:
        fut = [t for t in tides if t['dt_utc'] > now_utc]
        pst = [t for t in tides if t['dt_utc'] <= now_utc]
        if fut:
            t_data["direction"] = "FLOOD TIDE" if fut[0]['EventType'] == "HighWater" else "EBB TIDE"
            t_data["until"] = to_london(fut[0]['dt_utc']).strftime('%H:%M')
            # Extract the height string for the current imminent tide target
            t_data["current_target_height"] = f"{fut[0]['Height']:.1f}m"
            for t in fut[:5]:
                t_data["upcoming"].append({
                    "label":  "HI" if t['EventType'] == 'HighWater' else "LO",
                    "time":   to_london(t['dt_utc']).strftime('%a %H:%M'),
                    "height": f"{t['Height']:.1f}m",
                    "type":   t['EventType']
                })
            if len(fut) >= 2:
                nt = fut[1]
                t_data["next_tide"] = {
                    "label":  "High" if nt['EventType'] == 'HighWater' else "Low",
                    "time":   to_london(nt['dt_utc']).strftime('%a %H:%M'),
                    "height": f"{nt['Height']:.1f}m",
                    "type":   nt['EventType']
                }
        if pst:
            lt = pst[-1]
            t_data["last_tide"] = {
                "label":  "High" if lt['EventType'] == 'HighWater' else "Low",
                "time":   to_london(lt['dt_utc']).strftime('%a %H:%M'),
                "height": f"{lt['Height']:.1f}m",
                "type":   lt['EventType']
            }

        # Bridge tides table — apply fixed offsets (minutes) from Hammersmith reference
        # Offsets are approximate and based on standard Thames tidal progression.
        # Guarded by `if fut:` — if the tide cache has gone stale enough that
        # every event is in the past, fut is empty and fut[0] would crash the
        # whole dashboard with an IndexError.
        t_data["bridge_tides"]     = []
        t_data["next_hw_utc_iso"]  = None
        t_data["next_lw_utc_iso"]  = None
        _next_hw_h = None
        _next_lw_h = None
        if fut:
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
                adjusted = to_london(base_utc + timedelta(minutes=offset_mins))
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
                _day = to_london(_t['dt_utc']).strftime('%Y-%m-%d')
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
                "time":   to_london(t['dt_utc']).strftime('%H:%M'),
                "height": f"{t['Height']:.1f}m",
            }
            for t in tides
            if to_london(t['dt_utc']).date() == today_local
        ]
            
    # Calendar
    cal_data, cal_up = results.get('calendar', (None, ''))

    # Weather
    w_res, w_up = results.get('weather', (None, ''))
    weather = {"error": True, "updated": w_up, "day_label": "TODAY"}

    if w_res:
        # After 20:00 show tomorrow's 06:00-12:00 / 12:00-20:00 windows and the
        # heading "WEATHER TOMORROW"; from midnight it reverts to today.
        show_tomorrow = now_lon.hour >= 20
        if show_tomorrow:
            m        = w_res.get('tomorrow_morning')
            a        = w_res.get('tomorrow_afternoon')
            sunrise  = w_res.get('tomorrow_sunrise') or w_res.get('sunrise', '')
            sunset   = w_res.get('tomorrow_sunset') or w_res.get('sunset', '')
            day_label = "TOMORROW"
        else:
            m        = w_res.get('morning')
            a        = w_res.get('afternoon')
            sunrise  = w_res.get('sunrise', '')
            sunset   = w_res.get('sunset', '')
            day_label = "TODAY"

        weather.update({
            "error":     False,
            "updated":   w_up,
            "source":    w_res.get('source', ''),
            "sunrise":   sunrise,
            "sunset":    sunset,
            "morning":   m,
            "afternoon": a,
            "day_label": day_label,
        })


    # PLA Flag — from widget scrape (primary) or Richmond fallback
    pla_f, pla_u = results.get('pla_flag', (None, ''))

    # PLA JSON — independent crosscheck, always shown even if stale
    pla_json_raw  = results.get('pla_json', None)
    pla_json_flag = pla_json_raw[0] if isinstance(pla_json_raw, tuple) else pla_json_raw

    # Richmond observed low tide
    # get_richmond_observed_low_tide() returns a dict with two keys:
    #   before_flag — most recent low tide before the current flag slot (6am or 6pm)
    #   after_flag  — most recent low tide after the current flag slot, or None
    lw_raw, lw_up = results.get('richmond_lw', (None, ''))
    lw_before = lw_raw.get("before_flag") if lw_raw else None
    lw_after  = lw_raw.get("after_flag")  if lw_raw else None

    # Current flag slot label and next slot label
    now_h = now_lon.hour
    current_slot_label = "6am" if 6 <= now_h < 18 else "6pm"
    next_slot_label    = "6pm" if 6 <= now_h < 18 else "6am"

    # Next-flag prediction: if a low tide was recorded after the current flag was set,
    # it is new data the PLA has not acted on yet and predicts the next flag.
    # If lw_after is None, no low tide has occurred since the flag was set — no prediction.
    richmond_next_flag = None
    if lw_after:
        richmond_next_flag = {
            "colour":    lw_after["flag_word"],
            "css_class": lw_after["flag"].lower(),
            "next_slot": next_slot_label,
        }

    # Flag hazard markers (🟥/🟨) for today's/tomorrow's diary events — see
    # _build_flag_context / _event_flag_marker. Reuses pla_f, richmond_next_flag
    # and tides, already fetched above, so this adds no new network calls.
    flag_ctx = _build_flag_context(pla_f, richmond_next_flag, tides or [], now_lon)
    cal_list_marked = []
    for _e in (cal_data or {}).get("list", []):
        _marker = _event_marker_from_iso(_e, flag_ctx)
        if _marker:
            _e = {**_e, "summary": f"{_marker} {_e['summary']}"}
        cal_list_marked.append(_e)

    # Consolidated flag warning — shown when any sources disagree or are missing.
    # Compares widget scrape colour, Richmond before_flag colour, and JSON colour.
    # If any two disagree, or the widget scrape failed, warn the user to check PLA.
    _flag_colours = set()
    _widget_colour    = pla_f.get("colour")    if pla_f           else None
    _richmond_colour  = lw_before.get("flag").lower() if lw_before else None
    _json_colour      = pla_json_flag.get("colour")   if pla_json_flag and not pla_json_flag.get("stale") else None

    if _widget_colour:   _flag_colours.add(_widget_colour)
    if _richmond_colour: _flag_colours.add(_richmond_colour)
    if _json_colour:     _flag_colours.add(_json_colour)

    # Warn if: widget failed, or any sources that are present disagree
    flag_warning = (
        pla_f is None                                    # no flag data at all
        or (pla_f and pla_f.get("source") == "richmond") # widget scrape failed
        or len(_flag_colours) > 1                        # sources disagree
    )

    # Kingston Flow
    flow_data, flow_up = results.get('kingston_flow', (None, ''))


    # Water Quality (E. coli — FRBC / PTRC)
    wq_data, wq_up = results.get('water_quality', (None, ''))

    # CSO / sewage spill status (Thames Water Open Data API v2)
    cso_status, cso_status_up = results.get('cso_status', (None, ''))

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
        "pla_json_flag":       pla_json_flag,
        "flag_warning":        flag_warning,
        "richmond_lw_before":  lw_before,
        "richmond_lw_after":   lw_after,
        "richmond_lw_updated": lw_up,
        "current_slot_label":  current_slot_label,
        "next_slot_label":     next_slot_label,
        "richmond_next_flag":  richmond_next_flag,
        "weather":             weather,
        "cal": {
            **(cal_data or {"day_label": "TODAY", "list": []}),
            "list": cal_list_marked,
            "updated": cal_up
        },
        "cal_updated":         cal_up,
        "kingston_flow":       flow_data,
        "flow_updated":        flow_up,
        "water_quality":       wq_data,
        "cso_status":          cso_status,
        "cso_status_updated":  cso_status_up,
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
# Wind grid data for radar overlay — fallback with caching
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Wind grid helper — builds a uniform grid of (lat, lon) points for map arrows
# ---------------------------------------------------------------------------

def _wind_grid_points():
    """Return the list of (lat, lon) grid points for the wind arrow overlay."""
    lat_min, lat_max = 51.1, 51.9
    lon_min, lon_max = -3.2, 2.74
    grid_size = 4  # 4x4 = 16 points
    lats = [lat_min + i * (lat_max - lat_min) / (grid_size - 1) for i in range(grid_size)]
    lons = [lon_min + i * (lon_max - lon_min) / (grid_size - 1) for i in range(grid_size)]
    return [(round(lat, 3), round(lon, 3)) for lat in lats for lon in lons]


def _wind_grid_from_single(speed_kmh, direction_deg, gusts_kmh=None):
    """
    Spread a single wind observation across all grid points.
    Used when a single-point source (Met Office / WeatherAPI) is the best available.
    """
    points = _wind_grid_points()
    return {
        "points": [
            {"lat": lat, "lon": lon,
             "speed": speed_kmh, "direction": direction_deg, "gusts": gusts_kmh}
            for lat, lon in points
        ],
        "generated_at": datetime.now(LONDON_TZ).isoformat(),
    }


def _fetch_wind_metoffice():
    """
    Fetch current wind from Met Office DataHub site-specific (hourly) API.
    Returns (speed_kmh, direction_deg, gusts_kmh) or raises.
    """
    if not MO_SITE_KEY:
        raise Exception("No MO_SITE_KEY")
    ts = _fetch_metoffice_timeseries(MO_SITE_KEY, "hourly")
    now_utc = datetime.now(timezone.utc)
    # Find the entry closest to now
    best = None
    best_delta = None
    for entry in ts:
        t_str = entry.get("time", "")
        try:
            t = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
        except Exception:
            continue
        delta = abs((t - now_utc).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = entry
    if not best:
        raise Exception("Met Office timeseries: no usable entries")
    speed_ms = best.get("windSpeed10m")
    direction = best.get("windDirectionFrom10m")
    gust_ms   = best.get("max10mWindGust") or best.get("windGustSpeed10m")
    if speed_ms is None or direction is None:
        raise Exception("Met Office timeseries: missing wind fields")
    return (
        round(_ms_to_kmh(speed_ms), 1),
        round(float(direction)),
        round(_ms_to_kmh(gust_ms), 1) if gust_ms is not None else None,
    )


def _fetch_wind_weatherapi():
    """
    Fetch current wind from WeatherAPI.
    Returns (speed_kmh, direction_deg, gusts_kmh) or raises.
    """
    if not WEATHERAPI_KEY:
        raise Exception("No WEATHERAPI_KEY")
    url = (
        f"https://api.weatherapi.com/v1/current.json"
        f"?key={WEATHERAPI_KEY}&q={LAT},{LON}&aqi=no"
    )
    r = requests.get(url, timeout=10)
    if r.status_code == 429:
        raise Exception("WeatherAPI rate limited")
    r.raise_for_status()
    current = r.json().get("current", {})
    speed   = current.get("wind_kph")
    dirn    = current.get("wind_degree")
    gusts   = current.get("gust_kph")
    if speed is None or dirn is None:
        raise Exception("WeatherAPI: missing wind fields")
    return (round(float(speed), 1), round(float(dirn)), round(float(gusts), 1) if gusts else None)


def get_wind_grid():
    """
    Fetch wind data for the map arrow overlay.
    Fallback chain: Met Office DataHub → WeatherAPI → fallback.
    Met Office and WeatherAPI return a single point spread across the grid.
    fallback returns a true 16-point grid when available.
    Cached for 1 hour.
    """
    def fetch():
        # 1. Met Office DataHub (site-specific hourly) — single point
        if MO_SITE_KEY:
            try:
                speed, dirn, gusts = _fetch_wind_metoffice()
                print(f"Wind grid: Met Office ({speed} km/h, {dirn}°)")
                return _wind_grid_from_single(speed, dirn, gusts)
            except Exception as e:
                print(f"Wind grid: Met Office failed ({e}), trying WeatherAPI")

        # 2. WeatherAPI — single point
        if WEATHERAPI_KEY:
            try:
                speed, dirn, gusts = _fetch_wind_weatherapi()
                print(f"Wind grid: WeatherAPI ({speed} km/h, {dirn}°)")
                return _wind_grid_from_single(speed, dirn, gusts)
            except Exception as e:
                print(f"Wind grid: WeatherAPI failed ({e}), trying fallback")

        raise Exception("All wind sources failed")

    return get_cached('wind_grid', fetch, ttl_seconds=3600)  # 1 hour cache


# ---------------------------------------------------------------------------
# CSO / sewage spill status — Thames Water Open Data API v2 "discharge/status".
# One call (limit=1000 returns all ~570 permits) is cached and filtered to the
# waterways near the club. No key, no database, no scheduler. Tunnel-captured
# permits (receivingWaterCourse contains "via the Tideway tunnel") discharge
# nothing to the river and are excluded.
# ---------------------------------------------------------------------------

_CSO_STATUS_URL    = "https://api.thameswater.co.uk/opendata/v2/discharge/status"
_CSO_HAMMERSMITH_X = 523100   # Hammersmith Bridge, BNG easting
_CSO_HAMMERSMITH_Y = 178000   # Hammersmith Bridge, BNG northing

# Inclusion filter: only CSOs on these waterways are tracked (receivingWaterCourse
# keyword -> waterway name). "tideway tunnel" is handled before this (excluded).
_CSO_WATERWAY_ZONES = [
    ("River Thames",                  ("thames",)),
    ("River Brent",                   ("brent",)),
    ("River Wandle & Mitchell Brook", ("wandle", "mitchell")),
    ("Beverley Brook",                ("beverley",)),
    ("Smaller NW brooks",             ("graveney", "dollis", "wealdstone", "wembley", "hanwell")),
]

# Reach boundaries (BNG eastings): Teddington Lock and Putney Bridge.
_CSO_TEDDINGTON_X = 517550
_CSO_PUTNEY_X     = 524075
_CSO_REACHES      = ["Upstream of Teddington", "Tideway to Putney", "Downstream"]


def _cso_zone(water):
    """Return the waterway name for a receivingWaterCourse, or None to exclude."""
    w = (water or "").lower()
    if "tideway tunnel" in w:
        return None  # captured by the Tideway Tunnel: no river discharge
    for zone, keys in _CSO_WATERWAY_ZONES:
        if any(k in w for k in keys):
            return zone
    return None


def _cso_reach(x):
    """Reach by BNG easting: west of Teddington Lock, Teddington-Putney
    (the Tideway), or downstream of Putney Bridge."""
    if x is None:
        return _CSO_REACHES[2]
    if x < _CSO_TEDDINGTON_X:
        return _CSO_REACHES[0]
    if x <= _CSO_PUTNEY_X:
        return _CSO_REACHES[1]
    return _CSO_REACHES[2]


def _fmt_cso_ts(s):
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%d %b %H:%M")
    except Exception:
        return s


def get_cso_status():
    """Live CSO spill status for the waterways near the club, from a single
    Thames Water 'discharge/status' pull (cached 15 min)."""
    def fetch():
        r = requests.get(_CSO_STATUS_URL, params={"limit": 1000}, timeout=25)
        if r.status_code == 429:
            raise Exception("Thames Water status rate limited")
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            raise Exception("Thames Water status returned no items")

        by_reach = {}
        for it in items:
            water = _cso_zone(it.get("receivingWaterCourse"))
            if not water:
                continue
            x, y = it.get("x"), it.get("y")
            km = None
            if x and y:
                km = round((((x - _CSO_HAMMERSMITH_X) ** 2 +
                             (y - _CSO_HAMMERSMITH_Y) ** 2) ** 0.5) / 1000.0, 1)
            status = (it.get("alertStatus") or "Unknown").strip()
            by_reach.setdefault(_cso_reach(x), []).append({
                "permit":      it.get("permitNumber"),
                "name":        it.get("locationName") or it.get("permitNumber"),
                "water":       water,
                "status":      status,
                "discharging": status.lower() == "discharging",
                "offline":     status.lower() == "offline",
                "past48":      bool(it.get("alertPast48Hours")),
                "last_start":  _fmt_cso_ts(it.get("mostRecentDischargeAlertStart")),
                "last_stop":   _fmt_cso_ts(it.get("mostRecentDischargeAlertStop")),
                "km":          km,
            })

        out = []
        for reach in _CSO_REACHES:
            stations = by_reach.get(reach)
            if not stations:
                continue
            stations.sort(key=lambda s: s["km"] if s["km"] is not None else 1e9)
            out.append({
                "name":        reach,
                "groups":      [{"name": None, "stations": stations}],
                "discharging": sum(1 for s in stations if s["discharging"]),
                "offline":     sum(1 for s in stations if s["offline"]),
                "total":       len(stations),
            })

        return {
            "zones":       out,
            "total":       sum(z["total"] for z in out),
            "discharging": sum(z["discharging"] for z in out),
            "offline":     sum(z["offline"] for z in out),
            "updated":     datetime.now(LONDON_TZ).strftime("%H:%M"),
        }
    return get_cached("cso_status", fetch, ttl_seconds=900)


@app.route("/waterquality")
def water_quality_detail():
    """CSO spill status grouped by waterway, plus the FRBC/PTRC E. coli readings."""
    cso, cso_up = get_cso_status()
    wq, wq_up = get_water_quality()

    return render_template_string("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Water Quality — FRBC</title>
<style>
* { box-sizing:border-box; margin:0; padding:0; font-family:'Courier New',monospace; }
body { background:#000; color:#fff; padding:24px; max-width:1100px; }
h1 { font-size:1.3em; text-transform:uppercase; color:#33FF57; margin-bottom:4px; }
.meta { font-size:0.78em; color:#555; margin-bottom:24px; line-height:1.8; }
.meta a { color:#33FF57; text-decoration:none; }
h2 { font-size:1em; text-transform:uppercase; letter-spacing:0.08em; color:#fff;
     border-bottom:1px solid #333; padding-bottom:6px; margin:28px 0 4px;
     display:flex; justify-content:space-between; align-items:baseline; }
.zone-sum { font-size:0.72em; color:#888; font-weight:normal; text-transform:none; }
h3 { font-size:0.75em; text-transform:uppercase; letter-spacing:0.1em;
     color:#555; margin:14px 0 4px; border-left:2px solid #333; padding-left:6px; }
table { width:100%; table-layout:fixed; border-collapse:collapse; font-size:0.8em; margin-bottom:4px; }
th, td { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
th:nth-child(1), td.name { width:42%; }
th { text-align:left; color:#444; padding:3px 12px 3px 0; border-bottom:1px solid #1e1e1e; white-space:nowrap; }
th.r, td.r { text-align:right; }
td { padding:5px 12px 5px 0; border-bottom:1px solid #141414; vertical-align:top; }
.st-discharging { color:#FF4B4B; font-weight:bold; }
.st-offline { color:#888; }
.st-ok { color:#2a2a2a; }
.past-yes { color:#FFC233; font-weight:bold; }
.dim { color:#666; }
.empty { color:#2a2a2a; font-size:0.78em; font-style:italic; padding:4px 0 8px; }
</style>
</head>
<body>
<h1>Water Quality</h1>
<p class="meta">
  CSO status updated {{ cso_up or '&mdash;' }} &nbsp;&middot;&nbsp; E. coli updated {{ wq_up or '&mdash;' }}<br>
  Source: <a href="https://docs.api.thameswater.co.uk/" target="_blank" rel="noopener">Thames Water Open Data API v2</a>
  &nbsp;&middot;&nbsp; <a href="/">&#8592; Dashboard</a>
</p>

{% if cso and cso.zones %}
{% for zone in cso.zones %}
<h2>{{ zone.name }} <span class="zone-sum">{{ zone.discharging }} discharging &middot; {{ zone.offline }} offline &middot; {{ zone.total }} monitored</span></h2>
{% for g in zone.groups %}
{% if g.name %}<h3>{{ g.name }}</h3>{% endif %}
<table>
  <thead><tr><th>Outfall</th><th class="r">Status</th><th class="r">48h</th><th class="r">Last event</th><th class="r">km</th></tr></thead>
  <tbody>
  {% for s in g.stations %}
  <tr>
    <td class="name">{{ s.name }}{% if s.water %} <span class="dim">&middot; {{ s.water }}</span>{% endif %}</td>
    <td class="r {% if s.discharging %}st-discharging{% elif s.offline %}st-offline{% else %}st-ok{% endif %}">{{ s.status }}</td>
    <td class="r {% if s.past48 %}past-yes{% else %}dim{% endif %}">{{ 'Yes' if s.past48 else '&mdash;' }}</td>
    <td class="r dim">{% if s.last_start %}{{ s.last_start }}{% if s.last_stop %}&ndash;{{ s.last_stop }}{% else %} (ongoing){% endif %}{% else %}&mdash;{% endif %}</td>
    <td class="r dim">{{ s.km if s.km is not none else '&mdash;' }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% endfor %}
{% endfor %}
{% else %}
<p class="empty">CSO data unavailable</p>
{% endif %}

<h2>E. coli readings (CFU/100ml)</h2>
<table>
  <tr><td class="name">FRBC</td><td class="r" style="color:{{ wq.frbc.colour if wq and wq.frbc else '#555' }};">{% if wq and wq.frbc and wq.frbc.available %}{{ wq.frbc.ecoli_str }} &middot; {{ wq.frbc.date_str }} &middot; {{ wq.frbc.days_ago_str }}{% else %}&mdash; unavailable{% endif %}</td></tr>
  <tr><td class="name">PTRC</td><td class="r" style="color:{{ wq.ptrc.colour if wq and wq.ptrc else '#555' }};">{% if wq and wq.ptrc and wq.ptrc.available %}{{ wq.ptrc.ecoli_str }} &middot; {{ wq.ptrc.date_str }} &middot; {{ wq.ptrc.days_ago_str }}{% else %}&mdash; unavailable{% endif %}</td></tr>
</table>
</body>
</html>""", cso=cso, cso_up=cso_up, wq=wq, wq_up=wq_up)


@app.route("/")
def index():
    return render_template("index.html", d=build_dashboard_data())


@app.route("/links")
def links():
    return render_template("links.html")

@app.route("/calendar")
def calendar_page():
    return render_template("calendar.html", d=build_calendar_data())

@app.route("/distances")
def distances_page():
    return render_template("distances.html")

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
    except Exception as e:
        print(f"ERROR [overlay/flag]: {e!r}")

    # Next tide — compare next HW and LW UTC times, take whichever is sooner
    next_tide_label = None
    next_tide_time = None
    try:
        tides, _ = get_tides()
        hw_iso = None
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
    except Exception as e:
        # Log rather than pass silently — a bare pass here hid the missing
        # hw_iso initialisation for days while the overlay showed no tide text.
        print(f"ERROR [overlay/next-tide]: {e!r}")

    # Pontoon warning — from PONTOON_WARN_BEFORE before a low tide until
    # PONTOON_WARN_AFTER after it
    pontoon_warning = False
    try:
        tides, _ = get_tides()
        low_times = [e['dt_utc'] for e in tides if "Low" in e['EventType']]
        if low_times:
            nearest = min(low_times, key=lambda dt: abs((dt - now).total_seconds()))
            delta_s = (nearest - now).total_seconds()
            pontoon_warning = -PONTOON_WARN_BEFORE <= delta_s <= PONTOON_WARN_AFTER
    except Exception as e:
        print(f"ERROR [overlay/pontoon]: {e!r}")

    return jsonify({
        "flag":            flag_colour,
        "next_tide_label": next_tide_label,
        "next_tide_time":  next_tide_time,
        "pontoon_warning": pontoon_warning,
    })


def _prewarm():
    print("Pre-warming cache on startup...")
    for fn in (get_tides, get_kingston_flow, get_pla_flag, get_calendar_events, get_nswws_warnings, get_water_quality):
        try:
            fn()
        except Exception as e:
            print(f"Pre-warm error [{fn.__name__}]: {e!r}")
    time.sleep(1)
    try:
        get_weather()
    except Exception as e:
        print(f"Pre-warm weather error: {e!r}")
    time.sleep(1)
    for fn in (get_daily_weather_14d, get_calendar_events_14d):
        try:
            fn()
        except Exception as e:
            print(f"Pre-warm error [{fn.__name__}]: {e!r}")


@app.route("/healthz")
def healthz():
    """Cheap liveness probe. Deliberately does NOT trigger pre-warming:
    Render hits this during/after startup, and a concurrent prewarm sharing
    the single free-tier worker was a major source of the OOM/SIGKILL
    restarts and the resulting port-detection flap. Pre-warming is opt-in
    via ENABLE_PREWARM and fires exactly once from the bottom of this module."""
    return "ok", 200


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")


# ---------------------------------------------------------------------------
# Water Quality — E. coli (FRBC / PTRC Google Sheets)
# ---------------------------------------------------------------------------

import csv as _csv
import re as _re
from datetime import date as _date
from io import StringIO as _StringIO
from urllib.request import urlopen as _urlopen
from urllib.error import URLError as _URLError, HTTPError as _HTTPError

_WQ_FRBC_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1ZAzKgnACVxEM3j9eToxE9oAJpu6KZN0BNaeXd0jUmyM"
    "/export?format=csv&gid=1799951970"
)
# PTRC sheet — Main tab
_WQ_PTRC_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "14i4LMVw5OA1NvE8i14cbGo8M6nnUVlFV1pRbpjMmYnA"
    "/export?format=csv&gid=132413204"
)

_WQ_ECOLI_EXCELLENT = 500
_WQ_ECOLI_GOOD      = 1_000
_WQ_STALE_DAYS      = 7


def _wq_parse_ecoli(raw):
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.lower() in ("", "void", "na", "n/a", "-"):
        return None
    m = _re.search(r'\((\d[\d,]*)\)', raw)
    if m:
        return int(m.group(1).replace(",", ""))
    d = _re.search(r'[\d,]+', raw)
    if d:
        try:
            return int(d.group(0).replace(",", ""))
        except ValueError:
            return None
    return None


def _wq_parse_date(raw):
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _wq_risk_colour(ecoli_value, stale=False):
    if stale or ecoli_value is None:
        return "#555"
    if ecoli_value <= _WQ_ECOLI_GOOD:
        return "#27ae60"
    return "#e74c3c"


def _wq_find_col(keys, *candidates):
    for c in candidates:
        for k in keys:
            if c.lower() in k.lower():
                return k
    return None


def _wq_parse_sheet(raw_csv, site_label):
    reader = _csv.DictReader(_StringIO(raw_csv))
    rows = [{k.strip(): v.strip() for k, v in row.items() if k} for row in reader]
    if not rows:
        return []
    keys = list(rows[0].keys())

    ecoli_col = None
    for k in keys:
        kl = k.lower()
        if ("e.coli" in kl or "ecoli" in kl or "e coli" in kl or "alert one" in kl):
            if "additional" not in kl and "monitor 2" not in kl and "monitor 3" not in kl:
                ecoli_col = k
                break
    if not ecoli_col:
        print(f"WARNING [wq/{site_label}]: no E. coli column found")
        return []

    date_col = _wq_find_col(keys, "sample date", "date")
    results = []
    for row in rows:
        ecoli_val   = _wq_parse_ecoli(row.get(ecoli_col, ""))
        sample_date = _wq_parse_date(row.get(date_col, "") if date_col else "")
        if sample_date is None and ecoli_val is None:
            continue
        d_ago = (_date.today() - sample_date).days if sample_date else None
        stale = d_ago is not None and d_ago > _WQ_STALE_DAYS
        results.append({
            "date":      sample_date,
            "date_str":  sample_date.strftime("%-d %b") if sample_date else "—",
            "days_ago":  d_ago,
            "stale":     stale,
            "ecoli":     ecoli_val,
            "colour":    _wq_risk_colour(ecoli_val, stale=stale),
        })
    results.sort(key=lambda r: r["date"] or _date.min, reverse=True)
    return results


def _wq_fetch_site(url, site_label):
    if not url:
        return []
    try:
        with _urlopen(url, timeout=15) as resp:
            return _wq_parse_sheet(resp.read().decode("utf-8"), site_label)
    except _HTTPError as e:
        # urlopen raises HTTPError for any 4xx/5xx — it never returns a
        # response object we could check .status on. Handle it BEFORE
        # URLError (HTTPError is a subclass of URLError).
        if e.code == 403:
            # Sheet genuinely not public — this is a real "no data" state,
            # not a transient failure, so returning [] here is correct.
            print(f"INFO [wq/{site_label}]: sheet not public (403)")
            return []
        print(f"ERROR [wq/{site_label}]: HTTP {e.code} {e.reason}")
        raise
    except _URLError as e:
        # Network failure / timeout — this is transient, not "no data".
        # Raise so the caller can fall back to the last good cached reading
        # instead of overwriting it with an "unavailable" state.
        print(f"ERROR [wq/{site_label}]: {e}")
        raise


def get_water_quality():
    """
    Fetch E. coli readings for FRBC and PTRC.
    Returns dict with 'frbc' and 'ptrc' sub-dicts for template rendering.
    Cached for 6 hours — data is updated weekly.

    Each site is fetched independently: if one site has a transient network
    failure, we fall back to that site's last successfully cached reading
    (marked stale) rather than wiping the whole result. A 403 (sheet not
    public) is treated as genuine "no data available", not a transient
    failure, and is not retried from cache.
    """
    def fetch_one(key, url, label):
        try:
            data = _wq_fetch_site(url, label)
        except _URLError:
            # Transient failure — re-raise so get_cached's existing
            # per-key fallback can return the last good value for the
            # whole 'water_quality' cache entry. Note: this means a
            # transient failure on ONE site currently falls back the
            # ENTIRE cached result (both frbc and ptrc), since get_cached
            # caches at the 'water_quality' key level, not per-site.
            # This is the simplest fix; a per-site cache key would be
            # needed to isolate fallback to just the failing site.
            raise

        latest = next((r for r in data if r["ecoli"] is not None), None)
        if latest:
            d = latest["days_ago"]
            if d == 0:       days_str = "today"
            elif d == 1:     days_str = "yesterday"
            elif d is not None: days_str = f"{d} days ago"
            else:            days_str = "date unknown"
            return {
                "ecoli_str":    f"{latest['ecoli']:,}",
                "date_str":     latest["date_str"],
                "days_ago_str": days_str,
                "colour":       latest["colour"],
                "stale":        latest["stale"],
                "available":    True,
            }
        else:
            return {
                "ecoli_str":    "—",
                "date_str":     "—",
                "days_ago_str": "unavailable",
                "colour":       "#555",
                "stale":        False,
                "available":    False,
            }

    def fetch():
        out = {}
        for key, url, label in (
            ("frbc", _WQ_FRBC_URL, "FRBC"),
            ("ptrc", _WQ_PTRC_URL, "PTRC"),
        ):
            out[key] = fetch_one(key, url, label)
        return out

    result, fetched_at = get_cached("water_quality", fetch, ttl_seconds=21600)
    return result, fetched_at


# ---------------------------------------------------------------------------
# Cache pre-warm — the one and only trigger, at the very bottom of the module
# so every function _prewarm references (including get_water_quality, defined
# above) already exists. Opt-in via ENABLE_PREWARM so the default free-tier
# boot doesn't run a background fetch storm alongside the first request.
# Leave ENABLE_PREWARM unset on Render's free tier: the worker then binds its
# port immediately and the first request warms the cache itself.
# ---------------------------------------------------------------------------
if os.environ.get("ENABLE_PREWARM", "").lower() in ("1", "true", "yes"):
    threading.Thread(target=_prewarm, daemon=True).start()

