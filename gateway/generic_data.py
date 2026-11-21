"""Discover read-only upstream capabilities before exploring non-predefined records.

No business datasets or shareholder fields are pre-registered here. Capabilities
come from tools/list; successful, evidenced table recipes live in the personal
user_dictionary.json alongside numeric definitions.
"""
import copy
import hashlib
import json
import re
import time
import uuid
from datetime import date, datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from . import config, core, dictionary
from .parsing import tables, norm_date, json_objects
from .market_provider import normalize_symbol, symbol_for_row

REMOTE_PROVIDERS=config.MCP_PROVIDERS
IDENTITY_COLUMNS=('symbol','code','ts_code','security_code','证券代码','股票代码','债券代码','代码','Wind代码')
DATE_COLUMNS=('date','trade_date','end_date','报告期','报告日期','截止日期','日期','数据日期','交易日期')
LIST_DATE_COLUMNS=('list_date','上市日期')
DELIST_DATE_COLUMNS=('delist_date','退市日期','摘牌日期')
DATE_PATTERN=r'(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?'

def infer_market(symbol, market_hint=None):
    inferred='convertible_bond' if re.match(r'^(?:11\d{4}\.SH|12\d{4}\.SZ)$',symbol) else 'equity'
    if market_hint and market_hint!=inferred:raise ValueError('证券代码与市场类型不一致')
    return market_hint or inferred

class ExploreDataRequest(BaseModel):
    request: str = Field(min_length=1,max_length=300)
    symbol: str | None = None
    query_scope: Literal['security','market'] = 'security'
    market_kind: Literal['universe','daily'] | None = None
    market_hint: Literal['equity','convertible_bond'] | None = None
    period: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    provider: Literal['auto','tushare','akshare','wind','ifind','tdx','choice']='auto'
    max_attempts: int = Field(default=2,ge=1,le=3)
    auto_save: bool = True

    @model_validator(mode='after')
    def valid(self):
        self.request=self.request.strip()
        if not self.request:raise ValueError('请说明要查询的信息')
        if self.query_scope=='market':
            if self.symbol is not None:raise ValueError('全市场探索不接受symbol；省略代码即查询全部证券')
            if not self.market_hint:raise ValueError('全市场探索必须指定market_hint')
            if not self.market_kind:raise ValueError('全市场探索必须指定market_kind=universe或daily')
            if self.period:raise ValueError('市场探索不接受period；日线用日期区间，名单用end_date表示历史时点')
            if self.market_kind=='daily' and bool(self.start_date)!=bool(self.end_date):raise ValueError('市场日线需同时提供起止日期')
            if self.market_kind=='universe' and self.start_date:raise ValueError('市场名单用end_date表示历史时点，不接受start_date')
        else:
            if not self.symbol or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)',self.symbol):raise ValueError('单证券探索必须提供有效symbol')
            if self.market_kind:raise ValueError('单证券探索不接受market_kind')
            self.market_hint=infer_market(self.symbol,self.market_hint)
            if bool(self.start_date)!=bool(self.end_date):raise ValueError('时间范围需同时提供起止日期')
            if self.period and self.start_date:raise ValueError('报告期和日期区间二选一')
        for value in (self.period,self.start_date,self.end_date):
            if value and date.fromisoformat(value).isoformat()!=value:raise ValueError('日期使用 YYYY-MM-DD')
        if self.start_date and self.start_date>self.end_date:raise ValueError('开始日期不能晚于结束日期')
        return self

class DataQueryRequest(BaseModel):
    query_id: str = Field(pattern=r'^data_[a-f0-9]{16}$')
    symbol: str | None = None
    query_scope: Literal['security','market'] | None = None
    market_kind: Literal['universe','daily'] | None = None
    market_hint: Literal['equity','convertible_bond'] | None = None
    period: str | None = None
    start_date: str | None = None
    end_date: str | None = None

    @model_validator(mode='after')
    def valid(self):
        if self.symbol is not None and not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)',self.symbol):raise ValueError('证券代码格式不正确')
        if self.query_scope=='security' and not self.symbol:raise ValueError('单证券取法必须提供symbol')
        if self.query_scope=='market' and self.symbol is not None:raise ValueError('市场取法不接受symbol')
        return self


def compact(text):return re.sub(r'[\s，,。:：；;（）()\-_]','',str(text)).casefold()

def topic(text):
    """Remove request framing, not business qualifiers such as 流通 or 控股."""
    value=re.sub(r'\d{6}\.(?:SH|SZ|BJ)','',text,flags=re.I)
    value=re.sub(DATE_PATTERN,'',value)
    value=re.sub(r'20\d{2}年','',value)
    value=re.sub(r'^(?:请|帮我|帮忙|查一下|找一下|找|看一下|获取|查看)+','',value)
    for word in ['请帮我查询','帮我查询','请查询','帮我找','请查','查询','看看','查找','这家公司的','这家公司','该公司的','该公司','最新报告期的','最新的','最近的','完整名单','名单','情况','信息']:
        value=value.replace(word,'')
    return compact(value).strip('的')

def request_topics(text):
    parts=[topic(x) for x in re.split(r'以及|及|和|、|，|,|并含|包含|含有',text)]
    return [p for p in parts if p]

