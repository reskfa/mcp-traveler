import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from gateway import config, core
from gateway import market as m
from gateway import market_provider as mp
from gateway.acquisition import adjusted_frame
from gateway.app import app


@pytest.fixture
def home(tmp_path, monkeypatch):
    @asynccontextmanager
    async def http_lifespan(app):
        yield
    monkeypatch.setattr(app.router, 'lifespan_context', http_lifespan)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(core, 'available', lambda *args: True)
    monkeypatch.setattr(core, 'record', lambda *args: None)
    settings = {
        'global_route': ['tushare', 'choice', 'akshare'],
        'routes': {},
        'providers': {name: {'enabled': True, 'timeout_seconds': 10} for name in config.PROVIDERS},
        'market': {'min_request_interval_seconds': 0, 'max_concurrency': 4,
                   'per_provider_concurrency': 2, 'max_elapsed_seconds': 30},
    }
    config.write_json(tmp_path / 'config.json', settings)
    (tmp_path / '.env').write_text('')
    return tmp_path


def result(rows, complete=True, **extra):
    return {'data': rows, 'raw': rows, 'strategy': 'native_full', 'complete': complete,
            'source_reported_rows': len(rows), 'notes': [], **extra}


def install_market_fake(monkeypatch, dates, symbols, rows_for_call, universe=None, calls=None):
    async def invoke(name, query, timeout):
        call = query['market_call']
        if calls is not None:
            calls.append((name, copy.deepcopy(call)))
        if call['operation'] == 'calendar':
            selected = [day for day in dates if call['start_date'] <= day <= call['end_date']]
            return {'dates': selected, 'raw': selected, 'complete': True, 'strategy': 'native_full'}
        if call['operation'] == 'universe':
            rows = universe if universe is not None else [
                {'symbol': symbol, 'name': symbol, 'list_date': '2020-01-01', 'delist_date': None}
                for symbol in symbols]
            return result(rows, universe_complete=True, conflicts=[])
        if call['operation'] == 'daily':
            return rows_for_call(name, call)
        raise AssertionError(call)
    monkeypatch.setattr(core, 'invoke', invoke)


def test_request_scope_and_symbol_validation():
    with pytest.raises(ValueError, match='空数组'):
        m.MarketDailyRequest(scope='stock', start_date='2026-11-20', end_date='2026-11-20', symbols=[])
    request = m.MarketDailyRequest(scope='stock', start_date='2026-11-20', end_date='2026-11-20',
                                   symbols=['600519.SH', '600519.SH'])
    assert request.symbols == ['600519.SH']
    with pytest.raises(ValueError, match='转债'):
        m.MarketDailyRequest(scope='stock', start_date='2026-11-20', end_date='2026-11-20', symbols=['118027.SH'])
    with pytest.raises(ValueError, match='格式'):
        m.MarketDailyRequest(scope='stock', start_date='2026-11-20', end_date='2026-11-20', symbols=['T600018.SH'])


def test_historical_universe_and_unknown_dates(home, monkeypatch):
    universe = [
        {'symbol': '000001.SZ', 'name': 'current', 'list_date': '2020-01-01', 'delist_date': None},
        {'symbol': 'T600018.SH', 'name': 'delisted', 'list_date': '2000-01-01', 'delist_date': '2024-01-01'},
        {'symbol': '600519.SH', 'name': 'later', 'list_date': '2025-01-01', 'delist_date': None},
        {'symbol': '000002.SZ', 'name': 'unknown', 'list_date': None, 'delist_date': None},
    ]
    install_market_fake(monkeypatch, [], [], lambda *args: None, universe=universe)
    request = m.MarketUniverseRequest(scope='stock', as_of_date='2023-01-01', provider='tushare')
    response = asyncio.run(m.query_market_universe(request))
    assert [row['symbol'] for row in response['data']] == ['000001.SZ', 'T600018.SH']
    assert response['coverage']['unknown'] == ['000002.SZ']
    assert response['status'] == 'partial' and not response['complete']
    assert not response['universe']['complete']


