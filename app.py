import streamlit as st
import requests
from datetime import datetime, timezone, timedelta
import urllib3
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

# Disable warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 1. Refresh every 10 minutes
st_autorefresh(interval=600000, key="datarefresh")

# --- Page Config ---
st.set_page_config(layout="wide", page_title="Hammersmith Tide Monitor")

# Custom CSS
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;700&display=swap');

    html, body, [data-testid="stAppViewContainer"], .main, span, p, h1, h2, h3, div {
        font-family: 'Roboto', sans-serif !important;
    }
    
    .main { background-color: #000000; color: #ffffff; }
    
    /* Title Style (Small White) */
    .metric-title {
        color: #ffffff;
        font-size: 1rem;
        font-weight: 300;
        margin-bottom: -10px;
    }

    /* Data Style (Large Green) */
    .metric-data {
        color: #33FF57;
        font-weight: 700;
        font-size: 2.5rem;
        line-height: 1.2;
        margin-bottom: 15px;
    }

    /* Tide Table Styling */
    .tide-row {
        font-weight: 700;
        font-size: 2.2rem;
        line-height: 1.4;
        margin-bottom: 5px;
    }

    iframe {
        filter: invert(92%) hue-rotate(180deg) contrast(110%);
        border: 1px solid #333;
        border-radius: 8px;
    }

    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    [data-testid="stHeader"] { background: rgba(0,0,0,0); }
    </style>
    """, unsafe_allow_html=True)

# --- Data Functions ---
TIDE_API_KEY = st.secrets["TIDE_API_KEY"]
STATION_ID = "0115" # Hammersmith
LAT, LON = 51.4875, -0.2301

def get_tides():
    now_utc = datetime.now(timezone.utc)
    now_london = datetime.now(ZoneInfo("Europe/London"))
    is_bst = now_london.dst() != timedelta(0)
    
    r = requests.get(
        f"https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/{STATION_ID}/TidalEvents", 
        headers={"Ocp-Apim-Subscription-Key": TIDE_API_KEY}, timeout=10
    )
    events = r.json()
    processed = sorted([
        {'dt_utc': datetime.fromisoformat(e['DateTime'].replace('Z', '')).replace(tzinfo=timezone.utc), 
         'EventType': e['EventType'], 'Height': e['Height']} 
        for e in events
    ], key=lambda x: x['dt_utc'])
    
    return processed, is_bst, now_utc

def get_kingston_flow():
    try:
        # Using the Flood-Monitoring endpoint (Public Open Data)
        url = "https://environment.data.gov.uk/flood-monitoring/id/measures/3400TH-flow-m3s-instantaneous-15min-quals/readings?_limit=1"
        res = requests.get(url, timeout=5).json()
        return res['items'][0]['value']
    except:
        return None

# --- UI Layout ---
col_tide, col_weather, col_cal = st.columns([1, 1, 1])

with col_tide:
    st.header("TIDES")
    try:
        tides, is_bst, now_utc = get_tides()
        future = [t for t in tides if t['dt_utc'] > now_utc]
        
        if future:
            t_type = "Flood" if future[0]['EventType'] == "HighWater" else "Ebb"
            offset = timedelta(hours=1) if is_bst else timedelta(0)
            display_time = (future[0]['dt_utc'] + offset).strftime('%H:%M')
            st.markdown(f"<div class='metric-data'>{t_type} until {display_time}</div>", unsafe_allow_html=True)

        for t in future[:5]:
            offset = timedelta(hours=1) if is_bst else timedelta(0)
            time_str = (t['dt_utc'] + offset).strftime('%H:%M %a')
            label = "High" if t['EventType'] == "HighWater" else "Low"
            color = "#FFD700" if t['EventType'] == "HighWater" else "#00CED1"
            st.markdown(f"<div class='tide-row' style='color:{color};'>{label} {time_str} {t['Height']:.1f}m</div>", unsafe_allow_html=True)
    except:
        st.error("Tide data unavailable")

    st.markdown("---")
    st.header("PLA Ebb Flag")
    # Updated URL for the flag image
    st.image("https://mobile.pla.co.uk/assets/img/ebb_tide_flag.png", width=180)
    
    flow = get_kingston_flow()
    if flow:
        st.markdown("<p class='metric-title'>Kingston Flow</p>", unsafe_allow_html=True)
        st.markdown(f"<p class='metric-data'>{flow:.2f} m³/s</p>", unsafe_allow_html=True)

with col_weather:
    st.header("WEATHER")
    try:
        weather_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
            f"&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation_probability"
            f"&daily=sunrise,sunset&timezone=Europe/London&forecast_days=1"
        )
        res = requests.get(weather_url, timeout=5).json()
        curr, daily = res['current'], res['daily']
        code = curr['weather_code']

        # Formatting Weather Data
        st.markdown("<p class='metric-title'>Temperature</p>", unsafe_allow_html=True)
        st.markdown(f"<p class='metric-data'>{curr['temperature_2m']}°C</p>", unsafe_allow_html=True)

        st.markdown("<p class='metric-title'>Rain Chance</p>", unsafe_allow_html=True)
        st.markdown(f"<p class='metric-data'>{curr['precipitation_probability']}%</p>", unsafe_allow_html=True)
        
        dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
        wind_dir = dirs[int((curr['wind_direction_10m'] + 22.5) / 45) % 8]
        st.markdown("<p class='metric-title'>Wind</p>", unsafe_allow_html=True)
        st.markdown(f"<p class='metric-data'>{curr['wind_speed_10m']} km/h {wind_dir}</p>", unsafe_allow_html=True)

        st.markdown("<p class='metric-title'>Sunrise & Sunset</p>", unsafe_allow_html=True)
        sunrise = datetime.fromisoformat(daily['sunrise'][0]).strftime('%H:%M')
        sunset = datetime.fromisoformat(daily['sunset'][0]).strftime('%H:%M')
        st.markdown(f"<p class='metric-data'>{sunrise} GMT/BST<br>{sunset} GMT/BST</p>", unsafe_allow_html=True)

        st.markdown("<p class='metric-title'>Warnings (Fog / Storm)</p>", unsafe_allow_html=True)
        fog = "⚠️ Fog" if code in [45, 48] else "None"
        storm = "⛈️ Storm" if code in [95, 96, 99] else "None"
        st.markdown(f"<p class='metric-data'>{fog} / {storm}</p>", unsafe_allow_html=True)
        
    except:
        st.write("Weather update failed")

with col_cal:
    st.header("TODAY")
    cal_url = "https://calendar.google.com/calendar/embed?src=info%40fulhamreachboatclub.com&ctz=Europe%2FLondon&mode=AGENDA&showTitle=0&showNav=0&showDate=0&showPrint=0&showTabs=0&showCalendars=0&bgcolor=%23ffffff"
    st.components.v1.iframe(cal_url, height=550, scrolling=True)

# --- Footer ---
st.divider()
try:
    st.image("FRBC logo White on black.png", width=200)
except:
    pass

st.caption(f"Last Update: {datetime.now(ZoneInfo('Europe/London')).strftime('%H:%M:%S')} | Hammersmith Bridge")
