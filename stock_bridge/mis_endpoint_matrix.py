#!/usr/bin/env python3
"""Probe current TWSE MIS endpoint variants used by the official page scripts.

Read-only diagnostic. It emits compact metadata and the final rows only.
"""
from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

TZ = timezone(timedelta(hours=8))
BASE = "https://mis.twse.com.tw/stock/api/"
SYMBOLS = ("2002", "3019")


def client() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )


def get_json(opener: urllib.request.OpenerDirector, url: str, referer: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/127 Safari/537.36",
            "Accept": "application/json,text/javascript,*/*;q=0.01",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with opener.open(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def summarize(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    rows = payload.get("msgArray")
    out: dict[str, Any] = {
        "rtcode": payload.get("rtcode"),
        "rtmessage": payload.get("rtmessage"),
        "userDelay": payload.get("userDelay"),
        "queryTime": payload.get("queryTime"),
        "msg_count": len(rows) if isinstance(rows, list) else None,
    }
    if isinstance(rows, list) and rows:
        out["first"] = rows[0]
        out["last"] = rows[-1]
        out["fields"] = sorted({key for row in rows if isinstance(row, dict) for key in row})
    return out


def call(opener: urllib.request.OpenerDirector, url: str, referer: str) -> dict[str, Any]:
    try:
        return {"url": url, "response": summarize(get_json(opener, url, referer))}
    except urllib.error.HTTPError as exc:
        return {"url": url, "error": f"HTTPError:{exc.code}:{exc.reason}"}
    except Exception as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


def probe(symbol: str) -> dict[str, Any]:
    op = client()
    referer = f"https://mis.twse.com.tw/stock/fibest.jsp?stock={symbol}"
    root = urllib.request.Request(
        "https://mis.twse.com.tw/stock/",
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW,zh;q=0.9"},
    )
    with op.open(root, timeout=30) as response:
        response.read()

    now = datetime.now(TZ)
    date = now.strftime("%Y%m%d")
    nonce = int(time.time() * 1000)
    short_key = f"tse_{symbol}.tw"
    full_key = f"{short_key}_{date}"
    start = (now.date() - timedelta(days=10)).strftime("%Y%m%d")

    endpoints: dict[str, str] = {
        "stock": BASE + "getStock.jsp?" + urllib.parse.urlencode({"ch": f"{symbol}.tw", "json": 1, "_": nonce}),
        "stock_info_short": BASE + "getStockInfo.jsp?" + urllib.parse.urlencode({"ex_ch": short_key, "json": 1, "delay": 0, "_": nonce + 1}),
        "stock_info_full": BASE + "getStockInfo.jsp?" + urllib.parse.urlencode({"ex_ch": full_key, "json": 1, "delay": 0, "_": nonce + 2}),
        "ohlc_short_date": BASE + "getOhlc.jsp?" + urllib.parse.urlencode({"ex_ch": short_key, "d0": date, "_": nonce + 3}),
        "ohlc_full_date": BASE + "getOhlc.jsp?" + urllib.parse.urlencode({"ex_ch": full_key, "d0": date, "_": nonce + 4}),
        "ohlc_short_no_date": BASE + "getOhlc.jsp?" + urllib.parse.urlencode({"ex_ch": short_key, "_": nonce + 5}),
        "ohlc_full_no_date": BASE + "getOhlc.jsp?" + urllib.parse.urlencode({"ex_ch": full_key, "_": nonce + 6}),
        "chart_ohlc": BASE + "getChartOhlcStatis.jsp?" + urllib.parse.urlencode({"ex": "tse", "ch": f"{symbol}.tw", "fqy": 1, "_": nonce + 7}),
        "daily_ma_full": BASE + "getDailyRangeWithMA.jsp?" + urllib.parse.urlencode({"ex_ch": full_key, "d0": start, "d1": date, "_": nonce + 8}),
        "daily_ma_short": BASE + "getDailyRangeWithMA.jsp?" + urllib.parse.urlencode({"ex_ch": short_key, "d0": start, "d1": date, "_": nonce + 9}),
        "daily_kd_full": BASE + "getDailyRangeOnlyKD.jsp?" + urllib.parse.urlencode({"ex_ch": full_key, "d0": start, "d1": date, "_": nonce + 10}),
        "show_chart": BASE + "getShowChart.jsp?" + urllib.parse.urlencode({"_": nonce + 11}),
    }
    return {name: call(op, url, referer) for name, url in endpoints.items()}


def main() -> int:
    result = {
        "probed_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "symbols": {symbol: probe(symbol) for symbol in SYMBOLS},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
