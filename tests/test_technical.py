import asyncio
import copy
from unittest.mock import AsyncMock,patch
import pytest
from gateway import core,dictionary,exploration,config
from gateway.technical import fetch,candidate


def raw(n=9):
    return {'structuredContent':{'error_code':0,'data':[{'stock_code':'600519','period':'4','date_list':[20261112,20261113], 'indicator':[{'name':'KDJ','params_used':{'N':str(n),'M1':'3','M2':'3'},'line':['K','D','J'],'data':[[10,20,-10],[30,25,40]]}]}]}}

class Remote:
    def __init__(self,response):self.response=response;self.calls=[]
    def call(self,*args):self.calls.append(args);return self.response

def query(**kw):return core.Query(dataset='technical_indicators',symbol='600519.SH',start_date='2026-11-12',end_date='2026-11-13',**kw)

def test_parameter_transport_and_evidence():
    q=query(parameters={'n':14}).model_dump();r=Remote(raw(14))
    rows,_=fetch(r,q,{'kdj_j':{'component':'J'}})
    assert r.calls[0][1]['indicators']==[{'id':'KDJ','set':'0','parameters':{'N':14,'M1':3,'M2':3}}]
    assert rows[-1]['kdj_j']==40
    with pytest.raises(RuntimeError,match='参数'):fetch(Remote(raw()),q,{'kdj_j':{'component':'J'}})
    for adjust in ['none','qfq','hfq']:
        with pytest.raises(ValueError):query(adjust=adjust)
    with pytest.raises(ValueError):query(parameters={'unknown':3})

def test_explore_save_and_reuse_parameters(tmp_path):
    snapshot=copy.deepcopy(dictionary.DICTIONARY)
    settings={'routes':{'technical_indicators':['tdx']},'providers':{'tdx':{'timeout_seconds':60}}}
    try:
        with patch.object(config,'ROOT',tmp_path),patch.object(config,'load',return_value=settings),patch.object(core,'available',return_value=True),patch.object(core,'record'),patch.object(exploration,'invoke',new=AsyncMock(return_value=raw(14))):
            r=exploration.ExplorationRequest(dataset='technical_indicators',symbol='600519.SH',date='2026-11-13',name='KDJ J值',parameters={'n':14})
            result=asyncio.run(exploration.explore(r));assert result['status']=='success'
            saved=asyncio.run(exploration.save_exploration(result['id']));assert saved['field']=='kdj_j_14_3_3'
            assert dictionary.fields('technical_indicators')[saved['field']]['parameters']['n']==14
            with patch.object(exploration,'invoke',new=AsyncMock(return_value=raw())) as vendor:
                other=asyncio.run(exploration.explore(exploration.ExplorationRequest(dataset='technical_indicators',symbol='600519.SH',date='2026-11-13',name='KDJ J值',parameters={'n':9})))
                assert other['status']=='success' and other['candidate']['field']=='kdj_j_9_3_3'
                vendor.assert_awaited_once()
            async def invoke(name,q,timeout):
                response=raw(q['parameters']['n'])
                response['structuredContent']['data'][0]['indicator'][0]['params_used']['M1']=str(q['parameters']['m1'])
                rows,response=fetch(Remote(response),q,{'kdj_j_14_3_3':{'component':'J'}})
                return {'data':rows,'raw':response}
            with patch.object(core,'invoke',new=AsyncMock(side_effect=invoke)) as inv:
                result=asyncio.run(core.query(query(fields=[saved['field']])))
                assert result['query']['parameters']=={'n':14,'m1':3,'m2':3}
                assert result['complete']
                changed=asyncio.run(core.query(query(fields=[saved['field']],parameters={'n':7})))
                assert changed['query']['parameters']['n']==7
                partial=asyncio.run(core.query(query(fields=[saved['field']],parameters={'m1':5})))
                assert partial['query']['parameters']=={'n':14,'m1':5,'m2':3}
                assert inv.await_count==3
    finally:
        dictionary.DICTIONARY.clear();dictionary.DICTIONARY.update(snapshot)
        from gateway.catalog import refresh_catalog
        refresh_catalog()


def test_empty_values_and_boolean_parameters():
    r=exploration.ExplorationRequest(dataset='technical_indicators',symbol='600519.SH',date='2026-11-13',name='KDJ J值')
    data=raw();data['structuredContent']['data'][0]['indicator'][0]['data']=[[1,2,None],[1,2,None]]
    assert candidate(r,data)[0] is None
    data['structuredContent']['data'][0]['indicator'][0]['data'][1][2]=0
    assert candidate(r,data)[0]['data'][0]['kdj_j_9_3_3']==0
    with pytest.raises(ValueError):query(parameters={'n':True})


def test_unsupported_exploration_does_not_call_vendor():
    r=exploration.ExplorationRequest(dataset='technical_indicators',symbol='600519.SH',date='2026-11-13',name='RSI')
    with patch.object(exploration,'invoke',new=AsyncMock()) as invoke:
        result=asyncio.run(exploration.explore(r))
        assert result['network_calls']==0
        invoke.assert_not_called()
