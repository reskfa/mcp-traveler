import asyncio
import copy
from contextlib import asynccontextmanager
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gateway import config,core,dictionary
from gateway import generic_data as g
from gateway.app import app


def request(**kw):
    return g.ExploreDataRequest(**{'request':'前十大股东名单及持股比例','symbol':'300308.SZ','period':'2025-12-31',**kw})

def response(count=10,scope='前十大股东',symbol='300308.SZ',date='2025年12月31日',names=None):
    return {'content':[{'type':'text','text':json.dumps({'data':{'columns':[{'name':'Wind代码'},{'name':date+scope+'名称'},{'name':date+scope+'持股数量','unit':'股'},{'name':date+scope+'持股比例','unit':'%'},{'name':'排名'}],
        'rows':[[symbol,(names[i] if names is not None else f'股东{i+1}'),1000-i,10-i/10,i+1] for i in range(count)]}},ensure_ascii=False)}]}

def capability(name='get_stock_equity_holders',description='返回股本结构与股东构成，包括前十大股东、前十大流通股东、机构股东持股'):
    return {'name':name,'description':description,'inputSchema':{'type':'object','properties':{'question':{'type':'string','description':'证券实体与报告期'}},'required':['question']},'annotations':{'readOnlyHint':True}}

@pytest.fixture
def home(tmp_path,monkeypatch):
    @asynccontextmanager
    async def http_lifespan(app):yield
    monkeypatch.setattr(app.router,'lifespan_context',http_lifespan)
    snapshot=copy.deepcopy(dictionary.DICTIONARY)
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setattr(core,'available',lambda *a:True)
    monkeypatch.setattr(core,'record',lambda *a:None)
    settings={'global_route':['wind','ifind','tdx','tushare','akshare'],'routes':{},'providers':{p:{'enabled':True,'timeout_seconds':10} for p in config.PROVIDERS}}
    config.write_json(tmp_path/'config.json',settings)
    (tmp_path/'.env').write_text('')
    for p in config.MCP_PROVIDERS:config.write_json(tmp_path/f'local/schemas/{p}.json',{'tools':[capability()] if p=='wind' else []})
    yield tmp_path
    dictionary.DICTIONARY.clear();dictionary.DICTIONARY.update(snapshot)
    from gateway.catalog import refresh_catalog
    refresh_catalog()


def test_exact_topic_date_identity_and_units():
    r,errors=g.validate_records(response(),request())
    assert not errors and len(r['data'])==10
    assert r['data'][0]['date']=='2025-12-31'
    assert r['columns']['前十大股东持股数量']['unit']=='股'
    assert r['columns']['前十大股东持股比例']['unit']=='%'
    assert r['coverage']['status']=='requested_count_verified'
    for raw in [response(scope='控股股东'),response(scope='前十大流通股东'),response(symbol='600519.SH'),response(date='2025年'),response(date='2025年6月30日')]:
        rejected,reasons=g.validate_records(raw,request())
        assert rejected is None and reasons


def test_no_prefabricated_top10_definition():
    built=json.loads(dictionary.PATH.read_text())
    assert not any('前十大股东' in json.dumps(f,ensure_ascii=False) for ds in built['datasets'].values() for f in ds['fields'].values())
    assert 'data_queries' not in built


def test_partial_and_bad_records():
    r,_=g.validate_records(response(count=4),request())
    assert r['coverage']['status']=='partial' and r['save_allowed']
    r,_=g.validate_records(response(names=[None]*10),request())
    assert r is None
    r,_=g.validate_records(response(names=['同一股东']*10),request())
    assert not r['save_allowed'] and r['coverage']['status']=='partial'
    raw=json.loads(response()['content'][0]['text']);raw['data']['rows'][1][-1]=1
    r,_=g.validate_records(raw,request())
    assert not r['save_allowed']