def test_complete_json_and_top_n_projection(home, monkeypatch):
    symbols = [f'{index:06d}.SZ' for index in range(1, 121)]

    def daily(name, call):
        rows = [{'symbol': symbol, 'date': call['start_date'], 'close': 10 + index,
                 'pct_change': float(index % 17)} for index, symbol in enumerate(call['symbols'])]
        return result(rows)

    install_market_fake(monkeypatch, ['2026-11-20'], symbols, daily)
    request = m.MarketDailyRequest(scope='stock', symbols=symbols, start_date='2026-11-20',
                                   end_date='2026-11-20', provider='tushare', sort_by='pct_change',
                                   order='desc', top_n=5)
    response = asyncio.run(m.query_market_daily(request))
    assert response['row_count'] == 5 and response['full_row_count'] == 120
    assert len(response['data']) == len(response['field_sources']) == 5
    stored = json.loads(open(response['data_path']).read())
    assert stored['row_count'] == len(stored['data']) == len(stored['field_sources']) == 120
    assert response['data'] == stored['data'][:5]
    assert response['projection'] == {'type': 'top_n', 'top_n': 5, 'full_row_count': 120, 'returned_row_count': 5}


def test_full_market_scalar_receives_resolved_universe(home, monkeypatch):
    symbols = ['118027.SH', '128044.SZ']
    calls = []

    def daily(name, call):
        assert name == 'akshare'
        assert call['symbols'] and len(call['symbols']) == 1
        return result([{'symbol': call['symbols'][0], 'date': '2026-11-20', 'close': 100, 'pct_change': 0}],
                      strategy='scalar')

    install_market_fake(monkeypatch, ['2026-11-20'], symbols, daily, calls=calls)
    request = m.MarketDailyRequest(scope='convertible_bond', start_date='2026-11-20',
                                   end_date='2026-11-20', provider='akshare')
    response = asyncio.run(m.query_market_daily(request))
    assert response['row_count'] == 2
    scalar_calls = [call for name, call in calls if name == 'akshare' and call['operation'] == 'daily']
    assert [call['symbols'] for call in scalar_calls] == [[symbol] for symbol in symbols]


def test_adjusted_window_uses_one_anchor_call(home, monkeypatch):
    dates = ['2026-11-18', '2026-11-19', '2026-11-20']
    calls = []

    def daily(name, call):
        return result([{'symbol': '600519.SH', 'date': day, 'close': 10, 'pct_change': 0} for day in dates])

    install_market_fake(monkeypatch, dates, ['600519.SH'], daily, calls=calls)
    request = m.MarketDailyRequest(scope='stock', symbols=['600519.SH'], start_date=dates[0],
                                   end_date=dates[-1], provider='tushare', adjust='qfq', fields=['close'])
    response = asyncio.run(m.query_market_daily(request))
    daily_calls = [call for name, call in calls if call['operation'] == 'daily']
    assert len(daily_calls) == 1
    assert daily_calls[0]['trade_dates'] == dates
    assert daily_calls[0]['start_date'] == dates[0] and daily_calls[0]['end_date'] == dates[-1]
    assert response['complete']


