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
:root{--bg:#050a12;--line:rgba(255,255,255,.14);--cyan:#7df9ff;--violet:#9b5cff;--text:#f3f7fb;--muted:#aab4c2;}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 50% -18%,rgba(125,249,255,.20),transparent 34%),radial-gradient(circle at 15% 28%,rgba(155,92,255,.10),transparent 34%),linear-gradient(180deg,#080e18,#03070d 72%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}body:before{content:"";position:fixed;inset:0;background:linear-gradient(90deg,#ff3b30,#ffd60a,#32d74b,#64d2ff,#bf5af2);height:2px;z-index:10;box-shadow:0 0 22px rgba(125,249,255,.55)}
.glass{background:linear-gradient(135deg,rgba(255,255,255,.105),rgba(255,255,255,.036));border:1px solid var(--line);box-shadow:0 24px 70px rgba(0,0,0,.48),inset 0 1px 0 rgba(255,255,255,.12);backdrop-filter:blur(24px) saturate(160%);-webkit-backdrop-filter:blur(24px) saturate(160%);border-radius:26px}.logoBar{display:flex;justify-content:center;align-items:center;padding:34px 20px 18px}.logoImg{width:min(360px,62vw);height:auto;object-fit:contain;filter:drop-shadow(0 0 24px rgba(125,249,255,.38))}
.hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;overflow:hidden;padding-bottom:34px}.heroTitle{width:min(980px,72vw);margin:18px auto 16px;padding:24px 30px;text-align:center;font-size:clamp(28px,3vw,42px);font-weight:900;letter-spacing:.035em}.mapShell{position:relative;width:min(1120px,82vw);height:clamp(430px,54vh,610px);overflow:hidden}.mapHelp{position:absolute;top:18px;left:20px;z-index:4;color:var(--muted);font-size:14px}.country{fill:#101b2c;stroke:rgba(255,255,255,.07);stroke-width:.45;transition:.18s}.country:hover{fill:#11b8b0;filter:drop-shadow(0 0 12px rgba(125,249,255,.60));cursor:pointer}.mapTooltip{position:absolute;display:none;z-index:6;padding:9px 12px;border-radius:12px;background:rgba(0,0,0,.72);border:1px solid rgba(125,249,255,.30);color:#fff;pointer-events:none}
.appLayout{display:grid;grid-template-columns:300px minmax(0,1fr);gap:32px;width:min(1120px,86vw);margin:34px auto 56px}.side{padding:22px;height:max-content;position:sticky;top:22px}.mainPanel{padding:38px;min-height:600px}.field{margin:16px 0}.field label{display:block;color:var(--muted);font-size:13px;margin-bottom:8px}.field input,.field select{width:100%;background:rgba(3,7,13,.72);border:1px solid rgba(255,255,255,.12);color:var(--text);border-radius:12px;padding:13px}.primaryBtn,.secondaryBtn{border:1px solid rgba(125,249,255,.35);background:linear-gradient(135deg,rgba(125,249,255,.22),rgba(155,92,255,.13));color:#fff;border-radius:14px;padding:13px 18px;font-weight:800;cursor:pointer;box-shadow:0 0 28px rgba(125,249,255,.18);display:inline-block;text-decoration:none}.secondaryBtn{background:rgba(255,255,255,.06)}
.uploadZone{display:flex;align-items:center;gap:18px;border:1px dashed rgba(125,249,255,.48);border-radius:22px;padding:28px;background:linear-gradient(135deg,rgba(125,249,255,.10),rgba(255,255,255,.045));box-shadow:inset 0 1px 0 rgba(255,255,255,.10);cursor:pointer;transition:.2s}.uploadZone:hover{border-color:rgba(125,249,255,.80);box-shadow:0 0 30px rgba(125,249,255,.12),inset 0 1px 0 rgba(255,255,255,.14);transform:translateY(-1px)}.uploadIcon{width:52px;height:52px;border-radius:18px;border:1px solid rgba(125,249,255,.35);display:flex;align-items:center;justify-content:center;font-size:28px;color:var(--cyan);background:rgba(0,0,0,.22)}.uploadText strong{display:block;font-size:18px}.uploadText small{display:block;color:var(--muted);margin-top:6px}.hiddenFile{display:none}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:18px 0}.check{padding:10px;border-radius:12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.09)}table{width:100%;border-collapse:collapse;margin-top:18px;font-size:13px}th,td{padding:10px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left;vertical-align:top}th{color:var(--cyan)}.hidden{display:none!important}.overlay{position:fixed;inset:0;z-index:20;background:rgba(1,4,9,.76);backdrop-filter:blur(12px);display:none;align-items:center;justify-content:center}.loaderCard{width:min(720px,90vw);padding:24px;text-align:center}.progressTrack{width:100%;height:13px;border-radius:99px;background:rgba(255,255,255,.1);overflow:hidden;margin:18px 0}.progressFill{height:100%;width:0;background:linear-gradient(90deg,var(--cyan),var(--violet));box-shadow:0 0 18px rgba(125,249,255,.7);transition:width .25s}.error{color:#ffb4b4;background:rgba(255,59,48,.12);border:1px solid rgba(255,59,48,.35);padding:12px;border-radius:12px;margin-top:12px}.success{color:#b8ffd0;background:rgba(50,215,75,.12);border:1px solid rgba(50,215,75,.35);padding:12px;border-radius:12px;margin-top:12px}@media(max-width:900px){.appLayout{grid-template-columns:1fr}.side{position:relative}.heroTitle{width:90vw}.mapShell{width:92vw}.mainPanel{padding:24px}}
</style>
"""

LOADER_SCRIPT = """
<script src='https://cdnjs.cloudflare.com/ajax/libs/three.js/r134/three.min.js'></script>
<script>function mountGlobe(){const holder=document.getElementById('globe');holder.innerHTML='';const canvas=document.createElement('canvas');canvas.style.width='100%';canvas.style.height='360px';holder.appendChild(canvas);const renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});const scene=new THREE.Scene();const camera=new THREE.PerspectiveCamera(42,1,0.1,1000);camera.position.z=3.6;const geo=new THREE.IcosahedronGeometry(1.05,5);const mat=new THREE.MeshStandardMaterial({color:0x071226,emissive:0x5eead4,emissiveIntensity:.22,metalness:.55,roughness:.18,transparent:true,opacity:.96});const mesh=new THREE.Mesh(geo,mat);scene.add(mesh);const wire=new THREE.LineSegments(new THREE.WireframeGeometry(geo),new THREE.LineBasicMaterial({color:0x9b5cff,transparent:true,opacity:.42}));scene.add(wire);const p=new THREE.PointLight(0x7df9ff,1.7);p.position.set(4,4,4);scene.add(p);scene.add(new THREE.AmbientLight(0x333333));function resize(){const w=holder.clientWidth||620,h=360;renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix()}resize();function render(){mesh.rotation.y+=.008;mesh.rotation.x+=.002;wire.rotation.y+=.007;renderer.render(scene,camera);requestAnimationFrame(render)}render();}</script>
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


