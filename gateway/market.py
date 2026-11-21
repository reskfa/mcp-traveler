"""Cross-security market queries, coverage and close-to-close returns."""
from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
import weakref
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import config, core
from .market_provider import CAPABILITIES, MARKET_FIELDS, normalize_symbol


Provider = Literal['auto', 'tushare', 'akshare', 'wind', 'ifind', 'tdx', 'choice']
Scope = Literal['stock', 'convertible_bond']

FIELD_META = {
    'open': ('开盘价', '元'), 'high': ('最高价', '元'), 'low': ('最低价', '元'), 'close': ('收盘价', '元'),
    'volume': ('成交量', '股/手'), 'amount': ('成交额', '元'), 'turnover_rate': ('换手率', '%'),
    'pre_close': ('前收盘价', '元'), 'pct_change': ('涨跌幅', '%'),
    'name': ('证券简称', ''), 'list_date': ('上市日期', ''), 'delist_date': ('退市/摘牌日期', ''),
    'return_pct': ('区间价格收益率', '%'), 'return_method': ('收益计算方式', ''),
    'window_start': ('窗口开始日', ''), 'window_end': ('窗口结束日', ''), 'baseline_date': ('基准日', ''),
    'close_baseline': ('基准日复权收盘价', '元'), 'close_end': ('结束日复权收盘价', '元'),
    'provider': ('收益序列来源', ''), 'rank': ('排名', ''),
}

MARKET_DEFAULTS = {
    'batch_size': 50, 'max_concurrency': 4, 'per_provider_concurrency': 2,
    'min_request_interval_seconds': .25, 'max_elapsed_seconds': 900,
    'max_symbols': 10000, 'max_symbol_days': 250000,
}


def _iso(value):
    if value and date.fromisoformat(value).isoformat() != value:
        raise ValueError('日期使用 YYYY-MM-DD')
    return value


def _is_cb(symbol):
    return (symbol.startswith('11') and symbol.endswith('.SH')) or (symbol.startswith('12') and symbol.endswith('.SZ'))


def _scope_symbols(scope, symbols):
    if symbols is None:
        return None
    if not symbols:
        raise ValueError('symbols空数组无效；省略symbols才表示全市场')
    output = []
    for value in symbols:
        symbol = str(value).upper()
        normalized = normalize_symbol(symbol)
        if not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', symbol) or normalized != symbol:
            raise ValueError(f'证券代码格式不正确：{value}')
        if scope == 'convertible_bond' and not _is_cb(symbol):
            raise ValueError(f'{symbol}不是网关支持的可转债代码')
        if scope == 'stock' and _is_cb(symbol):
            raise ValueError(f'{symbol}是转债，请使用convertible_bond范围')
        if symbol not in output:
            output.append(symbol)
    return output


class MarketUniverseRequest(BaseModel):
    scope: Scope
    as_of_date: str | None = None
    provider: Provider = 'auto'

    @model_validator(mode='after')
    def valid(self):
        _iso(self.as_of_date)
        return self


class MarketDailyRequest(BaseModel):
    scope: Scope
    start_date: str
    end_date: str
    symbols: list[str] | None = None
    fields: list[str] | None = None
    provider: Provider = 'auto'
    adjust: Literal['none', 'qfq', 'hfq'] = 'none'
    sort_by: Literal['date', 'symbol', 'pct_change'] | None = None
    order: Literal['asc', 'desc'] = 'asc'
    top_n: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode='after')
    def valid(self):
        _iso(self.start_date); _iso(self.end_date)
        if self.start_date > self.end_date:
            raise ValueError('开始日期不能晚于结束日期')
        self.symbols = _scope_symbols(self.scope, self.symbols)
        if self.scope == 'convertible_bond' and self.adjust != 'none':
            raise ValueError('转债不接受复权参数')
        self.fields = list(dict.fromkeys(self.fields or ['close', 'pct_change']))
        if set(self.fields) - set(MARKET_FIELDS):
            raise ValueError('包含不支持的市场日线字段')
        if self.sort_by == 'pct_change' and self.start_date != self.end_date:
            raise ValueError('按单日涨跌幅排序时start_date必须等于end_date')
        return self


