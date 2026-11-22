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
    'tushare': {'universe': True, 'calendar': True, 'calendar_mode': 'native', 'native_full': True, 'scalar': True, 'adjusted': True},
    'choice': {'universe': True, 'calendar': True, 'calendar_mode': 'index_derived', 'native_full': True, 'native_batch': True, 'batch_limit': 500, 'scalar': True, 'adjusted': False},
    'akshare': {'universe': True, 'calendar': True, 'calendar_mode': 'native', 'scalar': True, 'adjusted': True},
    'wind': {'universe': False, 'calendar': True, 'calendar_mode': 'index_derived', 'scalar': True, 'adjusted': True},
    'ifind': {'universe': True, 'calendar': True, 'calendar_mode': 'index_derived', 'scalar': True, 'adjusted': False},
    'tdx': {'universe': True, 'calendar': True, 'calendar_mode': 'index_derived', 'scalar': True, 'adjusted': True},
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
    exchange = call.get('exchange') or 'SSE'
    frame = sdk.trade_cal(exchange=exchange, start_date=call['start_date'].replace('-', ''),
                          end_date=call['end_date'].replace('-', ''),
                          fields='exchange,cal_date,is_open,pretrade_date')
    rows = records(frame)
    open_rows = []
    for row in rows:
        if str(row.get('is_open')) in {'1', '1.0'}:
            day = norm_date(row.get('cal_date'))
            if day:
                open_rows.append({
                    'date': day,
                    'exchange': row.get('exchange') or exchange,
                    'is_open': True,
                    'pretrade_date': norm_date(row.get('pretrade_date')),
                })
    open_rows.sort(key=lambda item: item['date'])
    dates = [item['date'] for item in open_rows]
    return {'dates': dates, 'data': open_rows, 'raw': rows, 'complete': True,
            'calendar_mode': 'native', 'strategy': 'native_full',
            'upstream': f'Tushare trade_cal ({exchange})', 'notes': []}


def _calendar_rows_from_dates(dates, exchange):
    return [{'date': day, 'exchange': exchange, 'is_open': True, 'pretrade_date': None} for day in dates]


def _akshare_calendar(sdk, call):
    exchange = call.get('exchange') or 'SSE'
    frame = sdk.tool_trade_date_hist_sina()
    raw = records(frame)
    all_dates = sorted({d for row in raw if (d := norm_date(row.get('trade_date')))})
    start_date, end_date = call['start_date'], call['end_date']
    dates = [d for d in all_dates if start_date <= d <= end_date]
    notes = []
    complete = True
    if all_dates and end_date > all_dates[-1]:
        complete = False
        notes.append(f'AKShare 新浪交易日历当前覆盖至 {all_dates[-1]}，请求结束日期 {end_date} 超出已发布范围。')
    return {'dates': dates, 'data': _calendar_rows_from_dates(dates, exchange), 'raw': raw[-30:] if len(raw) > 500 else raw,
            'complete': complete, 'calendar_mode': 'native', 'strategy': 'native_full',
            'upstream': 'AKShare tool_trade_date_hist_sina', 'notes': notes}


def _index_calendar_notes(provider_label, end_date):
    notes = [f'{provider_label}无独立交易日历接口，本次通过上证指数(000001.SH)历史日线日期推导交易日。']
    if end_date > date.today().isoformat():
        notes.append('指数推导日历仅包含已发生的历史交易日，不包含未来节假日安排。')
    return notes


def _wind_calendar(remote, call):
    exchange = call.get('exchange') or 'SSE'
    start_date, end_date = call['start_date'], call['end_date']
    raw = remote.call('get_stock_kline', {'windcode': '000001.SH', 'begin_date': start_date, 'end_date': end_date, 'period': '10'})
    dates = sorted({
        d for row, _ in tables(raw)
        if (d := norm_date(row.get('TIME') or row.get('date') or row.get('日期')))
        and start_date <= d <= end_date
    })
    complete = end_date <= date.today().isoformat()
    return {'dates': dates, 'data': _calendar_rows_from_dates(dates, exchange), 'raw': raw,
            'complete': complete, 'calendar_mode': 'index_derived', 'strategy': 'index_derived',
            'upstream': 'Wind MCP get_stock_kline (000001.SH)',
            'notes': _index_calendar_notes('Wind MCP ', end_date)}


def _tdx_calendar(remote, call):
    exchange = call.get('exchange') or 'SSE'
    start_date, end_date = call['start_date'], call['end_date']
    raw_pages = []
    dates_set = set()
    page_size = 1000
    for page in range(20):
        raw = remote.call('tdx_kline', {
            'code': '000001', 'setcode': '1', 'period': '4',
            'wantNum': str(page_size), 'startxh': str(page * page_size),
        })
        raw_pages.append(raw)
        page_dates = [
            d for row, _ in tables(raw)
            if (d := norm_date(row.get('Data') or row.get('date') or row.get('日期')))
        ]
        if not page_dates:
            break
        for d in page_dates:
            if start_date <= d <= end_date:
                dates_set.add(d)
        if min(page_dates) <= start_date or len(page_dates) < page_size:
            break
    dates = sorted(dates_set)
    complete = end_date <= date.today().isoformat()
    return {'dates': dates, 'data': _calendar_rows_from_dates(dates, exchange), 'raw': raw_pages,
            'complete': complete, 'calendar_mode': 'index_derived', 'strategy': 'index_derived',
            'upstream': '通达信 MCP tdx_kline (000001.SH)',
            'notes': _index_calendar_notes('通达信 MCP ', end_date)}


