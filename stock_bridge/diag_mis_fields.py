#!/usr/bin/env python3
from __future__ import annotations
import json, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
TZ=timezone(timedelta(hours=8))
SYMS=("2002","3019")
UA="Mozilla/5.0 (compatible; TWSE-MIS-Diagnostic/1.2)"
MAX_SECONDS=120
ROUND_INTERVAL=2.2


def get(ex_ch):
    q=urllib.parse.urlencode({"ex_ch":ex_ch,"json":"1","delay":"0","_":str(int(time.time()*1000))},safe="|")
    u="https://mis.twse.com.tw/stock/api/getStockInfo.jsp?"+q
    req=urllib.request.Request(u,headers={"User-Agent":UA,"Accept":"application/json,*/*","Cache-Control":"no-cache","Pragma":"no-cache"})
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def numeric_z(v):
    try:
        return v not in (None,"","-","--","---") and float(v)>0
    except (TypeError,ValueError):
        return False


def compact(row):
    return {k:row.get(k) for k in ("c","n","d","t","tlong","z","s","tv","pz","ps","o","h","l","v","a","b","y") if k in row}


def init_variant(ex_ch):
    return {"ex_ch":ex_ch,"requests":0,"errors":0,"distinct_server_snapshots":0,"last_query":None,
            "symbols":{s:{"hit_count":0,"first_hit":None,"first_seen":None,"last_seen":None} for s in SYMS}}


def sample(v):
    v["requests"]+=1
    local=datetime.now(TZ).isoformat(timespec="seconds")
    try:
        p=get(v["ex_ch"]); qt=p.get("queryTime") or {}
        qkey=(qt.get("sysDate"),qt.get("sysTime"),qt.get("stockInfoItem"),qt.get("stockInfo"))
        if qkey!=v["last_query"]:
            v["distinct_server_snapshots"]+=1; v["last_query"]=qkey
        for row in p.get("msgArray") or []:
            s=str(row.get("c",""))
            if s not in SYMS: continue
            r=compact(row); st=v["symbols"][s]
            if st["first_seen"] is None: st["first_seen"]=r
            st["last_seen"]=r
            if numeric_z(row.get("z")):
                st["hit_count"]+=1
                if st["first_hit"] is None:
                    st["first_hit"]={"local":local,"query_time":qt.get("sysTime"),"row":r}
    except Exception:
        v["errors"]+=1


def main():
    now=datetime.now(TZ); date=now.strftime("%Y%m%d")
    variants={
      "STANDARD_NO_DATE":init_variant("|".join(f"tse_{s}.tw" for s in SYMS)),
      "DATED":init_variant("|".join(f"tse_{s}.tw_{date}" for s in SYMS)),
    }
    started=time.monotonic(); rounds=0
    while time.monotonic()-started<MAX_SECONDS:
        rounds+=1
        sample(variants["STANDARD_NO_DATE"])
        sample(variants["DATED"])
        remaining=MAX_SECONDS-(time.monotonic()-started)
        if remaining>0: time.sleep(min(ROUND_INTERVAL,remaining))
    for v in variants.values():
        v.pop("last_query",None)
        for s in SYMS:
            st=v["symbols"][s]
            st["captured"]=st["hit_count"]>0
    print(json.dumps({"started_at":now.isoformat(timespec="seconds"),"elapsed_seconds":round(time.monotonic()-started,1),"rounds":rounds,"variants":variants},ensure_ascii=False,indent=2))
if __name__=="__main__": main()
