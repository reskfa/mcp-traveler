import csv
import io
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI,Request,HTTPException,Query as QueryParam
from pydantic import BaseModel
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from . import config,core
from .catalog import catalog,dictionary_view
from .dictionary import search_metrics as find_metrics,delete_user_metric
from .exploration import ExplorationRequest,explore,save_exploration
from .generic_data import ExploreDataRequest,DataQueryRequest,search_capabilities as find_capabilities,explore_data as explore_generic,query_data as query_generic
from .helpers import TradeCalendarRequest,MarketSymbolsRequest,SearchSecuritiesRequest,query_trade_calendar as run_trade_calendar,query_market_symbols as run_market_symbols,search_securities as run_search_securities,get_helper_capabilities
from .market import MarketUniverseRequest,MarketDailyRequest,MarketReturnsRequest,query_market_universe as market_universe,query_market_daily as market_daily,query_market_returns as market_returns
from .panel import CrossSectionRequest,TimeSeriesRequest,query_market_cross_section as market_cross_section,query_market_time_series as market_time_series,get_panel_capabilities

mcp=FastMCP('羊毛伞',stateless_http=True,json_response=True)
@mcp.tool()
async def query_basic_info(symbol:str,fields:list[str]|None=None,provider:str='auto')->dict:
    """查询股票基本信息：公司档案、证券名称历史、行业归属、板块标签。文本/日期/记录列表按字典返回；不同分类体系分开。无历史时点参数，缺失字段为null，每个字段携带来源。fields=['*']查询全部。"""
    return await core.query(core.Query(dataset='basic_info',symbol=symbol,fields=fields,provider=provider))

@mcp.tool()
async def query_convertible_bond_basic(symbol:str,fields:list[str]|None=None,provider:str='auto')->dict:
    """查询可转债当前档案及合同原文：赎回call_clause、回售put_clause、下修reset_clause。不接受日期，原文不等于公告事件。先 search_metrics(dataset='cb_basic')。"""
    return await core.query(core.Query(dataset='cb_basic',symbol=symbol,fields=fields,provider=provider))

@mcp.tool()
async def query_convertible_bond_daily(symbol:str,start_date:str,end_date:str,fields:list[str]|None=None,provider:str='auto')->dict:
    """查询可转债历史日线与估值。先 search_metrics(dataset='cb_daily')；证券如113050.SH，不走股票接口。"""
    return await core.query(core.Query(dataset='cb_daily',symbol=symbol,start_date=start_date,end_date=end_date,fields=fields,provider=provider))

@mcp.tool()
async def query_convertible_bond_clauses(symbol:str,start_date:str|None=None,end_date:str|None=None,fields:list[str]|None=None,provider:str='auto')->dict:
    """查询可转债历史公告事件（赎回等）。日期指公告日期，不重建历史时点状态；缺失不推定为未触发。合同条款原文请用 query_convertible_bond_basic 的 call_clause/put_clause/reset_clause。未接入来源返回 status=unsupported；真实上游错误仍报告失败。"""
    return await core.query(core.Query(dataset='cb_clauses',symbol=symbol,start_date=start_date,end_date=end_date,fields=fields,provider=provider))

@mcp.tool()
def data_catalog(dataset:str|None=None,field:str|None=None)->dict:
    """查看数据目录。传 dataset 和可选 field 可查询字段取法、来源映射、单位转换与接口参数。"""
    return dictionary_view(dataset,field) if dataset else catalog()
@mcp.tool()
async def query_daily_prices(symbol:str,start_date:str,end_date:str,fields:list[str]|None=None,provider:str='auto',adjust:str='none')->dict:
    """查询 A 股历史日线。日期 YYYY-MM-DD，代码如 600519.SH。大结果返回预览和本地路径。"""
    return await core.query(core.Query(dataset='daily_prices',symbol=symbol,start_date=start_date,end_date=end_date,fields=fields,provider=provider,adjust=adjust))

@mcp.tool()
async def helper_trade_calendar(start_date:str,end_date:str,exchange:str='SSE',provider:str='auto')->dict:
    """辅助函数：拉取 A 股交易日历（返回有序交易日列表 dates 及明细）。支持多源自动路由（优先 Tushare/AKShare 原生交易日历，缺失时自动回退 Wind/通达信/东方财富妙想/iFinD 上证指数历史交易日），不锁定 Tushare。日期格式 YYYY-MM-DD。"""
    return await run_trade_calendar(TradeCalendarRequest(start_date=start_date,end_date=end_date,exchange=exchange,provider=provider))