def test_returns_window_stable_ranking_and_exclusions(home, monkeypatch):
    dates = ['2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19', '2026-11-20']
    symbols = ['000001.SZ', '000002.SZ', '000003.SZ', '000004.SZ']
    closes = {'000001.SZ': 8, '000002.SZ': 9, '000003.SZ': 8}

    def daily(name, call):
        assert call['adjust'] == 'qfq'
        rows = []
        for symbol in symbols:
            for day in call['trade_dates']:
                if symbol == '000004.SZ' and day == '2026-11-18':
                    continue
                close = 10 if day == '2026-11-16' else closes.get(symbol, 10) if day == '2026-11-20' else 10
                rows.append({'symbol': symbol, 'date': day, 'close': close})
        return result(rows)

    install_market_fake(monkeypatch, dates, symbols, daily)
    request = m.MarketReturnsRequest(scope='stock', symbols=symbols, trading_days=4,
                                     end_date='2026-11-20', provider='tushare', top_n=2)
    response = asyncio.run(m.query_market_returns(request))
    assert response['window']['baseline_date'] == '2026-11-16'
    assert response['window']['actual_trade_dates'] == dates[1:]
    assert [row['symbol'] for row in response['data']] == ['000001.SZ', '000003.SZ']
    assert [row['rank'] for row in response['data']] == [1, 2]
    stored = json.loads(open(response['data_path']).read())
    assert [row['symbol'] for row in stored['data']] == ['000001.SZ', '000003.SZ', '000002.SZ']
    assert stored['data'][0]['return_pct'] == pytest.approx(-20)
    assert stored['coverage']['excluded_symbols']['000004.SZ']['reason'] == 'missing_window_prices'
    assert not stored['coverage']['ranking_complete']


def test_returns_reject_non_trading_end_date(home, monkeypatch):
    install_market_fake(monkeypatch, ['2026-11-20'], [], lambda *args: None)
    request = m.MarketReturnsRequest(scope='convertible_bond', symbols=['118027.SH'], trading_days=1,
                                     end_date='2026-11-21', provider='tushare')
    with pytest.raises(ValueError, match='不是交易日'):
        asyncio.run(m.query_market_returns(request))


def test_tushare_conversions_and_shared_adjustment_anchor():
    factor_history = [
        {'ts_code': '600519.SH', 'trade_date': '20200102', 'adj_factor': 1},
        {'ts_code': '600519.SH', 'trade_date': '20261118', 'adj_factor': 2},
        {'ts_code': '600519.SH', 'trade_date': '20261119', 'adj_factor': 4},
        {'ts_code': '600519.SH', 'trade_date': '20261120', 'adj_factor': 8},
    ]

    class SDK:
        def daily(self, **kwargs):
            day = kwargs['trade_date']
            return pd.DataFrame([{'ts_code': '600519.SH', 'trade_date': day, 'close': 10,
                                  'vol': 2, 'amount': 3}])

        def adj_factor(self, **kwargs):
            if 'trade_date' not in kwargs:
                return pd.DataFrame(factor_history)
            day = kwargs['trade_date']
            return pd.DataFrame([row for row in factor_history if row['trade_date'] == day])

    call = {'scope': 'stock', 'start_date': '2026-11-18', 'end_date': '2026-11-19',
            'trade_dates': ['2026-11-18', '2026-11-19'], 'fields': ['close', 'volume', 'amount'],
            'adjust': 'qfq', 'symbols': ['600519.SH'], 'adjustment_anchor_date': '2026-11-20'}
    response = mp._tushare_daily(SDK(), 'stock', call)
    raw = pd.DataFrame([{'trade_date': '20261118', 'close': 10}, {'trade_date': '20261119', 'close': 10}])
    legacy, _ = adjusted_frame(raw, SDK(), {
        'method': 'adj_factor', 'params': {'ts_code': '600519.SH'},
        'date_column': 'trade_date', 'factor_column': 'adj_factor', 'fields': ['close']},
        {'dataset': 'daily_prices', 'symbol': '600519.SH', 'start_date': '2026-11-18',
         'end_date': '2026-11-19', 'adjust': 'qfq'})
    assert [row['close'] for row in response['data']] == legacy['close'].tolist() == [2.5, 5]
    assert response['data'][0]['volume'] == 200
    assert response['data'][0]['amount'] == 3000

    hfq = mp._tushare_daily(SDK(), 'stock', {**call, 'adjust': 'hfq'})
    assert [row['close'] for row in hfq['data']] == [20, 40]
    request = m.MarketDailyRequest(scope='stock', symbols=['600519.SH'], start_date='2026-11-18',
                                   end_date='2026-11-19', adjust='hfq', fields=['close'])
    tasks = m._daily_tasks('tushare', request, ['600519.SH'], call['trade_dates'], {}, m.MARKET_DEFAULTS)
    assert [task['strategy'] for task in tasks] == ['scalar']


