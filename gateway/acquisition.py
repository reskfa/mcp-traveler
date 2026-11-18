"""Execute a dictionary plan using a small set of transport/shape handlers."""
import json
from .dictionary import DICTIONARY, plan, bindings_for, fields
from .parsing import tables, map_rows, norm_date, number, json_objects


def records(df):
    return json.loads(df.to_json(orient='records',date_format='iso',force_ascii=False,double_precision=15))


def context(q, recipe, bindings):
    code, market=q['symbol'].split('.')
    ctx={**q,'code':code,'sina_symbol':market.lower()+code,'market_code':{'SH':'1','SZ':'0','BJ':'2'}[market],
         'ak_adjust':'' if q['adjust']=='none' else q['adjust'],
         'adjust_code':recipe.get('adjust_codes',{}).get(q['adjust']), 'offset':'0'}
    for source,target in [('start_date','start_compact'),('end_date','end_compact'),('date','date_compact'),('period','period_compact')]:
        ctx[target]=(q.get(source) or '').replace('-','')
    ctx['source_fields']=','.join(dict.fromkeys(recipe.get('select_columns',[])+[b['columns'][0] for b in bindings.values()]))
    ctx['labels']='、'.join(dict.fromkeys(b.get('prompt',fields(q['dataset'])[f]['label']) for f,b in bindings.items()))
    ctx['when']=(q['start_date']+'至'+q['end_date']) if q['dataset']=='daily_prices' else q.get('date') or q.get('period') or '最新可用交易日'
    period=q.get('period') or ''
    ctx['report']=period[:4]+'年年报' if period.endswith('-12-31') else period+'报告期累计'
    ctx['qualifier']=('按交易日返回，复权方式：'+{'none':'不复权','qfq':'前复权','hfq':'后复权'}[q['adjust']]+'。') if q['dataset']=='daily_prices' else '合并报表，报告期累计值，不要单季度或同比指标。' if q['dataset']=='financials' else ''
    if 'qualifier' in recipe:ctx['qualifier']=recipe['qualifier']
    if recipe.get('template'):ctx['question']=recipe['template'].format_map(ctx)
    return ctx


def parameters(recipe,ctx,q):
    params=dict(recipe['params'])
    if 'dated_params' in recipe:params.update(recipe['dated_params'] if q.get('date') else recipe.get('latest_params',{}))
    return {k:ctx[v[1:]] if isinstance(v,str) and v.startswith('$') else v for k,v in params.items()}


def adjusted_frame(df,sdk,adjustment,q):
    if df.empty or q['adjust']=='none':return df,[]
    ctx=context(q,{}, {})
    factors=getattr(sdk,adjustment['method'])(**parameters(adjustment,ctx,q))
    if factors.empty:raise RuntimeError('没有复权因子')
    dc=adjustment['date_column'];fc=adjustment['factor_column']
    factors=factors.sort_values(dc)
    anchor=float(factors.iloc[-1 if q['adjust']=='qfq' else 0][fc])
    df=df.merge(factors[[dc,fc]],on=dc,how='left',validate='one_to_one')
    if df[fc].isna().any():raise RuntimeError('部分日期缺少复权因子')
    for col in adjustment['fields']:
        if col in df:df[col]=df[col]*df[fc]/anchor
    return df,records(factors)


def indicator_dates(obj):
    """Read response indicator parameters, never dates echoed in a question."""
    dates=set();ambiguous=False
    def visit(x):
        nonlocal ambiguous
        if isinstance(x,dict):
            params=x.get('indicators_params')
            if isinstance(params,dict):
                for values in params.values():
                    if not isinstance(values,dict):continue
                    for key,value in values.items():
                        if not any(word in key for word in ['日期','报告期']):continue
                        text=str(value)
                        if any(word in text for word in ['最新','MRQ']):ambiguous=True
                        parsed=norm_date(text)
                        if parsed:dates.add(parsed)
            for key,value in x.items():
                if key=='text' and isinstance(value,str):
                    for embedded in json_objects(value):visit(embedded)
                elif isinstance(value,(dict,list)):visit(value)
        elif isinstance(x,list):
            for value in x:visit(value)
    visit(obj)
    return dates,ambiguous