def main_topic(text):
    text=re.split(r'[（(:：。；;]',text,maxsplit=1)[0]
    parts=request_topics(text)
    return parts[0] if parts else ''

def cached_tools(provider):
    path=config.ROOT/f'local/schemas/{provider}.json'
    if not path.exists():return []
    try:
        data=json.loads(path.read_text())
        return data.get('tools',[])
    except (ValueError,OSError):return []

async def ensure_tools(provider,refresh=False):
    cached=cached_tools(provider)
    if cached and all('description' in t for t in cached) and not refresh:return cached,None
    p=config.load()['providers'][provider]
    if not core.available(provider,p):return cached,'来源未启用或未配置'
    try:
        result=await core.invoke(provider,{'probe':True},min(20,p.get('timeout_seconds',60)))
        result['fetched_at']=core.now()
        config.write_json(config.ROOT/f'local/schemas/{provider}.json',result)
        return result.get('tools',[]),None
    except Exception as exc:return cached,config.redact(str(exc) or '能力目录刷新超时')

def runnable(tool):
    name=tool.get('name','')
    readonly=tool.get('annotations',{}).get('readOnlyHint') is True or bool(re.match(r'^(get_|search_|wenda_.*_query$|mx_)',name))
    schema=tool.get('inputSchema',{});props=schema.get('properties',{})
    nl=next((k for k in ('question','query') if props.get(k,{}).get('type')=='string'),None)
    required=set(schema.get('required',[]))
    supported={nl,'market','size','time_start','time_end','symbol','name','bdate','edate','keywords'}
    # Document/snippet tools are discoverable, but this iteration only verifies
    # structured records. Do not advertise a news search as a working parser.
    documents=bool(re.search(r'news|notice|report_query',name,re.I))
    return bool(readonly and nl and required<=supported and not documents)

def relevance(query,tool):
    target=main_topic(query)
    corpus=compact(tool.get('description','')+' '+tool.get('name','')+' '+json.dumps(tool.get('inputSchema',{}),ensure_ascii=False))
    if not target:return 0
    if target in corpus:return 100+len(target)
    grams={target[i:i+2] for i in range(len(target)-1)}
    hits=sum(g in corpus for g in grams)
    return hits/len(grams) if grams and hits/len(grams)>=.5 else 0


def market_tool_score(score,tool,market_hint,market_kind):
    name=tool.get('name','')
    corpus=name+' '+tool.get('description','')
    if market_hint=='convertible_bond':
        if re.search(r'mx_(?:hk|us|fund|macro)_',name,re.I):return 0
        if re.search(r'mx_ashare_',name,re.I) and not re.search(r'bond|债券|转债',corpus,re.I):return 0
        if re.search(r'bond|债券|转债',corpus,re.I):score+=100
    elif market_hint=='equity':
        if re.search(r'mx_(?:hk|us|fund|macro)_',name,re.I):return 0
        if re.search(r'mx_bond_',name,re.I) and not re.search(r'ashare|A股|股票',corpus,re.I):return 0
        if re.search(r'ashare|A股|股票',corpus,re.I):score+=100
    if market_kind=='daily' and re.search(r'index_block|板块|指数|宏观',corpus,re.I):score*=.05
    if market_kind=='universe' and re.search(r'screener|筛选|名单',corpus,re.I):score+=150
    return score


async def search_capabilities(query,provider='auto',refresh=False,limit=8,market_hint=None,query_scope='security',market_kind=None):
    if market_hint not in (None,'equity','convertible_bond'):raise ValueError('未知市场类型')
    if query_scope not in ('security','market'):raise ValueError('未知查询作用域')
    if query_scope=='market' and (not market_hint or market_kind not in ('universe','daily')):raise ValueError('市场能力搜索需要market_hint和market_kind')
    if query_scope=='security' and market_kind:raise ValueError('单证券能力搜索不接受market_kind')
    if provider not in ('auto',*config.PROVIDERS):raise ValueError('未知数据源')
    if not str(query).strip():raise ValueError('请描述所需信息')
    limit=max(1,min(20,int(limit)))
    c=config.load();order=c.get('global_route',config.PROVIDERS) if provider=='auto' else [provider]
    results=[];errors=[]
    # A partial dictionary match is only a suggestion, never a substitute answer.
    matches=dictionary.search_metrics(query,limit=limit)
    for m in matches['results']:
        if m.get('query_scope','security')!=query_scope:continue
        if query_scope=='market' and (m.get('market_kind')!=market_kind or m.get('market_hint')!=market_hint):continue
        if market_hint=='convertible_bond' and not (m['dataset'].startswith('cb_') or m.get('market_hint')=='convertible_bond'):continue
        if market_hint=='equity' and m['dataset'].startswith('cb_'):continue
        if provider!='auto' and provider not in m['sources']:continue
        exact=topic(query) in {topic(m['label']),topic(m['field']),*(topic(a) for a in m.get('aliases',[]))}
        results.append({**m,'kind':'saved_query' if m.get('query_id') else 'field','status':'ready' if m['sources'] else 'not_adapted','exact_match':exact,'description':m['description']})
    capabilities=[]
    for priority,p in enumerate(order):
        if p not in REMOTE_PROVIDERS:continue
        tools,error=await ensure_tools(p,refresh)
        if error:errors.append({'provider':p,'error':error})
        for t in tools:
            score=relevance(query,t)
            fallback=not score and market_hint=='convertible_bond' and runnable(t)
            if fallback:
                # Prefer broad company/security query surfaces over narrow risk
                # or indicator tools when the vendor omits CB topics in its schema.
                corpus=t.get('name','')+' '+t.get('description','')
                score=.03 if re.search(r'fundamental|基本面',corpus,re.I) else .02 if re.search(r'basic.?info|基本信息|基本资料',corpus,re.I) else .01
            if score and query_scope=='market':
                score=market_tool_score(score,t,market_hint,market_kind)
            if score:
                capabilities.append((priority,-score,{'kind':'upstream_tool','provider':p,'tool':t['name'],'description':t.get('description','')[:600],'status':'explorable' if runnable(t) else 'not_adapted','permission_status':'not_verified','required_parameters':t.get('inputSchema',{}).get('required',[]),'parameter_names':list(t.get('inputSchema',{}).get('properties',{}))[:24],'read_only':t.get('annotations',{}).get('readOnlyHint',runnable(t)),'score':score,'match_kind':'broad_readonly_candidate' if fallback else 'topic_match','confidence':'low' if fallback else 'topic_match','market_support':'not_verified'}))
    capabilities.sort(key=lambda x:(x[0],x[1]))
    results.extend(x[2] for x in capabilities)
    return {'query':query,'query_scope':query_scope,'market_kind':market_kind,'results':results[:limit],'total':len(results),'errors':errors,'next_step':'字典未精确命中时调用 explore_data；未适配不等于供应商没有能力。','external_search_recommended':False}