class MarketReturnsRequest(BaseModel):
    scope: Scope
    end_date: str
    start_date: str | None = None
    trading_days: int | None = Field(default=None, ge=1, le=250)
    symbols: list[str] | None = None
    provider: Provider = 'auto'
    order: Literal['asc', 'desc'] = 'asc'
    top_n: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode='after')
    def valid(self):
        _iso(self.start_date); _iso(self.end_date)
        if bool(self.start_date) == bool(self.trading_days):
            raise ValueError('start_date与trading_days必须二选一')
        if self.start_date and self.start_date > self.end_date:
            raise ValueError('开始日期不能晚于结束日期')
        self.symbols = _scope_symbols(self.scope, self.symbols)
        return self


_LIMITERS = weakref.WeakKeyDictionary()


def _settings(c):
    return {**MARKET_DEFAULTS, **c.get('market', {})}


def _limiters(settings):
    loop = asyncio.get_running_loop()
    if loop not in _LIMITERS:
        _LIMITERS[loop] = {
            'global': asyncio.Semaphore(settings['max_concurrency']),
            'providers': {name: asyncio.Semaphore(settings['per_provider_concurrency']) for name in config.PROVIDERS},
            'locks': {name: asyncio.Lock() for name in config.PROVIDERS},
            'next_start': {name: 0.0 for name in config.PROVIDERS},
        }
    return _LIMITERS[loop]


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('市场查询达到总时限')
    return remaining


async def _invoke_limited(name, query, timeout, deadline, settings):
    limiters = _limiters(settings)
    await asyncio.wait_for(limiters['global'].acquire(), _remaining(deadline))
    try:
        await asyncio.wait_for(limiters['providers'][name].acquire(), _remaining(deadline))
        try:
            async with limiters['locks'][name]:
                delay = limiters['next_start'][name] - time.monotonic()
                if delay > 0:
                    await asyncio.wait_for(asyncio.sleep(delay), _remaining(deadline))
                limiters['next_start'][name] = time.monotonic() + settings['min_request_interval_seconds']
            result = await asyncio.wait_for(core.invoke(name, query, min(timeout, _remaining(deadline))), _remaining(deadline))
            core.record(name, True)
            return result
        finally:
            limiters['providers'][name].release()
    finally:
        limiters['global'].release()


def _route(c, scope, provider):
    if provider != 'auto':
        return [provider]
    dataset = 'daily_prices' if scope == 'stock' else 'cb_daily'
    return c['routes'].get(dataset, c.get('global_route', config.PROVIDERS))


def _query_fields(fields):
    return {field: {'label': FIELD_META.get(field, (field, ''))[0], 'unit': FIELD_META.get(field, (field, ''))[1]} for field in fields}


def _store(kind, request, payload, top_n=None):
    rid = uuid.uuid4().hex
    folder = config.ROOT / 'local/results'
    folder.mkdir(parents=True, exist_ok=True)
    data = payload['data']
    provenance = payload['field_sources']
    providers = list(dict.fromkeys(source for row in provenance for source in row.values()))
    full_count = len(data)
    answer = {
        **{key: value for key, value in payload.items() if key not in {'data', 'field_sources', 'raws'}},
        'id': rid, 'dataset': f'market_{kind}', 'query': request.model_dump(),
        'requested_provider': request.provider, 'provider': providers[0] if len(providers) == 1 else ('mixed' if providers else None),
        'providers': providers, 'fetched_at': core.now(), 'row_count': full_count, 'full_row_count': full_count,
        'data': data, 'field_sources': provenance,
        'raw_path': str(folder / f'{rid}.raw.json'), 'data_path': str(folder / f'{rid}.json'),
        'download_url': f'/api/results/{rid}/csv', 'raw_url': f'/api/results/{rid}/raw',
    }
    if top_n:
        answer['projection'] = {'type': 'top_n', 'top_n': top_n, 'full_row_count': full_count, 'returned_row_count': min(top_n, full_count)}
    config.write_json(folder / f'{rid}.raw.json', config.redact(payload.get('raws', [])))
    config.write_json(folder / f'{rid}.json', config.redact(answer))
    if top_n:
        answer['data'] = data[:top_n]
        answer['field_sources'] = provenance[:top_n]
        answer['row_count'] = len(answer['data'])
    return answer


