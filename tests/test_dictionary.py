import copy
import json
from pathlib import Path
import pandas as pd
import pytest
from gateway.dictionary import DICTIONARY,validate,plan
from gateway.acquisition import acquire
from gateway.core import Query


def test_dictionary_complete_and_invalid_reference():
    builtin=json.loads((Path(__file__).parents[1]/'gateway/data/dictionary.json').read_text())
    assert {ds:len(g['fields']) for ds,g in builtin['datasets'].items()}=={'daily_prices':7,'valuations':17,'financials':169,'technical_indicators':401,'basic_info':56,'cb_basic':21,'cb_daily':19,'cb_clauses':12}
    d=copy.deepcopy(DICTIONARY)
    d['datasets']['daily_prices']['fields']['close']['sources']['wind'][0]['recipe']='not_a_recipe'
    with pytest.raises(KeyError):validate(d)


def test_new_field_method_and_mapping_only_from_dictionary(monkeypatch):
    from gateway.dictionary import reload_dictionary
    reload_dictionary(force=False)
    recipe={'provider':'tushare','dataset':'financials','kind':'sdk','method':'new_api','params':{'period':'$period_compact','fields':'$source_fields'},'select_columns':['end_date']}
    monkeypatch.setitem(DICTIONARY['recipes'],'test_recipe',recipe)
    field={'label':'测试字段','unit':'元','aliases':[],'sources':{p:[] for p in DICTIONARY['providers']}}
    field['sources']['tushare']=[{'recipe':'test_recipe','columns':['vendor_value'],'multiplier':10000}]
    monkeypatch.setitem(DICTIONARY['datasets']['financials']['fields'],'new_metric',field)
    calls=[]
    class SDK:
        def new_api(self,**kwargs):calls.append(kwargs);return pd.DataFrame([{'end_date':'20241231','vendor_value':2.5}])
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31').model_dump();q['fields']=['new_metric']
    r=acquire('tushare',q,sdk=SDK())
    assert calls==[{'period':'20241231','fields':'end_date,vendor_value'}]
    assert r['data'][0]['new_metric']==25000


def test_sdk_grouping_and_scaling():
    calls=[]
    class SDK:
        def daily(self,**kwargs):calls.append('daily');return pd.DataFrame([{'trade_date':'20251105','close':10,'vol':1.25,'amount':2}])
        def daily_basic(self,**kwargs):calls.append('basic');return pd.DataFrame([{'trade_date':'20251105','turnover_rate':0.5}])
    q=Query(dataset='daily_prices',symbol='600519.SH',start_date='2025-11-05',end_date='2025-11-05',fields=['close','volume','amount','turnover_rate']).model_dump()
    r=acquire('tushare',q,sdk=SDK())
    assert calls==['daily','basic']
    assert r['data']==[{'symbol':'600519.SH','date':'2025-11-05','close':10,'volume':125,'amount':2000,'turnover_rate':.5}]


def test_fallback_and_pivot():
    class SDK:
        def stock_zh_a_hist(self,**kwargs):raise RuntimeError('offline')
        def stock_zh_a_daily(self,**kwargs):return pd.DataFrame([{'date':'2025-11-05','volume':125,'turnover':.01}])
        def stock_financial_abstract(self,**kwargs):return pd.DataFrame([{'指标':'扣非净利润','20241231':12},{'指标':'净资产收益率_平均','20241231':20}])
    q=Query(dataset='daily_prices',symbol='600519.SH',start_date='2025-11-05',end_date='2025-11-05',fields=['volume','turnover_rate']).model_dump()
    r=acquire('akshare',q,sdk=SDK());assert r['data'][0]['volume']==125 and r['data'][0]['turnover_rate']==1
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['net_profit_excl','roe']).model_dump()
    r=acquire('akshare',q,sdk=SDK());assert r['data'][0]['net_profit_excl']==12 and r['data'][0]['roe']==20


