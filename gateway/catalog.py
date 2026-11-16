"""Public catalog is a view of the same dictionary used by acquisition."""
from .dictionary import DICTIONARY
FIELDS = {}
UNITS = {}
DEFAULT_FIELDS = {}
CAPABILITIES = {}

def refresh_catalog():
    for target, value in (
        (FIELDS, {ds:{f:m['label'] for f,m in g['fields'].items()} for ds,g in DICTIONARY['datasets'].items()}),
        (UNITS, {ds:{f:m['unit'] for f,m in g['fields'].items()} for ds,g in DICTIONARY['datasets'].items()}),
        (DEFAULT_FIELDS, {ds:list(g.get('default_fields',[])) for ds,g in DICTIONARY['datasets'].items()}),
        (CAPABILITIES, DICTIONARY['providers']),
    ):
        target.clear()
        target.update(value)

refresh_catalog()

def catalog():
    from .dictionary import reload_dictionary
    reload_dictionary(force=False)
    return {'datasets':{ds:{'default_fields':DEFAULT_FIELDS[ds],'date_semantics':g['date_semantics'],'fields':{f:{'label':m['label'],'unit':m['unit'],'description':m['description'],'unit_status':m.get('unit_status','existing'),'unit_note':m.get('unit_note',''),'available_sources':[p for p,b in m['sources'].items() if b]} for f,m in g['fields'].items()}} for ds,g in DICTIONARY['datasets'].items()},'providers':CAPABILITIES,
            'dictionary_url':'/api/dictionary','notes':['自动路由逐字段补齐，保留已有值；field_sources 与数据行对应。','字段取法、参数、别名和单位转换由 gateway/data/dictionary.json 定义。']}

def dictionary_view(dataset=None,field=None):
    from .dictionary import reload_dictionary
    reload_dictionary(force=False)
    if dataset is None:return DICTIONARY
    if dataset not in DICTIONARY['datasets']:raise ValueError('未知数据类别')
    group=DICTIONARY['datasets'][dataset]
    if field is not None:
        if field not in group['fields']:raise ValueError('未知字段')
        selected={field:group['fields'][field]}
    else:selected=group['fields']
    ids={b['recipe'] for m in selected.values() for bindings in m['sources'].values() for b in bindings}
    return {'schema_version':DICTIONARY['schema_version'],'dataset':dataset,'fields':selected,'recipes':{rid:DICTIONARY['recipes'][rid] for rid in sorted(ids)}}


def source_definitions(dataset,field,providers=None):
    """Source-specific meaning accompanies values; algorithms need not agree."""
    meta=DICTIONARY['datasets'][dataset]['fields'][field]
    return {p:[{'columns':b['columns'],'source_unit':b.get('source_unit','按返回单位'),
                'note':b.get('note','使用该来源自身指标口径；不保证跨来源数值或算法完全一致。')}
               for b in bindings if not DICTIONARY['recipes'][b['recipe']].get('fallback_only')]
            for p,bindings in meta['sources'].items() if bindings and (providers is None or p in providers)}
