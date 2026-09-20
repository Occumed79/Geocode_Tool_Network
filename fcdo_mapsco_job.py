import os, json, time, threading, urllib.parse, urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg

INPUT_FILES = ["fcdo_geocode_input.jsonl", "fcdo_geocode_input_2.jsonl", "fcdo_geocode_input_3_missing192.jsonl"]
JOB_KEY = "fcdo_fcdoproviders_20260920"
RESULTS_FILE = "fcdo_geocode_results.json"

_state = {"status":"idle","processed":0,"total":0,"matched":0,"city_fallback":0,"unmatched":0,"errors":0}
_lock = threading.Lock()
_thread = None
_query_cache = {}
_query_cache_lock = threading.Lock()
_stop_keepalive = threading.Event()

class _Limiter:
    def __init__(self, n=5, period=1.05):
        self.n=n
        self.period=period
        self.times=deque()
        self.lock=threading.Lock()
    def wait(self):
        while True:
            with self.lock:
                now=time.monotonic()
                while self.times and now-self.times[0] >= self.period:
                    self.times.popleft()
                if len(self.times) < self.n:
                    self.times.append(now)
                    return
                delay=self.period-(now-self.times[0])+0.01
            time.sleep(max(0.02,delay))

_limiter=_Limiter()

def _db():
    url=os.environ.get("DATABASE_URL","").strip()
    if not url:
        raise RuntimeError("DATABASE_URL missing")
    conn=psycopg.connect(url,autocommit=True)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fcdo_mapsco_checkpoint (
                job_key TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                match INTEGER NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (job_key,item_id)
            )
        """)
    return conn

def _load_items():
    items=[]
    for path in INPUT_FILES:
        with open(path,"r",encoding="utf-8") as f:
            items.extend(json.loads(line) for line in f if line.strip())
    items.sort(key=lambda x:x[0])
    return items

def _load_saved(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT item_id,latitude,longitude,match FROM fcdo_mapsco_checkpoint WHERE job_key=%s ORDER BY item_id",(JOB_KEY,))
        return {int(r[0]):[int(r[0]),r[1],r[2],int(r[3])] for r in cur.fetchall()}

def _save(conn,row):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fcdo_mapsco_checkpoint(job_key,item_id,latitude,longitude,match,updated_at)
            VALUES(%s,%s,%s,%s,%s,NOW())
            ON CONFLICT(job_key,item_id) DO UPDATE SET
              latitude=EXCLUDED.latitude,
              longitude=EXCLUDED.longitude,
              match=EXCLUDED.match,
              updated_at=NOW()
        """,(JOB_KEY,row[0],row[1],row[2],row[3]))

def _join(parts):
    return ", ".join(str(x).strip() for x in parts if x is not None and str(x).strip())

def _lookup(q,key):
    with _query_cache_lock:
        if q in _query_cache:
            return _query_cache[q]
    params=urllib.parse.urlencode({"q":q,"api_key":key,"limit":"1","addressdetails":"0","extratags":"0","namedetails":"0"})
    url="https://geocode.maps.co/search?"+params
    last=None
    for attempt in range(5):
        _limiter.wait()
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"OccuMed-FCDO-Geocoder/1.0"})
            with urllib.request.urlopen(req,timeout=30) as r:
                data=json.loads(r.read().decode("utf-8"))
            result=(float(data[0]["lat"]),float(data[0]["lon"])) if data else None
            with _query_cache_lock:
                _query_cache[q]=result
            return result
        except Exception as e:
            last=e
            time.sleep(0.8*(attempt+1))
    raise last

