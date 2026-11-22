import asyncio
import json
from contextlib import asynccontextmanager

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from gateway import config, core, helpers, market as m, market_provider as mp, parsing
from gateway.app import app


@pytest.fixture
def home(tmp_path, monkeypatch):
    @asynccontextmanager
    async def http_lifespan(app):
        yield
    monkeypatch.setattr(app.router, 'lifespan_context', http_lifespan)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
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
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return self.handler(tool, args)


def test_request_validation():
    with pytest.raises(ValueError, match='开始日期不能晚于结束日期'):
        helpers.TradeCalendarRequest(start_date='2025-11-30', end_date='2025-11-01')
    with pytest.raises(ValueError, match='日期'):
        helpers.MarketSymbolsRequest(scope='stock', as_of_date='20251101')


def test_calendar_route_prefers_native_over_index_derived():
    c = {'global_route': ['wind', 'ifind', 'tdx', 'akshare', 'tushare', 'choice']}
    order = helpers._calendar_route(c, 'auto')
    assert order[:2] == ['akshare', 'tushare']
    assert set(order[2:]) == {'wind', 'ifind', 'tdx', 'choice'}
    assert helpers._calendar_route(c, 'wind') == ['wind']


def test_akshare_calendar_and_universe():
    class FakeAk:
        def tool_trade_date_hist_sina(self):
            return pd.DataFrame({'trade_date': ['2025-11-03', '2025-11-04', '2025-11-05']})

        def stock_info_sh_name_code(self, symbol='主板A股'):
            if symbol == '主板A股':
                return pd.DataFrame([{'证券代码': '600519', '证券简称': '贵州茅台', '上市日期': '2001-08-27'}])
            return pd.DataFrame([{'证券代码': '688001', '证券简称': '华兴源创', '上市日期': '2019-07-22'}])

        def stock_info_sz_name_code(self, symbol='A股列表'):
            return pd.DataFrame([{'A股代码': '000001', 'A股简称': '平安银行', 'A股上市日期': '1991-04-03'}])

        def stock_info_bj_name_code(self):
            return pd.DataFrame([{'证券代码': '920000', '证券简称': '安徽凤凰', '上市日期': '2020-12-23'}])

        def stock_info_sh_delist(self):
            return pd.DataFrame([{'公司代码': '600001', '公司简称': '邯郸钢铁', '上市日期': '1998-01-22', '暂停上市日期': '2009-12-29'}])

        def stock_info_sz_delist(self):
            return pd.DataFrame([{'证券代码': '000003', '证券简称': 'PT金田A', '上市日期': '1991-01-14', '终止上市日期': '2002-06-14'}])

        def bond_zh_cov(self):
            return pd.DataFrame([
                {'债券代码': '113050', '债券简称': '南银转债', '上市时间': '2021-07-01'},
                {'债券代码': '123001', '债券简称': '蓝标转债', '上市时间': '2016-01-08'},
            ])

    ak = FakeAk()
    cal = mp._akshare_calendar(ak, {'start_date': '2025-11-01', 'end_date': '2025-11-05', 'exchange': 'SSE'})
    assert cal['dates'] == ['2025-11-03', '2025-11-04', '2025-11-05']
    assert cal['calendar_mode'] == 'native'
    assert cal['complete'] is True

    stock_uni = mp._akshare_universe(ak, 'stock', {'include_delisted': True})
    assert [r['symbol'] for r in stock_uni['data']] == [
        '000001.SZ', '000003.SZ', '600001.SH', '600519.SH', '688001.SH', '920000.BJ'
    ]
    assert stock_uni['complete'] is True

    cb_uni = mp._akshare_universe(ak, 'convertible_bond', {'include_delisted': False})
    assert [r['symbol'] for r in cb_uni['data']] == ['113050.SH', '123001.SZ']
    assert cb_uni['current_complete'] is True


