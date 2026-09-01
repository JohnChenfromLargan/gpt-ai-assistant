#!/usr/bin/env python3
"""Diagnostic probe for TWSE MIS quote endpoints.

This script is intentionally read-only. It prints compact response metadata for
getStock.jsp, getStockInfo.jsp and getOhlc.jsp so that the bridge can select a
reliable official last-trade source without guessing field semantics.
"""
from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

TZ_TAIPEI = timezone(timedelta(hours=8))
BASE = "https://mis.twse.com.tw/stock/api/"
SYMBOLS = ("2002", "3019")


def opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def request_json(client: urllib.request.OpenerDirector, url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/127 Safari/537.36",
            "Accept": "application/json,text/javascript,*/*;q=0.01",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Referer": "https://mis.twse.com.tw/stock/fibest.jsp?stock=2002",
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with client.open(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def compact_payload(payload: Any, *, tail: int = 3) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    messages = payload.get("msgArray")
    result: dict[str, Any] = {
        "rtcode": payload.get("rtcode"),
        "rtmessage": payload.get("rtmessage"),
        "queryTime": payload.get("queryTime"),
        "userDelay": payload.get("userDelay"),
        "msg_count": len(messages) if isinstance(messages, list) else None,
    }
    if isinstance(messages, list):
        selected = messages[-tail:]
        result["tail"] = [
            {
                key: row.get(key)
                for key in (
                    "key", "c", "n", "d", "t", "tlong", "z", "c", "o", "h", "l",
                    "s", "v", "tv", "y", "pz", "ps", "ch", "ex"
                )
                if isinstance(row, dict) and key in row
            }
            for row in selected
            if isinstance(row, dict)
        ]
        field_names: set[str] = set()
        for row in messages:
            if isinstance(row, dict):
                field_names.update(row)
        result["fields"] = sorted(field_names)
    return result


def probe_symbol(symbol: str) -> dict[str, Any]:
    client = opener()
    bootstrap = urllib.request.Request(
        f"https://mis.twse.com.tw/stock/fibest.jsp?stock={symbol}",
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW,zh;q=0.9"},
    )
    with client.open(bootstrap, timeout=30) as response:
        response.read(1024)

    nonce = int(time.time() * 1000)
    stock_url = BASE + "getStock.jsp?" + urllib.parse.urlencode(
        {"ch": f"{symbol}.tw", "json": "1", "_": nonce}
    )
    stock = request_json(client, stock_url)
    stock_messages = stock.get("msgArray") if isinstance(stock, dict) else None
    stock_row = stock_messages[0] if isinstance(stock_messages, list) and stock_messages else {}
    key = stock_row.get("key") if isinstance(stock_row, dict) else None
    d0 = stock_row.get("d") if isinstance(stock_row, dict) else None
    if not key:
        key = f"tse_{symbol}.tw"
    if not d0:
        d0 = datetime.now(TZ_TAIPEI).strftime("%Y%m%d")

    info_url = BASE + "getStockInfo.jsp?" + urllib.parse.urlencode(
        {"ex_ch": key, "json": "1", "delay": "0", "_": nonce + 1}
    )
    info = request_json(client, info_url)

    ohlc_variants: dict[str, Any] = {}
    variants = {
        "stock_d": d0,
        "today_yyyymmdd": datetime.now(TZ_TAIPEI).strftime("%Y%m%d"),
        "empty_d0": "",
    }
    for label, d0_value in variants.items():
        params = {"ex_ch": key, "_": nonce + 10 + len(ohlc_variants)}
        if d0_value:
            params["d0"] = d0_value
        url = BASE + "getOhlc.jsp?" + urllib.parse.urlencode(params)
        try:
            ohlc_variants[label] = compact_payload(request_json(client, url), tail=5)
        except Exception as exc:
            ohlc_variants[label] = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "symbol": symbol,
        "stock_lookup": compact_payload(stock),
        "resolved_key": key,
        "resolved_d0": d0,
        "stock_info": compact_payload(info, tail=1),
        "ohlc": ohlc_variants,
    }


def main() -> int:
    output = {
        "probed_at": datetime.now(TZ_TAIPEI).isoformat(timespec="seconds"),
        "symbols": {},
    }
    for symbol in SYMBOLS:
        try:
            output["symbols"][symbol] = probe_symbol(symbol)
        except Exception as exc:
            output["symbols"][symbol] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
