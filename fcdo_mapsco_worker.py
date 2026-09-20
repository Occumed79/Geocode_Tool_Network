import os, json, time, threading, urllib.parse, urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

API_KEY = os.environ["GEOCODE_MAPS_KEY"]
INPUT = "fcdo_geocode_input.jsonl"
RESULTS = "fcdo_geocode_results.json"
state = {"status":"starting","processed":0,"total":0,"matched":0,"city_fallback":0,"unmatched":0,"errors":0,"started":time.time()}
state_lock = threading.Lock()

class Limiter:
    def __init__(self, max_calls=5, period=1.05):
        self.max_calls=max_calls; self.period=period; self.times=deque(); self.lock=threading.Lock()
    def wait(self):
        while True:
            with self.lock:
                now=time.monotonic()
                while self.times and now-self.times[0] >= self.period:
                    self.times.popleft()
                if len(self.times) < self.max_calls:
                    self.times.append(now); return
                delay=self.period-(now-self.times[0])+0.01
            time.sleep(max(delay,0.02))
limiter=Limiter()

def join(parts):
    return ", ".join(str(x).strip() for x in parts if x is not None and str(x).strip())

def lookup(q):
    params=urllib.parse.urlencode({"q":q,"api_key":API_KEY,"limit":"1","addressdetails":"0","extratags":"0","namedetails":"0"})
    url="https://geocode.maps.co/search?"+params
    for attempt in range(5):
        limiter.wait()
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"OccuMed-FCDO-Geocoder/1.0"})
            with urllib.request.urlopen(req,timeout=25) as r:
                if r.status==200:
                    data=json.loads(r.read().decode("utf-8"))
                    if data:
                        return float(data[0]["lat"]),float(data[0]["lon"])
                    return None
        except Exception as e:
            if attempt==4: raise
            time.sleep(0.8*(attempt+1))
    return None

def geocode_item(it):
    ident,name,addr,city,region,country=it
    tries=[
        join([addr,city,region,country]),
        join([name,city,region,country]),
        join([city,region,country])
    ]
    seen=set(); tries=[q for q in tries if q and not (q in seen or seen.add(q))]
    last_error=None
    for idx,q in enumerate(tries):
        try:
            got=lookup(q)
        except Exception as e:
            last_error=str(e); continue
        if got:
            return [ident,got[0],got[1],idx]
    return [ident,None,None,-2 if last_error else -1]

def worker():
    try:
        with open(INPUT,"r",encoding="utf-8") as f:
            items=[json.loads(line) for line in f if line.strip()]
        with state_lock:
            state.update(status="running",total=len(items))
        out=[]
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs={ex.submit(geocode_item,it):it[0] for it in items}
            for fut in as_completed(futs):
                try: row=fut.result()
                except Exception:
                    row=[futs[fut],None,None,-2]
                out.append(row)
                with state_lock:
                    state["processed"]+=1
                    if row[3] in (0,1): state["matched"]+=1
                    elif row[3]==2: state["city_fallback"]+=1
                    elif row[3]==-2: state["errors"]+=1
                    else: state["unmatched"]+=1
        out.sort(key=lambda x:x[0])
        with open(RESULTS,"w",encoding="utf-8") as f:
            json.dump(out,f,separators=(",",":"))
        with state_lock:
            state["status"]="complete"; state["finished"]=time.time()
    except Exception as e:
        with state_lock:
            state["status"]="failed"; state["error"]=repr(e); state["finished"]=time.time()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/status"):
            with state_lock: body=json.dumps(state).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/results"):
            if not os.path.exists(RESULTS):
                body=json.dumps({"error":"not ready"}).encode(); self.send_response(202)
            else:
                body=open(RESULTS,"rb").read(); self.send_response(200)
            self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
        body=b"FCDO geocoder running"; self.send_response(200); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,format,*args): pass

threading.Thread(target=worker,daemon=True).start()
port=int(os.environ.get("PORT","10000"))
ThreadingHTTPServer(("0.0.0.0",port),Handler).serve_forever()