async def _run_tasks(tasks, deadline, settings):
    results = []
    wave_size = max(1, settings['max_concurrency'] * 2)
    for offset in range(0, len(tasks), wave_size):
        if time.monotonic() >= deadline:
            for task in tasks[offset:]:
                results.append({'task': task, 'status': 'not_attempted', 'error': '达到总时限，未发起请求'})
            break
        wave = tasks[offset:offset + wave_size]
        async def run(task):
            try:
                result = await _invoke_limited(task['provider'], {'market_call': task['call']}, task['timeout'], deadline, settings)
                return {'task': task, 'status': 'success', 'result': result}
            except Exception as exc:
                return {'task': task, 'status': 'timeout' if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else 'error',
                        'error': config.redact(str(exc) or '请求超时')}
        results.extend(await asyncio.gather(*(run(task) for task in wave)))
    return results


async def _calendar(start_date, end_date, provider, c, deadline, settings):
    candidates = ['tushare'] if provider == 'auto' or provider != 'tushare' else ['tushare']
    attempts = []
    raws = []
    for name in candidates:
        p = c['providers'][name]
        if not core.available(name, p):
            attempts.append({'provider': name, 'operation': 'calendar', 'status': 'skipped', 'error': '未启用或配置未完成'})
            continue
        call = {'operation': 'calendar', 'scope': 'stock', 'start_date': start_date, 'end_date': end_date}
        outcome = (await _run_tasks([{'provider': name, 'call': call, 'timeout': p.get('timeout_seconds', 60)}], deadline, settings))[0]
        if outcome['status'] == 'success':
            result = outcome['result']
            attempts.append({'provider': name, 'operation': 'calendar', 'status': 'success'})
            raws.append({'provider': name, 'query': call, 'raw': result['raw']})
            return result['dates'], attempts, raws, name
        attempts.append({'provider': name, 'operation': 'calendar', 'status': outcome['status'], 'error': outcome.get('error')})
    raise ValueError('市场查询需要可用的Tushare交易日历；请配置Tushare或缩小为已有日历支持的请求')


def _filter_universe(data, start_date, end_date):
    selected = []
    unknown = []
    for row in data:
        if not row.get('list_date'):
            unknown.append(row['symbol'])
            continue
        if row['list_date'] <= end_date and (not row.get('delist_date') or row['delist_date'] >= start_date):
            selected.append(row['symbol'])
    return selected, unknown


