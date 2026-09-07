"""Synthetic deterministic regressions; no live market/backtest assertions."""
from __future__ import annotations
import copy, json, math, sys, tempfile, unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge as b
import phase2 as p
import phase2_fixed as pf
import bridge_quote_layer as q1
import quality_p0 as q

NOW = datetime(2026, 9, 7, 10, 30, tzinfo=q.TZ)
TODAY, PREV = '2026-09-07', '2026-09-04'

def row(day=PREV, close=100., high=101., low=99., volume=1000):
    return dict(date=day, open=close, high=high, low=low, close=close, volume=volume)

def history(n=780, end=PREV):
    # Weekday dates are synthetic fixtures, NOT an official TWSE calendar.
    days=[];d=date.fromisoformat(end)
    while len(days)<n:
        if d.weekday()<5: days.append(d.isoformat())
        d-=timedelta(days=1)
    return [row(d) for d in sorted(days)]

def payload(z='100.0', t='10:29:59', d='20260907'):
    obj=dict(c='2002',n='中鋼',z=z,d=d,t=t,o='100',h='101',l='99',v='10')
    obj['tlong']=str(int(datetime.fromisoformat(f'{q.twse_day(d)}T{t}').replace(tzinfo=q.TZ).timestamp()*1000))
    return dict(rtcode='0000',queryTime=dict(sysDate=d,sysTime=t),msgArray=[obj])

def quote_at(at='10:29:59', day=TODAY):
    return dict(status='PASS',price=100,trade_date=day,trade_time=at,
                same_msgarray_verified=True,open=100,high=101,low=99,volume=10)

def run_history(cached=None, fetched=None, latest='DEFAULT', expected=PREV, fail=False):
    cached=history() if cached is None else cached
    fetched=[] if fetched is None else fetched
    if latest=='DEFAULT':latest=row()
    with ExitStack() as stack:
        stack.enter_context(patch.object(b,'now_taipei',return_value=NOW))
        stack.enter_context(patch.object(b,'load_history',return_value=copy.deepcopy(cached)))
        stack.enter_context(patch.object(b,'load_expected_completed_date',return_value=expected))
        stack.enter_context(patch.object(b,'month_starts_backwards',return_value=[NOW.replace(day=1)]))
        stack.enter_context(patch.object(b,'parse_stock_day',side_effect=lambda x,s:copy.deepcopy(x)))
        stack.enter_context(patch.object(b,'http_json',side_effect=OSError('synthetic outage') if fail else None,return_value=copy.deepcopy(fetched)))
        return b.update_history('2002',latest)

class NumericTests(unittest.TestCase):
    def test_Q01_reject_nonfinite(self):
        for value in ('NaN','Infinity','-Infinity','1e999',float('nan'),float('inf')):
            with self.subTest(value=str(value)):self.assertIsNone(b.parse_number(value))
    def test_finite_price(self):self.assertEqual(b.parse_number('1,234.50'),1234.5)
    def test_missing_price(self):
        for value in ('-','--','X','除息',None):self.assertIsNone(b.parse_number(value))
    def test_boolean_not_price(self):self.assertIsNone(b.parse_number(True))
    def test_integer_not_rounded(self):self.assertIsNone(b.parse_int('1.2'))
    def test_integer_zero_valid(self):self.assertEqual(b.parse_int('0'),0)
    def test_json_write_reject_nonfinite(self):
        with self.assertRaises(ValueError):q.dumps({'nested':[float('nan')]})
    def test_json_read_reject_nonfinite(self):
        for text in ('{"x":NaN}','{"x":Infinity}','{"x":1e999}'):
            with self.subTest(text=text),self.assertRaises(ValueError):q.loads(text)
    def test_duplicate_json_keys(self):
        with self.assertRaises(ValueError):q.loads('{"status":"FAIL","status":"PASS"}')
    def test_json_roundtrip(self):self.assertEqual(q.loads(q.dumps({'x':0,'y':None})),{'x':0,'y':None})

