#!/usr/bin/env python3
from __future__ import annotations
import json, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
TZ=timezone(timedelta(hours=8))
SYMS=("2002","3019")
UA="Mozilla/5.0 (compatible; TWSE-MIS-Diagnostic/1.1)"
INTERVAL=1.3
MAX_SECONDS=180


def get(ex_ch):
    q=urllib.parse.urlencode({"ex_ch":ex_ch,"json":"1","delay":"0","_":str(int(time.time()*1000))},safe="|")
    u="https://mis.twse.com.tw/stock/api/getStockInfo.jsp?"+q
    req=urllib.request.Request(u,headers={"User-Agent":UA,"Accept":"application/json,*/*","Cache-Control":"no-cache","Pragma":"no-cache"})
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def compact(row):
    keys=("c","n","d","t","tlong","z","s","tv","pz","ps","o","h","l","v","a","b","f","g","y")
    return {k:row.get(k) for k in keys if k in row}


def numeric_z(v):
    try:
        return v not in (None,"","-","--","---") and float(v)>0
    except (TypeError,ValueError):
        return False


def main():
    now=datetime.now(TZ)
    date=now.strftime("%Y%m%d")
    combined="|".join(f"tse_{s}.tw_{date}" for s in SYMS)
    started=time.monotonic()
    total=0; errors=0; query_snapshots=0; last_query=None
    hits={s:[] for s in SYMS}
    last_seen={s:None for s in SYMS}
    first_seen={s:None for s in SYMS}
    while time.monotonic()-started < MAX_SECONDS:
        total+=1
        local=datetime.now(TZ).isoformat(timespec="seconds")
        try:
            p=get(combined)
            qt=p.get("queryTime") or {}
            qkey=(qt.get("sysDate"),qt.get("sysTime"),qt.get("stockInfoItem"),qt.get("stockInfo"))
            if qkey!=last_query:
                query_snapshots+=1; last_query=qkey
            for row in p.get("msgArray") or []:
                s=str(row.get("c",""))
                if s not in SYMS: continue
                r=compact(row); last_seen[s]=r
                if first_seen[s] is None: first_seen[s]=r
                if numeric_z(row.get("z")):
                    hits[s].append({"sample":total,"local":local,"query_time":qt.get("sysTime"),"row":r})
            if all(hits[s] for s in SYMS):
                break
        except Exception as e:
            errors+=1
        remaining=MAX_SECONDS-(time.monotonic()-started)
        if remaining>0: time.sleep(min(INTERVAL,remaining))
    elapsed=round(time.monotonic()-started,1)
    print(json.dumps({
        "started_at":now.isoformat(timespec="seconds"),
        "elapsed_seconds":elapsed,
        "interval_seconds":INTERVAL,
        "request_count":total,
        "network_error_count":errors,
        "distinct_server_snapshots":query_snapshots,
        "capture":{s:{
            "captured":bool(hits[s]),
            "first_hit":hits[s][0] if hits[s] else None,
            "hit_count":len(hits[s]),
            "first_seen":first_seen[s],
            "last_seen":last_seen[s]
        } for s in SYMS}
    },ensure_ascii=False,indent=2))
if __name__=="__main__": main()
