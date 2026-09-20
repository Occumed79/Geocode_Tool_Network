import os, json, time, threading, urllib.parse, urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

INPUT_FILE = "fcdo_geocode_input.jsonl"
RESULTS_FILE = "fcdo_geocode_results.json"
_state = {"status":"idle","processed":0,"total":0,"matched":0,"city_fallback":0,"unmatched":0,"errors":0}
_lock = threading.Lock()
_thread = None
_query_cache = {}
_query_cache_lock = threading.Lock()

class _Limiter:
    def __init__(self, n=5, period=1.05):
        self.n=n; self.period=period; self.times=deque(); self.lock=threading.Lock()
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

def _join(parts):
    return ", ".join(str(x).strip() for x in parts if x is not None and str(x).strip())

def _lookup(q, key):
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
    seen=set(); tries=[q for q in tries if q and not (q in seen or seen.add(q))]
    had_error=False
    for idx,q in enumerate(tries):
        try: got=_lookup(q,key)
        except Exception: had_error=True; continue
        if got:
            if q==city_q: mt=2
            elif q==name_q and placeholder: mt=1
            else: mt=0
            return [ident,got[0],got[1],mt]
    return [ident,None,None,-2 if had_error else -1]

def _run():
    key=os.environ.get("GEOCODE_MAPS_KEY","")
    if not key:
        with _lock: _state.update(status="failed",error="GEOCODE_MAPS_KEY missing")
        return
    try:
        with open(INPUT_FILE,"r",encoding="utf-8") as f:
            items=[json.loads(x) for x in f if x.strip()]
        with _lock:
            _state.clear(); _state.update(status="running",processed=0,total=len(items),matched=0,city_fallback=0,unmatched=0,errors=0,started=time.time())
        out=[]
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs={ex.submit(_one,it,key):it[0] for it in items}
            for fut in as_completed(futs):
                ident=futs[fut]
                try: row=fut.result()
                except Exception: row=[ident,None,None,-2]
                out.append(row)
                with _lock:
                    _state["processed"]+=1
                    if row[3] in (0,1): _state["matched"]+=1
                    elif row[3]==2: _state["city_fallback"]+=1
                    elif row[3]==-2: _state["errors"]+=1
                    else: _state["unmatched"]+=1
        out.sort(key=lambda x:x[0])
        with open(RESULTS_FILE,"w",encoding="utf-8") as f:
            json.dump(out,f,separators=(",",":"))
        with _lock:
            _state["status"]="complete"; _state["finished"]=time.time()
    except Exception as e:
        with _lock:
            _state["status"]="failed"; _state["error"]=repr(e); _state["finished"]=time.time()

def start_job():
    global _thread
    with _lock:
        if _state.get("status")=="running":
            return dict(_state)
        _state.clear(); _state.update(status="starting",processed=0,total=0,matched=0,city_fallback=0,unmatched=0,errors=0)
    try:
        if os.path.exists(RESULTS_FILE): os.remove(RESULTS_FILE)
    except OSError: pass
    _thread=threading.Thread(target=_run,daemon=True)
    _thread.start()
    return status()

def status():
    with _lock: return dict(_state)

def results():
    if not os.path.exists(RESULTS_FILE): return None
    with open(RESULTS_FILE,"r",encoding="utf-8") as f: return json.load(f)
