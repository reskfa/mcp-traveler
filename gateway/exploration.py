"""Bounded discovery of explicit numeric fields; never guesses SDK endpoints."""
import asyncio
import copy
import hashlib
import json
import re
import sys
import time
import uuid
from typing import Literal
from pydantic import Field,field_validator
from . import config, core, dictionary
from .parsing import tables, compact, number, norm_date, map_rows, scale_factor

class ExplorationRequest(core.Query):
    dataset: Literal['daily_prices','valuations','financials','technical_indicators']
    auto_save: bool = True
    name: str = Field(min_length=1,max_length=100)
    unit: str | None = None
    aliases: list[str] = Field(default_factory=list,max_length=20)
    max_attempts: int = Field(default=2,ge=1,le=3)
    fields: list[str] | None = Field(default=None,exclude=True)

    @field_validator('name')
    @classmethod
    def nonempty_name(cls,value):
        if not value.strip():raise ValueError('指标名称不能为空')
        return value.strip()

    # Query's date validation is reused with default supported fields; arbitrary name
    # is separately resolved, never passed to Query as an unchecked field.

def _names(request):
    return list(dict.fromkeys([request.name.strip(),*[a.strip() for a in request.aliases if a.strip()]]))

def _known(request):
    wanted={compact(n) for n in _names(request)}
    return [f for f,m in dictionary.fields(request.dataset).items() if wanted & {compact(f),compact(m['label']),*[compact(a) for a in m.get('aliases',[])]}]

def _recipe(provider,dataset,name=None):
    if dataset=='technical_indicators':
        from .technical import field_for,field_profile
        field=field_for(name or '')
        return copy.deepcopy(dictionary.DICTIONARY['recipes'][field_profile(field)]) if provider=='tdx' and field else None
    options=[r for r in dictionary.DICTIONARY['recipes'].values() if r['provider']==provider and r['dataset']==dataset and r['kind']=='mcp_nl']
    if not options:return None
    r=copy.deepcopy(options[0])
    r['template']='查询{symbol}，{when}的{labels}。{qualifier}只返回指定指标的数据表，必须含实际证券代码、实际日期、指标全名及明确单位；无法提供请留空。'
    r['qualifier']='按请求的指标原义返回，保留累计、单季度、期末或TTM口径说明，不要擅自替换口径。'
    return r

_UNIT=r'(万亿|亿|万)?(元/股|元|股|%|倍|天|次|手)'
def _unit(key,value,units):
    u=units.get(key,'')
    if not u:
        m=re.search(r'(?:单位\s*[:：]?\s*|[（(])('+_UNIT+r')(?:[）)]|$)',str(key))
        if m:u=m[1]
    if not u and isinstance(value,str):
        m=re.search('('+_UNIT+r')$',value.strip())
        if m:u=m[1]
    return str(u).replace('人民币','').strip()

def _clean(key):
    s=re.sub(r'^(20\d{2})年(?:\d{1,2}月\d{1,2}日|年报)?','',str(key))
    s=re.sub(r'[（(](?:报告期|截止日期|交易日)[:：]\d{8}[）)]','',s)
    s=re.sub(r'(?:[（(]?单位\s*[:：]?\s*|[（(])'+_UNIT+r'[）)]?$', '',s)
    return compact(s)

