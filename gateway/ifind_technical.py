"""iFinD native daily technical indicators; verify returned parameters and dates.

This transport never computes indicators or substitutes high-frequency quotes.
The NL endpoint sometimes returns extra components or silently omits an indicator.
Only exact dictionary columns with their own matching parameter evidence are used.
"""
import re
from .parsing import json_objects, markdown_rows, norm_date, number


def payloads(raw):
    """Return actual answer blocks, excluding arbitrary request metadata."""
    out=[]
    def visit(value):
        if isinstance(value,dict):
            if isinstance(value.get('answer'),str) and isinstance(value.get('indicators_params'),dict):
                out.append(value);return
            for key in ('content','data','structuredContent'):
                if key in value:visit(value[key])
            if isinstance(value.get('text'),str):
                for obj in json_objects(value['text']):visit(obj)
        elif isinstance(value,list):
            for item in value:visit(item)
    visit(raw)
    return out


def clean(value):
    return re.sub(r'[\s_\ue000（）():：,，]','',str(value)).upper()


def resolved(q,recipe,bindings):
    tech=recipe['technical'];pid=next(iter(bindings.values()))['recipe']
    defaults={**tech['default_parameters'],**next(iter(bindings.values())).get('parameters',{})}
    params={**defaults,**q.get('resolved_parameters',{}).get(pid,q.get('parameters') or {})}
    schema=tech['parameter_schema']
    if set(params)!=set(schema):raise ValueError('iFinD技术指标参数不匹配')
    for key,value in params.items():
        rule=schema[key]
        if type(value) is not int or not rule['min']<=value<=rule['max']:raise ValueError('iFinD技术指标参数须为允许范围内的整数')
    return params


def fetch(remote,q,bindings,recipe):
    tech=recipe['technical'];params=resolved(q,recipe,bindings)
    if q.get('adjust','native') not in ('native','none'):raise ValueError('iFinD技术指标本方案仅支持不复权日线')
    if q.get('frequency','daily')!='daily':raise ValueError('iFinD技术指标本方案仅支持日线')
    specs=[]
    for binding in bindings.values():
        parts=[key+'='+str(params[p]) for p,key in tech['parameter_keys'].items()]
        parts.extend(key+'='+str(value) for key,value in binding.get('required_options',{}).items())
        specs.append(binding['indicator_name']+'（'+'，'.join(parts)+'）')
    question=('查询'+q['symbol']+'在'+q['start_date']+'至'+q['end_date']+'逐交易日的日线技术指标：'+'；'.join(dict.fromkeys(specs))+
              '。复权方式=不复权，计算周期=日。使用iFinD原生技术指标，不要自行计算或替换成普通行情。只返回指定分量，保留证券代码、日期、指标参数及单位。')
    raw=remote.call(recipe['method'],{'market':'A股','query':question})
    blocks=payloads(raw)
    if not blocks:raise RuntimeError('iFinD技术指标缺少结果或参数证据')
    merged={};matched=set()
    for block in blocks:
        for field,binding in bindings.items():
            evidence=[values for name,values in block['indicators_params'].items()
                      if (clean(name)==clean(binding['indicator_name']) or str(name).startswith(binding['indicator_name']+'__')) and isinstance(values,dict)
                      and all(str(values.get(k))==str(v) for k,v in binding.get('required_options',{}).items())]
            if len(evidence)!=1:continue
            used=evidence[0]
            if used.get('复权方式')!='不复权' or used.get('计算周期')!='日':raise RuntimeError('iFinD技术指标复权方式或频率与请求不一致')
            if any(str(used.get(key))!=str(params[p]) for p,key in tech['parameter_keys'].items()):raise RuntimeError('iFinD技术指标参数与请求不一致')
            for item in markdown_rows(block['answer']):
                keys=[key for key in item if clean(key) in {clean(c) for c in binding['columns']}]
                if not keys:continue
                if len(keys)!=1:raise RuntimeError('iFinD技术指标分量列有歧义')
                if item.get('证券代码')!=q['symbol']:raise RuntimeError('iFinD技术指标返回证券与请求不一致')
                day=norm_date(item.get('日期'))
                if not day:raise RuntimeError('iFinD技术指标缺少显式表格日期')
                if not q['start_date']<=day<=q['end_date']:raise RuntimeError('iFinD技术指标返回日期超出请求范围')
                value_text=str(item[keys[0]]).strip().replace(',','')
                if value_text not in ('','--','—','None','nan','N/A') and not re.fullmatch(r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?',value_text):raise RuntimeError('iFinD技术指标含未经确认的单位或缩放')
                value=number(value_text)
                row=merged.setdefault(day,{'symbol':q['symbol'],'date':day})
                if field in row and row[field]!=value:raise RuntimeError('iFinD技术指标同日同分量出现不同值，不能确定归属')
                row[field]=value;matched.add(field)
    missing=set(bindings)-matched
    if missing:raise RuntimeError('iFinD未返回可核对参数的技术指标：'+','.join(sorted(missing)))
    return [merged[day] for day in sorted(merged)],raw
