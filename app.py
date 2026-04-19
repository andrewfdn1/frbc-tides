from flask import Flask, render_template, jsonify
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

TIDE_API_KEY = "26ba56f9ff62427aa82cb2df17180da9"
LONDON_TZ = ZoneInfo("Europe/London")


def get_cardinal_direction(degree):
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[int((degree + 11.25) / 22.5) % 16]


def get_tides():
    r = requests.get(
        "https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/0115/TidalEvents",
        headers={"Ocp-Apim-Subscription-Key": TIDE_API_KEY},
        timeout=10
    )
    events = r.json()
    processed = sorted([
        {
            'dt_utc': datetime.fromisoformat(e['DateTime'].replace('Z', '')).replace(tzinfo=timezone.utc),
            'EventType': e['EventType'],
            'Height': e['Height']
        } for e in events
    ], key=lambda x: x['dt_utc'])
    return processed


def get_pla_flag():
    try:
        r = requests.get("https://pla.co.uk/pla-api-integration/ebb-tide-widget-embed", timeout=5)
        img_tag = BeautifulSoup(r.text, 'html.parser').find('img')
        if img_tag:
            src = img_tag['src']
            return src if src.startswith('http') else "https://pla.co.uk" + src
    except:
        return None


def get_kingston_flow():
    try:
        url = "https://environment.data.gov.uk/flood-monitoring/id/measures/3400TH-flow-water-i-15_min-m3_s/readings?_sorted&_limit=1"
        res = requests.get(url, timeout=5).json()
        return res['items'][0]['value']
    except:
        return None


def get_weather():
    url = "https://api.open-meteo.com/v1/forecast?latitude=51.488&longitude=-0.224&current=temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code&daily=sunrise,sunset,precipitation_probability_max&timezone=Europe/London&forecast_days=1"
    res = requests.get(url, timeout=10).json()
    if "current" not in res:
        raise ValueError(f"Unexpected API response: {res}")
    return res['current'], res['daily']


def build_dashboard_data():
    now_utc = datetime.now(timezone.utc)
    now_london = datetime.now(LONDON_TZ)
    is_bst = now_london.dst() != timedelta(0)
    off = timedelta(hours=1) if is_bst else timedelta(0)

    # --- Tides ---
    tides_data = {"error": False, "direction": "", "until": "", "upcoming": []}
    current_direction_str = ""
    try:
        tides = get_tides()
        future = [t for t in tides if t['dt_utc'] > now_utc]
        past = [t for t in tides if t['dt_utc'] <= now_utc]

        if future:
            next_event = future[0]
            current_direction_str = "Flood tide" if next_event['EventType'] == "HighWater" else "Ebb tide"
            tides_data["direction"] = current_direction_str.upper()
            tides_data["until"] = (next_event['dt_utc'] + off).strftime('%H:%M')

        for t in future[:5]:
            tides_data["upcoming"].append({
                "label": "HI" if t['EventType'] == 'HighWater' else "LO",
                "type": t['EventType'],
                "time": (t['dt_utc'] + off).strftime('%a %H:%M'),
                "height": f"{t['Height']:.1f}m"
            })

        # Launches/boats warning
        launch_msg = ""
        try:
            last_low = [t for t in past if t['EventType'] == 'LowWater'][-1]
            if (now_utc - last_low['dt_utc']).total_seconds() <= 3600:
                launch_msg = "CHECK PONTOON, FLOODING TIDE"
        except:
            pass
        tides_data["launch_warning"] = launch_msg

    except Exception as e:
        tides_data["error"] = True

    # --- PLA Flag & Flow ---
    pla_flag = get_pla_flag()
    kingston_flow = get_kingston_flow()

    # --- Weather ---
    weather_data = {"error": False}
    try:
        curr, daily = get_weather()
        w_speed = curr['wind_speed_10m']
        w_gusts = curr['wind_gusts_10m']
        w_dir_str = get_cardinal_direction(curr['wind_direction_10m'])
        w_code = curr['weather_code']

        weather_data.update({
            "temp": f"{curr['temperature_2m']}°C",
            "wind": f"{w_speed} km/h",
            "gusts": f"{w_gusts} km/h",
            "direction": w_dir_str,
            "rain": f"{daily['precipitation_probability_max'][0]}%",
            "sunrise": daily['sunrise'][0][-5:],
            "sunset": daily['sunset'][0][-5:],
        })

        # Warnings
        wat_warn = False
        if (w_speed > 15 or w_gusts > 15) and (
            (current_direction_str == "Ebb tide" and w_dir_str in ["S", "SE", "SW"]) or
            (current_direction_str == "Flood tide" and w_dir_str in ["N", "NE", "NW"])
        ):
            wat_warn = True

        weather_data["warnings"] = {
            "fog": w_code in [45, 48],
            "storm": w_code >= 95,
            "wind_vs_tide": wat_warn,
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        weather_data["error"] = True
        weather_data["error_msg"] = str(e)

    return {
        "tides": tides_data,
        "pla_flag": pla_flag,
        "kingston_flow": f"{kingston_flow:.1f}" if kingston_flow else None,
        "weather": weather_data,
        "last_updated": now_london.strftime('%H:%M:%S'),
        "cal_id": "info@fulhamreachboatclub.com",
    }


@app.route("/")
def index():
    data = build_dashboard_data()
    return render_template("index.html", d=data)


@app.route("/data")
def data_endpoint():
    return jsonify(build_dashboard_data())


if __name__ == "__main__":
    app.run(debug=True)