@mcp.tool()
async def helper_market_symbols(scope:str,as_of_date:str|None=None,include_delisted:bool=False,provider:str='auto')->dict:
    """辅助函数：拉取全市场 A 股(stock)或可转债(convertible_bond)代码清单（返回扁平 symbols 数组及含简称、交易所、上市/退市日期的 data 表）。支持 Tushare、AKShare、通达信、东方财富妙想、iFinD、Wind 多源探测与路由，不锁定 Tushare；截断或仅当前名单会在 coverage 中明确标注。"""
    return await run_market_symbols(MarketSymbolsRequest(scope=scope,as_of_date=as_of_date,include_delisted=include_delisted,provider=provider))

@mcp.tool()
async def helper_search_securities(query:str,scope:str='all',limit:int=20,include_delisted:bool=False,provider:str='auto')->dict:
    """辅助函数：双向搜索 A 股与可转债（名称/简称搜代码，或代码搜名称）。scope 可选 all（默认，同时搜股票和转债）、stock、convertible_bond；支持 Tushare、AKShare、Wind、通达信（A股）、iFinD、东方财富妙想多源自动路由，不锁定 Tushare。"""
    return await run_search_securities(SearchSecuritiesRequest(query=query,scope=scope,limit=limit,include_delisted=include_delisted,provider=provider))

@mcp.tool()
def helper_capabilities()->dict:
    """辅助函数：查看内置辅助函数（交易日历、全市场代码）在六家数据源上的实测能力矩阵、取数方式与限制。"""
    return get_helper_capabilities()

@mcp.tool()
async def query_market_cross_section(scope:str,date:str,symbols:list[str]|None=None,fields:list[str]|None=None,adjust:str='none',provider:str='auto',sort_by:str|None=None,order:str='asc',top_n:int|None=None)->dict:
    """多券/全市场截面数据：取指定日期多个券（省略 symbols 为全市场）的一个或多个指标。返回二维表结构（index=codes, columns=fields，含 matrix 与 data），并附六家数据源能力矩阵 capabilities。非交易日自动对齐至前一交易日。"""
    return await market_cross_section(CrossSectionRequest(scope=scope,date=date,symbols=symbols,fields=fields,adjust=adjust,provider=provider,sort_by=sort_by,order=order,top_n=top_n))

@mcp.tool()
async def query_market_time_series(scope:str,start_date:str,end_date:str,field:str='close',frequency:str='daily',symbols:list[str]|None=None,adjust:str='none',provider:str='auto')->dict:
    """多券/全市场时序数据：取日期区间（支持 daily/weekly/monthly 频率）多个券（省略 symbols 为全市场）的单个指标。返回二维表结构（index=dates, columns=codes，含 matrix 与 data），并附六家数据源能力矩阵 capabilities。"""
    return await market_time_series(TimeSeriesRequest(scope=scope,start_date=start_date,end_date=end_date,field=field,frequency=frequency,symbols=symbols,adjust=adjust,provider=provider))

@mcp.tool()
def market_panel_capabilities()->dict:
    """查看多券/全市场截面与时序取数在六家数据源上的能力矩阵（区分一次性全市场、多券批量轮询、单券轮询、复权与周期支持）。"""
    return get_panel_capabilities()

@mcp.tool()
async def query_market_universe(scope:str,as_of_date:str|None=None,provider:str='auto')->dict:
    """查询A股(stock)或可转债(convertible_bond)证券目录。as_of_date用于历史时点名单，含当时仍上市但后来退市/摘牌的证券；返回完整JSON及universe完整性、冲突和未知日期。"""
    return await market_universe(MarketUniverseRequest(scope=scope,as_of_date=as_of_date,provider=provider))

@mcp.tool()
async def query_market_daily(scope:str,start_date:str,end_date:str,symbols:list[str]|None=None,fields:list[str]|None=None,provider:str='auto',adjust:str='none',sort_by:str|None=None,order:str='asc',top_n:int|None=None)->dict:
    """跨券查询A股或转债日线。省略symbols查询历史时点全市场，显式symbols一次查询多只证券；fields支持OHLC、量额、pre_close、pct_change等。非交易日开始日顺延、结束日提前，单日优先取前一交易日，实际日期见window。默认完整JSON，不用旧单券工具循环。top_n只在完整取数排序后裁剪响应；逐券缺失、失败和覆盖率见coverage。"""
    return await market_daily(MarketDailyRequest(scope=scope,start_date=start_date,end_date=end_date,symbols=symbols,fields=fields,provider=provider,adjust=adjust,sort_by=sort_by,order=order,top_n=top_n))