def _choice_calendar(remote, call):
    exchange = call.get('exchange') or 'SSE'
    start_date, end_date = call['start_date'], call['end_date']
    raw = remote.call('mx_index_block_finance_data', {
        'query': f'上证指数 000001.SH {start_date}至{end_date} 每日收盘价'
    })
    dates = sorted({
        d for row, _ in tables(raw)
        if (d := norm_date(row.get('date') or row.get('日期') or row.get('交易日期')))
        and start_date <= d <= end_date
    })
    complete = end_date <= date.today().isoformat()
    return {'dates': dates, 'data': _calendar_rows_from_dates(dates, exchange), 'raw': raw,
            'complete': complete, 'calendar_mode': 'index_derived', 'strategy': 'index_derived',
            'upstream': '东方财富妙想 MCP mx_index_block_finance_data (000001.SH)',
            'notes': _index_calendar_notes('东方财富妙想 MCP ', end_date)}


def _ifind_calendar(remote, call):
    exchange = call.get('exchange') or 'SSE'
    start_date, end_date = call['start_date'], call['end_date']
    # Requesting volume (not close) is critical: iFinD forward-fills close on weekends,
    # whereas volume is blank on non-trading days.
    raw = remote.call('get_security_indicators', {
        'market': '指数',
        'query': f'上证指数 000001.SH {start_date}至{end_date} 每日成交量',
    })
    dates_set = set()
    for row, units in tables(raw):
        d = norm_date(row.get('日期') or row.get('date') or row.get('交易日期'))
        if not d or not (start_date <= d <= end_date):
            continue
        vol_val = next((v for k, v in row.items() if '成交量' in str(k) or 'volume' in str(k).lower()), None)
        vol = number(vol_val)
        if vol is not None and vol > 0:
            dates_set.add(d)
    dates = sorted(dates_set)
    complete = end_date <= date.today().isoformat()
    return {'dates': dates, 'data': _calendar_rows_from_dates(dates, exchange), 'raw': raw,
            'complete': complete, 'calendar_mode': 'index_derived', 'strategy': 'index_derived',
            'upstream': 'iFinD MCP get_security_indicators (000001.SH 成交量)',
            'notes': _index_calendar_notes('iFinD MCP ', end_date)}


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
    data = []
    skipped = 0
    dataset = 'daily_prices' if scope == 'stock' else 'cb_daily'
    expected_dates = sorted(call.get('trade_dates') or ([call['start_date']] if call.get('start_date') == call.get('end_date') else []))
    default_date = expected_dates[0] if len(expected_dates) == 1 else None
    requested_fields = list(call['fields'])

    # First check for Choice native multi-security matrix sheets (either columns=symbols or items=symbols×dates).
    text_blocks = [c.get('text', '') for c in (raw.get('content') or []) if isinstance(c, dict) and c.get('type') == 'text']
    matrix_parsed = False
    for text in text_blocks:
        try:
            obj = json.loads(text)
        except Exception:
            continue
        sheets = obj.get('data') if isinstance(obj, dict) and isinstance(obj.get('data'), list) else []
        for sheet in sheets:
            cols = sheet.get('columns') if isinstance(sheet, dict) else None
            items = sheet.get('items') if isinstance(sheet, dict) else None
            if not (isinstance(cols, list) and isinstance(items, list) and cols and items):
                continue
            col_syms = [normalize_symbol(h) for h in cols[1:]]
            item_syms = [normalize_symbol(it[0]) if isinstance(it, list) and it else None for it in items]
            col_dates = [norm_date(re.sub(r'[（(]日[）)]$', '', str(h).strip())) for h in cols[1:]]
            if any(col_syms) and not any(col_dates):
                # Cross-section sheet: columns = [label, sym1, sym2, ...], items = [[field, v1, v2, ...], ...]
                matrix_parsed = True
                for col_idx, sym in enumerate(col_syms, 1):
                    if not sym or not symbol_matches_scope(sym, scope) or not default_date:
                        skipped += 1
                        continue
                    row_map = {str(it[0]): it[col_idx] for it in items if isinstance(it, list) and col_idx < len(it)}
                    row = {'symbol': sym, 'date': default_date}
                    for field in requested_fields:
                        row[field] = _extract_named_field(row_map, {}, field)
                    data.append(row)
            elif any(col_dates) and any(item_syms) and len(requested_fields) == 1:
                # Time-series sheet: columns = [label, date1, date2, ...], items = [[sym1, v1, v2, ...], ...]
                matrix_parsed = True
                field = requested_fields[0]
                for it in items:
                    if not isinstance(it, list) or not it:
                        continue
                    sym = normalize_symbol(it[0])
                    if not sym or not symbol_matches_scope(sym, scope):
                        skipped += 1
                        continue
                    for col_idx, d in enumerate(col_dates, 1):
                        if not d or col_idx >= len(it):
                            continue
                        val = number(it[col_idx])
                        if val is not None:
                            data.append({'symbol': sym, 'date': d, field: val})

    if not matrix_parsed:
        table = tables(raw)
        for source, units in table:
            symbol = symbol_for_row(source)
            actual = next((d for key in DATE_COLUMNS if (d := norm_date(source.get(key)))), None) or default_date
            if not symbol or not symbol_matches_scope(symbol, scope) or not actual:
                skipped += 1
                continue
            standard = [field for field in requested_fields if field in FIELDS[dataset]]
            mapped, _ = map_rows([(source, units)], standard, symbol, dataset, bindings=None)
            row = {'symbol': symbol, 'date': actual}
            row.update({field: mapped[0].get(field) if mapped else None for field in standard})
            for field in requested_fields:
                if row.get(field) is None:
                    row[field] = _extract_named_field(source, units, field)
            data.append(row)
    from .coverage import coverage
    cov = coverage(raw, data)
    expected_symbols = set(call.get('symbols') or [])
    actual_symbols = {row['symbol'] for row in data}
    missing_symbols = sorted(expected_symbols - actual_symbols)
    missing_fields = sorted({field for field in requested_fields if any(row.get(field) is None for row in data)})
    expected_date_set = set(call.get('trade_dates') or [])
    actual_dates = {row['date'] for row in data}
    missing_dates = sorted(expected_date_set - actual_dates)
    reported = cov['source_reported_rows']
    count_mismatch = reported is not None and reported != len(data)
    complete = not skipped and not missing_symbols and not missing_fields and not missing_dates and not count_mismatch and cov['status'] != 'partial'
    strategy = 'native_full' if not call.get('symbols') else 'native_batch'
    return _result(data, raw, strategy, complete, truncated=cov['status'] == 'partial' or count_mismatch, skipped_rows=skipped,
                   missing_symbols=missing_symbols, missing_fields=missing_fields, missing_dates=missing_dates,
                   source_reported_rows=reported, upstream='东方财富妙想 MCP', actual_trade_dates=sorted(actual_dates))


