from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import time
from typing import Any

import pandas as pd
import psycopg
import pycountry
import requests
from flask import Flask, jsonify, request, send_from_directory
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
NOMINATIM_BASE_URL = os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org/search").strip()
GEOCODER_USER_AGENT = os.getenv("GEOCODER_USER_AGENT", "OccuMedAddressGeocoder/1.0").strip()
NOMINATIM_DELAY_SECONDS = float(os.getenv("NOMINATIM_DELAY_SECONDS", "1.0"))


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing in Render environment variables.")
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
    if value is None or pd.isna(value):
        return ""
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
    for field in ("alpha_2", "alpha_3"):
        try:
            match = pycountry.countries.get(**{field: value.upper()})
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


def address_hash(normalized: str, code: str) -> str:
    return hashlib.sha256(f"{normalized}|{code}".encode("utf-8")).hexdigest()


def cache_lookup(conn, ahash: str):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM geocode_cache WHERE address_hash=%s", (ahash,))
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
            (ahash,),
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


def nominatim(address: str, code: str):
    params = {"q": address, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    if code:
        params["countrycodes"] = code
    try:
        response = requests.get(
            NOMINATIM_BASE_URL,
            params=params,
            headers={"User-Agent": GEOCODER_USER_AGENT},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
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


def read_uploaded_file(file_storage):
    filename = file_storage.filename.lower()
    if filename.endswith(".csv"):
        return pd.read_csv(file_storage)
    return pd.read_excel(file_storage)


def dataframe_to_xlsx_base64(df: pd.DataFrame) -> str:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="geocoded")
    buffer.seek(0)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


STYLE = """
<style>
:root{--bg:#050a12;--panel:rgba(255,255,255,.07);--panel2:rgba(255,255,255,.11);--line:rgba(255,255,255,.13);--cyan:#7df9ff;--violet:#9b5cff;--text:#f3f7fb;--muted:#aab4c2;}
*{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at 50% -20%,rgba(125,249,255,.16),transparent 36%),linear-gradient(180deg,#080e18,#03070d 70%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh;}
body:before{content:"";position:fixed;inset:0;background:linear-gradient(90deg,#ff3b30,#ffd60a,#32d74b,#64d2ff,#bf5af2);height:2px;z-index:5;box-shadow:0 0 22px rgba(125,249,255,.5)}
.glass{background:linear-gradient(135deg,rgba(255,255,255,.10),rgba(255,255,255,.035));border:1px solid var(--line);box-shadow:0 24px 70px rgba(0,0,0,.48),inset 0 1px 0 rgba(255,255,255,.12);backdrop-filter:blur(22px) saturate(150%);-webkit-backdrop-filter:blur(22px) saturate(150%);border-radius:24px;}
.logoBar{position:relative;z-index:2;display:flex;justify-content:center;align-items:center;padding:46px 20px 22px}.logoText{letter-spacing:.16em;font-size:30px;font-weight:900;color:var(--cyan);text-shadow:0 0 18px rgba(125,249,255,.7)}.logoImg{max-width:240px;max-height:96px;object-fit:contain;filter:drop-shadow(0 0 22px rgba(125,249,255,.45))}
.hero{min-height:100vh;display:flex;flex-direction:column;overflow:hidden}.heroTitle{width:min(1180px,86vw);margin:16px auto 18px;padding:30px;text-align:center;font-size:34px;font-weight:900;letter-spacing:.04em}.mapShell{position:relative;flex:1;width:min(1260px,88vw);margin:0 auto 40px;min-height:560px;overflow:hidden}.mapHelp{position:absolute;top:18px;left:18px;z-index:4;color:var(--muted);font-size:14px}.country{fill:#111c2d;stroke:rgba(255,255,255,.07);stroke-width:.45;transition:.18s}.country:hover{fill:#11b8b0;filter:drop-shadow(0 0 10px rgba(125,249,255,.55));cursor:pointer}.mapTooltip{position:absolute;display:none;z-index:6;padding:9px 12px;border-radius:12px;background:rgba(0,0,0,.72);border:1px solid rgba(125,249,255,.3);color:#fff;pointer-events:none}
.appLayout{display:grid;grid-template-columns:300px 1fr;gap:34px;width:min(1220px,90vw);margin:44px auto}.side{padding:22px;height:max-content;position:sticky;top:22px}.mainPanel{padding:34px 42px;min-height:620px}.field{margin:16px 0}.field label{display:block;color:var(--muted);font-size:13px;margin-bottom:8px}.field input,.field select{width:100%;background:rgba(3,7,13,.72);border:1px solid rgba(255,255,255,.12);color:var(--text);border-radius:12px;padding:13px}.heroBtn,.primaryBtn,.secondaryBtn{border:1px solid rgba(125,249,255,.35);background:linear-gradient(135deg,rgba(125,249,255,.22),rgba(155,92,255,.13));color:#fff;border-radius:14px;padding:13px 18px;font-weight:800;cursor:pointer;box-shadow:0 0 28px rgba(125,249,255,.18)}.secondaryBtn{background:rgba(255,255,255,.06)}.drop{border:1px dashed rgba(125,249,255,.38);border-radius:18px;padding:22px;background:rgba(255,255,255,.055)}.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:18px 0}.check{padding:10px;border-radius:12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.09)}.status{margin-top:16px;color:var(--muted)}table{width:100%;border-collapse:collapse;margin-top:18px;font-size:13px}th,td{padding:10px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}th{color:var(--cyan)}.hidden{display:none!important}
.overlay{position:fixed;inset:0;z-index:20;background:rgba(1,4,9,.76);backdrop-filter:blur(12px);display:none;align-items:center;justify-content:center}.loaderCard{width:min(720px,90vw);padding:24px;text-align:center}.progressTrack{width:100%;height:13px;border-radius:99px;background:rgba(255,255,255,.1);overflow:hidden;margin:18px 0}.progressFill{height:100%;width:3%;background:linear-gradient(90deg,var(--cyan),var(--violet));box-shadow:0 0 18px rgba(125,249,255,.7);transition:width .25s}.made{color:rgba(255,255,255,.36);font-size:12px;margin-top:24px}.error{color:#ffb4b4;background:rgba(255,59,48,.12);border:1px solid rgba(255,59,48,.35);padding:12px;border-radius:12px;margin-top:12px}.success{color:#b8ffd0;background:rgba(50,215,75,.12);border:1px solid rgba(50,215,75,.35);padding:12px;border-radius:12px;margin-top:12px}@media(max-width:900px){.appLayout{grid-template-columns:1fr}.side{position:relative}.heroTitle{font-size:26px}.mapShell{width:94vw}}
</style>
"""


def logo_html() -> str:
    if os.path.exists(os.path.join(app.root_path, "assets", "logo.png")):
        return '<img class="logoImg" src="/assets/logo.png" alt="Occu-Med logo">'
    return '<div class="logoText">OCCU-MED</div>'


def page_shell(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>{title}</title>{STYLE}</head><body>{body}</body></html>"""


@app.get("/assets/<path:filename>")
def assets(filename: str):
    return send_from_directory("assets", filename)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "runtime": "flask", "streamlit": False})


@app.get("/")
def landing():
    body = f"""
    <section class='hero'>
      <div class='logoBar'>{logo_html()}</div>
      <div class='heroTitle glass'>Select a country to geocode addresses in</div>
      <div class='mapShell glass'>
        <div class='mapHelp'>Click a country to open the geocoder for that country.</div>
        <div id='tooltip' class='mapTooltip'></div>
        <svg id='worldMap' width='100%' height='100%'></svg>
      </div>
    </section>
    <script src='https://d3js.org/d3.v7.min.js'></script>
    <script>
      const svg = d3.select('#worldMap');
      const shell = document.querySelector('.mapShell');
      const tip = document.getElementById('tooltip');
      async function draw() {{
        const width = shell.clientWidth;
        const height = Math.max(shell.clientHeight, 560);
        svg.attr('viewBox', `0 0 ${{width}} ${{height}}`);
        const projection = d3.geoMercator().scale(width / 6.45).translate([width / 2, height / 1.58]);
        const path = d3.geoPath().projection(projection);
        const world = await fetch('https://raw.githubusercontent.com/holtzy/D3-graph-gallery/master/DATA/world.geojson').then(r => r.json());
        svg.selectAll('path').data(world.features).join('path')
          .attr('class','country')
          .attr('d', path)
          .on('mousemove', function(event, d) {{
            const name = d.properties.name || d.properties.ADMIN || d.id;
            tip.style.display = 'block'; tip.textContent = name;
            tip.style.left = (event.offsetX + 14) + 'px'; tip.style.top = (event.offsetY + 14) + 'px';
          }})
          .on('mouseleave', () => tip.style.display = 'none')
          .on('click', function(event, d) {{
            const name = d.properties.name || d.properties.ADMIN || d.id;
            window.location.assign('/app?country=' + encodeURIComponent(name));
          }});
      }}
      draw(); window.addEventListener('resize', () => {{ svg.selectAll('*').remove(); draw(); }});
    </script>
    """
    return page_shell("Occu-Med Global Address Geocoder", body)


@app.get("/app")
def geocoder_app():
    country = request.args.get("country", "").strip()
    body = f"""
    <div class='logoBar'>{logo_html()}</div>
    <main class='appLayout'>
      <aside class='side glass'>
        <h2>Settings</h2>
        <div class='field'><label>Nominatim User-Agent / contact</label><input id='ua' value='{GEOCODER_USER_AGENT}' disabled></div>
        <div class='field'><label>Nominatim URL</label><input value='{NOMINATIM_BASE_URL}' disabled></div>
        <div class='field'><label>Country selected from landing map</label><input id='country' value='{country}'></div>
        <div class='field'><label>Delay between new lookups</label><input id='delay' type='number' step='.25' value='{NOMINATIM_DELAY_SECONDS}'></div>
        <div class='field'><label>Max new external lookups this run</label><input id='maxExternal' type='number' step='50' value='500'></div>
        <div class='field'><label><input id='cacheOnly' type='checkbox'> Cache-only mode</label></div>
        <button class='secondaryBtn' onclick="window.location.assign('/')">Back to world map</button>
        <div class='made'>No Streamlit. Flask + Neon cache.</div>
      </aside>
      <section class='mainPanel glass'>
        <h1>📍 Free Address Geocoder</h1>
        <p>Upload an Excel or CSV file, select address columns, preview, then geocode using the shared Neon cache.</p>
        <div class='drop'>
          <label><strong>Upload Excel or CSV</strong></label><br><br>
          <input id='file' type='file' accept='.xlsx,.xlsm,.xls,.csv'>
        </div>
        <div id='columnBox' class='hidden'>
          <h3>1. Select address columns</h3>
          <div id='cols' class='cols'></div>
          <h3>2. Preview</h3>
          <div id='preview'></div>
          <div class='field'><label>Download format</label><select id='format'><option value='xlsx'>Excel</option><option value='csv'>CSV</option></select></div>
          <button id='runBtn' class='primaryBtn'>Run geocoding</button>
        </div>
        <div id='message'></div>
        <div id='results'></div>
      </section>
    </main>
    <div id='overlay' class='overlay'>
      <div class='loaderCard glass'>
        <h2>Luminous geocoding in progress</h2>
        <div id='globe'></div>
        <div class='progressTrack'><div id='progressFill' class='progressFill'></div></div>
        <div id='progressText'>Starting…</div>
      </div>
    </div>
    {LOADER_SCRIPT}
    <script>{APP_SCRIPT}</script>
    """
    return page_shell("Occu-Med Geocoder App", body)


@app.post("/api/columns")
def api_columns():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    df = read_uploaded_file(request.files["file"])
    cols = [str(c) for c in df.columns]
    preview = df.head(8).fillna("").astype(str).to_dict(orient="records")
    return jsonify({"columns": cols, "preview": preview})


@app.post("/api/geocode")
def api_geocode():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    cols = request.form.getlist("address_cols")
    if not cols:
        return jsonify({"error": "Select at least one address column."}), 400
    country = request.form.get("country", "").strip()
    delay = float(request.form.get("delay", NOMINATIM_DELAY_SECONDS))
    max_external = int(float(request.form.get("max_external", 500)))
    cache_only = request.form.get("cache_only") == "true"
    output_format = request.form.get("format", "xlsx")

    df = read_uploaded_file(request.files["file"])
    conn = get_conn()
    code = country_code(country)
    out = []
    stats = {"processed": 0, "cache_hits": 0, "cache_misses": 0, "external": 0, "errors": 0}

    for _, row in df.iterrows():
        raw = ", ".join([clean(row.get(col)) for col in cols if clean(row.get(col))] + ([country] if country else []))
        norm = normalize(raw)
        ahash = address_hash(norm, code)
        cached = cache_lookup(conn, ahash)
        if cached:
            result = cached
            result_status = "cache_hit"
            stats["cache_hits"] += 1
        elif cache_only or stats["external"] >= max_external:
            result = {"latitude": None, "longitude": None, "geocode_source": None, "geocode_confidence": None, "display_name": None, "normalized_address": norm}
            result_status = "cache_miss"
            stats["cache_misses"] += 1
        else:
            lat, lon, geocode_status, source, confidence, display_name, payload = nominatim(raw, code)
            save_row = {
                "address_hash": ahash,
                "raw_address": raw,
                "normalized_address": norm,
                "country_name": country,
                "country_code": code,
                "latitude": lat,
                "longitude": lon,
                "geocode_status": geocode_status,
                "geocode_source": source,
                "geocode_confidence": confidence,
                "display_name": display_name,
                "error": None if geocode_status in ("geocoded", "not_found") else str(payload),
                "provider_response_json": payload,
            }
            result = cache_save(conn, save_row)
            result_status = geocode_status
            stats["cache_misses"] += 1
            stats["external"] += 1
            time.sleep(delay)
        if result_status == "failed":
            stats["errors"] += 1
        stats["processed"] += 1
        out.append({
            "latitude": result.get("latitude"),
            "longitude": result.get("longitude"),
            "geocode_status": result_status,
            "geocode_source": result.get("geocode_source"),
            "geocode_confidence": result.get("geocode_confidence"),
            "display_name": result.get("display_name"),
            "normalized_address": result.get("normalized_address"),
        })

    result_df = pd.concat([df.reset_index(drop=True), pd.DataFrame(out)], axis=1)
    preview = result_df.head(50).fillna("").astype(str).to_dict(orient="records")
    if output_format == "csv":
        file_b64 = base64.b64encode(result_df.to_csv(index=False).encode("utf-8")).decode("ascii")
        filename = "geocoded_addresses.csv"
        mime = "text/csv"
    else:
        file_b64 = dataframe_to_xlsx_base64(result_df)
        filename = "geocoded_addresses.xlsx"
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return jsonify({"stats": stats, "rows": preview, "file_b64": file_b64, "filename": filename, "mime": mime})


LOADER_SCRIPT = """
<script src='https://cdnjs.cloudflare.com/ajax/libs/three.js/r134/three.min.js'></script>
<script>
function mountGlobe(){
  const holder=document.getElementById('globe'); holder.innerHTML='';
  const canvas=document.createElement('canvas'); canvas.style.width='100%'; canvas.style.height='360px'; holder.appendChild(canvas);
  const renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});
  const scene=new THREE.Scene(); const camera=new THREE.PerspectiveCamera(42,1,0.1,1000); camera.position.z=3.6;
  const geo=new THREE.IcosahedronGeometry(1.05,5);
  const mat=new THREE.MeshStandardMaterial({color:0x071226,emissive:0x5eead4,emissiveIntensity:.22,metalness:.55,roughness:.18,transparent:true,opacity:.96});
  const mesh=new THREE.Mesh(geo,mat); scene.add(mesh);
  const wire=new THREE.LineSegments(new THREE.WireframeGeometry(geo),new THREE.LineBasicMaterial({color:0x9b5cff,transparent:true,opacity:.42})); scene.add(wire);
  const p=new THREE.PointLight(0x7df9ff,1.7); p.position.set(4,4,4); scene.add(p); scene.add(new THREE.AmbientLight(0x333333));
  function resize(){const w=holder.clientWidth||620,h=360;renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix()} resize();
  function render(){mesh.rotation.y+=.008;mesh.rotation.x+=.002;wire.rotation.y+=.007;renderer.render(scene,camera);requestAnimationFrame(render)} render();
}
</script>
"""


APP_SCRIPT = r"""
let chosenFile=null;
const fileInput=document.getElementById('file');
const msg=document.getElementById('message');
const columnBox=document.getElementById('columnBox');
const colsBox=document.getElementById('cols');
const previewBox=document.getElementById('preview');
const resultsBox=document.getElementById('results');

