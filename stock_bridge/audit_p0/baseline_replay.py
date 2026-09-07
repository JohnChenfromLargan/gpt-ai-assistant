"""Replay nine audit findings against the actual, hash-verified source modules."""
import copy,json,math,sys
from contextlib import ExitStack
from datetime import date,datetime,timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(sys.argv[1]).resolve()))
import bridge as b
import phase2 as p
import phase2_fixed as pf
NOW=datetime(2026,9,7,10,30,tzinfo=b.TZ_TAIPEI)
def row(d='2026-09-04',c=100,h=101,l=99):return dict(date=d,open=c,high=h,low=l,close=c,volume=1000)
def rows(end='2026-09-04'):
 dates=[];d=date.fromisoformat(end)
 while len(dates)<780:
  if d.weekday()<5:dates.append(d.isoformat())
  d-=timedelta(days=1)
 return [row(d) for d in sorted(dates)]
def update(cached,fetched,latest=None,fail=False):
 with ExitStack() as s:
  s.enter_context(patch.object(b,'now_taipei',return_value=NOW))
  s.enter_context(patch.object(b,'load_history',return_value=copy.deepcopy(cached)))
  s.enter_context(patch.object(b,'month_starts_backwards',return_value=[NOW.replace(day=1)]))
  s.enter_context(patch.object(b,'parse_stock_day',side_effect=lambda d,s:d))
  s.enter_context(patch.object(b,'http_json',side_effect=OSError('synthetic outage') if fail else None,return_value=fetched))
  return b.update_history('2002',latest)
results=[]
def check(id,value):
 results.append({'id':id,'defect_reproduced':bool(value)});assert value,id
check('Q01',not math.isfinite(b.parse_number('Infinity')))
clean,errors=b.validate_history([row(),row(c=110,h=111,l=109)])
check('H01',not errors and clean[-1]['close']==110)
_,meta=update(rows('2026-06-30'),[],fail=True)
check('H02',meta['history_gate']=='PASS' and meta['freshness_status']=='PASS')
_,meta=update(rows(),[row('2026-09-03',h=90)],row())
check('H03',meta['integrity_error_count']==0 and meta['history_gate']=='PASS')
clean,meta=update(rows(),[row()],row(c=110,h=111,l=109))
check('H04',meta['latest_day_crosscheck_status']=='PASS' and clean[-1]['close']==110)
with patch.object(p,'http_json',return_value={'stat':'ERROR','message':'maintenance'}):
 events,errors,diag=pf.corrected_fetch_corporate_actions('2002','2026-09-01','2026-09-04')
check('C01',not errors and all(n==1 for n in diag['source_success_windows'].values()))
check('C02',p.adjust_history([row()],[{'date':'2026-09-07','back_adjust_factor':.9}])[0]['close']==100)
stats=p.horizon_stats([row(h=130,l=80)])
check('A01',stats['current_20d_range']=={'low':100.,'high':100.})
pro=p.provisional_rows([row()],dict(status='PASS',price=100,trade_date='2026-09-07'))
check('A02',pro[-1]['high']==100 and pro[-1]['volume']==0)
assert b.parse_number('-') is None
assert not b.validate_history([row()])[1]
report={'type':'ACTUAL_SOURCE_SYNTHETIC_REPLAY','tests':results,'normal_controls_passed':2,'market_backtest':False,'production_state_modified':False}
print(json.dumps(report,indent=2))