def execute_recipe(rid,recipe,bindings,q,sdk,remote):
    if recipe.get('handler')=='basic_info':
        from .basic_info import fetch
        return fetch(recipe,bindings,q,sdk,remote)
    if recipe.get('handler')=='wind_technical_history':
        from .wind_history import fetch
        return fetch(remote,q,bindings,recipe)
    if recipe.get('handler')=='ifind_technical':
        from .ifind_technical import fetch
        return fetch(remote,q,bindings,recipe)
    if recipe.get('handler')=='wind_technical':
        from .wind_technical import fetch
        return fetch(remote,q,bindings,recipe)
    if recipe['method']=='tdx_technical_indicator_query':
        from .technical import fetch
        return fetch(remote,q,bindings,recipe)
    if recipe.get('provider')=='tushare' and recipe.get('technical',{}).get('fixed'):
        return fixed_technical(recipe,bindings,q,sdk)
    ctx=context(q,recipe,bindings);kind=recipe['kind'];raw=[];table=[]
    if kind.startswith('sdk'):
        batches=[{f:b} for f,b in bindings.items()] if kind=='sdk_per_field' else [bindings]
        for selected in batches:
            local_ctx={**ctx}
            if kind=='sdk_per_field':local_ctx['indicator']=next(iter(selected.values()))['indicator']
            df=getattr(sdk,recipe['method'])(**parameters(recipe,local_ctx,q))
            raw.append(records(df))
            revision=recipe.get('latest_revision')
            if revision and not df.empty:df=df.sort_values(revision['sort'],ascending=False,kind='stable').drop_duplicates(revision['key'])
            if recipe.get('adjustment'):
                df,factor_rows=adjusted_frame(df,sdk,recipe['adjustment'],q)
                if factor_rows:raw.append({'adjustment_factors':factor_rows})
            rr=records(df)
            if kind=='sdk_pivot':
                pivot={'date':q['period']}
                for b in bindings.values():
                    for row in rr:
                        if row.get(recipe['indicator_column'])==b['indicator']:
                            pivot[b['columns'][0]]=row.get(ctx['period_compact']);break
                table.append((pivot,{}))
            elif kind=='sdk_per_field':
                # Normalize each indicator independently; don't cross-match generic 'value'.
                mapped,_=map_rows([(r,{}) for r in rr],list(selected),q['symbol'],q['dataset'],bindings=selected)
                table.extend((r,{}) for r in mapped)
            else:table.extend((r,{}) for r in rr)
    else:
        pagination=recipe.get('pagination');max_pages=pagination['max_pages'] if pagination else 1
        for page in range(max_pages):
            if pagination:ctx['offset']=str(page*pagination['size'])
            r=remote.call(recipe['method'],parameters(recipe,ctx,q));raw.append(r);batch=tables(r);table.extend(batch)
            if not pagination:break
            dates=[norm_date(row.get(pagination['date_column'])) for row,_ in batch];dates=[d for d in dates if d]
            if not batch or len(batch)<pagination['size'] or (dates and min(dates)<=q['start_date']):break
        else:raise RuntimeError('历史分页达到上限，请缩小日期范围')
    expected=q.get('date') or q.get('period');assumed=None
    if recipe.get('date_evidence')=='year_columns' and expected and expected.endswith('-12-31') and table and all(any(expected[:4] in str(k) for k in row) for row,_ in table):assumed=expected
    if recipe.get('date_evidence')=='indicator_params' and expected:
        dates,ambiguous=indicator_dates(raw)
        if ambiguous or dates-set([expected]):
            raise RuntimeError('返回指标参数为最新报告期或不同日期，不能用于指定历史日期')
        # Each mapped column must carry its own date parameters; no global date borrowing.
    if kind=='sdk_per_field':
        data=[r for r,_ in table]
    else:
        data,matched=map_rows(table,list(bindings),q['symbol'],q['dataset'],assumed,bindings=bindings,expected_date=expected,require_indicator_date=bool(expected and recipe.get('date_evidence') in ['indicator_params','table_or_indicator_params']),prefer_row_date=recipe.get('date_evidence')=='table_or_indicator_params')
        if table and not matched:raise RuntimeError(f'{rid}: 返回有内容，但没有识别到字典配置的字段')
    if not table and kind.startswith('mcp'):
        txt=json.dumps(raw,ensure_ascii=False)
        if not any(t in txt for t in ['无数据','暂无','无结果','rows": []','Rows": []']):raise RuntimeError(f'{rid}: 无法解析上游结果')
    if q['dataset']=='daily_prices':
        if any(not r['date'] for r in data):raise RuntimeError('行情缺少日期')
        data=[r for r in data if q['start_date']<=r['date']<=q['end_date']]
    elif expected:
        if any(not r['date'] for r in data):raise RuntimeError('返回未提供可核对的日期／报告期')
        data=[r for r in data if r['date']==expected]
    elif data and all(r.get('date') for r in data):
        latest=max(r['date'] for r in data);data=[r for r in data if r['date']==latest]
    return data,raw