function table(rows){
  if(!rows || !rows.length) return '<p>No rows to preview.</p>';
  const cols=Object.keys(rows[0]);
  return '<table><thead><tr>'+cols.map(c=>`<th>${c}</th>`).join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>`<td>${r[c]??''}</td>`).join('')+'</tr>').join('')+'</tbody></table>';
}
function showError(text){msg.innerHTML=`<div class='error'>${text}</div>`}
function showSuccess(text){msg.innerHTML=`<div class='success'>${text}</div>`}

fileInput.addEventListener('change', async () => {
  chosenFile=fileInput.files[0]; if(!chosenFile) return;
  msg.innerHTML=''; resultsBox.innerHTML=''; columnBox.classList.add('hidden');
  const fd=new FormData(); fd.append('file', chosenFile);
  const res=await fetch('/api/columns',{method:'POST',body:fd});
  const data=await res.json();
  if(!res.ok){showError(data.error||'Could not read file.'); return;}
  colsBox.innerHTML=data.columns.map(c=>`<label class='check'><input type='checkbox' name='address_col' value='${c}' ${/address|street|city|state|zip|postal|complete/i.test(c)?'checked':''}> ${c}</label>`).join('');
  previewBox.innerHTML=table(data.preview);
  columnBox.classList.remove('hidden');
});

