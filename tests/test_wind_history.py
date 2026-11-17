"""Offline replay of actual Wind historical technical responses (no vendor calls)."""
import copy
import json
from pathlib import Path

import pytest
from gateway import wind_history

FIXTURES = Path(__file__).parent / 'fixtures/wind_history'
MANIFEST = json.loads((FIXTURES / 'recipes_manifest.json').read_text())
QUERY = {'symbol': '600519.SH', 'start_date': '2026-11-10', 'end_date': '2026-11-13'}


class Replay:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def call(self, method, arguments):
        self.calls.append((method, arguments))
        return self.raw


def raw(name):
    return json.loads((FIXTURES / f'{name}.raw.json').read_text())


def setup(monkeypatch, name):
    monkeypatch.setattr(wind_history, 'normalized_parameters', lambda values, pid: values or {})
    spec = next(item for item in MANIFEST if item['id'] == name)
    bindings = {f'component_{i}': {'recipe': f'wind_history_{name}',
        'columns': [component['column_suffix']], 'source_unit': component['unit'],
        'required_options': component['required_options']}
        for i, component in enumerate(spec['components'])
        if component['column_suffix'] not in {'成交量', '成交额', '交易币种'}}
    recipe = {'method': 'query', 'query_template': spec['query_template'],
              'technical': {'fixed': True, 'parameter_keys': {}}}
    return bindings, recipe


def ma_setup(monkeypatch):
    monkeypatch.setattr(wind_history, 'normalized_parameters', lambda values, pid: {'n': 10, **(values or {})})
    return {'ma': {'recipe': 'wind_history_ma', 'columns': ['{n}日均线'], 'source_unit': '上游未声明'}}, {
        'method': 'query', 'query_template': '查询{symbol}{start_cn}至{end_cn}每日的{n}日均线。',
        'technical': {'parameter_keys': {'n': '周期数'}}}


def mutate_ma(monkeypatch, column, value):
    bindings, recipe = ma_setup(monkeypatch)
    table = copy.deepcopy(next(wind_history.tables(raw('ma10'))))
    names = [c['name'] for c in table['columns']]
    index = names.index(column) if column in names else next(i for i, name in enumerate(names) if name.endswith(column))
    table['rows'][0][index] = value
    return Replay(table), bindings, recipe


@pytest.mark.parametrize('name', [item['id'] for item in MANIFEST if item['status'] == 'verified_history'])
def test_all_real_manifest_recipes(monkeypatch, name):
    bindings, recipe = setup(monkeypatch, name)
    response = raw(name)
    rows, source = wind_history.fetch(Replay(response), QUERY, bindings, recipe)
    assert source is response
    assert [row['date'] for row in rows] == ['2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13']
    assert all(set(row) == {'date', 'symbol', *bindings} for row in rows)


def test_ma_dynamic_parameter_and_question(monkeypatch):
    bindings, recipe = ma_setup(monkeypatch)
    remote = Replay(raw('ma10'))
    rows, _ = wind_history.fetch(remote, {**QUERY, 'parameters': {'n': 10}}, bindings, recipe)
    assert rows[0]['ma'] == 1423.133
    assert '10日均线' in remote.calls[0][1]['question']
    with pytest.raises(RuntimeError, match='未返回目标'):
        wind_history.fetch(remote, {**QUERY, 'parameters': {'n': 20}}, bindings, recipe)


def test_macd_multitable_kdj_single_table(monkeypatch):
    assert len(list(wind_history.tables(raw('macd')))) == 3
    assert len(list(wind_history.tables(raw('kdj')))) == 1
    bindings, recipe = setup(monkeypatch, 'macd')
    rows, _ = wind_history.fetch(Replay(raw('macd')), QUERY, bindings, recipe)
    assert [rows[0][field] for field in bindings] == [43.2659, 35.9223, 14.6872]


@pytest.mark.parametrize('column,value,message', [
    ('Wind代码', '000001.SZ', '证券'), ('时间', '2026-11-09', '日期'),
    ('时间', 'not-a-date', '日期'), ('.周期数', 20, '参数'), ('.周期', '周', '日线'),
    ('10日均线', float('nan'), '有限'), ('10日均线', float('inf'), '有限'),
    ('10日均线', True, '非数值'), ('10日均线', '1423元', '非数值')])
def test_rejects_identity_date_parameters_and_values(monkeypatch, column, value, message):
    remote, bindings, recipe = mutate_ma(monkeypatch, column, value)
    with pytest.raises(RuntimeError, match=message):
        wind_history.fetch(remote, QUERY, bindings, recipe)


def test_rejects_snapshot_without_actual_history_dates(monkeypatch):
    bindings, recipe = ma_setup(monkeypatch)
    table = next(wind_history.tables(raw('ma10')))
    table['columns'] = [c for c in table['columns'] if not c['name'].endswith('时间')]
    for row in table['rows']:
        del row[3]
    with pytest.raises(RuntimeError, match='实际日期'):
        wind_history.fetch(Replay(table), QUERY, bindings, recipe)


def test_conflicting_duplicate_rows_and_columns(monkeypatch):
    bindings, recipe = ma_setup(monkeypatch)
    table = next(wind_history.tables(raw('ma10')))
    table['rows'].append([*table['rows'][0]])
    table['rows'][-1][2] += 1
    with pytest.raises(RuntimeError, match='冲突'):
        wind_history.fetch(Replay(table), QUERY, bindings, recipe)
    table = next(wind_history.tables(raw('ma10')))
    table['columns'].append(table['columns'][2])
    with pytest.raises(RuntimeError, match='重复列'):
        wind_history.fetch(Replay(table), QUERY, bindings, recipe)


