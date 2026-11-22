import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from gateway import config, core, market_provider as mp, panel
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
        'global_route': ['wind', 'ifind', 'tdx', 'akshare', 'tushare', 'choice'],
        'routes': {},
        'providers': {name: {'enabled': True, 'timeout_seconds': 10} for name in config.PROVIDERS},
        'market': {'min_request_interval_seconds': 0, 'max_concurrency': 4,
                   'per_provider_concurrency': 2, 'max_elapsed_seconds': 30},
    }
    config.write_json(tmp_path / 'config.json', settings)
    (tmp_path / '.env').write_text('')
    return tmp_path


class FakeRemote:
    def __init__(self, handler):
        self.handler = handler

    def call(self, tool, args):
        return self.handler(tool, args)


def test_panel_routing_tiers():
    c = {'global_route': ['wind', 'ifind', 'tdx', 'akshare', 'tushare', 'choice'], 'market': {}}
    # Multi-security queries use dedicated default route: tushare > choice > ifind > tdx > wind > akshare
    expected = ['tushare', 'choice', 'ifind', 'tdx', 'wind', 'akshare']
    assert panel._panel_route(c, 'stock', 'cross_section', 'auto') == expected
    assert panel._panel_route(c, 'stock', 'time_series', 'auto') == expected
    assert panel._panel_route(c, 'convertible_bond', 'cross_section', 'auto') == expected

    # Adjusted stock excludes non-adjusted choice/ifind while preserving exact default order
    assert panel._panel_route(c, 'stock', 'time_series', 'auto', adjust='qfq') == ['tushare', 'tdx', 'wind', 'akshare']

    # Custom market.panel_route override is respected
    c_custom = {'market': {'panel_route': ['choice', 'ifind', 'tushare']}}
    assert panel._panel_route(c_custom, 'stock', 'cross_section', 'auto')[:3] == ['choice', 'ifind', 'tushare']


def test_choice_ifind_tdx_batch_parsers():
    # Choice cross-section matrix sheet (columns=symbols, items=fields)
    choice_cs_raw = {
        'content': [{'type': 'text', 'text': json.dumps({
            'data': [{
                'columns': ['', '贵州茅台(600519.SH)', '平安银行(000001.SZ)'],
                'items': [['收盘价', '1420.08元', '11.52元'], ['涨跌幅', '-0.62%', '-0.60%']],
            }]
        })}]
    }
    res_cs = mp._choice_daily(
        FakeRemote(lambda t, a: choice_cs_raw), 'stock',
        {'start_date': '2025-11-05', 'end_date': '2025-11-05', 'trade_dates': ['2025-11-05'],
         'symbols': ['600519.SH', '000001.SZ'], 'fields': ['close', 'pct_change']},
    )
    by_sym = {r['symbol']: r for r in res_cs['data']}
    assert by_sym['600519.SH']['close'] == pytest.approx(1420.08)
    assert by_sym['000001.SZ']['close'] == pytest.approx(11.52)

    # Choice time-series matrix sheet (columns=dates, items=symbols)
    choice_ts_raw = {
        'content': [{'type': 'text', 'text': json.dumps({
            'data': [{
                'columns': ['', '2025-11-05(日)', '2025-11-04(日)'],
                'items': [['贵州茅台(600519.SH)', '1420.08元', '1429元'], ['平安银行(000001.SZ)', '11.52元', '11.59元']],
            }]
        })}]
    }
    res_ts = mp._choice_daily(
        FakeRemote(lambda t, a: choice_ts_raw), 'stock',
        {'start_date': '2025-11-04', 'end_date': '2025-11-05', 'trade_dates': ['2025-11-04', '2025-11-05'],
         'symbols': ['600519.SH', '000001.SZ'], 'fields': ['close']},
    )
    assert len(res_ts['data']) == 4

    # iFinD batch daily
    ifind_raw = {
        'content': [{'type': 'text', 'text': json.dumps({
            'data': {'answer': '|证券代码|证券简称|日期|收盘价|涨跌幅（单位：%）|\n|---|---|---|---|---|\n'
                               '|600519.SH|贵州茅台|20251105|1420.08|-0.62|\n'
                               '|000001.SZ|平安银行|20251105|11.52|-0.60|\n'}
        })}]
    }
    res_if = mp._ifind_batch_daily(
        FakeRemote(lambda t, a: ifind_raw), 'stock',
        {'start_date': '2025-11-05', 'end_date': '2025-11-05', 'symbols': ['600519.SH', '000001.SZ'],
         'fields': ['close', 'pct_change']},
    )
    assert len(res_if['data']) == 2

    # TDX batch cross-section
    tdx_raw = {
        'content': [{'type': 'text', 'text': json.dumps([
            {'market': 1, 'code': '600519', 'sec_name': '贵州茅台', '收盘价(复权方式:不复权,交易日:20251105)': '1420.08'},
            {'market': 0, 'code': '000001', 'sec_name': '平安银行', '收盘价(复权方式:不复权,交易日:20251105)': '11.52'},
        ])}]
    }
    res_tdx = mp._tdx_batch_cross_section(
        FakeRemote(lambda t, a: tdx_raw), 'stock',
        {'start_date': '2025-11-05', 'end_date': '2025-11-05', 'symbols': ['600519.SH', '000001.SZ'], 'fields': ['close']},
    )
    assert {r['symbol']: r['close'] for r in res_tdx['data']} == {'600519.SH': 1420.08, '000001.SZ': 11.52}


