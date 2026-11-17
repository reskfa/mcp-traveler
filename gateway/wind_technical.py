"""Wind native technical snapshots; never fill historical dates or compute values."""
from .parsing import json_objects, norm_date, number


def _data(raw):
    for item in raw.get('content', []):
        for obj in json_objects(item.get('text', '')):
            if isinstance(obj, dict) and isinstance(obj.get('data'), dict):
                data = obj['data']
                if isinstance(data.get('columns'), list) and isinstance(data.get('rows'), list):
                    return data
    raise RuntimeError('Wind技术快照缺少结构化数据表')


def fetch(remote, q, bindings, recipe=None):
    if q.get('parameters'):
        raise ValueError('Wind技术快照使用厂商原生固定参数，不支持parameters覆盖')
    if q.get('adjust', 'native') != 'native':
        raise ValueError('Wind技术快照未披露复权方式，只支持native')
    selected = list(dict.fromkeys(b['columns'][0] for b in bindings.values()))
    raw = remote.call('get_stock_price_indicators', {
        'windcode': q['symbol'],
        'indexes': ','.join(['最新交易日', '交易时间', *selected]),
    })
    data = _data(raw)
    columns = [c['name'] for c in data['columns']]
    if len(columns) != len(set(columns)):
        raise RuntimeError('Wind技术快照存在重复列')
    units = data.get('unit', {})
    out = []
    for values in data['rows']:
        if len(values) != len(columns):
            raise RuntimeError('Wind技术快照维度不一致')
        row = dict(zip(columns, values))
        if row.get('Wind代码') != q['symbol']:
            raise RuntimeError('Wind技术快照返回证券不匹配')
        day = norm_date(row.get('最新交易日'))
        if not day:
            raise RuntimeError('Wind技术快照未返回实际交易日')
        timestamp = row.get('交易时间')
        if not timestamp or norm_date(timestamp) != day:
            raise RuntimeError('Wind技术快照交易时间与交易日不一致')
        if (q.get('start_date') and day < q['start_date']) or (q.get('end_date') and day > q['end_date']):
            continue
        result = {'symbol': q['symbol'], 'date': day}
        for field, binding in bindings.items():
            column = binding['columns'][0]
            if column not in row:
                raise RuntimeError('Wind技术快照缺少请求列：' + column)
            if units.get(column) != binding['source_unit'] and not (binding.get('unit_inferred') and not units.get(column)):
                raise RuntimeError('Wind技术快照单位缺失或不匹配：' + column)
            result[field] = number(row[column])
        out.append(result)
    if not out:
        raise RuntimeError('Wind仅提供最新技术快照，实际交易日不在请求区间，不能用于历史补值')
    return out, raw
