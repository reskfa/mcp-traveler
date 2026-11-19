"""Fixed-parameter TDX bond K-lines; never reuse attached current quotes as history."""
from datetime import date
from .basic_info import typed,pick
from .dictionary import fields
from .parsing import json_objects,norm_date,tables


def payloads(raw):
    """Read typed JSON envelopes and embedded text while keeping K-line identity."""
    if isinstance(raw,dict):
        if 'Rows' in raw and 'Code' in raw:
            yield raw
            return
        structured=raw.get('structuredContent')
        if isinstance(structured,dict):
            yield from payloads(structured)
            return
        for value in raw.values():
            yield from payloads(value)
    elif isinstance(raw,list):
        for value in raw:yield from payloads(value)
    elif isinstance(raw,str):
        for value in json_objects(raw):yield from payloads(value)


def fetch(recipe,bindings,q,remote):
    from .acquisition import context,parameters
    ctx=context(q,recipe,bindings);raw=[];out={};previous_oldest=None
    pagination=recipe['pagination']
    for page in range(pagination['max_pages']):
        result=remote.call(recipe['method'],parameters(recipe,{**ctx,'offset':str(page*pagination['size'])},q));raw.append(result)
        blocks=list(payloads(result))
        if not blocks:raise RuntimeError('通达信转债结果缺少可核对证券身份的K线JSON')
        batch=[]
        for block in blocks:
            if str(block['Code'])!=ctx['code'] or str(block.get('Setcode'))!=ctx['market_code'] or str(block.get('Period'))!='4':
                raise RuntimeError('通达信转债K线的证券／市场／日线周期不匹配')
            batch.extend(block['Rows'])
            if q['dataset']=='cb_basic':
                info=block.get('AttachInfo',{})
                row={'symbol':q['symbol'],'date':None}
                for f,b in bindings.items():row[f]=typed(pick(info,b['columns']),fields(q['dataset'])[f]['data_type'],b)
                return ([row] if any(row[f] is not None for f in bindings) else []),raw
        if not batch:break
        days=[]
        for item in batch:
            day=norm_date(item.get('Data'))
            if not day:raise RuntimeError('通达信转债K线缺少日期')
            date.fromisoformat(day);days.append(day)
            if not q['start_date']<=day<=q['end_date']:continue
            row={'symbol':q['symbol'],'date':day}
            for f,b in bindings.items():
                if f=='volume' and item.get('Unit')!=1:raise RuntimeError('通达信转债成交量单位标志变化，拒绝套用已核实口径')
                row[f]=typed(pick(item,b['columns']),fields(q['dataset'])[f]['data_type'],b)
            out[day]=row
        oldest=min(days)
        if previous_oldest is not None and oldest>=previous_oldest:raise RuntimeError('通达信转债历史分页没有向前推进')
        if oldest<=q['start_date'] or len(batch)<pagination['size']:break
        previous_oldest=oldest
    else:raise RuntimeError('通达信转债历史分页达到上限，请缩小日期范围')
    return list(out.values()),raw
