"""Dictionary-driven vendor technical transport; no local formulas."""
import copy
import math
from datetime import date
from .parsing import json_objects, norm_date, number

def profile(profile_id='tdx_kdj'):
    from .dictionary import DICTIONARY
    return DICTIONARY['recipes'][profile_id]


def field_profile(field):
    from .dictionary import fields
    definition=fields('technical_indicators')[field]
    return definition.get('profile_id') or definition['sources']['tdx'][0]['recipe']


def normalized_parameters(values=None,profile_id='tdx_kdj',defaults=None):
    tech=profile(profile_id)['technical'];schema=tech['parameter_schema']
    values=values or {}
    if set(values)-set(schema):raise ValueError(profile(profile_id).get('label',profile_id)+'参数只支持 '+ '、'.join(schema))
    out={**tech['default_parameters'],**(defaults or {}),**values}
    for key,value in out.items():
        rule=schema[key]
        if type(value) is not int or not rule['min']<=value<=rule['max']:raise ValueError(key+'参数须为'+str(rule['min'])+'至'+str(rule['max'])+'的整数')
    return out


def resolve_parameters(q):
    from .dictionary import fields
    selected=q['fields'];ids={field_profile(f) for f in selected}
    for pid in ids:
        tech=profile(pid)['technical']
        if tech.get('fixed') and q.get('parameters'):raise ValueError('该技术指标采用来源固定参数，不支持parameters覆盖')
        adjustment=tech.get('adjustment','native')
        if q.get('adjust','native')!='native' and adjustment!=q['adjust']:raise ValueError('复权方式由技术指标字段固定，adjust须与所有所选字段一致或使用native')
    if q.get('parameters') and len(ids)!=1:raise ValueError('parameters仅可覆盖同一技术指标profile；请分开查询不同指标')
    resolved={}
    for pid in ids:
        defaults=[fields('technical_indicators')[f].get('parameters',{}) for f in selected if field_profile(f)==pid]
        if any(p!=defaults[0] for p in defaults) and set(q.get('parameters') or {})!=set(profile(pid)['technical']['parameter_schema']):raise ValueError('所选字段保存的默认参数不同，请明确传入该指标全部parameters或分开查询')
        resolved[pid]=normalized_parameters(q.get('parameters'),pid,defaults[0])
    return resolved

def body(raw):
    if isinstance(raw.get('structuredContent'),dict):return raw['structuredContent']
    for content in raw.get('content',[]):
        for obj in json_objects(content.get('text','')):return obj
    raise RuntimeError('技术指标返回没有结构化数据')

def fetch(remote,q,bindings,recipe=None):
    recipe=recipe or profile(next(iter(bindings.values())).get('recipe','tdx_kdj'))
    tech=recipe['technical'];keys=tech['parameter_keys']
    pid=next((rid for rid in q.get('resolved_parameters',{}) if profile(rid)==recipe),None)
    params=q.get('resolved_parameters',{}).get(pid) or normalized_parameters(q.get('parameters'),next(iter(bindings.values())).get('recipe','tdx_kdj'))
    age=max(0,(date.today()-date.fromisoformat(q['start_date'])).days)
    limit=min(tech['max_limit'],max(tech['min_limit'],age+30))
    raw=remote.call(recipe['method'],{'code':{'SH':'1','SZ':'0','BJ':'2'}[q['symbol'].split('.')[1]]+'|'+q['symbol'].split('.')[0], 'indicators':[{'id':tech['indicator_id'],'set':tech['set'],'parameters':{keys[k]:v for k,v in params.items()}}], 'period':tech['period'],'limit':limit})
    obj=body(raw)
    if obj.get('error_code')!=0:raise RuntimeError('通达信技术指标查询失败')
    rows=[];seen_dates=[]
    for stock in obj.get('data',[]):
        if stock.get('stock_code')!=q['symbol'].split('.')[0] or stock.get('period')!=tech['period']:continue
        dates=[norm_date(str(d)) for d in stock.get('date_list',[])]
        if not dates or any(not d for d in dates):raise RuntimeError('技术指标日期不明确')
        seen_dates.extend(dates)
        for indicator in stock.get('indicator',[]):
            if indicator.get('name')!=tech['indicator_id']:continue
            used=indicator.get('params_used',{})
            if {k:str(v) for k,v in used.items()}!={keys[k]:str(v) for k,v in params.items()}:raise RuntimeError('厂商返回的技术指标参数与请求不一致')
            lines=indicator.get('line',[]); values=indicator.get('data',[])
            if len(values)!=len(dates) or not set(tech['required_lines'])<=set(lines):raise RuntimeError('技术指标分量或日期维度缺失')
            for day,values_row in zip(dates,values):
                if len(values_row)!=len(lines):raise RuntimeError('技术指标返回维度不一致')
                if not q['start_date']<=day<=q['end_date']:continue
                row={'symbol':q['symbol'],'date':day}
                for field,binding in bindings.items():
                    value=number(values_row[lines.index(binding['component'])])
                    if binding.get('active_parameter') and params[binding['active_parameter']]==0:value=None
                    row[field]=value if value is not None and math.isfinite(value) else None
                rows.append(row)
    if seen_dates and q['start_date']<min(seen_dates) and len(seen_dates)>=tech['max_limit']:raise RuntimeError('请求起日超出来源最近'+str(tech['max_limit'])+'根日线覆盖')
    if not rows:raise RuntimeError('来源最近'+str(tech['max_limit'])+'根日线内没有请求日期的数据')
    return rows,raw

