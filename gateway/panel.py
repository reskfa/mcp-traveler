"""Structured panel queries for multi-security / full-market data.

Two clean matrix primitives:
1. Cross-section (`query_market_cross_section`):
   Specified date × multiple symbols (or full market) × one or more fields.
   Returns index=codes, columns=fields.
2. Time-series (`query_market_time_series`):
   Date range (daily / weekly / monthly) × multiple symbols (or full market) × one field.
   Returns index=dates, columns=codes.

Every response includes the empirical 6-provider capability matrix (`PANEL_CAPABILITIES`).
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import config, core
from .helpers import MarketSymbolsRequest, query_market_symbols
from .market import (
    FIELD_META,
    _align_daily_dates,
    _calendar,
    _merge_result,
    _run_tasks,
    _scope_symbols,
    _settings,
)
from .market_provider import MARKET_FIELDS, normalize_symbol
from .tabular import PREVIEW_ROW_LIMIT, preview_notice, write_csv_and_describe

Provider = Literal['auto', 'tushare', 'akshare', 'wind', 'ifind', 'tdx', 'choice']
Scope = Literal['stock', 'convertible_bond']
Frequency = Literal['daily', 'weekly', 'monthly']

PANEL_DEFAULT_ROUTE = ['tushare', 'choice', 'ifind', 'tdx', 'wind', 'akshare']

PANEL_CAPABILITIES = {
    'tushare': {
        'tier': 0,
        'mode': 'native_full',
        'mode_label': '一次性全市场',
        'batch_limit': None,
        'cross_section': {
            'stock': 'native_full',
            'convertible_bond': 'native_full',
            'tool': 'daily / cb_daily (按 trade_date 一次取全市场)',
            'adjusted': True,
        },
        'time_series': {
            'stock': 'native_full',
            'convertible_bond': 'native_full',
            'frequencies': ['daily', 'weekly', 'monthly'],
            'native_frequencies': ['daily', 'weekly', 'monthly'],
            'tool': 'daily / cb_daily (+ 周/月频标准聚合)',
            'adjusted': True,
        },
        'summary': '按交易日单次拉全市场股票或可转债，支持前/后复权与日/周/月频，最适合全市场截面与宽面板。',
    },
    'choice': {
        'tier': 1,
        'mode': 'native_batch',
        'mode_label': '多券批量轮询',
        'batch_limit': 500,
        'cross_section': {
            'stock': 'native_batch',
            'convertible_bond': 'native_batch',
            'tool': 'mx_ashare_finance_data / mx_bond_finance_data (单次最多 500 只)',
            'adjusted': False,
        },
        'time_series': {
            'stock': 'native_batch',
            'convertible_bond': 'native_batch',
            'frequencies': ['daily', 'weekly', 'monthly'],
            'native_frequencies': ['daily'],
            'tool': 'mx_ashare_finance_data / mx_bond_finance_data (单次最多 500 只)',
            'adjusted': False,
        },
        'summary': '原生支持多券批量矩阵（单次最多 500 只股票/转债），适合中大规模多券截面与时序；不复权。',
    },
    'ifind': {
        'tier': 1,
        'mode': 'native_batch',
        'mode_label': '多券批量轮询',
        'batch_limit': 10,
        'cross_section': {
            'stock': 'native_batch',
            'convertible_bond': 'native_batch',
            'tool': 'get_security_indicators (每批约 10 只)',
            'adjusted': False,
        },
        'time_series': {
            'stock': 'native_batch',
            'convertible_bond': 'native_batch',
            'frequencies': ['daily', 'weekly', 'monthly'],
            'native_frequencies': ['daily'],
            'tool': 'get_security_indicators (每批约 10 只)',
            'adjusted': False,
        },
        'summary': '支持多券批量查询（实测 5~10 只/批稳定返回，覆盖 A 股与可转债），受账户限流与额度约束；不复权。',
    },
    'tdx': {
        'tier': 2,
        'mode': 'hybrid_batch_scalar',
        'mode_label': 'A股截面多券批量 / 时序单券轮询',
        'batch_limit': 10,
        'cross_section': {
            'stock': 'native_batch',
            'convertible_bond': 'scalar',
            'tool': 'tdx_indicator_select (A股多券批量) / tdx_kline (单券)',
            'adjusted': True,
        },
        'time_series': {
            'stock': 'scalar',
            'convertible_bond': 'scalar',
            'frequencies': ['daily', 'weekly', 'monthly'],
            'native_frequencies': ['daily', 'weekly', 'monthly'],
            'tool': 'tdx_kline (单券轮询，原生支持日/周/月 K)',
            'adjusted': True,
        },
        'summary': '单日 A 股截面支持 tdx_indicator_select 多券批量；时序与转债走 tdx_kline 单券轮询（支持复权与多周期）。',
    },
    'wind': {
        'tier': 3,
        'mode': 'scalar',
        'mode_label': '单券轮询',
        'batch_limit': 1,
        'cross_section': {
            'stock': 'scalar',
            'convertible_bond': 'scalar',
            'tool': 'get_stock_kline (单券，不支持逗号多代码)',
            'adjusted': True,
        },
        'time_series': {
            'stock': 'scalar',
            'convertible_bond': 'scalar',
            'frequencies': ['daily', 'weekly', 'monthly'],
            'native_frequencies': ['daily', 'weekly', 'monthly'],
            'tool': 'get_stock_kline (单券轮询，原生支持日/周/月 K)',
            'adjusted': True,
        },
        'summary': 'get_stock_kline 实测仅接受单只证券（支持 A 股与可转债、支持日/周/月 K 与复权），多券时自动并发单券轮询。',
    },
    'akshare': {
        'tier': 3,
        'mode': 'scalar',
        'mode_label': '单券轮询',
        'batch_limit': 1,
        'cross_section': {
            'stock': 'scalar',
            'convertible_bond': 'scalar',
            'tool': 'stock_zh_a_hist / bond_zh_hs_cov_daily (单券，免额度)',
            'adjusted': True,
        },
        'time_series': {
            'stock': 'scalar',
            'convertible_bond': 'scalar',
            'frequencies': ['daily', 'weekly', 'monthly'],
            'native_frequencies': ['daily', 'weekly', 'monthly'],
            'tool': 'stock_zh_a_hist / bond_zh_hs_cov_daily (单券轮询)',
            'adjusted': True,
        },
        'summary': '免额度但历史行情仅支持单券轮询（A 股支持复权与日/周/月频，转债日线聚合周/月频），适合中小批券池。',
    },
}


def _iso(value):
    if value and date.fromisoformat(value).isoformat() != value:
        raise ValueError('日期使用 YYYY-MM-DD')
    return value


class CrossSectionRequest(BaseModel):
    scope: Scope
    date: str
    symbols: list[str] | None = None
    fields: list[str] | None = None
    adjust: Literal['none', 'qfq', 'hfq'] = 'none'
    provider: Provider = 'auto'
    sort_by: str | None = None
    order: Literal['asc', 'desc'] = 'asc'
    top_n: int | None = Field(default=None, ge=1, le=1000)
    include_capabilities: bool = False

    @model_validator(mode='after')
    def valid(self):
        _iso(self.date)
        self.symbols = _scope_symbols(self.scope, self.symbols)
        if self.scope == 'convertible_bond' and self.adjust != 'none':
            raise ValueError('转债不接受复权参数')
        self.fields = list(dict.fromkeys(self.fields or ['close', 'pct_change']))
        bad = sorted(set(self.fields) - set(MARKET_FIELDS))
        if bad:
            raise ValueError(f'包含不支持的截面字段：{", ".join(bad)}；当前支持：{", ".join(MARKET_FIELDS)}')
        if self.sort_by and self.sort_by not in self.fields and self.sort_by != 'symbol':
            raise ValueError('sort_by 必须是已请求的字段或 symbol')
        return self


class TimeSeriesRequest(BaseModel):
    scope: Scope
    start_date: str
    end_date: str
    field: str = 'close'
    frequency: Frequency = 'daily'
    symbols: list[str] | None = None
    adjust: Literal['none', 'qfq', 'hfq'] = 'none'
    provider: Provider = 'auto'
    include_capabilities: bool = False

    @model_validator(mode='after')
    def valid(self):
        _iso(self.start_date)
        _iso(self.end_date)
        if self.start_date > self.end_date:
            raise ValueError('开始日期不能晚于结束日期')
        self.symbols = _scope_symbols(self.scope, self.symbols)
        if self.scope == 'convertible_bond' and self.adjust != 'none':
            raise ValueError('转债不接受复权参数')
        if self.field not in MARKET_FIELDS:
            raise ValueError(f'不支持的时序字段：{self.field}；当前支持：{", ".join(MARKET_FIELDS)}')
        return self


def get_panel_capabilities():
    c = config.load()
    availability = {}
    for name in config.PROVIDERS:
        p = c['providers'].get(name, {})
        availability[name] = {'enabled': bool(p.get('enabled', True)), 'available': core.available(name, p)}
    return {
        'default_route': list(c.get('market', {}).get('panel_route') or PANEL_DEFAULT_ROUTE),
        'shapes': {
            'cross_section': '截面数据：指定日期，多个券（或全市场）× 一个或多个指标；index=codes, columns=fields',
            'time_series': '时序数据：日期区间（日/周/月频），多个券（或全市场）× 单个指标；index=dates, columns=codes',
        },
        'providers': {
            name: {**PANEL_CAPABILITIES[name], **availability[name]}
            for name in config.PROVIDERS
        },
    }


def _panel_route(c, scope, kind, provider, adjust='none'):
    if provider != 'auto':
        return [provider]
    base = list((c.get('market') or {}).get('panel_route') or PANEL_DEFAULT_ROUTE)
    for name in PANEL_DEFAULT_ROUTE:
        if name not in base:
            base.append(name)
    if adjust != 'none':
        return [name for name in base if PANEL_CAPABILITIES[name][kind].get('adjusted')]
    return base


def _build_panel_tasks(name, scope, kind, symbols, start_date, end_date, trade_dates, fields, adjust, p, settings, anchor_date=None):
    cap = PANEL_CAPABILITIES[name][kind]
    if adjust != 'none' and not cap.get('adjusted'):
        return [], None
    mode = cap.get(scope, 'scalar')
    timeout = p.get('timeout_seconds', 60)
    base = {
        'scope': scope, 'start_date': start_date, 'end_date': end_date,
        'fields': fields, 'adjust': adjust, 'trade_dates': trade_dates,
        'full_market': False, 'adjustment_anchor_date': anchor_date,
    }
    tasks = []
    if mode == 'native_full':
        if adjust != 'none':
            tasks.append({'provider': name, 'call': {**base, 'operation': 'daily', 'symbols': symbols},
                          'timeout': timeout, 'strategy': 'native_full', 'symbol_count': len(symbols)})
        else:
            for day in trade_dates:
                tasks.append({'provider': name,
                              'call': {**base, 'operation': 'daily', 'start_date': day, 'end_date': day,
                                       'trade_dates': [day], 'symbols': symbols},
                              'timeout': timeout, 'strategy': 'native_full', 'symbol_count': len(symbols)})
        return tasks, 'native_full'

    if mode == 'native_batch':
        limit = PANEL_CAPABILITIES[name]['batch_limit'] or settings['batch_size']
        size = min(settings['batch_size'], limit)
        # TDX batch is cross-section only; choice/ifind support both.
        op = 'batch_daily'
        for offset in range(0, len(symbols), size):
            batch = symbols[offset:offset + size]
            tasks.append({'provider': name, 'call': {**base, 'operation': op, 'symbols': batch},
                          'timeout': timeout, 'strategy': 'native_batch',
                          'batch_index': offset // size, 'symbol_count': len(batch)})
        return tasks, 'native_batch'

    # Scalar per-symbol polling
    for symbol in symbols:
        tasks.append({'provider': name, 'call': {**base, 'operation': 'daily', 'symbols': [symbol]},
                      'timeout': timeout, 'strategy': 'scalar', 'symbol_count': 1})
    return tasks, 'scalar'


async def _resolve_symbols(scope, as_of_date, symbols, provider, attempts, notes):
    if symbols is not None:
        return symbols, {'requested_count': len(symbols), 'resolved_count': len(symbols), 'complete': True, 'source': 'explicit'}
    sym_prov = provider if provider != 'auto' and PANEL_CAPABILITIES.get(provider) else 'auto'
    # Always use auto if explicit provider doesn't support full-market universe (e.g., wind or tdx CB).
    from .helpers import HELPER_CAPABILITIES
    if sym_prov != 'auto' and HELPER_CAPABILITIES['market_symbols']['scopes'][scope].get(sym_prov, {}).get('support') == 'unsupported':
        sym_prov = 'auto'
    res = await query_market_symbols(MarketSymbolsRequest(scope=scope, as_of_date=as_of_date, include_delisted=bool(as_of_date), provider=sym_prov))
    attempts.extend(res.get('attempts') or [])
    notes.extend(res.get('notes') or [])
    if not res['symbols']:
        raise core.QueryFailure(attempts)
    return res['symbols'], {'requested_count': res['count'], 'resolved_count': res['count'],
                           'complete': res['complete'], 'source': res['provider']}


def _resample_series(rows_by_key, symbols, field, frequency):
    """Resample daily (symbol, date) -> row mapping to weekly or monthly period-end dates."""
    if frequency == 'daily':
        dates = sorted({d for _, d in rows_by_key})
        grid = {d: {sym: rows_by_key.get((sym, d), {}).get(field) for sym in symbols} for d in dates}
        return dates, grid

    def bucket_key(day_str):
        dt = date.fromisoformat(day_str)
        if frequency == 'weekly':
            iso = dt.isocalendar()
            return (iso.year, iso.week)
        return (dt.year, dt.month)

    # Group per symbol per bucket, keeping chronological order
    per_symbol_buckets = defaultdict(lambda: defaultdict(list))
    bucket_last_date = {}
    for (sym, d), row in rows_by_key.items():
        bk = bucket_key(d)
        per_symbol_buckets[sym][bk].append((d, row))
        if bk not in bucket_last_date or d > bucket_last_date[bk]:
            bucket_last_date[bk] = d

    ordered_buckets = sorted(bucket_last_date.keys())
    dates = [bucket_last_date[bk] for bk in ordered_buckets]
    grid = {}
    for bk, period_date in zip(ordered_buckets, dates):
        grid[period_date] = {}
        for sym in symbols:
            entries = sorted(per_symbol_buckets[sym].get(bk, []), key=lambda x: x[0])
            if not entries:
                grid[period_date][sym] = None
                continue
            vals = [r.get(field) for _, r in entries if r.get(field) is not None]
            if not vals:
                grid[period_date][sym] = None
            elif field == 'open':
                grid[period_date][sym] = vals[0]
            elif field == 'high':
                grid[period_date][sym] = max(vals)
            elif field == 'low':
                grid[period_date][sym] = min(vals)
            elif field == 'close':
                grid[period_date][sym] = vals[-1]
            elif field in {'volume', 'amount', 'turnover_rate'}:
                grid[period_date][sym] = sum(vals)
            elif field == 'pre_close':
                grid[period_date][sym] = vals[0]
            elif field == 'pct_change':
                # Compound daily pct_change within period when raw closes aren't fetched
                factor = 1.0
                for v in vals:
                    factor *= (1.0 + v / 100.0)
                grid[period_date][sym] = (factor - 1.0) * 100.0
            else:
                grid[period_date][sym] = vals[-1]
    return dates, grid


def _store_panel(kind, request, payload):
    rid = uuid.uuid4().hex
    folder = config.ROOT / 'local/results'
    folder.mkdir(parents=True, exist_ok=True)
    rows = payload.get('data') or []
    csv_cols = ([payload.get('index_name', 'symbol')] + list(payload.get('columns') or [])) if rows else None
    csv_path, desc = write_csv_and_describe(rows, folder / f'{rid}.csv', csv_cols)
    answer = {
        **{k: v for k, v in payload.items() if k != 'raws'},
        'id': rid,
        'dataset': f'panel_{kind}',
        'query': request.model_dump(),
        'requested_provider': request.provider,
        'fetched_at': core.now(),
        'describe': desc,
        'csv_path': csv_path,
        'raw_path': str(folder / f'{rid}.raw.json'),
        'data_path': str(folder / f'{rid}.json'),
        'download_url': f'/api/results/{rid}/csv',
        'raw_url': f'/api/results/{rid}/raw',
    }
    config.write_json(folder / f'{rid}.raw.json', config.redact(payload.get('raws', [])))
    config.write_json(folder / f'{rid}.json', config.redact(answer))
    if len(rows) > PREVIEW_ROW_LIMIT:
        trimmed = dict(answer)
        trimmed['data'] = rows[:PREVIEW_ROW_LIMIT]
        if 'index' in trimmed:
            trimmed['index'] = trimmed['index'][:PREVIEW_ROW_LIMIT]
        if 'matrix' in trimmed and isinstance(trimmed['matrix'], dict):
            trimmed['matrix'] = {
                **trimmed['matrix'],
                'index': trimmed['matrix']['index'][:PREVIEW_ROW_LIMIT],
                'rows': trimmed['matrix']['rows'][:PREVIEW_ROW_LIMIT],
            }
        if 'field_sources' in trimmed and isinstance(trimmed['field_sources'], list):
            trimmed['field_sources'] = trimmed['field_sources'][:PREVIEW_ROW_LIMIT]
        trimmed['preview_only'] = True
        trimmed['preview_row_count'] = PREVIEW_ROW_LIMIT
        trimmed['full_row_count'] = len(rows)
        trimmed['preview_notice'] = preview_notice(PREVIEW_ROW_LIMIT, len(rows), csv_path)
        return trimmed
    return answer


async def _fetch_panel_grid(scope, kind, symbols, start_date, end_date, trade_dates, fields, adjust, provider, c, deadline, settings):
    attempts = []
    raws = []
    notes = []
    rows = {}
    provenance = {}
    allowed = set(symbols)
    used_modes = []
    anchor_date = None

    if scope == 'stock' and adjust == 'qfq':
        today = date.today().isoformat()
        if end_date > today:
            raise ValueError('前复权需要截至当前的完整因子历史，end_date 不能晚于今天')
        if today > end_date:
            anchor_cal, a_att, a_raw, _ = await _calendar(end_date, today, provider, c, deadline, settings)
            attempts.extend(a_att)
            raws.extend(a_raw)
        else:
            anchor_cal = trade_dates
        anchor_dates = [d for d in anchor_cal if d <= today]
        if anchor_dates:
            anchor_date = anchor_dates[-1]

    class _DummyReq:
        def __init__(self):
            self.start_date = start_date
            self.end_date = end_date
            self.fields = fields
            self.adjust = adjust

    dummy = _DummyReq()
    for name in _panel_route(c, scope, kind, provider, adjust=adjust):
        p = c['providers'][name]
        if not core.available(name, p):
            attempts.append({'provider': name, 'operation': kind, 'status': 'skipped', 'error': '未启用或配置未完成'})
            continue
        tasks, mode = _build_panel_tasks(name, scope, kind, symbols, start_date, end_date, trade_dates, fields, adjust, p, settings, anchor_date)
        if not tasks:
            attempts.append({'provider': name, 'operation': kind, 'status': 'unsupported', 'error': '不支持当前复权或取数策略'})
            continue
        outcomes = await _run_tasks(tasks, deadline, settings)
        ok_any = False
        for outcome in outcomes:
            task = outcome['task']
            att = {'provider': name, 'operation': kind, 'strategy': task['strategy'], 'status': outcome['status'],
                   'symbol_count': task.get('symbol_count', 0)}
            if outcome['status'] == 'success':
                ok_any = True
                res = outcome['result']
                att['row_count'] = len(res.get('data') or [])
                _merge_result(rows, provenance, res, name, dummy, allowed)
                raws.append({'provider': name, 'query': task['call'], 'raw': res.get('raw')})
                notes.extend(res.get('notes') or [])
            else:
                att['error'] = outcome.get('error')
            attempts.append(att)
        if ok_any:
            used_modes.append({'provider': name, 'mode': mode})
        missing_cells = any(row.get(f) is None for row in rows.values() for f in fields)
        symbols_seen = {sym for sym, _ in rows}
        if provider != 'auto' or (ok_any and not missing_cells and set(symbols) <= symbols_seen):
            break

    if not rows and symbols and trade_dates and any(a['status'] in {'error', 'timeout'} for a in attempts):
        raise core.QueryFailure(attempts)
    return rows, provenance, attempts, raws, notes, used_modes


async def query_market_cross_section(request: CrossSectionRequest):
    request = CrossSectionRequest.model_validate(request.model_dump())
    c = config.load()
    settings = _settings(c)
    start = time.monotonic()
    deadline = start + settings['max_elapsed_seconds']
    cal_start = (date.fromisoformat(request.date) - timedelta(days=31)).isoformat()
    cal_end = (date.fromisoformat(request.date) + timedelta(days=31)).isoformat()
    all_dates, cal_att, cal_raw, cal_prov = await _calendar(cal_start, cal_end, request.provider, c, deadline, settings)
    actual_date, _, trade_dates = _align_daily_dates(request.date, request.date, all_dates)

    attempts = list(cal_att)
    raws = list(cal_raw)
    notes = [f'交易日历来源：{cal_prov}']
    if actual_date != request.date:
        notes.append(f'非交易日自动对齐：{request.date} → {actual_date}')

    symbols, universe_info = await _resolve_symbols(request.scope, actual_date, request.symbols, request.provider, attempts, notes)
    if len(symbols) > settings['max_symbols']:
        raise ValueError(f'证券数量超过 market.max_symbols={settings["max_symbols"]}')

    rows_map, prov_map, fetch_att, fetch_raw, fetch_notes, used_modes = await _fetch_panel_grid(
        request.scope, 'cross_section', symbols, actual_date, actual_date, trade_dates,
        request.fields, request.adjust, request.provider, c, deadline, settings,
    )
    attempts.extend(fetch_att)
    raws.extend(fetch_raw)
    notes.extend(fetch_notes)

    # Build index=codes, columns=fields
    ordered_symbols = list(symbols)
    records_list = []
    sources_list = []
    for sym in ordered_symbols:
        row = rows_map.get((sym, actual_date)) or {f: None for f in request.fields}
        src = prov_map.get((sym, actual_date)) or {}
        records_list.append({'symbol': sym, **{f: row.get(f) for f in request.fields}})
        sources_list.append(src)

    if request.sort_by and request.sort_by != 'symbol':
        fld = request.sort_by
        paired = list(zip(records_list, sources_list))
        present = [p for p in paired if p[0].get(fld) is not None]
        missing = [p for p in paired if p[0].get(fld) is None]
        present.sort(key=lambda p: p[0]['symbol'])
        present.sort(key=lambda p: p[0][fld], reverse=request.order == 'desc')
        paired = present + missing
        records_list = [p[0] for p in paired]
        sources_list = [p[1] for p in paired]
    elif request.sort_by == 'symbol' and request.order == 'desc':
        records_list.reverse()
        sources_list.reverse()

    full_count = len(records_list)
    if request.top_n:
        records_list = records_list[:request.top_n]
        sources_list = sources_list[:request.top_n]

    index = [r['symbol'] for r in records_list]
    columns = list(request.fields)
    matrix_rows = [[r.get(f) for f in columns] for r in records_list]
    providers = list(dict.fromkeys(src for row_src in sources_list for src in row_src.values()))
    non_empty_rows = sum(1 for r in records_list if any(r.get(f) is not None for f in columns))
    missing_cells = sum(1 for r in records_list for f in columns if r.get(f) is None)
    complete = bool(records_list) and missing_cells == 0 and universe_info['complete']

    payload = {
        'status': 'success' if complete else ('empty' if non_empty_rows == 0 else 'partial'),
        'complete': complete,
        'shape': 'cross_section',
        'scope': request.scope,
        'index_name': 'symbol',
        'index': index,
        'columns': columns,
        'matrix': {'index_name': 'symbol', 'index': index, 'columns': columns, 'rows': matrix_rows},
        'data': records_list,
        'field_sources': sources_list,
        'row_count': len(records_list),
        'full_row_count': full_count,
        'provider': providers[0] if len(providers) == 1 else ('mixed' if providers else None),
        'providers': providers,
        'execution_modes': used_modes,
        'window': {'requested_date': request.date, 'actual_date': actual_date, 'calendar_provider': cal_prov},
        'universe': universe_info,
        'coverage': {
            'status': 'complete' if complete else ('partial' if non_empty_rows else 'empty'),
            'expected_symbols': len(symbols),
            'returned_symbols': non_empty_rows,
            'missing_cells': missing_cells,
        },
        'fields': {f: {'label': FIELD_META.get(f, (f, ''))[0], 'unit': FIELD_META.get(f, (f, ''))[1]} for f in columns},
        **({'capabilities': get_panel_capabilities()['providers']} if request.include_capabilities else {}),
        'attempts': attempts,
        'raws': raws,
        'notes': list(dict.fromkeys(notes)),
        'elapsed_seconds': round(time.monotonic() - start, 2),
    }
    return _store_panel('cross_section', request, payload)


async def query_market_time_series(request: TimeSeriesRequest):
    request = TimeSeriesRequest.model_validate(request.model_dump())
    c = config.load()
    settings = _settings(c)
    start = time.monotonic()
    deadline = start + settings['max_elapsed_seconds']
    cal_start = (date.fromisoformat(request.start_date) - timedelta(days=31)).isoformat()
    cal_end = (date.fromisoformat(request.end_date) + timedelta(days=31)).isoformat()
    all_dates, cal_att, cal_raw, cal_prov = await _calendar(cal_start, cal_end, request.provider, c, deadline, settings)
    actual_start, actual_end, trade_dates = _align_daily_dates(request.start_date, request.end_date, all_dates)

    attempts = list(cal_att)
    raws = list(cal_raw)
    notes = [f'交易日历来源：{cal_prov}']
    if (actual_start, actual_end) != (request.start_date, request.end_date):
        notes.append(f'非交易日自动对齐：{request.start_date}至{request.end_date} → {actual_start}至{actual_end}')
    if request.frequency != 'daily':
        notes.append(f'频率 {request.frequency} 由区间日线按标准 OHLCV 规则聚合（以各周期末交易日为索引）。')

    symbols, universe_info = await _resolve_symbols(request.scope, actual_end, request.symbols, request.provider, attempts, notes)
    if len(symbols) > settings['max_symbols']:
        raise ValueError(f'证券数量超过 market.max_symbols={settings["max_symbols"]}')
    if len(symbols) * max(1, len(trade_dates)) > settings['max_symbol_days']:
        raise ValueError(f'证券×交易日超过 market.max_symbol_days={settings["max_symbol_days"]}')

    rows_map, prov_map, fetch_att, fetch_raw, fetch_notes, used_modes = await _fetch_panel_grid(
        request.scope, 'time_series', symbols, actual_start, actual_end, trade_dates,
        [request.field], request.adjust, request.provider, c, deadline, settings,
    )
    attempts.extend(fetch_att)
    raws.extend(fetch_raw)
    notes.extend(fetch_notes)

    index_dates, grid = _resample_series(rows_map, symbols, request.field, request.frequency)
    columns = list(symbols)
    records_list = []
    matrix_rows = []
    for d in index_dates:
        row_dict = {'date': d}
        row_vals = []
        for sym in columns:
            val = grid[d].get(sym)
            row_dict[sym] = val
            row_vals.append(val)
        records_list.append(row_dict)
        matrix_rows.append(row_vals)

    providers = list(dict.fromkeys(src for row_src in prov_map.values() for src in row_src.values()))
    total_cells = len(index_dates) * len(columns)
    missing_cells = sum(1 for row_vals in matrix_rows for v in row_vals if v is None)
    complete = bool(index_dates) and missing_cells == 0 and universe_info['complete']

    payload = {
        'status': 'success' if complete else ('empty' if total_cells == 0 or missing_cells == total_cells else 'partial'),
        'complete': complete,
        'shape': 'time_series',
        'scope': request.scope,
        'field': request.field,
        'field_meta': {'label': FIELD_META.get(request.field, (request.field, ''))[0],
                       'unit': FIELD_META.get(request.field, (request.field, ''))[1]},
        'frequency': request.frequency,
        'index_name': 'date',
        'index': index_dates,
        'columns': columns,
        'matrix': {'index_name': 'date', 'index': index_dates, 'columns': columns, 'rows': matrix_rows},
        'data': records_list,
        'row_count': len(records_list),
        'provider': providers[0] if len(providers) == 1 else ('mixed' if providers else None),
        'providers': providers,
        'execution_modes': used_modes,
        'window': {
            'requested_start': request.start_date, 'requested_end': request.end_date,
            'actual_start': actual_start, 'actual_end': actual_end,
            'frequency': request.frequency, 'dates': index_dates, 'calendar_provider': cal_prov,
        },
        'universe': universe_info,
        'coverage': {
            'status': 'complete' if complete else ('partial' if total_cells > missing_cells else 'empty'),
            'dates_count': len(index_dates),
            'symbols_count': len(columns),
            'total_cells': total_cells,
            'missing_cells': missing_cells,
        },
        'fields': {sym: {'label': sym, 'unit': FIELD_META.get(request.field, ('', ''))[1]} for sym in columns},
        **({'capabilities': get_panel_capabilities()['providers']} if request.include_capabilities else {}),
        'attempts': attempts,
        'raws': raws,
        'notes': list(dict.fromkeys(notes)),
        'elapsed_seconds': round(time.monotonic() - start, 2),
    }
    return _store_panel('time_series', request, payload)
