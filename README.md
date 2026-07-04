# Occu-Med Global Address Geocoder

A Render-ready Flask app for uploading Excel/CSV address lists, selecting a country from a landing-page world map, and geocoding through a shared Neon Postgres cache.

## What it does

- Shows a landing page with a global country-selection map
- Opens the geocoder app with the selected country prefilled
- Uploads `.xlsx`, `.xlsm`, `.xls`, or `.csv` address lists
- Lets the user select the columns that make up the address
- Checks the shared Neon `geocode_cache` table before external lookup
- Uses OpenStreetMap/Nominatim only for cache misses
- Streams real row-by-row progress while geocoding runs
- Shows a luminous three.js loader during geocoding
- Downloads the completed Excel or CSV file

## Runtime

Python is pinned to `3.11.5`.

## Render settings

Build command:

```bash
pip install --upgrade pip && pip install -r requirements.txt
```

Start command:

```bash
gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --timeout 300
```

Required Render environment variables:

- `DATABASE_URL`
- `GEOCODER_USER_AGENT`
- `NOMINATIM_BASE_URL`

Optional Render environment variable:

- `NOMINATIM_DELAY_SECONDS`

## Health check

```text
/healthz
```

## Shared Neon cache

The app creates the `geocode_cache` table and supporting indexes on startup when the database connection is first used. Repeat normalized addresses and country contexts return faster cache hits.
