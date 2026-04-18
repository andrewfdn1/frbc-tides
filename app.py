import streamlit as st
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
import urllib3
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

# Disable warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Refresh every 10 minutes
st_autorefresh(interval=600000, key="datarefresh")

# --- Page Config ---
st.set_page_config(layout="wide", page_title="Hammersmith Tide Monitor", initial_sidebar_state="collapsed")

# --- Custom CSS: Absolute Courier & Black Theme Enforcement ---
st.markdown("""
    <style>
    /* Global Courier Enforcement */
    * {
        font-family: 'Courier New', Courier, monospace !important;
    }
    
    html, body, [data-testid="stAppViewContainer"], .main {
        background-color: #000000 !important;
        color: #ffffff !important;
    }

    [data-testid="stHeader"] { visibility: hidden; height: 0px; }
    .block-container { padding-top: 1rem; padding-bottom: 0rem; max-width: 95%; }
    
    .metric-value { color: #33FF57 !important; font-weight: bold; font-size: 2rem; line-height: 1.1; margin-bottom: 15px; }
    .tide-grid { font-size: 1.6rem; line-height: 1.3; white-space: pre; font-weight: bold; }
    
    /* Weather Table Alignment */
    .weather-table { width: 100%; border-collapse: collapse; font-size: 1.3rem; margin-bottom: 10px; }
    .weather-label { text-align: left; width: 50%; padding: 2px 0; color: #ffffff; }
    .weather-data { text-align: left; width: 50%; padding: 2px 0; font-weight: bold; }

    /* Calendar Container - White on Black via Filter */
    .calendar-container { position: relative; width: 100%; height: 600px; border: 1px solid #444; overflow: hidden; }
    .calendar-shield { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 10; background: transparent; }
    iframe { 
        width: 100%; 
        height: 100%; 
        border: none; 
        filter: invert(90%) hue-rotate(180deg) contrast(120%); 
    }
    
    hr { border-color: #444; }
    h3 { color: #ffffff !important; border-bottom: 1px solid #333; padding-bottom: 5px; text-transform: uppercase; }
    .warning-head { color: #FF4B4B; font-weight: bold; margin-top: 15px; margin-bottom: 5px; font-size: 1.4rem; }
    </style>
    """, unsafe_allow_html=True)

# --- Logic Functions ---
def get_cardinal_direction(degree):
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[int((degree + 11.25) / 22.5) % 16]

def get_tides():
    TIDE_API_KEY = "26ba56f9ff62427aa82cb2df17180da9"
    r = requests.get("https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/0115/TidalEvents", 
                     headers={"Ocp-Apim-Subscription-Key": TIDE_API_KEY}, timeout=10)
    events = r.json()
    processed = sorted([{'dt_utc': datetime.fromisoformat(e['DateTime'].replace('Z', '')).replace(tzinfo=timezone.utc), 
                         'EventType': e['EventType'], 'Height': e['Height']} for e in events], key=lambda x: x['dt_utc'])
    return processed, datetime.now(ZoneInfo("Europe/London")).dst() != timedelta(0), datetime.now(timezone.utc)

def get_pla_flag():
    try:
        r_flag = requests.get("https://pla.co.uk/pla-api-integration/ebb-tide-widget-embed", timeout=5)
        img_tag = BeautifulSoup(r_flag.text, 'html.parser').find('img')
        if img_tag:
            src = img_tag['src']
            return src if src.startswith('http') else "https://pla.co.uk" + src
    except: return None

def get_kingston_flow():
    try:
        url = "https://environment.data.gov.uk/flood-monitoring/id/measures/3400TH-flow-water-i-15_min-m3_s/readings?_limit=1"
        res = requests.get(url, timeout=5).json()
        return res['items'][0]['value']
    except: return None

# --- UI Layout ---
try: st.image("FRBC logo White on black.png", width=250)
except: st.write("### FULHAM REACH BOAT CLUB")

col_left, col_mid, col_right = st.columns([1.3, 1.1, 1.3])