def build_arguments(tool,request):
    props=tool['inputSchema'].get('properties',{})
    market='可转债' if request.market_hint=='convertible_bond' else 'A股'
    required=set(tool['inputSchema'].get('required',[]))
    key='question' if 'question' in props else 'query'
    if request.query_scope=='market':
        if request.market_kind=='universe':
            when=f'截至{request.end_date}的历史时点' if request.end_date else '当前及历史已退出证券'
            question=(f'查询全部{market}的{request.request}，{when}。返回逐证券完整名单表，每行必须有真实证券代码、证券简称、完整上市日期；'
                      '已退市或摘牌证券另给完整退市/摘牌日期。不要注入样例证券，不要只返回当前证券或前若干条。'
                      '如只能返回部分数据，必须明确总数量和截断原因。')
        else:
            if request.start_date and request.start_date==request.end_date:
                question=(f'查询{request.start_date}全部{market}的逐券日行情，每只证券一行，列出真实证券代码、实际交易日和{request.request}。'
                          '返回全部证券的完整记录表，数值保留单位。不要注入样例证券，不要返回市场成交统计、总成交量、汇总数字、'
                          '固定size或前若干条样例；如只能返回部分数据，必须明确总数量和截断原因。')
            else:
                when=f'交易日期范围{request.start_date}至{request.end_date}' if request.start_date else '最新可用交易日，并给出每行实际交易日'
                question=(f'查询全部{market}的{request.request}，{when}。返回全部证券的逐券逐交易日完整记录表，'
                          '每行必须有真实证券代码和实际交易日（YYYY-MM-DD），数值保留单位。不要注入样例证券，不要用摘要、统计数、'
                          '市场成交统计、固定size或前若干条样例代替完整结果；如只能返回部分数据，必须明确总数量和截断原因。')
        args={key:question}
        optional={'market':market,'bdate':request.start_date,'edate':request.end_date}
    else:
        when=f'报告期精确为{request.period}' if request.period else f'发生日期范围{request.start_date}至{request.end_date}' if request.start_date else '最新可用报告期或最新记录，并给出实际日期'
        question=(f'查询{market}证券{request.symbol}的{request.request}，{when}。返回所请求对象的记录表，保留指标原义，不能用相近概念或单条摘要替代名单。'
                  '每行必须包含证券代码、实际报告期或发生日期（YYYY-MM-DD），按名称/数量/比例/排名等实际列给出；数量和比例标明单位。'
                  '必须单独给出实际日期列或在数据列名写明完整年月日，不要仅用年份标题，也不要以请求日期冒充实际日期。无法提供请留空。')
        args={key:question}
        optional={'market':market,'size':20,'symbol':request.symbol,'bdate':request.start_date or request.period,'edate':request.end_date or request.period}
    if request.query_scope=='market' and 'size' in required:raise ValueError('需要固定size上限的工具不能用于未截断的全市场探索')
    for k,v in optional.items():
        if k in props and v is not None and (k in required or k in ['market','symbol','bdate','edate']):args[k]=v
    for k,v in [('time_start',request.start_date or request.period),('time_end',request.end_date or request.period)]:
        if k in required:
            if not v:raise ValueError('该工具要求明确起止日期，请补充时间范围后探索')
            args[k]=v
    if required-set(args):raise ValueError('工具存在尚未适配的必填参数')
    return args


def date_in_header(column):
    m=re.search(DATE_PATTERN,str(column))
    if not m:return None
    try:return date(*map(int,m.groups())).isoformat()
    except ValueError:return None