@pytest.mark.parametrize('change,message', [('missing', '未返回目标'), ('option', '参数')])
def test_rejects_missing_or_wrong_macd_component(monkeypatch, change, message):
    bindings, recipe = setup(monkeypatch, 'macd')
    tables = list(wind_history.tables(raw('macd')))
    if change == 'missing':
        tables.pop()
    else:
        tables[0]['rows'][0][-1] = 'DEA'
    with pytest.raises(RuntimeError, match=message):
        wind_history.fetch(Replay(tables), QUERY, bindings, recipe)


def test_fixed_recipe_rejects_override_before_remote(monkeypatch):
    bindings, recipe = setup(monkeypatch, 'kdj')
    remote = Replay(raw('kdj'))
    with pytest.raises(ValueError, match='默认参数'):
        wind_history.fetch(remote, {**QUERY, 'parameters': {'n': 9}}, bindings, recipe)
    assert not remote.calls


def test_partial_component_dates_and_empty_tables_preserved(monkeypatch):
    bindings, recipe = setup(monkeypatch, 'macd')
    tables = list(wind_history.tables(raw('macd')))
    tables[0]['rows'].pop()
    rows, _ = wind_history.fetch(Replay(tables), QUERY, bindings, recipe)
    assert 'component_0' not in rows[-1]
    assert rows[-1]['component_1'] == 36.2232
    for table in tables:
        table['rows'].clear()
    rows, _ = wind_history.fetch(Replay(tables), QUERY, bindings, recipe)
    assert rows == []


def test_rejects_explicit_unit_change(monkeypatch):
    bindings, recipe = ma_setup(monkeypatch)
    table = next(wind_history.tables(raw('ma10')))
    table['unit'] = {table['columns'][2]['name']: '万元'}
    with pytest.raises(RuntimeError, match='单位'):
        wind_history.fetch(Replay(table), QUERY, bindings, recipe)


@pytest.mark.parametrize('override', [{'frequency': 'weekly'}, {'adjust': 'qfq'}, {'start_date': None}])
def test_invalid_history_scope_never_calls_remote(monkeypatch, override):
    bindings, recipe = ma_setup(monkeypatch)
    remote = Replay(raw('ma10'))
    with pytest.raises(ValueError):
        wind_history.fetch(remote, {**QUERY, **override}, bindings, recipe)
    assert not remote.calls


def test_multiple_fixed_profiles_keep_their_own_queries(monkeypatch):
    questions = []
    for name in ('macd', 'kdj'):
        bindings, recipe = setup(monkeypatch, name)
        remote = Replay(raw(name))
        rows, _ = wind_history.fetch(remote, QUERY, bindings, recipe)
        assert len(rows) == 4
        questions.append(remote.calls[0][1]['question'])
    assert 'MACD' in questions[0] and 'KDJ' in questions[1]


def test_resolved_ma_parameters_control_query_and_validation(monkeypatch):
    bindings, recipe = ma_setup(monkeypatch)
    remote = Replay(raw('ma10'))
    rows, _ = wind_history.fetch(remote, {**QUERY, 'parameters': {'n': 20},
        'resolved_parameters': {'wind_history_ma': {'n': 10}}}, bindings, recipe)
    assert len(rows) == 4
    assert '10日均线' in remote.calls[0][1]['question']


COVERAGE = json.loads((FIXTURES / 'coverage.json').read_text())


@pytest.mark.parametrize('entry', COVERAGE, ids=lambda entry: entry['field'])
def test_main_dictionary_real_field_replay(entry):
    """Exercise real dictionary normalization and bindings for every admitted field."""
    from gateway.dictionary import DICTIONARY, fields
    field = entry['field']
    definition = fields('technical_indicators')[field]
    binding = definition['sources']['wind'][0]
    assert binding['recipe'] == entry['recipe']
    recipe = DICTIONARY['recipes'][entry['recipe']]
    response = json.loads((FIXTURES / entry['source_file']).read_text())
    rows, _ = wind_history.fetch(Replay(response), QUERY, {field: binding}, recipe)
    assert [row['date'] for row in rows] == entry['verified_dates']
    assert all(row[field] is not None for row in rows)


def test_coverage_exactly_matches_admitted_dictionary_fields():
    from gateway.dictionary import fields
    admitted = {name for name in fields('technical_indicators') if name.startswith('wind_history_')}
    assert admitted == {entry['field'] for entry in COVERAGE}
    assert len(admitted) == 32
    assert len({entry['recipe'] for entry in COVERAGE}) == 23
    assert all(Path(entry['source_file']).stem not in {'vr.raw', 'cr.raw', 'brar.raw'} for entry in COVERAGE)


@pytest.mark.parametrize('envelope', ['outer', 'embedded'])
def test_error_takes_priority_over_present_tables(monkeypatch, envelope):
    bindings, recipe = ma_setup(monkeypatch)
    response = raw('ma10')
    if envelope == 'outer':
        response['isError'] = True
    else:
        body = json.loads(response['content'][0]['text'])
        body['error'] = {'message': 'upstream failed'}
        response['content'][0]['text'] = json.dumps(body)
    with pytest.raises(RuntimeError):
        wind_history.fetch(Replay(response), QUERY, bindings, recipe)