def field_for(name):
    from .dictionary import fields
    normalized=name.replace(' ','').upper()
    matches=[field for field,definition in fields('technical_indicators').items() if normalized in [n.replace(' ','').upper() for n in [field,definition['label'],*definition.get('aliases',[])]]]
    if name in matches:return name
    # Base components precede saved parameter variants in dictionary order.
    return matches[0] if matches else None


def component_for(name):
    from .dictionary import fields
    field=field_for(name)
    return fields('technical_indicators')[field]['sources']['tdx'][0]['component'] if field else None


def candidate(request,raw):
    from . import dictionary
    field=field_for(request.name)
    if field is None:return None,['请明确选择技术指标的一个分量']
    definition=copy.deepcopy(dictionary.fields('technical_indicators')[field]);pid=field_profile(field)
    if definition.get('unit_status') in {'unknown','vendor_undisclosed','unconfirmed'}:return None,['来源未披露该分量的单位及缩放，暂不能沉淀为单位已确认的个人指标']
    if request.unit not in [None,definition['unit']]:return None,['该分量单位须为'+definition['unit']]
    binding=definition['sources']['tdx'][0];component=binding['component']
    class Replay:
        def call(self,*args):return raw
    q=request.model_dump()
    try:
        q['parameters']=normalized_parameters(q.get('parameters'),pid,definition.get('parameters'))
        rows,_=fetch(Replay(),q,{field:binding},profile(pid))
    except (RuntimeError,ValueError) as exc:return None,[str(exc)]
    if not any(row.get(field) is not None for row in rows):return None,['来源未返回有限数值']
    base_field=next((name for name,m in dictionary.fields('technical_indicators').items() if m.get('profile_id')==pid and any(b.get('component')==component for b in m['sources'].get('tdx',[]))),field)
    suffix='_'.join(str(q['parameters'][key]) for key in profile(pid)['technical']['parameter_schema'])
    chosen=base_field+('_'+suffix if suffix else '')
    rows=[{'symbol':row['symbol'],'date':row['date'],chosen:row[field]} for row in rows]
    definition['parameters']=q['parameters'];definition['profile_id']=pid;definition['label']=request.name
    definition['description']+=' 保存参数：'+str(q['parameters'])+'；查询可部分覆盖参数。'
    return {'dataset':'technical_indicators','field':chosen,'definition':definition,'recipes':{},'provider':'tdx','data':rows,'parameters':q['parameters'],'profile_id':pid,'source_column':component,'parameter_status':'厂商params_used已核验','notes':['来源原生算法；复权方式未披露。']},[]
