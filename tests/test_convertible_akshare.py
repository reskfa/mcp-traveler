import json
from pathlib import Path
import pandas as pd
import pytest
from gateway.convertible_akshare import fetch,value_history

FRAGMENT=json.loads((Path(__file__).parent/'fixtures/akshare_cb_dictionary.json').read_text())

def run(rid,ds,sdk,**extra):
    recipe=FRAGMENT['recipes'][rid]
    bindings={f:next(b for b in bs if b['recipe']==rid) for f,bs in FRAGMENT['bindings'][ds].items() if any(b['recipe']==rid for b in bs)}
    q={'symbol':'113052.SH','dataset':ds,'adjust':'none','fields':list(bindings),'start_date':'2025-11-05','end_date':'2025-11-05',**extra}
    return fetch(recipe,bindings,q,sdk)[0]

def test_profile_units_identity_and_snapshot():
    class SDK:
        def bond_cb_profile_sina(self,**kwargs):
            assert kwargs=={'symbol':'sh113052'}
            return pd.DataFrame({'item':['债券代码','发行规模（亿元）','上市日期','债券简称'],'value':['sh113052','500','2022-01-14','兴业转债']})
    row=run('akshare_cb_profile','cb_basic',SDK())[0]
    assert row['date'] is None and row['issue_size']==50000000000 and row['name']=='兴业转债'
    assert row['maturity_date'] is None
    class Wrong(SDK):
        def bond_cb_profile_sina(self,**kwargs):return pd.DataFrame({'item':['债券代码'],'value':['sz123001']})
    with pytest.raises(RuntimeError,match='身份'):run('akshare_cb_profile','cb_basic',Wrong())

def test_summary_date_range_and_missing_values():
    class SDK:
        def bond_cb_summary_sina(self,**kwargs):return pd.DataFrame({'item':['转换期间','最新转股价（元）'],'value':['2022-06-30~2027-12-26','--']})
    row=run('akshare_cb_summary','cb_basic',SDK())[0]
    assert row['conversion_start_date']=='2022-06-30' and row['conversion_end_date']=='2027-12-26' and row['conversion_price'] is None

def test_history_requires_dates_filters_and_preserves_nulls():
    class SDK:
        def bond_zh_hs_cov_daily(self,**kwargs):return pd.DataFrame([{'date':'2025-11-05','open':120,'close':None},{'date':'2025-11-06','close':121}])
    row=run('akshare_cb_daily','cb_daily',SDK())[0]
    assert row['open']==120 and row['close'] is None
    class Wrong(SDK):
        def bond_zh_hs_cov_daily(self,**kwargs):return pd.DataFrame([{'close':121}])
    with pytest.raises(RuntimeError,match='日期'):run('akshare_cb_daily','cb_daily',Wrong())

def test_named_value_parser_ignores_extra_columns_and_rejects_truncation():
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'result':{'pages':1,'data':[{'SECUCODE':'113052.SH','DATE':'2025-11-05','SWAPOR':20,'NEW_COLUMN':9}]}}
    rows,_=value_history('113052.SH',lambda *a,**kw:Response())
    assert rows[0]['SWAPOR']==20
    class Truncated(Response):
        def json(self):return {'result':{'pages':2,'data':[]}}
    with pytest.raises(RuntimeError,match='上限'):value_history('113052.SH',lambda *a,**kw:Truncated())


def test_redeem_snapshot_filters_identity_and_normalizes_units():
    class SDK:
        def bond_cb_redeem_jsl(self):return pd.DataFrame([{'代码':'113052','正股代码':'601166','正股名称':'兴业银行','剩余规模':413.5,'强赎条款':'连续三十日至少十五日'}, {'代码':'123001','剩余规模':1}])
    rows=run('akshare_cb_redeem_snapshot','cb_basic',SDK())
    assert len(rows)==1 and rows[0]['date'] is None
    assert rows[0]['stock_code']=='601166.SH' and rows[0]['outstanding']==41350000000