class DateQuoteTests(unittest.TestCase):
    def test_roc_and_gregorian(self):
        for text in ('115/09/04','2026/09/04','115年09月04日','20260904','1150904'):
            self.assertEqual(pf.robust_roc_date_to_iso(text),PREV)
    def test_invalid_calendar_day(self):
        with self.assertRaises(ValueError):q.twse_day('115/02/30')
    def test_valid_quote_extracted(self):self.assertIsNotNone(b.extract_quote_candidate(payload(),'2002'))
    def test_quote_nan_rejected(self):self.assertIsNone(b.extract_quote_candidate(payload('NaN'),'2002'))
    def test_quote_wrong_symbol(self):self.assertIsNone(b.extract_quote_candidate(payload(),'3019'))
    def test_tlong_mismatch(self):
        data=payload();data['msgArray'][0]['tlong']=str(int(data['msgArray'][0]['tlong'])+60000)
        self.assertIsNone(b.extract_quote_candidate(data,'2002'))
    def test_missing_tlong_allowed_with_dt(self):
        data=payload();data['msgArray'][0].pop('tlong')
        self.assertIsNotNone(b.extract_quote_candidate(data,'2002'))
    def test_quote_900_seconds_allowed(self):self.assertTrue(q.quote_eligible(quote_at('10:15:00'),NOW))
    def test_quote_901_seconds_rejected(self):self.assertFalse(q.quote_eligible(quote_at('10:14:59'),NOW))
    def test_future_trade_rejected(self):self.assertFalse(q.quote_eligible(quote_at('10:30:01'),NOW))
    def test_previous_day_rejected(self):self.assertFalse(q.quote_eligible(quote_at(day=PREV),NOW))
    def test_naive_reference_rejected(self):
        with self.assertRaises(ValueError):q.quote_eligible(quote_at(),NOW.replace(tzinfo=None))
    def test_cached_timestamp_is_not_renewed(self):
        c=b.extract_quote_candidate(payload(t='10:20:00'),'2002')
        record=q1.candidate_to_record('2002',c,NOW)
        out,_=q1.merge_live_and_cached_quote('2002',{'status':'FAIL_NO_RECENT_TRADE'},None,record,NOW)
        self.assertEqual(out['trade_time'],'10:20:00');self.assertEqual(out['age_seconds'],600)
    def test_cache_timestamp_mismatch_rejected(self):
        c=b.extract_quote_candidate(payload(),'2002');record=q1.candidate_to_record('2002',c,NOW)
        record['trade_time']='10:00:00'
        self.assertIsNone(q1.record_to_candidate('2002',record))

