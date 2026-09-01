#!/usr/bin/env python3
"""Enhanced TWSE quote layer with concurrent sampling and persisted last-valid-trade cache.

This entrypoint replaces only the Phase-1 quote acquisition path. Historical
OHLCV handling remains delegated to ``bridge.py``. The persisted cache is used
only when it contains a TWSE MIS trade captured from one complete msgArray
object, for the same Taiwan trading date, and no more than 15 minutes old.

The module deliberately keeps fail-closed semantics:
- no bid/ask midpoint, volume, pz/ps, OHLC, or previous close can replace z;
- a cached trade keeps its original trade date/time;
- previous-day or >15-minute records never become PASS.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

try:
    import bridge
except ModuleNotFoundError:  # pragma: no cover - package-style import
    from . import bridge  # type: ignore

CACHE_SCHEMA_VERSION = "1.0"
QUOTE_MAX_AGE_SECONDS = 15 * 60
FUTURE_TOLERANCE_SECONDS = 5
QUOTE_CACHE_PATH = Path(
    os.environ.get("TWSE_QUOTE_CACHE_PATH", str(bridge.ROOT / "quote_cache.json"))
).expanduser()


def trade_datetime(candidate: bridge.QuoteCandidate) -> datetime | None:
    try:
        return datetime.fromisoformat(
            f"{candidate.trade_date}T{candidate.trade_time}"
        ).replace(tzinfo=bridge.TZ_TAIPEI)
    except (TypeError, ValueError):
        return None


def candidate_key(candidate: bridge.QuoteCandidate) -> tuple[str, str]:
    return candidate.trade_date, candidate.trade_time


def candidate_to_record(
    symbol: str,
    candidate: bridge.QuoteCandidate,
    captured_at: datetime,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": bridge.SYMBOLS[symbol],
        "price": candidate.price,
        "trade_date": candidate.trade_date,
        "trade_time": candidate.trade_time,
        "tlong": candidate.tlong,
        "query_date": candidate.query_date,
        "query_time": candidate.query_time,
        "open": candidate.open,
        "high": candidate.high,
        "low": candidate.low,
        "volume": candidate.volume,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "source": "TWSE_MIS",
        "same_msgarray_verified": True,
    }


def record_to_candidate(symbol: str, record: Any) -> bridge.QuoteCandidate | None:
    if not isinstance(record, dict):
        return None
    if str(record.get("symbol", symbol)) != symbol:
        return None
    if str(record.get("name", "")) != bridge.SYMBOLS[symbol]:
        return None
    price = bridge.parse_number(record.get("price"))
    if price is None or price <= 0:
        return None
    trade_date = str(record.get("trade_date", ""))
    trade_time = str(record.get("trade_time", ""))
    try:
        datetime.fromisoformat(trade_date)
        datetime.fromisoformat(f"{trade_date}T{trade_time}")
    except ValueError:
        return None
    if record.get("same_msgarray_verified") is not True:
        return None
    return bridge.QuoteCandidate(
        price=price,
        trade_date=trade_date,
        trade_time=trade_time,
        tlong=str(record.get("tlong")) if record.get("tlong") is not None else None,
        query_date=str(record.get("query_date")) if record.get("query_date") else None,
        query_time=str(record.get("query_time")) if record.get("query_time") else None,
        open=bridge.parse_number(record.get("open")),
        high=bridge.parse_number(record.get("high")),
        low=bridge.parse_number(record.get("low")),
        volume=bridge.parse_int(record.get("volume")),
        raw={},
    )


def load_quote_cache(path: Path = QUOTE_CACHE_PATH) -> dict[str, Any]:
    empty = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "updated_at": None,
        "quotes": {},
    }
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(data, dict) or data.get("schema_version") != CACHE_SCHEMA_VERSION:
        return empty
    quotes = data.get("quotes")
    if not isinstance(quotes, dict):
        return empty
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "updated_at": data.get("updated_at"),
        "quotes": quotes,
    }


def write_quote_cache(cache: dict[str, Any], path: Path = QUOTE_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def sample_summary(
    index: int,
    payload: dict[str, Any],
    symbol: str,
) -> dict[str, Any]:
    query = payload.get("queryTime") or {}
    item = None
    messages = payload.get("msgArray") or []
    if isinstance(messages, list):
        for row in messages:
            if isinstance(row, dict) and str(row.get("c", "")) == symbol:
                item = row
                break

    def parsed_date(value: Any) -> str | None:
        if not value:
            return None
        try:
            return bridge.compact_date_to_iso(str(value))
        except ValueError:
            return None

    return {
        "sample": index,
        "query_date": parsed_date(query.get("sysDate")),
        "query_time": query.get("sysTime"),
        "trade_date": parsed_date(item.get("d")) if item else None,
        "trade_time": item.get("t") if item else None,
        "tlong": item.get("tlong") if item else None,
        "z": item.get("z") if item else None,
        "open": bridge.parse_number(item.get("o")) if item else None,
        "high": bridge.parse_number(item.get("h")) if item else None,
        "low": bridge.parse_number(item.get("l")) if item else None,
        "volume": bridge.parse_int(item.get("v")) if item else None,
    }


def render_candidate_quote(
    symbol: str,
    candidate: bridge.QuoteCandidate,
    reference: datetime,
    *,
    status: str,
    origin: str,
    reused_from_cache: bool,
    live_result: dict[str, Any],
    cache_captured_at: str | None,
) -> dict[str, Any]:
    age = bridge.candidate_age_seconds(candidate, reference)
    return {
        "status": status,
        "name": bridge.SYMBOLS[symbol],
        "price": candidate.price,
        "trade_date": candidate.trade_date,
        "trade_time": candidate.trade_time,
        "tlong": candidate.tlong,
        "age_seconds": round(age, 1) if age is not None else None,
        "open": candidate.open,
        "high": candidate.high,
        "low": candidate.low,
        "volume": candidate.volume,
        "query_date": candidate.query_date,
        "query_time": candidate.query_time,
        "quote_origin": origin,
        "reused_from_cache": reused_from_cache,
        "cache_captured_at": cache_captured_at,
        "same_msgarray_verified": True,
        "live_fetch_status": live_result.get("status"),
        "sample_count": live_result.get("sample_count", 0),
        "samples": live_result.get("samples", []),
        "last_error": live_result.get("last_error"),
    }


def fetch_live_quote(
    symbol: str,
    reference: datetime | None = None,
) -> tuple[dict[str, Any], bridge.QuoteCandidate | None]:
    """Sample one symbol up to eight times and preserve full z+d+t provenance."""
    samples: list[dict[str, Any]] = []
    candidates: list[bridge.QuoteCandidate] = []
    last_error: str | None = None
    reference = reference or bridge.now_taipei()

    for i in range(bridge.MIS_MAX_SAMPLES):
        url = bridge.mis_url(symbol, i + 1)
        try:
            payload = bridge.http_json(url)
            if not isinstance(payload, dict):
                raise bridge.BridgeError("MIS response is not an object")
            samples.append(sample_summary(i + 1, payload, symbol))
            candidate = bridge.extract_quote_candidate(payload, symbol)
            if candidate is not None:
                candidates.append(candidate)
                age = bridge.candidate_age_seconds(candidate, bridge.now_taipei())
                # Stop only when the candidate is actually fresh. A numeric but stale
                # z must not prevent later samples from finding a newer trade.
                if age is not None and -FUTURE_TOLERANCE_SECONDS <= age <= QUOTE_MAX_AGE_SECONDS:
                    break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            samples.append({"sample": i + 1, "error": last_error})

        if i + 1 < bridge.MIS_MAX_SAMPLES:
            time.sleep(bridge.MIS_SAMPLE_INTERVAL_SECONDS)

    final_reference = bridge.now_taipei()
    valid: list[tuple[bridge.QuoteCandidate, float]] = []
    stale: list[tuple[bridge.QuoteCandidate, float]] = []
    for candidate in candidates:
        age = bridge.candidate_age_seconds(candidate, final_reference)
        if age is None or age < -FUTURE_TOLERANCE_SECONDS:
            continue
        if candidate.trade_date != final_reference.date().isoformat():
            stale.append((candidate, age))
        elif age <= QUOTE_MAX_AGE_SECONDS:
            valid.append((candidate, age))
        else:
            stale.append((candidate, age))

    if valid:
        candidate, age = max(valid, key=lambda item: candidate_key(item[0]))
        result = render_candidate_quote(
            symbol,
            candidate,
            final_reference,
            status="PASS",
            origin="LIVE_MIS",
            reused_from_cache=False,
            live_result={
                "status": "PASS",
                "sample_count": len(samples),
                "samples": samples,
                "last_error": last_error,
            },
            cache_captured_at=None,
        )
        return result, candidate

    if stale:
        candidate, age = max(stale, key=lambda item: candidate_key(item[0]))
        result = render_candidate_quote(
            symbol,
            candidate,
            final_reference,
            status="FAIL_STALE_TRADE",
            origin="LIVE_MIS",
            reused_from_cache=False,
            live_result={
                "status": "FAIL_STALE_TRADE",
                "sample_count": len(samples),
                "samples": samples,
                "last_error": last_error,
            },
            cache_captured_at=None,
        )
        return result, candidate

    status = (
        "FAIL_NO_RECENT_TRADE"
        if any("error" not in sample for sample in samples)
        else "FAIL_NETWORK"
    )
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


def newest_candidate(
    first: bridge.QuoteCandidate | None,
    second: bridge.QuoteCandidate | None,
) -> bridge.QuoteCandidate | None:
    if first is None:
        return second
    if second is None:
        return first
    return max((first, second), key=candidate_key)


def merge_live_and_cached_quote(
    symbol: str,
    live_result: dict[str, Any],
    live_candidate: bridge.QuoteCandidate | None,
    cached_record: Any,
    reference: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Select the newest verified trade while preserving original trade time."""
    cached_candidate = record_to_candidate(symbol, cached_record)
    selected = newest_candidate(live_candidate, cached_candidate)

    # Persist a newly observed legitimate MIS trade even when it is already stale.
    cache_update: dict[str, Any] | None = None
    if live_candidate is not None:
        if cached_candidate is None or candidate_key(live_candidate) >= candidate_key(cached_candidate):
            cache_update = candidate_to_record(symbol, live_candidate, reference)

    if selected is None:
        return live_result, cache_update

    age = bridge.candidate_age_seconds(selected, reference)
    if age is None or age < -FUTURE_TOLERANCE_SECONDS:
        return live_result, cache_update

    same_day = selected.trade_date == reference.date().isoformat()
    origin = "LIVE_MIS" if selected is live_candidate else "PERSISTED_MIS_CACHE"
    reused = origin == "PERSISTED_MIS_CACHE"
    cache_captured_at = (
        str(cached_record.get("captured_at"))
        if reused and isinstance(cached_record, dict) and cached_record.get("captured_at")
        else None
    )

    if same_day and age <= QUOTE_MAX_AGE_SECONDS:
        return (
            render_candidate_quote(
                symbol,
                selected,
                reference,
                status="PASS",
                origin=origin,
                reused_from_cache=reused,
                live_result=live_result,
                cache_captured_at=cache_captured_at,
            ),
            cache_update,
        )

    # A same-day or previous-day verified trade can be reported as stale, but
    # it never becomes eligible for a signal.
    stale_result = render_candidate_quote(
        symbol,
        selected,
        reference,
        status="FAIL_STALE_TRADE",
        origin=origin,
        reused_from_cache=reused,
        live_result=live_result,
        cache_captured_at=cache_captured_at,
    )
    return stale_result, cache_update


