# Fairweather Sculler

Standalone Flask app showing forecast low tides at Hammersmith alongside
Met Office wind and rain forecasts, for planning fair-weather outings.

Portrait-first, four-column table:

1. Low tide time, day, date (e.g. `14:56 Mon 8 Aug`)
2. Wind speed/gust, mph
3. Wind speed/gust, km/h
4. Rain probability, %

Wind/rain cells turn green under 10mph / 10%. A row is bold when the low
tide falls between 8am and 6pm, wind and gusts are both under 10mph, and
no rain is forecast.

## Data sources

- **Tides** — UKHO Admiralty API, station 0115 (Hammersmith-referenced).
- **Wind & rain** — Met Office Weather DataHub, Site-Specific (Global Spot)
  forecast. Hourly (~2 days) and three-hourly (~7 days) timeseries are
  merged, and each low tide is matched to the nearest forecast point.

## Deploying on Render

This repo includes a `render.yaml` for a single Web Service. Point Render
at this branch and set the two required environment variables:

- `TIDE_API_KEY` — Admiralty UK Tidal API subscription key
- `METOFFICE_SITESPECIFIC` — Met Office Weather DataHub Site-Specific API key

## Running locally

```
pip install -r requirements.txt
TIDE_API_KEY=... METOFFICE_SITESPECIFIC=... python app.py
```
