#!/usr/bin/env python3
"""Quote layer v3: multi-path TWSE MIS sampling with persisted verified cache.

Runs three official MIS paths concurrently:
1. combined dephased sampler for 2002 + 3019;
2. independent single-symbol sampler for 2002;
3. independent single-symbol sampler for 3019.

Only numeric z+d+t/tlong from one complete msgArray object is accepted. The
persisted cache remains same-day and <=15 minutes only.
"""
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import bridge
    import bridge_quote_layer as q1
    import bridge_quote_layer_v2 as q2
except ModuleNotFoundError:  # pragma: no cover
    from . import bridge  # type: ignore
    from . import bridge_quote_layer as q1  # type: ignore
    from . import bridge_quote_layer_v2 as q2  # type: ignore

CACHE_PATH = Path(
    os.environ.get("TWSE_QUOTE_CACHE_PATH", str(bridge.ROOT / "quote_cache.json"))
).expanduser()


def tag_samples(result: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for raw in result.get("samples") or []:
        sample = dict(raw) if isinstance(raw, dict) else {"raw": raw}
        sample["probe_phase"] = phase
        tagged.append(sample)
    return tagged


def empty_live_result(
    symbol: str,
    status: str,
    samples: list[dict[str, Any]],
    last_error: str | None,
) -> dict[str, Any]:
    return {
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
    }


def merge_probe_paths(
    symbol: str,
    combined_pair: tuple[dict[str, Any], bridge.QuoteCandidate | None],
    single_pair: tuple[dict[str, Any], bridge.QuoteCandidate | None],
    reference: datetime,
) -> tuple[dict[str, Any], bridge.QuoteCandidate | None, str]:
    combined_result, combined_candidate = combined_pair
    single_result, single_candidate = single_pair
    samples = tag_samples(combined_result, "COMBINED") + tag_samples(
        single_result, "SINGLE_SYMBOL"
    )
    last_error = single_result.get("last_error") or combined_result.get("last_error")

    available = [
        (phase, candidate)
        for phase, candidate in (
            ("COMBINED", combined_candidate),
            ("SINGLE_SYMBOL", single_candidate),
        )
        if candidate is not None
    ]

    if available:
        selected_phase, selected = max(
            available,
            key=lambda item: q1.candidate_key(item[1]),  # type: ignore[arg-type]
        )
        assert selected is not None
        age = bridge.candidate_age_seconds(selected, reference)
        if (
            selected.trade_date == reference.date().isoformat()
            and age is not None
            and -q1.FUTURE_TOLERANCE_SECONDS <= age <= q1.QUOTE_MAX_AGE_SECONDS
        ):
            status = "PASS"
        else:
            status = "FAIL_STALE_TRADE"
        result = q1.render_candidate_quote(
            symbol,
            selected,
            reference,
            status=status,
            origin="LIVE_MIS",
            reused_from_cache=False,
            live_result={
                "status": status,
                "sample_count": len(samples),
                "samples": samples,
                "last_error": last_error,
            },
            cache_captured_at=None,
        )
        result["live_probe_source"] = selected_phase
        result["combined_fetch_status"] = combined_result.get("status")
        result["single_fetch_status"] = single_result.get("status")
        return result, selected, selected_phase

    statuses = {combined_result.get("status"), single_result.get("status")}
    status = "FAIL_NETWORK" if statuses == {"FAIL_NETWORK"} else "FAIL_NO_RECENT_TRADE"
    result = empty_live_result(symbol, status, samples, last_error)
    result["live_probe_source"] = "NONE"
    result["combined_fetch_status"] = combined_result.get("status")
    result["single_fetch_status"] = single_result.get("status")
    return result, None, "NONE"


def run_multi_path_sampling() -> tuple[
    dict[str, tuple[dict[str, Any], bridge.QuoteCandidate | None]],
    dict[str, Any],
]:
    with ThreadPoolExecutor(max_workers=3) as executor:
        combined_future = executor.submit(q2.poll_combined)
        single_futures = {
            symbol: executor.submit(q1.fetch_live_quote, symbol)
            for symbol in bridge.SYMBOLS
        }
        combined_live, combined_diagnostics = combined_future.result()
        single_live = {
            symbol: future.result() for symbol, future in single_futures.items()
        }

    reference = bridge.now_taipei()
    merged: dict[str, tuple[dict[str, Any], bridge.QuoteCandidate | None]] = {}
    selected_paths: dict[str, str] = {}
    single_counts: dict[str, int] = {}
    for symbol in bridge.SYMBOLS:
        result, candidate, selected_path = merge_probe_paths(
            symbol,
            combined_live[symbol],
            single_live[symbol],
            reference,
        )
        merged[symbol] = (result, candidate)
        selected_paths[symbol] = selected_path
        single_counts[symbol] = int(single_live[symbol][0].get("sample_count") or 0)

    diagnostics = {
        "mode": "MULTIPATH_COMBINED_AND_SINGLE_MIS",
        "combined_request": True,
        "parallel_sampling": True,
        "workers": 3,
        "paths": ["COMBINED_DEPHASED", "SINGLE_2002", "SINGLE_3019"],
        "selected_live_path": selected_paths,
        "single_sample_count": single_counts,
        "combined_diagnostics": combined_diagnostics,
    }
    return merged, diagnostics


def build_quotes() -> tuple[dict[str, Any], dict[str, Any]]:
    cache = q1.load_quote_cache(CACHE_PATH)
    cached = cache.get("quotes") or {}
    live, diagnostics = run_multi_path_sampling()
    reference = bridge.now_taipei()
    merged: dict[str, Any] = {}
    next_cache = dict(cached)
    updated: list[str] = []
    reused: list[str] = []

    for symbol in bridge.SYMBOLS:
        live_result, live_candidate = live[symbol]
        quote, cache_update = q1.merge_live_and_cached_quote(
            symbol,
            live_result,
            live_candidate,
            cached.get(symbol),
            reference,
        )
        quote["live_probe_source"] = live_result.get("live_probe_source")
        quote["combined_fetch_status"] = live_result.get("combined_fetch_status")
        quote["single_fetch_status"] = live_result.get("single_fetch_status")
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
        "max_age_seconds": q1.QUOTE_MAX_AGE_SECONDS,
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
            "transport_ready": any(
                quote.get("status") == "PASS" for quote in quotes.values()
            )
            and all(
                history.get("count", 0) >= bridge.MIN_HISTORY_GATE
                for history in histories.values()
            ),
            "analysis_ready": False,
            "phase": "MVP_DATA_BRIDGE",
            "note": (
                "Quote layer v3 combines dephased multi-symbol and independent "
                "single-symbol MIS polling with a persisted verified-trade cache."
            ),
            "quote_cache": quote_cache,
        },
        "quotes": quotes,
        "history": histories,
    }


