import json
import requests
from . import config

class Remote:
    def __init__(self,name,p,key):
        server=next(iter(p['mcp_servers'].values()))
        self.url=server['url'];self.timeout=p.get('timeout_seconds',60)
        if not self.url:raise ValueError('尚未配置 MCP 地址')
        self.s=requests.Session()
        self.s.headers.update({'Content-Type':'application/json','Accept':'application/json, text/event-stream',server['auth_header']:server['auth_prefix']+key})
        self.seq=0
        if name in config.HANDSHAKE_MCP:
            r=self.rpc('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'mcp-traveler','version':'0.1.0'}})
            self.s.headers['MCP-Protocol-Version']=r.get('protocolVersion','2024-11-05')
            self.rpc('notifications/initialized',{},notification=True)
    def rpc(self,method,params,notification=False):
        self.seq+=1
        body={'jsonrpc':'2.0','method':method,'params':params}
        if not notification:body['id']=self.seq
        r=self.s.post(self.url,json=body,timeout=self.timeout)
        if r.status_code>=400:raise RuntimeError(f'MCP HTTP {r.status_code}')
        if r.headers.get('Mcp-Session-Id'):self.s.headers['Mcp-Session-Id']=r.headers['Mcp-Session-Id']
        if notification:return {}
        raw=r.content.decode('utf-8');messages=[]
        if raw.lstrip().startswith('{'):messages=[json.loads(raw)]
        else:
            for event in raw.replace('\r\n','\n').split('\n\n'):
                payload='\n'.join(line[5:].lstrip() for line in event.split('\n') if line.startswith('data:'))
                if payload:
                    try:messages.append(json.loads(payload))
                    except ValueError:continue
        msg=next((m for m in messages if m.get('id')==self.seq),None)
        if msg is None:raise RuntimeError('MCP 未返回匹配的响应')
        if msg.get('error'):raise RuntimeError('MCP 请求错误：'+str(msg['error'])[:200])
        result=msg.get('result',{})
        if result.get('isError'):raise RuntimeError('上游工具执行失败：'+str(result.get('content',''))[:200])
        return result
    def call(self,tool,args):return self.rpc('tools/call',{'name':tool,'arguments':args})


def records(df):return json.loads(df.to_json(orient='records',date_format='iso',force_ascii=False))
def compact_date(s):return s.replace('-','') if s else None

def fetch(name,q,p,key):
    if q.get('market_call'):
        from .market_provider import fetch as market_fetch
        return market_fetch(name,p,key,q['market_call'])
    if q.get('generic_call'):
        from .generic_data import fetch_remote
        return fetch_remote(name,p,key,q['generic_call'])
    if q.get('probe'):
        if name in config.MCP_PROVIDERS:
            r=Remote(name,p,key).rpc('tools/list',{})
            return {'tools':[{'name':t['name'],'description':t.get('description',''),'inputSchema':t.get('inputSchema',{}),'annotations':t.get('annotations',{})} for t in r.get('tools',[])]}
        if name=='tushare':
            import tushare as ts
            df=ts.pro_api(key).trade_cal(exchange='SSE',start_date='20251105',end_date='20251105')
            return {'rows':len(df)}
        import akshare as ak
        return {'version':ak.__version__,'note':'本地库可用；网络取数请到试查验证'}
    if name=='tushare':return tushare_fetch(q,key)
    if name=='akshare':return akshare_fetch(q)
    return remote_fetch(name,q,p,key)


def tushare_fetch(q,key):
    import tushare as ts
    from .acquisition import acquire
    return acquire('tushare',q,sdk=ts.pro_api(key))


def akshare_fetch(q):
    import akshare as ak
    from .acquisition import acquire
    return acquire('akshare',q,sdk=ak)


def remote_fetch(name,q,p,key):
    from .acquisition import acquire
    return acquire(name,q,remote=Remote(name,p,key))
