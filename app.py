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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

TIDE_API_KEY      = os.environ.get("TIDE_API_KEY", "")
GOOGLE_API_KEY    = os.environ.get("GOOGLE_CALENDAR_API_KEY", "")
WEATHERAPI_KEY    = os.environ.get("WEATHERAPI_KEY", "")
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


RADIO_STATIONS = {
    "Capital FM":      "https://media-ice.musicradio.com/CapitalMP3",
    "Capital Anthems": "https://media-ice.musicradio.com/CapitalAnthemsMP3",
    "Capital Dance":   "https://media-ice.musicradio.com/CapitalDanceMP3",
    "Capital XTRA":    "https://media-ice.musicradio.com/CapitalXTRALondonMP3",
    "Heart FM":        "https://media-ice.musicradio.com/HeartLondonMP3",
    "Heart 80s":       "https://media-ice.musicradio.com/Heart80sMP3",
    "Heart 90s":       "https://media-ice.musicradio.com/Heart90sMP3",
    "Heart Dance":     "https://media-ice.musicradio.com/HeartDanceMP3",
}


@app.route("/play/<station_name>")
def play_radio(station_name):
    os.system("pkill vlc")
    url = RADIO_STATIONS.get(station_name)
    if url:
        os.system(f"cvlc {url} &")
        return jsonify(status="playing", station=station_name)
    return jsonify(status="error"), 404


@app.route("/stop")
def stop_radio_service():
    os.system("pkill vlc")
    return jsonify(status="stopped")


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
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=15)

    # Tides
    tides, t_up = results.get('tides', (None, ''))
    t_data = {"upcoming": [], "direction": "", "until": "", "launch_warning": "", "updated": t_up}

    if tides:
        fut = [t for t in tides if t['dt_utc'] > now_utc]
        pst = [t for t in tides if t['dt_utc'] <= now_utc]
        if fut:
            t_data["direction"] = "FLOOD TIDE" if fut[0]['EventType'] == "HighWater" else "EBB TIDE"
            t_data["until"] = (fut[0]['dt_utc'] + off).strftime('%H:%M')
            for t in fut[:5]:
                t_data["upcoming"].append({
                    "label":  "HI" if t['EventType'] == 'HighWater' else "LO",
                    "time":   (t['dt_utc'] + off).strftime('%a %H:%M'),
                    "height": f"{t['Height']:.1f}m",
                    "type":   t['EventType']
                })
        try:
            last_lo = [t for t in pst if t['EventType'] == 'LowWater'][-1]
            if (now_utc - last_lo['dt_utc']).total_seconds() <= 3600:
                t_data["launch_warning"] = "CHECK PONTOON, FLOODING TIDE"
        except Exception:
            pass

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
            "warnings": {
                "fog_morning":     m['fog']   if m else False,
                "fog_afternoon":   a['fog']   if a else False,
                "storm_morning":   m['storm'] if m else False,
                "storm_afternoon": a['storm'] if a else False,
                "wvt_morning":     wvt(m, 6),
                "wvt_afternoon":   wvt(a, 12),
            }
        })

    # PLA Flag
    pla_f, pla_u = results.get('pla_flag', (None, ''))

    # Kingston Flow
    flow_data, flow_up = results.get('kingston_flow', (None, ''))

    return {
        "tides":         t_data,
        "pla_flag":      pla_f,
        "pla_updated":   pla_u,
        "weather":       weather,
        "cal":           cal_data or {"day_label": "TODAY", "list": []},
        "cal_updated":   cal_up,
        "kingston_flow": flow_data,
        "flow_updated":  flow_up,
        "last_updated":  now_lon.strftime('%H:%M:%S'),
        "tz_label":      "BST" if is_bst else "GMT"
    }


@app.route("/")
def index():
    return render_template("index.html", d=build_dashboard_data())


@app.route("/data")
def data_endpoint():
    return jsonify(build_dashboard_data())


@app.route("/music")
def music():
    return render_template("music.html", stations=RADIO_STATIONS)


@app.route("/ping")
def ping():
    return "ok", 200


def _prewarm():
    import time
    print("Pre-warming cache on startup...")
    for fn in (get_tides, get_kingston_flow, get_pla_flag, get_calendar_events):
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
