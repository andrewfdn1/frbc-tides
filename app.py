import streamlit as st
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO
import urllib3
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

# Disable warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 1. Refresh every 10 minutes (600,000 milliseconds)
st_autorefresh(interval=600000, key="datarefresh")

# --- Page Config ---
st.set_page_config(layout="wide", page_title="Hammersmith Tide Monitor")

# Custom CSS for the "Kiosk" black background look
st.markdown("""
    <style>
    .main { background-color: #000000; color: #ffffff; }
    div[data-testid="stMetricValue"] { color: #33FF57; }
    [data-testid="stHeader"] { background: rgba(0,0,0,0); }
    h1, h2, h3 { color: white !important; }
    /* Hide Streamlit menu for a cleaner kiosk look */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    </style>
    """, unsafe_allow_name_html=True)

# 2. Configuration from Streamlit Secrets (Set this up in Streamlit Cloud Dashboard)
TIDE_API_KEY = st.secrets["TIDE_API_KEY"]
STATION_ID = "0115"
KINGSTON_STATION_ID = "3400TH"
LAT, LON = 51.488, -0.224

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
            st.subheader(f"{t_type} tide until {display_time}")

        for t in future[:5]:
            offset = timedelta(hours=1) if is_bst else timedelta(0)
            time_str = (t['dt_utc'] + offset).strftime('%a %H:%M')
            color = "#FFD700" if t['EventType'] == "HighWater" else "#00CED1"
            st.markdown(f"<span style='color:{color}; font-family:monospace; font-size:22px; font-weight:bold;'>{time_str} {'HI' if t['EventType'] == 'HighWater' else 'LO'} {t['Height']:.1f}m</span>", unsafe_allow_html=True)
    except:
        st.error("Tide data unavailable")

with col_weather:
    st.header("WEATHER")
    try:
        res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m&daily=sunrise,sunset&timezone=Europe%2FLondon&forecast_days=1", timeout=5).json()
        curr = res['current']
        st.metric("Temperature", f"{curr['temperature_2m']}°C")
        st.metric("Wind Speed", f"{curr['wind_speed_10m']} km/h")
        st.metric("Wind Gusts", f"{curr['wind_gusts_10m']} km/h")
    except:
        st.write("Weather update failed")

with col_cal:
    st.header("TODAY")
    # 3. Calendar: Embed the Google Public URL for Fulham Reach Boat Club
    cal_url = "https://calendar.google.com/calendar/embed?src=info%40fulhamreachboatclub.com&ctz=Europe%2FLondon&mode=AGENDA&showTitle=0&showNav=0&showDate=0&showPrint=0&showTabs=0&showCalendars=0"
    st.components.v1.iframe(cal_url, height=500, scrolling=True)

# --- Footer ---
st.divider()
try:
    # 4. Ensure your logo filename matches exactly what is on GitHub
    st.image("FRBC logo White on black.png", width=250)
except:
    st.warning("Logo file not found on GitHub")

st.caption(f"Last Update: {datetime.now(ZoneInfo('Europe/London')).strftime('%H:%M:%S')} BST | Secure View")
