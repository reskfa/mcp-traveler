import csv
import io
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from . import config,core
from .catalog import catalog,dictionary_view

mcp=FastMCP('A股数据网关',stateless_http=True,json_response=True)
@mcp.tool()
def data_catalog(dataset:str|None=None,field:str|None=None)->dict:
    """查看数据目录。传 dataset 和可选 field 可查询字段取法、来源映射、单位转换与接口参数。"""
    return dictionary_view(dataset,field) if dataset else catalog()
@mcp.tool()
async def query_daily_prices(symbol:str,start_date:str,end_date:str,fields:list[str]|None=None,provider:str='auto',adjust:str='none')->dict:
    """查询 A 股历史日线。日期 YYYY-MM-DD，代码如 600519.SH。大结果返回预览和本地路径。"""
    return await core.query(core.Query(dataset='daily_prices',symbol=symbol,start_date=start_date,end_date=end_date,fields=fields,provider=provider,adjust=adjust))
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

mcp_app=mcp.streamable_http_app()
@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():yield
app=FastAPI(title='MCP Traveler',lifespan=lifespan)

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
@app.get('/api/results/{rid}/{kind}')
def result(rid:str,kind:str):
    if not re.fullmatch(r'[0-9a-f]{32}',rid):raise HTTPException(404)
    path=config.ROOT/'local/results'/f'{rid}.json'
    if not path.exists():raise HTTPException(404)
    if kind=='raw':return FileResponse(path.with_name(f'{rid}.raw.json'),media_type='application/json')
    if kind=='json':return FileResponse(path,media_type='application/json',filename=f'{rid}.json')
    if kind!='csv':raise HTTPException(404)
    data=json.loads(path.read_text());s=io.StringIO();cols=['symbol','date']+list(data['fields'])
    w=csv.DictWriter(s,fieldnames=cols,extrasaction='ignore');w.writeheader()
    for row in data['data']:w.writerow(row)
    return Response('\ufeff'+s.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{rid}.csv"'})
app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')
app.mount('/',mcp_app)
