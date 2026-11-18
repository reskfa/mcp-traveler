"""Typed basic-information acquisition, driven entirely by dictionary bindings."""
import json
import re
from datetime import date
from .parsing import tables, norm_date, number
from .dictionary import fields

_MISSING={'','--','—','-','null','none','nan','n/a','暂无','暂无数据','无数据'}

def present(value):
    return value is not None and not (isinstance(value,str) and value.strip().lower() in _MISSING)

def pick(row, columns):
    return next((row[c] for c in columns if c in row and present(row[c])),None)

def typed(value, kind='string', binding=None):
    b=binding or {}
    if not present(value):return None
    if kind=='number':
        n=number(value,b.get('parse_unit',''))
        return n*b.get('multiplier',1) if n is not None else None
    if kind=='date':
        d=norm_date(str(value).replace('/','-'))
        try:return date.fromisoformat(d).isoformat() if d else None
        except ValueError:return None
    if isinstance(value,(dict,list,bool)):return None
    text=str(value).strip()
    if b.get('split'):
        parts=text.split(b['split']);index=b.get('index',0)
        text=parts[index].strip() if index<len(parts) else None
    if b.get('value_map'):text=b['value_map'].get(text,text)
    return text if present(text) else None

def eligible(row, units, binding):
    if any(str(row.get(k))!=str(v) for k,v in binding.get('row_filter',{}).items()):return False
    expected=binding.get('indicator_params',{})
    if expected:
        params=units.get('__indicator_params__',{})
        for col in binding['columns']:
            actual=params.get(col,{})
            if all(actual.get(k)==v for k,v in expected.items()):return True
        return False
    return True

def map_basic(table,bindings,q,recipe):
    """Match identity before fields; no numeric coercion for names/codes."""
    selected=[]
    identity=recipe.get('identity_columns',[])
    for row,units in table:
        codes=[str(row[c]).strip().upper() for c in identity if present(row.get(c))]
        if codes and any(code not in {q['symbol'],q['symbol'].split('.')[0]} for code in codes):continue
        if not codes and not recipe.get('allow_request_identity'):continue
        selected.append((row,units))
    out={'symbol':q['symbol'],'date':None}
    for field,b in bindings.items():
        candidates=[(r,u) for r,u in selected if eligible(r,u,b)]
        shape=b.get('shape','scalar');value=None
        if shape=='records':
            records=[]
            for r,u in candidates:
                item={k:typed(pick(r,v['columns']),v.get('type','string'),v) for k,v in b['record_fields'].items()}
                if not all(present(item.get(k)) for k in b.get('required_keys',['name'])):continue
                records.append({**b.get('constants',{}),**item})
            value=records or None
        elif shape=='split_records':
            raw=next((pick(r,b['columns']) for r,u in candidates if present(pick(r,b['columns']))),None)
            if isinstance(raw,str):value=[{**b.get('constants',{}),b.get('name_key','name'):s.strip()} for s in re.split(b['separator'],raw) if present(s.strip())] or None
        else:
            kind=fields('basic_info')[field]['data_type']
            value=next((v for r,u in candidates if (v:=typed(pick(r,b['columns']),kind,b)) is not None),None)
        if isinstance(value,list):
            schema=fields('basic_info')[field].get('record_schema',{})
            if schema:value=[{key:item.get(key) for key in schema} for item in value]
            seen=set();unique=[]
            for item in value:
                key=json.dumps(item,sort_keys=True,ensure_ascii=False)
                if key not in seen:
                    unique.append(item);seen.add(key)
            value=unique
        out[field]=value
    return [out]

def fetch(recipe,bindings,q,sdk=None,remote=None):
    from .acquisition import context,parameters,records
    ctx=context(q,recipe,bindings);args=parameters(recipe,ctx,q);raw=[]
    if recipe['kind']=='sdk':
        rows=records(getattr(sdk,recipe['method'])(**args));raw.append(rows)
        for status in recipe.get('status_fallbacks',[]):
            if rows:break
            rows=records(getattr(sdk,recipe['method'])(**{**args,'list_status':status}));raw.append(rows)
        if join:=recipe.get('join'):
            directory=records(getattr(sdk,join['method'])(**join['params']));raw.append(directory)
            lookup={r[join['right']]:r for r in directory}
            rows=[{**r,**{target:lookup.get(r.get(join['left']),{}).get(source) for source,target in join['columns'].items()}} for r in rows]
        table=[(r,{}) for r in rows]
    else:
        result=remote.call(recipe['method'],args);raw.append(result)
        if result.get('isError'):raise RuntimeError('基本信息上游工具返回错误')
        table=tables(result)
        if not table:raise RuntimeError('基本信息返回未包含可解析的数据表')
    return map_basic(table,bindings,q,recipe),raw
