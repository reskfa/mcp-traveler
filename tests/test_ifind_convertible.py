import json
from pathlib import Path
from copy import deepcopy
import pytest
from gateway.ifind_convertible import fetch

F=Path(__file__).parent/'fixtures'/'ifind_convertible'
class Remote:
    def __init__(self, name):self.raw=json.loads((F/f'ifind_{name}.raw.json').read_text())
    def call(self,method,args):
        assert args['market']=='债券'
        return self.raw

def query(ds):return {'symbol':'113052.SH','dataset':ds,'start_date':'2026-11-12','end_date':'2026-11-13'}
def recipe(series=False):return {'temporal':'series' if series else 'snapshot','method':'get_security_indicators'}
def bind(column,units=None):
    b={'columns':[column],'prompt':column}
    if units is not None:b['accepted_units']=units
    return b

def test_live_basic_and_scale():
    data,_=fetch(recipe(),{'stock_code':bind('正股代码'),'issue_size':bind('发行总额',['元','亿元'])},query('cb_basic'),Remote('basic'))
    assert data==[{'symbol':'113052.SH','date':None,'stock_code':'601166.SH','issue_size':50_000_000_000.0}]

def test_live_daily_units_and_actual_dates():
    data,_=fetch(recipe(True),{'close':bind('收盘价',['元']),'conversion_premium':bind('转股溢价率',['%'])},query('cb_daily'),Remote('daily'))
    assert [r['date'] for r in data]==['2026-11-12','2026-11-13']
    assert data[0]['close']==112.715
    assert data[1]['conversion_premium']==14.4862

def test_live_balance_and_no_tax_guess():
    data,_=fetch(recipe(True),{'outstanding':bind('未转股余额',['元','亿元']),'remaining_years':bind('剩余期限(年)',['']),'ytm':bind('税前到期收益率',['%']),'ytm_after_tax':bind('税后到期收益率',['%'])},query('cb_daily'),Remote('daily2'))
    assert data[0]['outstanding']==41_351_780_000
    assert data[0]['remaining_years']==1.1233
    assert all('ytm' not in r and 'ytm_after_tax' not in r for r in data)

def test_live_contract_not_event():
    data,_=fetch(recipe(),{'reset_clause':bind('特别向下修正条款全文'),'call_clause':bind('赎回条款'),'put_clause':bind('条件回售条款全文')},query('cb_basic'),Remote('clauses'))
    assert '80%' in data[0]['reset_clause'] and '130%' in data[0]['call_clause']
    assert data[0]['put_clause'] is None and data[0]['date'] is None

def test_reject_identity_and_undated_history():
    r=Remote('daily');r.raw=json.loads(json.dumps(r.raw).replace('113052.SH','110000.SH'))
    with pytest.raises(RuntimeError,match='身份'):fetch(recipe(True),{'close':bind('收盘价',['元'])},query('cb_daily'),r)
    r=Remote('basic')
    with pytest.raises(RuntimeError,match='交易日'):fetch(recipe(True),{'outstanding':bind('债券余额',['亿元'])},query('cb_daily'),r)

def test_single_indicator_date_is_source_evidence():
    q=query('cb_daily');q.update(start_date='2025-11-13',end_date='2025-11-13')
    data,_=fetch(recipe(True),{'outstanding':bind('未转股余额',['元','亿元'])},q,Remote('historical_basic'))
    assert data==[{'symbol':'113052.SH','date':'2025-11-13','outstanding':41_352_020_000}]

def test_generic_ytm_retains_original_basis_without_tax_assignment(monkeypatch):
    from gateway import ifind_convertible
    original=ifind_convertible.fields
    monkeypatch.setattr(ifind_convertible,'fields',lambda ds:{**original(ds),'ytm_provider':{'data_type':'number'}})
    b=bind('到期收益率',['%']);b['indicator_params']={'计算方法':'央行规则'}
    data,_=fetch(recipe(True),{'ytm_provider':b},query('cb_daily'),Remote('daily2'))
    assert data[0]['ytm_provider']==-1.1391

