import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from gateway import config, core, dictionary, explorer, sdk_catalog
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
        'global_route': ['wind', 'ifind', 'tdx', 'choice', 'tushare', 'akshare'],
        'routes': {},
        'providers': {name: {'enabled': True, 'timeout_seconds': 10} for name in config.PROVIDERS},
        'market': {'panel_route': ['tushare', 'choice', 'ifind', 'tdx', 'wind', 'akshare']},
    }
    config.write_json(tmp_path / 'config.json', settings)
    (tmp_path / '.env').write_text('')
    # Seed cached MCP schemas for the 4 MCP providers
    schemas_dir = tmp_path / 'local/schemas'
    schemas_dir.mkdir(parents=True, exist_ok=True)
    for prov, tool_name, param_key in [
        ('wind', 'get_stock_fundamentals', 'question'),
        ('ifind', 'get_security_indicators', 'query'),
        ('tdx', 'tdx_indicator_select', 'query'),
        ('choice', 'mx_ashare_finance_data', 'query'),
    ]:
        config.write_json(schemas_dir / f'{prov}.json', {
            'tools': [{
                'name': tool_name,
                'description': 'A股财务基本面与净息差指标查询',
                'annotations': {'readOnlyHint': True},
                'inputSchema': {'type': 'object', 'properties': {param_key: {'type': 'string'}}, 'required': [param_key]},
            }]
        })
    dictionary.reload_dictionary(force=True)
    return tmp_path


def test_sdk_catalog_matching_and_param_mapping(home):
    ts_hits = sdk_catalog.search_sdk_candidates('tushare', '融资余额', scope='stock', limit=3)
    assert ts_hits and ts_hits[0]['api_name'] == 'margin_detail'
    params = sdk_catalog.build_tushare_params(ts_hits[0], {'symbol': '600519.SH', 'date': '2025-11-05'})
    assert params['ts_code'] == '600519.SH' and params['trade_date'] == '20251105'
    assert 'rzye' in params['fields']

    # Regression: long NL sentence mentioning cb_over_rate must hit cb_daily first (no hard cutoff!)
    long_hits = sdk_catalog.search_sdk_candidates(
        'tushare', '全市场可转债转股溢价率（百分数，Tushare 列为 cb_over_rate）', scope='convertible_bond', limit=3
    )
    assert long_hits and long_hits[0]['api_name'] == 'cb_daily'

    # Zero-overlap query still returns scope fallback hints instead of empty list
    fallback_hits = sdk_catalog.search_sdk_candidates('tushare', '完全不存在的神秘词汇XYZ', scope='convertible_bond', limit=5)
    assert fallback_hits and all(h.get('fallback_hint') for h in fallback_hits)

    cb_hits = sdk_catalog.search_sdk_candidates('tushare', '可转债强赎', scope='convertible_bond', limit=3)
    assert cb_hits and cb_hits[0]['api_name'] == 'cb_call'

    ak_hits = sdk_catalog.search_sdk_candidates('akshare', '可转债', scope='convertible_bond', limit=5)
    assert ak_hits and any(('cb' in h['api_name'] or 'cov' in h['api_name']) for h in ak_hits)


def test_mcp_legacy_tools_removed_and_csv_describe_returned(home, monkeypatch):
    from gateway.app import mcp
    tool_names = set(mcp._tool_manager._tools.keys())
    assert 'explore_field' in tool_names and 'save_explored_field' in tool_names
    assert 'explore_metric' not in tool_names
    assert 'save_metric' not in tool_names
    assert 'explore_data' not in tool_names

    async def fake_invoke(name, query, timeout):
        if 'sdk_probe' in query:
            rows = [{'ts_code': f'113{i:03d}.SH', 'trade_date': '20261120', 'cb_over_rate': float(10 + i)} for i in range(60)]
            return {'rows': rows, 'columns': ['ts_code', 'trade_date', 'cb_over_rate'], 'row_count': 60}
        return {'content': []}

    monkeypatch.setattr(core, 'invoke', fake_invoke)
    res = asyncio.run(explorer.explore_field(explorer.ExploreFieldRequest(
        request='随便写个长句', scope='convertible_bond', date='2026-11-20',
        providers=['tushare'], tool_overrides={'tushare': 'cb_daily'},
    )))
    r0 = res['results'][0]
    assert r0['status'] == 'ok' and r0['tool'] == 'cb_daily'
    assert r0['row_count'] == 60 and len(r0['preview']) == 10
    assert '此处仅为前 10 行预览' in r0['preview_notice']
    assert r0['describe']['row_count'] == 60
    assert r0['describe']['numeric_summary']['cb_over_rate']['min'] == 10.0
    assert r0['csv_path'] and r0['csv_path'].endswith('.csv')
    csv_lines = open(r0['csv_path'], encoding='utf-8-sig').read().splitlines()
    assert len(csv_lines) == 61  # header + 60 full rows


