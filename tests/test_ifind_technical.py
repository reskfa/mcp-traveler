"""Replay actual iFinD output; no network, paid calls, or local formulas."""
import copy
import json
from pathlib import Path
import pytest
from gateway.ifind_technical import fetch,payloads
from gateway.dictionary import DICTIONARY,validate

ROOT=Path(__file__).resolve().parent/'fixtures/ifind_technical'
TECH_FIELDS={k:v for k,v in DICTIONARY['datasets']['technical_indicators']['fields'].items() if k.startswith('ifind_') and v['sources']['ifind'] and 'technical' in DICTIONARY['recipes'][v['sources']['ifind'][0]['recipe']]}
PATCH={'fields':TECH_FIELDS,'recipes':{v['profile_id']:DICTIONARY['recipes'][v['profile_id']] for v in TECH_FIELDS.values()}}
COVERAGE=json.loads((ROOT/'coverage.json').read_text())['daily_verified']

class Remote:
    def __init__(self,raw):self.raw=raw;self.calls=[]
    def call(self,*args):self.calls.append(args);return self.raw

def setup(field='ifind_ma',source='batch_0.raw.json'):
    definition=PATCH['fields'][field];binding=definition['sources']['ifind'][0];recipe=PATCH['recipes'][binding['recipe']]
    q={'symbol':'600519.SH','start_date':'2026-11-10','end_date':'2026-11-11','adjust':'native','frequency':'daily'}
    return q,{field:binding},copy.deepcopy(recipe),json.loads((ROOT/source).read_text())

def change(raw,mutation):
    # iFinD's real wire response embeds the answer JSON in a text block.
    raw=copy.deepcopy(raw);obj=json.loads(raw['content'][0]['text']);mutation(obj['data']);raw['content'][0]['text']=json.dumps(obj,ensure_ascii=False);return raw

def test_patch_schema_and_units():
    merged=copy.deepcopy(DICTIONARY);merged['recipes'].update(PATCH['recipes']);merged['datasets']['technical_indicators']['fields'].update(PATCH['fields']);validate(merged)
    assert len(PATCH['fields'])==28
    assert PATCH['fields']['ifind_ma']['unit']=='元'
    assert PATCH['fields']['ifind_ma']['unit_status']=='definition_inferred'
    assert PATCH['fields']['ifind_obv_obv']['unit_status']=='vendor_undisclosed'

@pytest.mark.parametrize('field',list(PATCH['fields']))
def test_actual_batch_replay_all_fields(field):
    evidence=next(c for c in COVERAGE if c['field']==field)
    q,bindings,recipe,raw=setup(field,evidence['source'])
    remote=Remote(raw);rows,_=fetch(remote,q,bindings,recipe)
    assert [r['date'] for r in rows]==['2026-11-10','2026-11-11']
    assert all(r[field] is not None for r in rows)
    assert remote.calls[0][0]=='get_security_indicators'
    assert '不复权' in remote.calls[0][1]['query']

def test_reject_wrong_parameters_in_actual_response():
    q,b,r,raw=setup();q['parameters']={'n':10}
    with pytest.raises(RuntimeError,match='参数与请求'):fetch(Remote(raw),q,b,r)
    # Explicit real MA10 request returned MA5, so this evidence cannot certify MA10.
    _,_,_,raw=setup(source='batch_9.raw.json')
    with pytest.raises(RuntimeError,match='参数与请求'):fetch(Remote(raw),q,b,r)

def test_override_transport_and_verified_response():
    q,b,r,raw=setup();q['parameters']={'n':10}
    raw=change(raw,lambda data:data['indicators_params']['MA简单移动平均'].update(周期='10'))
    remote=Remote(raw);fetch(remote,q,b,r)
    assert '周期=10' in remote.calls[0][1]['query']

@pytest.mark.parametrize('kind',['symbol','date','adjust','frequency','scaled','approximate_name'])
def test_reject_invalid_native_evidence(kind):
    q,b,r,raw=setup()
    def mutate(data):
        if kind=='symbol':data['answer']=data['answer'].replace('600519.SH','000001.SZ')
        if kind=='date':data['answer']=data['answer'].replace('20261111','20261112')
        if kind=='adjust':data['indicators_params']['MA简单移动平均']['复权方式']='前复权'
        if kind=='frequency':data['indicators_params']['MA简单移动平均']['计算周期']='周'
        if kind=='scaled':data['answer']=data['answer'].replace('1380.0755','1380.0755万')
        if kind=='approximate_name':data['indicators_params']['MA简单移动平均信号线']=data['indicators_params'].pop('MA简单移动平均')
    with pytest.raises(RuntimeError):fetch(Remote(change(raw,mutate)),q,b,r)

def test_kdj_mismatched_option_not_accepted():
    q,b,r,raw=setup('ifind_kdj_d','batch_6.raw.json')
    with pytest.raises(RuntimeError,match='未返回可核对参数'):fetch(Remote(raw),q,b,r)

def test_ambiguous_duplicate_component_not_assigned_by_table_order():
    q,b,r,raw=setup('ifind_kdj_k','daily_batch.raw.json')
    with pytest.raises(RuntimeError,match='不同值'):fetch(Remote(raw),q,b,r)