def test_latest_does_not_combine_periods():
    first=json.loads(response(count=5,date='2024年12月31日')['content'][0]['text'])
    second=json.loads(response(count=5,date='2025年12月31日')['content'][0]['text'])
    r,_=g.validate_records({'data':[first,second]},request(period=None))
    assert len(r['data'])==5 and {v['date'] for v in r['data']}=={'2025-12-31'}
    assert r['coverage']['status']=='partial'


def test_bad_calendar_date_rejected():
    raw=json.loads(response()['content'][0]['text'])['data']
    for c in raw['columns']:c['name']=c['name'].replace('2025年12月31日','')
    raw['columns'].append({'name':'报告期'})
    for row in raw['rows']:row.append('2025-99-99')
    assert g.validate_records(raw,request(period=None))[0] is None


def test_capability_search_uses_upstream_description_and_preserves_scope(home,monkeypatch):
    async def check():
        r=await g.search_capabilities('前十大股东名单及持股比例')
        candidates=[x for x in r['results'] if x.get('kind')=='upstream_tool']
        assert candidates[0]['tool']=='get_stock_equity_holders'
        assert candidates[0]['status']=='explorable'
        assert 'input_schema' not in candidates[0] and candidates[0]['required_parameters']==['question']
        assert not any(x.get('field')=='controlling_shareholder' and x.get('exact_match') for x in r['results'])
    asyncio.run(check())
    assert not g.runnable(capability('search_news','新闻片段'))
    assert not g.runnable(capability('delete_records','删除数据')|{'annotations':{}})


def test_saved_recipe_reuses_new_symbol_and_period_and_can_be_deleted(home,monkeypatch):
    calls=[]
    async def invoke(provider,q,timeout):
        calls.append(q)
        if q.get('probe'):return {'tools':[]}
        args=q['generic_call']['arguments']['question']
        if '600519.SH' in args:
            assert '300308.SZ' not in args and '2025-12-31' not in args
            return response(symbol='600519.SH',date='2024年12月31日')
        return response()
    monkeypatch.setattr(core,'invoke',invoke)
    async def check():
        assert dictionary.search_metrics('前十大股东')['total']==0
        r=await g.explore_data(request())
        assert r['status']=='success' and r['saved']
        key=r['saved_query_id']
        stored=json.loads((home/'local/user_dictionary.json').read_text())
        assert key in stored['data_queries'] and not stored.get('datasets',{})
        assert json.loads(Path(r['data_path']).read_text())['capability_search']['tools'][0]['tool']=='get_stock_equity_holders'
        found=dictionary.search_metrics('前十大股东')['results'][0]
        assert found['query_tool']=='query_data' and found['query_id']==key
        again=await g.query_data(g.DataQueryRequest(query_id=key,symbol='600519.SH',period='2024-12-31'))
        assert again['status']=='success' and again['reused']
        assert again['data'][0]['symbol']=='600519.SH' and again['data'][0]['date']=='2024-12-31'
        assert dictionary.delete_user_metric('records',key)['deleted']
        assert dictionary.search_metrics('前十大股东')['total']==0
    asyncio.run(check())


def test_ambiguous_reused_recipe_not_success(home,monkeypatch):
    monkeypatch.setattr(core,'invoke',AsyncMock(return_value=response()))
    r=asyncio.run(g.explore_data(request()))
    monkeypatch.setattr(core,'invoke',AsyncMock(return_value=response(names=['同一股东']*10)))
    r=asyncio.run(g.query_data(g.DataQueryRequest(query_id=r['saved_query_id'],symbol='300308.SZ',period='2025-12-31')))
    assert r['status']=='needs_confirmation' and not r['complete']


def test_no_save_on_substitute_and_no_arbitrary_method(home,monkeypatch):
    monkeypatch.setattr(core,'invoke',AsyncMock(return_value=response(scope='前十大流通股东')))
    r=asyncio.run(g.explore_data(request()))
    assert r['status']=='needs_confirmation' and not r['saved']
    assert not (home/'local/user_dictionary.json').exists()
    with pytest.raises(ValueError,match='只读'):
        g.fetch_remote('wind',{},'',{'tool':'some_unknown_method','arguments':{}})