def test_per_field_valuation_not_cross_mapped():
    class SDK:
        def stock_zh_valuation_baidu(self,**kwargs):return pd.DataFrame([{'date':'2025-11-05','value':15 if kwargs['indicator']=='市盈率(TTM)' else 3}])
    q=Query(dataset='valuations',symbol='600519.SH',date='2025-11-05',fields=['pe_ttm','pb']).model_dump()
    r=acquire('akshare',q,sdk=SDK());assert r['data'][0]['pe_ttm']==15 and r['data'][0]['pb']==3


def test_unavailable_field_does_not_call_upstream():
    assert not plan('tdx','financials',['roe'])
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['roe']).model_dump()
    with pytest.raises(RuntimeError,match='没有所请求字段'):acquire('tdx',q,remote=object())


def test_adjustment_uses_full_factor_history_and_keeps_raw():
    class SDK:
        def daily(self,**kwargs):return pd.DataFrame([{'trade_date':'20251105','close':100.0}])
        def adj_factor(self,**kwargs):
            assert kwargs=={'ts_code':'600519.SH'}
            return pd.DataFrame([{'trade_date':'20200101','adj_factor':1.0},{'trade_date':'20251105','adj_factor':2.0},{'trade_date':'20260101','adj_factor':4.0}])
    q=Query(dataset='daily_prices',symbol='600519.SH',start_date='2025-11-05',end_date='2025-11-05',fields=['close'],adjust='qfq').model_dump()
    r=acquire('tushare',q,sdk=SDK());assert r['data'][0]['close']==50
    assert r['raw'][0]['response'][1]['adjustment_factors'][-1]['adj_factor']==4


def test_tdx_pagination_parameters_from_dictionary(monkeypatch):
    recipe=copy.deepcopy(DICTIONARY['recipes']['tdx_kline']);recipe['pagination']['size']=2;recipe['params']['wantNum']='2'
    monkeypatch.setitem(DICTIONARY['recipes'],'tdx_kline',recipe)
    calls=[]
    class Remote:
        def call(self,tool,args):
            calls.append(args)
            ds=['20251107','20251106'] if args['startxh']=='0' else ['20251105']
            return {'Rows':[{'Data':d,'Close':10,'RawVolume':123} for d in ds]}
    q=Query(dataset='daily_prices',symbol='600519.SH',start_date='2025-11-05',end_date='2025-11-06',fields=['close','volume']).model_dump()
    r=acquire('tdx',q,remote=Remote())
    assert [c['startxh'] for c in calls]==['0','2']
    assert len(r['data'])==2 and all(row['volume']==123 for row in r['data'])


def test_catalog_uses_executable_binding():
    from gateway.catalog import dictionary_view
    view=dictionary_view('financials','net_profit_excl')
    source=view['fields']['net_profit_excl']['sources']['tushare'][0]
    assert source['columns']==['profit_dedt']
    assert view['recipes'][source['recipe']]['method']=='fina_indicator'


def test_official_coverage_and_defaults():
    import json
    from pathlib import Path
    coverage=json.loads((Path(__file__).parents[1]/'docs/tushare-coverage.json').read_text())
    for api,ds,n in [('daily_basic','valuations',17),('fina_indicator','financials',163)]:
        mapping=coverage[api]['fields']
        assert len(mapping)==n
        for source,field in mapping.items():
            bindings=DICTIONARY['datasets'][ds]['fields'][field]['sources']['tushare']
            assert any(source in b['columns'] for b in bindings) or (source=='eps' and field=='eps')
    assert len(Query(dataset='financials',symbol='600519.SH',period='2024-12-31').fields)==9
    assert len(Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['*']).fields)==len(DICTIONARY['datasets']['financials']['fields'])
    assert len(Query(dataset='valuations',symbol='600519.SH').fields)==4


def test_expanded_financial_fields_and_revision():
    class SDK:
        def fina_indicator(self,**kwargs):
            assert set(kwargs['fields'].split(',')) >= {'update_flag','invturn_days','q_netprofit_yoy'}
            return pd.DataFrame([
                {'end_date':'20241231','ann_date':'20250301','update_flag':'0','invturn_days':10.123456789012,'q_netprofit_yoy':2},
                {'end_date':'20241231','ann_date':'20250301','update_flag':'1','invturn_days':11.123456789012,'q_netprofit_yoy':-3.12}])
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['invturn_days','q_netprofit_yoy']).model_dump()
    row=acquire('tushare',q,sdk=SDK())['data'][0]
    assert row['invturn_days']==11.123456789012
    assert row['q_netprofit_yoy']==-3.12


