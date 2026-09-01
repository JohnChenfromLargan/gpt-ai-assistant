#!/usr/bin/env python3
"""Read-only diagnostic probe for TWSE MIS quote endpoints."""
from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

TZ_TAIPEI = timezone(timedelta(hours=8))
API_BASE = "https://mis.twse.com.tw/stock/api/"
SYMBOLS = ("2002", "3019")


def build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )


def request_bytes(
    client: urllib.request.OpenerDirector,
    url: str,
    *,
    referer: str | None = None,
) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/127 Safari/537.36",
        "Accept": "application/json,text/javascript,text/html,*/*;q=0.01",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(url, headers=headers)
    with client.open(req, timeout=30) as response:
        return response.read()


def request_json(
    client: urllib.request.OpenerDirector,
    url: str,
    *,
    referer: str | None = None,
) -> Any:
    return json.loads(request_bytes(client, url, referer=referer).decode("utf-8-sig"))


def error_text(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError:{exc.code}:{exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def compact_payload(payload: Any, *, tail: int = 5) -> dict[str, Any]:
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
        result["tail"] = [
            {
                key: row.get(key)
                for key in (
                    "key", "c", "n", "d", "t", "tlong", "z", "o", "h", "l",
                    "s", "v", "tv", "y", "pz", "ps", "ch", "ex"
                )
                if isinstance(row, dict) and key in row
            }
            for row in messages[-tail:]
            if isinstance(row, dict)
        ]
        fields: set[str] = set()
        for row in messages:
            if isinstance(row, dict):
                fields.update(row)
        result["fields"] = sorted(fields)
    return result


def try_json(
    client: urllib.request.OpenerDirector,
    url: str,
    *,
    referer: str | None = None,
) -> dict[str, Any]:
    try:
        return {"url": url, "result": compact_payload(request_json(client, url, referer=referer))}
    except Exception as exc:
        return {"url": url, "error": error_text(exc)}


def probe_symbol(symbol: str) -> dict[str, Any]:
    client = build_opener()
    referer = f"https://mis.twse.com.tw/stock/fibest.jsp?stock={symbol}"
    bootstrap_results = []
    for url in (
        "https://mis.twse.com.tw/stock/",
        "https://mis.twse.com.tw/stock/index.jsp",
        referer,
    ):
        try:
            body = request_bytes(client, url)
            bootstrap_results.append({"url": url, "status": "PASS", "bytes": len(body)})
            break
        except Exception as exc:
            bootstrap_results.append({"url": url, "status": "FAIL", "error": error_text(exc)})

    nonce = int(time.time() * 1000)
    stock_url = API_BASE + "getStock.jsp?" + urllib.parse.urlencode(
        {"ch": f"{symbol}.tw", "json": "1", "_": nonce}
    )
    stock_probe = try_json(client, stock_url, referer=referer)
    stock_payload = None
    try:
        stock_payload = request_json(client, stock_url, referer=referer)
    except Exception:
        stock_payload = None

    key = f"tse_{symbol}.tw"
    d0 = datetime.now(TZ_TAIPEI).strftime("%Y%m%d")
    if isinstance(stock_payload, dict):
        rows = stock_payload.get("msgArray")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            key = str(rows[0].get("key") or key)
            d0 = str(rows[0].get("d") or d0)

    info_url = API_BASE + "getStockInfo.jsp?" + urllib.parse.urlencode(
        {"ex_ch": key, "json": "1", "delay": "0", "_": nonce + 1}
    )
    info_probe = try_json(client, info_url, referer=referer)

    ohlc: dict[str, Any] = {}
    now = datetime.now(TZ_TAIPEI)
    variants = {
        "resolved_d0": d0,
        "today_yyyymmdd": now.strftime("%Y%m%d"),
        "today_roc": str(now.year - 1911) + now.strftime("%m%d"),
        "no_d0": None,
    }
    for index, (label, d0_value) in enumerate(variants.items(), start=1):
        params: dict[str, Any] = {"ex_ch": key, "_": nonce + 10 + index}
        if d0_value is not None:
            params["d0"] = d0_value
        url = API_BASE + "getOhlc.jsp?" + urllib.parse.urlencode(params)
        ohlc[label] = try_json(client, url, referer=referer)

    return {
        "symbol": symbol,
        "bootstrap": bootstrap_results,
        "stock_lookup": stock_probe,
        "resolved_key": key,
        "resolved_d0": d0,
        "stock_info": info_probe,
        "ohlc": ohlc,
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
            output["symbols"][symbol] = {"error": error_text(exc)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
