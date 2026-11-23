"""Standalone helper functions (辅助函数) for users and Agents.

Provides multi-provider, non-Tushare-locked utilities:
1. trade_calendar: A-share trading calendar across all 6 providers
2. market_symbols: Full-market A-share / convertible bond symbol lists
3. helper_capabilities: Verified capability matrix across all 6 providers
"""
from __future__ import annotations

import time
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import config, core
from .market import _filter_universe, _run_tasks, _settings
from .tabular import PREVIEW_ROW_LIMIT, preview_notice, write_csv_and_describe

Provider = Literal['auto', 'tushare', 'akshare', 'wind', 'ifind', 'tdx', 'choice']
Scope = Literal['stock', 'convertible_bond']
SearchScope = Literal['all', 'stock', 'convertible_bond']
Exchange = Literal['SSE', 'SZSE']

HELPER_CAPABILITIES = {
    'trade_calendar': {
        'label': '交易日历',
        'description': '拉取指定日期区间内的 A 股交易日列表（YYYY-MM-DD），支持多源自动路由与单源指定。',
        'providers': {
            'tushare': {
                'support': 'native',
                'tier': 0,
                'tool': 'trade_cal',
                'summary': '原生交易所日历，支持历史与已发布未来交易日',
            },
            'akshare': {
                'support': 'native',
                'tier': 0,
                'tool': 'tool_trade_date_hist_sina',
                'summary': '原生新浪 A 股交易日历（1990 至今及当年已公布交易日），免额度',
            },
            'wind': {
                'support': 'index_derived',
                'tier': 1,
                'tool': 'get_stock_kline (000001.SH)',
                'summary': '通过上证指数日 K 线推导历史交易日（不含未来节假日）',
            },
            'tdx': {
                'support': 'index_derived',
                'tier': 1,
                'tool': 'tdx_kline (000001.SH)',
                'summary': '通过上证指数日 K 线分页推导历史交易日（不含未来节假日）',
            },
            'choice': {
                'support': 'index_derived',
                'tier': 1,
                'tool': 'mx_index_block_finance_data (000001.SH)',
                'summary': '通过上证指数每日收盘价推导历史交易日（不含未来节假日）',
            },
            'ifind': {
                'support': 'index_derived',
                'tier': 1,
                'tool': 'get_security_indicators (000001.SH 成交量)',
                'summary': '通过上证指数非空成交量日期推导历史交易日（过滤周末填充行）',
            },
        },
    },
    'market_symbols': {
        'label': '全市场证券代码',
        'description': '拉取全市场 A 股（stock）或可转债（convertible_bond）代码清单及简称、上市/退市日期。',
        'scopes': {
            'stock': {
                'tushare': {
                    'support': 'full_historical',
                    'tier': 0,
                    'tool': 'stock_basic (L/D/P)',
                    'summary': '完整在市+暂停+退市 A 股，含上市/退市日期，支持历史时点',
                },
                'akshare': {
                    'support': 'full_historical',
                    'tier': 0,
                    'tool': 'stock_info_{sh,sz,bj}_name_code + {sh,sz}_delist',
                    'summary': '沪深京全量在市 + 沪深退市，含上市/退市日期（不含北交所历史退市）',
                },
                'tdx': {
                    'support': 'full_current',
                    'tier': 1,
                    'tool': 'tdx_screener (分页 500/页)',
                    'summary': '分页拉取全部当前在市 A 股（约 5572 只），无上市/退市日期',
                },
                'choice': {
                    'support': 'screener',
                    'tier': 1,
                    'tool': 'mx_stocks_screener',
                    'summary': '自然语言全市场筛选，受上游单次返回上限与额度影响',
                },
                'ifind': {
                    'support': 'full_current',
                    'tier': 1,
                    'tool': 'search_securities (分片 CSV 导出)',
                    'summary': '按沪深京 8 分片拉取并下载官方 CSV 合并全部当前在市 A 股（含上市日期，不含已退市）',
                },
                'wind': {
                    'support': 'unsupported',
                    'tier': 9,
                    'tool': 'search_stocks',
                    'summary': '单次硬上限 100 条且无正式分页，不具备全市场名单能力（自动路由跳过）',
                },
            },
            'convertible_bond': {
                'tushare': {
                    'support': 'full_historical',
                    'tier': 0,
                    'tool': 'cb_basic',
                    'summary': '完整在市与历史已摘牌可转债，含上市/摘牌日期，支持历史时点',
                },
                'akshare': {
                    'support': 'full_current',
                    'tier': 0,
                    'tool': 'bond_zh_cov / bond_zh_cov_info_ths',
                    'summary': '覆盖千余只发行与在市可转债（含上市日期，无摘牌日期）',
                },
                'ifind': {
                    'support': 'full_current',
                    'tier': 0,
                    'tool': 'market_statistics (CSV 导出)',
                    'summary': '单次调用并下载官方 CSV 导出全部可转债（约 1136 条，含上市日期）',
                },
                'choice': {
                    'support': 'screener',
                    'tier': 1,
                    'tool': 'mx_stocks_screener',
                    'summary': '自然语言全市场可转债筛选，受上游返回上限与额度影响',
                },
                'wind': {
                    'support': 'unsupported',
                    'tier': 9,
                    'tool': 'search_stocks',
                    'summary': '单次硬上限 100 条且无正式分页，不具备全市场名单能力（自动路由跳过）',
                },
                'tdx': {
                    'support': 'unsupported',
                    'tier': 9,
                    'tool': 'tdx_screener',
                    'summary': '不支持可转债全市场筛选（仅支持 A 股/基金/指数/港股）',
                },
            },
        },
    },
    'search_securities': {
        'label': '搜索证券',
        'description': '按名称、简称或代码双向搜索 A 股与可转债（名称搜代码 / 代码搜名称）。',
        'providers': {
            'tushare': {
                'support': 'native',
                'tier': 0,
                'tool': 'stock_basic / cb_basic',
                'summary': '全量股票与可转债目录精准/模糊双向检索，支持含已退市证券',
            },
            'akshare': {
                'support': 'native',
                'tier': 0,
                'tool': 'stock_info_a_code_name / bond_zh_cov',
                'summary': '免额度全量股票与可转债双向检索，支持含沪深退市股票',
            },
            'wind': {
                'support': 'native',
                'tier': 0,
                'tool': 'search_stocks',
                'summary': '原生支持 A 股与可转债的名称↔代码双向检索',
            },
            'tdx': {
                'support': 'stock_only',
                'tier': 1,
                'tool': 'tdx_lookup_stock',
                'summary': '支持 A 股名称、代码与拼音/别名检索；不支持可转债',
            },
            'ifind': {
                'support': 'native',
                'tier': 1,
                'tool': 'search_securities / get_security_indicators',
                'summary': '支持 A 股与可转债双向检索，受账户额度限制',
            },
            'choice': {
                'support': 'native',
                'tier': 1,
                'tool': 'mx_stocks_screener / mx_bond_finance_data',
                'summary': '支持 A 股与可转债双向检索，受账户积分限制',
            },
        },
    },
}


