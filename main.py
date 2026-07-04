from __future__ import annotations

import html
import json
import os

from flask import Flask, Response, jsonify, request, send_from_directory

from geocode_core import (
    GEOCODER_USER_AGENT,
    NOMINATIM_BASE_URL,
    NOMINATIM_DELAY_SECONDS,
    dataframe_payload,
    default_columns,
    process_rows,
    read_uploaded_file,
    safe_records,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
LOGO_FILENAME = "OM-logo-for landing page" + ".png"

STYLE = """
<style>
:root{--cyan:#7df9ff;--violet:#9b5cff;--text:#f3f7fb;--muted:#aab4c2;--line:rgba(255,255,255,.16);--line2:rgba(255,255,255,.075);--c1:#161638;--c2:#1B435E;--c3:#38667E;--c4:#563457;--c5:#3A2B50}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:#03070d;color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow-x:hidden}body{background:radial-gradient(circle at 52% -14%,rgba(56,102,126,.38),transparent 36%),radial-gradient(circle at 13% 25%,rgba(86,52,87,.28),transparent 32%),linear-gradient(180deg,var(--c1) 0%,#07101b 46%,#03070d 100%)}
.bgfx{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}.ribbon{position:absolute;inset:-30% -12%;opacity:.58;filter:blur(34px);mix-blend-mode:screen;background:linear-gradient(110deg,transparent 20%,rgba(56,102,126,.17) 39%,rgba(86,52,87,.12) 48%,rgba(255,255,255,.025) 55%,transparent 72%);animation:sweep 18s ease-in-out infinite alternate}.ribbon.two{opacity:.34;animation-duration:26s;animation-direction:alternate-reverse;background:linear-gradient(250deg,transparent 25%,rgba(27,67,94,.16) 42%,rgba(58,43,80,.11) 55%,transparent 70%)}@keyframes sweep{0%{transform:translate(-8%,-4%) rotate(-8deg) scale(1.02)}50%{transform:translate(4%,2%) rotate(5deg) scale(1.08)}100%{transform:translate(10%,-2%) rotate(12deg) scale(1.03)}}
.logoBar{position:relative;z-index:3;display:flex;justify-content:center;align-items:center;padding:34px 20px 14px}.logoImg{width:min(360px,62vw);height:auto;object-fit:contain;filter:drop-shadow(0 0 24px rgba(56,102,126,.38)) drop-shadow(0 0 12px rgba(255,255,255,.16))}
.card{position:relative;border:1px solid var(--line);border-radius:28px;background:linear-gradient(135deg,rgba(255,255,255,.105),rgba(255,255,255,.036));box-shadow:0 24px 70px rgba(0,0,0,.48),inset 0 1px 0 rgba(255,255,255,.13);backdrop-filter:blur(24px) saturate(160%);-webkit-backdrop-filter:blur(24px) saturate(160%)}.card:after{content:"";position:absolute;inset:10px;border:1px solid var(--line2);border-radius:20px;pointer-events:none;box-shadow:inset 0 0 0 1px rgba(0,0,0,.18)}
.hero{position:relative;z-index:1;height:100vh;min-height:680px;overflow:hidden}.hero .logoBar{position:absolute;left:0;right:0;top:34px;z-index:5;padding:0}.mapShell{position:absolute;inset:0;overflow:hidden}.country{fill:#0f1c2e;stroke:rgba(255,255,255,.07);stroke-width:.45;transition:.18s}.country:hover{fill:#38667E;filter:drop-shadow(0 0 14px rgba(125,249,255,.64));cursor:pointer}.mapTip{position:absolute;display:none;z-index:6;padding:9px 12px;border-radius:12px;background:rgba(0,0,0,.72);border:1px solid rgba(125,249,255,.30);color:white;pointer-events:none}.mapShade{position:absolute;inset:0;z-index:2;pointer-events:none;background:radial-gradient(circle at 50% 42%,transparent 46%,rgba(3,7,13,.18) 68%,rgba(3,7,13,.65) 100%)}
.appLayout{position:relative;z-index:2;display:grid;grid-template-columns:300px minmax(0,1fr);gap:32px;width:min(1120px,86vw);margin:34px auto 56px}.side{padding:28px;height:max-content;position:sticky;top:22px}.mainPanel{padding:46px;min-height:600px}.field{margin:16px 0}.field label{display:block;color:var(--muted);font-size:13px;margin-bottom:8px}.field input,.field select{width:100%;background:rgba(3,7,13,.72);border:1px solid rgba(255,255,255,.12);color:var(--text);border-radius:12px;padding:13px}.primaryBtn,.secondaryBtn{border:1px solid rgba(125,249,255,.35);background:linear-gradient(135deg,rgba(56,102,126,.55),rgba(86,52,87,.28));color:white;border-radius:14px;padding:13px 18px;font-weight:800;cursor:pointer;box-shadow:0 0 28px rgba(56,102,126,.22);display:inline-block;text-decoration:none}.secondaryBtn{background:rgba(255,255,255,.06)}
.uploadZone{position:relative;display:flex;align-items:center;gap:18px;border:1px dashed rgba(125,249,255,.50);border-radius:22px;padding:28px;background:linear-gradient(135deg,rgba(56,102,126,.18),rgba(86,52,87,.10));box-shadow:inset 0 1px 0 rgba(255,255,255,.10);cursor:pointer;transition:.2s}.uploadZone:after{content:"";position:absolute;inset:8px;border:1px solid rgba(255,255,255,.07);border-radius:15px;pointer-events:none}.uploadZone:hover{border-color:rgba(125,249,255,.84);box-shadow:0 0 30px rgba(56,102,126,.20),inset 0 1px 0 rgba(255,255,255,.14);transform:translateY(-1px)}.uploadIcon{width:52px;height:52px;border-radius:18px;border:1px solid rgba(125,249,255,.35);display:flex;align-items:center;justify-content:center;font-size:28px;color:var(--cyan);background:rgba(0,0,0,.22)}.uploadText strong{display:block;font-size:18px}.uploadText small{display:block;color:var(--muted);margin-top:6px}.hiddenFile{display:none}.hidden{display:none!important}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:18px 0}.check{padding:10px;border-radius:12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.09)}table{width:100%;border-collapse:collapse;margin-top:18px;font-size:13px}th,td{padding:10px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left;vertical-align:top}th{color:var(--cyan)}.overlay{position:fixed;inset:0;z-index:20;background:rgba(1,4,9,.76);backdrop-filter:blur(12px);display:none;align-items:center;justify-content:center}.loaderCard{width:min(720px,90vw);padding:30px;text-align:center}.progressTrack{width:100%;height:13px;border-radius:99px;background:rgba(255,255,255,.1);overflow:hidden;margin:18px 0}.progressFill{height:100%;width:0;background:linear-gradient(90deg,#38667E,#563457);box-shadow:0 0 18px rgba(125,249,255,.7);transition:width .25s}.error{color:#ffb4b4;background:rgba(255,59,48,.12);border:1px solid rgba(255,59,48,.35);padding:12px;border-radius:12px;margin-top:12px}.success{color:#b8ffd0;background:rgba(50,215,75,.12);border:1px solid rgba(50,215,75,.35);padding:12px;border-radius:12px;margin-top:12px}@media(max-width:900px){.hero{min-height:620px}.appLayout{grid-template-columns:1fr}.side{position:relative}.mainPanel{padding:28px}}
</style>
"""

LOADER_SCRIPT = """
<script src='https://cdnjs.cloudflare.com/ajax/libs/three.js/r134/three.min.js'></script>
<script>function mountGlobe(){const holder=document.getElementById('globe');holder.innerHTML='';const canvas=document.createElement('canvas');canvas.style.width='100%';canvas.style.height='360px';holder.appendChild(canvas);const renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});const scene=new THREE.Scene();const camera=new THREE.PerspectiveCamera(42,1,0.1,1000);camera.position.z=3.6;const geo=new THREE.IcosahedronGeometry(1.05,5);const mat=new THREE.MeshStandardMaterial({color:0x071226,emissive:0x38667e,emissiveIntensity:.28,metalness:.55,roughness:.18,transparent:true,opacity:.96});const mesh=new THREE.Mesh(geo,mat);scene.add(mesh);const wire=new THREE.LineSegments(new THREE.WireframeGeometry(geo),new THREE.LineBasicMaterial({color:0x563457,transparent:true,opacity:.5}));scene.add(wire);scene.add(new THREE.PointLight(0x7df9ff,1.7));scene.add(new THREE.AmbientLight(0x333333));function resize(){const w=holder.clientWidth||620,h=360;renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix()}resize();function render(){mesh.rotation.y+=.008;mesh.rotation.x+=.002;wire.rotation.y+=.007;renderer.render(scene,camera);requestAnimationFrame(render)}render();}</script>
"""

APP_SCRIPT = r"""
let chosenFile=null;const fileInput=document.getElementById('file');const msg=document.getElementById('message');const columnBox=document.getElementById('columnBox');const colsBox=document.getElementById('cols');const previewBox=document.getElementById('preview');const resultsBox=document.getElementById('results');const fileTitle=document.getElementById('fileTitle');const fileHelp=document.getElementById('fileHelp');
function esc(value){return String(value??'').replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));}
function table(rows){if(!rows||!rows.length)return'<p>No rows to preview.</p>';const keys=Object.keys(rows[0]);return'<table><thead><tr>'+keys.map(c=>`<th>${esc(c)}</th>`).join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+keys.map(c=>`<td>${esc(r[c])}</td>`).join('')+'</tr>').join('')+'</tbody></table>';}
function showError(text){msg.innerHTML=`<div class='error'>${esc(text)}</div>`}function showSuccess(text){msg.innerHTML=`<div class='success'>${esc(text)}</div>`}
function addColumnCheckbox(name,checked){const label=document.createElement('label');label.className='check';const input=document.createElement('input');input.type='checkbox';input.name='address_col';input.value=name;input.checked=checked;label.appendChild(input);label.appendChild(document.createTextNode(' '+name));colsBox.appendChild(label);}
fileInput.addEventListener('change',async()=>{chosenFile=fileInput.files[0];if(!chosenFile)return;fileTitle.textContent=chosenFile.name;fileHelp.textContent='File selected. Choose address columns below.';msg.innerHTML='';resultsBox.innerHTML='';columnBox.classList.add('hidden');colsBox.innerHTML='';previewBox.innerHTML='';const fd=new FormData();fd.append('file',chosenFile);const res=await fetch('/api/columns',{method:'POST',body:fd});const data=await res.json();if(!res.ok){showError(data.error||'Could not read file.');return;}data.columns.forEach(c=>addColumnCheckbox(c,(data.default_columns||[]).includes(c)));previewBox.innerHTML=table(data.preview);columnBox.classList.remove('hidden');});
document.getElementById('runBtn')?.addEventListener('click',async()=>{const selected=[...document.querySelectorAll("input[name='address_col']:checked")].map(i=>i.value);if(!chosenFile){showError('Upload a file first.');return}if(!selected.length){showError('Select at least one address column.');return}const overlay=document.getElementById('overlay');const fill=document.getElementById('progressFill');const txt=document.getElementById('progressText');overlay.style.display='flex';mountGlobe();fill.style.width='0%';txt.textContent='Starting geocoding...';const fd=new FormData();fd.append('file',chosenFile);selected.forEach(c=>fd.append('address_cols',c));fd.append('country',document.getElementById('country').value);fd.append('delay',document.getElementById('delay').value);fd.append('max_external',document.getElementById('maxExternal').value);fd.append('cache_only',document.getElementById('cacheOnly').checked?'true':'false');fd.append('format',document.getElementById('format').value);try{const res=await fetch('/api/geocode',{method:'POST',body:fd});if(!res.ok){const data=await res.json();overlay.style.display='none';showError(data.error||'Geocoding failed.');return;}const reader=res.body.getReader();const decoder=new TextDecoder();let buffer='';let finalData=null;while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});const lines=buffer.split('\n');buffer=lines.pop();for(const line of lines){if(!line.trim())continue;const data=JSON.parse(line);if(data.type==='progress'){const pct=data.total?Math.round((data.stats.processed/data.total)*100):0;fill.style.width=pct+'%';txt.textContent=`${pct}% - processed ${data.stats.processed}/${data.total} | cache hits ${data.stats.cache_hits} | external ${data.stats.external}`;}else if(data.type==='complete'){finalData=data;}else if(data.type==='error'){throw new Error(data.error);}}}if(finalData){fill.style.width='100%';txt.textContent='100% - complete';setTimeout(()=>overlay.style.display='none',450);showSuccess(`Complete. Processed ${finalData.stats.processed}. Cache hits ${finalData.stats.cache_hits}. External lookups ${finalData.stats.external}. Errors ${finalData.stats.errors}.`);const href=`data:${finalData.mime};base64,${finalData.file_b64}`;resultsBox.innerHTML=`<p><a class='primaryBtn' download='${esc(finalData.filename)}' href='${href}'>Download ${esc(finalData.filename)}</a></p>`+table(finalData.rows);}else{overlay.style.display='none';showError('Geocoding finished without a final result.');}}catch(e){overlay.style.display='none';showError(e.message||'Request failed.');}});
"""


def lights() -> str:
    return "<div class='bgfx'><div class='ribbon'></div><div class='ribbon two'></div></div>"


def logo_html() -> str:
    return '<img class="logoImg" src="/logo.png" alt="Occu-Med logo">'


def page_shell(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>{html.escape(title)}</title>{STYLE}</head><body>{lights()}{body}</body></html>"""


@app.get("/logo.png")
def logo_png():
    return send_from_directory(".", LOGO_FILENAME)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "runtime": "flask", "database_required": True})


@app.get("/")
def landing():
    body = f"""
    <section class='hero'><div class='logoBar'>{logo_html()}</div><div class='mapShell'><div id='tooltip' class='mapTip'></div><svg id='worldMap' width='100%' height='100%'></svg><div class='mapShade'></div></div></section>
    <script src='https://d3js.org/d3.v7.min.js'></script>
    <script>
    const svg=d3.select('#worldMap');const shell=document.querySelector('.mapShell');const tip=document.getElementById('tooltip');
    async function draw(){{const width=shell.clientWidth;const height=shell.clientHeight;svg.attr('viewBox',`0 0 ${{width}} ${{height}}`);const world=await fetch('https://raw.githubusercontent.com/holtzy/D3-graph-gallery/master/DATA/world.geojson').then(r=>r.json());const features=world.features.filter(f=>((f.properties.name||f.properties.ADMIN||'')!=='Antarctica'));const fitted={{type:'FeatureCollection',features:features}};const projection=d3.geoMercator().fitExtent([[6,86],[width-6,height-10]],fitted);const path=d3.geoPath().projection(projection);svg.selectAll('path').data(features).join('path').attr('class','country').attr('d',path).on('mousemove',function(event,d){{const name=d.properties.name||d.properties.ADMIN||d.id;tip.style.display='block';tip.textContent=name;tip.style.left=(event.offsetX+14)+'px';tip.style.top=(event.offsetY+14)+'px';}}).on('mouseleave',()=>tip.style.display='none').on('click',function(event,d){{const name=d.properties.name||d.properties.ADMIN||d.id;window.location.assign('/app?country='+encodeURIComponent(name));}});}}
    draw();window.addEventListener('resize',()=>{{svg.selectAll('*').remove();draw();}});
    </script>
    """
    return page_shell("Occu-Med Global Address Geocoder", body)


@app.get("/app")
def geocoder_app():
    country = html.escape(request.args.get("country", "").strip(), quote=True)
    user_agent = html.escape(GEOCODER_USER_AGENT, quote=True)
    nominatim_url = html.escape(NOMINATIM_BASE_URL, quote=True)
    delay = html.escape(str(NOMINATIM_DELAY_SECONDS), quote=True)
    body = f"""
    <div class='logoBar'>{logo_html()}</div><main class='appLayout'><aside class='side card'><h2>Settings</h2><div class='field'><label>Nominatim User-Agent / contact</label><input value='{user_agent}' disabled></div><div class='field'><label>Nominatim URL</label><input value='{nominatim_url}' disabled></div><div class='field'><label>Country selected from landing map</label><input id='country' value='{country}'></div><div class='field'><label>Delay between new lookups</label><input id='delay' type='number' step='.25' value='{delay}'></div><div class='field'><label>Max new external lookups this run</label><input id='maxExternal' type='number' step='50' value='500'></div><div class='field'><label><input id='cacheOnly' type='checkbox'> Cache-only mode</label></div><button class='secondaryBtn' onclick="window.location.assign('/')">Back to world map</button></aside><section class='mainPanel card'><h1>Global Address Geocoder</h1><p>Upload an Excel or CSV file, select address columns, preview, then geocode using the shared Neon cache.</p><label class='uploadZone' for='file'><span class='uploadIcon'>↑</span><span class='uploadText'><strong id='fileTitle'>Drag and drop your Excel or CSV file here</strong><small id='fileHelp'>or click anywhere in this glass panel to browse</small></span><input id='file' class='hiddenFile' type='file' accept='.xlsx,.xlsm,.xls,.csv'></label><div id='columnBox' class='hidden'><h3>1. Select address columns</h3><div id='cols' class='cols'></div><h3>2. Preview</h3><div id='preview'></div><div class='field'><label>Download format</label><select id='format'><option value='xlsx'>Excel</option><option value='csv'>CSV</option></select></div><button id='runBtn' class='primaryBtn'>Run geocoding</button></div><div id='message'></div><div id='results'></div></section></main><div id='overlay' class='overlay'><div class='loaderCard card'><h2>Luminous geocoding in progress</h2><div id='globe'></div><div class='progressTrack'><div id='progressFill' class='progressFill'></div></div><div id='progressText'>Starting...</div></div></div>{LOADER_SCRIPT}<script>{APP_SCRIPT}</script>
    """
    return page_shell("Occu-Med Geocoder App", body)


@app.post("/api/columns")
def api_columns():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    try:
        df = read_uploaded_file(request.files["file"])
    except Exception as exc:
        return jsonify({"error": f"Could not read file: {exc}"}), 400
    cols = [str(col) for col in df.columns]
    return jsonify({"columns": cols, "default_columns": default_columns(cols), "preview": safe_records(df, limit=8)})


@app.post("/api/geocode")
def api_geocode():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    cols = request.form.getlist("address_cols")
    if not cols:
        return jsonify({"error": "Select at least one address column."}), 400
    try:
        df = read_uploaded_file(request.files["file"])
    except Exception as exc:
        return jsonify({"error": f"Could not read file: {exc}"}), 400

    country = request.form.get("country", "").strip()
    delay = float(request.form.get("delay", NOMINATIM_DELAY_SECONDS))
    max_external = int(float(request.form.get("max_external", 500)))
    cache_only = request.form.get("cache_only") == "true"
    output_format = request.form.get("format", "xlsx")

    def stream():
        try:
            for event in process_rows(df, cols, country, delay, max_external, cache_only):
                if event["type"] == "progress":
                    yield json.dumps(event) + "\n"
                else:
                    result_df = event.pop("result_df")
                    file_b64, filename, mime = dataframe_payload(result_df, output_format)
                    event.update({"rows": safe_records(result_df, limit=50), "file_b64": file_b64, "filename": filename, "mime": mime})
                    yield json.dumps(event) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "error": str(exc)}) + "\n"

    return Response(stream(), mimetype="application/x-ndjson")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
