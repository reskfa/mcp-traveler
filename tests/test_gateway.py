import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from gateway import config,core
from gateway.parsing import number,tables,map_rows
from gateway.app import app

@pytest.fixture
def home(tmp_path,monkeypatch):
    original=config.ROOT
    monkeypatch.setattr(config,'ROOT',tmp_path)
    (tmp_path/'config.json').write_text((original/'config.example.json').read_text())
    (tmp_path/'.env').write_text('TUSHARE_TOKEN="test-secret-value"\nWIND_API_KEY="test-wind-value"\n# preserve\nOTHER="keep"\n')
    return tmp_path

def test_units():
    assert number('1708.9915亿','元')==170899150000
    assert number('2.5万','亿元')==25000
    assert number('--') is None
    assert number('15.3%')==15.3

def test_vendor_tables():
    wind={'content':[{'text':json.dumps({'data':{'columns':[{'name':'MATCH'}],'rows':[['1420.08']]}})}]}
    nested={'data':{'data':[{'columns':[{'name':'营业收入','unit':'亿人民币'}],'rows':[[1708.9915]]}]}}
    assert map_rows(tables(wind),['close'],'600519.SH','daily_prices')[0][0]['close']==1420.08
    assert map_rows(tables(nested),['revenue'],'600519.SH','financials')[0][0]['revenue']==170899150000
    ifind={'data':{'answer':'|证券代码|营业收入（单位：元）|\n|---|---|\n|600519.SH|1708.9915亿|'}}
    assert map_rows(tables(ifind),['revenue'],'600519.SH','financials')[0][0]['revenue']==170899150000
    tdx={'content':[{'text':'摘要\n'+json.dumps({'Rows':[{'Data':'20251105','Close':'1420.08','RawVolume':3447527}]})},{'text':'[MCP App候选元数据] ignore'}]}
    mapped,_=map_rows(tables(tdx),['close','volume'],'600519.SH','daily_prices')
    assert mapped[0]['volume']==3447527 and mapped[0]['date']=='2025-11-05'

def test_secrets_and_config(home):
    config.save_provider('tushare',{'key':'new-secret-value','enabled':True})
    assert config.secrets()['OTHER']=='keep'
    assert '# preserve' in (home/'.env').read_text()
    assert 'new-secret-value' not in json.dumps(config.public())
    assert (home/'.env').stat().st_mode & 0o777 == 0o600
    config.save_routes({'daily_prices':['akshare','tushare']})
    assert config.load()['routes']['daily_prices'][0]=='akshare'

def test_public_survives_unknown_provider(home):
    c=json.loads((home/'config.json').read_text())
    c['providers']['future_source']={'enabled':False,'timeout_seconds':30}
    (home/'config.json').write_text(json.dumps(c))
    public=config.public()
    assert public['providers']['future_source']['official_url']==''
    assert 'future_source' in public['providers']

def test_origin_and_paths(home):
    with TestClient(app, raise_server_exceptions=True) as c:
        assert c.get('/api/config').status_code==200
        assert c.patch('/api/providers/tushare',json={'enabled':False},headers={'Origin':'https://evil.example'}).status_code==403
        assert c.get('/api/config',headers={'Host':'evil.example'}).status_code==403
        assert c.get('/api/results/not-an-id/raw').status_code==404
        assert c.post('/api/query',json={'dataset':'daily_prices','symbol':'600519.SH'}).status_code==422

def test_fallback_and_empty(home,monkeypatch):
    config.save_routes({"daily_prices":["tushare","akshare"]})
    async def fake(name,q,timeout):
        if name=='tushare':raise RuntimeError('simulated failure')
        return {'data':[],'raw':{},'notes':[]}
    monkeypatch.setattr(core,'invoke',fake)
    q=core.Query(dataset='daily_prices',symbol='600519.SH',start_date='2025-11-03',end_date='2025-11-05')
    r=asyncio.run(core.query(q))
    assert r['provider']=='akshare' and r['row_count']==0
    assert [a['status'] for a in r['attempts']]==['error','success']
    c=TestClient(app)
    assert c.get(r['download_url']).status_code==200
    assert c.get(r['raw_url']).json()=={}

def test_remote_session(monkeypatch):
    from gateway.providers import Remote
    calls=[]
    class Resp:
        status_code=200
        headers={'Mcp-Session-Id':'session-123'}
        def __init__(self,b):self.content=('event: message\ndata: '+json.dumps({'jsonrpc':'2.0','id':b.get('id'),'result':{'protocolVersion':'2024-11-05','tools':[]}})+'\n\n').encode()
    class Session:
        def __init__(self):self.headers={}
        def post(self,url,json,timeout):calls.append((json,dict(self.headers)));return Resp(json)
    monkeypatch.setattr('gateway.providers.requests.Session',Session)
    r=Remote('tdx',{'mcp_servers':{'stock':{'url':'https://example.com/mcp','auth_header':'Authorization','auth_prefix':'Bearer '}}},'key')
    r.rpc('tools/list',{})
    assert calls[1][1]['Mcp-Session-Id']=='session-123'
    assert calls[1][0]['method']=='notifications/initialized'
    assert 'id' not in calls[1][0]


def test_tdx_dated_valuation_array():
    payload={'content':[{'text':json.dumps([{'code':'600519','市盈率(PE,TTM)(交易日:20251105)':'19.7532','市净率(PB,MRQ)(交易日:20251105)':'6.9177'}])}]}
    rows,_=map_rows(tables(payload),['pe_ttm','pb'],'600519.SH','valuations')
    assert rows==[{'symbol':'600519.SH','date':'2025-11-05','pe_ttm':19.7532,'pb':6.9177}]


