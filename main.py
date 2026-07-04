from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import requests
import streamlit as st

try:
    import plotly.express as px
except Exception:
    px = None

try:
    import pycountry
except Exception:
    pycountry = None

APP_TITLE = "Global Address Geocoder"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
NOMINATIM_BASE_URL = os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org/search")
GEOCODER_USER_AGENT = os.getenv("GEOCODER_USER_AGENT", "OccuMedAddressGeocoder/1.0")
GEOCODER_ANALYST = os.getenv("GEOCODER_ANALYST", "").strip()
APP_ACCESS_PASSWORD = os.getenv("APP_ACCESS_PASSWORD", "").strip()
RATE_LIMIT_SECONDS = float(os.getenv("NOMINATIM_DELAY_SECONDS", "1.1"))
LOGO_PATH = Path(__file__).parent / "assets" / "occu-med-logo.svg"

OUTPUT_COLUMNS = [
    "latitude",
    "longitude",
    "geocode_status",
    "geocode_source",
    "geocode_confidence",
    "normalized_address",
    "geocode_display_name",
    "geocode_error",
    "country_context_used",
    "geocode_address_hash",
    "geocode_usage_count",
    "geocode_manual_override",
]

FALLBACK_COUNTRIES = [
    {"name": "United States", "alpha_2": "US", "alpha_3": "USA"},
    {"name": "Canada", "alpha_2": "CA", "alpha_3": "CAN"},
    {"name": "United Kingdom", "alpha_2": "GB", "alpha_3": "GBR"},
    {"name": "Australia", "alpha_2": "AU", "alpha_3": "AUS"},
    {"name": "Germany", "alpha_2": "DE", "alpha_3": "DEU"},
    {"name": "France", "alpha_2": "FR", "alpha_3": "FRA"},
    {"name": "Mexico", "alpha_2": "MX", "alpha_3": "MEX"},
]


def image_to_data_uri(path: Path) -> str:
    if not path.exists():
        return ""
    mime = "image/svg+xml" if path.suffix.lower() == ".svg" else "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{encoded}"


@st.cache_data(show_spinner=False)
def country_options() -> list[dict[str, str]]:
    if pycountry is None:
        return FALLBACK_COUNTRIES
    countries: list[dict[str, str]] = []
    for country in pycountry.countries:
        name = getattr(country, "common_name", None) or getattr(country, "name", "")
        a2 = getattr(country, "alpha_2", "")
        a3 = getattr(country, "alpha_3", "")
        if name and a2 and a3:
            countries.append({"name": name, "alpha_2": a2, "alpha_3": a3})
    return sorted(countries, key=lambda item: item["name"])


def country_by_name(name: str) -> dict[str, str]:
    countries = country_options()
    for country in countries:
        if country["name"] == name:
            return country
    return countries[0]


@st.cache_resource(show_spinner=False)
def get_connection(database_url: str) -> psycopg.Connection:
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing. Add the Neon connection string in Render environment variables.")
    conn = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
    ensure_schema(conn)
    return conn


def ensure_schema(conn: psycopg.Connection) -> None:
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
        columns = {
            "address_hash": "TEXT",
            "raw_address": "TEXT",
            "normalized_address": "TEXT",
            "country_name": "TEXT",
            "country_code": "TEXT",
            "latitude": "DOUBLE PRECISION",
            "longitude": "DOUBLE PRECISION",
            "geocode_status": "TEXT",
            "geocode_source": "TEXT",
            "geocode_confidence": "DOUBLE PRECISION",
            "display_name": "TEXT",
            "error": "TEXT",
            "provider_response_json": "JSONB",
            "usage_count": "INTEGER DEFAULT 1",
            "first_seen_at": "TIMESTAMPTZ DEFAULT NOW()",
            "last_used_at": "TIMESTAMPTZ DEFAULT NOW()",
            "created_by": "TEXT",
            "manual_override_lat": "DOUBLE PRECISION",
            "manual_override_lng": "DOUBLE PRECISION",
            "manual_override_reason": "TEXT",
            "reviewed_by": "TEXT",
            "reviewed_at": "TIMESTAMPTZ",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()",
            "updated_at": "TIMESTAMPTZ DEFAULT NOW()",
        }
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='geocode_cache'
            """
        )
        existing = {row["column_name"] for row in cur.fetchall()}
        for name, col_type in columns.items():
            if name not in existing:
                cur.execute(f"ALTER TABLE geocode_cache ADD COLUMN IF NOT EXISTS {name} {col_type}")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_geocode_cache_address_hash_unique ON geocode_cache(address_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geocode_cache_country_code ON geocode_cache(country_code)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geocode_cache_last_used_at ON geocode_cache(last_used_at DESC)")


def cache_stats(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE geocode_status='geocoded') AS geocoded,
              COUNT(*) FILTER (WHERE geocode_status='not_found') AS not_found,
              COUNT(*) FILTER (WHERE geocode_status='failed') AS failed,
              COALESCE(SUM(usage_count), 0) AS uses
            FROM geocode_cache
            """
        )
        row = cur.fetchone() or {}
        return {key: int(row.get(key) or 0) for key in ["total", "geocoded", "not_found", "failed", "uses"]}


