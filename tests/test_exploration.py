import asyncio
import json
from unittest.mock import patch,AsyncMock
import pytest
from gateway import exploration as e


def req(**kw):return e.ExplorationRequest(dataset='valuations',symbol='600000.SH',date='2025-12-31',name='测试周转天数',**kw)
def response(code='600000',day='2025-12-31',column='测试周转天数',unit='天',value=3):
    return {'columns':[{'name':'证券代码'},{'name':'日期'},{'name':column,'unit':unit}], 'rows':[[code,day,value]]}
def validate(raw,r=None):return e.validate_response(r or req(),'ifind',e._recipe('ifind','valuations'),raw)

def test_strict_evidence():
    c,_=validate(response());assert c['data'][0][c['field']]==3
    for raw in [response(code='600001'),response(day='2024-12-31'),response(unit=''),response(column='其他指标')]:assert validate(raw)[0] is None

def test_unit_conversion_and_no_user_evidence():
    c,_=validate(response(unit='万股',value=2),req(unit='股'));assert c['data'][0][c['field']]==20000
    assert validate(response(unit=''),req(unit='天'))[0] is None
    assert validate(response(unit='股'),req(unit='元'))[0] is None

def test_financial_scope():
    r=e.ExplorationRequest(dataset='financials',symbol='600000.SH',period='2025-12-31',name='测试利润')
    assert validate(response(column='测试利润',unit='元'),r)[0]
    explicit=r.model_copy(update={'name':'测试累计利润','aliases':['测试利润']})
    assert validate(response(column='测试利润',unit='元'),explicit)[0] is None
    raw=response(column='测试利润',unit='元');raw['columns'].append({'name':'口径'});raw['rows'][0].append('报告期累计')
    assert validate(raw,r)[0]

def test_ifind_row_date_beats_mrq():
    raw={'answer':'|证券代码|日期|测试周转天数(天)|\n|---|---|---|\n|600000|2025-12-31|3|','indicators_params':{'测试周转天数':{'报告期':'MRQ'}}}
    assert validate(raw)[0]

def test_existing_reuses_query():
    r=e.ExplorationRequest(dataset='valuations',symbol='600000.SH',date='2025-12-31',name='市盈率')
    with patch.object(e.core,'query',new=AsyncMock(return_value={'data':[]})) as query,patch.object(e,'invoke',new=AsyncMock()) as invoke:
        # Existing dictionaries may use a more specific PE label; use exact stored label.
        f,m=next(iter(e.dictionary.fields('valuations').items()));r.name=m['label']
        result=asyncio.run(e.explore(r));assert result['status']=='existing';query.assert_awaited_once();invoke.assert_not_called()

def test_bounded_attempts_and_persist(tmp_path):
    settings={'routes':{'valuations':['tushare','wind','ifind','tdx']},'providers':{p:{'timeout_seconds':60} for p in ['tushare','wind','ifind','tdx']}}
    with patch.object(e.config,'ROOT',tmp_path),patch.object(e.config,'load',return_value=settings),patch.object(e.config,'redact',side_effect=lambda x:x),patch.object(e.core,'available',return_value=True),patch.object(e,'invoke',new=AsyncMock(return_value=response())) as invoke:
        result=asyncio.run(e.explore(req()));assert result['status']=='success';assert invoke.await_count==1
        assert json.loads((tmp_path/'local/explorations'/f"{result['id']}.json").read_text())['candidate']

def test_save_revalidates_and_dictionary_accepts(tmp_path):
    settings={'routes':{'valuations':['ifind']},'providers':{'ifind':{'timeout_seconds':60}}}
    snapshot=__import__('copy').deepcopy(e.dictionary.DICTIONARY)
    try:
        with patch.object(e.config,'ROOT',tmp_path),patch.object(e.config,'load',return_value=settings),patch.object(e.config,'redact',side_effect=lambda x:x),patch.object(e.core,'available',return_value=True),patch.object(e,'invoke',new=AsyncMock(return_value=response())):
            result=asyncio.run(e.explore(req()))
            saved=asyncio.run(e.save_exploration(result['id'],'turnover_days_test'))
            assert saved['status']=='saved'
            assert e.dictionary.plan('ifind','valuations',['turnover_days_test'])
            with pytest.raises(ValueError,match='字段ID已存在'):
                asyncio.run(e.save_exploration(result['id'],'turnover_days_test'))
            rawpath=tmp_path/'local/explorations'/f"{result['id']}.raw.json"
            raw=json.loads(rawpath.read_text());raw[0]['raw']['rows'][0][0]='600001';rawpath.write_text(json.dumps(raw))
            with pytest.raises(ValueError):asyncio.run(e.save_exploration(result['id'],'other_test'))
    finally:
        e.dictionary.DICTIONARY.clear();e.dictionary.DICTIONARY.update(snapshot)

def test_timeout_kills_worker():
    class Process:
        returncode=None
        killed=False
        async def communicate(self,payload):await asyncio.Future()
        def kill(self):self.killed=True;self.returncode=-9
        async def wait(self):return self.returncode
    process=Process()
    with patch.object(e.asyncio,'create_subprocess_exec',new=AsyncMock(return_value=process)):
        with pytest.raises(asyncio.TimeoutError):asyncio.run(e.invoke('ifind',req(),e._recipe('ifind','valuations'),0.001))
    assert process.killed

def test_real_wind_replay_and_future_year_binding():
    from pathlib import Path
    from gateway.acquisition import execute_recipe
    base=Path(__file__).resolve().parents[1]/'local/cross_source_expansion/wind'
    if not base.exists():pytest.skip('Local upstream replay files unavailable')
    raw=json.loads((base/'financials_response.json').read_text())['response']
    request=e.ExplorationRequest(dataset='financials',symbol='600519.SH',period='2024-12-31',name='应收账款周转天数')
    recipe=e._recipe('wind','financials')
    candidate,reasons=e.validate_response(request,'wind',recipe,raw)
    assert candidate,reasons
    binding=candidate['definition']['sources']['wind'][0]
    assert binding['columns']==['应收账款周转天数']
    assert candidate['data'][0]['date']=='2024-12-31'
    # Exact captured shape with a different year's column: the saved binding must
    # work via ordinary acquisition without another exploration.
    future=json.loads(json.dumps(raw,ensure_ascii=False).replace('2024年','2025年'))
    class Remote:
        def call(self,method,params):return future
    q=request.model_dump();q.update(period='2025-12-31',adjust='none')
    field=candidate['field']
    with patch.dict(e.dictionary.fields('financials'),{field:candidate['definition']}):
        rows,_=execute_recipe('future',recipe,{field:binding},q,None,Remote())
    assert rows[0]['date']=='2025-12-31' and rows[0][field]==0.0836
    valuations=json.loads((base/'valuations_response.json').read_text())['response']
    r=e.ExplorationRequest(dataset='valuations',symbol='600519.SH',date='2025-11-06',name='总股本')
    c,reasons=e.validate_response(r,'wind',e._recipe('wind','valuations'),valuations)
    assert c,reasons
    assert c['data'][0]['date']=='2025-11-06'
    # Year-only evidence cannot establish a nonannual reporting date.
    assert e.validate_response(request.model_copy(update={'period':'2024-09-30'}),'wind',recipe,raw)[0] is None