with col_left:
    st.markdown("### Tides")
    try:
        tides, is_bst, now_utc = get_tides()
        future = [t for t in tides if t['dt_utc'] > now_utc]
        off = (timedelta(hours=1) if is_bst else timedelta(0))
        
        current_direction_str = ""
        if future:
            current_direction_str = "Flood tide" if future[0]['EventType'] == "HighWater" else "Ebb tide"
            st.markdown(f"<div class='metric-value'>{current_direction_str.upper()} UNTIL {(future[0]['dt_utc']+off).strftime('%H:%M')}</div>", unsafe_allow_html=True)
        
        for t in future[:5]:
            color = '#FFD700' if t['EventType'] == 'HighWater' else '#00CED1'
            label = 'HI' if t['EventType'] == 'HighWater' else 'LO'
            st.markdown(f"<div class='tide-grid' style='color:{color};'>{label:<3} {(t['dt_utc']+off).strftime('%a %H:%M'):<12} {t['Height']:.1f}m</div>", unsafe_allow_html=True)
    except: st.write("Tide Sync Error")

    st.markdown("---")
    st.markdown("### PLA Ebb Flag")
    flag_url = get_pla_flag()
    if flag_url: st.image(flag_url, width=140)
    
    flow = get_kingston_flow()
    if flow:
        st.markdown(f"<div class='metric-value' style='font-size:1.5rem;'>KINGSTON FLOW: {flow:.1f} m³/s</div>", unsafe_allow_html=True)

with col_mid:
    st.markdown("### Weather")
    try:
        res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude=51.488&longitude=-0.224&current=temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code&daily=sunrise,sunset,precipitation_probability_max&timezone=Europe/London&forecast_days=1", timeout=5).json()
        curr, daily = res['current'], res['daily']
        
        w_speed = curr['wind_speed_10m']
        w_gusts = curr['wind_gusts_10m']
        w_dir_str = get_cardinal_direction(curr['wind_direction_10m'])
        
        # Weather Core Data
        core_rows = [
            ("Temp:", f"{curr['temperature_2m']}°C"),
            ("Wind:", f"{w_speed} km/h"),
            ("Gusts:", f"{w_gusts} km/h"),
            ("Dir:", w_dir_str),
            ("Rain:", f"{daily['precipitation_probability_max'][0]}%"),
            ("Sunrise:", daily['sunrise'][0][-5:]),
            ("Sunset:", daily['sunset'][0][-5:])
        ]

        html_table = "<table class='weather-table'>"
        for label, val in core_rows:
            html_table += f"<tr><td class='weather-label'>{label}</td><td class='weather-data' style='color:white;'>{val}</td></tr>"
        html_table += "</table>"
        st.markdown(html_table, unsafe_allow_html=True)

        # Warnings Section
        st.markdown("<div class='warning-head'>WARNINGS</div>", unsafe_allow_html=True)
        
        wat_icon, wat_color = "None", "white"
        if (w_speed > 15 or w_gusts > 15) and ((current_direction_str == "Ebb tide" and w_dir_str in ["S", "SE", "SW"]) or (current_direction_str == "Flood tide" and w_dir_str in ["N", "NE", "NW"])):
            wat_icon, wat_color = "⚠️⚠️⚠️⚠️⚠️", "#FFFF00"
            
        fog_icon = "⚠️⚠️⚠️⚠️⚠️" if curr['weather_code'] in [45, 48] else "None"
        storm_icon = "⚠️⚠️⚠️⚠️⚠️" if curr['weather_code'] >= 95 else "None"

        warning_rows = [
            ("Fog:", fog_icon, "#FFFF00" if fog_icon != "None" else "white"),
            ("Storm:", storm_icon, "#FFFF00" if storm_icon != "None" else "white"),
            ("Wind v Tide:", wat_icon, wat_color)
        ]

        warn_table = "<table class='weather-table'>"
        for label, val, color in warning_rows:
            warn_table += f"<tr><td class='weather-label'>{label}</td><td class='weather-data' style='color:{color};'>{val}</td></tr>"
        warn_table += "</table>"
        st.markdown(warn_table, unsafe_allow_html=True)

    except: st.write("Weather Sync Error")

with col_right:
    st.markdown("### Calendar")
    cal_id = "info@fulhamreachboatclub.com"
    cal_url = f"https://calendar.google.com/calendar/embed?src={cal_id.replace('@', '%40')}&ctz=Europe%2FLondon&mode=AGENDA&showTitle=0&showNav=0&showDate=0&showPrint=0&showTabs=0&showCalendars=0"
    
    st.markdown(f'''
        <div class="calendar-container">
            <div class="calendar-shield"></div>
            <iframe src="{cal_url}"></iframe>
        </div>
    ''', unsafe_allow_html=True)

st.divider()
st.caption(f"SYSTEM STATUS: CONNECTED | {datetime.now(ZoneInfo('Europe/London')).strftime('%H:%M:%S')}")