def test_own_indicator_date_cannot_be_replaced_by_request_date():
    # Response carries 20251113; requesting 2026 must not re-date that value.
    data,_=fetch(recipe(True),{'outstanding':bind('未转股余额',['元','亿元'])},query('cb_daily'),Remote('historical_basic'))
    assert data==[]

def test_request_metadata_does_not_supply_indicator_date():
    r=Remote('basic')
    r.raw['request']={'indicators_params':{'债券余额':{'交易日期':'20261112'}}}
    with pytest.raises(RuntimeError,match='交易日'):
        fetch(recipe(True),{'outstanding':bind('债券余额',['亿元'])},query('cb_daily'),r)

@pytest.mark.parametrize('parameter',['交易日期','日期','截止日期'])
def test_contradicting_indicator_date_never_maps_to_row(parameter):
    r=Remote('daily')
    for content in r.raw['content']:
        obj=json.loads(content['text'])
        obj['data']['indicators_params']['收盘价'][parameter]='20251113'
        content['text']=json.dumps(obj,ensure_ascii=False)
    with pytest.raises(RuntimeError,match='close.*日期冲突'):
        fetch(recipe(True),{'close':bind('收盘价',['元'])},query('cb_daily'),r)

def test_percent_suffix_preserves_percentage_points():
    r=Remote('daily')
    r.raw=json.loads(json.dumps(r.raw).replace('14.4862','14.4862%'))
    data,_=fetch(recipe(True),{'conversion_premium':bind('转股溢价率',['%'])},query('cb_daily'),r)
    assert data[-1]['conversion_premium']==14.4862

def test_live_pure_bond_ytm_alias_unknown_method_and_tax(monkeypatch):
    from gateway import ifind_convertible
    original=ifind_convertible.fields
    monkeypatch.setattr(ifind_convertible,'fields',lambda ds:{**original(ds),'ytm_provider':{'data_type':'number'}})
    b={'columns':['到期收益率','纯债到期收益率'],'prompt':'纯债到期收益率','accepted_units':['%']}
    q=query('cb_daily');q.update(start_date='2025-11-05',end_date='2025-11-05')
    data,_=fetch(recipe(True),{'ytm_provider':b,'ytm':bind('税前到期收益率',['%']),'ytm_after_tax':bind('税后到期收益率',['%'])},q,Remote('ytm_alias'))
    assert data==[{'symbol':'113052.SH','date':'2025-11-05','ytm_provider':-4.4685}]

def test_native_ohlc_amount_and_percentage_live_fixture():
    bindings={f:bind(c,u) for f,c,u in [('open','债券开盘价',['元']),('high','最高价',['元']),('low','最低价',['元']),('amount','成交金额',['元']),('pct_change','涨跌幅',['%'])]}
    q=query('cb_daily');q.update(start_date='2025-11-05',end_date='2025-11-06')
    data,_=fetch(recipe(True),bindings,q,Remote('ohlcv'))
    assert data[0]=={'symbol':'113052.SH','date':'2025-11-05','open':124.049,'high':124.6,'low':123.699,'amount':453480000.,'pct_change':0.1371}
    assert data[1]['amount']==362530000.


def volume_binding():
    b=bind('债券成交量',['手']);b.update(unit_parameter='单位设置',indicator_params={'单位设置':'手'})
    return b


def test_volume_uses_own_native_hand_unit_and_not_face_amount():
    q=query('cb_daily');q.update(start_date='2025-11-05',end_date='2025-11-05')
    data,_=fetch(recipe(True),{'volume':volume_binding(),'pre_close':bind('债券前收盘价',['元']),'bond_floor_premium':bind('纯债溢价率',['%'])},q,Remote('volume'))
    assert data==[{'symbol':'113052.SH','date':'2025-11-05','volume':365351.,'pre_close':123.979,'bond_floor_premium':14.1897}]
    b=volume_binding();b['columns']=['成交量']
    data,_=fetch(recipe(True),{'volume':b},q,Remote('ohlcv'))
    assert data==[]  # Explicit native 元 volume is not silently relabelled 手.


def test_volume_rejects_conflicting_native_unit_evidence():
    r=Remote('volume')
    r.raw=json.loads(json.dumps(r.raw,ensure_ascii=False).replace('|债券成交量|','|债券成交量（单位：元）|'))
    q=query('cb_daily');q.update(start_date='2025-11-05',end_date='2025-11-05')
    with pytest.raises(RuntimeError,match='单位冲突'):
        fetch(recipe(True),{'volume':volume_binding()},q,r)


