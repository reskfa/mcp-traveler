"""Provider-native market operations and scalar fallbacks.

All functions run inside the existing worker process.  Market orchestration,
concurrency and cross-provider coverage live in market.py.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date

from .acquisition import acquire, records
from .basic_info import pick
from .catalog import FIELDS
from .parsing import map_rows, norm_date, number, tables


CAPABILITIES = {
    'tushare': {'universe': True, 'calendar': True, 'native_full': True, 'scalar': True, 'adjusted': True},
    'choice': {'universe': True, 'calendar': False, 'native_full': True, 'native_batch': True, 'batch_limit': 500, 'scalar': True, 'adjusted': False},
    'akshare': {'universe': False, 'calendar': False, 'scalar': True, 'adjusted': True},
    'wind': {'universe': False, 'calendar': False, 'scalar': True, 'adjusted': True},
    'ifind': {'universe': False, 'calendar': False, 'scalar': True, 'adjusted': False},
    'tdx': {'universe': False, 'calendar': False, 'scalar': True, 'adjusted': True},
}

MARKET_FIELDS = ('open', 'high', 'low', 'close', 'volume', 'amount', 'turnover_rate', 'pre_close', 'pct_change')
IDENTITY_COLUMNS = ('symbol', 'ts_code', 'code', 'security_code', '证券代码', '股票代码', '债券代码', '代码', 'Wind代码')
DATE_COLUMNS = ('date', 'trade_date', '日期', '交易日期', '数据日期')
SOURCE_COLUMNS = {
    'open': ('open',), 'high': ('high',), 'low': ('low',), 'close': ('close',),
    'pre_close': ('pre_close',), 'pct_change': ('pct_chg',), 'volume': ('vol',), 'amount': ('amount',),
}


class MarketUnsupported(RuntimeError):
    pass


def normalize_symbol(value):
    if value is None:
        return None
    text = str(value).strip().upper()
    if re.fullmatch(r'[A-Z]\d{6}\.(?:SH|SZ|BJ)', text):
        return text
    match = re.search(r'(\d{6})(?:\.(SH|SZ|BJ|XSHG|XSHE|XBSE))?', text)
    if not match:
        match = re.search(r'(SH|SZ|BJ)(\d{6})', text)
        if match:
            code, market = match[2], match[1]
        else:
            return None
    else:
        code, market = match[1], match[2]
    market = {'XSHG': 'SH', 'XSHE': 'SZ', 'XBSE': 'BJ'}.get(market, market)
    if not market:
        if code.startswith('11'):
            market = 'SH'
        elif code.startswith('12'):
            market = 'SZ'
        elif code.startswith(('4', '8', '92')):
            market = 'BJ'
        elif code.startswith(('5', '6', '9')):
            market = 'SH'
        else:
            market = 'SZ'
    return f'{code}.{market}'


def symbol_for_row(row):
    symbols = {symbol for column in IDENTITY_COLUMNS if (symbol := normalize_symbol(row.get(column)))}
    return next(iter(symbols)) if len(symbols) == 1 else None


def symbol_matches_scope(symbol, scope):
    is_cb = bool(re.fullmatch(r'(?:11\d{4}\.SH|12\d{4}\.SZ)', symbol))
    return is_cb if scope == 'convertible_bond' else not is_cb


def _result(data, raw, strategy, complete=True, **extra):
    return {
        'data': data, 'raw': raw, 'strategy': strategy, 'complete': complete,
        'source_reported_rows': len(data), 'notes': [], **extra,
    }


def _paged(method, args, limit):
    rows = []
    raw = []
    truncated = False
    for offset in range(0, limit * 100, limit):
        frame = method(**args, limit=limit, offset=offset)
        batch = records(frame)
        raw.extend(batch)
        rows.extend(batch)
        if len(batch) < limit:
            break
    else:
        truncated = True
    return rows, raw, truncated


def _tushare_universe(sdk, scope, call):
    rows = []
    raw = {}
    truncated = False
    if scope == 'stock':
        for status in ('L', 'D', 'P'):
            frame = sdk.stock_basic(exchange='', list_status=status, fields='ts_code,name,list_date,delist_date')
            part = records(frame)
            raw[status] = part
            rows.extend(part)
            truncated |= len(part) >= 6000
    else:
        frame = sdk.cb_basic(fields='ts_code,bond_short_name,list_date,delist_date')
        rows = records(frame)
        raw['all'] = rows
        truncated = len(rows) >= 2000
    merged = {}
    conflicts = []
    for item in rows:
        symbol = normalize_symbol(item.get('ts_code'))
        if not symbol or not symbol_matches_scope(symbol, scope):
            continue
        entry = merged.setdefault(symbol, {'symbol': symbol, 'name': None, 'list_date': None, 'delist_date': None})
        values = {
            'name': item.get('name') or item.get('bond_short_name'),
            'list_date': norm_date(item.get('list_date')),
            'delist_date': norm_date(item.get('delist_date')),
        }
        for field, value in values.items():
            if value is None:
                continue
            if entry[field] is not None and entry[field] != value:
                conflicts.append({'symbol': symbol, 'field': field, 'values': [entry[field], value]})
            else:
                entry[field] = value
    data = sorted(merged.values(), key=lambda row: row['symbol'])
    return _result(data, raw, 'native_full', not truncated, truncated=truncated,
                   universe_complete=not truncated, conflicts=conflicts, upstream='Tushare security directory')


def _tushare_calendar(sdk, call):
    frame = sdk.trade_cal(exchange='SSE', start_date=call['start_date'].replace('-', ''),
                          end_date=call['end_date'].replace('-', ''),
                          fields='exchange,cal_date,is_open,pretrade_date')
    rows = records(frame)
    dates = sorted(norm_date(row['cal_date']) for row in rows if str(row.get('is_open')) in {'1', '1.0'})
    return {'dates': dates, 'raw': rows, 'complete': True, 'strategy': 'native_full', 'upstream': 'Tushare trade_cal'}


def _tushare_daily(sdk, scope, call):
    dataset = 'daily_prices' if scope == 'stock' else 'cb_daily'
    method = sdk.daily if scope == 'stock' else sdk.cb_daily
    limit = 6000 if scope == 'stock' else 2000
    fields = list(call['fields'])
    source_fields = ['ts_code', 'trade_date'] + list(dict.fromkeys(
        SOURCE_COLUMNS[field][0] for field in fields if field in SOURCE_COLUMNS))
    symbols = set(call.get('symbols') or [])
    selected = set(fields)
    rows = []
    raw = []
    truncated = False
    adjust = call.get('adjust', 'none')
    factor_rows_by_date = {}
    for day in call.get('trade_dates') or [call['start_date']]:
        args = {'trade_date': day.replace('-', ''), 'fields': ','.join(source_fields)}
        part, evidence, hit_limit = _paged(method, args, limit)
        truncated |= hit_limit
        entry = {'trade_date': day, 'rows': evidence}
        factors = {}
        if adjust != 'none' and scope == 'stock':
            factor_rows, factor_evidence, hit_limit = _paged(
                sdk.adj_factor,
                {'trade_date': day.replace('-', ''), 'fields': 'ts_code,trade_date,adj_factor'},
                6000)
            entry['factors'] = factor_evidence
            factor_rows_by_date[day] = factor_rows
            factors = {normalize_symbol(row.get('ts_code')): (number(row.get('adj_factor')), norm_date(row.get('trade_date'))) for row in factor_rows}
            truncated |= hit_limit
        for item in part:
            symbol = normalize_symbol(item.get('ts_code'))
            actual = norm_date(item.get('trade_date'))
            if not symbol or not symbol_matches_scope(symbol, scope) or not actual or (symbols and symbol not in symbols):
                continue
            row = {'symbol': symbol, 'date': actual}
            for field in fields:
                column = SOURCE_COLUMNS.get(field, (field,))[0]
                value = number(item.get(column))
                if value is not None and field == 'volume' and scope == 'stock':
                    value *= 100
                if value is not None and field == 'amount':
                    value *= 1000 if scope == 'stock' else 10000
                row[field] = value
            if adjust != 'none' and scope == 'stock':
                factor, _ = factors.get(symbol, (None, None))
                if factor is None:
                    raise RuntimeError(f'{symbol}在{actual}缺少复权因子')
                row['_adj_factor'] = factor
            rows.append(row)
        raw.append(entry)
    if adjust != 'none' and scope == 'stock':
        grouped = defaultdict(list)
        for row in rows:
            grouped[row['symbol']].append(row)
        anchor_date = call.get('adjustment_anchor_date')
        if adjust == 'qfq' and not anchor_date:
            raise RuntimeError('前复权缺少截至当前的锚点交易日')
        anchor_factors = {}
        if adjust == 'qfq':
            anchor_rows = factor_rows_by_date.get(anchor_date)
            if anchor_rows is None:
                anchor_rows, evidence, hit_limit = _paged(
                    sdk.adj_factor,
                    {'trade_date': anchor_date.replace('-', ''), 'fields': 'ts_code,trade_date,adj_factor'},
                    6000)
                raw.append({'adjustment_anchor_date': anchor_date, 'factors': evidence})
                truncated |= hit_limit
            anchor_factors = {
                symbol: factor for item in anchor_rows
                if (symbol := normalize_symbol(item.get('ts_code'))) and symbol_matches_scope(symbol, scope)
                and (factor := number(item.get('adj_factor'))) is not None
            }
        for symbol, group in grouped.items():
            anchor = anchor_factors.get(symbol) if adjust == 'qfq' else None
            if not anchor:
                # Delisted securities may be absent from the latest all-market factor table.
                # Full per-symbol history also gives hfq its true first-factor anchor.
                history, evidence, hit_limit = _paged(
                    sdk.adj_factor, {'ts_code': symbol, 'fields': 'ts_code,trade_date,adj_factor'}, 6000)
                raw.append({'symbol': symbol, 'adjustment_anchor_date': anchor_date, 'factor_history': evidence})
                truncated |= hit_limit
                factors = sorted(
                    (day, factor) for item in history
                    if (day := norm_date(item.get('trade_date')))
                    and (adjust != 'qfq' or day <= anchor_date)
                    and (factor := number(item.get('adj_factor'))) is not None
                )
                if factors:
                    anchor = factors[-1 if adjust == 'qfq' else 0][1]
            if not anchor:
                raise RuntimeError(f'{symbol}复权因子无效或完整历史为空')
            for row in group:
                ratio = row['_adj_factor'] / anchor
                for field in ('open', 'high', 'low', 'close', 'pre_close'):
                    if field in selected and row.get(field) is not None:
                        row[field] *= ratio
                row.pop('_adj_factor', None)
    for row in rows:
        row.pop('_adj_factor', None)
        if row.get('pct_change') is None and 'pct_change' in selected and row.get('close') and row.get('pre_close'):
            row['pct_change'] = 100 * (row['close'] / row['pre_close'] - 1)
    rows.sort(key=lambda row: (row['date'], row['symbol']))
    return _result(rows, raw, 'native_full', not truncated, truncated=truncated,
                   upstream='Tushare Pro market daily', actual_trade_dates=sorted({row['date'] for row in rows}))


def _compact_column(value):
    return re.sub(r'[\s_（）()：:，,.\-]', '', str(value)).lower()


def _custom_field(row, units, names):
    targets = {_compact_column(name) for name in names}
    for key, value in row.items():
        clean = re.sub(r'^20\d{2}年(?:(?:\d{1,2})月)?(?:\d{1,2}日)?', '', str(key))
        clean = re.sub(r'[（(]?(?:交易日|日期)[:：]?\d{8}[）)]?', '', clean)
        if _compact_column(clean) in targets:
            parsed = number(value, units.get(key, ''))
            if parsed is not None:
                return parsed
    return None


def _choice_question(scope, call):
    market = 'A股' if scope == 'stock' else '可转债'
    symbols = call.get('symbols')
    entity = '全部' + market if not symbols else market + '证券' + '、'.join(symbols)
    labels = {'open': '开盘价', 'high': '最高价', 'low': '最低价', 'close': '收盘价', 'volume': '成交量',
              'amount': '成交金额', 'turnover_rate': '换手率', 'pre_close': '前收盘价', 'pct_change': '涨跌幅'}
    fields = '、'.join(labels[field] for field in call['fields'])
    return (f'查询{entity}在{call["start_date"]}至{call["end_date"]}的{fields}。'
            '返回逐证券逐交易日完整记录表，每行必须有证券代码和实际交易日（YYYY-MM-DD），数值保留单位。'
            '不要用摘要、统计数或前若干条样例代替完整结果；如只能返回部分数据，必须明确总数量和截断原因。')


def _choice_daily(remote, scope, call):
    raw = remote.call('mx_ashare_finance_data' if scope == 'stock' else 'mx_bond_finance_data',
                      {'query': _choice_question(scope, call)})
    table = tables(raw)
    data = []
    skipped = 0
    dataset = 'daily_prices' if scope == 'stock' else 'cb_daily'
    for source, units in table:
        symbol = symbol_for_row(source)
        actual = next((d for key in DATE_COLUMNS if (d := norm_date(source.get(key)))), None)
        if not symbol or not symbol_matches_scope(symbol, scope) or not actual:
            skipped += 1
            continue
        standard = [field for field in call['fields'] if field in FIELDS[dataset]]
        mapped, _ = map_rows([(source, units)], standard, symbol, dataset, bindings=None)
        row = {'symbol': symbol, 'date': actual}
        row.update({field: mapped[0].get(field) if mapped else None for field in standard})
        for field in call['fields']:
            if field == 'pre_close' and scope == 'stock':
                row[field] = _custom_field(source, units, ('pre_close', '前收盘价', '昨收'))
            elif field == 'pct_change' and scope == 'stock':
                row[field] = _custom_field(source, units, ('pct_change', 'pct_chg', '涨跌幅', '日涨跌幅'))
        data.append(row)
    from .coverage import coverage
    cov = coverage(raw, data)
    expected_symbols = set(call.get('symbols') or [])
    actual_symbols = {row['symbol'] for row in data}
    missing_symbols = sorted(expected_symbols - actual_symbols)
    missing_fields = sorted({field for field in call['fields'] if any(row.get(field) is None for row in data)})
    expected_dates = set(call.get('trade_dates') or [])
    actual_dates = {row['date'] for row in data}
    missing_dates = sorted(expected_dates - actual_dates)
    reported = cov['source_reported_rows']
    count_mismatch = reported is not None and reported != len(data)
    complete = not skipped and not missing_symbols and not missing_fields and not missing_dates and not count_mismatch and cov['status'] != 'partial'
    strategy = 'native_full' if not call.get('symbols') else 'native_batch'
    return _result(data, raw, strategy, complete, truncated=cov['status'] == 'partial' or count_mismatch, skipped_rows=skipped,
                   missing_symbols=missing_symbols, missing_fields=missing_fields, missing_dates=missing_dates,
                   source_reported_rows=reported, upstream='东方财富妙想 MCP', actual_trade_dates=sorted(actual_dates))


def _choice_universe(remote, scope, call):
    market = 'A股（包括当前上市、暂停上市和已退市股票）' if scope == 'stock' else '可转债（包括当前上市和历史已摘牌转债）'
    question = (f'查询全部{market}名单。返回逐证券完整表，每行包含证券代码、证券简称、上市日期、退市或摘牌日期；'
                '不要只给当前上市证券，不要只返回前若干条。若无法提供历史证券或日期，请明确说明。')
    raw = remote.call('mx_stocks_screener', {'query': question})
    data = []
    skipped = 0
    for row, units in tables(raw):
        symbol = symbol_for_row(row)
        if not symbol or not symbol_matches_scope(symbol, scope):
            skipped += 1
            continue
        data.append({'symbol': symbol,
                     'name': pick(row, ('name', '证券简称', '股票简称', '债券简称', '简称')),
                     'list_date': next((d for key in ('list_date', '上市日期') if (d := norm_date(row.get(key)))), None),
                     'delist_date': next((d for key in ('delist_date', '退市日期', '摘牌日期') if (d := norm_date(row.get(key)))), None)})
    from .coverage import coverage
    cov = coverage(raw, data)
    historical_dates = all(row['list_date'] for row in data)
    reported = cov['source_reported_rows']
    complete = bool(data) and not skipped and historical_dates and reported is not None and reported == len(data) and cov['status'] != 'partial'
    return _result(data, raw, 'native_full', complete, truncated=cov['status'] == 'partial' or (reported is not None and reported != len(data)), skipped_rows=skipped,
                   source_reported_rows=reported, universe_complete=complete, conflicts=[], upstream='东方财富妙想证券筛选')


def _scalar_daily(name, scope, call, sdk=None, remote=None):
    symbols = call.get('symbols') or []
    if len(symbols) != 1:
        raise MarketUnsupported(f'{name}仅支持逐券历史降级')
    dataset = 'daily_prices' if scope == 'stock' else 'cb_daily'
    supported = [field for field in call['fields'] if field in FIELDS[dataset]]
    if not supported:
        supported = ['close']
    from .core import Query
    query = Query(dataset=dataset, symbol=symbols[0], start_date=call['start_date'], end_date=call['end_date'],
                  fields=supported, adjust=call.get('adjust', 'none')).model_dump()
    result = acquire(name, query, sdk=sdk, remote=remote)
    return _result(result['data'], result['raw'], 'scalar', True, notes=result.get('notes', []),
                   upstream=result.get('upstream', name), actual_trade_dates=sorted({row['date'] for row in result['data'] if row.get('date')}))


def fetch(name, p, key, call):
    operation = call['operation']
    scope = call['scope']
    if name == 'tushare':
        import tushare as ts
        sdk = ts.pro_api(key)
        if operation == 'universe':
            return _tushare_universe(sdk, scope, call)
        if operation == 'calendar':
            return _tushare_calendar(sdk, call)
        if operation == 'daily':
            return _tushare_daily(sdk, scope, call)
    if name == 'choice':
        from .providers import Remote
        remote = Remote(name, p, key)
        if operation == 'universe':
            return _choice_universe(remote, scope, call)
        if operation == 'daily':
            return _choice_daily(remote, scope, call)
        if operation == 'calendar':
            raise MarketUnsupported('Choice未接入交易日历')
    if operation == 'daily' and CAPABILITIES.get(name, {}).get('scalar'):
        sdk = None
        remote = None
        if name == 'akshare':
            import akshare as ak
            sdk = ak
        elif name != 'tushare':
            from .providers import Remote
            remote = Remote(name, p, key)
        return _scalar_daily(name, scope, call, sdk=sdk, remote=remote)
    raise MarketUnsupported(f'{name}未接入市场操作 {operation}')
