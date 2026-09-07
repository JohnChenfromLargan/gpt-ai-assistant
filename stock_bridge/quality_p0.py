"""Fail-closed data helpers. Candidate P0 fixes; no network or trading actions.

Calendar/endpoint provenance is an explicit input dependency, not inferred from
whatever market data happens to be cached. These helpers do not validate the
predictive performance of any investment strategy.
"""
from __future__ import annotations
import copy
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

TZ = timezone(timedelta(hours=8))
FIELDS = ('open', 'high', 'low', 'close')


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(str(value).strip().replace(',', ''))
        return x if math.isfinite(x) else None
    except (ValueError, TypeError, OverflowError):
        return None


def integer(value: Any) -> int | None:
    x = number(value)
    return int(x) if x is not None and x.is_integer() else None


def iso_day(value: Any) -> str:
    text = str(value)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        raise ValueError('invalid ISO calendar date')
    return date.fromisoformat(text).isoformat()


def twse_day(value: Any) -> str:
    text = str(value).strip()
    match = re.fullmatch(r'(\d{2,4})[年/](\d{1,2})[月/](\d{1,2})日?', text)
    if match:
        y, m, d = map(int, match.groups())
        y = y + 1911 if y < 1911 else y
    elif re.fullmatch(r'\d{7,8}', text):
        n = len(text) - 4
        y, m, d = int(text[:n]), int(text[n:n+2]), int(text[n+2:])
        y = y + 1911 if n == 3 else y
    else:
        return iso_day(text)
    return date(y, m, d).isoformat()


def reject_constant(value: str) -> None:
    raise ValueError(f'non-finite JSON constant: {value}')


def loads(text: str) -> Any:
    # Also disallow duplicate keys: an attacker/error must not change meanings
    # by placing two copies of a Gate or price key into a JSON object.
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f'duplicate JSON key: {key}')
            obj[key] = value
        return obj
    result = json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique)
    ensure_finite(result)  # Covers 1e999, which is not handled by parse_constant.
    return result


def ensure_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('non-finite numeric value')
    if isinstance(value, dict):
        for item in value.values():
            ensure_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            ensure_finite(item)


def dumps(value: Any, **kwargs: Any) -> str:
    kwargs['allow_nan'] = False
    return json.dumps(value, **kwargs)


