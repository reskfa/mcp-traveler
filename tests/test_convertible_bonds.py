import json
from pathlib import Path
import pandas as pd
import pytest
from gateway import acquisition,core
from gateway.dictionary import DICTIONARY
from gateway.convertible_bonds import fetch_wind


def query(ds,fields,**kwargs):
    return core.Query(dataset=ds,symbol='113052.SH',fields=fields,**kwargs).model_dump()


def test_cb_typed_identity_units_and_events():
    class SDK:
        def cb_daily(self,**args):
            assert 'trade_date' in args['fields']
            return pd.DataFrame([dict(ts_code='113052.SH',trade_date='20251105',close=124.15,amount=2,cb_over_rate=23.9)])
        def cb_basic(self,**args):return pd.DataFrame([dict(ts_code='113052.SH',bond_short_name='兴业转债',maturity_date='20271227',remain_size=100)])
        def cb_call(self,**args):return pd.DataFrame([dict(ts_code='113052.SH',ann_date='20251105',is_call=x,call_type='强赎') for x in ['公告提示强赎','公告不强赎']])
    r=acquisition.acquire('tushare',query('cb_daily',['close','amount','conversion_premium'],start_date='2025-11-05',end_date='2025-11-05'),sdk=SDK())
    assert r['data'][0]['amount']==20000 and r['data'][0]['conversion_premium']==23.9
    r=acquisition.acquire('tushare',query('cb_basic',['name','maturity_date','outstanding']),sdk=SDK())
    assert r['data'][0]['name']=='兴业转债' and r['data'][0]['date'] is None and r['data'][0]['maturity_date']=='2027-12-27'
    r=acquisition.acquire('tushare',query('cb_clauses',['call_status'],start_date='2025-11-05',end_date='2025-11-05'),sdk=SDK())
    assert len(r['data'])==2 and len({r['event_id'] for r in r['data']})==2


def test_wind_cb_date_and_unit_evidence():
    recipe=DICTIONARY['recipes']['wind_cb_daily']
    fields=DICTIONARY['datasets']['cb_daily']['fields'];bindings={f:fields[f]['sources']['wind'][0] for f in ['outstanding','ytm']}
    class Remote:
        def call(self,*args):return {'columns':[{'name':'Wind代码'},{'name':'2025年11月5日未转股余额','unit':'亿元'},{'name':'2025年11月5日税前到期收益率','unit':'%'}],'rows':[['113052.SH',413.5203,-4.4685]]}
    r,_=fetch_wind(recipe,bindings,query('cb_daily',['outstanding','ytm'],start_date='2025-11-05',end_date='2025-11-05'),Remote())
    assert r[0]['outstanding']==41352030000 and r[0]['ytm']==-4.4685
    assert r[0]['date']=='2025-11-05'


def test_cb_history_and_identity_guards():
    with pytest.raises(ValueError):query('cb_basic',['conversion_price'],date='2025-11-05')
    with pytest.raises(ValueError):query('cb_daily',['close'],start_date='2025-11-05',end_date='2025-11-05',adjust='qfq')
    with pytest.raises(ValueError):core.Query(dataset='daily_prices',symbol='113052.SH',start_date='2025-11-05',end_date='2025-11-05')


def test_cb_snapshot_fallback_matches_null_date(monkeypatch,tmp_path):
    from gateway import config
    c=json.loads((Path(__file__).parents[1]/'config.example.json').read_text())
    monkeypatch.setattr(config,'ROOT',tmp_path)
    def settings():
        c.setdefault('routes',{})['cb_basic']=['tushare','wind'];return c
    monkeypatch.setattr(config,'load',settings)
    monkeypatch.setattr(core,'available',lambda *a:True)
    monkeypatch.setattr(core,'record',lambda *a:None)
    async def invoke(name,q,timeout):
        return {'data':[{'symbol':q['symbol'],'date':None,'conversion_price':20.13 if name=='tushare' else None,'call_clause':'赎回条件' if name=='wind' else None}],'raw':[]}
    monkeypatch.setattr(core,'invoke',invoke)
    import asyncio
    r=asyncio.run(core.query(core.Query(dataset='cb_basic',symbol='113052.SH',fields=['conversion_price','call_clause'])))
    assert r['data'][0]['call_clause']=='赎回条件'
    assert r['field_sources'][0]=={'conversion_price':'tushare','call_clause':'wind'}
    monkeypatch.undo()
    from gateway.dictionary import reload_dictionary
    reload_dictionary()


def test_cb_unadapted_is_not_upstream_failure(monkeypatch,tmp_path):
    import asyncio
    from gateway import config
    c=json.loads((Path(__file__).parents[1]/'config.example.json').read_text())
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setattr(config,'load',lambda:c)
    monkeypatch.setattr(core,'available',lambda *a:True)
    async def forbidden(*args):raise AssertionError('unmapped source must not be invoked')
    monkeypatch.setattr(core,'invoke',forbidden)
    for provider in ['ifind','tdx']:
        r=asyncio.run(core.query(core.Query(dataset='cb_clauses',symbol='113052.SH',provider=provider)))
        assert r['status']=='unsupported' and r['requested_provider']==provider
        assert r['attempts'][0]['provider']==provider and r['row_count']==0 and not r['complete']
        assert r['alternatives'][0]['dataset']=='cb_basic'
    async def upstream(*args):raise RuntimeError('真实上游异常')
    monkeypatch.setattr(core,'invoke',upstream)
    monkeypatch.setattr(core,'record',lambda *a:None)
    with pytest.raises(core.QueryFailure) as exc:
        asyncio.run(core.query(core.Query(dataset='cb_clauses',symbol='113052.SH',provider='tushare',fields=['call_status'])))
    assert exc.value.attempts[0]['error']=='真实上游异常'