def test_mcp_provider_calendars_including_ifind_weekend_filter():
    # Wind
    wind_remote = FakeRemote(lambda tool, args: {
        'content': [{'type': 'text', 'text': json.dumps({
            'data': {
                'columns': [{'name': 'TIME'}, {'name': 'MATCH'}],
                'rows': [['2025-11-03T00:00:00+08:00', '3976.5'], ['2025-11-04T00:00:00+08:00', '3960.2']],
            }
        })}]
    })
    w_cal = mp._wind_calendar(wind_remote, {'start_date': '2025-11-01', 'end_date': '2025-11-05', 'exchange': 'SSE'})
    assert w_cal['dates'] == ['2025-11-03', '2025-11-04']
    assert w_cal['calendar_mode'] == 'index_derived'

    # iFinD: must drop weekend rows with blank 成交量
    ifind_remote = FakeRemote(lambda tool, args: {
        'content': [{'type': 'text', 'text': json.dumps({
            'data': {
                'answer': '|证券代码|证券简称|日期|成交量|\n|---|---|---|---|\n'
                          '|000001.SH|上证指数|20251103|450亿|\n'
                          '|000001.SH|上证指数|20251102||\n'
                          '|000001.SH|上证指数|20251101||\n'
            }
        })}]
    })
    i_cal = mp._ifind_calendar(ifind_remote, {'start_date': '2025-11-01', 'end_date': '2025-11-05', 'exchange': 'SSE'})
    assert i_cal['dates'] == ['2025-11-03']

    # TDX
    tdx_remote = FakeRemote(lambda tool, args: {
        'content': [{'type': 'text', 'text': json.dumps({
            'Rows': [{'Data': '20251103', 'Close': '3976'}, {'Data': '20251104', 'Close': '3960'}]
        })}]
    })
    t_cal = mp._tdx_calendar(tdx_remote, {'start_date': '2025-11-01', 'end_date': '2025-11-05', 'exchange': 'SSE'})
    assert t_cal['dates'] == ['2025-11-03', '2025-11-04']

    # Choice
    choice_remote = FakeRemote(lambda tool, args: {
        'content': [{'type': 'text', 'text': json.dumps({
            'data': [{
                'columns': ['上证指数(000001.SH)(指数)', '2025-11-04(日)', '2025-11-03(日)'],
                'items': [['收盘价', '3960点', '3976点']],
            }]
        })}]
    })
    c_cal = mp._choice_calendar(choice_remote, {'start_date': '2025-11-01', 'end_date': '2025-11-05', 'exchange': 'SSE'})
    assert c_cal['dates'] == ['2025-11-03', '2025-11-04']