FIELD_CN_ALIASES = {
    'open': ('open', '开盘价', '今开'),
    'high': ('high', '最高价'),
    'low': ('low', '最低价'),
    'close': ('close', '收盘价', '最新价'),
    'volume': ('volume', 'vol', '成交量'),
    'amount': ('amount', '成交额', '成交金额'),
    'turnover_rate': ('turnover_rate', '换手率'),
    'pre_close': ('pre_close', '前收盘价', '昨收', '昨收盘价'),
    'pct_change': ('pct_change', 'pct_chg', '涨跌幅', '日涨跌幅'),
}


def _extract_named_field(row, units, field):
    names = FIELD_CN_ALIASES.get(field, (field,))
    val = _custom_field(row, units, names)
    if val is not None:
        return val
    targets = {_compact_column(n) for n in names}
    for key, raw_val in row.items():
        clean = re.sub(r'[（(][^）)]*[）)]', '', str(key))
        clean = re.sub(r'^20\d{2}年?(?:\d{1,2}月)?(?:\d{1,2}日)?', '', clean)
        if _compact_column(clean) in targets:
            unit = units.get(key, '')
            if not unit and isinstance(units.get('__indicator_params__'), dict):
                for k2, meta in units['__indicator_params__'].items():
                    if _compact_column(k2) in targets and isinstance(meta, dict):
                        unit = meta.get('单位', '')
                        break
            parsed = number(raw_val, unit if unit not in {'元', '股', '%'} else '')
            if parsed is not None:
                return parsed
    return None


def _ifind_batch_daily(remote, scope, call):
    symbols = list(call.get('symbols') or [])
    if not symbols:
        raise MarketUnsupported('iFinD 批量行情需要显式证券列表')
    fields = list(call['fields'])
    labels = {'open': '开盘价', 'high': '最高价', 'low': '最低价', 'close': '收盘价', 'volume': '成交量',
              'amount': '成交额', 'turnover_rate': '换手率', 'pre_close': '前收盘价', 'pct_change': '涨跌幅'}
    field_text = '、'.join(labels.get(f, f) for f in fields)
    sym_text = '、'.join(symbols)
    start_date, end_date = call['start_date'], call['end_date']
    if start_date == end_date:
        query = f'{sym_text} 在{start_date}的{field_text}'
    else:
        query = f'{sym_text} {start_date}至{end_date} 每日{field_text}'
    market = 'A股' if scope == 'stock' else '债券'
    raw = remote.call('get_security_indicators', {'market': market, 'query': query})
    _extract_ifind_payload(raw)
    data = []
    default_date = start_date if start_date == end_date else None
    for source, units in tables(raw):
        symbol = symbol_for_row(source)
        actual = next((d for key in DATE_COLUMNS if (d := norm_date(source.get(key)))), None) or default_date
        if not symbol or not symbol_matches_scope(symbol, scope) or not actual:
            continue
        if not (start_date <= actual <= end_date):
            continue
        row = {'symbol': symbol, 'date': actual}
        for field in fields:
            row[field] = _extract_named_field(source, units, field)
        data.append(row)
    return _result(data, raw, 'native_batch', True, upstream='iFinD MCP get_security_indicators (批量)',
                   actual_trade_dates=sorted({r['date'] for r in data}))


def _tdx_batch_cross_section(remote, scope, call):
    symbols = list(call.get('symbols') or [])
    start_date, end_date = call['start_date'], call['end_date']
    if scope != 'stock' or start_date != end_date or not symbols:
        raise MarketUnsupported('通达信批量截面仅支持单日 A 股查询')
    fields = list(call['fields'])
    labels = {'open': '开盘价', 'high': '最高价', 'low': '最低价', 'close': '收盘价', 'volume': '成交量',
              'amount': '成交额', 'turnover_rate': '换手率', 'pre_close': '前收盘价', 'pct_change': '涨跌幅'}
    codes = [s.split('.')[0] for s in symbols]
    query = f'{"、".join(codes)}在{start_date}的{"、".join(labels.get(f, f) for f in fields)}'
    raw = remote.call('tdx_indicator_select', {'message': query, 'rang': 'AG'})
    market_map = {'1': 'SH', '0': 'SZ', '2': 'BJ'}
    data = []
    for source, units in tables(raw):
        code = str(source.get('code') or source.get('sec_code') or '').strip()
        mkt = market_map.get(str(source.get('market') or '').strip())
        symbol = normalize_symbol(f'{code}.{mkt}' if mkt and code else code) or symbol_for_row(source)
        if not symbol or not symbol_matches_scope(symbol, scope):
            continue
        row = {'symbol': symbol, 'date': start_date}
        for field in fields:
            row[field] = _extract_named_field(source, units, field)
        data.append(row)
    return _result(data, raw, 'native_batch', True, upstream='通达信 MCP tdx_indicator_select (批量截面)',
                   actual_trade_dates=[start_date] if data else [])


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


