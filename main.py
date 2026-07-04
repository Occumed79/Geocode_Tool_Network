from __future__ import annotations

import hashlib
import io
import os
import re
import time
from typing import Any

import pandas as pd
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import requests
import streamlit as st

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
NOMINATIM_BASE_URL = os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org/search").strip()
GEOCODER_USER_AGENT = os.getenv("GEOCODER_USER_AGENT", "OccuMedAddressGeocoder/1.0").strip()
GEOCODER_ANALYST = os.getenv("GEOCODER_ANALYST", "").strip()
NOMINATIM_DELAY_SECONDS = float(os.getenv("NOMINATIM_DELAY_SECONDS", "1.0"))


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing in Render environment variables.")
    conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geocode_cache (
                id BIGSERIAL PRIMARY KEY,
                address_hash TEXT UNIQUE NOT NULL,
                raw_address TEXT,
                normalized_address TEXT NOT NULL,
                country_name TEXT,
                country_code TEXT,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                geocode_status TEXT NOT NULL,
                geocode_source TEXT,
                geocode_confidence DOUBLE PRECISION,
                display_name TEXT,
                error TEXT,
                provider_response_json JSONB,
                usage_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_by TEXT,
                manual_override_lat DOUBLE PRECISION,
                manual_override_lng DOUBLE PRECISION,
                manual_override_reason TEXT,
                reviewed_by TEXT,
                reviewed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_geocode_cache_address_hash_unique ON geocode_cache(address_hash)")
    return conn


@st.cache_resource(show_spinner=False)
def cached_conn():
    return get_conn()


def clean(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def normalize(text: str) -> str:
    text = f" {clean(text).lower()} "
    for old, new in {
        " street ": " st ", " avenue ": " ave ", " boulevard ": " blvd ",
        " drive ": " dr ", " road ": " rd ", " suite ": " ste ",
    }.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r",+", ",", text)
    text = re.sub(r"[^a-z0-9,#./&\- ]+", "", text)
    return text.strip(" ,")


def country_code(country: str) -> str:
    value = clean(country).lower()
    return {"usa": "us", "united states": "us", "canada": "ca", "mexico": "mx"}.get(value, value[:2])


def hash_address(normalized: str, code: str) -> str:
    return hashlib.sha256(f"{normalized}|{code}".encode("utf-8")).hexdigest()


def cache_lookup(conn, address_hash: str):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM geocode_cache WHERE address_hash=%s", (address_hash,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("""
            UPDATE geocode_cache
            SET usage_count = COALESCE(usage_count, 0) + 1,
                last_used_at = NOW(),
                updated_at = NOW()
            WHERE address_hash=%s
            RETURNING *
        """, (address_hash,))
        return dict(cur.fetchone())


def cache_save(conn, row: dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO geocode_cache (
                address_hash, raw_address, normalized_address, country_name, country_code,
                latitude, longitude, geocode_status, geocode_source, geocode_confidence,
                display_name, error, provider_response_json, usage_count,
                first_seen_at, last_used_at, created_by, created_at, updated_at
            ) VALUES (
                %(address_hash)s, %(raw_address)s, %(normalized_address)s, %(country_name)s, %(country_code)s,
                %(latitude)s, %(longitude)s, %(geocode_status)s, %(geocode_source)s, %(geocode_confidence)s,
                %(display_name)s, %(error)s, %(provider_response_json)s, 1,
                NOW(), NOW(), %(created_by)s, NOW(), NOW()
            )
            ON CONFLICT (address_hash) DO UPDATE SET
                usage_count = geocode_cache.usage_count + 1,
                last_used_at = NOW(), updated_at = NOW()
            RETURNING *
        """, {**row, "provider_response_json": Jsonb(row.get("provider_response_json"))})
        return dict(cur.fetchone())


def nominatim(address: str, code: str):
    params = {"q": address, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    if code:
        params["countrycodes"] = code
    try:
        r = requests.get(NOMINATIM_BASE_URL, params=params, headers={"User-Agent": GEOCODER_USER_AGENT}, timeout=25)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        return None, None, "failed", None, None, str(exc), None
    if not payload:
        return None, None, "not_found", None, None, "No result returned", []
    best = payload[0]
    return float(best["lat"]), float(best["lon"]), "geocoded", "nominatim", float(best.get("importance") or 0), best.get("display_name"), best


def read_file(uploaded):
    name = uploaded.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded)
    return pd.read_excel(uploaded)


