#!/usr/bin/env python3
"""Corrected Phase-2 entrypoint using current TWSE corporate-action endpoints."""
from __future__ import annotations

import argparse
import json
import re
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


def corrected_fetch_corporate_actions(symbol: str, start: str, end: str):
    actions = []
    errors = []
    endpoint_families = {
        "EX_RIGHT_DIVIDEND": [
            ("https://www.twse.com.tw/rwd/zh/exRight/TWT49U", "startDate", "endDate"),
            ("https://www.twse.com.tw/exchangeReport/TWT49U", "strDate", "endDate"),
        ],
        "CAPITAL_REDUCTION": [
            ("https://www.twse.com.tw/exchangeReport/TWTAUU", "startDate", "endDate"),
            ("https://www.twse.com.tw/exchangeReport/TWTAUU", "strDate", "endDate"),
        ],
    }
    source_success = {kind: 0 for kind in endpoint_families}
    source_rows = {kind: 0 for kind in endpoint_families}
    source_market_rows = {kind: 0 for kind in endpoint_families}
    status_samples = {kind: [] for kind in endpoint_families}

    # Year-size windows are sufficient now that the real issue (ROC Chinese date format)
    # is fixed. This keeps the scheduled job fast while retaining full 3-year coverage.
    for d1, d2 in p.chunk_date_ranges(start, end, days=365):
        for kind, variants in endpoint_families.items():
            window_done = False
            last_problem = None
            for base, start_key, end_key in variants:
                params = urllib.parse.urlencode({"response": "json", start_key: d1, end_key: d2})
                url = base + "?" + params
                try:
                    payload = p.http_json(url)
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
                    if isinstance(payload, dict) and isinstance(fields, list) and isinstance(rows, list):
                        if rows and not fields:
                            last_problem = f"{kind}:{d1}-{d2}:rows_without_fields:{base}"
                            continue
                        parsed = p.parse_action_table(payload, symbol, kind) if rows else []
                        source_success[kind] += 1
                        source_market_rows[kind] += len(rows)
                        source_rows[kind] += len(parsed)
                        actions.extend(parsed)
                        window_done = True
                        break
                    last_problem = f"{kind}:{d1}-{d2}:unexpected_payload:{base}:{stat}"
                except Exception as exc:
                    last_problem = f"{kind}:{d1}-{d2}:{base}:{type(exc).__name__}:{exc}"
            if not window_done and last_problem:
                errors.append(last_problem)

    uniq = {}
    for a in actions:
        uniq[(a["date"], a["kind"], a["pre_close"], a["reference_price"])] = a
    result = sorted(uniq.values(), key=lambda x: (x["date"], x["kind"]))
    diagnostics = {
        "source_success_windows": source_success,
        "source_action_rows": source_rows,
        "source_market_rows": source_market_rows,
        "status_samples": status_samples,
    }
    return result, errors, diagnostics


def run():
    original_fetch = p.fetch_corporate_actions
    original_date_parser = p.roc_date_to_iso
    diagnostics_by_symbol = {}

    def adapter(symbol, start, end):
        actions, errors, diag = corrected_fetch_corporate_actions(symbol, start, end)
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