def _akshare_universe(sdk, scope, call):
    include_delisted = call.get('include_delisted', True)
    merged = {}
    raw = {}
    notes = []

    def add_row(code_val, name_val=None, list_val=None, delist_val=None, market_hint=None):
        raw_code = str(code_val or '').strip()
        if market_hint and re.fullmatch(r'\d{6}', raw_code):
            raw_code = f'{raw_code}.{market_hint}'
        symbol = normalize_symbol(raw_code)
        if not symbol or not symbol_matches_scope(symbol, scope):
            return
        entry = merged.setdefault(symbol, {'symbol': symbol, 'name': None, 'list_date': None, 'delist_date': None})
        if name_val is not None and str(name_val).strip() and str(name_val).strip() not in {'nan', 'None', 'NaT'}:
            entry['name'] = entry['name'] or str(name_val).strip()
        ld = norm_date(list_val)
        if ld:
            entry['list_date'] = entry['list_date'] or ld
        dd = norm_date(delist_val)
        if dd:
            entry['delist_date'] = entry['delist_date'] or dd

    if scope == 'stock':
        exchange_ok = 0
        sources = [
            ('sh_main', lambda: sdk.stock_info_sh_name_code(symbol='主板A股'), '证券代码', '证券简称', '上市日期', None, 'SH'),
            ('sh_kcb', lambda: sdk.stock_info_sh_name_code(symbol='科创板'), '证券代码', '证券简称', '上市日期', None, 'SH'),
            ('sz_a', lambda: sdk.stock_info_sz_name_code(symbol='A股列表'), 'A股代码', 'A股简称', 'A股上市日期', None, 'SZ'),
            ('bj_a', lambda: sdk.stock_info_bj_name_code(), '证券代码', '证券简称', '上市日期', None, 'BJ'),
        ]
        if include_delisted:
            sources.extend([
                ('sh_delist', lambda: sdk.stock_info_sh_delist(), '公司代码', '公司简称', '上市日期', '暂停上市日期', 'SH'),
                ('sz_delist', lambda: sdk.stock_info_sz_delist(), '证券代码', '证券简称', '上市日期', '终止上市日期', 'SZ'),
            ])
        errors = []
        for label, loader, col_code, col_name, col_list, col_delist, mkt in sources:
            try:
                part = records(loader())
                raw[label] = {'count': len(part), 'sample': part[:3]}
                for item in part:
                    add_row(item.get(col_code), item.get(col_name), item.get(col_list),
                            item.get(col_delist) if col_delist else None, mkt)
                if label in {'sh_main', 'sh_kcb', 'sz_a', 'bj_a'}:
                    exchange_ok += 1
            except Exception as exc:
                errors.append(f'{label}: {exc}')
        if exchange_ok < 4:
            try:
                fallback = records(sdk.stock_info_a_code_name())
                raw['fallback_a_code_name'] = {'count': len(fallback), 'sample': fallback[:3]}
                for item in fallback:
                    add_row(item.get('code'), item.get('name'))
                notes.append('部分交易所分表拉取失败，已用 stock_info_a_code_name 补齐当前在市代码（无上市日期）。')
            except Exception as exc:
                errors.append(f'stock_info_a_code_name: {exc}')
        if errors:
            notes.append('AKShare 部分分表异常：' + '；'.join(errors[:3]))
        if include_delisted:
            notes.append('AKShare 包含上交所与深交所退市名单，不含北交所历史退市分表。')
        data = sorted(merged.values(), key=lambda row: row['symbol'])
        complete = bool(data) and exchange_ok == 4 and not errors
        return _result(data, raw, 'native_full', complete, truncated=not complete,
                       universe_complete=complete, current_complete=bool(data) and exchange_ok == 4,
                       conflicts=[], notes=notes, upstream='AKShare 交易所证券名单')

    # Convertible bonds
    upstream = 'AKShare bond_zh_cov'
    try:
        part = records(sdk.bond_zh_cov())
        raw['bond_zh_cov'] = {'count': len(part), 'sample': part[:3]}
        for item in part:
            add_row(item.get('债券代码'), item.get('债券简称'), item.get('上市时间'))
    except Exception as exc_em:
        notes.append(f'东方财富转债列表拉取失败（{exc_em}），已切换同花顺备用接口 bond_zh_cov_info_ths。')
        upstream = 'AKShare bond_zh_cov_info_ths'
        part = records(sdk.bond_zh_cov_info_ths())
        raw['bond_zh_cov_info_ths'] = {'count': len(part), 'sample': part[:3]}
        for item in part:
            add_row(item.get('债券代码'), item.get('债券简称'), item.get('上市日期'))
    notes.append('AKShare 可转债列表包含发行与在市转债，但未提供摘牌日期，不适用于精确历史时点过滤。')
    data = sorted(merged.values(), key=lambda row: row['symbol'])
    return _result(data, raw, 'native_full', bool(data), truncated=False,
                   universe_complete=False, current_complete=bool(data),
                   conflicts=[], notes=notes, upstream=upstream)


