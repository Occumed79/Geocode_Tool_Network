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
import pycountry
import requests
import streamlit as st

# Configuration
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
        # index to speed common lookups by normalized_address + country_code
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geocode_cache_normalized_country ON geocode_cache(normalized_address, country_code)")
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
    """Return an ISO 3166-1 alpha2 country code (lowercase) for a given country name or code.

    Uses pycountry for robust mapping. Falls back to first two letters if unknown.
    """
    value = clean(country)
    if not value:
        return ""
    # If user already supplied alpha2 or alpha3, try to normalize
    try:
        c = pycountry.countries.get(alpha_2=value.upper())
        if c:
            return c.alpha_2.lower()
    except Exception:
        pass
    try:
        c = pycountry.countries.get(alpha_3=value.upper())
        if c:
            return c.alpha_2.lower()
    except Exception:
        pass
    # Try fuzzy search by name
    try:
        matches = pycountry.countries.search_fuzzy(value)
        if matches:
            return matches[0].alpha_2.lower()
    except Exception:
        pass
    # last resort
    return value[:2].lower()


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


# Helper: macOS Tahoe liquid glass CSS injection
GLASS_CSS = """
<style>
:root{
  --glass-bg: rgba(255,255,255,0.06);
  --panel-bg: rgba(255,255,255,0.08);
  --accent: #7df9ff;
  --accent-2: #9b5cff;
}
body { background: linear-gradient(180deg, #0f1724 0%, #071226 100%); }
.stApp > div:first-child { padding-top: 8px !important; }
.header-glass { display:flex; align-items:center; justify-content:center; padding:16px; backdrop-filter: blur(8px) saturate(120%); -webkit-backdrop-filter: blur(8px) saturate(120%); background: linear-gradient(135deg, rgba(255,255,255,0.03), rgba(255,255,255,0.02)); border-radius:12px; box-shadow: 0 6px 30px rgba(30,41,59,0.6), inset 0 1px 0 rgba(255,255,255,0.02); }
.panel-glass { padding:18px; border-radius:14px; background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.01)); backdrop-filter: blur(8px); box-shadow: 0 8px 24px rgba(12,18,30,0.6); border: 1px solid rgba(255,255,255,0.03); }
.neon-text { color: var(--accent); text-shadow: 0 0 8px rgba(125,249,255,0.25); }
.logo-centered { display:flex; justify-content:center; align-items:center; margin-bottom:8px; }
.loader-overlay { position: relative; width:100%; display:flex; align-items:center; justify-content:center; flex-direction:column; }
</style>
"""


def landing_map_html():
    # Lightweight D3 world map that returns country name on click
    return """
<!doctype html>
<html>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1' />
  <style>
    html, body { margin:0; padding:0; height:100%; background: linear-gradient(180deg, #071226 0%, #021019 100%);} 
    #map { width:100%; height: calc(100vh - 120px); }
    .country { fill: #111827; stroke: rgba(255,255,255,0.04); stroke-width:0.5; }
    .country:hover { fill: #0ea5a4; cursor:pointer; }
    .label { font-family: Inter, sans-serif; fill: #e6eef6; font-size: 13px; }
  </style>
</head>
<body>
<div id='map'></div>
<script src='https://d3js.org/d3.v7.min.js'></script>
<script>
(async function(){
  const width = Math.max(window.innerWidth, 960);
  const height = Math.max(window.innerHeight - 120, 600);
  const svg = d3.select('#map').append('svg').attr('width', width).attr('height', height);
  const projection = d3.geoMercator().scale(width/6.8).translate([width/2, height/1.6]);
  const path = d3.geoPath().projection(projection);
  const world = await fetch('https://raw.githubusercontent.com/holtzy/D3-graph-gallery/master/DATA/world.geojson').then(r=>r.json());

  svg.append('g').selectAll('path')
    .data(world.features)
    .join('path')
      .attr('d', path)
      .attr('class','country')
      .on('mouseover', function(e,d){ d3.select(this).attr('fill', '#134e4a'); })
      .on('mouseout', function(e,d){ d3.select(this).attr('fill', null); })
      .on('click', function(e,d){
         const name = d.properties.name || d.properties.ADMIN || d.id;
         // navigate to same page with ?country=Country+Name
         const qs = new URLSearchParams(window.location.search);
         qs.set('country', name);
         window.location.href = window.location.pathname + '?' + qs.toString();
      });

})();
</script>
</body>
</html>
"""


