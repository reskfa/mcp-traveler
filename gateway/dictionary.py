"""Validated, declarative field acquisition dictionary. No executable expressions."""
import json
import copy
import math
import re
import sys
from . import config
from pathlib import Path

PATH = Path(__file__).parent / 'data/dictionary.json'
EXPLORATION_TEMPLATE = '查询{symbol}，{when}的{labels}。{qualifier}只返回指定指标的数据表，必须含实际证券代码、实际日期、指标全名及明确单位；无法提供请留空。'
EXPLORATION_QUALIFIER = '按请求的指标原义返回，保留累计、单季度、期末或TTM口径说明，不要擅自替换口径。'
KINDS = {'sdk', 'sdk_per_field', 'sdk_pivot', 'mcp', 'mcp_paged', 'mcp_nl'}

def validate(d):
    if d.get('schema_version') != 1:
        raise ValueError('Unsupported dictionary schema_version')
    recipes=d['recipes']
    for rid,r in recipes.items():
        if r['kind'] not in KINDS or r['provider'] not in d['providers'] or r['dataset'] not in d['datasets']:
            raise ValueError(f'Invalid recipe: {rid}')
        if not r['method'].isidentifier() or r['method'].startswith('_'):
            raise ValueError(f'Invalid method: {rid}')
        if r.get('fallback'):
            f=recipes[r['fallback']]
            if not f.get('fallback_only') or f['provider']!=r['provider'] or f['dataset']!=r['dataset'] or f.get('fallback'):
                raise ValueError(f'Invalid fallback: {rid}')
    for ds,g in d['datasets'].items():
        if set(g.get('default_fields',[]))-set(g['fields']):raise ValueError(f'Unknown default field: {ds}')
        for field,m in g['fields'].items():
            if not m['label'] or not m['unit'] or set(m['sources']) != set(d['providers']):
                raise ValueError(f'Incomplete field: {ds}.{field}')
            for p,bindings in m['sources'].items():
                for b in bindings:
                    r=recipes[b['recipe']]
                    if r.get('kind') in {'sdk_per_field', 'sdk_pivot'} and not isinstance(b.get('indicator'),str):
                        raise ValueError(f'Missing indicator: {ds}.{field}.{p}')
                    if r['provider']!=p or r['dataset']!=ds or not b['columns']:
                        raise ValueError(f'Invalid binding: {ds}.{field}.{p}')
                    if not isinstance(b.get('multiplier',1),(int,float)):
                        raise ValueError(f'Invalid multiplier: {field}')
    return d

def user_dictionary_path():
    return config.ROOT / 'local/user_dictionary.json'


def _read_user():
    path = user_dictionary_path()
    return json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'datasets': {}, 'recipes': {}}


def _safe_user(base, user):
    if user.get('schema_version') != 1:
        raise ValueError('Unsupported user dictionary schema_version')
    # Personal recipes reuse an approved acquisition template, never a new SDK
    # method, endpoint, parameter or executable expression.
    for rid, recipe in user.get('recipes', {}).items():
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', rid):
            raise ValueError('Invalid recipe ID')
        operational = {k:v for k,v in recipe.items() if k not in {'notes', 'upstream'}}
        exploration = (recipe.get('kind') == 'mcp_nl' and recipe.get('template') == EXPLORATION_TEMPLATE
                       and recipe.get('qualifier') == EXPLORATION_QUALIFIER)
        ignored={'template','qualifier'} if exploration else set()
        if not any({k:v for k,v in operational.items() if k not in ignored} ==
                   {k:v for k,v in approved.items() if k not in {'notes','upstream'} | ignored}
                   for approved in base['recipes'].values()):
            raise ValueError('Personal recipes must reuse an approved acquisition template')
        if rid in base['recipes'] and recipe != base['recipes'][rid]:
            raise ValueError('Cannot replace a built-in recipe')
    for dataset, group in user.get('datasets', {}).items():
        if dataset not in base['datasets']:
            raise ValueError('未知数据类别')
        for field, definition in group.get('fields', {}).items():
            if not re.fullmatch(r'[a-z][a-z0-9_]*', field) or field in {'symbol', 'date'}:
                raise ValueError('字段 ID 须为小写英文、数字及下划线')
            for key in ('label', 'unit', 'description'):
                if not isinstance(definition.get(key), str) or not definition[key].strip():
                    raise ValueError('字段名称、单位和说明不能为空')
            if not isinstance(definition.get('aliases', []), list) or any(not isinstance(v,str) for v in definition.get('aliases', [])):
                raise ValueError('Invalid aliases')
            if not isinstance(definition.get('sources'), dict) or set(definition['sources']) != set(base['providers']):
                raise ValueError('sources 必须包含所有 provider 的列表')
            for provider, bindings in definition['sources'].items():
                if not isinstance(bindings, list): raise ValueError('sources 必须为列表')
                for binding in bindings:
                    if not isinstance(binding, dict) or not isinstance(binding.get('columns'),list) or not binding['columns'] or any(not isinstance(c,str) or not c.strip() for c in binding['columns']):
                        raise ValueError('Invalid source columns')
                    multiplier=binding.get('multiplier',1)
                    if isinstance(multiplier,bool) or not isinstance(multiplier,(int,float)) or not math.isfinite(multiplier):
                        raise ValueError('Invalid multiplier')
                    if 'round_digits' in binding and (type(binding['round_digits']) is not int or not 0 <= binding['round_digits'] <= 15):
                        raise ValueError('Invalid rounding')
    return user