def test_http_and_mcp_tool_contract(home,monkeypatch):
    monkeypatch.setattr(core,'invoke',AsyncMock(return_value=response()))
    with TestClient(app) as client:
        caps=client.get('/api/capabilities',params={'query':'前十大股东','provider':'wind'}).json()
        assert caps['results'][0]['tool']=='get_stock_equity_holders'
        r=client.post('/api/explore-data',json=request().model_dump()).json()
        assert r['saved']
        assert client.get(r['download_url']).status_code==200
        assert client.get(r['raw_url']).status_code==200
        assert client.post('/api/data-query',json={'query_id':r['saved_query_id'],'symbol':'300308.SZ','period':'2025-12-31'}).json()['reused']
        assert client.post('/api/explore-data',json={},headers={'Origin':'https://other.example'}).status_code==403
    from gateway.app import mcp
    names=[t.name for t in asyncio.run(mcp.list_tools())]
    assert {'explore_field','save_explored_field','query_data','search_capabilities'}<=set(names)


def test_parenthetical_and_long_request_match_topic():
    for text in ['前十大股东名单（持股数、比例、变动）', '前十大股东名单：每行一个股东，需要股东名称列。注意日期前缀']:
        assert g.main_topic(text)=='前十大股东'
        assert g.relevance(text,capability())>0
        result,_=g.validate_records(response(),request(request=text))
        assert len(result['data'])==10


def test_unverified_rows_return_without_relabeling_or_save(home,monkeypatch):
    monkeypatch.setattr(core,'invoke',AsyncMock(return_value=response(symbol='002988.SZ',date='2026年9月30日')))
    r=asyncio.run(g.explore_data(request()))
    assert r['status']=='needs_confirmation' and not r['saved']
    assert len(r['data'])==10 and r['validation_status']=='unverified'
    assert r['data'][0]['Wind代码']=='002988.SZ'
    assert r['candidates'][0]['warnings']
    assert not (home/'local/user_dictionary.json').exists()
    assert json.loads(Path(r['data_path']).read_text())['data']==r['data']


def market_response(count=120,date='2026-11-20'):
    return {'content':[{'type':'text','text':json.dumps({'summary':f'共计 {count} 条数据','data':{
        'columns':[{'name':'债券代码'},{'name':'交易日期'},{'name':'收盘价','unit':'元'},{'name':'涨跌幅','unit':'%'}],
        'rows':[[f'{120000+i:06d}.SZ',date,100+i,i/10] for i in range(count)]}},ensure_ascii=False)}]}


def market_request(**kw):
    return g.ExploreDataRequest(**{'request':'收盘价及涨跌幅','query_scope':'market','market_kind':'daily',
        'market_hint':'convertible_bond','start_date':'2026-11-20','end_date':'2026-11-20',**kw})


def test_market_request_requires_no_symbol_and_explicit_kind():
    assert market_request().symbol is None
    with pytest.raises(ValueError,match='不接受symbol'):
        market_request(symbol='120001.SZ')
    with pytest.raises(ValueError,match='market_kind'):
        g.ExploreDataRequest(request='日线',query_scope='market',market_hint='equity')
    with pytest.raises(ValueError,match='有效symbol'):
        g.ExploreDataRequest(request='前十大股东')


def test_market_validation_preserves_identities_and_full_coverage():
    result,errors=g.validate_market_records(market_response(),market_request())
    assert not errors and len(result['data'])==120
    assert result['coverage']['status']=='complete'
    assert result['coverage']['distinct_symbols']==120
    assert len({row['symbol'] for row in result['data']})==120
    reused,_=g.validate_market_records(market_response(date='2026-11-19'),market_request())
    assert reused is None