def test_explore_field_does_not_block_on_dictionary_and_probes_all_providers(home, monkeypatch):
    # Even when querying a field that has similar/existing names in dictionary ("营业收入"),
    # explore_field must NOT short-circuit or block on dictionary matches!
    called = []

    async def fake_invoke(name, query, timeout):
        called.append(name)
        if 'generic_call' in query:
            return {'content': [{'type': 'text', 'text': json.dumps({
                'data': {'columns': [{'name': '证券代码'}, {'name': '日期'}, {'name': '净息差(%)'}],
                         'rows': [['600036.SH', '2024-12-31', '1.98']]}
            })}]}
        if 'sdk_probe' in query:
            return {'rows': [{'ts_code': '600036.SH', 'end_date': '20241231', 'net_int_margin': 1.98}],
                    'columns': ['ts_code', 'end_date', 'net_int_margin'], 'row_count': 1}
        raise AssertionError(query)

    monkeypatch.setattr(core, 'invoke', fake_invoke)

    res = asyncio.run(explorer.explore_field(explorer.ExploreFieldRequest(
        request='营业收入', symbol='600036.SH', period='2024-12-31', stop_on_first=False,
    )))
    # Must preserve exact global_route order for single-security mode
    assert res['mode'] == 'single'
    assert res['route'] == ['wind', 'ifind', 'tdx', 'choice', 'tushare', 'akshare']
    assert [r['provider'] for r in res['results']] == res['route']
    # All 4 MCPs + Tushare SDK were probed (no early stop!)
    assert {'wind', 'ifind', 'tdx', 'choice', 'tushare'} <= set(called)
    assert res['exploration_id'].startswith('exp_')


def test_explore_field_batch_and_market_use_panel_route_and_stop_on_first(home, monkeypatch):
    called = []

    async def fake_invoke(name, query, timeout):
        called.append(name)
        if 'sdk_probe' in query:
            return {'rows': [{'ts_code': '600519.SH', 'trade_date': '20251105', 'rzye': 100.0}],
                    'columns': ['ts_code', 'trade_date', 'rzye'], 'row_count': 1}
        return {'content': [{'type': 'text', 'text': json.dumps({
            'data': {'columns': [{'name': '证券代码'}, {'name': '融资余额'}], 'rows': [['600519.SH', '100亿']]}
        })}]}

    monkeypatch.setattr(core, 'invoke', fake_invoke)

    res = asyncio.run(explorer.explore_field(explorer.ExploreFieldRequest(
        request='融资余额', symbols=['600519.SH', '000001.SZ'], date='2025-11-05', stop_on_first=True,
    )))
    assert res['mode'] == 'batch'
    assert res['route'] == ['tushare', 'choice', 'ifind', 'tdx', 'wind', 'akshare']
    # Stopped immediately after first ok provider (tushare)
    assert called == ['tushare']
    assert len(res['results']) == 1 and res['results'][0]['status'] == 'ok'


def test_save_explored_field_multi_source_and_anti_hallucination(home, monkeypatch):
    async def fake_invoke(name, query, timeout):
        if 'generic_call' in query:
            return {'content': [{'type': 'text', 'text': json.dumps({
                'data': {'columns': [{'name': '证券代码'}, {'name': '报告期'}, {'name': '净息差(%)'}],
                         'rows': [['600036.SH', '2024-12-31', '1.98']]}
            })}]}
        return {'rows': [], 'columns': [], 'row_count': 0}

    monkeypatch.setattr(core, 'invoke', fake_invoke)

    with TestClient(app) as client:
        exp = client.post('/api/explore-field', json={
            'request': '净息差', 'symbol': '600036.SH', 'period': '2024-12-31',
            'providers': ['wind', 'ifind'],
        }).json()
        exp_id = exp['exploration_id']
        assert exp['ok_providers'] == ['wind', 'ifind']

        # Anti-hallucination: saving a provider/tool not in evidence must fail
        bad = client.post('/api/explore-field/save', json={
            'exploration_id': exp_id, 'kind': 'metric', 'dataset': 'financials',
            'field_id': 'net_interest_margin', 'label': '净息差', 'unit': '%',
            'sources': [{'provider': 'choice', 'tool': 'mx_ashare_finance_data', 'columns': ['净息差']}],
        })
        assert bad.status_code == 400

        # Valid multi-source save (Wind + iFinD in one call!)
        saved = client.post('/api/explore-field/save', json={
            'exploration_id': exp_id, 'kind': 'metric', 'dataset': 'financials',
            'field_id': 'net_interest_margin', 'label': '净息差', 'unit': '%',
            'sources': [
                {'provider': 'wind', 'tool': 'get_stock_fundamentals', 'columns': ['净息差(%)'], 'source_unit': '%'},
                {'provider': 'ifind', 'tool': 'get_security_indicators', 'columns': ['净息差(%)'], 'source_unit': '%'},
            ],
        })
        assert saved.status_code == 200
        body = saved.json()
        assert body['saved'] is True and body['providers'] == ['wind', 'ifind']

        # Immediately visible in merged dictionary with BOTH providers bound!
        fields = dictionary.fields('financials')
        assert 'net_interest_margin' in fields
        assert len(fields['net_interest_margin']['sources']['wind']) == 1
        assert len(fields['net_interest_margin']['sources']['ifind']) == 1
