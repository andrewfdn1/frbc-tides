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
st.set_page_config(layout="wide", page_title="Hammersmith Tide Monitor")

# --- Top Logo ---
try:
    st.image("FRBC logo White on black.png", width=250)
except:
    st.write("### FULHAM REACH BOAT CLUB")

# Custom CSS
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;700&display=swap');
    @import url('https://fonts.googleapis.com/css2?family=Roboto+Mono:wght@400;700&display=swap');

    html, body, [data-testid="stAppViewContainer"], .main, span, p, h1, h2, h3, div {
        font-family: 'Roboto', sans-serif !important;
    }
    
    .main { background-color: #000000; color: #ffffff; }
    
    .metric-label {
        color: #ffffff;
        font-size: 0.9rem;
        font-weight: 300;
        margin-bottom: -5px;
        text-transform: uppercase;
    }

    .metric-value {
        color: #33FF57;
        font-weight: 700;
        font-size: 2.2rem;
        line-height: 1.1;
        margin-bottom: 20px;
    }

    /* Monospaced Grid for Tides Alignment */
    .tide-grid {
        font-family: 'Roboto Mono', monospace !important;
        font-weight: 700;
        font-size: 1.8rem;
        line-height: 1.4;
        white-space: pre;
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

# --- Helper Functions ---
def get_cardinal_direction(degree):
    directions = [
        "North", "North North East", "North East", "East North East", 
        "East", "East South East", "South East", "South South East", 
        "South", "South South West", "South West", "West South West", 
        "West", "West North West", "North West", "North North West"
    ]
    idx = int((degree + 11.25) / 22.5) % 16
    return directions[idx]

def get_tides():
    TIDE_API_KEY = st.secrets["TIDE_API_KEY"]
    STATION_ID = "0115"
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
        # EA Real-time API for Kingston Flow
        url = "https://environment.data.gov.uk/flood-monitoring/id/stations/3400TH/measures"
        res = requests.get(url, timeout=5).json()
        # Find the flow measure among station measures
        flow_url = next(m['@id'] for m in res['items'] if 'flow' in m['label'].lower())
        latest = requests.get(f"{flow_url}/readings?_limit=1", timeout=5).json()
        return latest['items'][0]['value']
    except:
        return None

# --- UI Layout ---
col_tide, col_weather, col_cal = st.columns([1.2, 1, 1])

with col_tide:
    st.header("TIDES")
    try:
        tides, is_bst, now_utc = get_tides()
        future = [t for t in tides if t['dt_utc'] > now_utc]
        
        if future:
            t_type = "Flood" if future[0]['EventType'] == "HighWater" else "Ebb"
            offset = timedelta(hours=1) if is_bst else timedelta(0)
            display_time = (future[0]['dt_utc'] + offset).strftime('%H:%M')
            st.markdown(f"<div class='metric-value'>{t_type} until {display_time}</div>", unsafe_allow_html=True)

        for t in future[:5]:
            offset = timedelta(hours=1) if is_bst else timedelta(0)
            t_time = (t['dt_utc'] + offset).strftime('%H:%M')
            t_day = (t['dt_utc'] + offset).strftime('%a')
            label = "High" if t['EventType'] == "HighWater" else "Low"
            color = "#FFD700" if t['EventType'] == "HighWater" else "#00CED1"
            # Formatted Grid: Label (4) | Time (5) | Day (3) | Height (4)
            st.markdown(f"<div class='tide-grid' style='color:{color};'>{label:<5} {t_time:<6} {t_day:<4} {t['Height']:.1f}m</div>", unsafe_allow_html=True)
    except:
        st.error("Tide data unavailable")

    st.markdown("---")
    st.header("PLA EBB FLAG")
    # Verified public image link
    st.image("https://pla.co.uk/sites/default/files/ebb_tide_flag.png", width=180)
    
    flow = get_kingston_flow()
    if flow:
        st.markdown("<div class='metric-label'>Kingston Flow</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-value'>{flow:.2f} m³/s</div>", unsafe_allow_html=True)

with col_weather:
    st.header("HAMMERSMITH WEATHER")
    try:
        weather_url = (f"https://api.open-meteo.com/v1/forecast?latitude=51.4875&longitude=-0.2301"
                       f"&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation_probability"
                       f"&daily=sunrise,sunset&timezone=Europe/London&forecast_days=1")
        res = requests.get(weather_url, timeout=5).json()
        curr, daily = res['current'], res['daily']

        st.markdown("<div class='metric-label'>Temperature</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-value'>{curr['temperature_2m']}°C</div>", unsafe_allow_html=True)

        st.markdown("<div class='metric-label'>Rain Chance</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-value'>{curr['precipitation_probability']}%</div>", unsafe_allow_html=True)
        
        st.markdown("<div class='metric-label'>Wind Speed</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-value'>{curr['wind_speed_10m']} km/h</div>", unsafe_allow_html=True)

        # Wind Direction Spelled Out
        full_dir = get_cardinal_direction(curr['wind_direction_10m'])
        st.markdown("<div class='metric-label'>Wind Direction</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-value'>{full_dir}</div>", unsafe_allow_html=True)

        sr = datetime.fromisoformat(daily['sunrise'][0]).strftime('%H:%M')
        ss = datetime.fromisoformat(daily['sunset'][0]).strftime('%H:%M')
        st.markdown("<div class='metric-label'>Sunrise</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-value'>{sr}</div>", unsafe_allow_html=True)
        st.markdown("<div class='metric-label'>Sunset</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-value'>{ss}</div>", unsafe_allow_html=True)

        fog = "⚠️ Fog" if curr['weather_code'] in [45, 48] else "None"
        storm = "⛈️ Storm" if curr['weather_code'] in [95, 96, 99] else "None"
        st.markdown("<div class='metric-label'>Fog Warning</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-value'>{fog}</div>", unsafe_allow_html=True)
        st.markdown("<div class='metric-label'>Storm Warning</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-value'>{storm}</div>", unsafe_allow_html=True)
    except:
        st.write("Weather update failed")

with col_cal:
    st.header("CLUB CALENDAR")
    cal_url = "https://calendar.google.com/calendar/embed?src=info%40fulhamreachboatclub.com&ctz=Europe%2FLondon&mode=AGENDA&showTitle=0&showNav=0&showDate=0&showPrint=0&showTabs=0&showCalendars=0&bgcolor=%23ffffff"
    # Added sandbox attribute to disable click-throughs while allowing scrolling
    st.components.v1.iframe(cal_url, height=600, scrolling=True)

st.divider()
st.caption(f"Last Update: {datetime.now(ZoneInfo('Europe/London')).strftime('%H:%M:%S')} BST")
