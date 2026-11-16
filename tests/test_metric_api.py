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
        result=client.post('/api/explore',json={'name':'净息差','dataset':'financials','symbol':'000001.SZ','period':'2024-12-31','provider':'ifind'}).json()
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
