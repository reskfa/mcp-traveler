import json
from pathlib import Path
from datetime import date,timedelta
import pandas as pd
import pytest
from gateway import dictionary
from gateway.acquisition import acquire
from gateway.core import Query
from gateway.catalog import technical_profiles


def query(**kwargs):
    return Query(dataset='technical_indicators',symbol='600000.SH',start_date='2026-11-13',end_date='2026-11-13',fields=['ts_atr_qfq'],**kwargs).model_dump()


def test_full_official_coverage_and_captured_live_response():
    coverage=json.loads((Path(__file__).parents[1]/'docs/tushare-technical-coverage.json').read_text())['coverage']
    evidence=json.loads((Path(__file__).parent/'fixtures/tushare_technical_full.json').read_text())
    assert len(coverage)==226 and len({v['field'] for v in coverage.values()})==226
    class SDK:
        def stk_factor_pro(self,**kwargs):
            return pd.DataFrame([{k:v for k,v in row.items() if k in kwargs['fields'].split(',')} for row in evidence])
    q=query();q['fields']=[v['field'] for v in coverage.values()]
    result=acquire('tushare',q,sdk=SDK())
    assert len(result['data'])==1
    row=result['data'][0]
    for source,meta in coverage.items():
        assert dictionary.fields('technical_indicators')[meta['field']]['sources']['tushare'][0]['columns']==[source]
        assert row[meta['field']]==evidence[0][source]
    assert row['ts_kdj_qfq']==pytest.approx(3*row['ts_kdj_k_qfq']-2*row['ts_kdj_d_qfq'],abs=3e-5)
    profiles=technical_profiles()
    assert sum(len(profiles[p]['components']) for p in profiles if p.startswith('ts_factor_'))==226
    assert all(profiles[p]['fixed'] and not profiles[p]['parameters'] for p in profiles if p.startswith('ts_factor_'))


def test_fixed_parameters_and_field_adjustment():
    assert query()['adjust']=='native'
    assert query(adjust='qfq')['adjust']=='qfq'
    with pytest.raises(ValueError,match='固定参数'):query(parameters={'n':20})
    with pytest.raises(ValueError,match='复权方式'):query(adjust='hfq')
    with pytest.raises(ValueError,match='复权方式'):
        Query(dataset='technical_indicators',symbol='600000.SH',date='2026-11-13',fields=['ts_atr_qfq','ts_atr_hfq'],adjust='qfq')


def test_sdk_pagination_boundary_and_date_validation():
    calls=[];end=date(2026,11,13)
    first=[{'ts_code':'600000.SH','trade_date':(end-timedelta(days=i)).strftime('%Y%m%d'),'atr_qfq':i} for i in range(10000)]
    class SDK:
        def stk_factor_pro(self,**kwargs):
            calls.append(kwargs)
            if len(calls)==1:return pd.DataFrame(first)
            return pd.DataFrame([{'ts_code':'600000.SH','trade_date':kwargs['end_date'],'atr_qfq':7}])
    q=query();q['start_date']='1990-01-01'
    result=acquire('tushare',q,sdk=SDK())
    assert len(calls)==2 and len(result['data'])==10001
    assert calls[1]['end_date']==(end-timedelta(days=10000)).strftime('%Y%m%d')
    assert len({r['date'] for r in result['data']})==10001
    class Wrong:
        def stk_factor_pro(self,**kwargs):return pd.DataFrame([{'ts_code':'000001.SZ','trade_date':'20261113','atr_qfq':1}])
    with pytest.raises(RuntimeError,match='证券'):acquire('tushare',query(),sdk=Wrong())
    class Missing:
        def stk_factor_pro(self,**kwargs):return pd.DataFrame([{'ts_code':'600000.SH','trade_date':'20261113'}])
    with pytest.raises(RuntimeError,match='缺少请求字段'):acquire('tushare',query(),sdk=Missing())