async def _load_universe(request, c, deadline, settings):
    rows = {}
    provenance = {}
    conflicts = []
    attempts = []
    raws = []
    complete = False
    notes = []
    for name in _route(c, request.scope, request.provider):
        p = c['providers'][name]
        if not core.available(name, p):
            attempts.append({'provider': name, 'operation': 'universe', 'status': 'skipped', 'error': '未启用或配置未完成'})
            continue
        if not CAPABILITIES.get(name, {}).get('universe'):
            attempts.append({'provider': name, 'operation': 'universe', 'status': 'unsupported', 'error': '未接入证券目录'})
            continue
        call = {'operation': 'universe', 'scope': request.scope}
        outcome = (await _run_tasks([{'provider': name, 'call': call, 'timeout': p.get('timeout_seconds', 60)}], deadline, settings))[0]
        if outcome['status'] != 'success':
            attempts.append({'provider': name, 'operation': 'universe', 'status': outcome['status'], 'error': outcome.get('error')})
            continue
        result = outcome['result']
        raws.append({'provider': name, 'query': call, 'raw': result['raw']})
        attempts.append({'provider': name, 'operation': 'universe', 'strategy': result.get('strategy'),
                         'status': 'success' if result.get('complete') else 'partial', 'row_count': len(result['data'])})
        for item in result['data']:
            symbol = item['symbol']
            target = rows.setdefault(symbol, {'symbol': symbol, 'name': None, 'list_date': None, 'delist_date': None})
            sources = provenance.setdefault(symbol, {})
            for field in ('name', 'list_date', 'delist_date'):
                value = item.get(field)
                if value is None:
                    continue
                if target[field] is not None and target[field] != value:
                    conflicts.append({'symbol': symbol, 'field': field, 'values': [target[field], value], 'provider': name})
                elif target[field] is None:
                    target[field] = value
                    sources[field] = name
        conflicts.extend(result.get('conflicts', []))
        notes.extend(result.get('notes', []))
        complete = bool(result.get('universe_complete'))
        if complete or request.provider != 'auto':
            break
    if request.as_of_date:
        selected, unknown = _filter_universe(list(rows.values()), request.as_of_date, request.as_of_date)
        allowed = set(selected)
        rows = {symbol: row for symbol, row in rows.items() if symbol in allowed}
        provenance = {symbol: sources for symbol, sources in provenance.items() if symbol in allowed}
    else:
        unknown = [symbol for symbol, row in rows.items() if not row.get('list_date')]
    if conflicts:
        complete = False
    complete = complete and not conflicts and not unknown
    data = sorted(rows.values(), key=lambda row: row['symbol'])
    return {'data': data, 'field_sources': [provenance[row['symbol']] for row in data], 'attempts': attempts, 'raws': raws,
            'universe': {'requested_count': len(data), 'resolved_count': len(data), 'complete': complete,
                         'unknown': unknown, 'conflicts': conflicts},
            'complete': complete, 'fields': _query_fields(('name', 'list_date', 'delist_date')),
            'notes': list(dict.fromkeys(notes))}


async def query_market_universe(request):
    request = MarketUniverseRequest.model_validate(request.model_dump())
    c = config.load(); settings = _settings(c); start = time.monotonic()
    deadline = start + settings['max_elapsed_seconds']
    payload = await _load_universe(request, c, deadline, settings)
    if not payload['data'] and any(item['status'] in {'error', 'timeout'} for item in payload['attempts']):
        raise core.QueryFailure(payload['attempts'])
    payload['status'] = 'success' if payload['complete'] else ('empty' if not payload['data'] else 'partial')
    payload['coverage'] = {'status': 'complete' if payload['complete'] else 'not_verified', **payload['universe']}
    payload['elapsed_seconds'] = round(time.monotonic() - start, 2)
    return _store('universe', request, payload)