def _tdx_universe(remote, scope, call):
    if scope != 'stock':
        raise MarketUnsupported('通达信 MCP tdx_screener 不支持可转债全市场名单')
    from .parsing import markdown_rows
    market_map = {'1': 'SH', '0': 'SZ', '2': 'BJ'}
    merged = {}
    raw_pages = []
    total_reported = None
    page_size = 500
    for page in range(1, 21):
        raw = remote.call('tdx_screener', {'message': '全部A股', 'rang': 'AG', 'pageNo': page, 'pageSize': page_size})
        text = '\n'.join(c.get('text', '') for c in (raw.get('content') or []) if isinstance(c, dict))
        raw_pages.append({'page': page, 'length': len(text), 'head': text[:200]})
        if total_reported is None:
            match = re.search(r'共找到\s*(\d+)\s*条记录', text)
            if match:
                total_reported = int(match.group(1))
        page_rows = markdown_rows(text)
        if not page_rows:
            break
        for row in page_rows:
            code = str(row.get('sec_code') or '').strip()
            mkt = market_map.get(str(row.get('market') or '').strip())
            symbol = normalize_symbol(f'{code}.{mkt}' if mkt and code else code)
            if not symbol or not symbol_matches_scope(symbol, 'stock'):
                continue
            merged[symbol] = {
                'symbol': symbol,
                'name': (row.get('sec_name') or '').strip() or None,
                'list_date': None,
                'delist_date': None,
            }
        if total_reported is not None and len(merged) >= total_reported:
            break
        if len(page_rows) < page_size:
            break
    data = sorted(merged.values(), key=lambda row: row['symbol'])
    current_complete = bool(data) and (total_reported is None or len(data) >= total_reported)
    notes = ['通达信 MCP tdx_screener 返回当前在市 A 股名单，不含上市日期、退市日期及已退市证券。']
    return _result(data, raw_pages, 'native_paged', current_complete,
                   truncated=not current_complete, source_reported_rows=total_reported or len(data),
                   universe_complete=False, current_complete=current_complete,
                   conflicts=[], notes=notes, upstream='通达信 MCP tdx_screener')


IFIND_STOCK_SLICES = (
    ('sh_main_early', '上证主板上市A股，上市日期早于2016-01-01，返回股票代码、简称、上市日期'),
    ('sh_main_recent', '上证主板上市A股，上市日期晚于2015-12-31，返回股票代码、简称、上市日期'),
    ('sh_kcb', '科创板上市A股，返回股票代码、简称、上市日期'),
    ('sz_main_early', '深证主板上市A股，上市日期早于2010-01-01，返回股票代码、简称、上市日期'),
    ('sz_main_recent', '深证主板上市A股，上市日期晚于2009-12-31，返回股票代码、简称、上市日期'),
    ('sz_cyb_early', '创业板上市A股，上市日期早于2021-01-01，返回股票代码、简称、上市日期'),
    ('sz_cyb_recent', '创业板上市A股，上市日期晚于2020-12-31，返回股票代码、简称、上市日期'),
    ('bj', '北交所上市A股，返回股票代码、简称、上市日期'),
)


def _fetch_ifind_csv(url, timeout=20):
    import csv
    import io
    from urllib.parse import urlsplit
    import requests
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname != 'o.thsi.cn':
        raise RuntimeError(f'拒绝非官方 iFinD 导出链接：{url}')
    resp = requests.get(url, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f'下载 iFinD CSV 失败：HTTP {resp.status_code}')
    return list(csv.DictReader(io.StringIO(resp.content.decode('utf-8-sig', errors='replace'))))


def _extract_ifind_payload(raw):
    text = '\n'.join(c.get('text', '') for c in (raw.get('content') or []) if isinstance(c, dict))
    reported = None
    search_text = text
    try:
        parsed_json = json.loads(text)
        data_node = parsed_json.get('data', {})
        reported = data_node.get('selectedSecuritiesCount')
        answer_node = data_node.get('answer')
        if isinstance(answer_node, str):
            search_text = answer_node
        elif isinstance(answer_node, dict):
            search_text = '\n'.join(str(v) for v in answer_node.values())
    except Exception:
        pass
    if '请求用量已耗尽' in search_text or '请求过于频繁' in search_text:
        raise RuntimeError(f'iFinD MCP 限流或额度耗尽：{search_text[:160]}')
    if reported is None:
        m = re.search(r'前\s*(\d+)\s*行', search_text)
        if m:
            reported = int(m.group(1))
    csv_match = re.search(r'https?://o\.thsi\.cn/\S+\.csv', search_text)
    csv_url = csv_match.group(0).rstrip('.,;)') if csv_match else None
    return reported, csv_url, search_text


def _add_ifind_rows(merged, rows, scope):
    for row in rows:
        symbol = symbol_for_row(row)
        if not symbol or not symbol_matches_scope(symbol, scope):
            continue
        merged[symbol] = {
            'symbol': symbol,
            'name': pick(row, ('股票简称', '债券简称', '代码简称', '证券简称', '简称', 'name')),
            'list_date': next((d for k in ('新股上市日期', '上市日期', 'list_date') if (d := norm_date(row.get(k)))), None),
            'delist_date': next((d for k in ('退市日期', '摘牌日期', 'delist_date') if (d := norm_date(row.get(k)))), None),
        }


