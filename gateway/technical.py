"""Vendor KDJ transport. Values are never calculated locally."""
import copy
import math
from datetime import date
from .parsing import json_objects, norm_date, number

def profile():
    from .dictionary import DICTIONARY
    return DICTIONARY['recipes']['tdx_kdj']


def normalized_parameters(values=None):
    defaults=profile()['technical']['default_parameters']
    values=values or {}
    if set(values)-set(defaults):raise ValueError('KDJ参数只支持 n、m1、m2')
    out={**defaults,**values}
    if any(type(v) is not int or not 1<=v<=1000 for v in out.values()):raise ValueError('KDJ参数须为1至1000的整数')
    return out

def body(raw):
    if isinstance(raw.get('structuredContent'),dict):return raw['structuredContent']
    for content in raw.get('content',[]):
        for obj in json_objects(content.get('text','')):return obj
    raise RuntimeError('技术指标返回没有结构化数据')

def fetch(remote,q,bindings):
    params=normalized_parameters(q.get('parameters'))
    recipe=profile();tech=recipe['technical'];keys=tech['parameter_keys']
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
            if {k:str(v) for k,v in used.items()}!={keys[k]:str(v) for k,v in params.items()}:raise RuntimeError('厂商返回的KDJ参数与请求不一致')
            lines=indicator.get('line',[]); values=indicator.get('data',[])
            if len(values)!=len(dates) or not {'K','D','J'}<=set(lines):raise RuntimeError('KDJ三分量或日期维度缺失')
            for day,values_row in zip(dates,values):
                if len(values_row)!=len(lines):raise RuntimeError('KDJ返回维度不一致')
                if not q['start_date']<=day<=q['end_date']:continue
                row={'symbol':q['symbol'],'date':day}
                for field,binding in bindings.items():
                    value=number(values_row[lines.index(binding['component'])])
                    row[field]=value if value is not None and math.isfinite(value) else None
                rows.append(row)
    if seen_dates and q['start_date']<min(seen_dates) and len(seen_dates)>=tech['max_limit']:raise RuntimeError('请求起日超出来源最近5000根日线覆盖')
    if not rows:raise RuntimeError('来源最近5000根日线内没有请求日期的数据')
    return rows,raw

def component_for(name):
    from .dictionary import fields
    normalized=name.replace(' ','').upper()
    for field,definition in fields('technical_indicators').items():
        if normalized in [n.replace(' ','').upper() for n in [field,definition['label'],*definition.get('aliases',[])]]:
            bindings=definition['sources'].get('tdx',[])
            if bindings:return bindings[0]['component']
    return None

def candidate(request,raw):
    from . import dictionary,config
    if request.unit not in [None,'无量纲']:return None,['KDJ单位须为无量纲']
    component=component_for(request.name)
    if component is None:return None,['请明确选择 KDJ K值、KDJ D值或KDJ J值']
    field='kdj_'+component.lower();definition=copy.deepcopy(dictionary.fields('technical_indicators')[field])
    class Replay:
        def call(self,*args):return raw
    q=request.model_dump();q['parameters']=normalized_parameters(q.get('parameters'))
    try:rows,_=fetch(Replay(),q,{field:definition['sources']['tdx'][0]})
    except (RuntimeError,ValueError) as exc:return None,[str(exc)]
    if not any(row.get(field) is not None for row in rows):return None,['来源未返回有限数值']
    p=q['parameters'];chosen=field+'_'+str(p['n'])+'_'+str(p['m1'])+'_'+str(p['m2'])
    rows=[{'symbol':row['symbol'],'date':row['date'],chosen:row[field]} for row in rows]
    field=chosen
    definition['parameters']=q['parameters'];definition['label']=request.name
    definition['description']+=' 默认参数：'+str(q['parameters'])+'；查询可覆盖参数。'
    return {'dataset':'technical_indicators','field':field,'definition':definition,'recipes':{},'provider':'tdx','data':rows,'parameters':q['parameters'],'source_column':component,'parameter_status':'厂商params_used已核验','notes':['来源原生算法；复权方式未披露。']},[]
