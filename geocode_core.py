from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import time
from typing import Any, Generator

import pandas as pd
import psycopg
import pycountry
import requests
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
NOMINATIM_BASE_URL = os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org/search").strip()
GEOCODER_USER_AGENT = os.getenv("GEOCODER_USER_AGENT", "OccuMedAddressGeocoder/1.0").strip()
NOMINATIM_DELAY_SECONDS = float(os.getenv("NOMINATIM_DELAY_SECONDS", "1.0"))


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing. Set it in Render environment variables.")

    conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute(
            """
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
            """
        )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_geocode_cache_address_hash_unique ON geocode_cache(address_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geocode_cache_normalized_country ON geocode_cache(normalized_address, country_code)")
    return conn


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def normalize(text: str) -> str:
    text = f" {clean(text).lower()} "
    for old, new in {
        " street ": " st ",
        " avenue ": " ave ",
        " boulevard ": " blvd ",
        " drive ": " dr ",
        " road ": " rd ",
        " suite ": " ste ",
    }.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r",+", ",", text)
    text = re.sub(r"[^a-z0-9,#./&\- ]+", "", text)
    return text.strip(" ,")


def country_code(country: str) -> str:
    value = clean(country)
    if not value:
        return ""

    for key in ("alpha_2", "alpha_3"):
        try:
            match = pycountry.countries.get(**{key: value.upper()})
            if match:
                return match.alpha_2.lower()
        except Exception:
            pass

    try:
        matches = pycountry.countries.search_fuzzy(value)
        if matches:
            return matches[0].alpha_2.lower()
    except Exception:
        pass

    return value[:2].lower()


def hash_address(normalized: str, code: str) -> str:
    return hashlib.sha256(f"{normalized}|{code}".encode("utf-8")).hexdigest()


def cache_lookup(conn, address_hash: str):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM geocode_cache WHERE address_hash=%s", (address_hash,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            """
            UPDATE geocode_cache
            SET usage_count = COALESCE(usage_count, 0) + 1,
                last_used_at = NOW(),
                updated_at = NOW()
            WHERE address_hash=%s
            RETURNING *
            """,
            (address_hash,),
        )
        return dict(cur.fetchone())


def cache_save(conn, row: dict):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO geocode_cache (
                address_hash, raw_address, normalized_address, country_name, country_code,
                latitude, longitude, geocode_status, geocode_source, geocode_confidence,
                display_name, error, provider_response_json, usage_count,
                first_seen_at, last_used_at, created_by, created_at, updated_at
            ) VALUES (
                %(address_hash)s, %(raw_address)s, %(normalized_address)s, %(country_name)s, %(country_code)s,
                %(latitude)s, %(longitude)s, %(geocode_status)s, %(geocode_source)s, %(geocode_confidence)s,
                %(display_name)s, %(error)s, %(provider_response_json)s, 1,
                NOW(), NOW(), NULL, NOW(), NOW()
            )
            ON CONFLICT (address_hash) DO UPDATE SET
                usage_count = geocode_cache.usage_count + 1,
                last_used_at = NOW(),
                updated_at = NOW()
            RETURNING *
            """,
            {**row, "provider_response_json": Jsonb(row.get("provider_response_json"))},
        )
        return dict(cur.fetchone())


def geocode(address: str, code: str):
    params = {"q": address, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    if code:
        params["countrycodes"] = code
    try:
        res = requests.get(
            NOMINATIM_BASE_URL,
            params=params,
            headers={"User-Agent": GEOCODER_USER_AGENT},
            timeout=25,
        )
        res.raise_for_status()
        payload = res.json()
    except Exception as exc:
        return None, None, "failed", None, None, str(exc), None

    if not payload:
        return None, None, "not_found", None, None, "No result returned", []

    best = payload[0]
    return (
        float(best["lat"]),
        float(best["lon"]),
        "geocoded",
        "nominatim",
        float(best.get("importance") or 0),
        best.get("display_name"),
        best,
    )


def read_uploaded_file(file_storage) -> pd.DataFrame:
    filename = file_storage.filename.lower()
    if filename.endswith(".csv"):
        return pd.read_csv(file_storage)
    return pd.read_excel(file_storage)


def safe_records(df: pd.DataFrame, limit: int = 50):
    safe = df.head(limit).where(pd.notnull(df.head(limit)), None)
    return safe.astype(object).to_dict(orient="records")


def default_columns(columns: list[str]) -> list[str]:
    keys = {"address", "street", "city", "state", "zip", "zipcode", "postal_code", "postal code", "completeaddress"}
    return [str(col) for col in columns if str(col).strip().lower() in keys]


def dataframe_payload(df: pd.DataFrame, output_format: str):
    if output_format == "csv":
        file_b64 = base64.b64encode(df.to_csv(index=False).encode("utf-8")).decode("ascii")
        return file_b64, "geocoded_addresses.csv", "text/csv"

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="geocoded")
    buffer.seek(0)
    file_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return file_b64, "geocoded_addresses.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def process_rows(
    df: pd.DataFrame,
    address_cols: list[str],
    country: str,
    delay: float,
    max_external: int,
    cache_only: bool,
) -> Generator[dict, None, None]:
    conn = get_conn()
    code = country_code(country)
    output = []
    stats = {"processed": 0, "cache_hits": 0, "cache_misses": 0, "external": 0, "errors": 0}
    total = len(df)

    try:
        for _, row in df.iterrows():
            raw = ", ".join([clean(row.get(col)) for col in address_cols if clean(row.get(col))] + ([country] if country else []))
            norm = normalize(raw)
            ahash = hash_address(norm, code)
            cached = cache_lookup(conn, ahash)

            if cached:
                result, status = cached, "cache_hit"
                stats["cache_hits"] += 1
            elif cache_only or stats["external"] >= max_external:
                result = {
                    "latitude": None,
                    "longitude": None,
                    "geocode_source": None,
                    "geocode_confidence": None,
                    "display_name": None,
                    "normalized_address": norm,
                }
                status = "cache_miss"
                stats["cache_misses"] += 1
            else:
                lat, lon, gstatus, source, confidence, display_name, payload = geocode(raw, code)
                result = cache_save(
                    conn,
                    {
                        "address_hash": ahash,
                        "raw_address": raw,
                        "normalized_address": norm,
                        "country_name": country,
                        "country_code": code,
                        "latitude": lat,
                        "longitude": lon,
                        "geocode_status": gstatus,
                        "geocode_source": source,
                        "geocode_confidence": confidence,
                        "display_name": display_name,
                        "error": None if gstatus in ("geocoded", "not_found") else str(payload),
                        "provider_response_json": payload,
                    },
                )
                status = gstatus
                stats["cache_misses"] += 1
                stats["external"] += 1
                time.sleep(max(0, delay))

            output.append(
                {
                    "latitude": result.get("latitude"),
                    "longitude": result.get("longitude"),
                    "geocode_status": status,
                    "geocode_source": result.get("geocode_source"),
                    "geocode_confidence": result.get("geocode_confidence"),
                    "display_name": result.get("display_name"),
                    "normalized_address": result.get("normalized_address"),
                }
            )
            stats["processed"] += 1
            if status == "failed":
                stats["errors"] += 1
            yield {"type": "progress", "stats": stats.copy(), "total": total}
    finally:
        conn.close()

    result_df = pd.concat([df.reset_index(drop=True), pd.DataFrame(output)], axis=1)
    yield {"type": "complete", "stats": stats.copy(), "total": total, "result_df": result_df}