def _ifind_universe(remote, scope, call):
    import time
    merged = {}
    notes = []
    if scope == 'convertible_bond':
        raw = remote.call('market_statistics', {'market': '债券', 'query': '全部上市可转债代码、简称、上市日期、摘牌日期'})
        reported, csv_url, _ = _extract_ifind_payload(raw)
        csv_rows = None
        if csv_url:
            csv_rows = _fetch_ifind_csv(csv_url)
            _add_ifind_rows(merged, csv_rows, scope)
            notes.append(f'已通过 iFinD 官方导出 CSV 获取全量可转债记录（原始 {len(csv_rows)} 行，标准代码 {len(merged)} 只）。')
        else:
            _add_ifind_rows(merged, [row for row, _ in tables(raw)], scope)
        data = sorted(merged.values(), key=lambda row: row['symbol'])
        full_fetched = bool(csv_rows is not None and data and (reported is None or len(csv_rows) >= reported))
        return _result(data, {'csv_url': csv_url, 'csv_row_count': len(csv_rows) if csv_rows is not None else 0, 'raw': raw},
                       'csv_export' if csv_rows is not None else 'partial_mcp', full_fetched,
                       truncated=not full_fetched, source_reported_rows=reported or len(data),
                       universe_complete=False, current_complete=full_fetched,
                       conflicts=[], notes=notes, upstream='iFinD MCP market_statistics (CSV 导出)')

    # A-shares: slice across 8 board+listing-date buckets (each < 1000 rows) and download each CSV.
    raw_slices = []
    total_reported = 0
    all_slices_ok = True
    any_truncated = False
    for index, (label, query) in enumerate(IFIND_STOCK_SLICES):
        if index > 0 and call.get('slice_delay', 1.2) > 0:
            time.sleep(call.get('slice_delay', 1.2))
        try:
            raw = remote.call('search_securities', {'market': '股票', 'query': query})
            reported, csv_url, _ = _extract_ifind_payload(raw)
            if csv_url:
                slice_rows = _fetch_ifind_csv(csv_url)
                _add_ifind_rows(merged, slice_rows, scope)
                fetched_count = len(slice_rows)
            else:
                inline_rows = [row for row, _ in tables(raw)]
                _add_ifind_rows(merged, inline_rows, scope)
                fetched_count = len(inline_rows)
            if reported is not None:
                total_reported += reported
                if fetched_count < reported:
                    any_truncated = True
            raw_slices.append({'slice': label, 'reported': reported, 'fetched': fetched_count, 'csv_url': csv_url})
        except Exception as exc:
            all_slices_ok = False
            raw_slices.append({'slice': label, 'error': str(exc)})
            notes.append(f'iFinD 分片 {label} 失败：{exc}')
    data = sorted(merged.values(), key=lambda row: row['symbol'])
    if not data and not all_slices_ok:
        raise RuntimeError('；'.join(notes) or 'iFinD A 股分片拉取全部失败')
    current_complete = bool(data) and all_slices_ok and not any_truncated
    notes.append('iFinD A 股通过沪深京 8 个板块/上市日期分片并下载官方 CSV 合并获取当前在市名单，不含历史已退市股票。')
    return _result(data, raw_slices, 'sliced_csv_export', current_complete,
                   truncated=not current_complete, source_reported_rows=total_reported or len(data),
                   universe_complete=False, current_complete=current_complete,
                   conflicts=[], notes=notes, upstream='iFinD MCP search_securities (分片 CSV 导出)')


def _wind_universe(remote, scope, call):
    raise MarketUnsupported('Wind MCP search_stocks 单次硬上限 100 条且无正式分页，不支持全市场证券名单')


def _normalize_search_text(text):
    if text is None:
        return ''
    s = str(text).strip().upper()
    s = s.replace('Ａ', 'A').replace('Ｂ', 'B').replace('＊', '*')
    return re.sub(r'\s+', '', s)


def _is_code_query(query):
    clean = _normalize_search_text(query)
    return bool(re.fullmatch(r'(?:[A-Z])?\d{3,6}(?:\.(?:SH|SZ|BJ))?', clean))


def _filter_search_candidates(rows, query, scope, limit):
    q_norm = _normalize_search_text(query)
    q_digits = re.sub(r'\.(?:SH|SZ|BJ)$', '', q_norm)
    dedup = {}
    for row in rows:
        symbol = normalize_symbol(row.get('symbol'))
        if not symbol:
            continue
        is_cb = bool(re.fullmatch(r'(?:11\d{4}\.SH|12\d{4}\.SZ)', symbol))
        row_scope = 'convertible_bond' if is_cb else 'stock'
        if scope != 'all' and row_scope != scope:
            continue
        name = (row.get('name') or '').strip() or None
        aliases = row.get('aliases') or []
        sym_norm = _normalize_search_text(symbol)
        code_only = sym_norm.split('.')[0]
        name_norm = _normalize_search_text(name)
        alias_norms = [_normalize_search_text(a) for a in aliases if a]

        rank = None
        match_type = None
        if q_norm in {sym_norm, code_only} or (name_norm and q_norm == name_norm) or (q_digits and q_digits == code_only):
            rank, match_type = 0, 'exact'
        elif sym_norm.startswith(q_norm) or code_only.startswith(q_digits) or (name_norm and name_norm.startswith(q_norm)):
            rank, match_type = 1, 'prefix'
        elif q_norm in sym_norm or (q_digits and q_digits in code_only) or (name_norm and q_norm in name_norm):
            rank, match_type = 2, 'contains'
        elif any(q_norm in a for a in alias_norms):
            rank, match_type = 3, 'alias'
        else:
            continue

        existing = dedup.get(symbol)
        if existing and existing['_rank'] <= rank:
            continue
        exchange = symbol.rsplit('.', 1)[1] if '.' in symbol else ''
        status = 'delisted' if row.get('delist_date') else 'listed'
        dedup[symbol] = {
            '_rank': rank,
            'symbol': symbol,
            'name': name,
            'scope': row_scope,
            'exchange': exchange,
            'list_date': row.get('list_date'),
            'delist_date': row.get('delist_date'),
            'status': status,
            'match_type': match_type,
        }
    ranked = sorted(dedup.values(), key=lambda r: (r['_rank'], 0 if r['status'] == 'listed' else 1, r['symbol']))
    total = len(ranked)
    cleaned = [{k: v for k, v in item.items() if k != '_rank'} for item in ranked[:limit]]
    return cleaned, total