def test_share_conversion_and_zero_limit_status():
    class SDK:
        def daily_basic(self,**kwargs):
            assert 'limit_status' in kwargs['fields'].split(',')
            return pd.DataFrame([{'trade_date':'20251105','total_share':1.25,'limit_status':0}])
    q=Query(dataset='valuations',symbol='600519.SH',date='2025-11-05',fields=['total_share','limit_status']).model_dump()
    row=acquire('tushare',q,sdk=SDK())['data'][0]
    assert row['total_share']==12500
    assert row['limit_status']==0


def test_explicit_growth_and_quarter_bindings_do_not_cross_fill():
    from gateway.parsing import map_rows
    table=[({'date':'2024-12-31','营业收入':100,'营业收入同比增长率':12,'单季度净利润':4},{})]
    bindings={'revenue':{'columns':['营业收入']},'or_yoy':{'columns':['营业收入同比增长率']},'q_dtprofit':{'columns':['单季度扣非净利润']}}
    rows,_=map_rows(table,list(bindings),'600519.SH','financials',bindings=bindings)
    assert rows[0]['revenue']==100 and rows[0]['or_yoy']==12
    assert rows[0]['q_dtprofit'] is None


def test_multiple_markdown_tables_keep_their_own_headers():
    from gateway.parsing import markdown_rows
    text='|代码|PS|\n|---|---|\n|600519|10|\n|代码|PB|\n|---|---|\n|600519|7|'
    assert markdown_rows(text)==[{'代码':'600519','PS':'10'},{'代码':'600519','PB':'7'}]


def test_response_units_are_checked_for_expanded_wind_fields():
    from gateway.parsing import map_rows
    b=DICTIONARY['datasets']['valuations']['fields']['ps_ttm']['sources']['wind'][0]
    rows,_=map_rows([({'市销率PS_TTM':1000},{'市销率PS_TTM':'%'})],['ps_ttm'],'600519.SH','valuations',bindings={'ps_ttm':b})
    assert rows==[]  # Unknown unit conversions must not silently become multiples.


def test_ifind_dated_query_rejects_latest_parameter_and_does_not_trust_question():
    from gateway.acquisition import execute_recipe,indicator_dates
    recipe={'provider':'ifind','kind':'mcp_nl','method':'test','params':{},'date_evidence':'indicator_params'}
    b={'revenue':{'columns':['营业收入']}}
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['revenue']).model_dump()
    class Remote:
        def call(self,*args):return {'data':{'answer':'|日期|营业收入|\n|---|---|\n|20241231|100|','indicators_params':{'营业收入':{'报告期':'最新一期(MRQ)'}}}}
    with pytest.raises(RuntimeError,match='最新报告期'):
        execute_recipe('test',recipe,b,q,None,Remote())
    assert indicator_dates({'request':{'question':'20241231'}})==(set(),False)


def test_tdx_unadjusted_close_does_not_accept_adjusted_close():
    from gateway.parsing import map_rows
    b={'close':{'columns':['收盘价']}}
    for mode,expected in [('不复权',100),('前复权',None)]:
        rows,_=map_rows([({f'收盘价(复权方式:{mode},交易日:20251106)':100},{})],['close'],'600519.SH','valuations',bindings=b)
        assert (rows[0]['close'] if rows else None)==expected


def test_year_columns_select_requested_year_instead_of_first_column():
    from gateway.acquisition import execute_recipe
    recipe={'kind':'mcp','method':'test','params':{},'date_evidence':'year_columns'}
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['dt_eps']).model_dump()
    class Remote:
        def call(self,*args):return {'rows':[{'2023年稀释每股收益':59.49,'2024年稀释每股收益':68.64}]}
    rows,_=execute_recipe('test',recipe,{'dt_eps':{'columns':['稀释每股收益']}},q,None,Remote())
    assert rows[0]['dt_eps']==68.64