def test_choice_missing_symbols_fields_and_dates_are_partial():
    class Remote:
        def call(self, tool, arguments):
            assert 'query' in arguments and 'question' not in arguments
            return {'data': {'columns': [{'name': '债券代码'}, {'name': '交易日期'}, {'name': '涨跌幅'}],
                             'rows': [['118027.SH', '2026-11-20', -0.5]]}}

    call = {'scope': 'convertible_bond', 'start_date': '2026-11-19', 'end_date': '2026-11-20',
            'trade_dates': ['2026-11-19', '2026-11-20'], 'symbols': ['118027.SH', '128044.SZ'],
            'fields': ['close', 'pre_close', 'pct_change'], 'adjust': 'none'}
    response = mp._choice_daily(Remote(), 'convertible_bond', call)
    assert not response['complete']
    assert response['missing_symbols'] == ['128044.SZ']
    assert response['missing_fields'] == ['close', 'pre_close']
    assert response['missing_dates'] == ['2026-11-19']


def test_market_http_contract(home, monkeypatch):
    def daily(name, call):
        return result([{'symbol': symbol, 'date': '2026-11-20', 'close': 100, 'pct_change': 1}
                       for symbol in call['symbols']])

    install_market_fake(monkeypatch, ['2026-11-20'], ['118027.SH'], daily)
    with TestClient(app) as client:
        response = client.post('/api/market/daily', json={
            'scope': 'convertible_bond', 'symbols': ['118027.SH'], 'start_date': '2026-11-20',
            'end_date': '2026-11-20', 'provider': 'tushare'})
        assert response.status_code == 200
        assert response.json()['data'][0]['symbol'] == '118027.SH'
        assert client.post('/api/market/daily', json={
            'scope': 'stock', 'symbols': [], 'start_date': '2026-11-20', 'end_date': '2026-11-20'}).status_code == 422


def test_fallback_adds_rows_and_preserves_zero(home, monkeypatch):
    calls = []

    def daily(name, call):
        if name == 'tushare':
            return result([{'symbol': '118027.SH', 'date': '2026-11-20', 'close': 0, 'pct_change': None}])
        return result([{'symbol': '118027.SH', 'date': '2026-11-20', 'close': 99, 'pct_change': 1},
                       {'symbol': '128044.SZ', 'date': '2026-11-20', 'close': 80, 'pct_change': 2}])

    install_market_fake(monkeypatch, ['2026-11-20'], ['118027.SH', '128044.SZ'], daily, calls=calls)
    request = m.MarketDailyRequest(scope='convertible_bond', symbols=['118027.SH', '128044.SZ'],
                                   start_date='2026-11-20', end_date='2026-11-20')
    response = asyncio.run(m.query_market_daily(request))
    assert response['complete'] and response['provider'] == 'mixed'
    assert response['data'][0]['close'] == 0 and response['data'][0]['pct_change'] == 1
    assert response['field_sources'][0] == {'close': 'tushare', 'pct_change': 'choice'}
    assert response['data'][1]['symbol'] == '128044.SZ'
    assert [name for name, call in calls if call['operation'] == 'daily'] == ['tushare', 'choice']


def test_daily_descending_sort_keeps_symbol_tie_order():
    request = m.MarketDailyRequest(scope='convertible_bond', start_date='2026-11-20', end_date='2026-11-20',
                                   symbols=['118027.SH', '128044.SZ'], sort_by='pct_change', order='desc')
    rows = [{'symbol': '128044.SZ', 'date': '2026-11-20', 'pct_change': 1},
            {'symbol': '118027.SH', 'date': '2026-11-20', 'pct_change': 1}]
    ordered = m._sort_daily(rows, [{}, {}], request)
    assert [row['symbol'] for row, sources in ordered] == ['118027.SH', '128044.SZ']