def test_market_choice_security_rows_by_date_columns_are_expanded():
    raw={'data':[{'columns':['涨跌幅','2026-11-20'],'items':[
        ['珀莱转债(113634.SH)','0.6035'],['煜邦转债(118039.SH)','-0.07696']]}]}
    wrapped={'content':[{'type':'text','text':json.dumps(raw,ensure_ascii=False)}]}
    for payload in (raw,wrapped):
        result,errors=g.validate_market_records(payload,market_request(request='涨跌幅'))
        assert not errors and len(result['data'])==2
        assert [row['symbol'] for row in result['data']]==['113634.SH','118039.SH']
        assert {row['date'] for row in result['data']}=={'2026-11-20'}
        assert result['data'][0]['涨跌幅']=='0.6035'
    assert raw['data'][0]['columns']==['涨跌幅','2026-11-20']


def test_market_conflicting_duplicate_not_saved():
    raw=json.loads(market_response(count=2)['content'][0]['text'])
    raw['data']['rows'].append(['120000.SZ','2026-11-20',999,0])
    result,_=g.validate_market_records(raw,market_request())
    assert result['coverage']['status']=='partial' and not result['save_allowed']
    assert any('冲突' in note for note in result['notes'])


def test_market_explore_save_reuse_and_no_100_row_trim(home,monkeypatch):
    config.write_json(home/'local/schemas/wind.json',{'tools':[capability('mx_ashare_finance_data','全部A股和可转债日线行情、收盘价、涨跌幅')]})
    calls=[]
    async def invoke(provider,q,timeout):
        if q.get('probe'):return {'tools':[]}
        args=q['generic_call']['arguments'];calls.append(args)
        text=args['question']
        assert 'size' not in args and '120000.SZ' not in text
        assert '逐券日行情' in text and '成交统计' in text
        return market_response(date='2026-11-19' if '2026-11-19' in text else '2026-11-20')
    monkeypatch.setattr(core,'invoke',invoke)
    async def check():
        explored=await g.explore_data(market_request())
        assert explored['status']=='success' and explored['saved'] and explored['complete']
        assert len(explored['data'])==120 and explored['row_count']==120
        definition=json.loads((home/'local/user_dictionary.json').read_text())['data_queries'][explored['saved_query_id']]
        assert definition['query_scope']=='market' and definition['market_kind']=='daily'
        assert 'symbols' not in definition and '120000.SZ' not in json.dumps(definition)
        reused=await g.query_data(g.DataQueryRequest(query_id=explored['saved_query_id'],start_date='2026-11-19',end_date='2026-11-19'))
        assert reused['reused'] and len(reused['data'])==120
        assert {row['date'] for row in reused['data']}=={'2026-11-19'}
        assert len(calls)==2 and all('size' not in args for args in calls)
        found=await g.search_capabilities('收盘价',market_hint='convertible_bond',query_scope='market',market_kind='daily')
        assert any(row.get('query_id')==explored['saved_query_id'] for row in found['results'])
    asyncio.run(check())


def test_market_rejects_fixed_size_tool():
    tool=capability()
    tool['inputSchema']['properties']['size']={'type':'integer'}
    tool['inputSchema']['required'].append('size')
    with pytest.raises(ValueError,match='固定size'):
        g.build_arguments(tool,market_request())


def test_market_rejects_wrong_security_type_and_substitute_columns():
    raw=json.loads(market_response(count=1)['content'][0]['text'])
    raw['data']['rows'][0][0]='600519.SH'
    assert g.validate_market_records(raw,market_request())[0] is None
    raw=json.loads(market_response(count=1)['content'][0]['text'])
    raw['data']['columns']=[raw['data']['columns'][0],raw['data']['columns'][1],raw['data']['columns'][3]]
    raw['data']['rows']=[[row[0],row[1],row[3]] for row in raw['data']['rows']]
    assert g.validate_market_records(raw,market_request(request='收盘价'))[0] is None