@mcp.tool()
async def query_market_returns(scope:str,end_date:str,start_date:str|None=None,trading_days:int|None=None,symbols:list[str]|None=None,provider:str='auto',order:str='asc',top_n:int|None=None)->dict:
    """查询全市场或代码列表的区间价格收益并排名。start_date/end_date与trading_days=N/end_date二选一；非交易日开始日顺延、结束日提前。只取基准日和结束日两次收盘价，不取窗口中间行情。近5日跌幅最大用trading_days=5、order='asc'、top_n=10。股票同源前复权、转债同源不复权；缺端点价格不当作0，ranking_complete说明是否完整全市场排名。默认完整JSON。"""
    return await market_returns(MarketReturnsRequest(scope=scope,end_date=end_date,start_date=start_date,trading_days=trading_days,symbols=symbols,provider=provider,order=order,top_n=top_n))

@mcp.tool()
async def query_technical_indicators(symbol:str,start_date:str|None=None,end_date:str|None=None,fields:list[str]|None=None,provider:str='auto',parameters:dict[str,int]|None=None,frequency:str='daily',adjust:str='native')->dict:
    """按字典查询A股技术指标。先search_metrics找字段；data_catalog查看参数、单位和复权口径。历史查询需日期范围；Wind历史用wind_history_*，MA/RSI周期可调；Wind *_native最新快照可省略日期，不能代替历史。来源可调与固定参数由字典明确，返回来源与实际日期，不在本地计算。"""
    return await core.query(core.Query(dataset='technical_indicators',symbol=symbol,start_date=start_date,end_date=end_date,fields=fields,provider=provider,parameters=parameters,frequency=frequency,adjust=adjust))
@mcp.tool()
async def query_valuations(symbol:str,date:str|None=None,fields:list[str]|None=None,provider:str='auto')->dict:
    """查询每日估值、股本和交易状态。fields 省略取常用项，["*"] 取全部；date 为空取最新。"""
    return await core.query(core.Query(dataset='valuations',symbol=symbol,date=date,fields=fields,provider=provider))
@mcp.tool()
async def query_financials(symbol:str,period:str,fields:list[str]|None=None,provider:str='auto')->dict:
    """查询报告期财务数据（含累计、单季度和比率，见字段定义）。fields 省略取常用项，["*"] 取全部。period 如 2024-12-31。"""
    return await core.query(core.Query(dataset='financials',symbol=symbol,period=period,fields=fields,provider=provider))
@mcp.tool()
def provider_status()->dict:
    """查看连接状态、手动账户记录、本地调用统计；不会自动读取官方余额。"""
    return config.redact(config.status())

@mcp.tool()
def search_metrics(query:str='',dataset:str|None=None,limit:int=10)->dict:
    """搜索A股与可转债字段及已保存通用取法的中文名称、字段代码、别名和来源列；优先使用此工具，不必读取完整目录。返回个人及内置字典的紧凑结果。未精确命中时继续search_capabilities或explore_data，不能据此判断上游没有数据。"""
    return find_metrics(query,dataset,limit)

@mcp.tool()
async def explore_metric(name:str,dataset:str,symbol:str,auto_save:bool=True,period:str|None=None,date:str|None=None,start_date:str|None=None,end_date:str|None=None,unit:str|None=None,aliases:list[str]|None=None,provider:str='auto',max_attempts:int=2,parameters:dict[str,int]|None=None,frequency:str='daily',adjust:str|None=None)->dict:
    """探索尚未入字典的A股数值指标。先查已有取法，再按路由尝试可能支持的MCP；每来源最多一次。名称应是一个明确指标，提供日期/报告期。成功默认自动保存到个人字典并返回saved/saved_field，告知用户即可。needs_confirmation不等于无数据：可解析的上游结果会保留在data和candidates，并标注validation_status=unverified及差异；先展示和比较，不因校验疑点弃用。未确认候选不自动保存，仅临时查询传auto_save=false。coverage独立说明数据完整性，不能把成功保存称作取全。"""
    return await explore(ExplorationRequest(name=name,auto_save=auto_save,dataset=dataset,symbol=symbol,period=period,date=date,start_date=start_date,end_date=end_date,unit=unit,aliases=aliases or [],provider=provider,max_attempts=max_attempts,parameters=parameters,frequency=frequency,adjust=adjust))

@mcp.tool()
async def save_metric(exploration_id:str,field:str|None=None)->dict:
    """将成功探索的取法保存到本机个人字典，之后搜索及常规查询立即可用。field可选，填写稳定英文代码；不保存无法核实的候选，不覆盖内置字典。"""
    return await save_exploration(exploration_id,field)

