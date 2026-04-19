from flask import Flask, render_template, jsonify
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
import requests
import urllib3
from icalevents.icalevents import events

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
    
    data = fetch_fn()
    fetched_at = datetime.now(LONDON_TZ).strftime('%H:%M')
    _cache[key] = {'ts': now, 'data': data, 'fetched_at': fetched_at}
    return data, fetched_at

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
    try:
        return get_cached('tides', fetch, ttl_seconds=7200)
    except:
        if 'tides' in _cache: return _cache['tides']['data'], _cache['tides']['fetched_at']
        return [], ''

def get_calendar_events():
    def fetch():
        now = datetime.now(LONDON_TZ)
        # Switch to tomorrow's view after 10 PM
        display_date = now + timedelta(days=1) if now.hour >= 22 else now
        
        start = display_date.replace(hour=0, minute=0, second=0)
        end = display_date.replace(hour=23, minute=59, second=59)
        
        url = f"https://calendar.google.com/calendar/ical/{CAL_ID.replace('@', '%40')}/public/basic.ics"
        try:
            evs = events(url=url, start=start, end=end)
            evs.sort(key=lambda x: x.start)
            return {
                "day_label": "TOMORROW" if now.hour >= 22 else "TODAY",
                "list": [{
                    "summary": e.summary,
                    "time": e.start.astimezone(LONDON_TZ).strftime('%H:%M') if not e.all_day else "All Day"
                } for e in evs]
            }
        except:
            return {"day_label": "SCHEDULE", "list": []}
    return get_cached('calendar', fetch, ttl_seconds=1800)

def get_pla_flag():
    def fetch():
        r = requests.get("https://pla.co.uk/pla-api-integration/ebb-tide-widget-embed", timeout=5)
        img = BeautifulSoup(r.text, 'html.parser').find('img')
        return "https://pla.co.uk" + img['src'] if img and not img['src'].startswith('http') else img['src'] if img else None
    
    now = datetime.now(LONDON_TZ)
    # Only fetch at 06:01, 06:05, 19:01, 19:05
    if (now.hour in [6, 19] and now.minute in [1, 5]) or 'pla_flag' not in _cache:
        try:
            data, t = get_cached('pla_flag', fetch, ttl_seconds=0) # Force fetch
            return data, t
        except: pass
    return _cache.get('pla_flag', {}).get('data'), _cache.get('pla_flag', {}).get('fetched_at', '')

def get_weather():
    def fetch():
        url = "https://api.open-meteo.com/v1/forecast?latitude=51.488&longitude=-0.224&current=temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code&daily=sunrise,sunset,precipitation_probability_max&timezone=Europe/London&forecast_days=1"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        d = res.json()
        return d['current'], d['daily']
    try:
        return get_cached('weather', fetch, ttl_seconds=3600)
    except:
        if 'weather' in _cache: return _cache['weather']['data'], _cache['weather']['fetched_at']
        return None, ''

def get_cardinal_direction(degree):
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[int((degree + 11.25) / 22.5) % 16]

def build_dashboard_data():
    now_utc = datetime.now(timezone.utc)
    now_lon = datetime.now(LONDON_TZ)
    is_bst = now_lon.dst() != timedelta(0)
    off = timedelta(hours=1) if is_bst else timedelta(0)

    # Tides & Warning
    tides, t_up = get_tides()
    t_data = {"upcoming": [], "direction": "", "until": "", "launch_warning": "", "updated": t_up}
    if tides:
        fut = [t for t in tides if t['dt_utc'] > now_utc]
        pst = [t for t in tides if t['dt_utc'] <= now_utc]
        if fut:
            t_data["direction"] = "FLOOD TIDE" if fut[0]['EventType'] == "HighWater" else "EBB TIDE"
            t_data["until"] = (fut[0]['dt_utc'] + off).strftime('%H:%M')
            t_data["upcoming"] = [{"label": "HI" if t['EventType'] == 'HighWater' else "LO", "time": (t['dt_utc'] + off).strftime('%a %H:%M'), "height": f"{t['Height']:.1f}m", "type": t['EventType']} for t in fut[:5]]
        
        try:
            last_lo = [t for t in pst if t['EventType'] == 'LowWater'][-1]
            if (now_utc - last_lo['dt_utc']).total_seconds() <= 3600:
                t_data["launch_warning"] = "CHECK PONTOON, FLOODING TIDE"
        except: pass

    # Calendar
    cal_data, cal_up = get_calendar_events()

    # Weather
    w_res, w_up = get_weather()
    weather = {"error": True, "updated": w_up}
    if w_res:
        curr, daily = w_res
        w_dir = get_cardinal_direction(curr['wind_direction_10m'])
        weather.update({
            "error": False, "temp": f"{curr['temperature_2m']}°C", "wind": f"{curr['wind_speed_10m']} km/h",
            "gusts": f"{curr['wind_gusts_10m']} km/h", "direction": w_dir, "rain": f"{daily['precipitation_probability_max'][0]}%",
            "sunrise": daily['sunrise'][0][-5:], "sunset": daily['sunset'][0][-5:],
            "warnings": {"fog": curr['weather_code'] in [45, 48], "storm": curr['weather_code'] >= 95, 
                         "wind_vs_tide": (curr['wind_speed_10m'] > 15 and ((t_data['direction'] == "EBB TIDE" and w_dir in ["S", "SE", "SW"]) or (t_data['direction'] == "FLOOD TIDE" and w_dir in ["N", "NE", "NW"]))) }
        })

    pla_f, pla_u = get_pla_flag()

    return {
        "tides": t_data, "pla_flag": pla_f, "pla_updated": pla_u,
        "weather": weather, "cal": cal_data, "cal_updated": cal_up,
        "last_updated": now_lon.strftime('%H:%M:%S'), "tz_label": "BST" if is_bst else "GMT"
    }

@app.route("/")
def index(): return render_template("index.html", d=build_dashboard_data())

@app.route("/data")
def data_endpoint(): return jsonify(build_dashboard_data())

if __name__ == "__main__":
    app.run(debug=True)
