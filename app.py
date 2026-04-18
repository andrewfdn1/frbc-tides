import streamlit as st
import requests
from datetime import datetime, timezone, timedelta
import urllib3
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

# Disable warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Refresh every 10 minutes
st_autorefresh(interval=600000, key="datarefresh")

# --- Page Config ---
st.set_page_config(layout="wide", page_title="Hammersmith Tide Monitor", initial_sidebar_state="collapsed")

# --- Custom CSS: Hides Header, White Bar, and Adds Formatting ---
st.markdown("""
    <style>
    /* Remove white bar / header area */
    [data-testid="stHeader"] { visibility: hidden; height: 0%; }
    .block-container { padding-top: 1rem; padding-bottom: 0rem; }
    
    @import url('https://fonts.googleapis.com/css2?family=Roboto+Mono:wght@400;700&display=swap');

    html, body, [data-testid="stAppViewContainer"], .main {
        background-color: #000000;
        color: #ffffff;
    }
    .metric-label { color: #aaaaaa; font-size: 0.85rem; text-transform: uppercase; margin-bottom: -5px; }
    .metric-value { color: #33FF57; font-weight: 700; font-size: 2rem; margin-bottom: 15px; }
    .tide-grid { font-family: 'Roboto Mono', monospace !important; font-size: 1.5rem; white-space: pre; }
    
    /* Shield for Calendar */
    .calendar-container { position: relative; width: 100%; height: 600px; border: 1px solid #333; }
    .calendar-shield { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 10; background: transparent; }
    iframe { width: 100%; height: 100%; filter: invert(90%) hue-rotate(180deg); }
    </style>
    """, unsafe_allow_html=True)

def get_cardinal_direction(degree):
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[int((degree + 11.25) / 22.5) % 16]

def get_tides():
    # Use your key here
    TIDE_API_KEY = "26ba56f9ff62427aa82cb2df17180da9" 
    r = requests.get("https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/0115/TidalEvents", 
                     headers={"Ocp-Apim-Subscription-Key": TIDE_API_KEY}, timeout=10)
    events = r.json()
    processed = sorted([{'dt_utc': datetime.fromisoformat(e['DateTime'].replace('Z', '')).replace(tzinfo=timezone.utc), 
                         'EventType': e['EventType'], 'Height': e['Height']} for e in events], key=lambda x: x['dt_utc'])
    return processed, datetime.now(ZoneInfo("Europe/London")).dst() != timedelta(0), datetime.now(timezone.utc)

def get_kingston_flow():
    try:
        url = "https://environment.data.gov.uk/flood-monitoring/id/measures/3400TH-flow-water-i-15_min-m3_s/readings?_limit=1"
        res = requests.get(url, timeout=5).json()
        return res['items'][0]['value']
    except: return "N/A"

# --- UI Header ---
col_logo, col_title = st.columns([1, 3])
with col_logo:
    try: st.image("FRBC logo White on black.png", width=180)
    except: st.write("### FRBC")

# --- Main Columns ---
col_tide, col_weather, col_cal = st.columns([1.2, 1, 1.2])

with col_tide:
    st.markdown("### TIDES (Hammersmith)")
    try:
        tides, is_bst, now_utc = get_tides()
        future = [t for t in tides if t['dt_utc'] > now_utc]
        if future:
            t_type = "Flood" if future[0]['EventType'] == "HighWater" else "Ebb"
            off = (timedelta(hours=1) if is_bst else timedelta(0))
            st.markdown(f"<div class='metric-value'>{t_type} Tide Until {(future[0]['dt_utc']+off).strftime('%H:%M')}</div>", unsafe_allow_html=True)
        
        for t in future[:5]:
            color = '#FFD700' if t['EventType'] == 'HighWater' else '#00CED1'
            label = 'HI' if t['EventType'] == 'HighWater' else 'LO'
            st.markdown(f"<div class='tide-grid' style='color:{color};'>{label:<3} {(t['dt_utc']+off).strftime('%a %H:%M'):<12} {t['Height']:.1f}m</div>", unsafe_allow_html=True)
    except: st.error("Tide data sync error")

    st.markdown("---")
    st.markdown("### PLA EBB FLAG")
    # Verified direct URL for the PLA flag image
    st.image("https://pla.co.uk/sites/default/files/ebb_tide_flag.png", width=140)
    st.markdown(f"<div class='metric-label'>Kingston Flow</div><div class='metric-value'>{get_kingston_flow()} m³/s</div>", unsafe_allow_html=True)

with col_weather:
    st.markdown("### WEATHER")
    try:
        res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude=51.488&longitude=-0.224&current=temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code,precipitation_probability&daily=sunrise,sunset&timezone=Europe/London&forecast_days=1", timeout=5).json()
        c = res['current']
        
        weather_items = [
            ("Temperature", f"{c['temperature_2m']}°C"),
            ("Wind Speed", f"{c['wind_speed_10m']} km/h"),
            ("Wind Gusts", f"{c['wind_gusts_10m']} km/h"),
            ("Direction", get_cardinal_direction(c['wind_direction_10m'])),
            ("Rain Chance", f"{c['precipitation_probability']}%"),
            ("Sunrise", res['daily']['sunrise'][0][-5:]),
            ("Sunset", res['daily']['sunset'][0][-5:])
        ]
        
        for label, val in weather_items:
            st.markdown(f"<div class='metric-label'>{label}</div><div class='metric-value' style='font-size:1.4rem; color:white;'>{val}</div>", unsafe_allow_html=True)
    except: st.error("Weather data sync error")

with col_cal:
    st.markdown("### TODAY'S SESSIONS")
    cal_url = "https://calendar.google.com/calendar/embed?src=info%40fulhamreachboatclub.com&ctz=Europe%2FLondon&mode=AGENDA&showTitle=0&showNav=0&showDate=0&showPrint=0&showTabs=0&showCalendars=0"
    st.markdown(f'<div class="calendar-container"><div class="calendar-shield"></div><iframe src="{cal_url}"></iframe></div>', unsafe_allow_html=True)
