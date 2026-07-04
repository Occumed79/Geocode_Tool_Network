# Occu-Med Global Address Geocoder

A Render-ready Streamlit app for uploading Excel/CSV address lists, selecting a country context, and geocoding through a shared Neon Postgres cache.

## What it does

- Upload `.xlsx`, `.xlsm`, `.xls`, or `.csv`
- Select columns that make up the address
- Select a country context for better geocoding
- Check shared Neon cache before any external lookup
- Use OpenStreetMap/Nominatim only on cache miss
- Save results into a shared `geocode_cache` table
- Download the completed Excel/CSV file
- Show cache hits, cache misses, processed rows, and errors

## Render settings

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
streamlit run main.py --server.address=0.0.0.0 --server.port=$PORT --server.headless=true
```

## Required environment variables

```text
DATABASE_URL=your Neon pooled connection string
GEOCODER_USER_AGENT=OccuMedAddressGeocoder/1.0 your-email@example.com
NOMINATIM_BASE_URL=https://nominatim.openstreetmap.org/search
APP_ACCESS_PASSWORD=your-password
```

Optional:

```text
GEOCODER_ANALYST=analyst name or email
NOMINATIM_DELAY_SECONDS=1.1
```

## Shared Neon cache

The app automatically creates or upgrades the `geocode_cache` table on startup. Every analyst uses the same Neon cache, so if one analyst geocodes a clinic once, the next analyst gets an instant cache hit for the same normalized address and country context.
