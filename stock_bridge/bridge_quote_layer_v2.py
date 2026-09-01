#!/usr/bin/env python3
"""Quote layer v2: combined dephased MIS polling plus verified cache."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import bridge
    import bridge_quote_layer as q1
except ModuleNotFoundError:  # pragma: no cover
    from . import bridge  # type: ignore
    from . import bridge_quote_layer as q1  # type: ignore

INTERVALS = (1.7, 2.3, 2.9, 1.9, 2.6, 2.1, 3.1, 1.8)
MAX_ROUNDS = 30
MAX_WINDOW_SECONDS = 68
CACHE_PATH = Path(
    os.environ.get("TWSE_QUOTE_CACHE_PATH", str(bridge.ROOT / "quote_cache.json"))
).expanduser()


def combined_url(reference: datetime, round_index: int) -> str:
    date = reference.strftime("%Y%m%d")
    channels = "|".join(f"tse_{s}.tw_{date}" for s in bridge.SYMBOLS)
    params = {
        "ex_ch": channels,
        "json": "1",
        "delay": "0",
        "_": f"{int(time.time() * 1000)}{round_index:03d}",
    }
    return "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?" + urllib.parse.urlencode(
        params, safe="|"
    )


def safe_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return bridge.compact_date_to_iso(str(value))
    except ValueError:
        return None


def sample_row(payload: dict[str, Any], symbol: str, index: int) -> dict[str, Any]:
    query = payload.get("queryTime") or {}
    item = next(
        (
            row
            for row in payload.get("msgArray") or []
            if isinstance(row, dict) and str(row.get("c", "")) == symbol
        ),
        None,
    )
    if item is None:
        return {"sample": index, "error": "SYMBOL_NOT_IN_MSGARRAY"}
    return {
        "sample": index,
        "query_date": safe_date(query.get("sysDate")),
        "query_time": query.get("sysTime"),
        "trade_date": safe_date(item.get("d")),
        "trade_time": item.get("t"),
        "tlong": item.get("tlong"),
        "z": item.get("z"),
        "open": bridge.parse_number(item.get("o")),
        "high": bridge.parse_number(item.get("h")),
        "low": bridge.parse_number(item.get("l")),
        "volume": bridge.parse_int(item.get("v")),
        "temporal_volume": bridge.parse_int(item.get("tv")),
    }


def candidate_fresh(candidate: bridge.QuoteCandidate, reference: datetime) -> bool:
    age = bridge.candidate_age_seconds(candidate, reference)
    return (
        candidate.trade_date == reference.date().isoformat()
        and age is not None
        and -q1.FUTURE_TOLERANCE_SECONDS <= age <= q1.QUOTE_MAX_AGE_SECONDS
    )


def live_result(
    symbol: str,
    candidates: list[bridge.QuoteCandidate],
    samples: list[dict[str, Any]],
    last_error: str | None,
    reference: datetime,
) -> tuple[dict[str, Any], bridge.QuoteCandidate | None]:
    valid: list[bridge.QuoteCandidate] = []
    stale: list[bridge.QuoteCandidate] = []
    for candidate in candidates:
        age = bridge.candidate_age_seconds(candidate, reference)
        if age is None or age < -q1.FUTURE_TOLERANCE_SECONDS:
            continue
        if candidate.trade_date == reference.date().isoformat() and age <= 900:
            valid.append(candidate)
        else:
            stale.append(candidate)
    meta = {"sample_count": len(samples), "samples": samples, "last_error": last_error}
    if valid:
        candidate = max(valid, key=q1.candidate_key)
        return (
            q1.render_candidate_quote(
                symbol, candidate, reference,
                status="PASS", origin="LIVE_MIS", reused_from_cache=False,
                live_result={"status": "PASS", **meta}, cache_captured_at=None,
            ),
            candidate,
        )
    if stale:
        candidate = max(stale, key=q1.candidate_key)
        return (
            q1.render_candidate_quote(
                symbol, candidate, reference,
                status="FAIL_STALE_TRADE", origin="LIVE_MIS", reused_from_cache=False,
                live_result={"status": "FAIL_STALE_TRADE", **meta}, cache_captured_at=None,
            ),
            candidate,
        )
    status = "FAIL_NO_RECENT_TRADE" if any("error" not in x for x in samples) else "FAIL_NETWORK"
    return (
        {
            "status": status,
            "name": bridge.SYMBOLS[symbol],
            "price": None,
            "trade_date": None,
            "trade_time": None,
            "tlong": None,
            "age_seconds": None,
            "open": None,
            "high": None,
            "low": None,
            "volume": None,
            "query_date": None,
            "query_time": None,
            "quote_origin": "NONE",
            "reused_from_cache": False,
            "cache_captured_at": None,
            "same_msgarray_verified": False,
            "live_fetch_status": status,
            "sample_count": len(samples),
            "samples": samples,
            "last_error": last_error,
        },
        None,
    )


def poll_combined() -> tuple[dict[str, tuple[dict[str, Any], bridge.QuoteCandidate | None]], dict[str, Any]]:
    start_reference = bridge.now_taipei()
    samples = {s: [] for s in bridge.SYMBOLS}
    candidates = {s: [] for s in bridge.SYMBOLS}
    last_error: str | None = None
    started = time.monotonic()
    rounds = 0
    for index in range(1, MAX_ROUNDS + 1):
        if index > 1 and time.monotonic() - started >= MAX_WINDOW_SECONDS:
            break
        try:
            payload = bridge.http_json(combined_url(start_reference, index))
            if not isinstance(payload, dict) or str(payload.get("rtcode")) != "0000":
                raise bridge.BridgeError(f"MIS response rejected: {payload}")
            for symbol in bridge.SYMBOLS:
                samples[symbol].append(sample_row(payload, symbol, index))
                candidate = bridge.extract_quote_candidate(payload, symbol)
                if candidate is not None:
                    candidates[symbol].append(candidate)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            for symbol in bridge.SYMBOLS:
                samples[symbol].append({"sample": index, "error": last_error})
        rounds = index
        now = bridge.now_taipei()
        if all(any(candidate_fresh(c, now) for c in candidates[s]) for s in bridge.SYMBOLS):
            break
        remaining = MAX_WINDOW_SECONDS - (time.monotonic() - started)
        if index < MAX_ROUNDS and remaining > 0:
            time.sleep(min(INTERVALS[(index - 1) % len(INTERVALS)], remaining))
    reference = bridge.now_taipei()
    result = {
        symbol: live_result(symbol, candidates[symbol], samples[symbol], last_error, reference)
        for symbol in bridge.SYMBOLS
    }
    return result, {
        "mode": "COMBINED_DEPHASED_MIS_POLLING",
        "combined_request": True,
        "parallel_sampling": True,
        "workers": 1,
        "rounds": rounds,
        "window_seconds": round(time.monotonic() - started, 1),
        "max_window_seconds": MAX_WINDOW_SECONDS,
        "intervals_seconds": list(INTERVALS),
        "network_last_error": last_error,
    }


def build_quotes() -> tuple[dict[str, Any], dict[str, Any]]:
    cache = q1.load_quote_cache(CACHE_PATH)
    cached = cache.get("quotes") or {}
    live, diagnostics = poll_combined()
    reference = bridge.now_taipei()
    merged: dict[str, Any] = {}
    next_cache = dict(cached)
    updated: list[str] = []
    reused: list[str] = []
    for symbol in bridge.SYMBOLS:
        live_quote, live_candidate = live[symbol]
        quote, cache_update = q1.merge_live_and_cached_quote(
            symbol, live_quote, live_candidate, cached.get(symbol), reference
        )
        merged[symbol] = quote
        if cache_update is not None:
            next_cache[symbol] = cache_update
            updated.append(symbol)
        if quote.get("reused_from_cache"):
            reused.append(symbol)
    q1.write_quote_cache(
        {
            "schema_version": q1.CACHE_SCHEMA_VERSION,
            "updated_at": reference.isoformat(timespec="seconds"),
            "quotes": next_cache,
        },
        CACHE_PATH,
    )
    return merged, {
        "enabled": True,
        "path": CACHE_PATH.name,
        "max_age_seconds": 900,
        "updated_symbols": updated,
        "reused_symbols": reused,
        **diagnostics,
    }


def build_latest() -> dict[str, Any]:
    try:
        openapi_rows = bridge.openapi_latest_rows()
        openapi_status = "PASS"
    except Exception as exc:
        openapi_rows = {}
        openapi_status = f"FAIL:{type(exc).__name__}:{exc}"
    quotes, quote_cache = build_quotes()
    histories: dict[str, Any] = {}
    for symbol in bridge.SYMBOLS:
        rows, meta = bridge.update_history(symbol, openapi_rows.get(symbol))
        bridge.write_history(symbol, rows, meta)
        histories[symbol] = meta
    generated = bridge.now_taipei()
    return {
        "schema_version": "1.0",
        "generated_at": generated.isoformat(timespec="seconds"),
        "market_date": generated.date().isoformat(),
        "source": {
            "primary": "TWSE",
            "quote": "TWSE MIS getStockInfo.jsp",
            "history": "TWSE STOCK_DAY",
            "latest_day_crosscheck": "TWSE OpenAPI STOCK_DAY_ALL",
            "openapi_status": openapi_status,
        },
        "bridge": {
            "transport_ready": any(q.get("status") == "PASS" for q in quotes.values())
            and all(h.get("count", 0) >= bridge.MIN_HISTORY_GATE for h in histories.values()),
            "analysis_ready": False,
            "phase": "MVP_DATA_BRIDGE",
            "note": "Combined dephased MIS polling with persisted verified trade cache.",
            "quote_cache": quote_cache,
        },
        "quotes": quotes,
        "history": histories,
    }


def self_test() -> None:
    reference = datetime(2026, 9, 1, 11, 30, tzinfo=bridge.TZ_TAIPEI)
    url = combined_url(reference, 1)
    assert "tse_2002.tw_20260901|tse_3019.tw_20260901" in url
    payload = {
        "rtcode": "0000",
        "queryTime": {"sysDate": "20260901", "sysTime": "11:30:05"},
        "msgArray": [
            {"c": "2002", "n": "中鋼", "d": "20260901", "t": "11:30:00", "z": "19.1"},
            {"c": "3019", "n": "亞光", "d": "20260901", "t": "11:29:59", "z": "145.5"},
        ],
    }
    for symbol in bridge.SYMBOLS:
        assert bridge.extract_quote_candidate(payload, symbol) is not None
        assert sample_row(payload, symbol, 1)["z"] != "-"
    print("QUOTE_LAYER_V2_SELF_TEST_PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    payload = build_latest()
    bridge.LATEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "generated_at": payload["generated_at"],
        "quotes": {s: {"status": q["status"], "origin": q.get("quote_origin"), "trade_time": q.get("trade_time")} for s, q in payload["quotes"].items()},
        "quote_cache": payload["bridge"]["quote_cache"],
        "transport_ready": payload["bridge"]["transport_ready"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