@mcp.tool()
async def search_capabilities(query:str,provider:str='auto',refresh:bool=False,limit:int=8,market_hint:str|None=None,query_scope:str='security',market_kind:str|None=None)->dict:
    """搜索字典与供应商能力。query_scope=market时不填代码，须同时给market_hint及market_kind=universe|daily。字典未精确命中（或只有相近字段）时继续此入口，不直接绕开网关。返回已接入、可探索或未适配状态，未适配不等于来源不支持。"""
    return await find_capabilities(query,provider,refresh,limit,market_hint=market_hint,query_scope=query_scope,market_kind=market_kind)

@mcp.tool()
async def explore_data(request:str,symbol:str|None=None,period:str|None=None,start_date:str|None=None,end_date:str|None=None,provider:str='auto',max_attempts:int=2,auto_save:bool=True,market_hint:str|None=None,query_scope:str='security',market_kind:str|None=None)->dict:
    """探索常用字典之外的信息。默认query_scope=security并传单券symbol；query_scope=market时省略symbol，传market_hint=equity|convertible_bond及market_kind=universe|daily，市场名单以end_date为历史时点。先发现上游能力再执行只读查询。成功默认保存取法，复用query_data；市场取法不保存代码清单，响应不裁100行，保存成功不代表全市场完整。"""
    return await explore_generic(ExploreDataRequest(request=request,symbol=symbol,market_hint=market_hint,query_scope=query_scope,market_kind=market_kind,period=period,start_date=start_date,end_date=end_date,provider=provider,max_attempts=max_attempts,auto_save=auto_save))

@mcp.tool()
async def query_data(query_id:str,symbol:str|None=None,period:str|None=None,start_date:str|None=None,end_date:str|None=None,market_hint:str|None=None,query_scope:str|None=None,market_kind:str|None=None)->dict:
    """复用个人字典中探索成功的记录取法。旧单券取法仍需symbol；市场取法省略symbol，可换日期，query_scope和market_kind可省略并自动从保存定义恢复。不重复探索，仍校验本次实际返回；市场响应保持完整JSON。"""
    return await query_generic(DataQueryRequest(query_id=query_id,symbol=symbol,market_hint=market_hint,query_scope=query_scope,market_kind=market_kind,period=period,start_date=start_date,end_date=end_date))

mcp_app=mcp.streamable_http_app()
@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():yield
app=FastAPI(title='羊毛伞 · MCP Traveler',lifespan=lifespan)
from .agent_access import router as agent_router
app.include_router(agent_router)