def strict_date(value):
    parsed=norm_date(value)
    if not parsed:return None
    try:return date.fromisoformat(parsed).isoformat()
    except ValueError:return None

def normalized_column(column):
    s=re.sub(DATE_PATTERN,'',str(column))
    return re.sub(r'^20\d{2}年','',s).strip()


def semantic_match(request,columns):
    target=main_topic(request)
    # Matching contiguous topic text preserves qualifiers: 前十大股东 does not
    # match 前十大流通股东, and neither matches 控股股东.
    return bool(target and any(target in compact(normalized_column(c)) for c in columns))


def validate_records(raw,request):
    extracted=tables(raw);accepted=[];columns={};reasons=[];mismatched=0
    for row,units in extracted:
        if not semantic_match(request.request,row):continue
        identities=[str(row[k]).strip().upper() for k in IDENTITY_COLUMNS if row.get(k) is not None]
        allowed={request.symbol,request.symbol[:6],request.symbol[:6]+{'.SH':'.XSHG','.SZ':'.XSHE','.BJ':'.XBSE'}[request.symbol[6:]]}
        if not identities or any(v not in allowed for v in identities):
            mismatched+=1;continue
        dates={strict_date(row[k]) for k in DATE_COLUMNS if row.get(k) is not None and strict_date(row[k])}
        # Only requested-topic columns can supply dates; unrelated tables or
        # echoed questions cannot lend a date to this record.
        dates.update(date_in_header(k) for k in row if main_topic(request.request) in compact(normalized_column(k)) and date_in_header(k))
        if len(dates)>1:reasons.append('同一记录存在多个日期，无法确定所请求记录的实际日期');continue
        actual=next(iter(dates),None)
        if request.period and actual!=request.period:
            reasons.append('返回未明确匹配所请求的完整报告期');continue
        if request.start_date and (not actual or not request.start_date<=actual<=request.end_date):
            reasons.append('返回未明确匹配所请求日期范围');continue
        if not actual:
            reasons.append('记录缺少完整实际报告期/日期，不能据年份或请求推断');continue
        item={'symbol':request.symbol,'date':actual}
        for key,value in row.items():
            if key in IDENTITY_COLUMNS or key in DATE_COLUMNS:continue
            name=normalized_column(key)
            if name in item and item[name]!=value:raise ValueError('去除日期后出现冲突列名')
            item[name]=value
            unit=units.get(key,'') or '未提供'
            if name in columns and columns[name]['unit']!=unit:raise ValueError('同名列在返回中单位冲突')
            columns[name]={'label':name,'unit':unit or '未提供','data_type':'number' if isinstance(value,(int,float)) and not isinstance(value,bool) else 'string'}
        accepted.append(item)
    object_columns=[c for c in columns if main_topic(request.request) in compact(c) and re.search(r'名称|姓名|主体|对象',c)]
    if re.search(r'前(?:\d+|十|二十|五|三)大?',request.request) and not object_columns:
        return None,list(dict.fromkeys(reasons+(['返回证券不匹配'] if mismatched else [])+['排名名单缺少明确的主体名称列，不能用汇总数值替代名单']))
    if object_columns:
        usable=[]
        for row in accepted:
            if all(isinstance(row.get(c),str) and row[c].strip() not in ['', '--', '—', '暂无', '无数据'] for c in object_columns):usable.append(row)
            else:reasons.append('记录主体名称缺失，不能作为有效名单记录')
        accepted=usable
    if not accepted:
        return None,list(dict.fromkeys(reasons+(['返回证券不匹配'] if mismatched else [])+['未得到与原请求含义、证券和实际日期匹配的记录表']))
    # Remove exact duplicates, never splice tables from different dates into one
    # supposedly complete ranking.
    unique=[];seen=set()
    for row in accepted:
        key=json.dumps(row,sort_keys=True,ensure_ascii=False)
        if key not in seen:seen.add(key);unique.append(row)
    # A latest query may receive historical tables. Pick the actual greatest
    # evidenced date, never concatenate periods to satisfy a top-N count.
    if not request.period and not request.start_date and len({r['date'] for r in unique})>1:
        latest=max(r['date'] for r in unique)
        unique=[r for r in unique if r['date']==latest]
        accepted=[r for r in accepted if r['date']==latest]
        reasons.append('上游返回多个报告期，最新查询仅保留实际日期'+latest)
    from .coverage import coverage
    cov=coverage(raw,unique);cov['note']='取法与数据完整性分开；未验证所有记录均已返回。'
    reasons=list(dict.fromkeys(reasons))
    requested_extras=request_topics(request.request)[1:]
    missing_extras=[part for part in requested_extras if not any(part in compact(c) for c in columns)]
    if missing_extras:reasons.append('仍缺请求列：'+'、'.join(missing_extras))
    if len(unique)!=len(accepted):reasons.append('上游含重复记录，已去重；不宣称完整名单')
    for col in object_columns:
        names=[row[col].strip() for row in unique]
        if len(set(names))!=len(names):reasons.append('记录主体名称重复且内容不同，可能混入多个版本；不自动保存')
    rank_column=next((c for c in columns if c in ['排名','名次','序号']),None)
    ranks=[r.get(rank_column) for r in unique] if rank_column else []
    if ranks and len(set(map(str,ranks)))!=len(ranks):reasons.append('同一名次重复，可能存在多个名单或版本；不自动保存')
    # Generic top-N cardinality check, not a predefined shareholder mapping.
    top=re.search(r'前(\d+|十|二十|五|三)大?',request.request)
    expected=int(top[1]) if top and top[1].isdigit() else {'十':10,'二十':20,'五':5,'三':3}.get(top[1]) if top else None
    dates={r['date'] for r in unique}
    if len(dates)>1:reasons.append('返回包含多个报告期或日期，保留记录但不宣称单期完整名单')
    if expected and (len(unique)!=expected or not ranks or set(map(str,ranks))!={str(i) for i in range(1,expected+1)}):
        cov['status']='partial';cov['note']=f'请求前{expected}项，返回{len(unique)}条；名次或数量未证实完整，成功取法仍可保存。'
    elif expected and len(unique)==len(accepted) and len(dates)==1 and cov['status']!='partial':
        cov['status']='requested_count_verified';cov['note']=f'当前表有{expected}条不重复记录，排名1至{expected}；不代表其他日期也完整。'
    if len(unique)!=len(accepted) or missing_extras or any('主体名称' in n for n in reasons):
        cov['status']='partial';cov['note']='返回缺少有效主体或含重复主体/记录，不能确认完整名单。'
    return {'data':unique,'columns':columns,'coverage':cov,'notes':reasons,'save_allowed':not any('不自动保存' in r for r in reasons)},[]