def _tushare_search_securities(sdk, scope, call):
    query = call['query']
    limit = int(call.get('limit') or 20)
    include_delisted = bool(call.get('include_delisted'))
    scopes = ['stock', 'convertible_bond'] if scope == 'all' else [scope]
    candidates = []
    raw = {}
    for sc in scopes:
        if sc == 'stock':
            statuses = ('L', 'D', 'P') if include_delisted else ('L',)
            for st in statuses:
                part = records(sdk.stock_basic(exchange='', list_status=st, fields='ts_code,name,list_date,delist_date'))
                raw[f'stock_{st}'] = len(part)
                for item in part:
                    candidates.append({
                        'symbol': item.get('ts_code'), 'name': item.get('name'),
                        'list_date': norm_date(item.get('list_date')),
                        'delist_date': norm_date(item.get('delist_date')),
                    })
        else:
            part = records(sdk.cb_basic(fields='ts_code,bond_short_name,list_date,delist_date'))
            raw['cb_basic'] = len(part)
            for item in part:
                if not include_delisted and norm_date(item.get('delist_date')):
                    continue
                candidates.append({
                    'symbol': item.get('ts_code'), 'name': item.get('bond_short_name'),
                    'list_date': norm_date(item.get('list_date')),
                    'delist_date': norm_date(item.get('delist_date')),
                })
    matched, total = _filter_search_candidates(candidates, query, scope, limit)
    return _result(matched, raw, 'directory_search', True, total_matches=total, upstream='Tushare stock_basic / cb_basic')


def _akshare_search_securities(sdk, scope, call):
    query = call['query']
    limit = int(call.get('limit') or 20)
    include_delisted = bool(call.get('include_delisted'))
    scopes = ['stock', 'convertible_bond'] if scope == 'all' else [scope]
    candidates = []
    raw = {}
    notes = []
    for sc in scopes:
        if sc == 'stock':
            if include_delisted:
                uni = _akshare_universe(sdk, 'stock', {'include_delisted': True})
                candidates.extend(uni['data'])
                raw['stock_universe'] = len(uni['data'])
                notes.extend(uni.get('notes') or [])
            else:
                part = records(sdk.stock_info_a_code_name())
                raw['stock_info_a_code_name'] = len(part)
                for item in part:
                    candidates.append({'symbol': item.get('code'), 'name': item.get('name'),
                                       'list_date': None, 'delist_date': None})
        else:
            uni = _akshare_universe(sdk, 'convertible_bond', {'include_delisted': include_delisted})
            candidates.extend(uni['data'])
            raw['cb_universe'] = len(uni['data'])
            notes.extend(uni.get('notes') or [])
    matched, total = _filter_search_candidates(candidates, query, scope, limit)
    return _result(matched, raw, 'directory_search', True, total_matches=total, notes=notes,
                   upstream='AKShare stock_info_a_code_name / bond_zh_cov')


def _wind_search_securities(remote, scope, call):
    query = call['query'].strip()
    limit = int(call.get('limit') or 20)
    is_code = _is_code_query(query)
    normalized_code = normalize_symbol(query) if is_code else None
    questions = []
    if is_code and normalized_code:
        questions.append(normalized_code)
    else:
        if scope in {'all', 'stock'}:
            questions.append(f'名称包含{query}的上市A股')
        if scope in {'all', 'convertible_bond'}:
            questions.append(f'名称包含{query}的可转债')
    candidates = []
    raws = []
    for q in questions:
        raw = remote.call('search_stocks', {'question': q})
        raws.append({'question': q, 'raw': raw})
        for row, _ in tables(raw):
            symbol = symbol_for_row(row)
            if not symbol:
                continue
            candidates.append({
                'symbol': symbol,
                'name': pick(row, ('证券简称', '股票简称', '债券简称', '简称', 'name')),
                'list_date': next((d for k in ('上市日期', 'list_date') if (d := norm_date(row.get(k)))), None),
                'delist_date': next((d for k in ('退市日期', '摘牌日期', 'delist_date') if (d := norm_date(row.get(k)))), None),
            })
    matched, total = _filter_search_candidates(candidates, query, scope, limit)
    return _result(matched, raws, 'native_search', True, total_matches=total, upstream='Wind MCP search_stocks')


def _tdx_search_securities(remote, scope, call):
    if scope == 'convertible_bond':
        raise MarketUnsupported('通达信 MCP tdx_lookup_stock 不支持可转债检索')
    query = call['query'].strip()
    limit = int(call.get('limit') or 20)
    clean_query = re.sub(r'\.(?:SH|SZ|BJ)$', '', query.upper()) if _is_code_query(query) else query
    raw = remote.call('tdx_lookup_stock', {'query': clean_query, 'range': 'AG'})
    text = '\n'.join(c.get('text', '') for c in (raw.get('content') or []) if isinstance(c, dict))
    market_map = {'1': 'SH', '0': 'SZ', '2': 'BJ'}
    candidates = []
    blocks = re.split(r'(?m)^\s*\d+\.\s*', text)
    for block in blocks[1:]:
        head = re.search(r'【(\d{6})】\s*([^\n]+)', block)
        meta = re.search(r'类型:\s*([^\|\n]+)\|\s*setcode:\s*(\d+)', block)
        if not head or not meta:
            continue
        kind = meta.group(1).strip()
        setcode = meta.group(2).strip()
        if 'A股' not in kind or setcode not in market_map:
            continue
        code, name = head.group(1), head.group(2).strip()
        alias_match = re.search(r'别名:\s*([^\n]+)', block)
        aliases = [a.strip().rstrip('.') for a in alias_match.group(1).split(',') if a.strip()] if alias_match else []
        candidates.append({
            'symbol': f'{code}.{market_map[setcode]}',
            'name': name,
            'list_date': None,
            'delist_date': None,
            'aliases': aliases,
        })
    notes = ['通达信 MCP tdx_lookup_stock 仅支持 A 股检索，不含可转债。'] if scope == 'all' else []
    matched, total = _filter_search_candidates(candidates, query, 'stock', limit)
    return _result(matched, raw, 'native_search', True, total_matches=total, notes=notes,
                   upstream='通达信 MCP tdx_lookup_stock')