class HistoryTests(unittest.TestCase):
    def test_H01_conflicting_duplicate_quarantined(self):
        clean,errors=b.validate_history([row(),row(close=110,high=111,low=109),row()])
        self.assertFalse(clean);self.assertTrue(any('conflicting_duplicate' in e for e in errors))
    def test_identical_duplicate_deduplicated(self):
        clean,errors=b.validate_history([row(),row()]);self.assertEqual(len(clean),1);self.assertFalse(errors)
    def test_reject_invalid_order(self):self.assertTrue(b.validate_history([row(high=90)])[1])
    def test_reject_nonfinite_ohlc(self):self.assertTrue(b.validate_history([row(high=float('nan'))])[1])
    def test_reject_negative_price(self):self.assertTrue(b.validate_history([row(close=-1,high=1,low=-2)])[1])
    def test_reject_negative_volume(self):self.assertTrue(b.validate_history([row(volume=-1)])[1])
    def test_reject_fractional_volume(self):self.assertTrue(b.validate_history([row(volume=1.5)])[1])
    def test_H02_outage_cannot_self_certify_cache(self):
        _,meta=run_history(cached=history(end='2026-06-30'),latest=None,expected=None,fail=True)
        self.assertNotEqual(meta['freshness_status'],'PASS');self.assertEqual(meta['history_gate'],'FAIL')
        self.assertGreater(meta['source_error_count'],0)
    def test_missing_calendar_blocks_even_matching_sources(self):
        _,meta=run_history(expected=None);self.assertEqual(meta['history_gate'],'FAIL')
    def test_missing_openapi_blocks_crosscheck(self):
        _,meta=run_history(latest=None);self.assertEqual(meta['history_gate'],'FAIL')
    def test_stale_openapi_is_not_calendar(self):
        _,meta=run_history(cached=history(end='2026-09-03'),latest=row('2026-09-03'))
        self.assertEqual(meta['history_gate'],'FAIL')
    def test_H03_errors_survive_cleanup(self):
        _,meta=run_history(fetched=[row('2026-09-03',high=90)])
        self.assertGreater(meta['integrity_error_count'],0);self.assertEqual(meta['history_gate'],'FAIL')
    def test_H04_crosscheck_before_overwrite(self):
        clean,meta=run_history(latest=row(close=110,high=111,low=109))
        self.assertEqual(clean[-1]['close'],100);self.assertEqual(meta['latest_day_crosscheck_status'],'FAIL_OHLCV_MISMATCH')
        self.assertEqual(meta['history_gate'],'FAIL')
    def test_valid_780_history(self):
        clean,meta=run_history();self.assertEqual(meta['history_gate'],'PASS');self.assertTrue(meta['long_term_750_ready'])
    def test_valid_300_history_independent_long_gate(self):
        _,meta=run_history(cached=history(300));self.assertEqual(meta['history_gate'],'PASS');self.assertFalse(meta['long_term_750_ready'])
    def test_under_260(self):
        _,meta=run_history(cached=history(259));self.assertEqual(meta['history_gate'],'FAIL')
    def test_newest_official_day_patch_only(self):
        clean,meta=run_history(cached=history(end='2026-09-03'))
        self.assertEqual(clean[-1]['date'],PREV);self.assertEqual(meta['history_gate'],'PASS')
    def test_future_openapi_rejected(self):
        _,meta=run_history(latest=row('2026-09-08'));self.assertEqual(meta['history_gate'],'FAIL')
    def test_preserve_good_cache_after_bad_refresh(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(b,'HISTORY_DIR',Path(tmp)):
            cache=Path(tmp)/'2002.json';cache.write_text('OLD_VALID_CACHE')
            b.write_history('2002',[],{'integrity_error_count':1})
            self.assertEqual(cache.read_text(),'OLD_VALID_CACHE');self.assertTrue((Path(tmp)/'2002.rejected.json').exists())
    def test_monthly_bad_row_not_silently_skipped(self):
        data={'stat':'OK','fields':['日期','成交股數','開盤價','最高價','最低價','收盤價'],
              'data':[['115/09/04','10','100','90','99','100']]}
        with self.assertRaises(b.BridgeError):b.parse_stock_day(data,'2002')
    def test_calendar_absent_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(b,'ROOT',Path(tmp)):
            self.assertIsNone(b.load_expected_completed_date(NOW))

FIELDS=['資料日期','股票代號','股票名稱','除權息前收盤價','除權息參考價','權/息']
class ActionTests(unittest.TestCase):
    def test_C01_error_dictionary_not_no_events(self):
        with patch.object(p,'http_json',return_value={'stat':'ERROR','message':'maintenance'}):
            events,errors,diag=pf.corrected_fetch_corporate_actions('2002',PREV,TODAY)
        self.assertTrue(errors);self.assertFalse(events)
        self.assertTrue(all(x==0 for x in diag['source_success_windows'].values()))
    def test_ok_missing_schema_rejected(self):
        with self.assertRaises(ValueError):q.action_table({'stat':'OK','data':[]})
    def test_wellformed_empty_table_accepted(self):
        self.assertEqual(p.parse_action_table({'stat':'OK','fields':FIELDS,'data':[]},'2002','EX_RIGHT_DIVIDEND'),[])
    def test_missing_required_fields_even_empty_rejected(self):
        with self.assertRaises(RuntimeError):p.parse_action_table({'stat':'OK','fields':['x'],'data':[]},'2002','EX_RIGHT_DIVIDEND')
    def test_malformed_row_rejected(self):
        with self.assertRaises(ValueError):q.action_table({'stat':'OK','fields':FIELDS,'data':[['x']]})
    def test_invalid_action_price_rejected(self):
        with self.assertRaises(ValueError):p.parse_action_table({'stat':'OK','fields':FIELDS,'data':[['115/09/04','2002','中鋼','100','NaN','息']]},'2002','EX_RIGHT_DIVIDEND')
    def test_C02_today_ex_date_not_in_raw_history(self):
        out=p.adjust_history([row()], [{'date':TODAY,'back_adjust_factor':0.9}],as_of=TODAY)
        self.assertEqual(out[-1]['close'],90)
    def test_future_action_not_applied(self):
        out=p.adjust_history([row()], [{'date':'2026-09-08','back_adjust_factor':0.9}],as_of=TODAY)
        self.assertEqual(out[-1]['close'],100)
    def test_event_day_not_adjusted_twice(self):
        out=p.adjust_history([row(PREV),row(TODAY,close=90,high=91,low=89)], [{'date':TODAY,'back_adjust_factor':0.9}],as_of=TODAY)
        self.assertEqual([r['close'] for r in out],[90,90])
    def test_duplicate_identical_events_not_multiplied_twice(self):
        ev={'date':TODAY,'kind':'EX_RIGHT_DIVIDEND','back_adjust_factor':0.9}
        self.assertEqual(p.adjust_history([row()],[ev,ev],as_of=TODAY)[0]['close'],90)
    def test_conflicting_events_rejected(self):
        ev={'date':TODAY,'kind':'EX_RIGHT_DIVIDEND','back_adjust_factor':0.9}
        with self.assertRaises(ValueError):p.adjust_history([row()],[ev,{**ev,'back_adjust_factor':0.8}],as_of=TODAY)
    def test_out_of_range_endpoint_response_fails(self):
        data={'stat':'OK','fields':FIELDS,'data':[['114/09/04','2002','中鋼','100','90','息']]}
        with patch.object(p,'http_json',return_value=data):
            _,errors,_=pf.corrected_fetch_corporate_actions('2002',PREV,TODAY)
        self.assertTrue(errors)

class AnalyticsTests(unittest.TestCase):
    def test_A01_close_and_ohlc_ranges_separate(self):
        stats=p.horizon_stats([row(high=130,low=80)])
        self.assertEqual(stats['current_20d_range']['basis'],'CLOSE_ONLY')
        self.assertEqual(stats['current_20d_close_range'],{'low':100,'high':100})
        self.assertEqual(stats['current_20d_ohlc_range'],{'low':80,'high':130})
    def test_A02_missing_fields_not_fabricated(self):
        out=p.provisional_rows(history(300),{'status':'PASS','price':101,'trade_date':TODAY})
        self.assertIsNone(out[-1]['high']);self.assertIsNone(out[-1]['volume'])
    def test_missing_high_disables_atr_not_close_indicators(self):
        out=p.provisional_rows(history(300),{'status':'PASS','price':101,'trade_date':TODAY})
        technical=p.technical(out)
        self.assertIsNone(technical['atr14']);self.assertIsNotNone(technical['ma240'])
        self.assertIsNotNone(technical['macd']);self.assertIsNotNone(technical['rsi14'])
    def test_missing_volume_not_fabricated_and_does_not_block_atr(self):
        quote=quote_at();quote.pop('volume');out=p.provisional_rows(history(300),quote)
        self.assertIsNone(out[-1]['volume']);self.assertAlmostEqual(p.technical(out)['atr14'],2)
    def test_invalid_intraday_range_rejected(self):
        quote=quote_at();quote['high']=90;self.assertIsNone(p.provisional_rows(history(300),quote))
    def test_no_duplicate_today_bar(self):
        self.assertIsNone(p.provisional_rows(history(300,end=TODAY),quote_at()))
    def test_flat_prices_rsi_50(self):self.assertEqual(p.rsi_wilder([100]*300),50)
    def test_rising_prices_rsi_100(self):self.assertEqual(p.rsi_wilder(list(range(1,301))),100)
    def test_falling_prices_rsi_0(self):self.assertEqual(p.rsi_wilder(list(range(301,1,-1))),0)
    def test_known_constant_price_indicators(self):
        tech=p.technical(history(300));self.assertEqual(tech['ma240'],100)
        self.assertEqual(tech['macd'],0);self.assertEqual(tech['macd_signal'],0);self.assertAlmostEqual(tech['atr14'],2)
    def test_300_base_ready_not_long(self):
        meta={'history_gate':'PASS','count':300,'freshness_status':'PASS','path_status':'PASS','integrity_error_count':0,'long_term_750_ready':False}
        flags=q.readiness(meta,'PASS',True);self.assertTrue(flags['base_260_ready']);self.assertFalse(flags['long_term_750_ready'])
    def test_no_strategy_success_claim_from_ready(self):
        self.assertNotIn('probability',q.readiness({'count':0},'PASS',True))

class IntegrationTests(unittest.TestCase):
    def test_complete_phase2_independent_symbol_and_current_exdate(self):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW if tz else NOW.replace(tzinfo=None)
        base=history(300)
        meta={'history_gate':'PASS','count':300,'freshness_status':'PASS','path_status':'PASS','integrity_error_count':0,'long_term_750_ready':False}
        obj={'schema_version':'1.0','generated_at':NOW.isoformat(),'market_date':TODAY,'bridge':{},'quotes':{'2002':{'status':'FAIL_STALE_TRADE'},'3019':quote_at()},'history':{'2002':{**meta,'history_gate':'FAIL'},'3019':dict(meta)}}
        calls=[]
        def actions(symbol,start,end):
            calls.append((symbol,start,end));return [],[]
        with tempfile.TemporaryDirectory() as tmp,patch.object(p,'LATEST_PATH',Path(tmp)/'latest.json'),patch.object(p,'load_history',return_value=base),patch.object(p,'fetch_corporate_actions',side_effect=actions),patch.object(p,'datetime',Clock):
            p.LATEST_PATH.write_text(json.dumps(obj));out=p.run_phase2()
        self.assertFalse(out['analytics']['2002']['analysis_ready'])
        self.assertTrue(out['analytics']['3019']['analysis_ready'])
        self.assertFalse(out['analytics']['3019']['period_readiness']['long_term_750_ready'])
        self.assertTrue(out['bridge']['analysis_ready']);self.assertFalse(out['bridge']['all_symbols_analysis_ready'])
        self.assertEqual(calls[0][2],TODAY)
        self.assertEqual(out['bridge']['strategy_validation_status'],'NOT_VALIDATED')
    def test_requesting_future_reference_not_accepted_as_fresh(self):
        self.assertFalse(q.quote_eligible(quote_at(day='2026-09-08'),NOW))

if __name__=='__main__':unittest.main(verbosity=2)