def market_request_topics(request):
    topics=[]
    for part in request_topics(request):
        value=re.sub(r'^(?:全部|所有|全市场|a股|股票|可转债|转债|证券|的)+','',part)
        value=re.sub(r'^(?:日线|行情|数据|名单|目录|列表|的)+','',value)
        if value and value not in {'日线','行情','数据','名单','目录','列表'}:topics.append(value)
    return topics


MARKET_TOPIC_ALIASES={'开盘价':['open'],'最高价':['high'],'最低价':['low'],'收盘价':['close'],'前收盘价':['preclose'],
                      '成交量':['volume','vol'],'成交额':['amount'],'换手率':['turnoverrate'],'涨跌幅':['pctchange','pctchg'],
                      '上市日期':['listdate'],'退市日期':['delistdate'],'摘牌日期':['delistdate'],'证券简称':['name']}


def market_topic_keys(topic,columns):
    keys=[]
    for key,meta in columns.items():
        corpus=compact(key)+compact(meta['label'])
        if topic=='收盘价' and '前收盘价' in corpus:continue
        if topic in corpus or any(alias in corpus for alias in MARKET_TOPIC_ALIASES.get(topic,[])):keys.append(key)
    return keys


def present(value):return value is not None and (not isinstance(value,str) or value.strip() not in {'','--','—','暂无','无数据'})


def market_topic_present(topic,columns,rows):
    keys=market_topic_keys(topic,columns)
    return any(present(row.get(key)) for row in rows for key in keys)


def market_topic_partial(topic,columns,rows):
    keys=market_topic_keys(topic,columns)
    return any(not any(present(row.get(key)) for key in keys) for row in rows)


def market_column_matches(column,topics):
    corpus=compact(normalized_column(column))
    return any(topic in corpus or any(alias in corpus for alias in MARKET_TOPIC_ALIASES.get(topic,[])) for topic in topics)


def market_tables(raw,topics,market_kind):
    """Expand Choice cross-sections (security rows × date columns) for market queries.

    The legacy single-security parser intentionally treats these sheets as indicator
    rows. Market validation needs the orthogonal shape without changing old recipes.
    """
    def transform(value):
        if isinstance(value,list):return [transform(item) for item in value]
        if isinstance(value,str):
            objects=list(json_objects(value))
            return transform(objects[0] if len(objects)==1 else objects) if objects else value
        if not isinstance(value,dict):return value
        columns=value.get('columns');items=value.get('items')
        if (market_kind=='daily' and isinstance(columns,list) and len(columns)>1
                and all(isinstance(column,str) for column in columns)
                and isinstance(items,list) and market_column_matches(columns[0],topics)):
            days=[strict_date(column) for column in columns[1:]]
            if any(days):
                rows=[]
                for item in items:
                    if not isinstance(item,list) or not item:continue
                    symbol=normalize_symbol(item[0])
                    for index,day in enumerate(days,1):
                        if not day or index>=len(item):continue
                        rows.append({'证券代码':symbol or str(item[0]),'date':day,columns[0]:item[index]})
                if rows:return {'rows':rows}
        return {key:transform(item) for key,item in value.items()}
    return tables(transform(copy.deepcopy(raw)))