document.getElementById('runBtn')?.addEventListener('click', async () => {
  const selected=[...document.querySelectorAll("input[name='address_col']:checked")].map(i=>i.value);
  if(!chosenFile){showError('Upload a file first.');return} if(!selected.length){showError('Select at least one address column.');return}
  const overlay=document.getElementById('overlay'); const fill=document.getElementById('progressFill'); const txt=document.getElementById('progressText');
  overlay.style.display='flex'; mountGlobe(); let p=5; fill.style.width='5%'; txt.textContent='Checking Neon cache and geocoding misses…';
  const timer=setInterval(()=>{p=Math.min(92,p+Math.random()*7);fill.style.width=p+'%';txt.textContent=Math.round(p)+'% — geocoding and writing cache';},700);
  const fd=new FormData(); fd.append('file',chosenFile); selected.forEach(c=>fd.append('address_cols',c));
  fd.append('country',document.getElementById('country').value); fd.append('delay',document.getElementById('delay').value);
  fd.append('max_external',document.getElementById('maxExternal').value); fd.append('cache_only',document.getElementById('cacheOnly').checked?'true':'false'); fd.append('format',document.getElementById('format').value);
  try{
    const res=await fetch('/api/geocode',{method:'POST',body:fd}); const data=await res.json();
    clearInterval(timer); fill.style.width='100%'; txt.textContent='100% — complete'; setTimeout(()=>overlay.style.display='none',450);
    if(!res.ok){showError(data.error||'Geocoding failed.'); return;}
    showSuccess(`Complete. Processed ${data.stats.processed}. Cache hits ${data.stats.cache_hits}. External lookups ${data.stats.external}. Errors ${data.stats.errors}.`);
    const href=`data:${data.mime};base64,${data.file_b64}`;
    resultsBox.innerHTML=`<p><a class='primaryBtn' download='${data.filename}' href='${href}'>Download ${data.filename}</a></p>`+table(data.rows);
  }catch(e){clearInterval(timer); overlay.style.display='none'; showError(e.message||'Request failed.');}
});
"""


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