def xlsx_bytes(df):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="geocoded")
    buffer.seek(0)
    return buffer.getvalue()


def main():
    st.set_page_config(page_title="Free Address Geocoder", page_icon="📍", layout="wide")
    st.sidebar.header("Settings")
    st.sidebar.text_input("Nominatim User-Agent / contact", value=GEOCODER_USER_AGENT, disabled=True)
    st.sidebar.text_input("Nominatim URL", value=NOMINATIM_BASE_URL, disabled=True)
    country = st.sidebar.text_input("Optional country code filter", value="USA")
    delay = st.sidebar.number_input("Delay between new lookups", min_value=0.0, value=NOMINATIM_DELAY_SECONDS, step=0.25)
    max_external = st.sidebar.number_input("Max new external lookups this run", min_value=0, value=500, step=50)
    cache_only = st.sidebar.checkbox("Cache-only mode")

    st.title("📍 Free Address Geocoder")
    st.caption("Upload an Excel or CSV file, select address columns, preview, then geocode using the shared Neon cache.")

    uploaded = st.file_uploader("Upload Excel or CSV", type=["xlsx", "xlsm", "xls", "csv"])
    if not uploaded:
        st.stop()

    df = read_file(uploaded)
    cols = list(df.columns)
    st.subheader("1. Select address columns")
    default = [c for c in cols if str(c).lower() in {"address", "street", "city", "state", "zip", "zipcode", "postal_code", "completeaddress"}]
    address_cols = st.multiselect("Columns to combine into the geocoding address", cols, default=default)
    if not address_cols:
        st.error("Select at least one address column.")
        st.stop()

    preview = df.head(20).copy()
    preview["_preview_full_address"] = preview.apply(lambda row: ", ".join([clean(row.get(c)) for c in address_cols if clean(row.get(c))] + ([country] if country else [])), axis=1)
    st.subheader("2. Preview")
    st.dataframe(preview, use_container_width=True)

    fmt = st.radio("Download format", ["Excel", "CSV"], horizontal=True)
    if st.button("Run geocoding", type="primary"):
        conn = cached_conn()
        code = country_code(country)
        out = []
        stats = {"processed": 0, "cache_hits": 0, "cache_misses": 0, "external": 0, "errors": 0}
        progress = st.progress(0)
        status = st.empty()
        for i, row in df.iterrows():
            raw = ", ".join([clean(row.get(c)) for c in address_cols if clean(row.get(c))] + ([country] if country else []))
            norm = normalize(raw)
            ahash = hash_address(norm, code)
            cached = cache_lookup(conn, ahash)
            if cached:
                result = cached
                result_status = "cache_hit"
                stats["cache_hits"] += 1
            elif cache_only or stats["external"] >= int(max_external):
                result = {"latitude": None, "longitude": None, "geocode_status": "cache_miss", "geocode_source": None, "geocode_confidence": None, "normalized_address": norm, "display_name": None[...]
                result_status = "cache_miss"
                stats["cache_misses"] += 1
            else:
                lat, lon, gstatus, source, confidence, display_name, payload = nominatim(raw, code)
                result = cache_save(conn, {"address_hash": ahash, "raw_address": raw, "normalized_address": norm, "country_name": country, "country_code": code, "latitude": lat, "longitude": lon,[...]
                result_status = gstatus
                stats["cache_misses"] += 1
                stats["external"] += 1
                time.sleep(float(delay))
            out.append({"latitude": result.get("latitude"), "longitude": result.get("longitude"), "geocode_status": result_status, "geocode_source": result.get("geocode_source"), "geocode_confide[...]
            stats["processed"] += 1
            if result_status == "failed":
                stats["errors"] += 1
            progress.progress((i + 1) / len(df))
            status.write(stats)
        result_df = pd.concat([df.reset_index(drop=True), pd.DataFrame(out)], axis=1)
        st.session_state["result_df"] = result_df
        st.success("Geocoding complete.")

    if "result_df" in st.session_state:
        result_df = st.session_state["result_df"]
        st.subheader("3. Results")
        st.dataframe(result_df.head(100), use_container_width=True)
        if fmt == "Excel":
            st.download_button("Download Excel", xlsx_bytes(result_df), "geocoded_addresses.xlsx")
        else:
            st.download_button("Download CSV", result_df.to_csv(index=False).encode("utf-8"), "geocoded_addresses.csv")


if __name__ == "__main__":
    main()
