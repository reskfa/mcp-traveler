"""Dictionary-driven typed CB snapshots, dated observations and disclosures."""
import hashlib
import json
from datetime import date,timedelta
from .basic_info import typed,pick
from .dictionary import fields
from .parsing import norm_date


def fetch(recipe,bindings,q,sdk=None,remote=None):
    from .acquisition import context,parameters,records
    ctx=context(q,recipe,bindings);args=parameters(recipe,ctx,q)
    args={k:v for k,v in args.items() if v not in (None,'')}
    # Date and identity columns are evidence, never supplied from the request.
    columns=list(dict.fromkeys(recipe.get('select_columns',[])+recipe.get('date_columns',[])+[c for b in bindings.values() for c in b['columns']]))
    args['fields']=','.join(columns)
    raw=[];out=[];end=q.get('end_date')
    for _ in range(100):
        if end and 'end_date' in args:args['end_date']=end.replace('-','')
        rows=records(getattr(sdk,recipe['method'])(**args));raw.append(rows)
        days=[]
        for item in rows:
            if str(item.get('ts_code','')).upper()!=q['symbol']:raise RuntimeError('转债上游返回证券与请求不一致')
            day=next((norm_date(item.get(c)) for c in recipe.get('date_columns',[]) if norm_date(item.get(c))),None)
            if recipe.get('temporal')!='snapshot' and not day:raise RuntimeError('转债上游未提供可核对的交易／公告日期')
            if day:
                date.fromisoformat(day);days.append(day)
                if q.get('start_date') and day<q['start_date']:continue
                if q.get('end_date') and day>q['end_date']:continue
                if q.get('date') and day!=q['date']:continue
            row={'symbol':q['symbol'],'date':day}
            if q['dataset']=='cb_clauses':row['event_id']=hashlib.sha256(json.dumps(item,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
            for f,b in bindings.items():row[f]=typed(pick(item,b['columns']),fields(q['dataset'])[f].get('data_type','number'),b)
            out.append(row)
        if not recipe.get('limit') or len(rows)<recipe['limit']:break
        if not days:raise RuntimeError('转债分页缺少日期')
        oldest=min(days)
        if not q.get('start_date') or oldest<=q['start_date']:
            if q['dataset']=='cb_clauses':raise RuntimeError('公告记录达到上游条数上限，请指定更小公告日期范围')
            break
        end=(date.fromisoformat(oldest)-timedelta(days=1)).isoformat()
    else:raise RuntimeError('转债历史分页达到上限，请缩小日期范围')
    return out,raw


def fetch_wind(recipe,bindings,q,remote):
    from .acquisition import context,parameters
    from .parsing import tables,map_rows
    ctx=context(q,recipe,bindings)
    raw=remote.call(recipe['method'],parameters(recipe,ctx,q))
    table=tables(raw)
    for row,_ in table:
        identity=pick(row,['Wind代码','证券代码','代码'])
        if not identity or str(identity).upper()!=q['symbol']:raise RuntimeError('Wind转债结果缺少匹配的证券身份')
    if q['dataset']=='cb_basic':
        out={'symbol':q['symbol'],'date':None}
        for f,b in bindings.items():out[f]=next((v for row,_ in table if (v:=typed(pick(row,b['columns']),fields(q['dataset'])[f]['data_type'],b)) is not None),None)
        return ([out] if table else []),[raw]
    data,_=map_rows(table,list(bindings),q['symbol'],q['dataset'],bindings=bindings)
    if any(not r['date'] for r in data):raise RuntimeError('Wind转债历史值缺少明确交易日，拒绝用当前快照回填')
    return [r for r in data if q['start_date']<=r['date']<=q['end_date']],[raw]