def test_tdx_wind_ifind_and_choice_row_table_universe(monkeypatch):
    # Choice row-oriented screener table must NOT be transposed
    choice_raw = {
        'content': [{'type': 'text', 'text': json.dumps({
            'data': [{
                'columns': ['序号', '代码', '名称', '上市日期'],
                'items': [['1', '600519', '贵州茅台', '2001-08-27'], ['2', '000001', '平安银行', '1991-04-03']],
            }]
        })}]
    }
    parsed = [row for row, _ in parsing.tables(choice_raw)]
    assert parsed[0]['代码'] == '600519' and parsed[1]['代码'] == '000001'

    # TDX paginated screener
    def tdx_handler(tool, args):
        if args['pageNo'] == 1:
            txt = '## 查询"全部A股"在A股市场中共找到 2 条记录，当前第 1 页，显示 2 条\n\n| 序号 | market | sec_code | sec_name |\n| --- | --- | --- | --- |\n| 1 | 1 | 600519 | 贵州茅台 |\n| 2 | 2 | 920000 | 安徽凤凰 |\n'
            return {'content': [{'type': 'text', 'text': txt}]}
        return {'content': [{'type': 'text', 'text': ''}]}

    tdx_res = mp._tdx_universe(FakeRemote(tdx_handler), 'stock', {})
    assert [r['symbol'] for r in tdx_res['data']] == ['600519.SH', '920000.BJ']
    assert tdx_res['current_complete'] is True
    with pytest.raises(mp.MarketUnsupported):
        mp._tdx_universe(FakeRemote(tdx_handler), 'convertible_bond', {})

    # Wind must be excluded from auto-routing and raise MarketUnsupported for universe
    c = {'global_route': ['wind', 'ifind', 'tdx', 'akshare', 'tushare', 'choice']}
    assert 'wind' not in helpers._symbols_route(c, 'stock', 'auto')
    assert 'wind' not in helpers._symbols_route(c, 'convertible_bond', 'auto')
    with pytest.raises(mp.MarketUnsupported):
        mp._wind_universe(FakeRemote(lambda tool, args: {}), 'stock', {})

    # iFinD CB full CSV download
    monkeypatch.setattr(mp, '_fetch_ifind_csv', lambda url, timeout=20: [
        {'代码': '113050.SH', '代码简称': '南银转债', '上市日期': '2021-07-01'},
        {'代码': '123001.SZ', '代码简称': '蓝标转债', '上市日期': '2016-01-08'},
    ])
    ifind_cb = mp._ifind_universe(FakeRemote(lambda tool, args: {
        'content': [{'type': 'text', 'text': json.dumps({
            'data': {'answer': {'sub_answer_1': '前 2 行\nCSV 下载链接为：http://o.thsi.cn/ifind-python-aime-tools-service.result-cache/test.csv'}}
        }, ensure_ascii=False)}]
    }), 'convertible_bond', {})
    assert [r['symbol'] for r in ifind_cb['data']] == ['113050.SH', '123001.SZ']
    assert ifind_cb['current_complete'] is True and ifind_cb['truncated'] is False

    # iFinD A-share 8-slice CSV download
    slice_counter = {'n': 0}

    def fake_ifind_stock_csv(url, timeout=20):
        slice_counter['n'] += 1
        code = f'600{slice_counter["n"]:03d}.SH'
        return [{'股票代码': code, '股票简称': f'股票{slice_counter["n"]}', '新股上市日期': '20200101'}]

    monkeypatch.setattr(mp, '_fetch_ifind_csv', fake_ifind_stock_csv)
    ifind_stock = mp._ifind_universe(FakeRemote(lambda tool, args: {
        'content': [{'type': 'text', 'text': json.dumps({
            'data': {'selectedSecuritiesCount': 1, 'answer': 'http://o.thsi.cn/ifind-java-ds-service-basic.cloud-model-trans/slice.csv'}
        })}]
    }), 'stock', {'slice_delay': 0})
    assert len(ifind_stock['data']) == len(mp.IFIND_STOCK_SLICES)
    assert ifind_stock['current_complete'] is True and ifind_stock['truncated'] is False


def test_helpers_non_tushare_fallback_and_http_endpoints(home, monkeypatch):
    # Disable Tushare completely to prove zero Tushare lock-in!
    monkeypatch.setattr(core, 'available', lambda name, p: name != 'tushare')
    called = []

    async def fake_invoke(name, query, timeout):
        call = query['market_call']
        called.append((name, call['operation']))
        if call['operation'] == 'calendar':
            return {'dates': ['2025-11-03', '2025-11-04'],
                    'data': [{'date': '2025-11-03', 'exchange': 'SSE', 'is_open': True, 'pretrade_date': None},
                             {'date': '2025-11-04', 'exchange': 'SSE', 'is_open': True, 'pretrade_date': None}],
                    'raw': [], 'complete': True, 'calendar_mode': 'native', 'strategy': 'native_full',
                    'upstream': f'{name} calendar', 'notes': []}
        if call['operation'] == 'universe':
            return {'data': [
                {'symbol': '000001.SZ', 'name': '平安银行', 'list_date': '1991-04-03', 'delist_date': None},
                {'symbol': '600001.SH', 'name': '邯郸钢铁', 'list_date': '1998-01-22', 'delist_date': '2009-12-29'},
            ], 'raw': [], 'strategy': 'native_full', 'complete': True, 'universe_complete': True,
               'current_complete': True, 'source_reported_rows': 2, 'conflicts': [], 'notes': [],
               'upstream': f'{name} universe'}
        raise AssertionError(call)

    monkeypatch.setattr(core, 'invoke', fake_invoke)

    with TestClient(app) as client:
        caps = client.get('/api/helpers/capabilities').json()
        assert 'trade_calendar' in caps['helpers'] and 'market_symbols' in caps['helpers']
        assert caps['providers']['tushare']['available'] is False
        assert caps['providers']['akshare']['available'] is True

        cal_resp = client.post('/api/helpers/trade-calendar', json={
            'start_date': '2025-11-01', 'end_date': '2025-11-05', 'provider': 'auto'
        })
        assert cal_resp.status_code == 200
        cal = cal_resp.json()
        assert cal['provider'] == 'akshare'
        assert cal['dates'] == ['2025-11-03', '2025-11-04']
        assert cal['complete'] is True

        csv_resp = client.get(cal['download_url'])
        assert csv_resp.status_code == 200
        assert '2025-11-03' in csv_resp.text

        sym_resp = client.post('/api/helpers/market-symbols', json={
            'scope': 'stock', 'include_delisted': False, 'provider': 'auto'
        })
        assert sym_resp.status_code == 200
        sym = sym_resp.json()
        assert sym['provider'] == 'akshare'
        assert sym['symbols'] == ['000001.SZ']  # delisted excluded when include_delisted=False

        sym_hist = client.post('/api/helpers/market-symbols', json={
            'scope': 'stock', 'as_of_date': '2005-01-01', 'provider': 'auto'
        }).json()
        assert sym_hist['symbols'] == ['000001.SZ', '600001.SH']

    # Also verify market._calendar works without Tushare
    c = config.load()
    dates, attempts, _, cal_prov = asyncio.run(
        m._calendar('2025-11-01', '2025-11-05', 'auto', c, 1e12, m._settings(c))
    )
    assert cal_prov == 'akshare' and dates == ['2025-11-03', '2025-11-04']


