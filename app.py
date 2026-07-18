from flask import Flask, render_template
from datetime import datetime, timezone, timedelta, time as dtime
from zoneinfo import ZoneInfo
import requests
import os

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TIDE_API_KEY = os.environ.get("TIDE_API_KEY", "")
MO_SITE_KEY  = os.environ.get("METOFFICE_SITESPECIFIC", "")

LONDON_TZ = ZoneInfo("Europe/London")

# Hammersmith, London
LAT, LON = 51.488, -0.224

WIND_THRESHOLD_MPH = 10
RAIN_THRESHOLD_PCT = 10

# Amber/red wind bands, matching the FRBC tides dashboard (evaluated in km/h)
WIND_AMBER_KMH = 10
WIND_RED_KMH   = 20

# Air temperature bands, matching the FRBC tides dashboard's cold/heat logic
TEMP_RED_HIGH_C   = 30
TEMP_RED_LOW_C    = 0
TEMP_AMBER_HIGH_C = 25
TEMP_AMBER_LOW_C  = 3

_MO_SS_BASE = "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/"

# ---------------------------------------------------------------------------
# Tiny in-process cache — avoids hammering the APIs on every page load and
# falls back to the last good value if a fetch fails.
# ---------------------------------------------------------------------------

_cache = {}


def cached(key, fetch_fn, ttl_seconds):
    now = datetime.now(timezone.utc).timestamp()
    hit = _cache.get(key)
    if hit and now - hit["ts"] < ttl_seconds:
        return hit["data"]
    try:
        data = fetch_fn()
        _cache[key] = {"ts": now, "data": data}
        return data
    except Exception:
        if hit:
            return hit["data"]
        raise


# ---------------------------------------------------------------------------
# Tides — UKHO Admiralty API, station 0115 (Hammersmith-referenced)
# ---------------------------------------------------------------------------

def _fetch_tides():
    r = requests.get(
        "https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/0115/TidalEvents",
        headers={"Ocp-Apim-Subscription-Key": TIDE_API_KEY},
        timeout=10,
    )
    r.raise_for_status()
    return sorted(
        [
            {
                "dt_utc": datetime.fromisoformat(e["DateTime"].replace("Z", "")).replace(tzinfo=timezone.utc),
                "EventType": e["EventType"],
            }
            for e in r.json()
        ],
        key=lambda x: x["dt_utc"],
    )


def get_tides():
    return cached("tides", _fetch_tides, ttl_seconds=7200)


# ---------------------------------------------------------------------------
# Met Office Weather DataHub — Site Specific (Global Spot) forecast
# ---------------------------------------------------------------------------

def _fetch_metoffice_timeseries(timestep):
    """Fetch a GeoJSON timeSeries from the Met Office Site-Specific API."""
    if not MO_SITE_KEY:
        raise Exception("METOFFICE_SITESPECIFIC is not set")
    url = f"{_MO_SS_BASE}{timestep}"
    headers = {"apikey": MO_SITE_KEY, "accept": "application/json"}
    params = {
        "latitude": LAT,
        "longitude": LON,
        "excludeParameterMetadata": "true",
        "includeLocationName": "false",
    }
    r = requests.get(url, headers=headers, params=params, timeout=15)
    if r.status_code in (401, 403):
        raise Exception("Met Office authentication failed — check METOFFICE_SITESPECIFIC")
    if r.status_code == 429:
        raise Exception("Met Office rate limited")
    r.raise_for_status()
    features = r.json().get("features", [])
    if not features:
        return []
    return features[0].get("properties", {}).get("timeSeries", [])


def _ms_to_mph(ms):
    return float(ms) * 2.236936


def _extract_temp_c(e):
    """
    Met Office field names for temperature vary by timestep: most points
    carry an instantaneous 'screenTemperature', but some (particularly
    further out in the three-hourly series) only report min/max bounds
    instead. Average whichever is present rather than requiring the exact
    instantaneous field, so temps beyond the ~2-day hourly window aren't
    silently dropped.
    """
    direct = e.get("screenTemperature")
    if direct is not None:
        return float(direct)
    bounds = [
        float(e[k]) for k in ("maxScreenAirTemp", "minScreenAirTemp")
        if e.get(k) is not None
    ]
    return sum(bounds) / len(bounds) if bounds else None