def validate_response(request,provider,recipe,raw):
    """Only table-owned evidence is trusted, never the echoed question."""
    if request.dataset=='technical_indicators':
        from .technical import candidate
        return candidate(request,raw)
    matches=[];reasons=[]
    for row,units in tables(raw):
        codes=[str(v).upper() for k,v in row.items() if k.lower() in ['symbol','code','ts_code','security_code'] or k in ['证券代码','股票代码','代码','Wind代码']]
        code=request.symbol.split('.')[0]
        if not any(v==request.symbol or v==code or v==code+{'SH':'.XSHG','SZ':'.XSHE','BJ':'.XBSE'}[request.symbol.split('.')[1]] for v in codes):
            reasons.append('返回没有匹配的证券代码');continue
        for key,value in row.items():
            if _clean(key) not in {compact(n) for n in _names(request)}:continue
            unit=_unit(key,value,units)
            if not re.fullmatch(_UNIT,unit):reasons.append('匹配列缺少可确认单位');continue
            target=request.unit or re.sub(r'^(万亿|亿|万)','',unit)
            if not re.fullmatch(_UNIT,target) or re.sub(r'^(万亿|亿|万)','',target)!=re.sub(r'^(万亿|亿|万)','',unit):
                reasons.append('来源单位与目标单位不兼容');continue
            if request.dataset=='financials':
                evidence=str(key)+' '+str(row.get('口径',''))+' '+str(row.get('报表类型',''))
                scopes=[s for s in ['单季度','累计','期末','TTM'] if s.lower() in evidence.lower()]
                wanted=[s for s in ['单季度','累计','期末','TTM'] if s.lower() in request.name.lower()]
                if wanted and not set(wanted)<=set(scopes):
                    reasons.append('财务返回未明确累计、单季度、期末或TTM口径');continue
            column=_clean(key)
            binding={'columns':[column],'extra_columns':[column+'('+unit+')'],'accepted_units':[unit],'multiplier':1/scale_factor(target),'source_unit':unit,'prompt':request.name,'note':f'{provider}原始字段：{column}；采用来源原生算法，返回单位统一为{target}。'}
            # map_rows already checks actual table dates first for iFinD.
            local_row=dict(row)
            stamp=re.match(r'^(20\d{2})年(?:(\d{1,2})月(\d{1,2})日)?',str(key))
            actual_date=None
            if stamp:
                if stamp[2]:actual_date=f'{int(stamp[1]):04d}-{int(stamp[2]):02d}-{int(stamp[3]):02d}'
                elif request.dataset=='financials' and request.period and request.period.endswith('-12-31'):actual_date=stamp[1]+'-12-31'
                if actual_date:local_row['date']=actual_date
                else:continue

            if isinstance(value,str) and number(value,unit) is None:
                suffix=re.fullmatch(r'\s*([-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*'+re.escape(unit)+r'\s*',value)
                if suffix:local_row[key]=suffix[1]
            mapped,_=map_rows([(local_row,{**units,key:unit})],['value'],request.symbol,request.dataset,bindings={'value':binding},expected_date=request.period or request.date,require_indicator_date=provider=='ifind' and bool(request.period or request.date),prefer_row_date=True)
            for item in mapped:
                day=item['date']
                if not day or norm_date(day)!=day:continue
                if request.period or request.date:
                    if day!=(request.period or request.date):continue
                if request.dataset=='daily_prices' and not request.start_date<=day<=request.end_date:continue
                if item.get('value') is not None:matches.append((key,target,binding,item))
    keys={(x[0],x[1]) for x in matches}
    if len(keys)!=1:return None, list(dict.fromkeys(reasons+(['匹配列不唯一'] if keys else ['未获得字段、证券、日期和单位一致的数值'])))
    key,unit,binding,_=matches[0]
    field=re.sub('[^a-z0-9_]+','_',request.name.lower()).strip('_') or 'metric_'+hashlib.sha256(request.name.encode()).hexdigest()[:12]
    rid='explore_'+provider+'_'+hashlib.sha256((request.dataset+request.name).encode()).hexdigest()[:12]
    definition={'label':request.name,'unit':unit,'aliases':_names(request),'description':request.name+'；来源原生算法，日期与口径以原始返回为依据','sources':{p:([dict(binding,recipe=rid)] if p==provider else []) for p in config.PROVIDERS},'unavailable':{},'default_selected':False}
    return {'dataset':request.dataset,'field':field,'definition':definition,'recipes':{rid:recipe},'provider':provider,'data':[{'symbol':m[3]['symbol'],'date':m[3]['date'],field:m[3]['value']} for m in matches],'source_column':key,'notes':['财务指标未额外指定累计或单季度时，采用精确字段名对应的来源原生口径。']},[]