def threejs_loader_html():
    # Simple three.js rotating luminous globe / geometric shape
    return """
<!doctype html>
<html>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1' />
  <style>
    html,body{margin:0;padding:0;background:transparent}
    #c{width:100%;height:420px;display:block}
    .center{display:flex;align-items:center;justify-content:center;flex-direction:column}
  </style>
</head>
<body>
<canvas id='c'></canvas>
<script src='https://cdnjs.cloudflare.com/ajax/libs/three.js/r134/three.min.js'></script>
<script>
  const canvas = document.getElementById('c');
  const renderer = new THREE.WebGLRenderer({canvas: canvas, antialias:true, alpha:true});
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, canvas.clientWidth / canvas.clientHeight, 0.1, 1000);
  camera.position.z = 3.5;
  renderer.setPixelRatio(window.devicePixelRatio);
  function resize(){
    const w = canvas.clientWidth;
    const h = 420;
    renderer.setSize(w,h,true);
    camera.aspect = w/h; camera.updateProjectionMatrix();
  }
  resize();
  window.addEventListener('resize', resize);

  // lights
  const light = new THREE.PointLight(0x7df9ff, 1.2);
  light.position.set(5,5,5);
  scene.add(light);
  const amb = new THREE.AmbientLight(0x222222);
  scene.add(amb);

  // geometry
  const geo = new THREE.IcosahedronGeometry(1.0, 5);
  const mat = new THREE.MeshStandardMaterial({
    color: 0x101827,
    emissive: 0x5eead4,
    emissiveIntensity: 0.18,
    metalness: 0.4,
    roughness: 0.2,
    transparent: true,
    opacity: 0.95
  });
  const mesh = new THREE.Mesh(geo, mat);
  scene.add(mesh);

  // subtle wireframe
  const wire = new THREE.LineSegments(new THREE.WireframeGeometry(geo), new THREE.LineBasicMaterial({color:0x9b5cff, linewidth:1, opacity:0.22}));
  scene.add(wire);

  let t0 = performance.now();
  function render(t){
    const dt = (t - t0) * 0.001; t0 = t;
    mesh.rotation.y += dt * 0.3;
    mesh.rotation.x += dt * 0.08;
    wire.rotation.y += dt * 0.28;
    renderer.render(scene, camera);
    requestAnimationFrame(render);
  }
  requestAnimationFrame(render);
</script>
</body>
</html>
"""


