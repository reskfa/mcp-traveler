import json
from contextlib import asynccontextmanager
import pytest
from fastapi.testclient import TestClient
from gateway import config,dictionary,exploration,core
from gateway.app import app

@pytest.fixture
def api_home(tmp_path,monkeypatch):
    @asynccontextmanager
    async def http_lifespan(app):yield
    monkeypatch.setattr(app.router,'lifespan_context',http_lifespan)
    original=config.ROOT
    (tmp_path/'config.json').write_text((original/'config.example.json').read_text())
    (tmp_path/'.env').write_text('IFIND_API_KEY=test-api-key\n')
    monkeypatch.setattr(config,'ROOT',tmp_path)
    dictionary.reload_dictionary()
    yield tmp_path
    monkeypatch.undo()
    dictionary.reload_dictionary()


def test_search_explore_save_query_delete_cycle(api_home,monkeypatch):
    raw={'data':{'answer':'|证券代码|日期|净息差（单位：%）|\n|---|---|---|\n|000001.SZ|20241231|1.87|','indicators_params':{'净息差':{'报告期':'最新一期(MRQ)'}}}}
    async def discover(*args):return raw
    monkeypatch.setattr(exploration,'invoke',discover)
    monkeypatch.setattr(core,'available',lambda *args:True)
    with TestClient(app) as client:
        assert client.get('/api/metrics/search',params={'q':'净息差','dataset':'financials'}).json()['total']==0
        result=client.post('/api/explore',json={'name':'净息差','dataset':'financials','symbol':'000001.SZ','period':'2024-12-31','provider':'ifind','auto_save':False}).json()
        assert result['status']=='success',result
        assert not (api_home/'local/user_dictionary.json').exists()
        saved=client.post(f"/api/explorations/{result['id']}/save",json={'field':'net_interest_margin'})
        assert saved.status_code==200,saved.text
        assert client.get('/api/metrics/search',params={'q':'净息差'}).json()['results'][0]['origin']=='user'
        assert 'net_interest_margin' in core.Query(dataset='financials',symbol='000001.SZ',period='2023-12-31',fields=['net_interest_margin']).fields
        async def invoke(name,q,timeout):
            from gateway.acquisition import acquire
            class Remote:
                def call(self,*args):return {'data':{'answer':'|证券代码|日期|净息差（单位：%）|\n|---|---|---|\n|000001.SZ|20231231|2.38|'}}
            return acquire(name,q,remote=Remote())
        monkeypatch.setattr(core,'invoke',invoke)
        result=client.post('/api/query',json={'dataset':'financials','symbol':'000001.SZ','period':'2023-12-31','fields':['net_interest_margin'],'provider':'ifind'})
        assert result.status_code==200,result.text
        assert result.json()['data'][0]['net_interest_margin']==2.38
        assert result.json()['field_sources'][0]['net_interest_margin']=='ifind'
        assert client.delete('/api/metrics/financials/net_interest_margin').json()['deleted']
        assert client.get('/api/metrics/search',params={'q':'净息差'}).json()['total']==0
        assert 'net_interest_margin' not in dictionary.DICTIONARY['datasets']['financials']['fields']


def test_personal_routes_use_local_origin_protection(api_home):
    with TestClient(app) as client:
        assert client.post('/api/explore',json={},headers={'Origin':'https://other.example'}).status_code==403
        assert client.post('/api/explorations/not-an-id/save',json={}).status_code==400
        assert client.get('/api/metrics/search',params={'limit':1000}).status_code==422


def test_auto_save_and_reuse_with_partial_coverage(api_home,monkeypatch):
    raw={'data':{'answer':'为您找到366条数据，以下为部分数据：\n|证券代码|日期|融资余额（单位：元）|\n|---|---|---|\n|300308.SZ|20261116|100|'}}
    calls=[]
    async def discover(*args):calls.append(args);return raw
    monkeypatch.setattr(exploration,'invoke',discover)
    monkeypatch.setattr(core,'available',lambda *args:True)
    with TestClient(app) as client:
        query={'name':'融资余额','dataset':'daily_prices','symbol':'300308.SZ','start_date':'2025-11-17','end_date':'2026-11-17','provider':'ifind'}
        r=client.post('/api/explore',json=query).json()
        assert r['status']=='success' and r['saved'] is True
        assert r['coverage']['status']=='partial'
        assert r['coverage']['source_reported_rows']==366
        field=r['saved_field']
        assert client.get('/api/metrics/search',params={'q':'融资余额'}).json()['results'][0]['field']==field
        assert client.post(f"/api/explorations/{r['id']}/save",json={}).json()['already_saved']
        dictionary.reload_dictionary()
        assert field in dictionary.fields('daily_prices')
        async def query_core(q):return {'data':[]}
        monkeypatch.setattr(core,'query',query_core)
        again=client.post('/api/explore',json=query).json()
        assert again['status']=='existing' and len(calls)==1
        assert client.delete('/api/metrics/daily_prices/'+field).json()['deleted']


def test_ambiguous_exploration_does_not_save(api_home,monkeypatch):
    async def discover(*args):return {'data':{'answer':'|证券代码|日期|融资余额|\n|---|---|---|\n|300308.SZ|20261116|100|'}}
    monkeypatch.setattr(exploration,'invoke',discover)
    monkeypatch.setattr(core,'available',lambda *args:True)
    with TestClient(app) as client:
        r=client.post('/api/explore',json={'name':'融资余额','dataset':'daily_prices','symbol':'300308.SZ','start_date':'2026-11-16','end_date':'2026-11-16','provider':'ifind'}).json()
        assert r['status']=='needs_confirmation' and not r['saved']
        assert not (api_home/'local/user_dictionary.json').exists()


def test_save_failure_keeps_successful_data(api_home,monkeypatch):
    async def discover(*args):return {'data':{'answer':'|证券代码|日期|融资余额（元）|\n|---|---|---|\n|300308.SZ|20261116|100|'}}
    monkeypatch.setattr(exploration,'invoke',discover)
    monkeypatch.setattr(core,'available',lambda *args:True)
    def fail(*args):raise OSError('disk unavailable')
    monkeypatch.setattr(dictionary,'save_user_metric',fail)
    with TestClient(app) as client:
        r=client.post('/api/explore',json={'name':'融资余额','dataset':'daily_prices','symbol':'300308.SZ','start_date':'2026-11-16','end_date':'2026-11-16','provider':'ifind'}).json()
        assert r['status']=='success' and not r['saved'] and r['data']
        assert r['save_status']=='failed' and 'disk unavailable' in r['save_error']
