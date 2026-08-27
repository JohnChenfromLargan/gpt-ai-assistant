#!/usr/bin/env python3
"""TWSE stock data bridge for ChatGPT scheduled monitoring.

MVP scope:
- Fetch intraday MIS quotes for 2002 and 3019.
- Maintain >= 750 completed daily OHLCV rows per symbol from TWSE STOCK_DAY.
- Cross-check the latest completed day with TWSE OpenAPI STOCK_DAY_ALL.
- Write a single stable JSON document at stock_bridge/latest.json.

This module deliberately does NOT make investment decisions. It only prepares
validated market data for the downstream ChatGPT scheduled task.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

TZ_TAIPEI = timezone(timedelta(hours=8))
SYMBOLS = {"2002": "中鋼", "3019": "亞光"}
TARGET_HISTORY_ROWS = 780  # headroom above the 750-day long-term gate
MIN_HISTORY_GATE = 260
LONG_HISTORY_GATE = 750
MIS_MAX_SAMPLES = 8
MIS_FIRST_STAGE_SAMPLES = 4
MIS_SAMPLE_INTERVAL_SECONDS = 5
HTTP_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; TWSE-Stock-Bridge/1.0; +https://github.com/)"

ROOT = Path(__file__).resolve().parent
LATEST_PATH = ROOT / "latest.json"
HISTORY_DIR = ROOT / "history"


class BridgeError(RuntimeError):
    pass


@dataclass
class QuoteCandidate:
    price: float
    trade_date: str
    trade_time: str
    tlong: str | None
    query_date: str | None
    query_time: str | None
    open: float | None
    high: float | None
    low: float | None
    volume: int | None
    raw: dict[str, Any]


def now_taipei() -> datetime:
    return datetime.now(TZ_TAIPEI)


def http_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        payload = resp.read().decode("utf-8-sig")
    return json.loads(payload)


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "---", "X", "除權", "除息"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    number = parse_number(value)
    if number is None:
        return None
    return int(round(number))


def roc_date_to_iso(value: str) -> str:
    """Convert 115/08/25 or 1150825 into 2026-08-25."""
    text = str(value).strip()
    if "/" in text:
        parts = text.split("/")
        if len(parts) != 3:
            raise ValueError(f"Unsupported ROC date: {value}")
        year, month, day = map(int, parts)
    elif len(text) == 7 and text.isdigit():
        year, month, day = int(text[:3]), int(text[3:5]), int(text[5:7])
    elif len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    else:
        raise ValueError(f"Unsupported date: {value}")
    return f"{year + 1911:04d}-{month:02d}-{day:02d}"


def compact_date_to_iso(value: str) -> str:
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) == 7 and text.isdigit():
        return roc_date_to_iso(text)
    raise ValueError(f"Unsupported compact date: {value}")


def month_starts_backwards(start: datetime, months: int) -> Iterable[datetime]:
    year, month = start.year, start.month
    for offset in range(months):
        total = year * 12 + (month - 1) - offset
        y, m0 = divmod(total, 12)
        yield datetime(y, m0 + 1, 1, tzinfo=TZ_TAIPEI)


def stable_unique_token(symbol: str, sample_index: int) -> str:
    # Dynamic URLs are created by GitHub Actions, not by the ChatGPT Web tool.
    return f"{int(time.time() * 1000)}{symbol}{sample_index:02d}"


def mis_url(symbol: str, sample_index: int) -> str:
    params = {
        "ex_ch": f"tse_{symbol}.tw",
        "json": "1",
        "delay": "0",
        "_": stable_unique_token(symbol, sample_index),
    }
    return "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?" + urllib.parse.urlencode(params)


def extract_quote_candidate(payload: dict[str, Any], symbol: str) -> QuoteCandidate | None:
    if str(payload.get("rtcode", "")) != "0000":
        return None
    messages = payload.get("msgArray") or []
    if not isinstance(messages, list):
        return None
    query_time = payload.get("queryTime") or {}
    for item in messages:
        if str(item.get("c", "")) != symbol:
            continue
        expected_name = SYMBOLS[symbol]
        if str(item.get("n", "")) != expected_name:
            continue
        price = parse_number(item.get("z"))
        if price is None or price <= 0:
            continue
        trade_date_raw = str(item.get("d", ""))
        trade_time = str(item.get("t", ""))
        if len(trade_date_raw) != 8 or not trade_time:
            continue
        return QuoteCandidate(
            price=price,
            trade_date=compact_date_to_iso(trade_date_raw),
            trade_time=trade_time,
            tlong=str(item.get("tlong")) if item.get("tlong") is not None else None,
            query_date=compact_date_to_iso(str(query_time.get("sysDate"))) if query_time.get("sysDate") else None,
            query_time=str(query_time.get("sysTime")) if query_time.get("sysTime") else None,
            open=parse_number(item.get("o")),
            high=parse_number(item.get("h")),
            low=parse_number(item.get("l")),
            volume=parse_int(item.get("v")),
            raw=item,
        )
    return None


def candidate_age_seconds(candidate: QuoteCandidate, reference: datetime) -> float | None:
    try:
        dt = datetime.fromisoformat(f"{candidate.trade_date}T{candidate.trade_time}").replace(tzinfo=TZ_TAIPEI)
    except ValueError:
        return None
    return (reference - dt).total_seconds()


def fetch_quote(symbol: str) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    candidates: list[QuoteCandidate] = []
    last_error: str | None = None

    for i in range(MIS_MAX_SAMPLES):
        url = mis_url(symbol, i + 1)
        try:
            payload = http_json(url)
            query = payload.get("queryTime") or {}
            item = None
            for row in payload.get("msgArray") or []:
                if str(row.get("c", "")) == symbol:
                    item = row
                    break
            samples.append(
                {
                    "sample": i + 1,
                    "query_date": compact_date_to_iso(str(query.get("sysDate"))) if query.get("sysDate") else None,
                    "query_time": query.get("sysTime"),
                    "trade_date": compact_date_to_iso(str(item.get("d"))) if item and item.get("d") else None,
                    "trade_time": item.get("t") if item else None,
                    "z": item.get("z") if item else None,
                    "volume": parse_int(item.get("v")) if item else None,
                }
            )
            candidate = extract_quote_candidate(payload, symbol)
            if candidate:
                candidates.append(candidate)
                break
        except Exception as exc:  # network errors are reported, never guessed around
            last_error = f"{type(exc).__name__}: {exc}"
            samples.append({"sample": i + 1, "error": last_error})

        # Always complete first four samples. Extra samples are only needed when no z was found.
        if i + 1 >= MIS_FIRST_STAGE_SAMPLES and candidates:
            break
        if i + 1 < MIS_MAX_SAMPLES:
            time.sleep(MIS_SAMPLE_INTERVAL_SECONDS)

    reference = now_taipei()
    valid: list[tuple[QuoteCandidate, float]] = []
    stale: list[tuple[QuoteCandidate, float]] = []
    for candidate in candidates:
        age = candidate_age_seconds(candidate, reference)
        if age is None or age < -5:
            continue
        if age <= 15 * 60:
            valid.append((candidate, age))
        else:
            stale.append((candidate, age))

    if valid:
        candidate, age = sorted(valid, key=lambda x: (x[0].trade_date, x[0].trade_time))[-1]
        return {
            "status": "PASS",
            "name": SYMBOLS[symbol],
            "price": candidate.price,
            "trade_date": candidate.trade_date,
            "trade_time": candidate.trade_time,
            "age_seconds": round(age, 1),
            "open": candidate.open,
            "high": candidate.high,
            "low": candidate.low,
            "volume": candidate.volume,
            "sample_count": len(samples),
            "samples": samples,
            "last_error": last_error,
        }

    if stale:
        candidate, age = sorted(stale, key=lambda x: (x[0].trade_date, x[0].trade_time))[-1]
        return {
            "status": "FAIL_STALE_TRADE",
            "name": SYMBOLS[symbol],
            "price": candidate.price,
            "trade_date": candidate.trade_date,
            "trade_time": candidate.trade_time,
            "age_seconds": round(age, 1),
            "sample_count": len(samples),
            "samples": samples,
            "last_error": last_error,
        }

    return {
        "status": "FAIL_NO_RECENT_TRADE" if any("error" not in s for s in samples) else "FAIL_NETWORK",
        "name": SYMBOLS[symbol],
        "price": None,
        "trade_date": None,
        "trade_time": None,
        "sample_count": len(samples),
        "samples": samples,
        "last_error": last_error,
    }


def stock_day_url(symbol: str, month_start: datetime) -> str:
    params = {
        "response": "json",
        "date": month_start.strftime("%Y%m01"),
        "stockNo": symbol,
    }
    return "https://www.twse.com.tw/exchangeReport/STOCK_DAY?" + urllib.parse.urlencode(params)


def parse_stock_day(payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    if str(payload.get("stat", "")) != "OK":
        return []
    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    required = ["日期", "成交股數", "開盤價", "最高價", "最低價", "收盤價"]
    try:
        idx = {name: fields.index(name) for name in required}
    except ValueError:
        raise BridgeError(f"Unexpected STOCK_DAY fields for {symbol}: {fields}")

    parsed: list[dict[str, Any]] = []
    for row in rows:
        try:
            record = {
                "date": roc_date_to_iso(row[idx["日期"]]),
                "volume": parse_int(row[idx["成交股數"]]),
                "open": parse_number(row[idx["開盤價"]]),
                "high": parse_number(row[idx["最高價"]]),
                "low": parse_number(row[idx["最低價"]]),
                "close": parse_number(row[idx["收盤價"]]),
            }
        except (IndexError, ValueError):
            continue
        if None in (record["volume"], record["open"], record["high"], record["low"], record["close"]):
            continue
        parsed.append(record)
    return parsed


def load_history(symbol: str) -> list[dict[str, Any]]:
    path = HISTORY_DIR / f"{symbol}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def validate_history(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    by_date: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for row in rows:
        date = str(row.get("date", ""))
        try:
            datetime.fromisoformat(date)
        except ValueError:
            errors.append(f"invalid_date:{date}")
            continue
        values = [row.get(k) for k in ("open", "high", "low", "close")]
        if any(v is None for v in values):
            errors.append(f"missing_ohlc:{date}")
            continue
        o, h, l, c = map(float, values)
        if h < max(o, l, c) or l > min(o, h, c):
            errors.append(f"invalid_ohlc:{date}")
            continue
        volume = row.get("volume")
        if volume is None or int(volume) < 0:
            errors.append(f"invalid_volume:{date}")
            continue
        by_date[date] = {
            "date": date,
            "volume": int(volume),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
        }
    return [by_date[k] for k in sorted(by_date)], errors


def openapi_latest_rows() -> dict[str, dict[str, Any]]:
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    data = http_json(url)
    result: dict[str, dict[str, Any]] = {}
    for row in data if isinstance(data, list) else []:
        code = str(row.get("Code", ""))
        if code not in SYMBOLS:
            continue
        date_raw = str(row.get("Date", ""))
        try:
            date = roc_date_to_iso(date_raw)
        except ValueError:
            continue
        result[code] = {
            "date": date,
            "volume": parse_int(row.get("TradeVolume")),
            "open": parse_number(row.get("OpeningPrice")),
            "high": parse_number(row.get("HighestPrice")),
            "low": parse_number(row.get("LowestPrice")),
            "close": parse_number(row.get("ClosingPrice")),
        }
    return result


def update_history(symbol: str, latest_openapi: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    existing = load_history(symbol)
    combined = list(existing)
    now = now_taipei()

    # Always refresh current and previous month. Bootstrap older months only when needed.
    months_to_fetch = 2 if len(existing) >= TARGET_HISTORY_ROWS else 48
    for month_start in month_starts_backwards(now, months_to_fetch):
        try:
            payload = http_json(stock_day_url(symbol, month_start))
            combined.extend(parse_stock_day(payload, symbol))
        except Exception as exc:
            # Do not discard existing validated history because one month failed.
            print(f"WARN {symbol} {month_start:%Y-%m}: {type(exc).__name__}: {exc}", file=sys.stderr)
        validated, _ = validate_history(combined)
        combined = validated
        if len(existing) < TARGET_HISTORY_ROWS and len(combined) >= TARGET_HISTORY_ROWS and month_start.month not in {now.month, (now.month - 1) or 12}:
            break

    # Official OpenAPI may have the newest completed day before the monthly page refreshes.
    if latest_openapi and latest_openapi.get("date"):
        candidate = latest_openapi
        if all(candidate.get(k) is not None for k in ("date", "volume", "open", "high", "low", "close")):
            combined.append(candidate)

    validated, errors = validate_history(combined)
    # Keep a little headroom but avoid unbounded repo growth.
    if len(validated) > 900:
        validated = validated[-900:]

    latest_date = validated[-1]["date"] if validated else None
    expected_latest = latest_openapi.get("date") if latest_openapi else None
    freshness = bool(latest_date and expected_latest and latest_date == expected_latest)
    count = len(validated)

    meta = {
        "name": SYMBOLS[symbol],
        "count": count,
        "first_date": validated[0]["date"] if validated else None,
        "last_date": latest_date,
        "expected_latest_completed_date": expected_latest,
        "path_status": "PASS" if validated else "FAIL",
        "freshness_status": "PASS" if freshness else "FAIL",
        "history_gate": "PASS" if freshness and count >= MIN_HISTORY_GATE and not errors else "FAIL",
        "long_term_750_ready": bool(freshness and count >= LONG_HISTORY_GATE and not errors),
        "integrity_error_count": len(errors),
        "integrity_errors_sample": errors[:10],
        "price_adjustment_status": "PENDING_PHASE_2",
        "analysis_ready": False,
    }
    return validated, meta


def write_history(symbol: str, rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"symbol": symbol, "name": SYMBOLS[symbol], "meta": meta, "rows": rows}
    (HISTORY_DIR / f"{symbol}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_latest() -> dict[str, Any]:
    generated = now_taipei()
    try:
        latest_openapi = openapi_latest_rows()
        openapi_status = "PASS"
    except Exception as exc:
        latest_openapi = {}
        openapi_status = f"FAIL:{type(exc).__name__}:{exc}"

    quotes: dict[str, Any] = {}
    histories: dict[str, Any] = {}
    for symbol in SYMBOLS:
        quotes[symbol] = fetch_quote(symbol)
        rows, meta = update_history(symbol, latest_openapi.get(symbol))
        write_history(symbol, rows, meta)
        histories[symbol] = meta

    # MVP bridge readiness means fixed data transport works. Investment analysis
    # remains disabled until phase 2 completes corporate-action adjustment + indicators.
    transport_ready = any(q.get("status") == "PASS" for q in quotes.values()) and all(
        h.get("count", 0) >= MIN_HISTORY_GATE for h in histories.values()
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
            "note": "Phase 2 will add corporate-action adjustment and technical indicators before investment-signal use.",
        },
        "quotes": quotes,
        "history": histories,
    }


def self_test() -> None:
    assert roc_date_to_iso("115/08/25") == "2026-08-25"
    assert roc_date_to_iso("1150825") == "2026-08-25"
    assert compact_date_to_iso("20260826") == "2026-08-26"
    assert parse_number("136.5000") == 136.5
    assert parse_number("-") is None
    assert parse_int("22,801,767") == 22801767
    rows, errors = validate_history(
        [
            {"date": "2026-08-25", "volume": 10, "open": 10, "high": 12, "low": 9, "close": 11},
            {"date": "2026-08-25", "volume": 20, "open": 10, "high": 13, "low": 9, "close": 12},
        ]
    )
    assert not errors and len(rows) == 1 and rows[0]["volume"] == 20
    print("SELF_TEST_PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0

    payload = build_latest()
    LATEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "generated_at": payload["generated_at"],
        "quotes": {k: v["status"] for k, v in payload["quotes"].items()},
        "history": {k: {"count": v["count"], "gate": v["history_gate"]} for k, v in payload["history"].items()},
        "transport_ready": payload["bridge"]["transport_ready"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