def _ifind_search_securities(remote, scope, call):
    query = call['query'].strip()
    limit = int(call.get('limit') or 20)
    is_code = _is_code_query(query)
    normalized_code = normalize_symbol(query) if is_code else None
    candidates = []
    raws = []
    if scope in {'all', 'stock'}:
        if is_code and normalized_code:
            raw = remote.call('get_security_indicators', {'market': 'A股', 'query': f'{normalized_code} 股票代码、股票简称、上市日期'})
        else:
            raw = remote.call('search_securities', {'market': '股票', 'query': f'股票简称包含{query}的A股，返回股票代码、简称、上市日期'})
        _extract_ifind_payload(raw)
        raws.append({'scope': 'stock', 'raw': raw})
        for row, _ in tables(raw):
            symbol = symbol_for_row(row)
            if symbol:
                candidates.append({
                    'symbol': symbol,
                    'name': pick(row, ('股票简称', '证券简称', '代码简称', '简称', 'name')),
                    'list_date': next((d for k in ('新股上市日期', '上市日期', 'list_date') if (d := norm_date(row.get(k)))), None),
                    'delist_date': None,
                })
    if scope in {'all', 'convertible_bond'}:
        raw = remote.call('get_security_indicators', {'market': '债券', 'query': f'{query} 可转债代码、简称、上市日期'})
        _extract_ifind_payload(raw)
        raws.append({'scope': 'convertible_bond', 'raw': raw})
        for row, _ in tables(raw):
            symbol = symbol_for_row(row)
            if symbol:
                candidates.append({
                    'symbol': symbol,
                    'name': pick(row, ('债券简称', '证券简称', '代码简称', '简称', 'name')),
                    'list_date': next((d for k in ('上市日期', 'list_date') if (d := norm_date(row.get(k)))), None),
                    'delist_date': next((d for k in ('摘牌日期', '退市日期', 'delist_date') if (d := norm_date(row.get(k)))), None),
                })
    matched, total = _filter_search_candidates(candidates, query, scope, limit)
    return _result(matched, raws, 'native_search', True, total_matches=total,
                   upstream='iFinD MCP search_securities / get_security_indicators')


def _choice_search_securities(remote, scope, call):
    query = call['query'].strip()
    limit = int(call.get('limit') or 20)
    candidates = []
    raws = []
    if scope in {'all', 'stock'}:
        raw = remote.call('mx_stocks_screener', {'query': f'代码或简称包含{query}的A股，返回证券代码、证券简称、上市日期'})
        raws.append({'scope': 'stock', 'raw': raw})
        for row, _ in tables(raw):
            symbol = symbol_for_row(row)
            if symbol:
                candidates.append({
                    'symbol': symbol,
                    'name': pick(row, ('名称', '股票简称', '证券简称', '简称', 'name')),
                    'list_date': next((d for k in ('上市日期', 'list_date') if (d := norm_date(row.get(k)))), None),
                    'delist_date': None,
                })
    if scope in {'all', 'convertible_bond'}:
        raw = remote.call('mx_bond_finance_data', {'query': f'{query} 可转债证券代码、债券简称、上市日期'})
        raws.append({'scope': 'convertible_bond', 'raw': raw})
        for row, _ in tables(raw):
            symbol = symbol_for_row(row)
            if symbol:
                candidates.append({
                    'symbol': symbol,
                    'name': pick(row, ('债券简称', '证券简称', '名称', '简称', 'name')),
                    'list_date': next((d for k in ('上市日期', 'list_date') if (d := norm_date(row.get(k)))), None),
                    'delist_date': None,
                })
    matched, total = _filter_search_candidates(candidates, query, scope, limit)
    return _result(matched, raws, 'native_search', True, total_matches=total,
                   upstream='东方财富妙想 MCP mx_stocks_screener / mx_bond_finance_data')


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
    scope = call.get('scope', 'stock')
    if name == 'tushare':
        import tushare as ts
        sdk = ts.pro_api(key)
        if operation == 'universe':
            return _tushare_universe(sdk, scope, call)
        if operation == 'search_securities':
            return _tushare_search_securities(sdk, scope, call)
        if operation == 'calendar':
            return _tushare_calendar(sdk, call)
        if operation == 'daily':
            return _tushare_daily(sdk, scope, call)
    if name == 'akshare':
        import akshare as ak
        if operation == 'calendar':
            return _akshare_calendar(ak, call)
        if operation == 'universe':
            return _akshare_universe(ak, scope, call)
        if operation == 'search_securities':
            return _akshare_search_securities(ak, scope, call)
        if operation == 'daily':
            return _scalar_daily(name, scope, call, sdk=ak)
    if name in {'choice', 'wind', 'ifind', 'tdx'}:
        from .providers import Remote
        remote = Remote(name, p, key)
        if operation == 'calendar':
            return {'choice': _choice_calendar, 'wind': _wind_calendar,
                    'ifind': _ifind_calendar, 'tdx': _tdx_calendar}[name](remote, call)
        if operation == 'universe':
            return {'choice': _choice_universe, 'wind': _wind_universe,
                    'ifind': _ifind_universe, 'tdx': _tdx_universe}[name](remote, scope, call)
        if operation == 'search_securities':
            return {'choice': _choice_search_securities, 'wind': _wind_search_securities,
                    'ifind': _ifind_search_securities, 'tdx': _tdx_search_securities}[name](remote, scope, call)
        if operation == 'daily':
            if name == 'choice':
                return _choice_daily(remote, scope, call)
            return _scalar_daily(name, scope, call, remote=remote)
        if operation == 'batch_daily':
            if name == 'choice':
                return _choice_daily(remote, scope, call)
            if name == 'ifind':
                return _ifind_batch_daily(remote, scope, call)
            if name == 'tdx':
                return _tdx_batch_cross_section(remote, scope, call)
            return _scalar_daily(name, scope, call, remote=remote)
    raise MarketUnsupported(f'{name}未接入市场操作 {operation}')