def validate_market_records(raw,request):
    accepted=[];columns={};reasons=[];conflicts=[]
    requested_topics=market_request_topics(request.request)
    if request.market_kind=='universe':
        columns={'name':{'label':'证券简称','unit':'未提供','data_type':'string'},
                 'list_date':{'label':'上市日期','unit':'未提供','data_type':'date'},
                 'delist_date':{'label':'退市/摘牌日期','unit':'未提供','data_type':'date'}}
    for row,units in market_tables(raw,requested_topics,request.market_kind):
        symbol=symbol_for_row(row)
        if not symbol:
            reasons.append('市场记录缺少可规范化的真实证券代码');continue
        is_cb=bool(re.fullmatch(r'(?:11\d{4}\.SH|12\d{4}\.SZ)',symbol))
        if (request.market_hint=='convertible_bond')!=is_cb:
            reasons.append('市场记录证券代码与market_hint不一致');continue
        if request.market_kind=='universe':
            dates={d for key in LIST_DATE_COLUMNS if (d:=strict_date(row.get(key)))}
            if len(dates)>1:
                reasons.append('同一证券存在多个上市日期');continue
            actual=next(iter(dates),None)
            if not actual:
                reasons.append('证券名单缺少完整上市日期，不能用于历史时点');continue
            delist=next((d for key in DELIST_DATE_COLUMNS if (d:=strict_date(row.get(key)))),None)
            if request.end_date and (actual>request.end_date or (delist and delist<request.end_date)):continue
        else:
            dates={d for key in DATE_COLUMNS if (d:=strict_date(row.get(key)))}
            dates.update(d for key in row if market_column_matches(key,requested_topics) and (d:=date_in_header(key)))
            if len(dates)>1:
                reasons.append('同一市场记录存在多个交易日，无法确定实际日期');continue
            actual=next(iter(dates),None)
            if not actual:
                reasons.append('市场日线缺少完整实际交易日');continue
            if request.start_date and not request.start_date<=actual<=request.end_date:
                reasons.append('返回交易日不在请求范围内');continue
            delist=None
        item={'symbol':symbol,'date':actual}
        if request.market_kind=='universe':
            item.update(list_date=actual,delist_date=delist)
            name=next((str(row[key]).strip() for key in ('name','证券简称','股票简称','债券简称','简称') if row.get(key)),None)
            item['name']=name
        for key,value in row.items():
            if key in IDENTITY_COLUMNS or key in DATE_COLUMNS or key in LIST_DATE_COLUMNS or key in DELIST_DATE_COLUMNS:continue
            name=normalized_column(key)
            if request.market_kind=='universe' and name in {'name','证券简称','股票简称','债券简称','简称'}:continue
            if name in item and item[name]!=value:raise ValueError('去除日期后出现冲突列名')
            item[name]=value
            unit=units.get(key,'') or '未提供'
            if name in columns and columns[name]['unit']!=unit:raise ValueError('同名列在返回中单位冲突')
            columns[name]={'label':name,'unit':unit,'data_type':'number' if isinstance(value,(int,float)) and not isinstance(value,bool) else 'string'}
        accepted.append(item)
    if not accepted:return None,list(dict.fromkeys(reasons+['未得到带真实证券代码和实际日期的全市场记录表']))
    required_topics=list(dict.fromkeys(requested_topics+(['证券简称'] if request.market_kind=='universe' else [])))
    missing_extras=[part for part in required_topics if not market_topic_present(part,columns,accepted)]
    partial_extras=[part for part in required_topics if part not in missing_extras and market_topic_partial(part,columns,accepted)]
    if (requested_topics and all(part in missing_extras for part in requested_topics)) or (request.market_kind=='universe' and '证券简称' in missing_extras):
        return None,list(dict.fromkeys(reasons+['返回表的所请求字段全部为空，不能用代码和日期列替代原需求']))
    unique=[];seen={};duplicate_count=0
    for row in accepted:
        key=(row['symbol'],row['date'])
        fingerprint=json.dumps(row,sort_keys=True,ensure_ascii=False)
        if key in seen:
            duplicate_count+=1
            if seen[key]!=fingerprint:conflicts.append({'symbol':key[0],'date':key[1]})
            continue
        seen[key]=fingerprint;unique.append(row)
    from .coverage import coverage
    cov=coverage(raw,unique)
    cov.update(returned_rows=len(unique),distinct_symbols=len({row['symbol'] for row in unique}),
               universe_count=cov['source_reported_rows'] if request.market_kind=='universe' else None)
    if missing_extras:reasons.append('仍缺请求列：'+'、'.join(missing_extras));cov['status']='partial'
    if partial_extras:reasons.append('部分记录的请求字段为空：'+'、'.join(partial_extras));cov['status']='partial'
    if duplicate_count:
        reasons.append('上游含重复证券日期记录，已去重；不宣称完整');cov['status']='partial'
    if conflicts:
        reasons.append('同一证券和日期存在冲突记录；不自动保存');cov['status']='partial'
    reported=cov['source_reported_rows']
    single_day_evidence=request.market_kind=='universe' or ((not request.start_date or request.start_date==request.end_date) and len({row['date'] for row in unique})==1)
    if not conflicts and not missing_extras and not partial_extras and not duplicate_count and cov['status']!='partial' and reported is not None and single_day_evidence:
        if reported==len(unique):
            cov['status']='complete';cov['note']='上游声明总数与本次不重复记录数一致；仅代表本次请求范围。'
        else:
            cov['status']='partial';cov['note']=f'上游声明{reported}条，实际解析{len(unique)}条。'
    elif not single_day_evidence:
        cov['status']='not_verified' if cov['status']!='partial' else cov['status']
        cov['note']='多日请求没有逐交易日覆盖证据，不能仅凭总行数宣称完整。'
    else:
        cov['note']='取法与全市场完整性分开；没有一致的上游总数及无节选证据时不宣称完整。'
    return {'data':unique,'columns':columns,'coverage':cov,'notes':list(dict.fromkeys(reasons)),
            'save_allowed':not conflicts},[]


