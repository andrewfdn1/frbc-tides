import streamlit as st
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image
import urllib3
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

# Disable warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Refresh every 10 minutes
st_autorefresh(interval=600000, key="datarefresh")

# --- Page Config ---
st.set_page_config(layout="wide", page_title="Hammersmith Tide Monitor", initial_sidebar_state="collapsed")

# --- Custom CSS ---
st.markdown("""
    <style>
    /* Completely remove Streamlit header and white bars */
    [data-testid="stHeader"] { visibility: hidden; height: 0px; padding: 0; }
    .block-container { padding-top: 0rem; padding-bottom: 0rem; max-width: 95%; }
    
    @import url('https://fonts.googleapis.com/css2?family=Roboto+Mono:wght@400;700&display=swap');

    html, body, [data-testid="stAppViewContainer"], .main {
        background-color: #000000 !important;
        color: #ffffff;
    }
    
    .metric-label { color: #aaaaaa; font-size: 0.9rem; text-transform: uppercase; margin-bottom: -5px; }
    .metric-value { color: #33FF57; font-weight: 700; font-size: 2.2rem; line-height: 1.1; margin-bottom: 20px; }
    .tide-grid { font-family: 'Roboto Mono', monospace !important; font-size: 1.8rem; line-height: 1.3; white-space: pre; }
    
    /* Calendar Shield: Blocks clicks while allowing visibility */
    .calendar-container { position: relative; width: 100%; height: 650px; border: 1px solid #333; margin-top: 10px; }
    .calendar-shield { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 10; background: transparent; }
    iframe { width: 100%; height: 100%; filter: invert(90%) hue-rotate(180deg) contrast(110%); border: none; }
    
    hr { margin: 1em 0; border-color: #333; }
    h3 { margin-bottom: 0.5rem !important; }
    </style>
    """, unsafe_allow_html=True)

# --- Helper Functions ---
def get_cardinal_direction(degree):
    directions = ["North", "North North East", "North East", "East North East", "East", "East South East", "South East", "South South East", "South", "South South West", "South West", "West South West", "West", "West North West", "North West", "North North West"]
    return directions[int((degree + 11.25) / 22.5) % 16]

def get_tides():
    TIDE_API_KEY = st.secrets["TIDE_API_KEY"]
    r = requests.get("https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/0115/TidalEvents", 
                     headers={"Ocp-Apim-Subscription-Key": TIDE_API_KEY}, timeout=10)
    events = r.json()
    processed = sorted([{'dt_utc': datetime.fromisoformat(e['DateTime'].replace('Z', '')).replace(tzinfo=timezone.utc), 
                         'EventType': e['EventType'], 'Height': e['Height']} for e in events], key=lambda x: x['dt_utc'])
    return processed, datetime.now(ZoneInfo("Europe/London")).dst() != timedelta(0), datetime.now(timezone.utc)

def get_kingston_flow():
    # Sourced from Environment Agency Real-Time Flood Monitoring
    # Station: 3400TH (Kingston), Measure: flow-water-i-15_min-m3_s
    try:
        url = "https://environment.data.gov.uk/flood-monitoring/id/measures/3400TH-flow-water-i-15_min-m3_s/readings?_limit=1"
        res = requests.get(url, timeout=5).json()
        return res['items'][0]['value']
    except: return None

def get_pla_flag():
    # Dynamic scraping logic for the Ebb Flag
    try:
        r_flag = requests.get("https://pla.co.uk/pla-api-integration/ebb-tide-widget-embed", timeout=5)
        soup = BeautifulSoup(r_flag.text, 'html.parser')
        img_tag = soup.find('img')
        if img_tag:
            src = img_tag['src']
            return src if src.startswith('http') else "https://pla.co.uk" + src
    except: return None

# --- UI Header ---
try:
    st.image("FRBC logo White on black.png", width=250)
except:
    st.write("## FULHAM REACH BOAT CLUB")

# --- Main Columns ---
col_tide, col_weather, col_cal = st.columns([1.3, 1, 1.3])

with col_tide:
    st.markdown("### TIDES")
    try:
        tides, is_bst, now_utc = get_tides()
        future = [t for t in tides if t['dt_utc'] > now_utc]
        off = (timedelta(hours=1) if is_bst else timedelta(0))
        
        if future:
            t_type = "Flood" if future[0]['EventType'] == "HighWater" else "Ebb"
            st.markdown(f"<div class='metric-value'>{t_type} Tide Until {(future[0]['dt_utc']+off).strftime('%H:%M')}</div>", unsafe_allow_html=True)
        
        for t in future[:5]:
            color = '#FFD700' if t['EventType'] == 'HighWater' else '#00CED1'
            label = 'High' if t['EventType'] == 'HighWater' else 'Low'
            t_time = (t['dt_utc']+off).strftime('%H:%M')
            t_day = (t['dt_utc']+off).strftime('%a')
            st.markdown(f"<div class='tide-grid' style='color:{color};'>{label:<5} {t_time:<6} {t_day:<4} {t['Height']:.1f}m</div>", unsafe_allow_html=True)
    except: st.error("Tide data sync error")

    st.markdown("---")
    st.markdown("### PLA EBB FLAG")
    flag_url = get_pla_flag()
    if flag_url:
        st.image(flag_url, width=140)
    else:
        st.write("Flag currently unavailable")
    
    flow = get_kingston_flow()
    if flow:
        st.markdown(f"<div class='metric-label'>Kingston Flow</div><div class='metric-value'>{flow:.2f} m³/s</div>", unsafe_allow_html=True)

with col_weather:
    st.markdown("### WEATHER")
    try:
        res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude=51.4875&longitude=-0.2301&current=temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code,precipitation_probability&daily=sunrise,sunset&timezone=Europe/London&forecast_days=1", timeout=5).json()
        c, d = res['current'], res['daily']
        
        weather_data = [
            ("Temperature", f"{c['temperature_2m']}°C"),
            ("Rain Chance", f"{c['precipitation_probability']}%"),
            ("Wind Speed", f"{c['wind_speed_10m']} km/h"),
            ("Wind Direction", get_cardinal_direction(c['wind_direction_10m'])),
            ("Wind Gusts", f"{c['wind_gusts_10m']} km/h"),
            ("Sunrise", d['sunrise'][0][-5:]),
            ("Sunset", d['sunset'][0][-5:]),
            ("Fog Warning", "⚠️ Fog" if c['weather_code'] in [45, 48] else "None"),
            ("Storm Warning", "⛈️ Storm" if c['weather_code'] >= 95 else "None")
        ]
        
        for label, val in weather_data:
            st.markdown(f"<div class='metric-label'>{label}</div><div class='metric-value'>{val}</div>", unsafe_allow_html=True)
    except: st.error("Weather update failed")

with col_cal:
    st.markdown("### CLUB CALENDAR")
    cal_url = "https://calendar.google.com/calendar/embed?src=info%40fulhamreachboatclub.com&ctz=Europe%2FLondon&mode=AGENDA&showTitle=0&showNav=0&showDate=0&showPrint=0&showTabs=0&showCalendars=0&bgcolor=%23ffffff"
    # The shield div blocks mouse interactions with the iframe links
    st.markdown(f'''
        <div class="calendar-container">
            <div class="calendar-shield"></div>
            <iframe src="{cal_url}"></iframe>
        </div>
    ''', unsafe_allow_html=True)

st.divider()
st.caption(f"Last Update: {datetime.now(ZoneInfo('Europe/London')).strftime('%H:%M:%S')} BST")