def synthetic_candidate(
    symbol: str, price: float, trade_time: str
) -> bridge.QuoteCandidate:
    return bridge.QuoteCandidate(
        price=price,
        trade_date="2026-09-03",
        trade_time=trade_time,
        tlong=None,
        query_date="2026-09-03",
        query_time=trade_time,
        open=price,
        high=price,
        low=price,
        volume=1,
        raw={"c": symbol, "n": bridge.SYMBOLS[symbol]},
    )


def self_test() -> None:
    reference = datetime(2026, 9, 3, 9, 30, 0, tzinfo=bridge.TZ_TAIPEI)
    base = {
        "status": "FAIL_NO_RECENT_TRADE",
        "samples": [{"sample": 1, "z": "-"}],
        "last_error": None,
    }
    combined_candidate = synthetic_candidate("2002", 19.0, "09:29:40")
    single_candidate = synthetic_candidate("2002", 19.1, "09:29:55")
    result, selected, phase = merge_probe_paths(
        "2002",
        ({**base, "status": "PASS"}, combined_candidate),
        ({**base, "status": "PASS"}, single_candidate),
        reference,
    )
    assert result["status"] == "PASS"
    assert selected is single_candidate
    assert phase == "SINGLE_SYMBOL"
    assert result["price"] == 19.1
    assert result["sample_count"] == 2

    result, selected, phase = merge_probe_paths(
        "3019",
        (base, None),
        (base, None),
        reference,
    )
    assert result["status"] == "FAIL_NO_RECENT_TRADE"
    assert selected is None
    assert phase == "NONE"
    print("QUOTE_LAYER_V3_SELF_TEST_PASS")


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
                        "probe": quote.get("live_probe_source"),
                        "trade_time": quote.get("trade_time"),
                    }
                    for symbol, quote in payload["quotes"].items()
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