def _daily_tasks(name, request, symbols, trade_dates, p, settings, adjustment_anchor_date=None):
    capability = CAPABILITIES.get(name, {})
    if request.adjust != 'none' and not capability.get('adjusted'):
        return []
    base = {'operation': 'daily', 'scope': request.scope, 'start_date': request.start_date, 'end_date': request.end_date,
            'fields': request.fields, 'adjust': request.adjust, 'trade_dates': trade_dates,
            'full_market': request.symbols is None, 'adjustment_anchor_date': adjustment_anchor_date}
    timeout = p.get('timeout_seconds', 60)
    tasks = []
    native_full = capability.get('native_full') and (name == 'tushare' or request.symbols is None) and not (name == 'tushare' and request.adjust == 'hfq')
    if native_full:
        # Adjusted series must share one anchor across the whole window.
        calls = [{**base, 'symbols': symbols}] if request.adjust != 'none' else [
            {**base, 'start_date': day, 'end_date': day, 'trade_dates': [day],
             'symbols': symbols if name == 'tushare' else None}
            for day in trade_dates]
        for call in calls:
            tasks.append({'provider': name, 'call': call, 'timeout': timeout, 'strategy': 'native_full',
                          'symbol_count': len(symbols)})
    elif capability.get('native_batch') and symbols:
        size = min(settings['batch_size'], capability.get('batch_limit', settings['batch_size']))
        for offset in range(0, len(symbols), size):
            batch = symbols[offset:offset + size]
            tasks.append({'provider': name, 'call': {**base, 'symbols': batch}, 'timeout': timeout,
                          'strategy': 'native_batch', 'batch_index': offset // size, 'symbol_count': len(batch)})
    elif capability.get('scalar') and symbols:
        for symbol in symbols:
            tasks.append({'provider': name, 'call': {**base, 'symbols': [symbol]}, 'timeout': timeout,
                          'strategy': 'scalar', 'symbol_count': 1})
    return tasks


def _merge_result(rows, provenance, result, provider, request, allowed_symbols):
    for item in result.get('data', []):
        symbol = normalize_symbol(item.get('symbol'))
        actual = item.get('date')
        if not symbol or symbol not in allowed_symbols or not actual or not request.start_date <= actual <= request.end_date:
            continue
        key = (symbol, actual)
        target = rows.setdefault(key, {'symbol': symbol, 'date': actual, **{field: None for field in request.fields}})
        sources = provenance.setdefault(key, {})
        for field in request.fields:
            value = item.get(field)
            if value is None:
                continue
            if request.adjust != 'none' and field in {'open', 'high', 'low', 'close', 'pre_close'} and target.get(field) is not None and sources.get(field) != provider:
                continue
            if target.get(field) is None:
                target[field] = value
                sources[field] = provider


def _sort_daily(rows, provenance, request):
    pairs = list(zip(rows, provenance))
    if not request.sort_by or request.sort_by == 'date':
        return sorted(pairs, key=lambda pair: (pair[0]['date'], pair[0]['symbol']))
    if request.sort_by == 'symbol':
        return sorted(pairs, key=lambda pair: (pair[0]['symbol'], pair[0]['date']))
    field = request.sort_by
    present = [pair for pair in pairs if pair[0].get(field) is not None]
    missing = [pair for pair in pairs if pair[0].get(field) is None]
    present.sort(key=lambda pair: (pair[0]['symbol'], pair[0]['date']))
    present.sort(key=lambda pair: pair[0][field], reverse=request.order == 'desc')
    return present + missing


async def _daily_data(request, c, deadline, settings):
    calendar, calendar_attempts, calendar_raws, calendar_provider = await _calendar(
        request.start_date, request.end_date, request.provider, c, deadline, settings)
    if request.start_date == request.end_date and request.start_date not in calendar:
        raise ValueError(f'{request.start_date}不是交易日；不会自动切换到相邻交易日')
    attempts = list(calendar_attempts)
    raws = list(calendar_raws)
    notes = [f'交易日历来源：{calendar_provider}']
    adjustment_anchor_date = None
    tushare = c['providers'].get('tushare', {})
    if (request.scope == 'stock' and request.adjust == 'qfq'
            and 'tushare' in _route(c, request.scope, request.provider)
            and core.available('tushare', tushare)):
        today = date.today().isoformat()
        if request.end_date > today:
            raise ValueError('前复权需要截至当前的完整因子历史，end_date不能晚于今天')
        if today > request.end_date:
            anchor_calendar, anchor_attempts, anchor_raws, _ = await _calendar(
                request.end_date, today, request.provider, c, deadline, settings)
            attempts.extend(anchor_attempts)
            raws.extend(anchor_raws)
        else:
            anchor_calendar = calendar
        anchor_dates = [day for day in anchor_calendar if day <= today]
        if not anchor_dates:
            raise ValueError('缺少前复权锚点交易日')
        adjustment_anchor_date = anchor_dates[-1]
        notes.append(f'Tushare前复权锚点交易日：{adjustment_anchor_date}（与单券完整因子历史口径一致）')
    universe_info = {'requested_count': len(request.symbols or []), 'resolved_count': len(request.symbols or []),
                     'complete': request.symbols is not None, 'unknown': [], 'conflicts': []}
    symbols = request.symbols
    if symbols is None:
        directory_provider = request.provider if CAPABILITIES.get(request.provider, {}).get('universe') else 'auto'
        directory = await _load_universe(MarketUniverseRequest(scope=request.scope, provider=directory_provider), c, deadline, settings)
        symbols, unknown = _filter_universe(directory['data'], request.start_date, request.end_date)
        universe_info = {**directory['universe'], 'requested_count': len(symbols), 'resolved_count': len(symbols), 'unknown': unknown}
        attempts.extend(directory['attempts']); raws.extend(directory['raws']); notes.extend(directory['notes'])
        if not symbols:
            raise core.QueryFailure(attempts)
    if len(symbols) > settings['max_symbols']:
        raise ValueError(f'证券数量超过market.max_symbols={settings["max_symbols"]}')
    if len(symbols) * max(1, len(calendar)) > settings['max_symbol_days']:
        raise ValueError(f'证券×交易日超过market.max_symbol_days={settings["max_symbol_days"]}')
    rows = {}
    provenance = {}
    allowed_symbols = set(symbols)
    task_outcomes = []
    for name in _route(c, request.scope, request.provider):
        p = c['providers'][name]
        if not core.available(name, p):
            attempts.append({'provider': name, 'operation': 'daily', 'status': 'skipped', 'error': '未启用或配置未完成'})
            continue
        tasks = _daily_tasks(name, request, symbols, calendar, p, settings, adjustment_anchor_date)
        if not tasks:
            attempts.append({'provider': name, 'operation': 'daily', 'status': 'unsupported', 'error': '无可用的市场取数策略'})
            continue
        outcomes = await _run_tasks(tasks, deadline, settings)
        task_outcomes.extend(outcomes)
        for outcome in outcomes:
            task = outcome['task']
            attempt = {'provider': name, 'operation': 'daily', 'strategy': task['strategy'], 'status': outcome['status'],
                       'batch_index': task['call'].get('batch_index'), 'symbol_count': task.get('symbol_count', 0)}
            if outcome['status'] == 'success':
                result = outcome['result']
                if result.get('truncated') or not result.get('complete', True):
                    attempt['status'] = 'partial'
                attempt['row_count'] = len(result.get('data', []))
                for key in ('missing_symbols', 'missing_fields', 'missing_dates', 'skipped_rows'):
                    if result.get(key):
                        attempt[key] = result[key]
                _merge_result(rows, provenance, result, name, request, allowed_symbols)
                raws.append({'provider': name, 'query': task['call'], 'raw': result.get('raw')})
                notes.extend(result.get('notes', []))
            else:
                attempt['error'] = outcome.get('error')
            attempts.append(attempt)
        missing_cells = any(row.get(field) is None for row in rows.values() for field in request.fields)
        failed = any(outcome['status'] in {'error', 'timeout', 'not_attempted'} or
                     (outcome['status'] == 'success' and not outcome['result'].get('complete', True)) for outcome in outcomes)
        native_success = any(outcome['status'] == 'success' and outcome['task']['strategy'] == 'native_full' for outcome in outcomes)
        symbols_seen = {symbol for symbol, day in rows}
        if request.provider != 'auto' or (not missing_cells and not failed and (native_success or set(symbols) <= symbols_seen)):
            break
    if not rows and calendar and symbols:
        if task_outcomes and not any(outcome['status'] == 'success' for outcome in task_outcomes):
            raise core.QueryFailure(attempts)
        if not task_outcomes:
            raise core.QueryFailure(attempts)
    pairs = _sort_daily(list(rows.values()), list(provenance.values()), request)
    # Re-align provenance by identity after sorting; dict insertion order is not a sort key.
    pairs = sorted(pairs, key=lambda pair: (pair[0]['date'], pair[0]['symbol'])) if not request.sort_by else pairs
    data = [row for row, sources in pairs]
    data_sources = [sources for row, sources in pairs]
    per_symbol = {}
    for symbol in symbols:
        expected_dates = set(calendar)
        actual_dates = {day for sym, day in rows if sym == symbol}
        symbol_rows = [row for (sym, day), row in rows.items() if sym == symbol]
        missing_fields = sorted({field for row in symbol_rows for field in request.fields if row.get(field) is None})
        errors = [outcome.get('error') for outcome in task_outcomes
                  if outcome['status'] in {'error', 'timeout'} and symbol in (outcome['task']['call'].get('symbols') or [])]
        not_attempted = any(outcome['status'] == 'not_attempted' and symbol in (outcome['task']['call'].get('symbols') or []) for outcome in task_outcomes)
        missing_dates = sorted(expected_dates - actual_dates)
        if not_attempted and not symbol_rows:
            status = 'not_attempted'
        elif errors and not symbol_rows:
            status = 'failed'
        elif not symbol_rows:
            status = 'missing'
        elif errors or missing_dates or missing_fields or not_attempted:
            status = 'partial'
        else:
            status = 'success'
        per_symbol[symbol] = {'status': status, 'expected_rows': len(expected_dates), 'returned_rows': len(symbol_rows),
                              'missing_dates': missing_dates, 'missing_fields': missing_fields, 'errors': list(dict.fromkeys(filter(None, errors)))}
    failed_tasks = any(outcome['status'] != 'success' or not outcome['result'].get('complete', True) for outcome in task_outcomes)
    truncated = any(outcome['status'] == 'success' and outcome['result'].get('truncated') for outcome in task_outcomes)
    missing_cells = any(row.get(field) is None for row in rows.values() for field in request.fields)
    complete = bool(universe_info['complete']) and not failed_tasks and not truncated and not missing_cells and all(
        item['status'] == 'success' for item in per_symbol.values()) and bool(data)
    actual_dates = sorted({row['date'] for row in data})
    coverage = {'status': 'complete' if complete else 'partial' if data else 'empty',
                'expected_rows': len(symbols) * len(calendar), 'returned_rows': len(data), 'returned_symbols': len({row['symbol'] for row in data}),
                'symbols': per_symbol, 'ranking_complete': False}
    return {'status': 'success' if complete else 'empty' if not data else 'partial', 'complete': complete,
            'scope': request.scope, 'universe': universe_info,
            'window': {'requested_start': request.start_date, 'requested_end': request.end_date,
                       'actual_trade_dates': actual_dates, 'calendar_trade_dates': calendar, 'baseline_date': None},
            'data': data, 'field_sources': data_sources, 'coverage': coverage, 'attempts': attempts, 'raws': raws,
            'fields': _query_fields(request.fields), 'notes': list(dict.fromkeys(notes))}


async def query_market_daily(request):
    request = MarketDailyRequest.model_validate(request.model_dump())
    c = config.load(); settings = _settings(c); start = time.monotonic()
    deadline = start + settings['max_elapsed_seconds']
    payload = await _daily_data(request, c, deadline, settings)
    payload['elapsed_seconds'] = round(time.monotonic() - start, 2)
    return _store('daily', request, payload, request.top_n)


async def _returns_window(request, c, deadline, settings):
    lookback = max(20, (request.trading_days or 1) * 4 + 10)
    start = request.start_date or (date.fromisoformat(request.end_date) - timedelta(days=lookback)).isoformat()
    if request.start_date:
        start = (date.fromisoformat(request.start_date) - timedelta(days=20)).isoformat()
    dates, attempts, raws, provider = await _calendar(start, request.end_date, request.provider, c, deadline, settings)
    if request.end_date not in dates:
        raise ValueError(f'{request.end_date}不是交易日；不会自动切换到相邻交易日')
    through_end = [day for day in dates if day <= request.end_date]
    if request.trading_days:
        if len(through_end) < request.trading_days + 1:
            raise ValueError('交易日历不足以构造请求窗口和基准日')
        selected = through_end[-(request.trading_days + 1):]
        baseline = selected[0]
        window = selected[1:]
    else:
        if request.start_date not in through_end:
            raise ValueError(f'{request.start_date}不是交易日；不会自动切换到相邻交易日')
        index = through_end.index(request.start_date)
        if index == 0:
            raise ValueError('交易日历缺少窗口前一交易日基准')
        baseline = through_end[index - 1]
        window = through_end[index:]
    return baseline, window, attempts, raws, provider


async def query_market_returns(request):
    request = MarketReturnsRequest.model_validate(request.model_dump())
    c = config.load(); settings = _settings(c); start = time.monotonic()
    deadline = start + settings['max_elapsed_seconds']
    baseline, window, calendar_attempts, calendar_raws, calendar_provider = await _returns_window(request, c, deadline, settings)
    daily_request = MarketDailyRequest(scope=request.scope, symbols=request.symbols, provider=request.provider,
                                       start_date=baseline, end_date=request.end_date, fields=['close'],
                                       adjust='qfq' if request.scope == 'stock' else 'none', sort_by='date')
    daily = await _daily_data(daily_request, c, deadline, settings)
    required = [baseline] + window
    by_symbol = {}
    for row, sources in zip(daily['data'], daily['field_sources']):
        by_symbol.setdefault(row['symbol'], {})[row['date']] = (row.get('close'), sources.get('close'))
    expected_symbols = request.symbols or sorted(by_symbol)
    if request.symbols is None:
        expected_symbols = [symbol for symbol, state in daily['coverage']['symbols'].items()
                            if daily['coverage']['symbols'][symbol]['status'] != 'not_attempted']
    output = []
    excluded = {}
    for symbol in expected_symbols:
        observations = by_symbol.get(symbol, {})
        missing = [day for day in required if day not in observations or observations[day][0] is None]
        if missing:
            excluded[symbol] = {'reason': 'missing_window_prices', 'dates': missing}
            continue
        providers = {observations[day][1] for day in required}
        if len(providers) != 1 or None in providers:
            excluded[symbol] = {'reason': 'mixed_or_unknown_provider', 'providers': sorted(str(value) for value in providers)}
            continue
        base = observations[baseline][0]; end = observations[request.end_date][0]
        if not all(math.isfinite(value) and value > 0 for value in (base, end)):
            excluded[symbol] = {'reason': 'invalid_close'}
            continue
        provider = next(iter(providers))
        output.append({'symbol': symbol, 'date': request.end_date,
                       'return_pct': 100 * (end / base - 1),
                       'return_method': 'close_to_close_qfq' if request.scope == 'stock' else 'close_to_close_none',
                       'window_start': window[0], 'window_end': window[-1], 'baseline_date': baseline,
                       'close_baseline': base, 'close_end': end, 'provider': provider})
    output.sort(key=lambda row: row['symbol'])
    output.sort(key=lambda row: row['return_pct'], reverse=request.order == 'desc')
    provenance = []
    for rank, row in enumerate(output, 1):
        row['rank'] = rank
        provenance.append({field: row['provider'] for field in ('return_pct', 'close_baseline', 'close_end', 'provider')})
    expected_set = set(expected_symbols)
    ranking_complete = bool(output) and expected_set == set(by_symbol) and not excluded and daily['universe']['complete'] and daily['complete']
    coverage = {**daily['coverage'], 'status': 'complete' if ranking_complete else 'partial' if output else 'empty',
                'ranking_complete': ranking_complete, 'eligible_symbols': len(output), 'excluded_symbols': excluded,
                'returned_rows': len(output), 'returned_symbols': len(output)}
    fields = ('return_pct', 'return_method', 'window_start', 'window_end', 'baseline_date', 'close_baseline', 'close_end', 'provider', 'rank')
    payload = {'status': 'success' if ranking_complete else 'empty' if not output else 'partial', 'complete': ranking_complete,
               'scope': request.scope, 'universe': daily['universe'],
               'window': {'requested_start': request.start_date, 'requested_end': request.end_date,
                          'actual_trade_dates': window, 'baseline_date': baseline, 'trading_days': len(window)},
               'data': output, 'field_sources': provenance, 'coverage': coverage,
               'attempts': calendar_attempts + daily['attempts'], 'raws': calendar_raws + daily['raws'],
               'fields': _query_fields(fields),
               'notes': list(dict.fromkeys(daily['notes'] + [
                   '收益为同源收盘价价格收益；股票使用前复权，转债不复权，不含现金分红或利息再投资。',
                   f'收益窗口交易日历来源：{calendar_provider}；基准日为{baseline}。',
                   '缺窗口价格或跨来源序列的证券不进入排名，缺失不当作0。'])),
               'elapsed_seconds': round(time.monotonic() - start, 2)}
    return _store('returns', request, payload, request.top_n)
