from flask import Flask, render_template, jsonify
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
import requests
import urllib3
import threading
from icalevents.icalevents import events as ical_events

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

TIDE_API_KEY = "26ba56f9ff62427aa82cb2df17180da9"
LONDON_TZ = ZoneInfo("Europe/London")
CAL_ID = "info@fulhamreachboatclub.com"

_cache = {}


def get_cached(key, fetch_fn, ttl_seconds):
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
        events_data = r.json()
        return sorted([
            {
                'dt_utc': datetime.fromisoformat(e['DateTime'].replace('Z', '')).replace(tzinfo=timezone.utc),
                'EventType': e['EventType'],
                'Height': e['Height']
            } for e in events_data
        ], key=lambda x: x['dt_utc'])
    return get_cached('tides', fetch, ttl_seconds=7200)


def get_calendar_events():
    def fetch():
        now = datetime.now(LONDON_TZ)
        display_date = now + timedelta(days=1) if now.hour >= 22 else now
        target_date = display_date.date()

        fetch_start = display_date.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        fetch_end   = display_date.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=1)

        url = f"https://calendar.google.com/calendar/ical/{CAL_ID.replace('@', '%40')}/public/basic.ics"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        ics_content = r.text

        evs = ical_events(string_content=ics_content, start=fetch_start, end=fetch_end)

        day_events = []
        for e in evs:
            try:
                if e.all_day:
                    from datetime import date as date_type
                    ev_date = e.start if isinstance(e.start, date_type) else e.start.date()
                else:
                    ev_date = e.start.astimezone(LONDON_TZ).date()
                if ev_date == target_date:
                    day_events.append(e)
            except Exception:
                pass

        day_events.sort(key=lambda x: x.start)

        return {
            "day_label": "TOMORROW" if now.hour >= 22 else "TODAY",
            "list": [{
                "summary": e.summary or "(no title)",
                "time": "All Day" if e.all_day else e.start.astimezone(LONDON_TZ).strftime('%H:%M')
            } for e in day_events]
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
    should_refresh = (now.hour in [6, 19] and now.minute in [1, 5])
    if should_refresh or 'pla_flag' not in _cache:
        data, t = get_cached('pla_flag', fetch, ttl_seconds=0)
        return data, t
    return _cache.get('pla_flag', {}).get('data'), _cache.get('pla_flag', {}).get('fetched_at', '')


def get_cardinal_direction(degree):
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[int((degree + 11.25) / 22.5) % 16]


def prevailing_direction(degrees_list):
    if not degrees_list:
        return "N/A"
    cardinals = [get_cardinal_direction(d) for d in degrees_list]
    return max(set(cardinals), key=cardinals.count)


def get_weather():
    def fetch():
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=51.488&longitude=-0.224"
            "&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,"
            "wind_gusts_10m,weather_code,precipitation_probability,uv_index"
            "&daily=sunrise,sunset"
            "&timezone=Europe%2FLondon"
            "&forecast_days=1"
        )
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        d = res.json()
        hourly = d['hourly']
        daily  = d['daily']
        times  = hourly['time']

        def window(start_h, end_h):
            indices = [i for i, t in enumerate(times)
                       if start_h <= int(t[11:13]) < end_h]
            if not indices:
                return None

            def vals(key):
                return [hourly[key][i] for i in indices if hourly[key][i] is not None]

            temps = vals('temperature_2m')
            winds = vals('wind_speed_10m')
            gusts = vals('wind_gusts_10m')
            dirs  = vals('wind_direction_10m')
            rain  = vals('precipitation_probability')
            uv    = vals('uv_index')
            codes = vals('weather_code')

            return {
                'temp_min':  round(min(temps)) if temps else None,
                'temp_max':  round(max(temps)) if temps else None,
                'wind_min':  round(min(winds)) if winds else None,
                'wind_max':  round(max(winds)) if winds else None,
                'gust_min':  round(min(gusts)) if gusts else None,
                'gust_max':  round(max(gusts)) if gusts else None,
                'direction': prevailing_direction(dirs),
                'rain_min':  round(min(rain))  if rain  else None,
                'rain_max':  round(max(rain))  if rain  else None,
                'uv_min':    round(min(uv), 1) if uv    else None,
                'uv_max':    round(max(uv), 1) if uv    else None,
                'fog':       any(c in [45, 48] for c in codes),
                'storm':     any(c >= 95 for c in codes),
            }

        return {
            'morning':   window(6,  12),
            'afternoon': window(12, 21),
            'sunrise':   daily['sunrise'][0][-5:],
            'sunset':    daily['sunset'][0][-5:],
        }

    return get_cached('weather', fetch, ttl_seconds=3600)


def get_kingston_flow():
    def fetch():
        url = ("https://environment.data.gov.uk/flood-monitoring/id/measures/"
               "3400TH-flow-water-i-15_min-m3_s/readings?_sorted&_limit=1")
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        items = data.get('items', [])
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
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

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

    cal_data, cal_up = results.get('calendar', (None, ''))

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
            return spd > 15 and (
                (dirn == "EBB TIDE"   and wd in ["S", "SE", "SW"]) or
                (dirn == "FLOOD TIDE" and wd in ["N", "NE", "NW"])
            )

        weather.update({
            "error":     False,
            "updated":   w_up,
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

    pla_f, pla_u = results.get('pla_flag', (None, ''))
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


if __name__ == "__main__":
    app.run(debug=True)
