"""Validated, declarative field acquisition dictionary. No executable expressions."""
import json
from pathlib import Path

PATH = Path(__file__).parent / 'data/dictionary.json'
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
                    if r['provider']!=p or r['dataset']!=ds or not b['columns']:
                        raise ValueError(f'Invalid binding: {ds}.{field}.{p}')
                    if not isinstance(b.get('multiplier',1),(int,float)):
                        raise ValueError(f'Invalid multiplier: {field}')
    return d

def load_dictionary():
    return validate(json.loads(PATH.read_text()))

# One consistent dictionary snapshot per process; restart the service after edits.
DICTIONARY = load_dictionary()

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
