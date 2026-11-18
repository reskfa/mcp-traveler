import asyncio
import copy
import json
from pathlib import Path
import pandas as pd
import pytest
from gateway import core,config
from gateway.acquisition import acquire
from gateway.basic_info import map_basic
from gateway.dictionary import DICTIONARY,plan,validate
from gateway.exploration import ExplorationRequest


def q(fields=None,provider='auto'):
    return core.Query(dataset='basic_info',symbol='300308.SZ',fields=fields,provider=provider).model_dump()

def mapped(provider,fields,rows,units=None):
    result={}
    for _,recipe,bindings in plan(provider,'basic_info',fields):
        result.update(map_basic([(r,units or {}) for r in rows],bindings,{**q(),'fields':fields},recipe)[0])
    return result

def test_identity_and_typed_values():
    rows=[{'A股代码':'600519','公司名称':'错误公司'}, {'A股代码':'300308','公司名称':'中际旭创股份有限公司','上市日期':'20120410','曾用简称':'中际装备','入选指数':'沪深300,创业板指'}]
    r=mapped('akshare',['company_name','listing_date','former_names','cninfo_indexes'],rows)
    assert r['company_name']=='中际旭创股份有限公司'
    assert r['listing_date']=='2012-04-10'
    assert r['former_names']==[{'name':'中际装备'}]
    assert r['cninfo_indexes']==[{'system':'巨潮','name':'沪深300'},{'system':'巨潮','name':'创业板指'}]
    assert mapped('wind',['company_name'],[{'公司中文名称':'未知证券'}])['company_name'] is None

def test_ownership_not_enterprise_size_and_controller_type():
    r=mapped('ifind',['ownership_type','ifind_enterprise_type','controller_type'],[{'证券代码':'300308.SZ','企业类型':'大型企业','实际控制人类型':'个人'}])
    assert r['ifind_enterprise_type']=='大型企业' and r['controller_type']=='个人'
    assert 'ownership_type' not in r

def test_industry_systems_and_levels():
    r=mapped('wind',['sw_industry_l1','sw_industry_l2','wind_industry_l1'],[{'Wind代码':'300308.SZ','所属申万行业明细':'通信--通信设备--器件','所属WIND行业明细':'信息技术--硬件'}])
    assert r['sw_industry_l1']=='通信' and r['sw_industry_l2']=='通信设备' and r['wind_industry_l1']=='信息技术'
    row={'证券代码':'300308.SZ','所属申万行业':'通信设备'}
    assert mapped('ifind',['sw_industry_l1'],[row],{'__indicator_params__':{'所属申万行业':{'行业级别':'二级行业'}}})['sw_industry_l1'] is None
    assert mapped('ifind',['sw_industry_l1'],[{**row,'所属申万行业':'通信'}],{'__indicator_params__':{'所属申万行业':{'行业级别':'一级行业'}}})['sw_industry_l1']=='通信'

def test_history_dates_and_empty_values():
    r=mapped('tushare',['name_history'],[{'ts_code':'300308.SZ','name':'中际装备','start_date':'20120410','end_date':'20171008','ann_date':None,'change_reason':'其他'}])
    assert r['name_history'][0]=={'name':'中际装备','start_date':'2012-04-10','end_date':'2017-10-08','announcement_date':None,'reason':'其他'}
    assert mapped('akshare',['former_names'],[{'A股代码':'300308','曾用简称':'--'}])['former_names'] is None
    r=mapped('tushare',['employees','registered_capital'],[{'ts_code':'300308.SZ','employees':0,'reg_capital':1.25}])
    assert r['employees']==0 and r['registered_capital']==12500

def test_tdx_json_text_and_unknown_units():
    class Remote:
        def call(self,method,args):
            assert method=='tdx_api_data' and args['code']=='300308'
            return {'content':[{'type':'text','text':json.dumps({'Rows':[{'公司名称':'中际旭创','关联主题':'CPO概念','主题id':2917,'注册资金':117790.9641}]},ensure_ascii=False)}]}
    r=acquire('tdx',q(['company_name','tdx_themes','registered_capital']),remote=Remote())
    assert r['data'][0]['company_name']=='中际旭创'
    assert r['data'][0]['tdx_themes']==[{'system':'通达信','name':'CPO概念','code':'2917'}]
    assert r['data'][0]['registered_capital'] is None