def test_run_tasks_deadline_marks_unstarted_work(home, monkeypatch):
    async def slow(name, query, timeout):
        await asyncio.sleep(.1)
        return result([])
    monkeypatch.setattr(core, 'invoke', slow)
    settings = {**m.MARKET_DEFAULTS, 'max_concurrency': 1, 'per_provider_concurrency': 1,
                'min_request_interval_seconds': 0}
    tasks = [{'provider': 'tushare', 'call': {'operation': 'daily', 'symbols': [f'11802{i}.SH']},
              'timeout': 10, 'strategy': 'scalar'} for i in range(3)]
    outcomes = asyncio.run(m._run_tasks(tasks, time.monotonic() + .01, settings))
    assert [outcome['status'] for outcome in outcomes] == ['timeout', 'timeout', 'not_attempted']
    assert '未发起请求' in outcomes[-1]['error']


def test_incomplete_provider_result_cannot_be_complete_or_ranked_complete(home, monkeypatch):
    dates = ['2026-11-19', '2026-11-20']

    def daily(name, call):
        return result([{'symbol': '118027.SH', 'date': day, 'close': 100, 'pct_change': 0}
                       for day in call['trade_dates']], complete=False)

    install_market_fake(monkeypatch, dates, ['118027.SH'], daily)
    daily_response = asyncio.run(m.query_market_daily(m.MarketDailyRequest(
        scope='convertible_bond', symbols=['118027.SH'], start_date='2026-11-20', end_date='2026-11-20', provider='tushare')))
    assert daily_response['data'] and not daily_response['complete']
    assert daily_response['attempts'][-1]['status'] == 'partial'
    returns = asyncio.run(m.query_market_returns(m.MarketReturnsRequest(
        scope='convertible_bond', symbols=['118027.SH'], trading_days=1, end_date='2026-11-20', provider='tushare')))
    assert returns['data'] and not returns['coverage']['ranking_complete']


def test_full_market_filters_rows_outside_resolved_universe(home, monkeypatch):
    def daily(name, call):
        return result([{'symbol': '000001.SZ', 'date': '2026-11-20', 'close': 10, 'pct_change': 1},
                       {'symbol': '000002.SZ', 'date': '2026-11-20', 'close': 20, 'pct_change': 2}])

    install_market_fake(monkeypatch, ['2026-11-20'], ['000001.SZ'], daily)
    response = asyncio.run(m.query_market_daily(m.MarketDailyRequest(
        scope='stock', start_date='2026-11-20', end_date='2026-11-20', provider='tushare')))
    assert response['row_count'] == 1 and response['data'][0]['symbol'] == '000001.SZ'


def test_choice_universe_requires_matching_reported_count_and_scope():
    class Remote:
        def call(self, tool, arguments):
            return {'summary': '共计 2 条数据', 'data': {'columns': [
                {'name': '股票代码'}, {'name': '股票简称'}, {'name': '上市日期'}, {'name': '退市日期'}],
                'rows': [['600519.SH', '贵州茅台', '2001-08-27', None]]}}

    response = mp._choice_universe(Remote(), 'stock', {})
    assert not response['complete'] and not response['universe_complete']
    assert response['source_reported_rows'] == 2
    wrong_scope = mp._choice_universe(Remote(), 'convertible_bond', {})
    assert wrong_scope['data'] == [] and not wrong_scope['complete']


def test_conflicting_identity_columns_are_rejected():
    assert mp.symbol_for_row({'股票代码': '128044.SZ', '债券代码': '118027.SH'}) is None
    assert mp.symbol_for_row({'股票代码': '128044', '债券代码': '128044.SZ'}) == '128044.SZ'