def _fetch_met_office_forecast():
    """
    Merge the hourly (~2 days) and three-hourly (~7 days) Met Office
    timeseries into one time-sorted list, so nearby low tides can be
    matched against the best-resolution forecast point available.
    """
    points = {}
    for timestep in ("hourly", "three-hourly"):
        for e in _fetch_metoffice_timeseries(timestep):
            try:
                dt = datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone(LONDON_TZ)
            except Exception:
                continue

            wind = e.get("windSpeed10m")
            gust = e.get("max10mWindGust", e.get("windGustSpeed10m"))
            rain = e.get("probOfPrecipitation")
            if wind is None or gust is None or rain is None:
                continue

            # Temperature is a bonus field, not required — dropping the
            # whole row over a missing temp would hide otherwise-good
            # wind/rain data.
            temp = _extract_temp_c(e)

            key = dt.isoformat()
            if key in points:
                continue  # keep the first (hourly, fetched first) value
            points[key] = {
                "dt":       dt,
                "wind_mph": _ms_to_mph(wind),
                "gust_mph": _ms_to_mph(gust),
                "rain_pct": float(rain),
                "temp_c":   temp,
            }

    return sorted(points.values(), key=lambda p: p["dt"])


def get_met_office_forecast():
    return cached("met_office_forecast", _fetch_met_office_forecast, ttl_seconds=3600)


def _nearest_forecast_point(forecast, target_dt, max_delta=timedelta(hours=2)):
    """Closest forecast point to target_dt, or None if nothing is within range."""
    best, best_diff = None, None
    for point in forecast:
        diff = abs(point["dt"] - target_dt)
        if best_diff is None or diff < best_diff:
            best, best_diff = point, diff
    if best is not None and best_diff <= max_delta:
        return best
    return None


# ---------------------------------------------------------------------------
# Colour coding
# ---------------------------------------------------------------------------

def _wind_band(mph, kmh):
    """Green under the mph threshold (this app's own rule); above that,
    the same amber/red bands the FRBC tides dashboard uses for wind/gusts."""
    if mph < WIND_THRESHOLD_MPH:
        return "green"
    if kmh > WIND_RED_KMH:
        return "red"
    if kmh > WIND_AMBER_KMH:
        return "yellow"
    return ""


def _temp_band(temp_c):
    """Same hot/cold bands the FRBC tides dashboard uses for air temperature."""
    if temp_c is None:
        return ""
    if temp_c >= TEMP_RED_HIGH_C or temp_c < TEMP_RED_LOW_C:
        return "red"
    if temp_c >= TEMP_AMBER_HIGH_C or temp_c < TEMP_AMBER_LOW_C:
        return "yellow"
    return ""


# ---------------------------------------------------------------------------
# Forecast table
# ---------------------------------------------------------------------------

def build_forecast_rows():
    """Every future low tide between 6am and 7pm, matched against the Met
    Office wind/rain/temp forecast for the nearest available forecast
    point."""
    tides = get_tides()
    forecast = get_met_office_forecast()
    now = datetime.now(timezone.utc)

    rows = []
    for t in tides:
        if "Low" not in t["EventType"] or t["dt_utc"] < now:
            continue

        dt_london = t["dt_utc"].astimezone(LONDON_TZ)

        # Low tides before 6am or after 7pm aren't useful for planning an
        # outing, so drop them from the table entirely.
        if dt_london.time() < dtime(6, 0) or dt_london.time() > dtime(19, 0):
            continue

        wx = _nearest_forecast_point(forecast, dt_london)
        if wx is None:
            continue

        wind_mph = wx["wind_mph"]
        gust_mph = wx["gust_mph"]
        rain_pct = wx["rain_pct"]
        temp_c   = wx["temp_c"]

        wind_kmh = round(wind_mph * 1.609344)
        gust_kmh = round(gust_mph * 1.609344)
        temp_c_r = round(temp_c) if temp_c is not None else None

        wind_ok = wind_mph < WIND_THRESHOLD_MPH
        rain_ok = rain_pct < RAIN_THRESHOLD_PCT
        # Bold-row rule looks at wind speed only, not gusts.
        highlight = wind_ok

        rows.append({
            "time":        dt_london.strftime("%H:%M"),
            "day":         dt_london.strftime("%a"),
            "date":        dt_london.strftime("%-d %b"),
            "wind_mph":    round(wind_mph),
            "gust_mph":    round(gust_mph),
            "wind_kmh":    wind_kmh,
            "gust_kmh":    gust_kmh,
            "wind_color":  _wind_band(wind_mph, wind_kmh),
            "gust_color":  _wind_band(gust_mph, gust_kmh),
            "rain_pct":    round(rain_pct),
            "rain_color":  "green" if rain_ok else "",
            "temp_c":      temp_c_r,
            "temp_color":  _temp_band(temp_c_r),
            "highlight":   highlight,
        })

    return rows


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    error = None
    rows = []
    try:
        rows = build_forecast_rows()
    except Exception as e:
        error = str(e)
    return render_template(
        "index.html",
        rows=rows,
        error=error,
        updated=datetime.now(LONDON_TZ).strftime("%H:%M"),
    )


@app.route("/ping")
def ping():
    return "ok", 200


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
