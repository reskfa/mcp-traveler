import copy
import json
from pathlib import Path
import pytest
from gateway import acquisition,core
from gateway.dictionary import DICTIONARY
from gateway.tdx_convertible import fetch

ROOT=Path(__file__).parents[1]
def q(start='2025-11-05',end='2025-11-05'):
    return core.Query(dataset='cb_daily',symbol='113052.SH',start_date=start,end_date=end,fields=['close','amount','volume']).model_dump()
def bind():return {f:DICTIONARY['datasets']['cb_daily']['fields'][f]['sources']['tdx'][0] for f in ['close','amount','volume']}
def block(day='20251105',code='113052'):
    return {'structuredContent':{'Code':code,'Setcode':1,'Period':4,'Rows':[{'Data':day,'Close':124.15,'RawAmount':20000,'RawVolume':16,'Unit':1}], 'AttachInfo':{'Name':'兴业转债','HqDate':'20261118','Now':111}}}

def test_tdx_cb_history_identity_and_dates():
    class Remote:
        def call(self,method,args):
            assert args['tqFlag']=='0' and args['period']=='4'
            return block()
    rows,_=fetch(DICTIONARY['recipes']['tdx_cb_daily'],bind(),q(),Remote())
    assert rows==[{'symbol':'113052.SH','date':'2025-11-05','close':124.15,'amount':20000.,'volume':16.}]
    for invalid in [block(code='600519'),block(day='bad')]:
        class Bad:
            def call(self,*a):return invalid
        with pytest.raises(RuntimeError):fetch(DICTIONARY['recipes']['tdx_cb_daily'],bind(),q(),Bad())

def test_tdx_actual700_cap_pages_and_no_latest_quote_backfill():
    recipe=copy.deepcopy(DICTIONARY['recipes']['tdx_cb_daily']);recipe['pagination']['size']=1
    class Remote:
        def __init__(self):self.offsets=[]
        def call(self,method,args):
            self.offsets.append(args['startxh'])
            return block('20251106' if args['startxh']=='0' else '20251105')
    r=Remote();rows,_=fetch(recipe,bind(),q(),r)
    assert r.offsets==['0','1'] and len(rows)==1 and rows[0]['date']=='2025-11-05'
    class Stuck:
        def call(self,*a):return block('20251106')
    with pytest.raises(RuntimeError,match='没有向前推进'):fetch(recipe,bind(),q(),Stuck())

def test_tdx_cb_live_recorded_envelope_and_snapshot():
    raw=json.loads((ROOT/'tests/fixtures/tdx_cb_kline.json').read_text())
    class Remote:
        def call(self,*a):return raw
    r=acquisition.acquire('tdx',q(),remote=Remote())
    assert len(r['data'])==1 and r['data'][0]['close']>100
    basic=core.Query(dataset='cb_basic',symbol='113052.SH',fields=['name']).model_dump()
    r=acquisition.acquire('tdx',basic,remote=Remote())
    assert r['data']==[{'symbol':'113052.SH','date':None,'name':'兴业转债'}]
