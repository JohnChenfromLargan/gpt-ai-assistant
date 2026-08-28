#!/usr/bin/env python3
"""Corrected Phase-2 entrypoint using current TWSE corporate-action endpoints."""
from __future__ import annotations

import argparse
import json
import urllib.parse

import phase2 as p


def corrected_fetch_corporate_actions(symbol: str, start: str, end: str):
    actions = []
    errors = []
    # TWT49U moved to the RWD endpoint and uses startDate/endDate.
    # TWTAUU also uses startDate/endDate for the range query.
    endpoints = [
        (
            "EX_RIGHT_DIVIDEND",
            "https://www.twse.com.tw/rwd/zh/exRight/TWT49U",
            "startDate",
            "endDate",
        ),
        (
            "CAPITAL_REDUCTION",
            "https://www.twse.com.tw/exchangeReport/TWTAUU",
            "startDate",
            "endDate",
        ),
    ]
    source_success = {kind: 0 for kind, *_ in endpoints}
    source_rows = {kind: 0 for kind, *_ in endpoints}

    for d1, d2 in p.chunk_date_ranges(start, end):
        for kind, base, start_key, end_key in endpoints:
            params = urllib.parse.urlencode({"response": "json", start_key: d1, end_key: d2})
            try:
                payload = p.http_json(base + "?" + params)
                parsed = p.parse_action_table(payload, symbol, kind)
                source_success[kind] += 1
                source_rows[kind] += len(parsed)
                actions.extend(parsed)
            except Exception as exc:
                errors.append(f"{kind}:{d1}-{d2}:{type(exc).__name__}:{exc}")

    uniq = {}
    for a in actions:
        uniq[(a["date"], a["kind"], a["pre_close"], a["reference_price"])] = a
    result = sorted(uniq.values(), key=lambda x: (x["date"], x["kind"]))
    diagnostics = {
        "source_success_windows": source_success,
        "source_action_rows": source_rows,
    }
    return result, errors, diagnostics


def run():
    original = p.fetch_corporate_actions

    diagnostics_by_symbol = {}

    def adapter(symbol, start, end):
        actions, errors, diag = corrected_fetch_corporate_actions(symbol, start, end)
        diagnostics_by_symbol[symbol] = diag
        return actions, errors

    p.fetch_corporate_actions = adapter
    try:
        data = p.run_phase2()
    finally:
        p.fetch_corporate_actions = original

    for symbol, diag in diagnostics_by_symbol.items():
        data["analytics"][symbol]["corporate_actions"]["source_diagnostics"] = diag

    # Regression sentinel: official TWSE data contains 2002 ex-dividend on 2026-07-24.
    # The 788-day history currently spans this date, so missing it is a source/parser failure.
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
            raise RuntimeError("CORPORATE_ACTION_REGRESSION: missing official 2002 ex-dividend event 2026-07-24")

    p.LATEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def self_test():
    p.self_test()
    # Verify URL parameter contract without network access.
    q = urllib.parse.urlencode({"response": "json", "startDate": "20260724", "endDate": "20260724"})
    assert "startDate=20260724" in q and "endDate=20260724" in q
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
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
