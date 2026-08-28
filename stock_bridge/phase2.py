#!/usr/bin/env python3
"""Phase 2 analytics for the TWSE stock bridge.

Reads Phase-1 raw TWSE data, removes known corporate-action price discontinuities,
computes technical indicators and multi-horizon return statistics, then enriches
stock_bridge/latest.json. It never makes buy/sell decisions.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ_TAIPEI = timezone(timedelta(hours=8))
SYMBOLS = {"2002": "中鋼", "3019": "亞光"}
ROOT = Path(__file__).resolve().parent
LATEST_PATH = ROOT / "latest.json"
HISTORY_DIR = ROOT / "history"
HTTP_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; TWSE-Stock-Bridge/2.0; +https://github.com/)"
HORIZONS = [10, 20, 22, 40, 60, 120, 240]


def http_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def parse_number(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in {"-", "--", "---", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_date_to_iso(v: str) -> str:
    s = str(v).strip()
    if "/" in s:
        y, m, d = map(int, s.split("/"))
        return f"{y+1911:04d}-{m:02d}-{d:02d}"
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3])+1911:04d}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    raise ValueError(s)


def chunk_date_ranges(start: str, end: str, days: int = 365):
    a = date.fromisoformat(start)
    b = date.fromisoformat(end)
    cur = a
    while cur <= b:
        stop = min(b, cur + timedelta(days=days - 1))
        yield cur.strftime("%Y%m%d"), stop.strftime("%Y%m%d")
        cur = stop + timedelta(days=1)


def field_index(fields: list[str], aliases: list[str]) -> int | None:
    normalized = [str(x).strip().replace("\n", "") for x in fields]
    for alias in aliases:
        if alias in normalized:
            return normalized.index(alias)
    return None


def parse_action_table(payload: dict[str, Any], symbol: str, kind: str) -> list[dict[str, Any]]:
    if str(payload.get("stat", "")) not in {"OK", ""}:
        return []
    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    if not isinstance(fields, list) or not isinstance(rows, list):
        return []

    i_date = field_index(fields, ["資料日期", "除權息日期", "恢復買賣日期"])
    i_code = field_index(fields, ["股票代號", "證券代號"])
    i_name = field_index(fields, ["股票名稱", "名稱", "證券名稱"])
    i_pre = field_index(fields, ["除權息前收盤價", "停止買賣前收盤價格", "停止買賣前收盤價"])
    i_ref = field_index(fields, ["除權息參考價", "恢復買賣參考價"])
    i_type = field_index(fields, ["權/息", "除權息", "減資原因"])
    if None in {i_date, i_code, i_pre, i_ref}:
        raise RuntimeError(f"unexpected {kind} fields: {fields}")

    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            if str(row[i_code]).strip() != symbol:
                continue
            pre = parse_number(row[i_pre])
            ref = parse_number(row[i_ref])
            if not pre or not ref or pre <= 0 or ref <= 0:
                continue
            d = roc_date_to_iso(row[i_date])
            factor = ref / pre
            if factor <= 0 or factor > 5:
                continue
            out.append({
                "date": d,
                "kind": kind,
                "name": str(row[i_name]).strip() if i_name is not None else SYMBOLS[symbol],
                "type": str(row[i_type]).strip() if i_type is not None else kind,
                "pre_close": pre,
                "reference_price": ref,
                "back_adjust_factor": factor,
            })
        except (IndexError, ValueError):
            continue
    return out


def fetch_corporate_actions(symbol: str, start: str, end: str) -> tuple[list[dict[str, Any]], list[str]]:
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    endpoints = [
        ("EX_RIGHT_DIVIDEND", "https://www.twse.com.tw/exchangeReport/TWT49U"),
        ("CAPITAL_REDUCTION", "https://www.twse.com.tw/exchangeReport/TWTAUU"),
    ]
    for d1, d2 in chunk_date_ranges(start, end):
        for kind, base in endpoints:
            params = urllib.parse.urlencode({"response": "json", "strDate": d1, "endDate": d2})
            try:
                payload = http_json(base + "?" + params)
                actions.extend(parse_action_table(payload, symbol, kind))
            except Exception as exc:
                errors.append(f"{kind}:{d1}-{d2}:{type(exc).__name__}:{exc}")
    # Exact duplicate removal.
    uniq: dict[tuple[str, str, float, float], dict[str, Any]] = {}
    for a in actions:
        uniq[(a["date"], a["kind"], a["pre_close"], a["reference_price"])] = a
    return sorted(uniq.values(), key=lambda x: (x["date"], x["kind"])), errors


def load_history(symbol: str) -> list[dict[str, Any]]:
    p = HISTORY_DIR / f"{symbol}.json"
    obj = json.loads(p.read_text(encoding="utf-8"))
    return list(obj.get("rows") or [])


def adjust_history(rows: list[dict[str, Any]], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Back-adjust all dates before each action so the series is continuous on the latest price basis.
    action_by_date: dict[str, float] = {}
    for a in actions:
        action_by_date[a["date"]] = action_by_date.get(a["date"], 1.0) * float(a["back_adjust_factor"])

    cumulative = 1.0
    out_rev: list[dict[str, Any]] = []
    for row in reversed(rows):
        d = row["date"]
        # The action factor applies to dates strictly before the action date.
        r = dict(row)
        for k in ("open", "high", "low", "close"):
            r[k] = round(float(r[k]) * cumulative, 6)
        r["adjustment_factor"] = round(cumulative, 10)
        out_rev.append(r)
        if d in action_by_date:
            cumulative *= action_by_date[d]
    return list(reversed(out_rev))


def unresolved_discontinuities(rows: list[dict[str, Any]], action_dates: set[str]) -> list[dict[str, Any]]:
    out = []
    for prev, cur in zip(rows, rows[1:]):
        pc = float(prev["close"])
        op = float(cur["open"])
        if pc <= 0:
            continue
        jump = abs(op / pc - 1.0)
        # Normal Taiwan daily limits make >20% an excellent safety tripwire for unresolved actions.
        if jump > 0.20 and cur["date"] not in action_dates:
            out.append({"date": cur["date"], "previous_close": pc, "open": op, "gap_pct": round(jump * 100, 3)})
    return out


def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def ema_series(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (n + 1.0)
    out = [values[0]]
    for x in values[1:]:
        out.append(alpha * x + (1 - alpha) * out[-1])
    return out


def rsi_wilder(values: list[float], n: int = 14) -> float | None:
    if len(values) <= n:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr_wilder(rows: list[dict[str, Any]], n: int = 14) -> float | None:
    if len(rows) <= n:
        return None
    trs = []
    for prev, cur in zip(rows, rows[1:]):
        h, l, pc = float(cur["high"]), float(cur["low"]), float(prev["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def technical(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(r["close"]) for r in rows]
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_series = [a - b for a, b in zip(ema12, ema26)]
    signal_series = ema_series(macd_series, 9)
    macd = macd_series[-1] if macd_series else None
    signal = signal_series[-1] if signal_series else None
    return {
        "date": rows[-1]["date"] if rows else None,
        "ma5": sma(closes, 5),
        "ma20": sma(closes, 20),
        "ma60": sma(closes, 60),
        "ma120": sma(closes, 120),
        "ma240": sma(closes, 240),
        "rsi14": rsi_wilder(closes, 14),
        "macd": macd,
        "macd_signal": signal,
        "macd_histogram": (macd - signal) if macd is not None and signal is not None else None,
        "atr14": atr_wilder(rows, 14),
    }


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    x = (len(vals) - 1) * p
    lo, hi = math.floor(x), math.ceil(x)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (x - lo)


def horizon_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(r["close"]) for r in rows]
    out: dict[str, Any] = {}
    for h in HORIZONS:
        rets = [(closes[i + h] / closes[i] - 1.0) * 100.0 for i in range(len(closes) - h)]
        out[str(h)] = {
            "samples": len(rets),
            "p10_pct": percentile(rets, 0.10),
            "p25_pct": percentile(rets, 0.25),
            "median_pct": percentile(rets, 0.50),
            "p75_pct": percentile(rets, 0.75),
            "p90_pct": percentile(rets, 0.90),
            "mean_pct": statistics.fmean(rets) if rets else None,
            "positive_probability": (sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
        }
    for h in (20, 60, 120, 240):
        window = closes[-h:] if len(closes) >= h else closes
        out[f"current_{h}d_range"] = {
            "low": min(window) if window else None,
            "high": max(window) if window else None,
        }
    return out


def provisional_rows(adjusted: list[dict[str, Any]], quote: dict[str, Any]) -> list[dict[str, Any]] | None:
    if quote.get("status") != "PASS" or quote.get("price") is None:
        return None
    qdate = quote.get("trade_date")
    price = float(quote["price"])
    o = quote.get("open") if quote.get("open") is not None else price
    h = quote.get("high") if quote.get("high") is not None else price
    l = quote.get("low") if quote.get("low") is not None else price
    row = {"date": qdate, "open": float(o), "high": float(h), "low": float(l), "close": price, "volume": int(quote.get("volume") or 0)}
    base = [r for r in adjusted if r["date"] != qdate]
    return base + [row]


def run_phase2() -> dict[str, Any]:
    latest = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    latest.setdefault("analytics", {})
    all_ready = True
    per_symbol_ready = {}

    for symbol in SYMBOLS:
        raw = load_history(symbol)
        if len(raw) < 260:
            latest["analytics"][symbol] = {"analysis_ready": False, "reason": "INSUFFICIENT_HISTORY"}
            all_ready = False
            per_symbol_ready[symbol] = False
            continue
        start, end = raw[0]["date"], raw[-1]["date"]
        actions, action_errors = fetch_corporate_actions(symbol, start, end)
        adjusted = adjust_history(raw, actions)
        unresolved = unresolved_discontinuities(raw, {a["date"] for a in actions})
        completed_tech = technical(adjusted)
        p_rows = provisional_rows(adjusted, latest.get("quotes", {}).get(symbol, {}))
        intraday_tech = technical(p_rows) if p_rows else None
        stats = horizon_stats(adjusted)

        corp_status = "PASS" if not action_errors and not unresolved else "FAIL"
        indicators_ok = all(completed_tech.get(k) is not None for k in ("ma20", "ma60", "ma120", "ma240", "rsi14", "macd", "macd_signal", "atr14"))
        hist_meta = latest.get("history", {}).get(symbol, {})
        ready = bool(hist_meta.get("history_gate") == "PASS" and hist_meta.get("long_term_750_ready") and corp_status == "PASS" and indicators_ok)
        per_symbol_ready[symbol] = ready
        all_ready = all_ready and ready

        latest["analytics"][symbol] = {
            "analysis_ready": ready,
            "corporate_actions": {
                "status": corp_status,
                "event_count": len(actions),
                "events": actions,
                "fetch_error_count": len(action_errors),
                "fetch_errors_sample": action_errors[:10],
                "unresolved_discontinuities": unresolved[:20],
                "method": "back_adjust_prior_ohlc_by_reference_price/pre_close; raw volume retained",
            },
            "technical_completed_day": completed_tech,
            "technical_intraday_estimate": intraday_tech,
            "horizon_statistics": stats,
        }
        hist_meta["price_adjustment_status"] = corp_status
        hist_meta["analysis_ready"] = ready

    latest["bridge"]["phase"] = "PHASE2_ANALYTICS"
    latest["bridge"]["analysis_ready"] = all_ready
    latest["bridge"]["symbol_analysis_ready"] = per_symbol_ready
    latest["bridge"]["note"] = "Corporate-action adjustment + MA/RSI/MACD/ATR + multi-horizon statistics applied; bridge itself does not issue investment signals."
    latest["schema_version"] = "2.0"
    LATEST_PATH.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return latest


def self_test() -> None:
    rows = [
        {"date": "2026-01-01", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
        {"date": "2026-01-02", "open": 90, "high": 92, "low": 89, "close": 91, "volume": 1},
    ]
    adj = adjust_history(rows, [{"date": "2026-01-02", "back_adjust_factor": 0.9}])
    assert abs(adj[0]["close"] - 90.0) < 1e-9
    assert abs(adj[1]["close"] - 91.0) < 1e-9
    vals = [float(i) for i in range(1, 301)]
    assert sma(vals, 20) is not None
    assert rsi_wilder(vals, 14) == 100.0
    sample_rows = [{"date": f"2026-01-{(i%28)+1:02d}", "open": x, "high": x+1, "low": x-1, "close": x, "volume": 1} for i, x in enumerate(vals)]
    t = technical(sample_rows)
    assert t["ma240"] is not None and t["atr14"] is not None and t["macd"] is not None
    print("PHASE2_SELF_TEST_PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    data = run_phase2()
    print(json.dumps({
        "schema_version": data["schema_version"],
        "analysis_ready": data["bridge"]["analysis_ready"],
        "symbol_analysis_ready": data["bridge"]["symbol_analysis_ready"],
        "corporate_action_status": {s: data["analytics"][s]["corporate_actions"]["status"] for s in SYMBOLS},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