def test_indicator_cannot_borrow_another_indicators_date():
    from gateway.acquisition import execute_recipe
    recipe={'kind':'mcp','method':'test','params':{},'date_evidence':'indicator_params'}
    q=Query(dataset='valuations',symbol='600519.SH',date='2025-11-06',fields=['pb']).model_dump()
    class Remote:
        def call(self,*args):return {'data':{'answer':'|市净率|\n|---|\n|7|','indicators_params':{'市净率':{},'无关指标':{'交易日期':'20251106'}}}}
    with pytest.raises(RuntimeError,match='没有识别'):
        execute_recipe('test',recipe,{'pb':{'columns':['市净率']}},q,None,Remote())


def test_indicator_own_date_and_unit_header():
    from gateway.acquisition import execute_recipe
    recipe={'kind':'mcp','method':'test','params':{},'date_evidence':'indicator_params'}
    q=Query(dataset='financials',symbol='600519.SH',period='2024-12-31',fields=['grossprofit_margin']).model_dump()
    class Remote:
        def call(self,*args):return {'data':{'answer':'|销售毛利率（单位：%）|\n|---|\n|91.93|','indicators_params':{'销售毛利率':{'报告期':'20241231'}}}}
    rows,_=execute_recipe('test',recipe,{'grossprofit_margin':{'columns':['销售毛利率'],'accepted_units':['%']}},q,None,Remote())
    assert rows[0]['date']=='2024-12-31' and rows[0]['grossprofit_margin']==91.93



def test_ifind_actual_table_date_overrides_default_mrq_metadata():
    from gateway.acquisition import execute_recipe
    recipe={'kind':'mcp','method':'test','params':{},'date_evidence':'table_or_indicator_params'}
    class Remote:
        def __init__(self,year,value):self.year=year;self.value=value
        def call(self,*args):return {'data':{'answer':f'|报告期|稀释每股收益|\n|---|---|\n|{self.year}1231|{self.value}|','indicators_params':{'稀释每股收益':{'报告期':'最新一期(MRQ)'}}}}
    for year,value in [(2023,59.49),(2024,68.64)]:
        q=Query(dataset='financials',symbol='600519.SH',period=f'{year}-12-31',fields=['dt_eps']).model_dump()
        rows,_=execute_recipe('test',recipe,{'dt_eps':{'columns':['稀释每股收益']}},q,None,Remote(year,value))
        assert rows[0]['date']==q['period'] and rows[0]['dt_eps']==value
    with pytest.raises(RuntimeError):
        execute_recipe('test',recipe,{'dt_eps':{'columns':['稀释每股收益']}},q,None,Remote(2023,59.49))


def test_source_specific_percentage_conversion_and_roe_metadata():
    from gateway.parsing import map_rows
    from gateway.catalog import source_definitions
    b=DICTIONARY['datasets']['valuations']['fields']['pe']['sources']['wind'][0]
    for value,unit in [(2084.2041,'%'),(20.842041,'倍'),('2084.2041%','')]:
        rows,_=map_rows([({'市盈率PE':value},{'市盈率PE':unit})],['pe'],'600519.SH','valuations',bindings={'pe':b})
        assert rows[0]['pe']==20.842041
    notes=source_definitions('financials','roe')
    assert '摊薄' in notes['wind'][0]['note'] and '加权' in notes['ifind'][0]['note']


def test_column_date_uses_own_cutoff_and_rejects_future_report():
    from gateway.parsing import map_rows
    b={'free_share':{'columns':['自由流通股本']}}
    rows,_=map_rows([({'自由流通股本(截止日期:20251106)':100},{})],['free_share'],'600519.SH','valuations',bindings=b,expected_date='2025-11-06')
    assert rows[0]['date']=='2025-11-06'
    rows,_=map_rows([({'自由流通股本(截止日期:20251231)':100},{})],['free_share'],'600519.SH','valuations',bindings=b,expected_date='2025-11-06')
    assert rows==[]