def test_sdk_join_no_extra_members_and_partial_recipe_failure():
    class SDK:
        def stock_basic(self,**kwargs):return pd.DataFrame([{'ts_code':'300308.SZ','name':'中际旭创'}])
        def stock_company(self,**kwargs):raise RuntimeError('offline')
        def ths_member(self,**kwargs):return pd.DataFrame([{'con_code':'300308.SZ','ts_code':'885001.TI'}])
        def ths_index(self,**kwargs):return pd.DataFrame([{'ts_code':'885001.TI','name':'主题A','type':'N'},{'ts_code':'885002.TI','name':'不属于该股','type':'N'}])
    r=acquire('tushare',q(['security_name','province','ths_boards']),sdk=SDK())
    assert r['data'][0]['security_name']=='中际旭创' and r['data'][0]['province'] is None
    assert r['data'][0]['ths_boards']==[{'system':'同花顺','name':'主题A','code':'885001.TI','type':'N'}]
    assert any('offline' in n for n in r['notes'])

def test_new_text_field_only_requires_dictionary_config(monkeypatch):
    m={'label':'测试文本','unit':'文本','description':'测试','aliases':[],'data_type':'string','group':'profile','sources':{p:[] for p in DICTIONARY['providers']}}
    m['sources']['akshare']=[{'recipe':'ak_profile','columns':['任意新增列']}]
    monkeypatch.setitem(DICTIONARY['datasets']['basic_info']['fields'],'new_text',m)
    assert mapped('akshare',['new_text'],[{'A股代码':'300308','任意新增列':'ABC-001'}])['new_text']=='ABC-001'

def test_validation_rejects_historical_snapshot_and_numeric_exploration():
    with pytest.raises(ValueError,match='历史时点'):core.Query(dataset='basic_info',symbol='300308.SZ',date='2024-12-31')
    with pytest.raises(ValueError):ExplorationRequest(dataset='basic_info',symbol='300308.SZ',name='公司名')
    d=copy.deepcopy(DICTIONARY);d['datasets']['basic_info']['fields']['company_name']['data_type']='bad'
    with pytest.raises(ValueError,match='type/group'):validate(d)

def test_route_fills_undated_fields_and_retains_lists(monkeypatch,tmp_path):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setattr(config,'load',lambda:{'routes':{'basic_info':['ifind','wind','tushare']},'providers':{p:{} for p in ['ifind','wind','tushare']}})
    monkeypatch.setattr(core,'available',lambda *args:True)
    monkeypatch.setattr(core,'record',lambda *args:None)
    calls=[]
    async def invoke(provider,request,timeout):
        calls.append((provider,request['fields']))
        row={'symbol':'300308.SZ','date':None,'company_name':'先到值' if provider=='ifind' else '不覆盖','ownership_type':'民营企业' if provider=='wind' else None,'former_names':[{'name':'旧名'}]}
        return {'data':[row],'raw':[]}
    monkeypatch.setattr(core,'invoke',invoke)
    r=asyncio.run(core.query(core.Query(**q(['company_name','ownership_type','former_names']))))
    assert r['data'][0]['company_name']=='先到值' and r['data'][0]['ownership_type']=='民营企业'
    assert calls==[('ifind',['company_name','ownership_type','former_names']),('wind',['ownership_type'])]
    assert r['field_sources'][0]['ownership_type']=='wind' and r['complete']
    assert r['fields']['former_names']['data_type']=='records'

def test_mcp_and_catalog_expose_basic_info():
    from gateway.app import mcp
    from gateway.catalog import catalog
    tools=asyncio.run(mcp.list_tools())
    assert any(t.name=='query_basic_info' for t in tools)
    g=catalog()['datasets']['basic_info'];assert len(g['groups'])==4 and g['fields']['company_name']['data_type']=='string'
