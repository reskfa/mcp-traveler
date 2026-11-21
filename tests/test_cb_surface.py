import asyncio
import importlib
import pytest
from gateway import generic_data, exploration


def test_cb_market_is_explicit_in_upstream_arguments():
    tool={'inputSchema':{'properties':{'query':{'type':'string'},'market':{'type':'string'}},'required':['query']}}
    request=generic_data.ExploreDataRequest(request='前十大持有人',symbol='113050.SH')
    args=generic_data.build_arguments(tool,request)
    assert request.market_hint=='convertible_bond'
    assert args['market']=='可转债'
    assert '可转债证券113050.SH' in args['query']
    assert 'A股' not in args['query']
    with pytest.raises(ValueError):
        generic_data.ExploreDataRequest(request='估值',symbol='113050.SH',market_hint='equity')


def test_cb_cannot_reuse_equity_recipe(monkeypatch):
    monkeypatch.setattr(generic_data,'saved_queries',lambda:{'data_'+'a'*16:{'request':'持有人','provider':'ifind','tool':'query','market_hint':'equity'}})
    with pytest.raises(ValueError,match='证券类型不匹配'):
        asyncio.run(generic_data.query_data(generic_data.DataQueryRequest(query_id='data_'+'a'*16,symbol='113050.SH')))


def test_cb_unknown_numeric_does_not_explore_equity():
    with pytest.raises(ValueError,match='可转债'):
        request=exploration.ExplorationRequest(name='转股溢价率',dataset='valuations',symbol='113050.SH')
        asyncio.run(exploration.explore(request))


def test_mcp_cb_tools_route_to_cb_datasets(monkeypatch):
    module=importlib.import_module('gateway.app')
    seen=[]
    async def query(request):seen.append(request);return {'dataset':request.dataset}
    monkeypatch.setattr(module.core,'query',query)
    async def check():
        assert (await module.query_convertible_bond_basic('113050.SH'))['dataset']=='cb_basic'
        assert (await module.query_convertible_bond_daily('113050.SH','2026-11-16','2026-11-17'))['dataset']=='cb_daily'
        assert (await module.query_convertible_bond_clauses('113050.SH'))['dataset']=='cb_clauses'
        tools={t.name for t in await module.mcp.list_tools()}
        assert {'query_convertible_bond_basic','query_convertible_bond_daily','query_convertible_bond_clauses'}<=tools
    asyncio.run(check())
    assert all(q.symbol=='113050.SH' for q in seen)


def test_unknown_cb_can_discover_readonly_nl_candidate(monkeypatch):
    from gateway import config
    monkeypatch.setattr(config,'load',lambda:{'global_route':['wind']})
    monkeypatch.setattr(generic_data.dictionary,'search_metrics',lambda *a,**kw:{'results':[]})
    async def tools(*a):
        return [{'name':'get_stock_fundamentals','description':'股票财务与公司数据','annotations':{'readOnlyHint':True},'inputSchema':{'properties':{'question':{'type':'string'}},'required':['question']}}],None
    monkeypatch.setattr(generic_data,'ensure_tools',tools)
    result=asyncio.run(generic_data.search_capabilities('完全未知的债券数据',market_hint='convertible_bond'))
    assert result['results'][0]['confidence']=='low'
    assert result['results'][0]['market_support']=='not_verified'
    assert result['results'][0]['status']=='explorable'


def test_saved_cb_recipe_is_scoped_and_reuses_market(tmp_path,monkeypatch):
    import json
    from gateway import config,dictionary
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setattr(dictionary,'reload_dictionary',lambda *a,**kw:None)
    cap={'provider':'wind','tool':'get_stock_basicinfo'}
    result={'columns':{'票面利率':{'unit':'%'}},'data':[]}
    cb=generic_data.ExploreDataRequest(request='票面利率',symbol='113050.SH')
    equity=generic_data.ExploreDataRequest(request='票面利率',symbol='600519.SH')
    cb_id=generic_data.save_query(cb,cap,result,'a'*32)
    equity_id=generic_data.save_query(equity,cap,result,'b'*32)
    assert cb_id!=equity_id
    saved=json.loads(dictionary.user_dictionary_path().read_text())['data_queries'][cb_id]
    assert saved['market_hint']=='convertible_bond'
    async def ensure(*a):return [],None
    async def run(request,caps,query_id):
        assert request.market_hint=='convertible_bond'
        assert request.symbol=='123001.SZ'
        return {'saved_query_id':query_id}
    monkeypatch.setattr(generic_data,'ensure_tools',ensure)
    monkeypatch.setattr(generic_data,'run_capabilities',run)
    answer=asyncio.run(generic_data.query_data(generic_data.DataQueryRequest(query_id=cb_id,symbol='123001.SZ')))
    assert answer['reused'] and answer['saved_query_id']==cb_id


def test_http_cb_market_hint_contract(monkeypatch):
    import httpx
    module=importlib.import_module('gateway.app')
    async def capability(query,provider,refresh,limit,market_hint=None,query_scope='security',market_kind=None):return {'market_hint':market_hint,'query_scope':query_scope,'market_kind':market_kind}
    async def explore(request):return request.model_dump()
    monkeypatch.setattr(module,'find_capabilities',capability)
    monkeypatch.setattr(module,'explore_generic',explore)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),base_url='http://localhost') as client:
            r=await client.get('/api/capabilities',params={'query':'票面利率','market_hint':'convertible_bond'})
            assert r.status_code==200 and r.json()['market_hint']=='convertible_bond'
            r=await client.post('/api/explore-data',json={'request':'票面利率','symbol':'113050.SH','market_hint':'convertible_bond'})
            assert r.status_code==200 and r.json()['market_hint']=='convertible_bond'
    asyncio.run(check())
