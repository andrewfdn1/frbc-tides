import streamlit as st
import requests
from datetime import datetime, timezone, timedelta
import urllib3
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

# Disable warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 1. Refresh every 10 minutes (600,000 milliseconds)
st_autorefresh(interval=600000, key="datarefresh")

# --- Page Config ---
st.set_page_config(layout="wide", page_title="Hammersmith Tide Monitor")

# Custom CSS for the "Google Dark" look
st.markdown("""
    <style>
    /* 1. Import Roboto from Google Fonts */
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;700&display=swap');

    /* 2. Apply Roboto to the entire app */
    html, body, [data-testid="stAppViewContainer"], .main, span, p, h1, h2, h3, div {
        font-family: 'Roboto', sans-serif !important;
    }
    
    .main { background-color: #000000; color: #ffffff; }
    
    /* 3. Style Metrics and Headers */
    div[data-testid="stMetricValue"] { 
        color: #33FF57; 
        font-weight: 700;
        font-size: 2.2rem !important;
    }
    
    h1, h2, h3 { 
        text-transform: uppercase; 
        letter-spacing: 1px;
        color: white !important; 
    }

    /* 4. The Calendar "Dark Mode" Filter */
    iframe {
        filter: invert(92%) hue-rotate(180deg) contrast(110%);
        border: 1px solid #333;
        border-radius: 8px;
    }

    /* Hide Streamlit clutter */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    [data-testid="stHeader"] { background: rgba(0,0,0,0); }
    </style>
    """, unsafe_allow_html=True)

# --- Configuration & Data Functions ---
TIDE_API_KEY = st.secrets["TIDE_API_KEY"]
STATION_ID = "0115"
# Hammersmith Bridge Coordinates
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
        # EA Hydrology API for Kingston (Station 3400TH)
        url = "https://environment.data.gov.uk/hydrology/id/stations/3400TH/measures/flow-m3s-instantaneous-15min-quals/readings?_limit=1"
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
            st.subheader(f"{t_type} tide until {display_time}")

        for t in future[:5]:
            offset = timedelta(hours=1) if is_bst else timedelta(0)
            time_str = (t['dt_utc'] + offset).strftime('%a %H:%M')
            color = "#FFD700" if t['EventType'] == "HighWater" else "#00CED1"
            st.markdown(f"<span style='color:{color}; font-weight:bold; font-size:18px;'>{time_str} {'HI' if t['EventType'] == 'HighWater' else 'LO'} {t['Height']:.1f}m</span>", unsafe_allow_html=True)
    except:
        st.error("Tide data unavailable")

    st.markdown("---")
    st.header("PLA Ebb Tide Flag")
    # Pulling the flag image from PLA
    st.image("https://www.pla.co.uk/sites/default/files/ebb_tide_flag.png", width=180)
    
    flow = get_kingston_flow()
    if flow:
        st.metric("Kingston Flow", f"{flow:.2f} m³/s")
    else:
        st.write("Flow data unavailable")

with col_weather:
    st.header("WEATHER")
    try:
        # Open-Meteo API for Hammersmith Bridge
        weather_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
            f"&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation_probability"
            f"&daily=sunrise,sunset&timezone=Europe/London&forecast_days=1"
        )
        res = requests.get(weather_url, timeout=5).json()
        curr = res['current']
        daily = res['daily']
        code = curr['weather_code']

        st.metric("Temperature", f"{curr['temperature_2m']}°C")
        st.metric("Rain Chance", f"{curr['precipitation_probability']}%")
        
        # Wind Logic
        dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
        wind_dir = dirs[int((curr['wind_direction_10m'] + 22.5) / 45) % 8]
        st.write(f"**Wind:** {curr['wind_speed_10m']} km/h {wind_dir}")
        st.write(f"**Gusts:** {curr['wind_gusts_10m']} km/h")

        # Warning Logic
        fog_warn = "⚠️ Fog Warning" if code in [45, 48] else "None"
        storm_warn = "⛈️ Storm Warning" if code in [95, 96, 99] else "None"
        st.write(f"**Fog:** {fog_warn}")
        st.write(f"**Storm:** {storm_warn}")

        # Sun Times
        sunrise = datetime.fromisoformat(daily['sunrise'][0]).strftime('%H:%M')
        sunset = datetime.fromisoformat(daily['sunset'][0]).strftime('%H:%M')
        st.write(f"**Sunrise:** {sunrise} | **Sunset:** {sunset}")
    except:
        st.write("Weather update failed")

with col_cal:
    st.header("TODAY")
    # Use white background for the iframe so the CSS filter can invert it to black
    cal_url = "https://calendar.google.com/calendar/embed?src=info%40fulhamreachboatclub.com&ctz=Europe%2FLondon&mode=AGENDA&showTitle=0&showNav=0&showDate=0&showPrint=0&showTabs=0&showCalendars=0&bgcolor=%23ffffff"
    st.components.v1.iframe(cal_url, height=520, scrolling=True)

# --- Footer ---
st.divider()
try:
    st.image("FRBC logo White on black.png", width=200)
except:
    pass

st.caption(f"Last Update: {datetime.now(ZoneInfo('Europe/London')).strftime('%H:%M:%S')} | Hammersmith Bridge Data")
