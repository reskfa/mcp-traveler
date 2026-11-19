import asyncio
import csv
import io
import json
import sys
import time
import uuid
from datetime import datetime,timezone,date
from pydantic import BaseModel,Field,model_validator,StrictInt
from typing import Literal
from . import config
from .catalog import FIELDS,UNITS,DEFAULT_FIELDS,source_definitions
from .dictionary import plan,reload_dictionary,fields as dictionary_fields

def now():return datetime.now(timezone.utc).isoformat()

class Query(BaseModel):
    dataset: Literal['daily_prices','valuations','financials','technical_indicators','basic_info','cb_basic','cb_daily','cb_clauses']
    symbol: str = Field(pattern=r'^\d{6}\.(SH|SZ|BJ)$')
    start_date: str | None = None
    end_date: str | None = None
    date: str | None = None
    period: str | None = None
    fields: list[str] | None = None
    provider: Literal['auto','tushare','akshare','wind','ifind','tdx','choice']='auto'
    adjust: Literal['none','qfq','hfq','native'] | None = None
    parameters: dict[str,StrictInt] | None = None
    frequency: Literal['daily']='daily'
    @model_validator(mode='after')
    def validate_request(self):
        reload_dictionary(force=False)
        if self.dataset=='technical_indicators':
            from .technical import normalized_parameters
            self.adjust=self.adjust or 'native'
            if self.date:
                self.start_date=self.end_date=self.date
        else:
            self.adjust=self.adjust or 'none'
            if self.adjust=='native':raise ValueError('native仅适用于技术指标')
            if self.parameters:raise ValueError('parameters仅适用于技术指标')
        if self.dataset in ['basic_info','cb_basic'] and (any([self.start_date,self.end_date,self.date,self.period]) or self.adjust!='none'):
            raise ValueError('基本信息仅查询当前档案及来源提供的历史记录，不接受历史时点或复权参数')
        for value in [self.start_date,self.end_date,self.date,self.period]:
            if value and date.fromisoformat(value).isoformat()!=value:raise ValueError('日期使用 YYYY-MM-DD')
        if self.dataset in ['daily_prices','cb_daily'] and (not self.start_date or not self.end_date):raise ValueError('日线需要起止日期')
        is_cb=(self.symbol.startswith('11') and self.symbol.endswith('.SH')) or (self.symbol.startswith('12') and self.symbol.endswith('.SZ'))
        if is_cb and not self.dataset.startswith('cb_'):raise ValueError('可转债代码请使用 cb_basic、cb_daily 或 cb_clauses 数据类别')
        if self.dataset.startswith('cb_'):
            if not is_cb:raise ValueError('可转债代码须为11开头上海或12开头深圳证券代码')
            if self.date:raise ValueError('转债请使用start_date/end_date，档案不接受日期')
            if self.adjust!='none' or self.period:raise ValueError('转债不接受复权或股票报告期参数')
            if self.dataset=='cb_clauses' and bool(self.start_date)!=bool(self.end_date):raise ValueError('公告查询需要成对起止日期')
        if self.start_date and self.end_date and self.start_date>self.end_date:raise ValueError('开始日期不能晚于结束日期')
        if self.dataset=='financials' and not self.period:raise ValueError('财务查询需要报告期')
        self.fields=list(FIELDS[self.dataset]) if self.fields==['*'] else (self.fields or list(DEFAULT_FIELDS[self.dataset]))
        if set(self.fields)-set(FIELDS[self.dataset]):raise ValueError('包含不支持的字段')
        if self.dataset=='technical_indicators':
            from .technical import resolve_parameters,field_for,field_profile,profile
            selected=self.fields
            if hasattr(self,'name'):
                matched=field_for(self.name)
                selected=[matched] if matched else []
            snapshot=bool(selected) and all(profile(field_profile(f))['technical'].get('temporal')=='snapshot' for f in selected)
            if bool(self.start_date)!=bool(self.end_date) or (not snapshot and not self.start_date):
                raise ValueError('历史技术指标需要起止日期或date；只有最新快照可省略日期')
            if not hasattr(self,'name'):resolve_parameters(self.model_dump())
        return self

