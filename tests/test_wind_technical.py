import copy
import json
from pathlib import Path
import pytest
from gateway.wind_technical import fetch

ROOT = Path(__file__).resolve().parents[1]
RAW = json.loads((ROOT / 'tests/fixtures/wind_technical_snapshot.json').read_text())
BINDINGS = {'wind_ma_20_native': {'columns':['20周期移动平均'], 'source_unit':'元'}, 'wind_kdj_k_native': {'columns':['随机指标K值'], 'source_unit':'无量纲', 'unit_inferred':True}}

class Replay:
    def __init__(self, raw=RAW): self.raw = raw
    def call(self, tool, args):
        assert tool == 'get_stock_price_indicators'
        assert args['windcode'] == '600519.SH'
        return self.raw


def test_real_snapshot_all_components():
    rows, _ = fetch(Replay(), {'symbol':'600519.SH', 'adjust':'native'}, BINDINGS)
    assert len(rows) == 1
    assert rows[0]['date'] == '2026-11-17'
    assert rows[0]['wind_ma_20_native'] == 1403.126
    assert rows[0]['wind_kdj_k_native'] == 69.648
    assert len(rows[0]) == 4


def test_never_stamp_historical_request_date():
    with pytest.raises(RuntimeError, match='不能用于历史补值'):
        fetch(Replay(), {'symbol':'600519.SH','start_date':'2026-11-12','end_date':'2026-11-13'}, BINDINGS)


def changed(column, value):
    raw = copy.deepcopy(RAW)
    obj = json.loads(raw['content'][0]['text'])
    data = obj['data']; names = [c['name'] for c in data['columns']]
    data['rows'][0][names.index(column)] = value
    raw['content'][0]['text'] = json.dumps(obj)
    return raw


@pytest.mark.parametrize('column,value,match', [('Wind代码','000001.SZ','证券'), ('最新交易日','20261116','时间')])
def test_checks_source_identity_and_time(column, value, match):
    with pytest.raises(RuntimeError, match=match):
        fetch(Replay(changed(column, value)), {'symbol':'600519.SH'}, BINDINGS)


def test_rejects_unit_drift_and_parameter_override():
    raw = copy.deepcopy(RAW); obj = json.loads(raw['content'][0]['text'])
    del obj['data']['unit']['20周期移动平均']
    raw['content'][0]['text'] = json.dumps(obj)
    with pytest.raises(RuntimeError, match='单位'):
        fetch(Replay(raw), {'symbol':'600519.SH'}, BINDINGS)
    with pytest.raises(ValueError, match='parameters'):
        fetch(Replay(), {'symbol':'600519.SH', 'parameters':{'n':10}}, BINDINGS)