def _one(it,key):
    ident,name,addr,city,region,country=it
    addr_text=str(addr or "").strip()
    city_q=_join([city,region,country])
    address_q=_join([addr,city,region,country])
    name_q=_join([name,city,region,country])
    low=addr_text.lower()
    placeholder=(not addr_text) or ("multiple" in low) or ("see provider" in low) or ("see website" in low)
    tries=[address_q]
    if placeholder:
        tries.append(name_q)
    tries.append(city_q)
    seen=set()
    tries=[q for q in tries if q and not (q in seen or seen.add(q))]
    had_error=False
    for q in tries:
        try:
            got=_lookup(q,key)
        except Exception:
            had_error=True
            continue
        if got:
            if q==city_q:
                mt=2
            elif q==name_q and placeholder:
                mt=1
            else:
                mt=0
            return [ident,got[0],got[1],mt]
    return [ident,None,None,-2 if had_error else -1]

def _recount(rows,total,status="running",errors=0,started=None):
    vals=list(rows.values())
    return {
        "status":status,
        "processed":len(vals),
        "total":total,
        "matched":sum(1 for r in vals if r[3] in (0,1)),
        "city_fallback":sum(1 for r in vals if r[3]==2),
        "unmatched":sum(1 for r in vals if r[3]==-1),
        "errors":errors,
        **({"started":started} if started else {})
    }

def _keepalive():
    base=os.environ.get("RENDER_EXTERNAL_URL","").rstrip("/")
    if not base:
        return
    while not _stop_keepalive.wait(240):
        try:
            urllib.request.urlopen(base+"/api/fcdo-mapsco/status",timeout=20).read(64)
        except Exception:
            pass

def _run():
    key=os.environ.get("GEOCODE_MAPS_KEY","")
    if not key:
        with _lock:
            _state.update(status="failed",error="GEOCODE_MAPS_KEY missing")
        return
    conn=None
    started=time.time()
    try:
        items=_load_items()
        conn=_db()
        saved=_load_saved(conn)
        pending=[it for it in items if it[0] not in saved]
        with _lock:
            _state.clear()
            _state.update(_recount(saved,len(items),"running",0,started))
        _stop_keepalive.clear()
        threading.Thread(target=_keepalive,daemon=True).start()

        run_errors=0
        with ThreadPoolExecutor(max_workers=24) as ex:
            futs={ex.submit(_one,it,key):it[0] for it in pending}
            for fut in as_completed(futs):
                ident=futs[fut]
                try:
                    row=fut.result()
                except Exception:
                    row=[ident,None,None,-2]
                if row[3] != -2:
                    _save(conn,row)
                    saved[ident]=row
                else:
                    run_errors += 1
                with _lock:
                    _state.clear()
                    _state.update(_recount(saved,len(items),"running",run_errors,started))

        all_rows=[saved[k] for k in sorted(saved)]
        with open(RESULTS_FILE,"w",encoding="utf-8") as f:
            json.dump(all_rows,f,separators=(",",":"))
        final_status="complete" if len(saved)==len(items) else "complete_with_errors"
        with _lock:
            _state.clear()
            _state.update(_recount(saved,len(items),final_status,run_errors,started))
            _state["finished"]=time.time()
    except Exception as e:
        with _lock:
            _state["status"]="failed"
            _state["error"]=repr(e)
            _state["finished"]=time.time()
    finally:
        _stop_keepalive.set()
        if conn:
            conn.close()

def start_job():
    global _thread
    with _lock:
        if _state.get("status")=="running":
            return dict(_state)
    _thread=threading.Thread(target=_run,daemon=True)
    _thread.start()
    time.sleep(0.15)
    return status()

def status():
    with _lock:
        snapshot=dict(_state)
    if snapshot.get("status") in ("idle","failed"):
        try:
            items=_load_items()
            conn=_db()
            saved=_load_saved(conn)
            conn.close()
            snapshot=_recount(saved,len(items),snapshot.get("status","idle"),snapshot.get("errors",0))
        except Exception:
            pass
    return snapshot

def results():
    try:
        items=_load_items()
        conn=_db()
        saved=_load_saved(conn)
        conn.close()
        if not saved:
            return None
        return [saved[k] for k in sorted(saved)]
    except Exception:
        if os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE,"r",encoding="utf-8") as f:
                return json.load(f)
        return None