def saved_queries():return dictionary._read_user().get('data_queries',{})


def save_query(request,capability,result,exploration_id):
    target=topic(request.request)
    identity=capability['provider']+'|'+capability['tool']+'|'+request.market_hint+'|'+target
    if request.query_scope=='market':identity+='|market|'+request.market_kind
    query_id='data_'+hashlib.sha256(identity.encode()).hexdigest()[:16]
    # Strip the sample entity from the reusable question. Explicit identity and
    # dates are supplied afresh by query_data, never copied from this evidence.
    reusable=request.request.replace(request.symbol,'') if request.symbol else request.request
    reusable=re.sub(DATE_PATTERN,'',reusable)
    for row in result['data']:
        for key in ['证券简称','股票简称','公司名称','公司中文名称']:
            value=row.get(key)
            if isinstance(value,str) and value:reusable=reusable.replace(value,'')
    definition={'id':query_id,'label':topic(reusable),'request':topic(reusable),'provider':capability['provider'],'tool':capability['tool'],'kind':'mcp_records','columns':result['columns'],'created_at':core.now(),'evidence_id':exploration_id,'schema_version':2 if request.query_scope=='market' else 1,'market_hint':request.market_hint,'query_scope':request.query_scope,'market_kind':request.market_kind}
    with config.LOCK:
        user=dictionary._read_user();existing=user.setdefault('data_queries',{}).get(query_id)
        if existing and any(existing[k]!=definition[k] for k in ['request','provider','tool','kind']):raise ValueError('已存在不同取法，需确认后保存')
        user['data_queries'][query_id]=definition
        dictionary._merge(dictionary.validate(json.loads(dictionary.PATH.read_text())),user)
        config.write_json(dictionary.user_dictionary_path(),user)
        dictionary.reload_dictionary()
    return query_id


def store_result(answer,raws):
    folder=config.ROOT/'local/results';folder.mkdir(parents=True,exist_ok=True)
    rid=answer['id'];answer.update(raw_path=str(folder/f'{rid}.raw.json'),data_path=str(folder/f'{rid}.json'),download_url=f'/api/results/{rid}/csv',raw_url=f'/api/results/{rid}/raw')
    answer['fields']=answer.get('columns',{})
    answer['row_count']=len(answer.get('data',[]))
    config.write_json(folder/f'{rid}.raw.json',config.redact(raws))
    config.write_json(folder/f'{rid}.json',config.redact(answer))
    if answer.get('request',{}).get('query_scope')=='market':return answer
    return {**answer,'data':answer.get('data',[])[:100], 'candidates':[{**c,'data':c['data'][:100],'units_by_row':c['units_by_row'][:100]} for c in answer.get('candidates',[])]}

async def run_capabilities(request,capabilities,query_id=None):
    start=time.monotonic();rid=uuid.uuid4().hex;attempts=[];raws=[];used=set()
    candidates=[]
    answer={'id':rid,'status':'failed','request':request.model_dump(),'data':[],'columns':{},'saved':False,'saved_query_id':query_id,'notes':[],'attempts':attempts}
    for cap in capabilities:
        provider=cap['provider']
        attempt_identity=(provider,cap['tool']) if request.query_scope=='market' else provider
        if attempt_identity in used or len(used)>=request.max_attempts:continue
        p=config.load()['providers'][provider]
        if not core.available(provider,p):attempts.append({'provider':provider,'tool':cap['tool'],'status':'unavailable','error':'来源未配置或未启用'});continue
        tool=next((t for t in cached_tools(provider) if t['name']==cap['tool']),None)
        if not tool or not runnable(tool):attempts.append({'provider':provider,'tool':cap['tool'],'status':'not_adapted','error':'当前工具不是已发现的只读自然语言查询'});continue
        try:args=build_arguments(tool,request)
        except ValueError as exc:attempts.append({'provider':provider,'tool':cap['tool'],'status':'needs_confirmation','error':str(exc)});continue
        used.add(attempt_identity);tick=time.monotonic()
        try:
            raw=await core.invoke(provider,{'generic_call':{'tool':tool['name'],'arguments':args}},min(60,p.get('timeout_seconds',60)))
            raws.append({'provider':provider,'tool':tool['name'],'arguments':args,'response':raw})
            try:
                result,reasons=validate_market_records(raw,request) if request.query_scope=='market' else validate_records(raw,request)
            except ValueError as exc:
                result,reasons=None,[str(exc)]
            if not result:
                extracted=tables(raw)
                if extracted:
                    candidates.append({'provider':provider,'tool':tool['name'],'data':[row for row,units in extracted], 'units_by_row':[units for row,units in extracted], 'warnings':reasons,'validation_status':'unverified','row_count':len(extracted)})
                attempts.append({'provider':provider,'tool':tool['name'],'status':'needs_confirmation','error':'；'.join(reasons),'seconds':round(time.monotonic()-tick,2)})
                core.record(provider,False,'；'.join(reasons));continue
            core.record(provider,True)
            attempts.append({'provider':provider,'tool':tool['name'],'status':'success','seconds':round(time.monotonic()-tick,2)})
            answer.update(status='success',provider=provider,tool=tool['name'],data=result['data'],columns=result['columns'],coverage=result['coverage'],notes=result['notes'],fetched_at=core.now(),query_tool='query_data',complete=result['coverage']['status']==('complete' if request.query_scope=='market' else 'requested_count_verified'))
            if query_id:answer.update(saved=True,saved_query_id=query_id)
            elif request.auto_save and result['save_allowed']:
                try:
                    saved=save_query(request,cap,result,rid);answer.update(saved=True,saved_query_id=saved)
                    answer['notes'].append('已保存到个人字典，可用 query_data 复用；保存的是取法而非本次名单。')
                except Exception as exc:answer['save_error']=config.redact(str(exc))
            if not result['save_allowed']:answer.update(status='needs_confirmation')
            break
        except Exception as exc:
            error=config.redact(str(exc) or '请求超时');core.record(provider,False,error)
            attempts.append({'provider':provider,'tool':tool['name'],'status':'error','error':error,'seconds':round(time.monotonic()-tick,2)})
    if not answer['data'] and candidates:
        candidate=candidates[0]
        answer.update(data=candidate['data'],provider=candidate['provider'],tool=candidate['tool'],status='needs_confirmation',validation_status='unverified',complete=False,coverage={'status':'unverified','note':'原始候选表，未通过自动校验；保留原列名、证券和日期，不作标准化推断。'})
        answer['notes'].extend(candidate['warnings'])
        answer['notes'].append('数据已返回，可结合疑点核对或接受；本次不自动保存取法。')
    answer['candidates']=candidates
    if not answer['data']:
        answer['status']='needs_confirmation' if any(a['status']=='needs_confirmation' for a in attempts) else 'failed'
        answer['notes'].append('网关已搜索上游能力并尝试可用取法；本次未取得可核实结果，不代表供应商没有数据。')
    answer.update(elapsed_seconds=round(time.monotonic()-start,2),network_calls=len(used))
    return store_result(answer,raws)