def logo_html() -> str:
    return '<img class="logoImg" src="/logo.png" alt="Occu-Med logo">'


def page_shell(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>{html.escape(title)}</title>{STYLE}</head><body>{body}</body></html>"""


@app.get("/logo.png")
def logo_png():
    return send_from_directory(".", LOGO_FILENAME)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "runtime": "flask", "database_required": True})


@app.get("/")
def landing():
    body = f"""
    <section class='hero'><div class='logoBar'>{logo_html()}</div><div class='heroTitle glass'>Select a country to geocode addresses in</div><div class='mapShell glass'><div class='mapHelp'>Click a country to open the geocoder for that country.</div><div id='tooltip' class='mapTooltip'></div><svg id='worldMap' width='100%' height='100%'></svg></div></section>
    <script src='https://d3js.org/d3.v7.min.js'></script>
    <script>
    const svg=d3.select('#worldMap');const shell=document.querySelector('.mapShell');const tip=document.getElementById('tooltip');
    async function draw(){{const width=shell.clientWidth;const height=shell.clientHeight;svg.attr('viewBox',`0 0 ${{width}} ${{height}}`);const projection=d3.geoMercator().scale(width/6.25).translate([width/2,height/1.55]);const path=d3.geoPath().projection(projection);const world=await fetch('https://raw.githubusercontent.com/holtzy/D3-graph-gallery/master/DATA/world.geojson').then(r=>r.json());svg.selectAll('path').data(world.features).join('path').attr('class','country').attr('d',path).on('mousemove',function(event,d){{const name=d.properties.name||d.properties.ADMIN||d.id;tip.style.display='block';tip.textContent=name;tip.style.left=(event.offsetX+14)+'px';tip.style.top=(event.offsetY+14)+'px';}}).on('mouseleave',()=>tip.style.display='none').on('click',function(event,d){{const name=d.properties.name||d.properties.ADMIN||d.id;window.location.assign('/app?country='+encodeURIComponent(name));}});}}
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
    <div class='logoBar'>{logo_html()}</div><main class='appLayout'><aside class='side glass'><h2>Settings</h2><div class='field'><label>Nominatim User-Agent / contact</label><input value='{user_agent}' disabled></div><div class='field'><label>Nominatim URL</label><input value='{nominatim_url}' disabled></div><div class='field'><label>Country selected from landing map</label><input id='country' value='{country}'></div><div class='field'><label>Delay between new lookups</label><input id='delay' type='number' step='.25' value='{delay}'></div><div class='field'><label>Max new external lookups this run</label><input id='maxExternal' type='number' step='50' value='500'></div><div class='field'><label><input id='cacheOnly' type='checkbox'> Cache-only mode</label></div><button class='secondaryBtn' onclick="window.location.assign('/')">Back to world map</button></aside><section class='mainPanel glass'><h1>Global Address Geocoder</h1><p>Upload an Excel or CSV file, select address columns, preview, then geocode using the shared Neon cache.</p><label class='uploadZone' for='file'><span class='uploadIcon'>↑</span><span class='uploadText'><strong id='fileTitle'>Drag and drop your Excel or CSV file here</strong><small id='fileHelp'>or click anywhere in this glass panel to browse</small></span><input id='file' class='hiddenFile' type='file' accept='.xlsx,.xlsm,.xls,.csv'></label><div id='columnBox' class='hidden'><h3>1. Select address columns</h3><div id='cols' class='cols'></div><h3>2. Preview</h3><div id='preview'></div><div class='field'><label>Download format</label><select id='format'><option value='xlsx'>Excel</option><option value='csv'>CSV</option></select></div><button id='runBtn' class='primaryBtn'>Run geocoding</button></div><div id='message'></div><div id='results'></div></section></main><div id='overlay' class='overlay'><div class='loaderCard glass'><h2>Luminous geocoding in progress</h2><div id='globe'></div><div class='progressTrack'><div id='progressFill' class='progressFill'></div></div><div id='progressText'>Starting...</div></div></div>{LOADER_SCRIPT}<script>{APP_SCRIPT}</script>
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