def build_quotes_concurrently(
    reference: datetime,
    cache_path: Path = QUOTE_CACHE_PATH,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cache = load_quote_cache(cache_path)
    cached_quotes = cache.get("quotes") or {}
    live: dict[str, tuple[dict[str, Any], bridge.QuoteCandidate | None]] = {}

    with ThreadPoolExecutor(max_workers=len(bridge.SYMBOLS)) as executor:
        futures = {
            executor.submit(fetch_live_quote, symbol, reference): symbol
            for symbol in bridge.SYMBOLS
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                live[symbol] = future.result()
            except Exception as exc:
                status = {
                    "status": "FAIL_NETWORK",
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
                    "live_fetch_status": "FAIL_NETWORK",
                    "sample_count": 0,
                    "samples": [],
                    "last_error": f"{type(exc).__name__}: {exc}",
                }
                live[symbol] = (status, None)

    final_reference = bridge.now_taipei()
    merged: dict[str, Any] = {}
    updated_symbols: list[str] = []
    reused_symbols: list[str] = []
    next_quotes = dict(cached_quotes)

    for symbol in bridge.SYMBOLS:
        live_result, live_candidate = live[symbol]
        merged_result, cache_update = merge_live_and_cached_quote(
            symbol,
            live_result,
            live_candidate,
            cached_quotes.get(symbol),
            final_reference,
        )
        merged[symbol] = merged_result
        if cache_update is not None:
            next_quotes[symbol] = cache_update
            updated_symbols.append(symbol)
        if merged_result.get("reused_from_cache"):
            reused_symbols.append(symbol)

    next_cache = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "updated_at": final_reference.isoformat(timespec="seconds"),
        "quotes": next_quotes,
    }
    write_quote_cache(next_cache, cache_path)
    diagnostics = {
        "enabled": True,
        "path": cache_path.name,
        "max_age_seconds": QUOTE_MAX_AGE_SECONDS,
        "updated_symbols": updated_symbols,
        "reused_symbols": reused_symbols,
        "parallel_sampling": True,
        "workers": len(bridge.SYMBOLS),
    }
    return merged, diagnostics


def build_latest() -> dict[str, Any]:
    start = bridge.now_taipei()
    try:
        latest_openapi = bridge.openapi_latest_rows()
        openapi_status = "PASS"
    except Exception as exc:
        latest_openapi = {}
        openapi_status = f"FAIL:{type(exc).__name__}:{exc}"

    quotes, quote_cache = build_quotes_concurrently(start)
    histories: dict[str, Any] = {}
    for symbol in bridge.SYMBOLS:
        rows, meta = bridge.update_history(symbol, latest_openapi.get(symbol))
        bridge.write_history(symbol, rows, meta)
        histories[symbol] = meta

    generated = bridge.now_taipei()
    transport_ready = any(q.get("status") == "PASS" for q in quotes.values()) and all(
        h.get("count", 0) >= bridge.MIN_HISTORY_GATE for h in histories.values()
    )

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
            "transport_ready": transport_ready,
            "analysis_ready": False,
            "phase": "MVP_DATA_BRIDGE",
            "note": (
                "Enhanced quote layer uses concurrent MIS sampling and a persisted "
                "same-day <=15-minute last-valid-trade cache. Phase 2 adds "
                "corporate-action adjustment and technical indicators."
            ),
            "quote_cache": quote_cache,
        },
        "quotes": quotes,
        "history": histories,
    }