async def invoke(provider,request,recipe,timeout):
    process=await asyncio.create_subprocess_exec(sys.executable,'-m','gateway.exploration_worker',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,cwd=str(config.ROOT),env={**__import__('os').environ,'MCP_TRAVELER_HOME':str(config.ROOT)})
    try:stdout,_=await asyncio.wait_for(process.communicate(json.dumps({'provider':provider,'request':request.model_dump(),'recipe':recipe}).encode()),timeout)
    except (asyncio.TimeoutError,asyncio.CancelledError):
        if process.returncode is None:process.kill()
        await process.wait();raise
    marker='__GATEWAY_RESULT__'
    if marker not in stdout.decode(errors='replace'):raise RuntimeError('探索进程未返回有效结果')
    payload=json.loads(stdout.decode().rsplit(marker,1)[1])
    if not payload['ok']:raise RuntimeError(payload['error'])
    return payload['result']

async def explore(request:ExplorationRequest):
    from .generic_data import infer_market
    if infer_market(request.symbol)=='convertible_bond':
        raise ValueError('可转债未知指标请用 explore_data，market_hint=convertible_bond；不要用股票类别探索。已有字段用 cb_basic/cb_daily/cb_clauses 查询。')
    known=_known(request)
    if request.dataset=='technical_indicators':
        # Precomputed SDK fields already have an exact, reusable dictionary
        # binding. Do not send their names to an unrelated vendor's MCP.
        exact=[f for f in known if f==request.name]
        if exact:known=exact
        elif request.parameters:
            known=[f for f in known if not any(
                dictionary.DICTIONARY['recipes'][b['recipe']].get('technical',{}).get('fixed')
                for bindings in dictionary.fields(request.dataset)[f]['sources'].values()
                for b in bindings)]
        if request.provider!='auto':
            known=[f for f in known if dictionary.fields(request.dataset)[f]['sources'].get(request.provider)]
        sdk_known=[f for f in known if any(
            (dictionary.DICTIONARY['recipes'][b['recipe']]['kind'].startswith('sdk') or dictionary.DICTIONARY['recipes'][b['recipe']].get('handler') in {'wind_technical','ifind_technical','wind_technical_history'})
            for bindings in dictionary.fields(request.dataset)[f]['sources'].values()
            for b in bindings)]
        if sdk_known:
            if len(known)!=1:return {'status':'needs_confirmation','matches':known,'attempts':[],'network_calls':0}
            q=core.Query(**{**request.model_dump(exclude={'name','unit','aliases','max_attempts','fields'}),'fields':known})
            return {'status':'existing','existing_field':known[0],'result':await core.query(q),'attempts':[]}
        from .technical import field_for
        selected=field_for(request.name)
        if not selected or not dictionary.fields(request.dataset)[selected]['sources'].get('tdx'):
            return {'status':'needs_confirmation','attempts':[],'network_calls':0,'data':[],'notes':['请明确选择该来源支持的技术指标分量。']}
        # A display name may be shared by several saved parameter versions.
        # Only an exact personal ID reuses a saved entry; a named exploration
        # performs fresh vendor validation for the requested parameters.
        known=[f for f in known if request.name==f and f in dictionary._read_user().get('datasets',{}).get(request.dataset,{}).get('fields',{})]
    if len(known)==1:
        q=core.Query(**{**request.model_dump(exclude={'name','unit','aliases','max_attempts','fields'}),'fields':known})
        return {'status':'existing','existing_field':known[0],'result':await core.query(q),'attempts':[]}
    if len(known)>1:return {'status':'needs_confirmation','matches':known,'attempts':[]}
    started=time.monotonic();rid=uuid.uuid4().hex;attempts=[];candidate=None;raws=[];c=config.load();used=0
    order=c['routes'][request.dataset] if request.provider=='auto' else [request.provider]
    budget=request.max_attempts*60
    for provider in dict.fromkeys(order):
        if used>=request.max_attempts or time.monotonic()-started>=budget:break
        recipe=_recipe(provider,request.dataset,request.name)
        if not recipe or provider not in ['wind','ifind','tdx']:
            attempts.append({'provider':provider,'status':'skipped','reason':'没有明确的自然语言工具或SDK接口字段映射，不猜测接口'});continue
        if not core.available(provider,c['providers'][provider]):attempts.append({'provider':provider,'status':'skipped','reason':'来源未配置或未启用'});continue
        used+=1;tick=time.monotonic()
        try:
            raw=await invoke(provider,request,recipe,min(60,c['providers'][provider].get('timeout_seconds',60),budget-(tick-started)))
            raws.append({'provider':provider,'recipe':recipe,'raw':raw})
            candidate,reasons=validate_response(request,provider,recipe,raw)
            attempts.append({'provider':provider,'status':'success' if candidate else 'needs_confirmation','reasons':reasons,'seconds':round(time.monotonic()-tick,2)})
            core.record(provider,bool(candidate),None if candidate else '；'.join(reasons))
            if candidate:break
        except Exception as exc:
            message=config.redact(str(exc) or '探索超时');core.record(provider,False,message)
            attempts.append({'provider':provider,'status':'error','error':message,'seconds':round(time.monotonic()-tick,2)})
    folder=config.ROOT/'local/explorations';raw_path=folder/f'{rid}.raw.json'
    answer={'id':rid,'status':'success' if candidate else ('needs_confirmation' if raws else 'failed'),'request':request.model_dump(),'network_calls':used,'attempt_limit':request.max_attempts,'attempts':attempts,'candidate':candidate,'data':candidate['data'] if candidate else [],'elapsed_seconds':round(time.monotonic()-started,2),'raw_path':str(raw_path),'notes':['来源算法允许不同；只有字段含义、单位、证券、日期及财务口径明确时可保存。']}
    from .coverage import coverage
    answer['coverage']=coverage(raws,answer['data'])
    answer['notes'].append(answer['coverage']['note'])
    answer['saved']=False
    config.write_json(raw_path,config.redact(raws));config.write_json(folder/f'{rid}.json',config.redact(answer))
    if candidate and request.auto_save:
        try:
            saved=await save_exploration(rid)
            answer.update(saved=True,saved_field=saved['field'],save_status='saved')
            answer['notes'].append('已自动保存到个人字典，下次可直接查询；可在字典页删除。')
        except ValueError as exc:
            answer.update(status='needs_confirmation',save_status='needs_confirmation',save_error=config.redact(str(exc)))
        except Exception as exc:
            answer.update(save_status='failed',save_error=config.redact(str(exc)))
            answer['notes'].append('取数成功，但个人字典保存失败；可重试保存。')
    config.write_json(folder/f'{rid}.json',config.redact(answer))
    return answer