async def invoke(name,query,timeout):
    process=await asyncio.create_subprocess_exec(sys.executable,'-m','gateway.worker',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,cwd=str(config.ROOT),env={**__import__('os').environ,'MCP_TRAVELER_HOME':str(config.ROOT)})
    try:
        stdout,_=await asyncio.wait_for(process.communicate(json.dumps({'provider':name,'query':query}).encode()),timeout)
    except (asyncio.TimeoutError,asyncio.CancelledError):
        if process.returncode is None:process.kill()
        await process.wait()
        raise
    text=stdout.decode('utf-8',errors='replace')
    marker='__GATEWAY_RESULT__'
    if marker not in text:raise RuntimeError('取数进程未返回有效结果')
    payload=json.loads(text.rsplit(marker,1)[1])
    if not payload['ok']:raise RuntimeError(payload['error'])
    return payload['result']

def available(name,p):
    if not p.get('enabled',True):return False
    if name=='akshare':return True
    key=config.secrets().get(config.KEYS[name],'')
    return bool(key and '填写' not in key and (not p.get('mcp_servers') or next(iter(p['mcp_servers'].values())).get('url')))

def record(name,ok,error=None):
    with config.LOCK:
        s=config.status()['providers'].get(name,{})
        config.update_status(name,{'local_calls':s.get('local_calls',0)+1,'local_successes':s.get('local_successes',0)+int(ok),'last_call_at':now(),'last_error':error,**({'status':'connected','checked_at':now(),'connection_note':'最近一次真实取数成功'} if ok else {})})

async def probe(name):
    p=config.load()['providers'][name]
    if not available(name,p):return {'ok':False,'error':'来源未启用或配置未完成'}
    start=time.monotonic()
    try:
        result=await invoke(name,{'probe':True},p.get('timeout_seconds',60))
        patch={'status':'connected','checked_at':now(),'connection_note':result.get('note','连接成功')}
        if 'tools' in result:config.write_json(config.ROOT/f'local/schemas/{name}.json',result)
        config.update_status(name,patch)
        return {'ok':True,'elapsed_seconds':round(time.monotonic()-start,2),**patch}
    except Exception as exc:
        msg=config.redact(str(exc) or '连接超时')
        config.update_status(name,{'status':'error','checked_at':now(),'connection_note':msg})
        return {'ok':False,'error':msg}