def test_trillion_and_financial_aliases():
    assert number('1.7972万亿')==1797200000000
    assert number(1.7972,'万亿元')==1797200000000
    rows=[({'日期':'20241231','扣除非经常性损益后的归属母公司股东净利润（单位：元）':'862.4091亿','净资产收益率ROE(加权,公布值)（单位：%）':'36.02','资产合计':298944579918.7,'其中：母公司股东权益':233105984399.47,'经营活动现金流量净额':92463692168.43}, {})]
    r,_=map_rows(rows,['net_profit_excl','roe','total_assets','equity_parent','operating_cashflow'],'600519.SH','financials')
    assert all(r[0][f] is not None for f in ['net_profit_excl','roe','total_assets','equity_parent','operating_cashflow'])
    r,_=map_rows([({'2024年年报扣除非经常性损益后归母净利润':10},{})],['net_profit_parent','net_profit_excl'],'600519.SH','financials','2024-12-31')
    assert r[0]['net_profit_parent'] is None and r[0]['net_profit_excl']==10
    r,_=map_rows([({'2025年11月6日总市值':1.7972},{'2025年11月6日总市值':'万亿元'})],['total_mv'],'600519.SH','valuations')
    assert r[0]['date']=='2025-11-06' and r[0]['total_mv']==1797200000000


def test_partial_fill_preserves_values_dates_and_sources(home,monkeypatch):
    config.save_routes({'valuations':['wind','akshare','tushare']})
    calls=[]
    async def fake(name,q,timeout):
        calls.append((name,q))
        if name=='wind':rows=[{'symbol':'600519.SH','date':'2025-11-06','pe_ttm':0,'pb':None}]
        elif name=='akshare':rows=[{'symbol':'600519.SH','date':'2025-11-05','pe_ttm':99,'pb':99}]
        else:rows=[{'symbol':'600519.SH','date':'2025-11-06','pe_ttm':22,'pb':7}]
        return {'data':rows,'raw':rows}
    monkeypatch.setattr(core,'invoke',fake)
    r=asyncio.run(core.query(core.Query(dataset='valuations',symbol='600519.SH',fields=['pe_ttm','pb'])))
    assert r['data'][0]['pe_ttm']==0 and r['data'][0]['pb']==7
    assert calls[1][1]['fields']==['pb'] and calls[1][1]['date']=='2025-11-06'
    assert r['field_sources']==[{'pe_ttm':'wind','pb':'tushare'}]
    assert r['complete'] and r['provider']=='mixed'
    assert [a['status'] for a in r['attempts']]==['partial','partial','success']
    full=json.loads(open(r['data_path']).read());assert full['field_sources']==r['field_sources']
    raw=json.loads(open(r['raw_path']).read());assert len(raw)==3
    calls.clear()
    r=asyncio.run(core.query(core.Query(dataset='valuations',symbol='600519.SH',fields=['pe_ttm','pb'],provider='wind')))
    assert len(calls)==1 and r['missing_fields']==['pb']


def test_partial_survives_errors_and_empty(home,monkeypatch):
    config.save_routes({'financials':['wind','akshare','tushare']})
    async def fake(name,q,timeout):
        if name=='tushare':raise RuntimeError('unavailable')
        return {'data':[{'symbol':'600519.SH','date':'2024-12-31','revenue':10}] if name=='wind' else [],'raw':{}}
    monkeypatch.setattr(core,'invoke',fake)
    r=asyncio.run(core.query(core.Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['revenue','net_profit_excl'])))
    assert r['data'][0]['revenue']==10 and not r['complete']
    assert len(r['attempts'])==3 and r['missing_fields']==['net_profit_excl']


def test_fill_each_row_without_overwriting(home,monkeypatch):
    config.save_routes({'daily_prices':['wind','tushare']})
    async def fake(name,q,timeout):
        rows=[{'symbol':'600519.SH','date':'2025-11-05','close':10,'turnover_rate':None},{'symbol':'600519.SH','date':'2025-11-06','close':11,'turnover_rate':2}]
        if name=='tushare':rows=[{**r,'close':99,'turnover_rate':3} for r in rows]
        return {'data':rows,'raw':{}}
    monkeypatch.setattr(core,'invoke',fake)
    r=asyncio.run(core.query(core.Query(dataset='daily_prices',symbol='600519.SH',start_date='2025-11-05',end_date='2025-11-06',fields=['close','turnover_rate'])))
    assert [x['turnover_rate'] for x in r['data']]==[3,2]
    assert [x['close'] for x in r['data']]==[10,11]


def test_wind_separate_deduction_blocks(monkeypatch):
    from gateway.providers import remote_fetch
    calls=[]
    class FakeRemote:
        def __init__(self,*args):pass
        def call(self,tool,args):
            calls.append(args['question'])
            deduction='扣除非经常性' in args['question']
            col='2024年年报扣除非经常性损益后归母净利润' if deduction else '2024年归母净利润'
            return {'data':{'columns':[{'name':col,'unit':'亿元'}],'rows':[[8 if deduction else 10]]}}
    monkeypatch.setattr('gateway.providers.Remote',FakeRemote)
    q=core.Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['net_profit_parent','net_profit_excl']).model_dump()
    r=remote_fetch('wind',q,{},'')
    assert len(calls)==2 and len(r['data'])==1
    assert r['data'][0]['net_profit_parent']==1e9 and r['data'][0]['net_profit_excl']==8e8


def test_global_route_inheritance(home):
    config.save_routes({'global_route':['wind','akshare','tushare'],'route_overrides':{'financials':['tushare','wind']}})
    c=config.load()
    assert c['routes']['valuations']==['wind','akshare','tushare']
    assert c['routes']['financials']==['tushare','wind']
    config.save_routes({'global_route':['akshare','wind'],'route_overrides':{}})
    assert all(v==['akshare','wind'] for v in config.load()['routes'].values())