def test_cross_section_and_time_series_shapes_weekly_and_http(home, monkeypatch):
    dates = ['2025-11-03', '2025-11-07', '2025-11-10', '2025-11-14']
    prices = {
        ('000001.SZ', '2025-11-03'): (11.40, 0.5),
        ('000001.SZ', '2025-11-07'): (11.60, 1.0),
        ('000001.SZ', '2025-11-10'): (11.70, 0.8),
        ('000001.SZ', '2025-11-14'): (12.00, 2.5),
        ('600519.SH', '2025-11-03'): (1430.0, -0.5),
        ('600519.SH', '2025-11-07'): (1450.0, 1.4),
        ('600519.SH', '2025-11-10'): (1460.0, 0.7),
        ('600519.SH', '2025-11-14'): (1480.0, 1.3),
    }

    async def fake_invoke(name, query, timeout):
        call = query['market_call']
        if call['operation'] == 'calendar':
            return {'dates': dates, 'raw': dates, 'complete': True}
        if call['operation'] in {'daily', 'batch_daily'}:
            rows = []
            for sym in call.get('symbols') or ['000001.SZ', '600519.SH']:
                for d in call.get('trade_dates') or [call['start_date']]:
                    if (sym, d) in prices:
                        c_val, p_val = prices[(sym, d)]
                        rows.append({'symbol': sym, 'date': d, 'close': c_val, 'pct_change': p_val})
            return {'data': rows, 'raw': rows, 'strategy': 'native_full', 'complete': True, 'notes': []}
        raise AssertionError(call)

    monkeypatch.setattr(core, 'invoke', fake_invoke)

    with TestClient(app) as client:
        # 1. Capabilities endpoint
        caps = client.get('/api/market/capabilities').json()
        assert set(caps['providers'].keys()) == set(config.PROVIDERS)
        assert caps['providers']['tushare']['mode'] == 'native_full'
        assert caps['providers']['wind']['mode'] == 'scalar'

        # 2. Cross-section: index=codes, columns=fields
        cs = client.post('/api/market/cross-section', json={
            'scope': 'stock', 'date': '2025-11-08',  # weekend -> rolls back to 2025-11-07
            'symbols': ['000001.SZ', '600519.SH'],
            'fields': ['close', 'pct_change'],
            'sort_by': 'pct_change', 'order': 'desc',
        }).json()
        assert cs['shape'] == 'cross_section'
        assert cs['window']['actual_date'] == '2025-11-07'
        assert cs['index'] == ['600519.SH', '000001.SZ']
        assert cs['columns'] == ['close', 'pct_change']
        assert cs['matrix']['rows'] == [[1450.0, 1.4], [11.6, 1.0]]
        assert 'capabilities' in cs and 'tushare' in cs['capabilities']

        cs_csv = client.get(cs['download_url']).text
        assert 'symbol,close,pct_change' in cs_csv

        # 3. Time-series (weekly): index=dates, columns=codes
        ts = client.post('/api/market/time-series', json={
            'scope': 'stock', 'start_date': '2025-11-03', 'end_date': '2025-11-14',
            'field': 'close', 'frequency': 'weekly',
            'symbols': ['000001.SZ', '600519.SH'],
        }).json()
        assert ts['shape'] == 'time_series'
        assert ts['frequency'] == 'weekly'
        assert ts['index'] == ['2025-11-07', '2025-11-14']
        assert ts['columns'] == ['000001.SZ', '600519.SH']
        assert ts['matrix']['rows'] == [[11.6, 1450.0], [12.0, 1480.0]]
        assert 'capabilities' in ts

        ts_csv = client.get(ts['download_url']).text
        assert 'date,000001.SZ,600519.SH' in ts_csv