async def query(request:Query):
    request=Query.model_validate(request.model_dump())
    c=config.load();q=request.model_dump();attempts=[];start=time.monotonic()
    if request.dataset=='technical_indicators':
        from .technical import resolve_parameters
        q['resolved_parameters']=resolve_parameters(q)
        if len(q['resolved_parameters'])==1:q['parameters']=next(iter(q['resolved_parameters'].values()))
    order=c['routes'].get(q['dataset'],c.get('global_route',config.PROVIDERS)) if request.provider=='auto' else [request.provider]
    data=None; provenance=[];raws=[];notes=[];contributors=[];upstreams=[]
    missing=list(request.fields)
    for name in order:
        p=c['providers'][name]
        if not available(name,p):attempts.append({'provider':name,'status':'skipped','error':'未启用或配置未完成'});continue
        if not plan(name,q['dataset'],missing):
            attempts.append({'provider':name,'status':'unsupported','error':'当前尚未接入所选公告事件字段的取法' if q['dataset']=='cb_clauses' else '字典中没有所缺字段的取数方案'});continue
        tick=time.monotonic();subquery={**q,'fields':missing}
        # Latest snapshots must refer to the same trading date when filled.
        if data and q['dataset']=='valuations' and not q.get('date'):
            dates={r.get('date') for r in data}
            if len(dates)==1 and None not in dates:subquery['date']=next(iter(dates))
        try:
            result=await invoke(name,subquery,p.get('timeout_seconds',60))
            rows=result['data'];raws.append({'provider':name,'query':subquery,'raw':result['raw']})
            notes.extend(name+'：'+note for note in result.get('notes',[]))
            record(name,True);filled=0
            if data is None or (not data and q['dataset'].startswith('cb_')):
                data=[{'symbol':r['symbol'],'date':r.get('date'),**({'event_id':r['event_id']} if 'event_id' in r else {}),**{f:r.get(f) for f in request.fields}} for r in rows]
                provenance=[{f:name for f in request.fields if r.get(f) is not None} for r in data]
                filled=sum(len(p) for p in provenance)
            else:
                lookup={(r['symbol'],r.get('date'),r.get('event_id')):r for r in rows if r.get('date') or q['dataset'] in ['basic_info','cb_basic']}
                for i,row in enumerate(data):
                    extra=lookup.get((row['symbol'],row.get('date'),row.get('event_id')), {})
                    for f in missing:
                        # Adjusted prices can have different anchors across providers.
                        if q['dataset']=='daily_prices' and q['adjust']!='none' and f in ['open','high','low','close']:continue
                        if row.get(f) is None and extra.get(f) is not None:
                            row[f]=extra[f];provenance[i][f]=name;filled+=1
            if filled or not contributors:
                contributors.append(name);upstreams.append(result.get('upstream',name))
            missing=[f for f in request.fields if (not data and q['dataset'].startswith('cb_')) or any(r.get(f) is None for r in data)]
            attempts.append({'provider':name,'status':'partial' if missing else 'success','requested_fields':subquery['fields'],'filled_cells':filled,'missing_fields':missing,'seconds':round(time.monotonic()-tick,2)})
            if request.provider!='auto' or (not data and not q['dataset'].startswith('cb_')) or not missing:break
        except Exception as exc:
            msg=config.redact(str(exc) or '请求超时')
            attempts.append({'provider':name,'status':'error','error':msg,'requested_fields':subquery['fields'],'seconds':round(time.monotonic()-tick,2)})
            record(name,False,msg)
    unsupported=(data is None and q['dataset']=='cb_clauses' and any(a['status']=='unsupported' for a in attempts) and not any(a['status']=='error' for a in attempts))
    if unsupported:
        data=[]
        notes.append('所选来源尚未接入这些历史公告事件字段；本次未调用上游，不代表没有公告或未触发条款。')
        notes.append('查赎回、回售、下修的合同原文，请使用 cb_basic 的 call_clause、put_clause、reset_clause；当前条款不等于历史公告。')
    if data is None:raise QueryFailure(attempts)
    if missing:notes.append('仍有缺失字段：'+', '.join(missing))
    if len(contributors)>1:notes.append('按证券和日期逐项补齐，保留优先来源已有值；每个字段的来源见字段来源。')
    if request.dataset in ['basic_info','cb_basic']:notes.append('档案为当前可用快照；列表按字段保留优先来源，不将不同来源的名单拼成完整历史。')
    if request.dataset=='cb_clauses':notes.append('日期为公告日期；仅表示返回的披露记录，不推断未披露期间的条款状态。')
    if request.dataset=='daily_prices' and request.adjust!='none':notes.append('复权价格不跨来源补齐；其他字段仍可补齐。')
    paired=sorted(zip(data,provenance),key=lambda pair:pair[0].get('date') or '')
    data=[r for r,_ in paired];provenance=[p for _,p in paired]
    rid=uuid.uuid4().hex;folder=config.ROOT/'local/results';folder.mkdir(parents=True,exist_ok=True)
    config.write_json(folder/f'{rid}.raw.json',config.redact(raws[0]['raw'] if len(raws)==1 else raws))
    from .coverage import coverage
    data_coverage=coverage(raws,data)
    if data_coverage['status']=='partial':notes.append(data_coverage['note'])
    answer={'status':'unsupported' if unsupported else 'success','requested_provider':request.provider,'alternatives':[{'dataset':'cb_basic','fields':['call_clause','put_clause','reset_clause'],'description':'当前合同条款原文（非历史公告）','provider':'auto'}] if unsupported else [],'coverage':data_coverage,'id':rid,'dataset':q['dataset'],'provider':contributors[0] if len(contributors)==1 else ('mixed' if contributors else None),'providers':contributors,'upstream':' + '.join(upstreams),'query':q,'fetched_at':now(),'elapsed_seconds':round(time.monotonic()-start,2),'row_count':len(data),'data':data[:100],'field_sources':provenance[:100],'missing_fields':missing,'complete':not missing and data_coverage['status']!='partial','fields':{f:{'label':FIELDS[q['dataset']][f],'unit':UNITS[q['dataset']][f],'source_definitions':source_definitions(q['dataset'],f,contributors),**{k:v for k,v in dictionary_fields(q['dataset'])[f].items() if k in ['data_type','group','record_schema']}} for f in request.fields},'notes':notes,'attempts':attempts,'raw_path':str(folder/f'{rid}.raw.json'),'data_path':str(folder/f'{rid}.json'),'download_url':f'/api/results/{rid}/csv','raw_url':f'/api/results/{rid}/raw'}
    config.write_json(folder/f'{rid}.json',{**answer,'data':data,'field_sources':provenance})
    return answer

class QueryFailure(Exception):
    def __init__(self,attempts):self.attempts=attempts;super().__init__('没有来源完成查询')
