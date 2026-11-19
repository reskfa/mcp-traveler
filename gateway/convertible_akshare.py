"""AKShare CB adapters: dated history and explicitly undated current profiles."""
from datetime import date
import requests
from .basic_info import typed, pick
from .dictionary import fields
from .parsing import norm_date


def value_history(symbol, get=None):
    """Named JSON repair for AKShare's positional 13-column parser (now 15)."""
    get = get or requests.get
    params = {'sty':'ALL','st':'date','sr':'1','source':'WEB','type':'RPTA_WEB_KZZ_LS',
              'filter':f'(zcode="{symbol.split(".")[0]}")','p':'1','ps':'8000'}
    response = get('https://datacenter-web.eastmoney.com/api/data/get', params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    result = payload.get('result')
    if not isinstance(result, dict) or not isinstance(result.get('data'), list):
        raise RuntimeError('东财转债价值分析未返回有效数据表')
    if result.get('pages', 1) > 1:
        raise RuntimeError('东财转债价值分析超过8000条上限，不能保证完整历史')
    return result['data'], payload


def fetch(recipe, bindings, q, sdk, remote=None):
    from .acquisition import records, context, parameters
    args = parameters(recipe, context(q, recipe, bindings), q)
    if recipe.get('named_value_endpoint'):
        rows, payload = value_history(q['symbol'])
        raw = [payload]
    else:
        rows = records(getattr(sdk, recipe['method'])(**args))
        raw = [rows]
    if recipe.get('identity_column'):
        rows = [r for r in rows if str(r.get(recipe['identity_column'], '')).zfill(6) == q['symbol'].split('.')[0]]
    if recipe.get('shape') == 'item_value':
        item = {r['item']:r.get('value') for r in rows if 'item' in r}
        identity = item.get('债券代码')
        if identity and str(identity).lower() != q['symbol'].split('.')[1].lower()+q['symbol'].split('.')[0]:
            raise RuntimeError('新浪转债资料证券身份不一致')
        if recipe.get('require_identity') and not identity:
            raise RuntimeError('新浪转债资料缺少证券身份')
        # The summary endpoint is scoped by its symbol URL, but carries no code.
        rows = [item] if item else []
    out = []
    for item in rows:
        if recipe.get('named_value_endpoint') and (item.get('SECUCODE') != q['symbol'] or str(item.get('ZCODE')) != q['symbol'].split('.')[0]):
            raise RuntimeError('东财转债价值分析证券身份不一致')
        day = None
        if recipe.get('temporal') != 'snapshot':
            day = norm_date(pick(item, recipe.get('date_columns', [])))
            if not day: raise RuntimeError('转债历史缺少明确交易日期')
            date.fromisoformat(day)
            if not q['start_date'] <= day <= q['end_date']: continue
        row = {'symbol':q['symbol'], 'date':day}
        for field, binding in bindings.items():
            value = pick(item,binding['columns'])
            if binding.get('split') and value is not None:
                parts = str(value).split(binding['split'])
                value = parts[binding.get('index',0)] if len(parts)>binding.get('index',0) else None
            if binding.get('a_share_code') and value is not None:
                code = str(value).zfill(6)
                value = code + ('.SH' if code.startswith('6') else '.BJ' if code.startswith(('4','8','9')) else '.SZ')
            row[field] = typed(value, fields(q['dataset'])[field]['data_type'], {k:v for k,v in binding.items() if k not in ('split','index')})
        out.append(row)
    return out, raw
