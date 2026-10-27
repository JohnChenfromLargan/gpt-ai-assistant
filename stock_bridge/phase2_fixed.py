#!/usr/bin/env python3
"""Corrected Phase-2 entrypoint using current TWSE corporate-action endpoints."""
from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.parse

import phase2 as p


def robust_roc_date_to_iso(value: str) -> str:
    """Parse TWSE ROC/Gregorian dates, including forms like 115年07月24日."""
    s = str(value).strip()
    m = re.fullmatch(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        y, mo, d = map(int, m.groups())
        return f"{y + 1911:04d}-{mo:02d}-{d:02d}"
    if "/" in s:
        y, mo, d = map(int, s.split("/"))
        return f"{y + 1911:04d}-{mo:02d}-{d:02d}"
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    raise ValueError(f"unsupported TWSE date: {value}")


class MarketFetcher:
    """Share market responses within one run; stop on explicit access denial."""

    def __init__(self):
        self.cache = {}
        self.failures = {}
        self.blocked = None
        self.started = time.monotonic()
        self.requests = 0
        self.cache_hits = 0

    def get(self, url):
        if self.blocked is not None:
            raise self.blocked
        if url in self.cache:
            self.cache_hits += 1
            return self.cache[url]
        if url in self.failures:
            raise self.failures[url]
        for attempt in range(2):
            remaining = 90 - (time.monotonic() - self.started)
            if remaining <= 0:
                self.blocked = p.SourceFetchError("SOURCE_FETCH_BUDGET_EXHAUSTED", url)
                raise self.blocked
            try:
                self.requests += 1
                payload = p.http_json(url, timeout=min(15, remaining))
                self.cache[url] = payload
                return payload
            except p.SourceFetchError as exc:
                if exc.category in {"SOURCE_ACCESS_DENIED", "SOURCE_RATE_LIMITED"}:
                    self.blocked = exc
                if self.blocked is not None or not exc.retryable or attempt == 1:
                    self.failures[url] = exc
                    raise
                time.sleep(min(1, max(0, 90 - (time.monotonic() - self.started))))


def validated_action_table(payload, symbol, kind, d1, d2):
    if not isinstance(payload, dict) or payload.get("stat") != "OK":
        stat = payload.get("stat") if isinstance(payload, dict) else "NON_DICT"
        raise ValueError(f"SOURCE_INVALID_STATUS:{stat}")
    fields, rows = payload.get("fields"), payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise ValueError("SOURCE_INVALID_TABLE")
    aliases = (["資料日期", "除權息日期", "恢復買賣日期"],
               ["股票代號", "證券代號"],
               ["除權息前收盤價", "停止買賣前收盤價格", "停止買賣前收盤價"],
               ["除權息參考價", "恢復買賣參考價"])
    indices = [p.field_index(fields, a) for a in aliases]
    if any(i is None for i in indices):
        raise ValueError("SOURCE_MISSING_REQUIRED_FIELDS")
    if any(not isinstance(row, list) or len(row) <= max(indices) for row in rows):
        raise ValueError("SOURCE_MALFORMED_ROW")
    target_rows = [r for r in rows if str(r[indices[1]]).strip() == symbol]
    parsed = p.parse_action_table(payload, symbol, kind)
    if len(parsed) != len(target_rows):
        raise ValueError("SOURCE_UNPARSED_TARGET_ROW")
    for action in parsed:
        p.date.fromisoformat(action["date"])
        if not d1 <= action["date"].replace("-", "") <= d2:
            raise ValueError("SOURCE_ACTION_OUTSIDE_REQUESTED_RANGE")
        if action["name"] != p.SYMBOLS[symbol]:
            raise ValueError("SOURCE_ACTION_NAME_MISMATCH")
        if any(not math.isfinite(action[k]) or action[k] <= 0
               for k in ("pre_close", "reference_price", "back_adjust_factor")):
            raise ValueError("SOURCE_INVALID_ACTION_PRICE")
    return parsed


def corrected_fetch_corporate_actions(symbol: str, start: str, end: str, fetcher=None):
    fetcher = fetcher or MarketFetcher()
    actions = []
    errors = []
    endpoint_families = {
        "EX_RIGHT_DIVIDEND": [
            ("https://www.twse.com.tw/rwd/zh/exRight/TWT49U", "startDate", "endDate"),
            # The legacy route now also requires startDate. strDate is ignored
            # and can make a valid historical window look reversed server-side.
            ("https://www.twse.com.tw/exchangeReport/TWT49U", "startDate", "endDate"),
        ],
        "CAPITAL_REDUCTION": [
            ("https://www.twse.com.tw/exchangeReport/TWTAUU", "startDate", "endDate"),
        ],
    }
    source_success = {kind: 0 for kind in endpoint_families}
    source_rows = {kind: 0 for kind in endpoint_families}
    source_market_rows = {kind: 0 for kind in endpoint_families}
    status_samples = {kind: [] for kind in endpoint_families}
    endpoint_failures = []

    # Year-size windows are sufficient now that the real issue (ROC Chinese date format)
    # is fixed. This keeps the scheduled job fast while retaining full 3-year coverage.
    for d1, d2 in p.chunk_date_ranges(start, end, days=365):
        for kind, variants in endpoint_families.items():
            window_done = False
            window_problems = []
            for base, start_key, end_key in variants:
                params = urllib.parse.urlencode({"response": "json", start_key: d1, end_key: d2})
                url = base + "?" + params
                try:
                    payload = fetcher.get(url)
                    stat = str(payload.get("stat", "")) if isinstance(payload, dict) else "NON_DICT"
                    fields = payload.get("fields") or [] if isinstance(payload, dict) else []
                    rows = payload.get("data") or [] if isinstance(payload, dict) else []
                    if len(status_samples[kind]) < 8:
                        status_samples[kind].append({
                            "range": f"{d1}-{d2}",
                            "endpoint": base,
                            "stat": stat,
                            "market_rows": len(rows) if isinstance(rows, list) else None,
                            "fields": fields[:6] if isinstance(fields, list) else None,
                        })
                    parsed = validated_action_table(payload, symbol, kind, d1, d2)
                    source_success[kind] += 1
                    source_market_rows[kind] += len(rows)
                    source_rows[kind] += len(parsed)
                    actions.extend(parsed)
                    window_done = True
                    break
                except Exception as exc:
                    problem = f"{kind}:{d1}-{d2}:{base}:{type(exc).__name__}:{exc}"
                    window_problems.append(problem)
                    if len(endpoint_failures) < 16:
                        endpoint_failures.append(problem)
                    if fetcher.blocked is not None:
                        break
            if not window_done and window_problems:
                # A failing fallback must not hide the primary's actual error.
                errors.append(" | ".join(window_problems))

    uniq = {}
    event_prices = {}
    for a in actions:
        key = (a["date"], a["kind"])
        prices = (a["pre_close"], a["reference_price"])
        if key in event_prices and event_prices[key] != prices:
            errors.append(f"SOURCE_CONFLICTING_ACTION:{symbol}:{key}")
        event_prices[key] = prices
        uniq[(a["date"], a["kind"], a["pre_close"], a["reference_price"])] = a
    result = sorted(uniq.values(), key=lambda x: (x["date"], x["kind"]))
    diagnostics = {
        "source_success_windows": source_success,
        "source_action_rows": source_rows,
        "source_market_rows": source_market_rows,
        "status_samples": status_samples,
        "endpoint_failures_sample": endpoint_failures,
        "fetch_error_count": len(errors),
        "fetch_errors_sample": errors[:10],
        "http_request_count": fetcher.requests,
        "shared_response_cache_hits": fetcher.cache_hits,
        "circuit_open": fetcher.blocked is not None,
    }
    return result, errors, diagnostics


def run():
    original_fetch = p.fetch_corporate_actions
    original_date_parser = p.roc_date_to_iso
    diagnostics_by_symbol = {}
    fetcher = MarketFetcher()

    def adapter(symbol, start, end):
        actions, errors, diag = corrected_fetch_corporate_actions(symbol, start, end, fetcher)
        diagnostics_by_symbol[symbol] = diag
        return actions, errors

    p.fetch_corporate_actions = adapter
    p.roc_date_to_iso = robust_roc_date_to_iso
    try:
        data = p.run_phase2()
    finally:
        p.fetch_corporate_actions = original_fetch
        p.roc_date_to_iso = original_date_parser

    for symbol, diag in diagnostics_by_symbol.items():
        data["analytics"][symbol]["corporate_actions"]["source_diagnostics"] = diag

    failed = {s: a["corporate_actions"] for s, a in data["analytics"].items()
              if a.get("corporate_actions", {}).get("fetch_error_count", 0)}
    if failed:
        p.LATEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        details = {s: {k: a.get(k) for k in ("fetch_error_count", "fetch_errors_sample", "source_diagnostics")}
                   for s, a in failed.items()}
        raise RuntimeError("CORPORATE_ACTION_FETCH_FAILED: " + json.dumps(details, ensure_ascii=False))

    # Regression sentinel: official TWSE data contains 2002 ex-dividend on 2026-07-24.
    history_2002 = p.load_history("2002")
    if history_2002 and history_2002[0]["date"] <= "2026-07-24" <= history_2002[-1]["date"]:
        events = data["analytics"]["2002"]["corporate_actions"]["events"]
        if not any(e.get("date") == "2026-07-24" and e.get("kind") == "EX_RIGHT_DIVIDEND" for e in events):
            data["analytics"]["2002"]["corporate_actions"]["status"] = "FAIL"
            data["analytics"]["2002"]["analysis_ready"] = False
            data["history"]["2002"]["price_adjustment_status"] = "FAIL"
            data["history"]["2002"]["analysis_ready"] = False
            data["bridge"]["analysis_ready"] = False
            data["bridge"]["symbol_analysis_ready"]["2002"] = False
            p.LATEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise RuntimeError(
                "CORPORATE_ACTION_REGRESSION: missing official 2002 ex-dividend event 2026-07-24; "
                + json.dumps(diagnostics_by_symbol.get("2002", {}), ensure_ascii=False)
            )

    p.LATEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def self_test():
    p.self_test()
    assert robust_roc_date_to_iso("115年07月24日") == "2026-07-24"
    assert robust_roc_date_to_iso("115/07/24") == "2026-07-24"
    ranges = list(p.chunk_date_ranges("2025-07-01", "2026-08-15", days=365))
    assert ranges[0][0] == "20250701"
    assert ranges[-1][1] == "20260815"
    q = urllib.parse.urlencode({"response": "json", "startDate": "20260724", "endDate": "20260724"})
    assert "startDate=20260724" in q and "endDate=20260724" in q
    import unittest
    suite = unittest.defaultTestLoader.discover(str(p.ROOT), pattern="test_corporate_actions.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun == 0:
        raise RuntimeError("CORPORATE_ACTION_SELF_TEST_FAILED")
    print("PHASE2_FIXED_SELF_TEST_PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    data = run()
    print(json.dumps({
        "schema_version": data["schema_version"],
        "analysis_ready": data["bridge"]["analysis_ready"],
        "symbol_analysis_ready": data["bridge"]["symbol_analysis_ready"],
        "corporate_action_status": {s: data["analytics"][s]["corporate_actions"]["status"] for s in p.SYMBOLS},
        "corporate_action_counts": {s: data["analytics"][s]["corporate_actions"]["event_count"] for s in p.SYMBOLS},
        "corporate_action_source_diagnostics": {s: data["analytics"][s]["corporate_actions"].get("source_diagnostics") for s in p.SYMBOLS},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

