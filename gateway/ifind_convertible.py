"""Dictionary-driven iFinD bond indicators with explicit identity/date/unit evidence."""
import re
import time
from datetime import date
from .basic_info import typed
from .dictionary import fields
from .ifind_technical import payloads
from .parsing import markdown_rows, norm_date, number


def bond_rows(answer):
    # Contract text may contain literal newlines inside a Markdown cell.
    lines=[]; pending=''
    for line in answer.splitlines():
        if pending:
            pending+='\\n'+line
            if line.rstrip().endswith('|'):lines.append(pending);pending=''
        elif line.lstrip().startswith('|') and not line.rstrip().endswith('|'):pending=line
        else:lines.append(line)
    return markdown_rows('\n'.join(lines))


def fetch(recipe, bindings, q, remote):
    limit=recipe.get('max_fields_per_call',3)
    if type(limit) is not int or limit<1:raise ValueError('iFinD批次指标数须为正整数')
    singleton=set(recipe.get('singleton_fields',[]))
    batches=[];pending={}
    for field,binding in bindings.items():
        if field in singleton:
            if pending:batches.append(pending);pending={}
            batches.append({field:binding})
        else:
            pending[field]=binding
            if len(pending)==limit:batches.append(pending);pending={}
    if pending:batches.append(pending)
    merged={};raw=[];failures=[];succeeded=0;timeout=getattr(remote,'timeout',None)
    deadline=time.monotonic()+timeout if timeout else None
    try:
        for batch in batches:
            if deadline:
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    message='iFinD转债分批查询达到总超时'
                    failures.append(message);raw.append({'batch_fields':list(batch),'error':message})
                    continue
                remote.timeout=remaining
            try:
                rows,responses=fetch_batch(recipe,batch,q,remote)
            except Exception as exc:
                failures.append(str(exc))
                diagnostic={'batch_fields':list(batch),'error':str(exc)}
                if hasattr(exc,'raw_response'):diagnostic['response']=exc.raw_response
                raw.append(diagnostic)
                continue
            succeeded+=1;raw.extend(responses)
            for row in rows:
                key=row['date'];target=merged.setdefault(key,{'symbol':q['symbol'],'date':key})
                for field,value in row.items():
                    if field in target and target[field] is not None and value is not None and target[field]!=value:
                        raise RuntimeError('iFinD转债跨批次同日同指标出现不同值')
                    if value is not None or field not in target:target[field]=value
    finally:
        if timeout is not None:remote.timeout=timeout
    if failures and not succeeded:
        error=RuntimeError('iFinD转债所有批次失败：'+'；'.join(dict.fromkeys(failures)))
        error.raw_response=raw
        raise error
    return [merged[k] for k in sorted(merged,key=lambda k:k or '')],raw


def fetch_batch(recipe, bindings, q, remote):
    labels='、'.join(dict.fromkeys(b['prompt'] for b in bindings.values()))
    when=(q['start_date']+'至'+q['end_date']+'逐交易日') if recipe['temporal']=='series' else '当前档案'
    question=(f"查询{q['symbol']}可转换债券{when}的{labels}。保留证券代码、每行实际交易日期、指标名称和单位。"
              '不复权；不得用最新快照代替历史；仅用原生指标，缺失留空，不自行计算。')
    raw=remote.call(recipe['method'],{'market':'债券','query':question})
    blocks=payloads(raw)
    if not blocks:raise RuntimeError('iFinD转债缺少指标结果')
    if any('复杂问句降级提示' in block['answer'] for block in blocks):
        error=RuntimeError('iFinD转债请求被上游复杂问句降级，请缩小字段或日期范围')
        error.raw_response=raw
        raise error
    merged={}
    for block in blocks:
        for item in bond_rows(block['answer']):
            if item.get('证券代码')!=q['symbol']:raise RuntimeError('iFinD转债结果证券身份不匹配')
            day=norm_date(item.get('日期')) or norm_date(item.get('交易日期'))
            if day:
                date.fromisoformat(day)
                if recipe['temporal']=='series' and not q['start_date']<=day<=q['end_date']:continue
            for field,b in bindings.items():
                for column,value in item.items():
                    unit_match=re.search(r'[（(](?:[^）)]*?；)?单位[：:]([^）)]+)[）)]$',column)
                    base=column[:unit_match.start()] if unit_match else column
                    if base not in b['columns']:continue
                    params=block.get('indicators_params',{}).get(base,{})
                    own_dates={norm_date(v) for k,v in params.items() if k in ('交易日期','日期','截止日期') and norm_date(v)}
                    if recipe['temporal']=='series' and (len(own_dates)>1 or (day and own_dates and own_dates!={day})):
                        raise RuntimeError(f'iFinD转债指标{field}的表格日期与自身参数日期冲突')
                    actual_day=day or (next(iter(own_dates)) if len(own_dates)==1 else None)
                    if recipe['temporal']=='series':
                        if not actual_day:raise RuntimeError('iFinD转债历史值缺少逐行交易日或本指标日期')
                        date.fromisoformat(actual_day)
                        if not q['start_date']<=actual_day<=q['end_date']:continue
                    # Some native bond indicators put units only in their own
                    # parameter metadata (notably 债券成交量). Never borrow a
                    # unit from the request or another indicator.
                    unit=unit_match[1] if unit_match else ''
                    parameter_unit=params.get(b.get('unit_parameter')) if b.get('unit_parameter') else None
                    if parameter_unit is not None:
                        if unit and unit!=parameter_unit:raise RuntimeError(f'iFinD转债指标{field}的表头单位与自身参数单位冲突')
                        unit=unit or parameter_unit
                    if b.get('accepted_units') is not None and unit not in b['accepted_units']:continue
                    params=block.get('indicators_params',{}).get(base,{})
                    if any(str(params.get(k))!=str(v) for k,v in b.get('indicator_params',{}).items()):continue
                    kind=fields(q['dataset'])[field]['data_type']
                    result=number(value,unit) if kind=='number' else typed(value,kind,b)
                    if kind=='number' and result is not None:result*=b.get('multiplier',1)
                    # Locked and absent upstream values remain absent, never zero.
                    key=actual_day if recipe['temporal']=='series' else None
                    row=merged.setdefault(key,{'symbol':q['symbol'],'date':key})
                    if field in row and row[field] is not None and result is not None and row[field]!=result:raise RuntimeError('iFinD转债同日同指标出现不同值')
                    if result is not None or field not in row:row[field]=result
    return [merged[k] for k in sorted(merged,key=lambda k:k or '')], [raw]