def _iso(value):
    if value and date.fromisoformat(value).isoformat() != value:
        raise ValueError('日期使用 YYYY-MM-DD')
    return value


class TradeCalendarRequest(BaseModel):
    start_date: str
    end_date: str
    exchange: Exchange = 'SSE'
    provider: Provider = 'auto'

    @model_validator(mode='after')
    def valid(self):
        _iso(self.start_date)
        _iso(self.end_date)
        if self.start_date > self.end_date:
            raise ValueError('开始日期不能晚于结束日期')
        return self


class MarketSymbolsRequest(BaseModel):
    scope: Scope
    as_of_date: str | None = None
    include_delisted: bool = False
    provider: Provider = 'auto'

    @model_validator(mode='after')
    def valid(self):
        _iso(self.as_of_date)
        return self


class SearchSecuritiesRequest(BaseModel):
    query: str | None = None
    queries: list[str] | None = None
    scope: SearchScope = 'all'
    limit: int = Field(default=20, ge=1, le=100)
    include_delisted: bool = False
    provider: Provider = 'auto'

    @model_validator(mode='after')
    def valid(self):
        if self.queries is not None:
            cleaned = [str(q).strip() for q in self.queries if str(q).strip()]
            if not cleaned:
                raise ValueError('queries 不能为空列表')
            if len(cleaned) > 50:
                raise ValueError('单次批量搜索最多支持 50 个关键词')
            self.queries = list(dict.fromkeys(cleaned))
            if not self.query:
                self.query = self.queries[0]
        else:
            self.query = (self.query or '').strip()
            if not self.query:
                raise ValueError('搜索关键词不能为空（可输入证券名称、简称或代码，或传 queries 批量搜索）')
            if len(self.query) > 80:
                raise ValueError('搜索关键词过长')
        return self