def main():
    st.set_page_config(page_title="Free Address Geocoder", page_icon="📍", layout="wide")
    # inject glass css
    st.markdown(GLASS_CSS, unsafe_allow_html=True)

    # read query params for landing map country
    params = st.experimental_get_query_params()
    selected_country = params.get('country', [None])[0]

    # Top header with centered logo
    with st.container():
        st.markdown("<div class='header-glass logo-centered'>", unsafe_allow_html=True)
        try:
            if os.path.exists('assets/logo.png'):
                st.image('assets/logo.png', width=220)
            else:
                st.markdown("<div style='text-align:center'><h2 class='neon-text'>OCCU‑MED</h2></div>", unsafe_allow_html=True)
        except Exception:
            st.markdown("<div style='text-align:center'><h2 class='neon-text'>OCCU‑MED</h2></div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    if not selected_country:
        # Show landing full-screen world map
        st.markdown("""
        <div class='panel-glass' style='margin:16px'>
          <h1 style='text-align:center;color:#e6eef6'>Select a country to geocode</h1>
        </div>
        """, unsafe_allow_html=True)
        html = landing_map_html()
        st.components.v1.html(html, height=700, scrolling=True)
        st.stop()

    # Otherwise continue to the app with selected country
    country = selected_country

    st.sidebar.header("Settings")
    st.sidebar.text_input("Nominatim User-Agent / contact", value=GEOCODER_USER_AGENT, disabled=True)
    st.sidebar.text_input("Nominatim URL", value=NOMINATIM_BASE_URL, disabled=True)
    # prefill from selected country but allow editing
    country_input = st.sidebar.text_input("Optional country (name or code)", value=country)
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
    preview["_preview_full_address"] = preview.apply(lambda row: ", ".join([clean(row.get(c)) for c in address_cols if clean(row.get(c))] + ([country_input] if country_input else [])), axis=1)
    st.subheader("2. Preview")
    st.dataframe(preview, use_container_width=True)

    fmt = st.radio("Download format", ["Excel", "CSV"], horizontal=True)

    # placeholders for loader and progress
    loader_ph = st.empty()
    progress_ph = st.empty()
    status_ph = st.empty()

    if st.button("Run geocoding", type="primary"):
        # show three.js loader overlay
        loader_ph.markdown("<div class='panel-glass loader-overlay'>", unsafe_allow_html=True)
        loader_ph.markdown("<div style='width:100%;max-width:720px;margin:0 auto;text-align:center'>", unsafe_allow_html=True)
        loader_ph.components.v1.html(threejs_loader_html(), height=460, scrolling=False)
        loader_ph.markdown("</div>", unsafe_allow_html=True)

        conn = cached_conn()
        code = country_code(country_input)
        out = []
        stats = {"processed": 0, "cache_hits": 0, "cache_misses": 0, "external": 0, "errors": 0}
        progress = progress_ph.progress(0)
        status = status_ph.empty()

        for i, row in df.iterrows():
            raw = ", ".join([clean(row.get(c)) for c in address_cols if clean(row.get(c))] + ([country_input] if country_input else []))
            norm = normalize(raw)
            ahash = hash_address(norm, code)
            cached = cache_lookup(conn, ahash)
            if cached:
                result = cached
                result_status = "cache_hit"
                stats["cache_hits"] += 1
            elif cache_only or stats["external"] >= int(max_external):
                result = {"latitude": None, "longitude": None, "geocode_status": "cache_miss", "geocode_source": None, "geocode_confidence": None, "normalized_address": norm, "display_name": None, "error": None}
                result_status = "cache_miss"
                stats["cache_misses"] += 1
            else:
                lat, lon, gstatus, source, confidence, display_name, payload = nominatim(raw, code)
                save_row = {
                    "address_hash": ahash,
                    "raw_address": raw,
                    "normalized_address": norm,
                    "country_name": country_input,
                    "country_code": code,
                    "latitude": lat,
                    "longitude": lon,
                    "geocode_status": gstatus,
                    "geocode_source": source,
                    "geocode_confidence": confidence,
                    "display_name": display_name,
                    "error": None if gstatus in ("geocoded", "not_found") else str(payload),
                    "provider_response_json": payload,
                    "created_by": GEOCODER_ANALYST or None,
                }
                result = cache_save(conn, save_row)
                result_status = gstatus
                stats["cache_misses"] += 1
                stats["external"] += 1
                time.sleep(float(delay))

            out.append({"latitude": result.get("latitude"), "longitude": result.get("longitude"), "geocode_status": result_status, "geocode_source": result.get("geocode_source"), "geocode_confidence": result.get("geocode_confidence"), "display_name": result.get("display_name"), "normalized_address": result.get("normalized_address")})
            stats["processed"] += 1
            if result_status == "failed":
                stats["errors"] += 1
            progress.progress((i + 1) / len(df))
            status.write(stats)

        # remove loader
        loader_ph.empty()
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