@app.middleware('http')
async def local_only(request:Request,call_next):
    host=urlsplit('http://'+request.headers.get('host','')).hostname
    if host not in ['127.0.0.1','localhost','::1','testserver']:
        return JSONResponse({'error':'仅允许本机访问'},status_code=403)
    if request.client and request.client.host not in ['127.0.0.1','::1','testclient']:
        return JSONResponse({'error':'仅允许本机连接'},status_code=403)
    origin=request.headers.get('origin')
    if origin:
        parsed=urlsplit(origin)
        if parsed.scheme not in ['http','https'] or parsed.netloc != request.headers.get('host'):
            return JSONResponse({'error':'不允许跨站请求'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='no-store'
    return response

@app.exception_handler(core.QueryFailure)
async def failed_query(request,exc):return JSONResponse({'error':str(exc),'attempts':exc.attempts},status_code=502)
@app.exception_handler(ValueError)
async def value_error(request,exc):return JSONResponse({'error':config.redact(str(exc))},status_code=400)

@app.get('/')
def home():return FileResponse(Path(__file__).parent/'static/index.html')
@app.get('/api/catalog')
def get_catalog():return catalog()
@app.get('/api/dictionary')
def get_dictionary(dataset:str|None=None,field:str|None=None):return dictionary_view(dataset,field)
class SaveMetricRequest(BaseModel):
    field:str|None=None

@app.get('/api/metrics/search')
def metric_search(q:str='',dataset:str|None=None,limit:int=QueryParam(10,ge=1,le=50)):
    return find_metrics(q,dataset,limit)

@app.post('/api/explore')
async def metric_explore(request:ExplorationRequest):
    return await explore(request)

@app.post('/api/explorations/{exploration_id}/save')
async def metric_save(exploration_id:str,request:SaveMetricRequest):
    return await save_exploration(exploration_id,request.field)

@app.delete('/api/metrics/{dataset}/{field}')
def metric_delete(dataset:str,field:str):
    return delete_user_metric(dataset,field)

@app.get('/api/capabilities')
async def capability_search(query:str,provider:str='auto',refresh:bool=False,limit:int=QueryParam(8,ge=1,le=20),market_hint:str|None=None,query_scope:str='security',market_kind:str|None=None):
    return await find_capabilities(query,provider,refresh,limit,market_hint=market_hint,query_scope=query_scope,market_kind=market_kind)

@app.post('/api/explore-data')
async def generic_explore(request:ExploreDataRequest):return await explore_generic(request)

@app.post('/api/data-query')
async def generic_query(request:DataQueryRequest):return await query_generic(request)

@app.get('/api/config')
def get_config():return config.public()
@app.get('/api/status')
def get_status():return config.redact(config.status())
@app.patch('/api/providers/{name}')
def save_provider(name:str,patch:dict):
    if name not in config.PROVIDERS:raise HTTPException(404)
    return config.save_provider(name,patch)
@app.post('/api/providers/{name}/test')
async def test_provider(name:str):
    if name not in config.PROVIDERS:raise HTTPException(404)
    return await core.probe(name)
@app.put('/api/routes')
def routes(routes:dict):return config.save_routes(routes)
@app.patch('/api/accounts/{name}')
def account(name:str,patch:dict):
    if name not in config.PROVIDERS:raise HTTPException(404)
    allowed=['remaining_credits','remaining_calls','permission_points','expires_at','rate_limit']
    values={k:patch[k] for k in allowed if k in patch}
    for k in ['remaining_credits','remaining_calls','permission_points']:
        if k in values and values[k] is not None:
            values[k]=float(values[k])
            if not __import__('math').isfinite(values[k]) or values[k]<0:raise ValueError('额度应为非负数字')
    values.update({'source':'manual','account_updated_at':core.now()})
    return config.update_status(name,values)
@app.post('/api/accounts/{name}/refresh')
def refresh(name:str):
    if name not in config.PROVIDERS:raise HTTPException(404)
    return {'status':'login_required','message':'官方余额接口尚未接入，请登录官网核实后手动记录。','url':config.OFFICIAL[name]}
@app.post('/api/query')
async def query(q:core.Query):return await core.query(q)
@app.get('/api/helpers/capabilities')
def helpers_capabilities_api():return get_helper_capabilities()
@app.post('/api/helpers/trade-calendar')
async def helper_trade_calendar_api(request:TradeCalendarRequest):return await run_trade_calendar(request)
@app.post('/api/helpers/market-symbols')
async def helper_market_symbols_api(request:MarketSymbolsRequest):return await run_market_symbols(request)
@app.post('/api/helpers/search-securities')
async def helper_search_securities_api(request:SearchSecuritiesRequest):return await run_search_securities(request)
@app.get('/api/market/capabilities')
def market_capabilities_api():return get_panel_capabilities()
@app.post('/api/market/cross-section')
async def market_cross_section_api(request:CrossSectionRequest):return await market_cross_section(request)
@app.post('/api/market/time-series')
async def market_time_series_api(request:TimeSeriesRequest):return await market_time_series(request)
@app.post('/api/market/universe')
async def market_universe_api(request:MarketUniverseRequest):return await market_universe(request)
@app.post('/api/market/daily')
async def market_daily_api(request:MarketDailyRequest):return await market_daily(request)
@app.post('/api/market/returns')
async def market_returns_api(request:MarketReturnsRequest):return await market_returns(request)
@app.get('/api/results/{rid}/{kind}')
def result(rid:str,kind:str):
    if not re.fullmatch(r'[0-9a-f]{32}',rid):raise HTTPException(404)
    path=config.ROOT/'local/results'/f'{rid}.json'
    if not path.exists():raise HTTPException(404)
    if kind=='raw':return FileResponse(path.with_name(f'{rid}.raw.json'),media_type='application/json')
    if kind=='json':return FileResponse(path,media_type='application/json',filename=f'{rid}.json')
    if kind!='csv':raise HTTPException(404)
    data=json.loads(path.read_text());s=io.StringIO()
    ds_name=str(data.get('dataset',''))
    if ds_name=='panel_cross_section':
        cols=['symbol']+list(data.get('columns',[]))
    elif ds_name=='panel_time_series':
        cols=['date']+list(data.get('columns',[]))
    elif ds_name.startswith('helper_'):
        cols=list(data.get('fields',{}).keys()) or (list(data['data'][0].keys()) if data.get('data') else [])
    else:
        cols=['symbol','date']+list(data['fields'])
    w=csv.DictWriter(s,fieldnames=cols,extrasaction='ignore');w.writeheader()
    for row in data['data']:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()})
    return Response('\ufeff'+s.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{rid}.csv"'})
app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')
app.mount('/',mcp_app)
