"""Apply reproducible, hash-guarded P0 candidate fixes in an isolated checkout.
Does not access the network, Git, credentials, production state or task settings.
"""
from pathlib import Path
import ast, hashlib, re, sys
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
EXPECTED = {
 'bridge.py':'85569a2ed7419ad4b2eb4e675bb05da08d6cd4da',
 'bridge_quote_layer.py':'0d54f0306be117c92efa72c461715b1155a8b00e',
 'bridge_quote_layer_v2.py':'52bcfea9cd8ce81f442461bcaac4d9bfcdcecf1d',
 'phase2.py':'c6956a6ccf221df16f3ac6351ee9fa970a7a4968',
 'phase2_fixed.py':'98e13eb8941b060c3a86c66b1317cee667fbb286'}
texts = {}
for name, expected in EXPECTED.items():
 data = (ROOT/name).read_bytes()
 actual = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
 if actual != expected: raise RuntimeError(f'BASELINE_CHANGED:{name}:{actual}')
 texts[name] = data.decode('utf8')
def replace_func(text, name, new):
 nodes = [n for n in ast.parse(text).body if isinstance(n, (ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name]
 assert len(nodes)==1, name
 node = nodes[0];lines=text.splitlines(keepends=True)
 return ''.join(lines[:node.lineno-1])+new.strip()+'\n'+''.join(lines[node.end_lineno:])
def change(name, func, new): texts[name] = replace_func(texts[name],func,new)
for name in texts:
 texts[name] = texts[name].replace('from __future__ import annotations','from __future__ import annotations\nimport quality_p0 as quality')
 texts[name] = texts[name].replace('json.loads(', 'quality.loads(').replace('json.dumps(', 'quality.dumps(')
change('bridge.py','parse_number','''
def parse_number(value):
    return quality.number(value)
''')
change('bridge.py','parse_int','''
def parse_int(value):
    return quality.integer(value)
''')
change('bridge.py','roc_date_to_iso','''
def roc_date_to_iso(value):
    return quality.twse_day(value)
''')
change('bridge.py','compact_date_to_iso','''
def compact_date_to_iso(value):
    return quality.twse_day(value)
''')
change('bridge.py','validate_history','''
def validate_history(rows):
    return quality.history_rows(rows)
''')
# Validate timestamps in the object before declaring same-msgArray provenance.
texts['bridge.py'] = texts['bridge.py'].replace('''    for item in messages:
        if str(item.get("c", "")) != symbol:''','''    for item in messages:
        if not isinstance(item, dict):
            continue
        if str(item.get("c", "")) != symbol:''')
texts['bridge.py'] = texts['bridge.py'].replace('''        return QuoteCandidate(
            price=price,''','''        try:
            quality.quote_time(item)
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        return QuoteCandidate(
            price=price,''')
texts['bridge.py'] = texts['bridge.py'].replace('''    if str(payload.get("stat", "")) != "OK":
        return []''','''    if not isinstance(payload, dict) or payload.get("stat") != "OK":
        raise BridgeError("STOCK_DAY_STATUS_NOT_OK")''')
texts['bridge.py'] = texts['bridge.py'].replace('''        except (IndexError, ValueError):
            continue
        if None in (record["volume"], record["open"], record["high"], record["low"], record["close"]):
            continue
        parsed.append(record)''','''        except (IndexError, ValueError, TypeError) as exc:
            raise BridgeError(f"INVALID_STOCK_DAY_ROW:{symbol}:{exc}") from exc
        try:
            parsed.append(quality.normal_bar(record))
        except ValueError as exc:
            raise BridgeError(f"INVALID_STOCK_DAY_ROW:{symbol}:{exc}") from exc''')
change('bridge.py','update_history','''
def load_expected_completed_date(reference):
    """Read separately verified calendar evidence; never infer from quote/cache.

    The official calendar ingestion adapter is a deployment prerequisite.
    Absent/invalid evidence MUST return None, not yesterday or cached last_date.
    """
    path = ROOT / 'trading_calendar.json'
    try:
        data = quality.loads(path.read_text(encoding='utf-8'))
        today = reference.astimezone(TZ_TAIPEI).date().isoformat()
        if (data.get('schema_version') != '1.0' or data.get('source') != 'TWSE'
                or data.get('status') != 'PASS'
                or data.get('verified_for_market_date') != today):
            return None
        expected = quality.iso_day(data.get('last_completed_date'))
        sessions = data.get('sessions')
        if not isinstance(sessions, list) or not sessions:
            return None
        sessions = sorted(set(quality.iso_day(d) for d in sessions))
        completed = [d for d in sessions if d < today]
        if not completed or expected != completed[-1] or expected >= today:
            return None
        if quality.iso_day(data.get('coverage_end')) < today:
            return None
        if not data.get('source_snapshot_sha256'):
            return None
        return expected
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def update_history(symbol, latest_openapi):
    existing = load_history(symbol)
    raw = list(existing)
    now = now_taipei()
    today = now.date().isoformat()
    expected = load_expected_completed_date(now)
    source_errors = []
    tables_read = 0
    months = 2 if len(existing) >= TARGET_HISTORY_ROWS else 48
    for month_start in month_starts_backwards(now, months):
        try:
            payload = http_json(stock_day_url(symbol, month_start))
            rows = parse_stock_day(payload, symbol)
            # Retain all rows until final validation so invalid or conflicting
            # input cannot disappear during monthly cleanup/deduplication.
            raw.extend(rows)
            tables_read += 1
        except Exception as exc:
            source_errors.append(f'{month_start:%Y-%m}:{type(exc).__name__}:{exc}')
        unique_days = {str(r.get('date')) for r in raw if isinstance(r, dict)
                       and str(r.get('date','')) < today}
        if (len(existing) < TARGET_HISTORY_ROWS and len(unique_days) >= TARGET_HISTORY_ROWS
                and month_start.month not in {now.month, (now.month - 1) or 12}):
            break
    raw = [r for r in raw if not isinstance(r, dict) or str(r.get('date','')) < today]
    validated, errors = validate_history(raw)
    # Compare before patching. A mismatch is never replaced silently.
    if latest_openapi is not None:
        try:
            latest_row = quality.normal_bar(latest_openapi)
            if latest_row['date'] < today:
                matches = [r for r in validated if r['date'] == latest_row['date']]
                if matches and matches[0] != latest_row:
                    errors.append(f'OPENAPI_OHLCV_MISMATCH:{latest_row["date"]}')
                elif not matches and latest_row['date'] == expected:
                    # Only append a genuinely newer completed day, not a row
                    # quarantined by validation or an arbitrary older hole.
                    if not errors and (not validated or latest_row['date'] > validated[-1]['date']):
                        validated.append(latest_row)
            elif latest_row['date'] > today:
                errors.append('OPENAPI_FUTURE_DATE')
        except (ValueError, TypeError) as exc:
            errors.append(f'OPENAPI_INVALID:{exc}')
    validated = sorted(validated, key=lambda r:r['date'])[-900:]
    latest_date = validated[-1]['date'] if validated else None
    crosscheck = quality.latest_crosscheck(validated, latest_openapi, expected, today)
    fresh = bool(expected is not None and latest_date == expected)
    count = len(validated)
    ready = bool(fresh and count >= MIN_HISTORY_GATE and tables_read
                 and not errors and not source_errors
                 and crosscheck in {'PASS','DEFER_CURRENT_DAY'})
    meta = {
        'name': SYMBOLS[symbol], 'count': count,
        'first_date': validated[0]['date'] if validated else None,
        'last_date': latest_date, 'expected_latest_completed_date': expected,
        'latest_day_crosscheck_status': crosscheck,
        'path_status': 'PASS' if tables_read else 'FAIL',
        'freshness_status': 'PASS' if fresh else ('FAIL' if expected else 'UNVERIFIED_CALENDAR'),
        'history_gate': 'PASS' if ready else 'FAIL',
        'long_term_750_ready': bool(ready and count >= LONG_HISTORY_GATE),
        'integrity_error_count': len(errors), 'integrity_errors_sample': errors[:20],
        'source_error_count': len(source_errors), 'source_errors_sample': source_errors[:20],
        'source_tables_read': tables_read,
        'price_adjustment_status': 'PENDING_PHASE_2', 'analysis_ready': False,
    }
    return validated, meta
''')
texts['bridge.py'] = texts['bridge.py'].replace('''    assert not errors and len(rows) == 1 and rows[0]["volume"] == 20''','''    assert errors and len(rows) == 0  # Conflicting duplicates are quarantined.''')
texts['bridge.py'] = texts['bridge.py'].replace('''    (HISTORY_DIR / f"{symbol}.json").write_text''','''    if meta.get('integrity_error_count', 0) or meta.get('source_error_count', 0):
        # Preserve last known cache instead of persisting a sanitized failure.
        (HISTORY_DIR / f'{symbol}.rejected.json').write_text(
            quality.dumps({'symbol': symbol, 'meta': meta}, ensure_ascii=False, indent=2),
            encoding='utf-8')
        return
    (HISTORY_DIR / f"{symbol}.json").write_text''')
change('phase2.py','parse_number','''
def parse_number(value):
    return quality.number(value)
''')
change('phase2.py','adjust_history','''
def adjust_history(rows, actions, as_of=None):
    valuation_date = as_of or (rows[-1]['date'] if rows else date.today().isoformat())
    return quality.adjusted_history(rows, actions, valuation_date)
''')
change('phase2.py','provisional_rows','''
def provisional_rows(adjusted, quote):
    return quality.provisional(adjusted, quote)
''')
# Explicitly label close-only ranges and provide OHLC ranges separately.
text = texts['phase2.py'];node = next(n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef) and n.name=='horizon_stats')
old = ast.get_source_segment(text,node)
pos = old.index('    for h in (20, 60, 120, 240):')
new = old[:pos] + '''    out.update(quality.price_ranges(rows))
    return out'''
texts['phase2.py'] = text.replace(old,new)
texts['phase2.py'] = texts['phase2.py'].replace('''    if avg_loss == 0:
        return 100.0''','''    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0''')
texts['phase2.py'] = texts['phase2.py'].replace('''        h, l, pc = float(cur["high"]), float(cur["low"]), float(prev["close"])
        trs.append''','''        h, l, pc = (quality.number(cur.get('high')), quality.number(cur.get('low')),
                    quality.number(prev.get('close')))
        if h is None or l is None or pc is None or min(h,l,pc) <= 0 or h < l:
            return None
        trs.append''')
# Company-action schema/status failures must be explicit even for zero rows.
texts['phase2.py'] = texts['phase2.py'].replace('''    if str(payload.get("stat", "")) not in {"OK", ""}:
        return []
    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    if not isinstance(fields, list) or not isinstance(rows, list):
        return []''','''    fields, rows = quality.action_table(payload)''')
texts['phase2.py'] = texts['phase2.py'].replace('''            if not pre or not ref or pre <= 0 or ref <= 0:
                continue''','''            if not pre or not ref or pre <= 0 or ref <= 0:
                raise ValueError('INVALID_CORPORATE_ACTION_PRICE')''')
texts['phase2.py'] = texts['phase2.py'].replace('''            if factor <= 0 or factor > 5:
                continue''','''            if factor <= 0 or not math.isfinite(factor):
                raise ValueError('INVALID_CORPORATE_ACTION_FACTOR')''')
texts['phase2.py'] = texts['phase2.py'].replace('''        except (IndexError, ValueError):
            continue
    return out''','''        except (IndexError, ValueError, TypeError) as exc:
            raise ValueError(f'INVALID_CORPORATE_ACTION_ROW:{symbol}:{exc}') from exc
    return out''')
change('phase2.py','run_phase2','''
def run_phase2():
    latest = quality.loads(LATEST_PATH.read_text(encoding='utf-8'))
    latest['analytics'] = {}
    readiness_by_symbol = {}
    as_of = quality.iso_day(latest.get('market_date'))
    for symbol in SYMBOLS:
        meta = latest.get('history', {}).get(symbol, {})
        try:
            raw = load_history(symbol)
            if len(raw) < 260 or meta.get('history_gate') != 'PASS':
                raise ValueError('HISTORY_GATE_NOT_READY')
            # Include the current valuation/ex-date, not just the last raw bar.
            actions, action_errors = fetch_corporate_actions(symbol, raw[0]['date'], as_of)
            adjusted = adjust_history(raw, actions, as_of=as_of)
            unresolved = unresolved_discontinuities(raw, {a['date'] for a in actions})
            completed = technical(adjusted)
            now = datetime.now(TZ_TAIPEI)
            quote = latest.get('quotes', {}).get(symbol, {})
            if quote.get('status') == 'PASS' and not quality.quote_eligible(quote, now):
                quote['status'] = 'FAIL_STALE_TRADE'
            provisional = provisional_rows(adjusted, quote)
            intraday = technical(provisional) if provisional else None
            corp_status = 'PASS' if not action_errors and not unresolved else 'FAIL'
            required = ('ma20','ma60','ma120','ma240','rsi14','macd','macd_signal','atr14')
            indicators_ok = all(quality.number(completed.get(k)) is not None for k in required)
            flags = quality.readiness(meta, corp_status, indicators_ok)
            ready = flags['base_260_ready']
            readiness_by_symbol[symbol] = ready
            latest['analytics'][symbol] = {
                'analysis_ready': ready, 'period_readiness': flags,
                'corporate_actions': {
                    'status': corp_status, 'event_count': len(actions), 'events': actions,
                    'fetch_error_count': len(action_errors), 'fetch_errors_sample': action_errors[:10],
                    'unresolved_discontinuities': unresolved[:20], 'valuation_date': as_of,
                    'method': 'back_adjust_prior_ohlc_by_effective_date; raw volume retained',
                },
                'technical_completed_day': completed, 'technical_intraday_estimate': intraday,
                'intraday_indicator_availability': {k: intraday.get(k) is not None for k in required} if intraday else {},
                'horizon_statistics': horizon_stats(adjusted),
            }
            meta['price_adjustment_status'] = corp_status
            meta['analysis_ready'] = ready
        except Exception as exc:
            readiness_by_symbol[symbol] = False
            meta['analysis_ready'] = False
            meta['price_adjustment_status'] = 'FAIL'
            latest['analytics'][symbol] = {
                'analysis_ready': False, 'reason': f'{type(exc).__name__}:{exc}',
                'period_readiness': {'base_260_ready':False, 'long_term_750_ready':False},
                'corporate_actions': {'status':'FAIL', 'events':[], 'event_count':0, 'fetch_error_count':1, 'fetch_errors_sample':[str(exc)], 'unresolved_discontinuities':[]},
            }
    latest['bridge']['phase'] = 'PHASE2_ANALYTICS'
    latest['bridge']['analysis_ready'] = any(readiness_by_symbol.values())
    latest['bridge']['all_symbols_analysis_ready'] = all(readiness_by_symbol.values())
    latest['bridge']['symbol_analysis_ready'] = readiness_by_symbol
    latest['bridge']['strategy_validation_status'] = 'NOT_VALIDATED'
    latest['schema_version'] = '2.0'
    LATEST_PATH.write_text(quality.dumps(latest, ensure_ascii=False, indent=2)+'\\n', encoding='utf-8')
    return latest
''')
change('phase2_fixed.py','robust_roc_date_to_iso','''
def robust_roc_date_to_iso(value):
    return quality.twse_day(value)
''')
texts['phase2_fixed.py'] = texts['phase2_fixed.py'].replace('''                        parsed = p.parse_action_table(payload, symbol, kind) if rows else []''','''                        quality.action_table(payload)
                        parsed = p.parse_action_table(payload, symbol, kind)
                        if any(not quality.twse_day(d1) <= a['date'] <= quality.twse_day(d2) for a in parsed):
                            raise ValueError('ACTION_RESPONSE_OUTSIDE_REQUESTED_RANGE')''')
# Sentinel must not crash on another blocked Gate or disable a healthy symbol.
texts['phase2_fixed.py'] = texts['phase2_fixed.py'].replace('''            data["bridge"]["analysis_ready"] = False''','''            data["bridge"]["analysis_ready"] = any(
                s != '2002' and value for s, value in data['bridge']['symbol_analysis_ready'].items())''')
texts['bridge_quote_layer.py'] = texts['bridge_quote_layer.py'].replace('FUTURE_TOLERANCE_SECONDS = 5','FUTURE_TOLERANCE_SECONDS = 0')
# Preserve legacy cache compatibility but reject timestamp mismatches. Raw cache
# provenance/replay is deliberately left as a separately tracked P1 requirement.
needle='''    if record.get("same_msgarray_verified") is not True:
        return None'''
texts['bridge_quote_layer.py'] = texts['bridge_quote_layer.py'].replace(needle,needle+'''
    try:
        quality.quote_time({'d': trade_date.replace('-', ''), 't': trade_time,
                            'tlong': record.get('tlong')})
    except (ValueError, TypeError, OverflowError, OSError):
        return None''')
# Missing historical sentinel invalidates only the affected symbol, not the
# complete run. Preserve failure reasons instead of raising away other results.
texts['phase2_fixed.py'] = texts['phase2_fixed.py'].replace(
    'if history_2002 and history_2002[0]["date"] <= "2026-07-24" <= history_2002[-1]["date"]:',
    'if (data["analytics"].get("2002", {}).get("analysis_ready") and history_2002 and history_2002[0]["date"] <= "2026-07-24" <= history_2002[-1]["date"]):')
start = texts['phase2_fixed.py'].index('            raise RuntimeError(\n                "CORPORATE_ACTION_REGRESSION:')
end = texts['phase2_fixed.py'].index('\n\n    p.LATEST_PATH.write_text', start)
texts['phase2_fixed.py'] = texts['phase2_fixed.py'][:start] + '            data["analytics"]["2002"]["reason"] = "CORPORATE_ACTION_REGRESSION_MISSING_SENTINEL"\n            data["analytics"]["2002"]["period_readiness"] = {"base_260_ready": False, "long_term_750_ready": False}' + texts['phase2_fixed.py'][end:]
texts['phase2_fixed.py'] = texts['phase2_fixed.py'].replace(
    "\n    p.LATEST_PATH.write_text(quality.dumps(data, ensure_ascii=False, indent=2) + ",
    "\n    data['bridge']['all_symbols_analysis_ready'] = all(data['bridge']['symbol_analysis_ready'].values())\n    p.LATEST_PATH.write_text(quality.dumps(data, ensure_ascii=False, indent=2) + ")
for name,text in texts.items():
 ast.parse(text)
 (ROOT/name).write_text(text,encoding='utf8')
print('P0_CANDIDATE_PATCH_APPLIED; NOT A PRODUCTION DEPLOYMENT')