def _calendar_route(c, provider):
    if provider != 'auto':
        return [provider]
    base = list(c.get('global_route') or config.PROVIDERS)
    for name in config.PROVIDERS:
        if name not in base:
            base.append(name)
    meta = HELPER_CAPABILITIES['trade_calendar']['providers']
    return sorted(base, key=lambda name: meta.get(name, {}).get('tier', 9))


def _symbols_route(c, scope, provider, need_history=False):
    if provider != 'auto':
        return [provider]
    base = list(c.get('global_route') or config.PROVIDERS)
    for name in config.PROVIDERS:
        if name not in base:
            base.append(name)
    meta = HELPER_CAPABILITIES['market_symbols']['scopes'][scope]

    def rank(name):
        info = meta.get(name, {})
        support = info.get('support', 'unsupported')
        if support == 'unsupported':
            return 9
        if need_history:
            return {'full_historical': 0, 'screener': 1, 'full_current': 2, 'partial': 3}.get(support, 9)
        return info.get('tier', 9)

    return [name for name in sorted(base, key=rank) if meta.get(name, {}).get('support') != 'unsupported']


def _store_helper(helper_name, request, payload):
    rid = uuid.uuid4().hex
    folder = config.ROOT / 'local/results'
    folder.mkdir(parents=True, exist_ok=True)
    rows = payload.get('data') or []
    csv_cols = list((payload.get('fields') or {}).keys()) or None
    csv_path, desc = write_csv_and_describe(rows, folder / f'{rid}.csv', csv_cols)
    answer = {
        **{k: v for k, v in payload.items() if k != 'raws'},
        'id': rid,
        'dataset': f'helper_{helper_name}',
        'helper': helper_name,
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
    if helper_name == 'market_symbols' and len(rows) > PREVIEW_ROW_LIMIT:
        trimmed = dict(answer)
        trimmed['data'] = rows[:PREVIEW_ROW_LIMIT]
        trimmed['symbols_preview'] = (trimmed.get('symbols') or [])[:PREVIEW_ROW_LIMIT]
        trimmed['symbols'] = trimmed['symbols_preview']
        trimmed['symbols_truncated'] = True
        if 'field_sources' in trimmed and isinstance(trimmed['field_sources'], list):
            trimmed['field_sources'] = trimmed['field_sources'][:PREVIEW_ROW_LIMIT]
        trimmed['preview_only'] = True
        trimmed['preview_row_count'] = PREVIEW_ROW_LIMIT
        trimmed['full_row_count'] = len(rows)
        trimmed['preview_notice'] = preview_notice(PREVIEW_ROW_LIMIT, len(rows), csv_path)
        return trimmed
    return answer


def get_helper_capabilities():
    c = config.load()
    providers = {}
    for name in config.PROVIDERS:
        p = c['providers'].get(name, {})
        providers[name] = {'enabled': bool(p.get('enabled', True)), 'available': core.available(name, p)}
    return {
        'helpers': HELPER_CAPABILITIES,
        'providers': providers,
    }


async def query_trade_calendar(request: TradeCalendarRequest):
    request = TradeCalendarRequest.model_validate(request.model_dump())
    c = config.load()
    settings = _settings(c)
    start = time.monotonic()
    deadline = start + settings['max_elapsed_seconds']
    attempts = []
    raws = []
    best = None

    for name in _calendar_route(c, request.provider):
        p = c['providers'][name]
        if not core.available(name, p):
            attempts.append({'provider': name, 'operation': 'calendar', 'status': 'skipped', 'error': '未启用或配置未完成'})
            continue
        call = {'operation': 'calendar', 'scope': 'stock', 'exchange': request.exchange,
                'start_date': request.start_date, 'end_date': request.end_date}
        outcome = (await _run_tasks([{'provider': name, 'call': call, 'timeout': p.get('timeout_seconds', 60)}], deadline, settings))[0]
        if outcome['status'] != 'success':
            attempts.append({'provider': name, 'operation': 'calendar', 'status': outcome['status'], 'error': outcome.get('error')})
            continue
        result = outcome['result']
        dates = sorted(dict.fromkeys(result.get('dates') or []))
        rows = result.get('data') or [{'date': d, 'exchange': request.exchange, 'is_open': True, 'pretrade_date': None} for d in dates]
        mode = result.get('calendar_mode') or HELPER_CAPABILITIES['trade_calendar']['providers'].get(name, {}).get('support', 'native')
        complete = bool(result.get('complete', True))
        raws.append({'provider': name, 'query': call, 'raw': result.get('raw')})
        attempts.append({
            'provider': name, 'operation': 'calendar', 'strategy': result.get('strategy', mode),
            'calendar_mode': mode, 'status': 'success' if complete else 'partial', 'row_count': len(dates),
        })
        best = {
            'provider': name,
            'providers': [name],
            'upstream': result.get('upstream', name),
            'calendar_mode': mode,
            'complete': complete,
            'dates': dates,
            'data': rows,
            'notes': list(result.get('notes') or []),
        }
        if complete or request.provider != 'auto':
            break

    if best is None:
        if any(a['status'] in {'error', 'timeout'} for a in attempts):
            raise core.QueryFailure(attempts)
        raise ValueError('当前没有可用的数据源拉取交易日历，请启用至少一个已配置来源（如 AKShare / Tushare / MCP）。')

    data = best['data']
    payload = {
        'status': 'success' if best['complete'] else ('empty' if not data else 'partial'),
        'complete': best['complete'],
        'provider': best['provider'],
        'providers': best['providers'],
        'upstream': best['upstream'],
        'calendar_mode': best['calendar_mode'],
        'exchange': request.exchange,
        'start_date': request.start_date,
        'end_date': request.end_date,
        'count': len(best['dates']),
        'row_count': len(data),
        'dates': best['dates'],
        'data': data,
        'field_sources': [{'date': best['provider'], 'exchange': best['provider'], 'is_open': best['provider']} for _ in data],
        'fields': {
            'date': {'label': '交易日', 'unit': ''},
            'exchange': {'label': '交易所', 'unit': ''},
            'is_open': {'label': '是否开市', 'unit': ''},
            'pretrade_date': {'label': '上一交易日', 'unit': ''},
        },
        'attempts': attempts,
        'raws': raws,
        'notes': list(dict.fromkeys(best['notes'])),
        'elapsed_seconds': round(time.monotonic() - start, 2),
    }
    return _store_helper('trade_calendar', request, payload)


async def query_market_symbols(request: MarketSymbolsRequest):
    request = MarketSymbolsRequest.model_validate(request.model_dump())
    c = config.load()
    settings = _settings(c)
    start = time.monotonic()
    deadline = start + settings['max_elapsed_seconds']
    need_history = bool(request.as_of_date or request.include_delisted)
    attempts = []
    raws = []
    best = None
    meta_scope = HELPER_CAPABILITIES['market_symbols']['scopes'][request.scope]

    for name in _symbols_route(c, request.scope, request.provider, need_history=need_history):
        p = c['providers'][name]
        if not core.available(name, p):
            attempts.append({'provider': name, 'operation': 'universe', 'status': 'skipped', 'error': '未启用或配置未完成'})
            continue
        if meta_scope.get(name, {}).get('support') == 'unsupported':
            attempts.append({'provider': name, 'operation': 'universe', 'status': 'unsupported',
                             'error': meta_scope[name]['summary']})
            continue
        call = {'operation': 'universe', 'scope': request.scope, 'include_delisted': need_history}
        outcome = (await _run_tasks([{'provider': name, 'call': call, 'timeout': p.get('timeout_seconds', 60)}], deadline, settings))[0]
        if outcome['status'] != 'success':
            attempts.append({'provider': name, 'operation': 'universe', 'status': outcome['status'], 'error': outcome.get('error')})
            continue
        result = outcome['result']
        raws.append({'provider': name, 'query': call, 'raw': result.get('raw')})
        raw_rows = list(result.get('data') or [])
        truncated = bool(result.get('truncated'))
        universe_complete = bool(result.get('universe_complete'))
        current_complete = bool(result.get('current_complete', universe_complete))
        is_full = universe_complete if need_history else current_complete
        attempts.append({
            'provider': name, 'operation': 'universe', 'strategy': result.get('strategy'),
            'status': 'success' if is_full else 'partial', 'row_count': len(raw_rows),
            'source_reported_rows': result.get('source_reported_rows', len(raw_rows)),
        })
        candidate = {
            'provider': name,
            'upstream': result.get('upstream', name),
            'rows': raw_rows,
            'truncated': truncated,
            'universe_complete': universe_complete,
            'current_complete': current_complete,
            'source_reported_rows': result.get('source_reported_rows', len(raw_rows)),
            'conflicts': list(result.get('conflicts') or []),
            'notes': list(result.get('notes') or []),
            'is_full': is_full,
        }
        if best is None or (candidate['is_full'] and not best['is_full']) or (
            not best['is_full'] and len(candidate['rows']) > len(best['rows'])
        ):
            best = candidate
        if candidate['is_full'] or request.provider != 'auto':
            break

    if best is None:
        if any(a['status'] in {'error', 'timeout'} for a in attempts):
            raise core.QueryFailure(attempts)
        raise ValueError(f'当前没有可用的数据源拉取 {request.scope} 代码名单。')

    rows = best['rows']
    notes = list(best['notes'])
    unknown_dates = []
    if request.as_of_date:
        selected, unknown_dates = _filter_universe(rows, request.as_of_date, request.as_of_date)
        allowed = set(selected)
        rows = [row for row in rows if row['symbol'] in allowed]
        if unknown_dates:
            notes.append(f'有 {len(unknown_dates)} 只证券缺少上市日期，无法按历史时点 {request.as_of_date} 判定。')
    elif not request.include_delisted:
        rows = [row for row in rows if not row.get('delist_date')]

    data = []
    for row in sorted(rows, key=lambda item: item['symbol']):
        symbol = row['symbol']
        exchange = symbol.rsplit('.', 1)[1] if '.' in symbol else ''
        status = 'delisted' if row.get('delist_date') else 'listed'
        data.append({
            'symbol': symbol,
            'name': row.get('name'),
            'exchange': exchange,
            'list_date': row.get('list_date'),
            'delist_date': row.get('delist_date'),
            'status': status,
        })

    symbols = [row['symbol'] for row in data]
    complete = bool(best['is_full']) and not best['truncated'] and not best['conflicts'] and not unknown_dates and bool(data)
    if request.as_of_date and not best['universe_complete']:
        complete = False
    prov = best['provider']
    payload = {
        'status': 'success' if complete else ('empty' if not data else 'partial'),
        'complete': complete,
        'scope': request.scope,
        'as_of_date': request.as_of_date,
        'include_delisted': request.include_delisted,
        'provider': prov,
        'providers': [prov],
        'upstream': best['upstream'],
        'count': len(symbols),
        'row_count': len(data),
        'symbols': symbols,
        'data': data,
        'field_sources': [{k: prov for k in ('name', 'list_date', 'delist_date') if row.get(k) is not None} for row in data],
        'fields': {
            'symbol': {'label': '证券代码', 'unit': ''},
            'name': {'label': '证券简称', 'unit': ''},
            'exchange': {'label': '交易所', 'unit': ''},
            'list_date': {'label': '上市日期', 'unit': ''},
            'delist_date': {'label': '退市/摘牌日期', 'unit': ''},
            'status': {'label': '状态', 'unit': ''},
        },
        'coverage': {
            'status': 'complete' if complete else ('partial' if data else 'empty'),
            'complete': complete,
            'truncated': best['truncated'],
            'returned_count': len(data),
            'source_reported_rows': best['source_reported_rows'],
            'unknown_dates': unknown_dates,
            'conflicts': best['conflicts'],
        },
        'attempts': attempts,
        'raws': raws,
        'notes': list(dict.fromkeys(notes)),
        'elapsed_seconds': round(time.monotonic() - start, 2),
    }
    return _store_helper('market_symbols', request, payload)


def _search_route(c, scope, provider):
    if provider != 'auto':
        return [provider]
    base = list(c.get('global_route') or config.PROVIDERS)
    for name in config.PROVIDERS:
        if name not in base:
            base.append(name)
    meta = HELPER_CAPABILITIES['search_securities']['providers']

    def rank(name):
        info = meta.get(name, {})
        support = info.get('support', 'unsupported')
        if scope == 'convertible_bond' and support == 'stock_only':
            return 9
        if scope == 'all' and support == 'stock_only':
            return 5
        return info.get('tier', 9)

    return [name for name in sorted(base, key=rank) if rank(name) < 9]


async def search_securities(request: SearchSecuritiesRequest):
    request = SearchSecuritiesRequest.model_validate(request.model_dump())
    if request.queries and len(request.queries) > 1:
        import asyncio
        start = time.monotonic()
        sub_reqs = [
            SearchSecuritiesRequest(
                query=q, scope=request.scope, limit=request.limit,
                include_delisted=request.include_delisted, provider=request.provider,
            )
            for q in request.queries
        ]
        sub_results = await asyncio.gather(*(search_securities(sr) for sr in sub_reqs))
        by_query = {sr.query: {'symbols': res['symbols'], 'top': res['data'][0] if res['data'] else None, 'count': res['count'], 'provider': res['provider']}
                    for sr, res in zip(sub_reqs, sub_results)}
        merged_data = []
        seen = set()
        for res in sub_results:
            for row in res['data']:
                if row['symbol'] not in seen:
                    seen.add(row['symbol'])
                    merged_data.append(row)
        return {
            'status': 'success' if merged_data else 'empty',
            'helper': 'search_securities',
            'batch': True,
            'queries': request.queries,
            'by_query': by_query,
            'symbols': [r['symbol'] for r in merged_data],
            'count': len(merged_data),
            'data': merged_data,
            'elapsed_seconds': round(time.monotonic() - start, 2),
        }
    c = config.load()
    settings = _settings(c)
    start = time.monotonic()
    deadline = start + settings['max_elapsed_seconds']
    attempts = []
    raws = []
    best = None
    meta = HELPER_CAPABILITIES['search_securities']['providers']

    for name in _search_route(c, request.scope, request.provider):
        p = c['providers'][name]
        if not core.available(name, p):
            attempts.append({'provider': name, 'operation': 'search_securities', 'status': 'skipped', 'error': '未启用或配置未完成'})
            continue
        if request.scope == 'convertible_bond' and meta.get(name, {}).get('support') == 'stock_only':
            attempts.append({'provider': name, 'operation': 'search_securities', 'status': 'unsupported',
                             'error': meta[name]['summary']})
            continue
        call = {
            'operation': 'search_securities',
            'scope': request.scope,
            'query': request.query,
            'limit': request.limit,
            'include_delisted': request.include_delisted,
        }
        outcome = (await _run_tasks([{'provider': name, 'call': call, 'timeout': p.get('timeout_seconds', 60)}], deadline, settings))[0]
        if outcome['status'] != 'success':
            attempts.append({'provider': name, 'operation': 'search_securities', 'status': outcome['status'], 'error': outcome.get('error')})
            continue
        result = outcome['result']
        rows = list(result.get('data') or [])
        raws.append({'provider': name, 'query': call, 'raw': result.get('raw')})
        attempts.append({
            'provider': name, 'operation': 'search_securities', 'strategy': result.get('strategy'),
            'status': 'success' if rows else 'empty', 'row_count': len(rows),
        })
        candidate = {
            'provider': name,
            'upstream': result.get('upstream', name),
            'data': rows,
            'total_matches': result.get('total_matches', len(rows)),
            'notes': list(result.get('notes') or []),
        }
        if best is None or (rows and not best['data']):
            best = candidate
        if rows or request.provider != 'auto':
            break

    if best is None:
        if any(a['status'] in {'error', 'timeout'} for a in attempts):
            raise core.QueryFailure(attempts)
        raise ValueError('当前没有可用的数据源执行证券搜索，请检查来源配置。')

    data = best['data']
    prov = best['provider']
    from .market_provider import _is_code_query
    payload = {
        'status': 'success' if data else 'empty',
        'complete': True,
        'scope': request.scope,
        'query_text': request.query,
        'query_kind': 'code' if _is_code_query(request.query) else 'name',
        'provider': prov,
        'providers': [prov],
        'upstream': best['upstream'],
        'count': len(data),
        'total_matches': best['total_matches'],
        'row_count': len(data),
        'symbols': [row['symbol'] for row in data],
        'data': data,
        'field_sources': [{k: prov for k in ('name', 'list_date', 'delist_date') if row.get(k) is not None} for row in data],
        'fields': {
            'symbol': {'label': '证券代码', 'unit': ''},
            'name': {'label': '证券简称', 'unit': ''},
            'scope': {'label': '类型', 'unit': ''},
            'exchange': {'label': '交易所', 'unit': ''},
            'list_date': {'label': '上市日期', 'unit': ''},
            'delist_date': {'label': '退市/摘牌日期', 'unit': ''},
            'status': {'label': '状态', 'unit': ''},
            'match_type': {'label': '匹配方式', 'unit': ''},
        },
        'attempts': attempts,
        'raws': raws,
        'notes': list(dict.fromkeys(best['notes'])),
        'elapsed_seconds': round(time.monotonic() - start, 2),
    }
    return _store_helper('search_securities', request, payload)