def test_market_universe_historical_filter_and_standard_fields():
    raw={'data':{'columns':[{'name':'债券代码'},{'name':'债券简称'},{'name':'上市日期'},{'name':'摘牌日期'}],
        'rows':[['120001.SZ','存续','2020-01-01',None],['120002.SZ','已退出','2020-01-01','2024-01-01'],
                ['120003.SZ','尚未上市','2025-01-01',None],['120004.SZ','过早退出','2020-01-01','2022-01-01']]}}
    request=market_request(request='证券名单',market_kind='universe',start_date=None,end_date='2023-01-01')
    result,errors=g.validate_market_records(raw,request)
    assert not errors and [row['symbol'] for row in result['data']]==['120001.SZ','120002.SZ']
    assert result['data'][1]['delist_date']=='2024-01-01'
    assert result['columns']['list_date']['label']=='上市日期'
    assert result['coverage']['status']=='not_verified'


def test_market_empty_requested_values_are_not_complete():
    raw=json.loads(market_response(count=2)['content'][0]['text'])
    for row in raw['data']['rows']:row[2]=None
    assert g.validate_market_records(raw,market_request(request='收盘价'))[0] is None
    raw['data']['rows'][1][2]=101
    result,_=g.validate_market_records(raw,market_request())
    assert result['coverage']['status']=='partial' and result['save_allowed']
    universe={'data':{'columns':[{'name':'债券代码'},{'name':'债券简称'},{'name':'上市日期'}],
                      'rows':[['120001.SZ',None,'2020-01-01']]}}
    request=market_request(request='证券名单',market_kind='universe',start_date=None,end_date=None)
    assert g.validate_market_records(universe,request)[0] is None


def test_market_multiday_total_count_does_not_prove_daily_coverage():
    request=market_request(start_date='2026-11-19',end_date='2026-11-20')
    result,_=g.validate_market_records(market_response(count=1,date='2026-11-19'),request)
    assert result['coverage']['status']=='not_verified'
    assert '逐交易日' in result['coverage']['note']


def test_market_unrelated_header_cannot_supply_trade_date():
    raw={'data':{'columns':[{'name':'债券代码'},{'name':'2026-11-19公司名称'},{'name':'收盘价'}],
                 'rows':[['120001.SZ','某公司',100]]}}
    assert g.validate_market_records(raw,market_request(request='收盘价'))[0] is None


def test_market_choice_tool_priority_and_same_provider_retry(home,monkeypatch):
    def choice_tool(name,description):
        tool=capability(name,description)
        schema=tool['inputSchema'];schema['properties']['query']=schema['properties'].pop('question')
        schema['required']=['query']
        return tool
    tools=[choice_tool('mx_index_block_finance_data','市场板块指数成交统计'),
           choice_tool('mx_ashare_finance_data','全部A股日线行情、收盘价、涨跌幅'),
           choice_tool('mx_bond_finance_data','全部可转债日线行情、收盘价、涨跌幅')]
    config.write_json(home/'local/schemas/choice.json',{'tools':tools})
    async def search():
        found=await g.search_capabilities('收盘价及涨跌幅',provider='choice',query_scope='market',
                                          market_kind='daily',market_hint='convertible_bond')
        upstream=[row['tool'] for row in found['results'] if row.get('kind')=='upstream_tool']
        assert upstream[0]=='mx_bond_finance_data'
        assert 'mx_ashare_finance_data' not in upstream
    asyncio.run(search())

    calls=[]
    async def invoke(provider,q,timeout):
        tool=q['generic_call']['tool'];calls.append(tool)
        if tool=='mx_bond_finance_data':return market_response()
        return {'data':{'columns':[{'name':'日期范围'}],'rows':[['2026-11-20']]}}
    monkeypatch.setattr(core,'invoke',invoke)
    capabilities=[{'provider':'choice','tool':'mx_index_block_finance_data'},
                  {'provider':'choice','tool':'mx_bond_finance_data'}]
    result=asyncio.run(g.run_capabilities(market_request(),capabilities))
    assert result['status']=='success' and result['tool']=='mx_bond_finance_data'
    assert result['network_calls']==2 and len(calls)==2
    assert [attempt['status'] for attempt in result['attempts']]==['needs_confirmation','success']