def _merge(base, user):
    _safe_user(base, user)
    merged=copy.deepcopy(base)
    merged['recipes'].update(copy.deepcopy(user.get('recipes', {})))
    for dataset, group in user.get('datasets', {}).items():
        merged['datasets'][dataset]['fields'].update(copy.deepcopy(group.get('fields', {})))
    try:
        return validate(merged)
    except (KeyError, TypeError) as exc:
        raise ValueError('Invalid personal dictionary binding') from exc


def load_dictionary():
    """Read the built-in dictionary plus this home's personal overrides."""
    return _merge(validate(json.loads(PATH.read_text())), _read_user())


def _signature():
    paths=(PATH,user_dictionary_path())
    return tuple((str(p),p.stat().st_mtime_ns,p.stat().st_size) if p.exists() else (str(p),None,None) for p in paths)


DICTIONARY = load_dictionary()
_LOADED_SIGNATURE = _signature()


def reload_dictionary(force=True):
    """Refresh shared mappings in place; existing imports remain valid."""
    global _LOADED_SIGNATURE
    with config.LOCK:
        signature=_signature()
        if not force and signature == _LOADED_SIGNATURE:
            return DICTIONARY
        fresh=load_dictionary()
        DICTIONARY.clear()
        DICTIONARY.update(fresh)
        _LOADED_SIGNATURE=signature
        module=sys.modules.get(__package__ + '.catalog')
        if module is not None and hasattr(module, 'refresh_catalog'):
            module.refresh_catalog()
    return DICTIONARY


def save_user_metric(dataset, field, definition, recipes=None):
    """Atomically upsert one metric, preserving all unrelated local entries."""
    with config.LOCK:
        user=_read_user()
        user.setdefault('datasets', {}).setdefault(dataset, {}).setdefault('fields', {})[field]=copy.deepcopy(definition)
        user.setdefault('recipes', {}).update(copy.deepcopy(recipes or {}))
        _merge(validate(json.loads(PATH.read_text())), user)
        config.write_json(user_dictionary_path(),user)
        reload_dictionary()
    return {'saved':True,'dataset':dataset,'field':field,'storage':'user','path':str(user_dictionary_path())}


def delete_user_metric(dataset, field):
    with config.LOCK:
        user=_read_user()
        selected=user.get('datasets', {}).get(dataset, {}).get('fields', {})
        deleted=field in selected
        selected.pop(field,None)
        _merge(validate(json.loads(PATH.read_text())),user)
        config.write_json(user_dictionary_path(),user)
        reload_dictionary()
    return {'deleted':deleted,'dataset':dataset,'field':field,'builtin_retained':field in DICTIONARY.get('datasets',{}).get(dataset,{}).get('fields',{})}


def search_metrics(query='', dataset=None, limit=10):
    reload_dictionary(force=False)
    if dataset is not None and dataset not in DICTIONARY['datasets']:
        raise ValueError('未知数据类别')
    limit=max(1,min(100,int(limit)))
    needle=query.strip().casefold()
    tokens=needle.split()
    local=_read_user().get('datasets',{})
    matches=[]
    for ds,group in DICTIONARY['datasets'].items():
        if dataset is not None and ds != dataset: continue
        for field,m in group['fields'].items():
            primary=[field,m['label'],*m.get('aliases',[])]
            columns=[c for bindings in m['sources'].values() for binding in bindings for c in binding['columns']]
            corpus=' '.join([*primary,*columns,m.get('description','')]).casefold()
            if tokens and not all(t in corpus for t in tokens): continue
            score=0 if needle in [v.casefold() for v in primary] else 1 if any(needle in v.casefold() for v in primary) else 2
            matches.append((score,ds,field,{'dataset':ds,'field':field,'label':m['label'],'unit':m['unit'],'description':m.get('description',''),'aliases':m.get('aliases',[]),**({'parameters':m['parameters']} if 'parameters' in m else {}),'sources':[p for p,b in m['sources'].items() if b],'origin':'user' if field in local.get(ds,{}).get('fields',{}) else 'builtin'}))
    matches.sort(key=lambda x:x[:3])
    return {'query':query,'total':len(matches),'results':[m[3] for m in matches[:limit]]}

def fields(dataset):
    return DICTIONARY['datasets'][dataset]['fields']

def plan(provider, dataset, requested):
    """Group requested fields by configured recipe, preserving dictionary order."""
    groups={}
    for field in requested:
        for b in fields(dataset)[field]['sources'][provider]:
            if not DICTIONARY['recipes'][b['recipe']].get('fallback_only'):
                groups.setdefault(b['recipe'],{})[field]=b
    return [(rid,DICTIONARY['recipes'][rid],bindings) for rid,bindings in groups.items()]

def bindings_for(provider,dataset,requested,rid):
    return {f:b for f in requested for b in fields(dataset)[f]['sources'][provider] if b['recipe']==rid}