def test_native_daily_conversion_price_and_turnover_are_dated(monkeypatch):
    from gateway import ifind_convertible
    original=ifind_convertible.fields
    monkeypatch.setattr(ifind_convertible,'fields',lambda ds:{**original(ds),'conversion_price':{'data_type':'number'},'turnover_rate':{'data_type':'number'}})
    q=query('cb_daily');q.update(start_date='2025-11-05',end_date='2025-11-06')
    data,_=fetch(recipe(True),{'conversion_price':bind('转股价格',['元']),'turnover_rate':bind('换手率%',[''])},q,Remote('ohlcv'))
    assert data==[{'symbol':'113052.SH','date':'2025-11-05','conversion_price':21.19,'turnover_rate':0.8835},{'symbol':'113052.SH','date':'2025-11-06','conversion_price':21.19,'turnover_rate':0.7078}]


def test_dictionary_batch_limit_and_singleton_contract_fields():
    class Recording(Remote):
        def __init__(self):super().__init__('ohlcv');self.calls=[]
        def call(self,method,args):self.calls.append(args['query']);return self.raw
    r=Recording();q=query('cb_daily');q.update(start_date='2025-11-05',end_date='2025-11-06')
    bindings={f:bind(c,['元']) for f,c in [('open','债券开盘价'),('high','最高价'),('low','最低价'),('close','收盘价')]}
    rows,raw=fetch({**recipe(True),'max_fields_per_call':2,'singleton_fields':['low']},bindings,q,r)
    assert len(r.calls)==3 and len(raw)==3
    assert all(set(row)=={'symbol','date','open','high','low','close'} for row in rows)
    assert '最低价' in r.calls[1] and '收盘价' not in r.calls[1]


def test_explicit_upstream_complexity_downgrade_is_error():
    r=Remote('daily')
    r.raw={'content':[{'type':'text','text':json.dumps({'data':{'answer':'# 复杂问句降级提示\n本次执行数据指标: 无','indicators_params':{}}})}]}
    with pytest.raises(RuntimeError,match='复杂问句降级'):
        fetch(recipe(True),{'close':bind('收盘价',['元'])},query('cb_daily'),r)


def test_batches_share_original_timeout_and_restore_it(monkeypatch):
    from gateway import ifind_convertible
    class Timed(Remote):
        timeout=10
        def __init__(self):super().__init__('ohlcv');self.calls=0
        def call(self,method,args):self.calls+=1;return self.raw
    ticks=iter([100,101,111])
    monkeypatch.setattr(ifind_convertible.time,'monotonic',lambda:next(ticks))
    r=Timed();q=query('cb_daily');q.update(start_date='2025-11-05',end_date='2025-11-06')
    rows,raw=fetch({**recipe(True),'max_fields_per_call':1},{'open':bind('债券开盘价',['元']),'high':bind('最高价',['元'])},q,r)
    assert r.calls==1 and r.timeout==10
    assert rows[0]['open']==124.049 and 'high' not in rows[0]
    assert raw[-1]['batch_fields']==['high'] and '总超时' in raw[-1]['error']


def test_failed_batch_keeps_successes_and_attempts_later_batch():
    class Failing(Remote):
        def __init__(self):super().__init__('ohlcv');self.calls=0
        def call(self,method,args):
            self.calls+=1
            if self.calls==2:raise RuntimeError('供应商临时失败')
            return self.raw
    r=Failing();q=query('cb_daily');q.update(start_date='2025-11-05',end_date='2025-11-06')
    bindings={f:bind(c,['元']) for f,c in [('open','债券开盘价'),('high','最高价'),('low','最低价')]}
    rows,raw=fetch({**recipe(True),'max_fields_per_call':1},bindings,q,r)
    assert r.calls==3
    assert rows[0]['open']==124.049 and rows[0]['low']==123.699 and 'high' not in rows[0]
    assert raw[1]=={'batch_fields':['high'],'error':'供应商临时失败'}