async def save_exploration(id,field=None):
    if not re.fullmatch('[a-f0-9]{32}',id):raise ValueError('探索编号无效')
    folder=config.ROOT/'local/explorations'
    if not (folder/f'{id}.json').is_file() or not (folder/f'{id}.raw.json').is_file():raise ValueError('找不到这次探索记录，请重新探索')
    stored=json.loads((folder/f'{id}.json').read_text());raws=json.loads((folder/f'{id}.raw.json').read_text())
    if stored['status']!='success' and not (stored.get('save_status')=='needs_confirmation' and stored.get('candidate')):raise ValueError('探索尚未得到可保存的候选')
    original=stored['candidate'];response=next(r for r in raws if r['provider']==original['provider'])
    candidate,reasons=validate_response(ExplorationRequest(**stored['request']),response['provider'],response['recipe'],response['raw'])
    if not candidate:raise ValueError('候选重新校验失败：'+'；'.join(reasons))
    chosen=field or candidate['field']
    if not re.fullmatch('[a-z][a-z0-9_]{0,79}',chosen):raise ValueError('字段名须为英文小写字母、数字或下划线')
    with config.LOCK:
        dictionary.reload_dictionary(force=False)
        if chosen in dictionary.fields(candidate['dataset']):
            if stored.get('saved') and stored.get('saved_field')==chosen and dictionary.fields(candidate['dataset'])[chosen]==candidate['definition']:
                return {'id':id,'status':'saved','dataset':candidate['dataset'],'field':chosen,'already_saved':True}
            raise ValueError('字段ID已存在，请确认是否为同一口径；不会覆盖已有指标')
        result=dictionary.save_user_metric(candidate['dataset'],chosen,candidate['definition'],candidate['recipes'])
    from .coverage import coverage
    stored.setdefault('coverage',coverage(raws,candidate['data']))
    stored.update(saved_field=chosen,saved=True,save_status='saved');config.write_json(folder/f'{id}.json',stored)
    return {'id':id,'status':'saved','dataset':candidate['dataset'],'field':chosen,'metric':result}