async def explore_data(request):
    # Only exact reusable recipe matches skip discovery. A 控股股东 field is not
    # considered a match for a 前十大股东 request.
    for key,definition in saved_queries().items():
        if request.query_scope==definition.get('query_scope','security') and request.market_kind==definition.get('market_kind') and request.market_hint==definition.get('market_hint','equity') and topic(request.request)==topic(definition['request']) and request.provider in ['auto',definition['provider']]:
            return await query_data(DataQueryRequest(query_id=key,symbol=request.symbol,query_scope=request.query_scope,market_kind=request.market_kind,period=request.period,start_date=request.start_date,end_date=request.end_date))
    found=await search_capabilities(request.request,request.provider,limit=20,market_hint=request.market_hint,query_scope=request.query_scope,market_kind=request.market_kind)
    caps=[r for r in found['results'] if r.get('kind')=='upstream_tool' and r['status']=='explorable']
    answer=await run_capabilities(request,caps)
    answer['capability_search']={'total':found['total'],'errors':found['errors'],'tools':[{'provider':c['provider'],'tool':c['tool']} for c in caps]}
    path=config.ROOT/'local/results'/f"{answer['id']}.json"
    stored=json.loads(path.read_text());stored['capability_search']=answer['capability_search'];config.write_json(path,stored)
    return answer

async def query_data(request):
    definition=saved_queries().get(request.query_id)
    if not definition:raise ValueError('个人字典没有这个取法，请先 explore_data')
    scope=definition.get('query_scope','security');kind=definition.get('market_kind')
    if request.query_scope and request.query_scope!=scope:raise ValueError('query_scope与个人取法不匹配')
    if request.market_kind and request.market_kind!=kind:raise ValueError('market_kind与个人取法不匹配')
    if scope=='market':
        if request.symbol is not None:raise ValueError('市场取法不接受symbol')
        market_hint=definition['market_hint']
        if request.market_hint and request.market_hint!=market_hint:raise ValueError('个人取法的证券类型不匹配')
    else:
        if not request.symbol:raise ValueError('单证券取法必须提供symbol')
        market_hint=infer_market(request.symbol,request.market_hint)
    if market_hint!=definition.get('market_hint','equity'):raise ValueError('个人取法的证券类型不匹配，请按该市场重新 explore_data')
    await ensure_tools(definition['provider'])
    r=ExploreDataRequest(request=definition['request'],query_scope=scope,market_kind=kind,market_hint=market_hint,symbol=request.symbol,period=request.period,start_date=request.start_date,end_date=request.end_date,provider=definition['provider'],auto_save=False,max_attempts=1)
    answer=await run_capabilities(r,[{'provider':definition['provider'],'tool':definition['tool']}],request.query_id)
    answer['reused']=True
    return answer


def fetch_remote(name,p,key,call):
    from .providers import Remote
    tool=next((t for t in cached_tools(name) if t['name']==call.get('tool')),None)
    if not tool or not runnable(tool):raise ValueError('只能执行当前能力目录中已确认只读的自然语言工具')
    args=call.get('arguments',{})
    if set(args)-set(tool['inputSchema'].get('properties',{})):raise ValueError('出现未声明参数')
    return Remote(name,p,key).call(tool['name'],args)