def clean_cell(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def normalize_address(text: str) -> str:
    text = text.lower().strip()
    text = f" {text} "
    replacements = {
        " street ": " st ",
        " avenue ": " ave ",
        " boulevard ": " blvd ",
        " drive ": " dr ",
        " road ": " rd ",
        " suite ": " ste ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r",+", ",", text)
    text = re.sub(r"[^a-z0-9,#./&\- ]+", "", text)
    return text.strip(" ,")


def make_address_hash(normalized_address: str, country_code: str) -> str:
    return hashlib.sha256(f"{normalized_address}|{country_code.lower()}".encode("utf-8")).hexdigest()


def guess_country_code(value: str, selected: dict[str, str]) -> str:
    value = clean_cell(value)
    if not value:
        return selected["alpha_2"].lower()
    if pycountry is not None:
        try:
            match = pycountry.countries.lookup(value)
            return getattr(match, "alpha_2", selected["alpha_2"]).lower()
        except LookupError:
            pass
    return selected["alpha_2"].lower()


def build_address(row: pd.Series, columns: list[str], country_text: str, mode: str) -> str:
    parts = [clean_cell(row.get(col)) for col in columns]
    parts = [part for part in parts if part]
    if mode != "Do not append country context" and country_text:
        parts.append(country_text)
    return ", ".join(parts)


def lookup_cache(conn: psycopg.Connection, address_hash: str) -> dict[str, Any] | None:
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


def save_cache(conn: psycopg.Connection, record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("provider_response_json")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO geocode_cache (
                address_hash, raw_address, normalized_address, country_name, country_code,
                latitude, longitude, geocode_status, geocode_source, geocode_confidence,
                display_name, error, provider_response_json, usage_count, first_seen_at,
                last_used_at, created_by, created_at, updated_at
            ) VALUES (
                %(address_hash)s, %(raw_address)s, %(normalized_address)s, %(country_name)s, %(country_code)s,
                %(latitude)s, %(longitude)s, %(geocode_status)s, %(geocode_source)s, %(geocode_confidence)s,
                %(display_name)s, %(error)s, %(provider_response_json)s, 1, NOW(), NOW(), %(created_by)s, NOW(), NOW()
            )
            ON CONFLICT (address_hash) DO UPDATE SET
                usage_count = geocode_cache.usage_count + 1,
                last_used_at = NOW(),
                updated_at = NOW()
            RETURNING *
            """,
            {**record, "provider_response_json": Jsonb(payload)},
        )
        return dict(cur.fetchone())


def geocode_nominatim(raw_address: str, country_code: str) -> dict[str, Any]:
    headers = {"User-Agent": GEOCODER_USER_AGENT, "Accept": "application/json"}
    params: dict[str, Any] = {"q": raw_address, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    if country_code:
        params["countrycodes"] = country_code.lower()
    try:
        response = requests.get(NOMINATIM_BASE_URL, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {
            "latitude": None,
            "longitude": None,
            "geocode_status": "failed",
            "geocode_source": "nominatim",
            "geocode_confidence": None,
            "display_name": None,
            "error": str(exc),
            "provider_response_json": None,
        }
    if not payload:
        return {
            "latitude": None,
            "longitude": None,
            "geocode_status": "not_found",
            "geocode_source": "nominatim",
            "geocode_confidence": None,
            "display_name": None,
            "error": "No result returned",
            "provider_response_json": [],
        }
    best = payload[0]
    return {
        "latitude": float(best["lat"]) if best.get("lat") else None,
        "longitude": float(best["lon"]) if best.get("lon") else None,
        "geocode_status": "geocoded",
        "geocode_source": "nominatim",
        "geocode_confidence": float(best.get("importance")) if best.get("importance") else None,
        "display_name": best.get("display_name"),
        "error": None,
        "provider_response_json": best,
    }


def record_to_output(record: dict[str, Any], status: str | None, country_context: str) -> dict[str, Any]:
    has_override = record.get("manual_override_lat") is not None and record.get("manual_override_lng") is not None
    return {
        "latitude": record.get("manual_override_lat") if has_override else record.get("latitude"),
        "longitude": record.get("manual_override_lng") if has_override else record.get("longitude"),
        "geocode_status": status or record.get("geocode_status"),
        "geocode_source": "manual_override" if has_override else record.get("geocode_source"),
        "geocode_confidence": record.get("geocode_confidence"),
        "normalized_address": record.get("normalized_address"),
        "geocode_display_name": record.get("display_name"),
        "geocode_error": record.get("error"),
        "country_context_used": country_context,
        "geocode_address_hash": record.get("address_hash"),
        "geocode_usage_count": record.get("usage_count"),
        "geocode_manual_override": has_override,
    }


def blank_output(status: str, normalized_address: str = "", error: str = "", country_context: str = "") -> dict[str, Any]:
    return {col: None for col in OUTPUT_COLUMNS} | {
        "geocode_status": status,
        "normalized_address": normalized_address,
        "geocode_error": error,
        "country_context_used": country_context,
        "geocode_manual_override": False,
    }


def read_uploaded(uploaded: Any) -> pd.DataFrame:
    name = uploaded.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded)
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(uploaded)
    raise ValueError("Upload a CSV, XLSX, XLSM, or XLS file.")


def to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="geocoded")
    buffer.seek(0)
    return buffer.getvalue()


def inject_css() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stHeader"]{display:none!important}
        [data-testid="stToolbar"]{display:none!important}
        [data-testid="stDecoration"]{display:none!important}
        #MainMenu{visibility:hidden!important}
        footer{visibility:hidden!important}
        .stApp{background:radial-gradient(circle at 8% 5%,rgba(90,160,255,.28),transparent 30%),radial-gradient(circle at 90% 10%,rgba(115,235,255,.22),transparent 28%),linear-gradient(135deg,#020711,#071225 45%,#01040a);color:#f4fbff}
        .block-container{max-width:1180px;padding-top:0!important;padding-bottom:2rem!important}
        .stApp:before{content:"";position:fixed;inset:0;background-image:linear-gradient(rgba(255,255,255,.026) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.026) 1px,transparent 1px);background-size:42px 42px;pointer-events:none}
        h1,h2,h3,p,span,div,label{color:inherit}
        .glass{background:linear-gradient(135deg,rgba(255,255,255,.105),rgba(255,255,255,.035));border:1px solid rgba(132,224,255,.28);box-shadow:0 24px 80px rgba(0,0,0,.38),inset 0 1px 0 rgba(255,255,255,.13);backdrop-filter:blur(24px);border-radius:30px;padding:26px;margin:16px 0}
        .landing{height:100vh;max-height:100vh;overflow:hidden;display:grid;grid-template-rows:auto 1fr;gap:18px;padding:20px 0;box-sizing:border-box}
        .topbar{height:72px;display:flex;justify-content:space-between;align-items:center;margin:0!important}
        .landing-hero{height:calc(100vh - 126px);display:grid;grid-template-columns:1fr .95fr;gap:28px;align-items:center;margin:0!important;padding:34px}
        .landing h1{font-size:clamp(3.2rem,7vw,6.4rem);line-height:.88;margin:20px 0 14px;letter-spacing:-.075em}
        .muted{color:rgba(232,246,255,.72);font-size:1.08rem;line-height:1.65}
        .logo{width:230px;max-width:80%;filter:drop-shadow(0 0 20px rgba(160,235,255,.35))}
        .logo-small{width:130px;filter:drop-shadow(0 0 14px rgba(160,235,255,.32))}
        .pill,.chip{display:inline-flex;gap:8px;align-items:center;border:1px solid rgba(118,234,255,.30);border-radius:999px;padding:8px 14px;background:rgba(118,234,255,.07);box-shadow:0 0 24px rgba(118,234,255,.10)}
        .landing-actions{display:flex;gap:12px;align-items:center;margin-top:22px}
        .workflow-shell{padding-top:18px}
        .metric-row{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.metric{background:rgba(255,255,255,.055);border:1px solid rgba(118,234,255,.18);border-radius:22px;padding:18px}.metric b{font-size:1.55rem;color:#aef5ff}
        .globe{width:min(34vw,300px);height:min(34vw,300px);margin:auto;border-radius:50%;position:relative;background:radial-gradient(circle at 35% 30%,rgba(255,255,255,.95),rgba(118,234,255,.34) 23%,rgba(63,113,255,.12) 56%,rgba(255,255,255,.03));box-shadow:0 0 80px rgba(118,234,255,.35),inset 0 0 60px rgba(255,255,255,.12);animation:spin 9s linear infinite}.globe:before,.globe:after{content:"";position:absolute;inset:18px;border:1px solid rgba(174,245,255,.42);border-radius:50%;transform:rotate(58deg)}.globe:after{inset:46px;transform:rotate(-28deg);border-color:rgba(120,168,255,.45)}@keyframes spin{to{transform:rotate(360deg)}}.code{font-family:ui-monospace,Menlo,monospace;color:rgba(180,245,255,.62);font-size:.78rem;line-height:1.7}
        .stButton>button,.stDownloadButton>button{border-radius:999px!important;border:1px solid rgba(118,234,255,.48)!important;background:linear-gradient(135deg,rgba(120,168,255,.95),rgba(118,234,255,.86))!important;color:#03111f!important;font-weight:850!important;box-shadow:0 0 34px rgba(118,234,255,.26)!important;padding:.85rem 1.6rem!important}
        .stTextInput input,div[data-baseweb='select']>div,div[data-testid='stFileUploader'] section{background:rgba(255,255,255,.055)!important;border:1px solid rgba(118,234,255,.22)!important;border-radius:18px!important;color:#f4fbff!important}
        .stProgress > div > div > div > div{background:linear-gradient(90deg,#78a8ff,#76eaff)!important}
        @media(max-width:900px){.landing{height:auto;max-height:none;overflow:visible}.landing-hero{height:auto;grid-template-columns:1fr}.metric-row{grid-template-columns:1fr 1fr}.landing h1{font-size:3rem}.globe{width:220px;height:220px}}
        </style>
        """,
        unsafe_allow_html=True,
    )


def logo_img(src: str, css_class: str) -> str:
    if src:
        return f'<img class="{css_class}" src="{src}" alt="Occu-Med logo" />'
    return '<strong>OCCU-MED</strong>'


def render_map(countries: list[dict[str, str]], selected: dict[str, str], height: int = 300) -> None:
    if px is None:
        st.info("Plotly is required for the luminous global map.")
        return
    df = pd.DataFrame(countries)
    df["selected"] = df["alpha_3"].eq(selected["alpha_3"])
    df["intensity"] = df["selected"].map({True: 1.0, False: 0.08})
    fig = px.choropleth(
        df,
        locations="alpha_3",
        color="intensity",
        hover_name="name",
        color_continuous_scale=[[0, "#07172d"], [1, "#76eaff"]],
        projection="natural earth",
    )
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        coloraxis_showscale=False,
        height=height,
        geo=dict(bgcolor="rgba(0,0,0,0)", showframe=False, showcoastlines=True, coastlinecolor="rgba(160,235,255,.25)"),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def require_password() -> bool:
    if not APP_ACCESS_PASSWORD:
        return True
    with st.sidebar:
        st.markdown("### Access")
        entered = st.text_input("App password", type="password")
    return entered == APP_ACCESS_PASSWORD


def render_landing(logo_src: str, countries: list[dict[str, str]]) -> None:
    names = [country["name"] for country in countries]
    selected = country_by_name(st.session_state.selected_country)

    st.markdown('<div class="landing">', unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="glass topbar">
          <div>{logo_img(logo_src, 'logo-small')}</div>
          <div class="pill">Selected country: <b>{selected["name"]}</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="glass landing-hero">', unsafe_allow_html=True)
    left, right = st.columns([1.05, 0.95], gap="large")
    with left:
        st.markdown(
            f"""
            {logo_img(logo_src, 'logo')}
            <div class="pill" style="margin-top:24px;">Shared Neon cache • Free fallback • Excel-ready</div>
            <h1>Global Address Geocoder</h1>
            <p class="muted">Upload spreadsheets, choose a country context, and geocode addresses through a shared Neon memory so analysts do not geocode the same location twice.</p>
            """,
            unsafe_allow_html=True,
        )
        selected_name = st.selectbox("Country context", names, index=names.index(st.session_state.selected_country), key="landing_country")
        st.session_state.selected_country = selected_name
        if st.button("Start Geocoding", type="primary", use_container_width=False):
            st.session_state.workflow_started = True
            st.rerun()
    with right:
        st.markdown('<div class="globe"></div>', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="glass code" style="padding:14px;margin-top:14px;">
              &gt; normalize(address + country)<br>
              &gt; check Neon geocode_cache<br>
              &gt; cache hit: return coordinates instantly<br>
              &gt; cache miss: call free Nominatim once
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_workflow_header(logo_src: str, selected_country: str) -> None:
    st.markdown(
        f"""
        <div class="glass topbar">
          <div>{logo_img(logo_src, 'logo-small')}</div>
          <div class="pill">Selected country: <b>{selected_country}</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def geocode_dataframe(df: pd.DataFrame, address_cols: list[str], country_col: str | None, selected: dict[str, str], mode: str) -> tuple[pd.DataFrame, dict[str, int]]:
    conn = get_connection(DATABASE_URL)
    outputs: list[dict[str, Any]] = []
    stats = {"total": len(df), "processed": 0, "cache_hits": 0, "cache_misses": 0, "errors": 0}
    progress = st.progress(0, text="Starting geocode run...")
    metric_holder = st.empty()

    for _, row in df.iterrows():
        spreadsheet_country = clean_cell(row.get(country_col)) if country_col else ""
        if mode == "Use selected country for every row":
            country_text = selected["name"]
        elif mode == "Use spreadsheet country when available, otherwise selected country":
            country_text = spreadsheet_country or selected["name"]
        else:
            country_text = ""
        country_code = guess_country_code(country_text, selected) if country_text else ""
        raw_address = build_address(row, address_cols, country_text, mode)
        normalized = normalize_address(raw_address)
        if not normalized:
            outputs.append(blank_output("blank", error="No usable address", country_context=country_text))
            stats["errors"] += 1
        else:
            address_hash = make_address_hash(normalized, country_code)
            cached = lookup_cache(conn, address_hash)
            if cached:
                stats["cache_hits"] += 1
                outputs.append(record_to_output(cached, "cache_hit", country_text))
            else:
                stats["cache_misses"] += 1
                geo = geocode_nominatim(raw_address, country_code)
                if geo["geocode_status"] == "failed":
                    stats["errors"] += 1
                saved = save_cache(conn, {"address_hash": address_hash, "raw_address": raw_address, "normalized_address": normalized, "country_name": country_text, "country_code": country_code, "created_by": GEOCODER_ANALYST or None, **geo})
                outputs.append(record_to_output(saved, None, country_text))
                time.sleep(RATE_LIMIT_SECONDS)
        stats["processed"] += 1
        progress.progress(stats["processed"] / max(stats["total"], 1), text=f"Processed {stats['processed']} of {stats['total']} rows")
        metric_holder.markdown(
            f"""
            <div class="metric-row">
              <div class="metric"><span>Total</span><br><b>{stats['total']}</b></div>
              <div class="metric"><span>Processed</span><br><b>{stats['processed']}</b></div>
              <div class="metric"><span>Cache hits</span><br><b>{stats['cache_hits']}</b></div>
              <div class="metric"><span>Cache misses</span><br><b>{stats['cache_misses']}</b></div>
              <div class="metric"><span>Errors</span><br><b>{stats['errors']}</b></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    return pd.concat([df.reset_index(drop=True), pd.DataFrame(outputs)], axis=1), stats


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="🧭", layout="wide", initial_sidebar_state="collapsed")
    inject_css()
    logo_src = image_to_data_uri(LOGO_PATH)
    countries = country_options()
    names = [country["name"] for country in countries]
    if "selected_country" not in st.session_state:
        st.session_state.selected_country = "United States" if "United States" in names else names[0]
    if "workflow_started" not in st.session_state:
        st.session_state.workflow_started = False

    if not require_password():
        st.error("Enter the app password to continue.")
        st.stop()

    if not st.session_state.workflow_started:
        render_landing(logo_src, countries)
        st.stop()

    selected = country_by_name(st.session_state.selected_country)

    st.markdown('<div class="workflow-shell">', unsafe_allow_html=True)
    render_workflow_header(logo_src, selected["name"])

    if st.button("Back to landing"):
        st.session_state.workflow_started = False
        st.rerun()

    st.markdown('<div class="glass"><h2>1. Select country context</h2>', unsafe_allow_html=True)
    selected_name = st.selectbox("Country", names, index=names.index(st.session_state.selected_country), key="workflow_country")
    st.session_state.selected_country = selected_name
    selected = country_by_name(selected_name)
    render_map(countries, selected, height=300)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="glass"><h2>2. Upload file and map columns</h2>', unsafe_allow_html=True)
    uploaded = st.file_uploader("Upload Excel or CSV", type=["csv", "xlsx", "xlsm", "xls"])
    if not uploaded:
        st.markdown("</div></div>", unsafe_allow_html=True)
        st.stop()
    df = read_uploaded(uploaded)
    st.dataframe(df.head(25), use_container_width=True)
    columns = list(df.columns)
    suggested = [col for col in columns if str(col).lower() in {"address", "street", "city", "state", "zip", "postal_code", "country"}]
    address_cols = st.multiselect("Columns that make up the address", columns, default=suggested)
    country_choice = st.selectbox("Country column, if the file has one", ["None"] + columns)
    country_col = None if country_choice == "None" else country_choice
    country_mode = st.radio("Country handling", ["Use selected country for every row", "Use spreadsheet country when available, otherwise selected country", "Do not append country context"], index=1)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="glass"><h2>3. Geocode with shared Neon memory</h2><div class="globe" style="width:160px;height:160px"></div>', unsafe_allow_html=True)
    if st.button("Start Geocoding", disabled=not address_cols):
        try:
            result_df, run_stats = geocode_dataframe(df, address_cols, country_col, selected, country_mode)
            st.session_state.result_df = result_df
            st.session_state.run_stats = run_stats
            st.success("Geocoding complete.")
        except Exception as exc:
            st.error(f"Geocoding failed: {exc}")
    st.markdown("</div>", unsafe_allow_html=True)

    if "result_df" in st.session_state:
        result_df = st.session_state.result_df
        st.markdown('<div class="glass"><h2>4. Download results</h2>', unsafe_allow_html=True)
        st.dataframe(result_df.head(100), use_container_width=True)
        st.download_button("Download Excel", data=to_xlsx_bytes(result_df), file_name="geocoded_addresses.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.download_button("Download CSV", data=result_df.to_csv(index=False).encode("utf-8"), file_name="geocoded_addresses.csv", mime="text/csv")
        st.markdown("</div>", unsafe_allow_html=True)

    if DATABASE_URL:
        try:
            stats = cache_stats(get_connection(DATABASE_URL))
            st.markdown(f'<div class="glass"><h2>Shared Neon cache</h2><span class="chip">Total records: {stats["total"]}</span> <span class="chip">Geocoded: {stats["geocoded"]}</span> <span class="chip">Not found: {stats["not_found"]}</span> <span class="chip">Failed: {stats["failed"]}</span> <span class="chip">Total uses: {stats["uses"]}</span></div>', unsafe_allow_html=True)
        except Exception:
            pass

    st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