def test_search_securities_providers_and_http(home, monkeypatch):
    with pytest.raises(ValueError, match='不能为空'):
        helpers.SearchSecuritiesRequest(query='   ')

    # TDX lookup parser: keeps A股代码 only, drops HK/Fund, supports alias & code lookup
    tdx_text = (
        '找到 3 个与"茅台"相关的A股:\n\n'
        '1. 【600519】贵州茅台\n  类型: A股代码 | setcode: 1\n  别名: gzmt, MOUTAI\n\n'
        '2. 【01364】古茗\n  类型: 港股代码 | setcode: 31\n  别名: gm\n\n'
        '3. 【603919】金徽酒\n  类型: A股代码 | setcode: 1\n  别名: jhj\n'
    )
    tdx_res = mp._tdx_search_securities(
        FakeRemote(lambda tool, args: {'content': [{'type': 'text', 'text': tdx_text}]}),
        'stock', {'query': '茅台', 'limit': 10},
    )
    assert [r['symbol'] for r in tdx_res['data']] == ['600519.SH']
    assert tdx_res['data'][0]['name'] == '贵州茅台'
    with pytest.raises(mp.MarketUnsupported):
        mp._tdx_search_securities(FakeRemote(lambda *a: {}), 'convertible_bond', {'query': '南银转债'})

    # Wind bidirectional stock & CB search
    wind_res = mp._wind_search_securities(
        FakeRemote(lambda tool, args: {
            'content': [{'type': 'text', 'text': json.dumps({
                'data': {'data': [{'columns': [{'name': 'Wind代码'}, {'name': '证券简称'}],
                                   'rows': [['113050.SH', '南银转债']]}]}
            })}]
        }),
        'convertible_bond', {'query': '113050', 'limit': 10},
    )
    assert wind_res['data'][0]['symbol'] == '113050.SH'
    assert wind_res['data'][0]['name'] == '南银转债'
    assert wind_res['data'][0]['match_type'] == 'exact'

    # HTTP endpoint + auto-routing without Tushare
    monkeypatch.setattr(core, 'available', lambda name, p: name != 'tushare')

    async def fake_invoke(name, query, timeout):
        call = query['market_call']
        assert call['operation'] == 'search_securities'
        return {
            'data': [
                {'symbol': '600519.SH', 'name': '贵州茅台', 'scope': 'stock', 'exchange': 'SH',
                 'list_date': '2001-08-27', 'delist_date': None, 'status': 'listed', 'match_type': 'contains'},
            ],
            'raw': {}, 'strategy': 'native_search', 'complete': True, 'total_matches': 1,
            'notes': [], 'upstream': f'{name} search',
        }

    monkeypatch.setattr(core, 'invoke', fake_invoke)
    with TestClient(app) as client:
        resp = client.post('/api/helpers/search-securities', json={'query': '茅台', 'scope': 'all'})
        assert resp.status_code == 200
        body = resp.json()
        assert body['symbols'] == ['600519.SH']
        assert body['query_kind'] == 'name'
        assert body['provider'] in {'wind', 'akshare'}

