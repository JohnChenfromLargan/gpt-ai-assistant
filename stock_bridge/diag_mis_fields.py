#!/usr/bin/env python3
from __future__ import annotations
import json, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
TZ=timezone(timedelta(hours=8))
SYMS=("2002","3019")
UA="Mozilla/5.0 (compatible; TWSE-MIS-Diagnostic/1.0)"

def get(ex_ch):
    q=urllib.parse.urlencode({"ex_ch":ex_ch,"json":"1","delay":"0","_":str(int(time.time()*1000))},safe="|")
    u="https://mis.twse.com.tw/stock/api/getStockInfo.jsp?"+q
    req=urllib.request.Request(u,headers={"User-Agent":UA,"Accept":"application/json,*/*","Cache-Control":"no-cache","Pragma":"no-cache"})
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8-sig"))

def compact(row):
    keys=("c","n","d","t","tlong","z","s","tv","pz","ps","p","bp","mt","ts","o","h","l","v","a","b","f","g","y")
    return {k:row.get(k) for k in keys if k in row}

def main():
    date=datetime.now(TZ).strftime("%Y%m%d")
    combined="|".join(f"tse_{s}.tw_{date}" for s in SYMS)
    out=[]
    for i in range(30):
        rec={"i":i+1,"local":datetime.now(TZ).isoformat(timespec="seconds")}
        try:
            p=get(combined)
            rec["rtcode"]=p.get("rtcode"); rec["queryTime"]=p.get("queryTime")
            rec["rows"]=[compact(r) for r in (p.get("msgArray") or []) if str(r.get("c")) in SYMS]
        except Exception as e:
            rec["error"]=f"{type(e).__name__}: {e}"
        out.append(rec)
        time.sleep(1.3)
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
