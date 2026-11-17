"""Dictionary-driven Wind historical technical queries; no local indicators."""
import math
import re
from .parsing import json_objects, norm_date
from .technical import normalized_parameters


def tables(raw):
    """Wind may return one table or several component-specific tables."""
    if isinstance(raw,dict):
        if raw.get('error') or raw.get('isError'):
            raise RuntimeError('Wind历史技术指标返回上游错误')
        if isinstance(raw.get('columns'),list) and isinstance(raw.get('rows'),list):
            yield raw
            return
        for key in ('data','structuredContent','content'):
            if key in raw:yield from tables(raw[key])
        if isinstance(raw.get('text'),str):
            for obj in json_objects(raw['text']):yield from tables(obj)
    elif isinstance(raw,list):
        for item in raw:yield from tables(item)


def suffix(column):
    # Remove only the date-range prefix, not arbitrary indicator words.
    return re.sub(r'^\d{4}(?:年\d{1,2}月\d{1,2}日|[-/]\d{1,2}[-/]\d{1,2})(?:(?:至|到|—|~)\d{4}(?:年\d{1,2}月\d{1,2}日|[-/]\d{1,2}[-/]\d{1,2}))?(?:的)?(?:每日|逐日|各交易日)(?:的)?','',column).strip()


def cn_date(day):
    year,month,date=day.split('-')
    return f'{year}年{int(month)}月{int(date)}日'


def fetch(remote,q,bindings,recipe):
    if not q.get('start_date') or not q.get('end_date'):raise ValueError('Wind历史技术指标需要日期范围')
    if q.get('frequency','daily')!='daily' or q.get('adjust','native')!='native':raise ValueError('Wind历史技术指标使用原生日线口径，复权方式以返回为准')
    pid=next(iter(bindings.values()))['recipe']
    tech=recipe['technical']
    if tech.get('fixed') and q.get('parameters'):raise ValueError('Wind本方案使用来源默认参数，不支持参数覆盖')
    params=normalized_parameters(q.get('resolved_parameters',{}).get(pid,q.get('parameters')),pid)
    context={**params,'symbol':q['symbol'],'start_cn':cn_date(q['start_date']),'end_cn':cn_date(q['end_date'])}
    question=recipe['query_template'].format_map(context)
    raw=remote.call(recipe['method'],{'question':question})
    merged={};matched=set();found_table=False
    for table in tables(raw):
        found_table=True
        columns=[c['name'] if isinstance(c,dict) else c for c in table['columns']]
        if len(columns)!=len(set(columns)):raise RuntimeError('Wind历史技术指标有重复列')
        for field,binding in bindings.items():
            expected={c.format_map(context) for c in binding['columns']}
            candidates=[c for c in columns if suffix(c) in expected]
            if not candidates:continue
            if len(candidates)!=1:raise RuntimeError('Wind历史技术指标分量列有歧义')
            column=candidates[0]
            date_column=column+'时间' if column+'时间' in columns else '日期'
            if date_column not in columns:raise RuntimeError('Wind历史技术指标缺少实际日期列')
            unit=table.get('unit',{}).get(column)
            if unit and unit!=binding['source_unit']:raise RuntimeError('Wind历史技术指标单位与字典不一致')
            for values in table['rows']:
                if len(values)!=len(columns):raise RuntimeError('Wind历史技术指标表格维度不一致')
                row=dict(zip(columns,values))
                if row.get('Wind代码')!=q['symbol']:raise RuntimeError('Wind历史技术指标返回证券不匹配')
                day=norm_date(row[date_column])
                if not day or not q['start_date']<=day<=q['end_date']:raise RuntimeError('Wind历史技术指标实际日期与请求不符')
                required={**binding.get('required_options',{}),**{key:params[p] for p,key in tech['parameter_keys'].items()}}
                for key,wanted in required.items():
                    actual=row.get(column+'.'+key)
                    if str(actual)!=str(wanted):raise RuntimeError('Wind历史技术指标分量或参数不匹配：'+key)
                frequency=row.get(column+'.周期')
                if frequency is not None and frequency!='日':raise RuntimeError('Wind历史技术指标不是日线')
                value=row[column]
                if value in (None,'','--','—','N/A'):value=None
                else:
                    try:
                        if isinstance(value,bool):raise ValueError()
                        value=float(value)
                    except (ValueError,TypeError):raise RuntimeError('Wind历史技术指标含未知单位或非数值') from None
                    if not math.isfinite(value):raise RuntimeError('Wind历史技术指标不是有限数值')
                target=merged.setdefault(day,{'symbol':q['symbol'],'date':day})
                if field in target and target[field]!=value:raise RuntimeError('Wind历史技术指标同日返回冲突值')
                target[field]=value
            matched.add(field)
    if not found_table or set(bindings)-matched:raise RuntimeError('Wind未返回目标历史技术指标：'+','.join(sorted(set(bindings)-matched)))
    return [merged[day] for day in sorted(merged)],raw