def fixed_technical(recipe,bindings,q,sdk):
    """Keep vendor values unchanged and paginate backward at the documented cap."""
    from datetime import date,timedelta
    ctx=context(q,recipe,bindings);end=q['end_date'];raw=[];out={}
    for _ in range(20):
        args=parameters(recipe,{**ctx,'end_compact':end.replace('-','')},q)
        frame=getattr(sdk,recipe['method'])(**args)
        batch=records(frame);raw.append(batch)
        if not batch:break
        for item in batch:
            if item.get('ts_code')!=q['symbol']:raise RuntimeError('Tushare技术指标返回证券与请求不一致')
            day=norm_date(item.get('trade_date'))
            if not day or not q['start_date']<=day<=end:raise RuntimeError('Tushare技术指标返回日期缺失或越界')
            if any(b['columns'][0] not in item for b in bindings.values()):raise RuntimeError('Tushare技术指标返回缺少请求字段')
            out[day]={'symbol':q['symbol'],'date':day,**{f:number(item[b['columns'][0]]) for f,b in bindings.items()}}
        oldest=min(out)
        if len(batch)<10000 or oldest<=q['start_date']:break
        end=(date.fromisoformat(oldest)-timedelta(days=1)).isoformat()
    else:raise RuntimeError('Tushare技术指标历史分页达到上限，请缩小日期范围')
    return list(out.values()),raw


def acquire(provider,q,sdk=None,remote=None):
    steps=plan(provider,q['dataset'],q['fields'])
    if not steps:raise RuntimeError('字典中当前来源没有所请求字段的取数方案')
    merged={};raw=[];notes=[];upstreams=[]
    for rid,recipe,bindings in steps:
        try:
            data,response=execute_recipe(rid,recipe,bindings,q,sdk,remote)
        except Exception as exc:
            fallback=recipe.get('fallback')
            if not fallback:
                if q['dataset']=='basic_info':
                    from .config import redact
                    notes.append(f'{rid}失败：{redact(str(exc))}')
                    raw.append({'recipe':rid,'error':redact(str(exc))})
                    continue
                raise
            notes.append(f'{rid}失败（{type(exc).__name__}），使用字典配置的备用方案 {fallback}')
            rid=fallback;recipe=DICTIONARY['recipes'][rid];bindings=bindings_for(provider,q['dataset'],list(bindings),rid)
            data,response=execute_recipe(rid,recipe,bindings,q,sdk,remote)
        raw.append({'recipe':rid,'method':recipe['method'],'response':response})
        notes.extend(recipe.get('notes',[]));notes.extend(b['note'] for b in bindings.values() if b.get('note'))
        upstreams.append(recipe.get('upstream',provider))
        for row in data:
            target=merged.setdefault((row['symbol'],row['date']),{'symbol':row['symbol'],'date':row['date'],**{f:None for f in q['fields']}})
            for f in bindings:
                if target.get(f) is None and row.get(f) is not None:target[f]=row[f]
    if q['dataset']=='basic_info' and not merged:raise RuntimeError('基本信息取数方案均失败：'+'；'.join(notes))
    return {'data':list(merged.values()),'raw':raw,'notes':list(dict.fromkeys(notes)),'upstream':' + '.join(dict.fromkeys(upstreams))}