def normal_bar(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError('row must be object')
    d = iso_day(raw.get('date'))
    prices = {key: number(raw.get(key)) for key in FIELDS}
    if any(x is None or x <= 0 for x in prices.values()):
        raise ValueError(f'invalid_ohlc:{d}')
    o, h, l, c = (prices[k] for k in FIELDS)
    if not l <= min(o, c) <= max(o, c) <= h:
        raise ValueError(f'invalid_ohlc_order:{d}')
    volume = integer(raw.get('volume'))
    if volume is None or volume < 0:
        raise ValueError(f'invalid_volume:{d}')
    return {'date': d, **prices, 'volume': volume}


def history_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    by_day = {}
    quarantine = set()
    errors = []
    for index, raw in enumerate(rows):
        try:
            row = normal_bar(raw)
        except (ValueError, TypeError) as exc:
            errors.append(f'row[{index}]:{exc}')
            continue
        day = row['date']
        if day in quarantine:
            continue
        if day in by_day and row != by_day[day]:
            quarantine.add(day)
            by_day.pop(day)
            errors.append(f'conflicting_duplicate:{day}')
        else:
            by_day[day] = row
    return [by_day[k] for k in sorted(by_day)], errors


def latest_crosscheck(rows: list[dict[str, Any]], latest: Any,
                      expected: str | None, today: str) -> str:
    if expected is None:
        return 'UNVERIFIED_CALENDAR'
    expected = iso_day(expected)
    today = iso_day(today)
    if expected >= today:
        return 'FAIL_CALENDAR_EXPECTATION'
    if not rows or rows[-1]['date'] != expected:
        return 'FAIL_LAST_COMPLETED_DAY'
    if latest is None:
        return 'UNAVAILABLE'
    try:
        candidate = normal_bar(latest)
    except (ValueError, TypeError):
        return 'FAIL_OPENAPI_SCHEMA'
    if candidate['date'] == today:
        return 'DEFER_CURRENT_DAY'
    if candidate['date'] != expected:
        return 'FAIL_OPENAPI_DATE'
    return 'PASS' if candidate == rows[-1] else 'FAIL_OHLCV_MISMATCH'


def action_table(payload: Any) -> tuple[list[str], list[list[Any]]]:
    if not isinstance(payload, dict) or payload.get('stat') != 'OK':
        raise ValueError('ACTION_SOURCE_STATUS_NOT_OK')
    fields, rows = payload.get('fields'), payload.get('data')
    if not isinstance(fields, list) or not fields or not all(isinstance(x, str) for x in fields):
        raise ValueError('ACTION_SOURCE_FIELDS_INVALID')
    if len(fields) != len(set(fields)):
        raise ValueError('ACTION_SOURCE_DUPLICATE_FIELDS')
    if not isinstance(rows, list) or any(not isinstance(r, list) or len(r) != len(fields) for r in rows):
        raise ValueError('ACTION_SOURCE_ROWS_INVALID')
    # OK plus the complete expected field schema is acceptable even when empty.
    # Unspecified/error/no-data strings are not silently accepted.
    return fields, rows


def adjusted_history(rows: list[dict[str, Any]], actions: list[dict[str, Any]],
                     as_of: str) -> list[dict[str, Any]]:
    as_of = iso_day(as_of)
    clean, errors = history_rows(rows)
    if errors or len(clean) != len(rows):
        raise ValueError('invalid/duplicate adjustment input history')
    if any(r['date'] > as_of for r in clean):
        raise ValueError('history beyond valuation date')
    events = {}
    for event in actions:
        day = iso_day(event.get('date'))
        factor = number(event.get('back_adjust_factor'))
        if factor is None or factor <= 0:
            raise ValueError('invalid adjustment factor')
        key = (day, str(event.get('kind', 'UNSPECIFIED')))
        if key in events and events[key] != factor:
            raise ValueError('conflicting adjustment events')
        events[key] = factor
    output = []
    for row in clean:
        # No dependency on a raw bar existing on the effective date.
        factor = math.prod(f for (day, _), f in events.items() if row['date'] < day <= as_of)
        result = dict(row)
        for key in FIELDS:
            result[key] = round(row[key] * factor, 6)
            if not math.isfinite(result[key]) or result[key] <= 0:
                raise ValueError('invalid adjusted price')
        result['adjustment_factor'] = factor
        output.append(result)
    return output


def price_ranges(rows: list[dict[str, Any]], horizons=(20, 60, 120, 240)) -> dict[str, Any]:
    out = {}
    for horizon in horizons:
        window = rows[-horizon:]
        closes = [number(r.get('close')) for r in window]
        highs = [number(r.get('high')) for r in window]
        lows = [number(r.get('low')) for r in window]
        if any(x is None or x <= 0 for x in closes + highs + lows):
            raise ValueError('invalid range input')
        close_range = {'low': min(closes) if closes else None,
                       'high': max(closes) if closes else None}
        out[f'current_{horizon}d_close_range'] = close_range
        out[f'current_{horizon}d_ohlc_range'] = {
            'low': min(lows) if lows else None, 'high': max(highs) if highs else None}
        # Compatibility alias, with an explicit semantic label rather than a
        # silent change from closes to intraday highs/lows.
        out[f'current_{horizon}d_range'] = {**close_range, 'basis': 'CLOSE_ONLY',
                                          'observations': len(window)}
    return out


def quote_time(raw: dict[str, Any]) -> datetime:
    day = twse_day(raw.get('d'))
    text = str(raw.get('t', ''))
    if not re.fullmatch(r'\d{2}:\d{2}:\d{2}', text):
        raise ValueError('invalid quote time')
    stamp = datetime.fromisoformat(f'{day}T{text}').replace(tzinfo=TZ)
    if raw.get('tlong') is not None:
        epoch_ms = integer(raw['tlong'])
        if epoch_ms is None or epoch_ms < 0:
            raise ValueError('invalid tlong')
        epoch = datetime.fromtimestamp(epoch_ms / 1000, TZ)
        if epoch.replace(microsecond=0) != stamp:
            raise ValueError('tlong does not match d+t')
    return stamp


def quote_eligible(quote: dict[str, Any], reference: datetime) -> bool:
    if reference.tzinfo is None:
        raise ValueError('aware reference required')
    if quote.get('status') != 'PASS' or quote.get('same_msgarray_verified') is not True:
        return False
    price = number(quote.get('price'))
    if price is None or price <= 0:
        return False
    try:
        stamp = quote_time({'d': str(quote.get('trade_date', '')).replace('-', ''),
                            't': quote.get('trade_time'), 'tlong': quote.get('tlong')})
    except (ValueError, TypeError, OverflowError, OSError):
        return False
    now = reference.astimezone(TZ)
    return stamp.date() == now.date() and 0 <= (now - stamp).total_seconds() <= 900


def provisional(adjusted: list[dict[str, Any]], quote: dict[str, Any]) -> list[dict[str, Any]] | None:
    price = number(quote.get('price'))
    if quote.get('status') != 'PASS' or price is None or price <= 0:
        return None
    try:
        day = iso_day(quote.get('trade_date'))
    except ValueError:
        return None
    if adjusted and day <= adjusted[-1]['date']:
        return None
    fields = {k: number(quote.get(k)) for k in ('open', 'high', 'low')}
    if any(v is not None and v <= 0 for v in fields.values()):
        return None
    volume = integer(quote.get('volume'))
    if volume is not None and volume < 0:
        return None
    h, l, o = fields['high'], fields['low'], fields['open']
    if h is not None and h < max(price, o if o is not None else price):
        return None
    if l is not None and l > min(price, o if o is not None else price):
        return None
    return copy.deepcopy(adjusted) + [{'date': day, **fields, 'close': price,
                                      'volume': volume, 'provisional': True}]


def readiness(history: dict[str, Any], corporate_status: str, indicators_ok: bool) -> dict[str, bool]:
    count = integer(history.get('count')) or 0
    base = (history.get('history_gate') == 'PASS' and count >= 260
            and history.get('freshness_status') == 'PASS'
            and history.get('integrity_error_count') == 0
            and corporate_status == 'PASS' and indicators_ok)
    return {'base_260_ready': bool(base), 'long_term_750_ready': bool(
        base and count >= 750 and history.get('long_term_750_ready') is True)}
