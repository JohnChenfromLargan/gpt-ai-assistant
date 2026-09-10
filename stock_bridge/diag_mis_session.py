#!/usr/bin/env python3
from __future__ import annotations
import http.cookiejar, json, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
TZ=timezone(timedelta(hours=8)); SYMS=("2002","3019")
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/127 Safari/537.36"
MAX_SECONDS=120; INTERVAL=2.4

def numeric_z(v):
    try: return v not in (None,"","-","--","---") and float(v)>0
    except (TypeError,ValueError): return False

def make_stateful():
    jar=http.cookiejar.CookieJar(); op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req=urllib.request.Request("https://mis.twse.com.tw/stock/",headers={"User-Agent":UA,"Accept-Language":"zh-TW,zh;q=0.9"})
    try:
        with op.open(req,timeout=20) as r: r.read(4096)
    except Exception: pass
    return op

def request(opener, channels, referer=None):
    q=urllib.parse.urlencode({"ex_ch":channels,"json":"1","delay":"0","_":str(int(time.time()*1000))},safe="|")
    headers={"User-Agent":UA,"Accept":"application/json,text/javascript,*/*;q=0.01","Cache-Control":"no-cache","Pragma":"no-cache"}
    if referer: headers.update({"Referer":referer,"X-Requested-With":"XMLHttpRequest"})
    req=urllib.request.Request("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?"+q,headers=headers)
    if opener is None:
        with urllib.request.urlopen(req,timeout=20) as r: return json.loads(r.read().decode("utf-8-sig"))
    with opener.open(req,timeout=20) as r: return json.loads(r.read().decode("utf-8-sig"))

def init(): return {"requests":0,"errors":0,"hits":{s:0 for s in SYMS},"first_hit":{s:None for s in SYMS},"from_times":set(),"latest_times":set(),"last_rows":{}}

def sample(st,opener,channels,referer):
    st["requests"]+=1
    try:
        p=request(opener,channels,referer); qt=p.get("queryTime") or {}
        st["from_times"].add(str(qt.get("sessionFromTime"))); st["latest_times"].add(str(qt.get("sessionLatestTime")))
        for row in p.get("msgArray") or []:
            s=str(row.get("c",""));
            if s not in SYMS: continue
            st["last_rows"][s]={k:row.get(k) for k in ("d","t","tlong","z","s","tv","v","o","h","l")}
            if numeric_z(row.get("z")):
                st["hits"][s]+=1
                if st["first_hit"][s] is None:
                    st["first_hit"][s]={"local":datetime.now(TZ).isoformat(timespec="seconds"),"query":qt,"row":st["last_rows"][s]}
    except Exception as e: st["errors"]+=1

def main():
    channels="|".join(f"tse_{s}.tw" for s in SYMS); referer="https://mis.twse.com.tw/stock/"
    stateful=make_stateful(); variants={"STATELESS":init(),"STATEFUL":init()}; start=datetime.now(TZ); mono=time.monotonic()
    while time.monotonic()-mono<MAX_SECONDS:
        sample(variants["STATELESS"],None,channels,None)
        sample(variants["STATEFUL"],stateful,channels,referer)
        rem=MAX_SECONDS-(time.monotonic()-mono)
        if rem>0: time.sleep(min(INTERVAL,rem))
    for v in variants.values():
        v["from_times"]=sorted(v["from_times"]); v["latest_times"]=sorted(v["latest_times"])
    print(json.dumps({"started_at":start.isoformat(timespec="seconds"),"elapsed_seconds":round(time.monotonic()-mono,1),"variants":variants},ensure_ascii=False,indent=2))
if __name__=="__main__": main()
