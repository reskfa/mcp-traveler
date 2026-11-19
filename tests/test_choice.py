import json
from pathlib import Path
from gateway import config, dictionary, generic_data as g
from gateway.acquisition import acquire
from gateway.core import Query
from gateway.parsing import tables

FIX=Path(__file__).parent/'fixtures/choice_mx'

def raw(name):
    return json.loads((FIX/name).read_text())

def remote(payload):
    class Remote:
        def call(self,tool,args):
            assert tool in {'mx_ashare_finance_data','mx_bond_finance_data'}
            return payload
    return Remote()


def test_builtin_dictionary_has_choice_on_every_field():
    builtin=json.loads(dictionary.PATH.read_text())
    assert 'choice' in builtin['providers']
    assert {ds:len(g['fields']) for ds,g in builtin['datasets'].items()}=={'daily_prices':7,'valuations':17,'financials':169,'technical_indicators':401,'basic_info':56,'cb_basic':21,'cb_daily':19,'cb_clauses':12}
    assert builtin['recipes']['choice_technical']['method']=='mx_ashare_finance_data'
    assert builtin['datasets']['valuations']['fields']['volume_ratio']['sources']['choice']
    assert builtin['datasets']['financials']['fields']['current_ratio']['sources']['choice']
    assert not builtin['datasets']['cb_clauses']['fields']['call_date']['sources']['choice']
    for group in builtin['datasets'].values():
        for field,meta in group['fields'].items():
            assert 'choice' in meta['sources'], field
    assert builtin['recipes']['choice_daily_prices']['method']=='mx_ashare_finance_data'
    assert builtin['recipes']['choice_cb_daily']['method']=='mx_bond_finance_data'


def test_unpivot_daily_browser_sheet():
    rows=[r for r,_ in tables(raw('daily.raw.json'))]
    assert {r['date'] for r in rows}=={'2025-11-03','2025-11-04','2025-11-05'}
    day=next(r for r in rows if r['date']=='2025-11-05')
    assert day['证券代码']=='600519.SH' and day['收盘价']=='1420.08元' and day['成交量']=='344.8万股'


def test_acquire_daily_valuations_financials():
    q=Query(dataset='daily_prices',symbol='600519.SH',start_date='2025-11-03',end_date='2025-11-05',fields=['open','high','low','close','volume','amount']).model_dump()
    data=acquire('choice',q,remote=remote(raw('daily.raw.json')))['data']
    assert [r['date'] for r in data]==['2025-11-05','2025-11-04','2025-11-03']
    assert data[0]['close']==1420.08 and data[0]['volume']==3448000 and data[0]['amount']==4908000000
    q=Query(dataset='valuations',symbol='600519.SH',date='2025-11-05',fields=['pe_ttm','pb','total_mv','circ_mv']).model_dump()
    row=acquire('choice',q,remote=remote(raw('valuations.raw.json')))['data'][0]
    assert row['pe_ttm']==19.75 and row['pb']==6.918
    assert row['total_mv']==1778000000000 and row['circ_mv']==1778000000000
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['revenue','net_profit_parent']).model_dump()
    row=acquire('choice',q,remote=remote(raw('financials.raw.json')))['data'][0]
    assert row['revenue']==170900000000 and row['net_profit_parent']==86230000000


def test_acquire_basic_info_and_cb_name():
    q=Query(dataset='basic_info',symbol='600519.SH',fields=['company_name','listing_date','sw_industry_l1']).model_dump()
    row=acquire('choice',q,remote=remote(raw('basic.raw.json')))['data'][0]
    assert row['company_name']=='贵州茅台酒股份有限公司'
    assert row['listing_date']=='2001-08-27' and row['sw_industry_l1']=='食品饮料'
    q=Query(dataset='cb_basic',symbol='113050.SH',fields=['name']).model_dump()
    assert acquire('choice',q,remote=remote(raw('cb.raw.json')))['data'][0]['name']=='南银转债'


def test_mx_tools_explorable_despite_vendor_annotations():
    schema={'type':'object','properties':{'query':{'type':'string'}},'required':['query']}
    ann={'readOnlyHint':False,'destructiveHint':True}
    assert g.runnable({'name':'mx_ashare_finance_data','description':'A股','inputSchema':schema,'annotations':ann})
    assert g.runnable({'name':'mx_bond_finance_data','description':'可转债','inputSchema':schema,'annotations':ann})
    assert not g.runnable({'name':'mx_finance_search_news','description':'新闻','inputSchema':schema,'annotations':ann})
    assert not g.runnable({'name':'mx_finance_search_notice','description':'公告','inputSchema':schema,'annotations':ann})


def test_query_accepts_choice_provider():
    q=Query(dataset='daily_prices',symbol='600519.SH',start_date='2025-11-03',end_date='2025-11-05',provider='choice')
    assert q.provider=='choice'
    assert 'choice' in config.PROVIDERS and 'choice' in config.MCP_PROVIDERS
    assert config.KEYS['choice']=='CHOICE_MX_KEY'
    example=json.loads((Path(__file__).parents[1]/'config.example.json').read_text())
    assert set(example['providers'])==set(config.PROVIDERS)
    assert 'choice' in example['global_route']


def test_acquire_scanned_valuations_financials_basic():
    q=Query(dataset='valuations',symbol='600519.SH',date='2025-11-05',fields=['volume_ratio','dv_ttm']).model_dump()
    row=acquire('choice',q,remote=remote(raw('valuations_extra.raw.json')))['data'][0]
    assert abs(row['volume_ratio']-0.9673)<1e-6 and abs(row['dv_ttm']-3.637)<1e-6
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['current_ratio','revenue_ps']).model_dump()
    row=acquire('choice',q,remote=remote(raw('financials_extra.raw.json')))['data'][0]
    assert abs(row['current_ratio']-4.454)<1e-6 and abs(row['revenue_ps']-136)<1e-6
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['working_capital']).model_dump()
    row=acquire('choice',q,remote=remote(raw('financials_capital.raw.json')))['data'][0]
    assert row['working_capital']==195200000000
    q=Query(dataset='basic_info',symbol='600519.SH',fields=['listing_board','listing_status']).model_dump()
    row=acquire('choice',q,remote=remote(raw('basic_extra.raw.json')))['data'][0]
    assert row['listing_board']=='上交所主板' and row['listing_status']=='是'


def test_acquire_choice_technical_and_cb_archive():
    q=Query(dataset='technical_indicators',symbol='600519.SH',start_date='2025-11-03',end_date='2025-11-05',fields=['choice_kdj_k','choice_macd_hist','choice_ma20']).model_dump()
    data=acquire('choice',q,remote=remote(raw('technical.raw.json')))['data']
    day=next(r for r in data if r['date']=='2025-11-05')
    assert abs(day['choice_kdj_k']-12.35)<1e-6 and abs(day['choice_macd_hist']-(-5.023))<1e-6
    assert day['choice_ma20']==1388
    q=Query(dataset='cb_basic',symbol='113050.SH',fields=['put_clause','rate_clause','conversion_price']).model_dump()
    row=acquire('choice',q,remote=remote(raw('cb_basic_extra.raw.json')))['data'][0]
    assert '回售' in (row['put_clause'] or '') and row['conversion_price']==8.02
    assert dictionary.DICTIONARY['datasets']['cb_clauses']['fields']['call_date']['unavailable']['choice'].startswith('已询问')
