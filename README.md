# Occu-Med Global Address Geocoder

A Render-ready Streamlit app for uploading Excel/CSV address lists, selecting a country context, and geocoding through a shared Neon Postgres cache.

## What it does

- Upload Excel or CSV address lists
- Select columns that make up the address
- Select a country context for better geocoding
- Check the shared Neon cache before any external lookup
- Use OpenStreetMap/Nominatim only on cache miss
- Download the completed Excel or CSV file
- Show cache hits, cache misses, processed rows, and errors

## Runtime

Python is pinned to `3.11.5` for the current Streamlit dependency stack.

## Render settings

Build command:

```bash
pip install --upgrade pip && pip install -r requirements.txt
```

Start command:

```bash
python -m streamlit run main.py --server.address=0.0.0.0 --server.port=$PORT --server.headless=true
```

Required Render environment variables:

- `DATABASE_URL`
- `GEOCODER_USER_AGENT`
- `NOMINATIM_BASE_URL`

Optional Render environment variable:

- `NOMINATIM_DELAY_SECONDS`

## Shared Neon cache

The app creates the `geocode_cache` table on startup if it does not already exist. Every analyst uses the same Neon cache, so repeat normalized addresses and country contexts return faster cache hits.