def synthetic_candidate(
    *,
    price: float,
    trade_date: str,
    trade_time: str,
) -> bridge.QuoteCandidate:
    return bridge.QuoteCandidate(
        price=price,
        trade_date=trade_date,
        trade_time=trade_time,
        tlong=None,
        query_date=trade_date,
        query_time=trade_time,
        open=price,
        high=price,
        low=price,
        volume=1,
        raw={},
    )


def self_test() -> None:
    reference = datetime(2026, 9, 1, 11, 30, 0, tzinfo=bridge.TZ_TAIPEI)
    live_failure = {
        "status": "FAIL_NO_RECENT_TRADE",
        "name": bridge.SYMBOLS["2002"],
        "sample_count": 8,
        "samples": [],
        "last_error": None,
    }
    recent = synthetic_candidate(
        price=19.1,
        trade_date="2026-09-01",
        trade_time="11:20:00",
    )
    recent_record = candidate_to_record("2002", recent, reference)
    merged, update = merge_live_and_cached_quote(
        "2002", live_failure, None, recent_record, reference
    )
    assert merged["status"] == "PASS"
    assert merged["reused_from_cache"] is True
    assert merged["trade_time"] == "11:20:00"
    assert update is None

    stale = synthetic_candidate(
        price=19.0,
        trade_date="2026-09-01",
        trade_time="11:14:59",
    )
    stale_record = candidate_to_record("2002", stale, reference)
    merged, _ = merge_live_and_cached_quote(
        "2002", live_failure, None, stale_record, reference
    )
    assert merged["status"] == "FAIL_STALE_TRADE"

    previous_day = synthetic_candidate(
        price=19.0,
        trade_date="2026-08-31",
        trade_time="13:30:00",
    )
    previous_record = candidate_to_record("2002", previous_day, reference)
    merged, _ = merge_live_and_cached_quote(
        "2002", live_failure, None, previous_record, reference
    )
    assert merged["status"] == "FAIL_STALE_TRADE"

    live_newer = synthetic_candidate(
        price=19.2,
        trade_date="2026-09-01",
        trade_time="11:25:00",
    )
    direct = {
        "status": "PASS",
        "name": bridge.SYMBOLS["2002"],
        "sample_count": 2,
        "samples": [],
        "last_error": None,
    }
    merged, cache_update = merge_live_and_cached_quote(
        "2002", direct, live_newer, recent_record, reference
    )
    assert merged["status"] == "PASS"
    assert merged["quote_origin"] == "LIVE_MIS"
    assert cache_update is not None and cache_update["trade_time"] == "11:25:00"

    invalid_record = dict(recent_record)
    invalid_record["same_msgarray_verified"] = False
    assert record_to_candidate("2002", invalid_record) is None

    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.json"
        cache = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "updated_at": reference.isoformat(),
            "quotes": {"2002": recent_record},
        }
        write_quote_cache(cache, path)
        loaded = load_quote_cache(path)
        assert loaded["quotes"]["2002"]["trade_time"] == "11:20:00"

    print("QUOTE_LAYER_SELF_TEST_PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0

    payload = build_latest()
    bridge.LATEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "generated_at": payload["generated_at"],
                "quotes": {
                    symbol: {
                        "status": quote["status"],
                        "origin": quote.get("quote_origin"),
                        "trade_time": quote.get("trade_time"),
                    }
                    for symbol, quote in payload["quotes"].items()
                },
                "history": {
                    symbol: {
                        "count": meta["count"],
                        "gate": meta["history_gate"],
                    }
                    for symbol, meta in payload["history"].items()
                },
                "quote_cache": payload["bridge"]["quote_cache"],
                "transport_ready": payload["bridge"]["transport_ready"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
