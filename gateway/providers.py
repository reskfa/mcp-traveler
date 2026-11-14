import json
import re
from datetime import date
import requests
from . import config
from .parsing import tables, map_rows, norm_date, number
from .catalog import FIELDS

class Remote:
    def __init__(self,name,p,key):
        server=next(iter(p['mcp_servers'].values()))
        self.url=server['url'];self.timeout=p.get('timeout_seconds',60)
        if not self.url:raise ValueError('尚未配置 MCP 地址')
        self.s=requests.Session()
        self.s.headers.update({'Content-Type':'application/json','Accept':'application/json, text/event-stream',server['auth_header']:server['auth_prefix']+key})
        self.seq=0
        if name=='tdx':
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
    if q.get('probe'):
        if name in ['wind','ifind','tdx']:
            r=Remote(name,p,key).rpc('tools/list',{})
            return {'tools':[{'name':t['name'],'inputSchema':t.get('inputSchema',{})} for t in r.get('tools',[])]}
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
    pro=ts.pro_api(key);symbol=q['symbol'];cat=q['dataset'];raw=[];notes=[]
    fields=q['fields'];data=[]
    if cat=='daily_prices':
        args={'ts_code':symbol,'start_date':compact_date(q['start_date']),'end_date':compact_date(q['end_date'])}
        df=pro.daily(**args);raw.append(records(df))
        if not df.empty and q['adjust']!='none':
            # Use full factor history for a stable adjustment anchor, not the query end.
            factors=pro.adj_factor(ts_code=symbol);raw.append(records(factors))
            if factors.empty:raise RuntimeError('没有复权因子')
            factors=factors.sort_values('trade_date')
            anchor=float(factors.iloc[-1 if q['adjust']=='qfq' else 0]['adj_factor'])
            df=df.merge(factors[['trade_date','adj_factor']],on='trade_date',how='left',validate='one_to_one')
            if df.adj_factor.isna().any():raise RuntimeError('部分日期缺少复权因子')
            for col in ['open','high','low','close']:df[col]=df[col]*df.adj_factor/anchor
            notes.append('复权基准取当前可用因子历史的末日／首日；不与其他来源复权价拼接。')
        if 'turnover_rate' in fields and not df.empty:
            basic=pro.daily_basic(**args,fields='trade_date,turnover_rate');raw.append(records(basic))
            df=df.merge(basic,on='trade_date',how='left')
        for r in records(df):data.append({'symbol':symbol,'date':norm_date(r['trade_date']),**{f:(round(number(r.get('vol'))*100) if r.get('vol') is not None else None) if f=='volume' else (number(r.get('amount'))*1000 if r.get('amount') is not None else None) if f=='amount' else r.get(f) for f in fields}})
    elif cat=='valuations':
        args={'ts_code':symbol,'fields':'ts_code,trade_date,pe_ttm,pb,total_mv,circ_mv'}
        if q.get('date'):args['trade_date']=compact_date(q['date'])
        else:args['limit']=1
        df=pro.daily_basic(**args);raw.append(records(df))
        for r in records(df):data.append({'symbol':symbol,'date':norm_date(r['trade_date']),**{f:number(r.get(f),'万元' if f in ['total_mv','circ_mv'] else '') for f in fields}})
    else:
        groups={'income':{'revenue':'revenue','net_profit_parent':'n_income_attr_p','eps':'basic_eps'},'balancesheet':{'total_assets':'total_assets','total_liab':'total_liab','equity_parent':'total_hldr_eqy_exc_min_int'},'cashflow':{'operating_cashflow':'n_cashflow_act'},'fina_indicator':{'net_profit_excl':'profit_dedt','roe':'roe'}}
        merged={}
        for api,mapping in groups.items():
            needed={f:v for f,v in mapping.items() if f in fields}
            if not needed:continue
            args={'ts_code':symbol,'period':compact_date(q['period']),'fields':','.join(['ts_code','end_date','ann_date']+list(needed.values()))}
            if api!='fina_indicator':args['report_type']='1'
            df=pro.query(api,**args);raw.append({api:records(df)})
            if not df.empty:
                df=df.sort_values('ann_date',ascending=False).drop_duplicates('end_date')
                for r in records(df):
                    row=merged.setdefault(r['end_date'],{'symbol':symbol,'date':norm_date(r['end_date'])})
                    row.update({f:r.get(v) for f,v in needed.items()})
        data=[{**r,**{f:r.get(f) for f in fields}} for r in merged.values()]
        notes.append('按报告期查询当前可用报表版本，非历史时点数据库。')
    return {'data':data,'raw':raw,'notes':notes,'upstream':'Tushare Pro'}


def akshare_fetch(q):
    import akshare as ak
    symbol=q['symbol'];code=symbol.split('.')[0];fields=q['fields'];cat=q['dataset'];raw=[];notes=[]
    if cat=='daily_prices':
        try:
            df=ak.stock_zh_a_hist(symbol=code,period='daily',start_date=compact_date(q['start_date']),end_date=compact_date(q['end_date']),adjust='' if q['adjust']=='none' else q['adjust'])
            raw=records(df);data,_=map_rows([(r,{}) for r in raw],fields,symbol,cat)
            for row in data:
                if row.get('volume') is not None:row['volume']*=100
            return {'data':data,'raw':raw,'notes':['东财成交量按手展示，换算为股后可能有取整差异。'],'upstream':'AKShare / 东方财富'}
        except Exception as exc:
            # Different upstream, one request each; keep this explicit in provenance.
            df=ak.stock_zh_a_daily(symbol=symbol.split('.')[1].lower()+code,start_date=compact_date(q['start_date']),end_date=compact_date(q['end_date']),adjust='' if q['adjust']=='none' else q['adjust'])
            raw=records(df)
            data=[]
            for r in raw:
                row={'symbol':symbol,'date':norm_date(r.get('date'))}
                row.update({f:r.get(f) for f in fields})
                if 'turnover_rate' in fields:row['turnover_rate']=number(r.get('turnover'))*100 if r.get('turnover') is not None else None
                data.append(row)
            return {'data':data,'raw':raw,'notes':['东财请求失败（'+type(exc).__name__+'），已使用新浪备用日线；成交量单位股。'],'upstream':'AKShare / 新浪财经'}
    if cat=='valuations':
        indicators={'pe_ttm':'市盈率(TTM)','pb':'市净率','total_mv':'总市值','circ_mv':'流通市值'};merged={}
        for f in fields:
            if f in ['total_mv','circ_mv']:continue
            df=ak.stock_zh_valuation_baidu(symbol=code,indicator=indicators[f],period='全部');rr=records(df);raw.append({f:rr})
            for r in rr:
                d=norm_date(r.get('date') or r.get('日期'))
                if d:
                    merged.setdefault(d,{'symbol':symbol,'date':d})[f]=number(r.get('value') if 'value' in r else r.get(indicators[f]))
        if not merged and not any(f in ['pe_ttm','pb'] for f in fields):raise RuntimeError('AKShare 市值单位尚未核实，请换来源或查询 PE/PB')
        data=list(merged.values())
        if q.get('date'):data=[r for r in data if r['date']==q['date']]
        elif data:data=[max(data,key=lambda r:r['date'])]
        # Baidu market cap unit is not established here; do not invent a scale.
        for r in data:
            for f in fields:
                if f in ['total_mv','circ_mv']:r[f]=None
                else:r.setdefault(f,None)
        notes.append('百度估值；市值单位尚未核实，市值字段暂留空。')
        return {'data':data,'raw':raw,'notes':notes,'upstream':'AKShare / 百度股市通'}
    groups={'利润表':{'revenue','net_profit_parent','eps'},'资产负债表':{'total_assets','total_liab','equity_parent'},'现金流量表':{'operating_cashflow'}}
    merged={}
    for report,fs in groups.items():
        if not fs.intersection(fields):continue
        df=ak.stock_financial_report_sina(stock=symbol.split('.')[1].lower()+code,symbol=report)
        rr=records(df);raw.append({report:rr})
        mapped,_=map_rows([(r,{}) for r in rr],list(fs.intersection(fields)),symbol,cat)
        for r in mapped:
            if r['date']==q['period']:merged.setdefault(r['date'],{'symbol':symbol,'date':r['date']}).update(r)
    if set(fields).intersection({'net_profit_excl','roe'}):
        rr=records(ak.stock_financial_abstract(symbol=code));raw.append({'关键指标':rr})
        for r in rr:
            f={'扣非净利润':'net_profit_excl','净资产收益率_平均':'roe'}.get(r.get('指标'))
            value=number(r.get(compact_date(q['period'])))
            if f in fields and value is not None:
                merged.setdefault(q['period'],{'symbol':symbol,'date':q['period']})[f]=value
    data=[{**r,**{f:r.get(f) for f in fields}} for r in merged.values()]
    return {'data':data,'raw':raw,'notes':['新浪财务报表及关键指标；ROE 使用平均净资产口径。'],'upstream':'AKShare / 新浪财经'}


def remote_fetch(name,q,p,key):
    remote=Remote(name,p,key);cat=q['dataset'];symbol=q['symbol'];raw=[];notes=[];table=[]
    if cat=='daily_prices' and name=='wind':
        r=remote.call('get_stock_kline',{'windcode':symbol,'begin_date':q['start_date'],'end_date':q['end_date'],'period':'10','aftype':{'none':'2','qfq':'0','hfq':'1'}[q['adjust']],'issusp':'0','count':0})
        raw.append(r);table=tables(r)
    elif cat=='daily_prices' and name=='tdx':
        code,market=symbol.split('.');setcode={'SH':'1','SZ':'0','BJ':'2'}[market]
        # A bounded exploration query; fail instead of silently returning partial history.
        for page in range(20):
            r=remote.call('tdx_kline',{'code':code,'setcode':setcode,'period':'4','wantNum':'1000','startxh':str(page*1000),'tqFlag':{'none':'0','qfq':'1','hfq':'2'}[q['adjust']]})
            raw.append(r);batch=tables(r);table.extend(batch)
            dates=[norm_date(row.get('Data')) for row,_ in batch];dates=[d for d in dates if d]
            if not batch or len(batch)<1000 or (dates and min(dates)<=q['start_date']):break
        else:raise RuntimeError('通达信历史分页达到上限，请缩小日期范围')
    elif cat=='financials' and name=='tdx':
        groups={'TdxShareCW.ph_agf10_cw_lyb':{'revenue','net_profit_parent','net_profit_excl','eps'},'TdxShareCW.ph_agf10_cw_zcfzb':{'total_assets','total_liab','equity_parent'},'TdxShareCW.ph_agf10_cw_xjllb':{'operating_cashflow'}}
        merged={}
        for entry,fs in groups.items():
            if not fs.intersection(q['fields']):continue
            r=remote.call('tdx_api_data',{'entry':entry,'code':symbol.split('.')[0],'fixedTag':'00101'})
            raw.append(r)
            for row,units in tables(r):
                d=norm_date(row.get('日期'))
                if d==q['period']:merged.setdefault(d,{}).update(row)
        table=[(r,{}) for r in merged.values()]
        notes.append('报告期累计值；ROE 暂未映射。')
    else:
        labels='、'.join({'net_profit_excl':'扣除非经常性损益后归属于母公司股东的净利润','pe_ttm':'市盈率TTM'}.get(f,FIELDS[cat][f]) for f in q['fields'])
        when=(q['start_date']+'至'+q['end_date']) if cat=='daily_prices' else q.get('date') or q.get('period') or '最新可用交易日'
        query=f'查询{symbol}，{when}的{labels}。'
        if cat=='daily_prices':query+=f"按交易日返回，复权方式：{ {'none':'不复权','qfq':'前复权','hfq':'后复权'}[q['adjust']]}。"
        if cat=='financials':query+='合并报表，报告期累计值，不要单季度或同比指标。'
        query+='只返回指定指标，含证券代码、日期及单位。金额用人民币元，成交量用股。'
        if name=='wind' and cat=='financials' and 'net_profit_excl' in q['fields']:
            # Wind resolves the long deduction indicator correctly in a dedicated query.
            # These are planned distinct requests, not retries of a billed call.
            other=[f for f in q['fields'] if f!='net_profit_excl']
            blocks=[]
            if other:
                other_labels='、'.join(FIELDS[cat][f] for f in other)
                blocks.append(remote.call('get_stock_fundamentals',{'question':f'查询{symbol} {when} {other_labels}，合并报表，报告期累计值。'}))
            report=q['period'][:4]+'年年报' if q['period'].endswith('-12-31') else q['period']+'报告期累计'
            blocks.append(remote.call('get_stock_fundamentals',{'question':f'查询{symbol} {report} 扣除非经常性损益后归属于母公司股东的净利润。'}))
            r={'blocks':blocks}
            notes.append('Wind 扣非净利润使用独立指标查询，与其他财务指标分开调用。')
        elif name=='wind':r=remote.call('get_stock_fundamentals',{'question':query})
        elif name=='ifind':r=remote.call('get_security_indicators',{'market':'A股','query':query})
        else:
            r=remote.call('tdx_indicator_select',{'message':f"{symbol.split('.')[0]} {when} {labels}",'rang':'AG'})
        raw.append(r);table=tables(r);notes.append('自然语言模板查询；请结合原始返回确认口径。')
    if name=='wind' and cat=='valuations' and 'pe_ttm' in q['fields']:
        # The query explicitly asks TTM, but Wind sometimes abbreviates the title.
        for row,units in table:
            for key in list(row):
                if key.endswith('市盈率'):
                    row[key+'TTM']=row.pop(key);units[key+'TTM']=units.get(key,'')
                    notes.append('市盈率按 TTM 请求；Wind 返回标题简写为市盈率，未单列口径。')
    if name=='ifind' and cat=='financials' and 'roe' in q['fields']:
        notes.append('ROE 使用上游加权公布值；不同来源 ROE 计算方式可能不同。')
    expected=q.get('date') or q.get('period')
    # Wind NL columns can carry an explicit year instead of a separate date column.
    if name=='wind' and cat=='financials' and expected and expected.endswith('-12-31') and table and all(any(expected[:4] in str(k) for k in row) for row,_ in table):
        assumed=expected;notes.append('日期来自请求及返回列标题；未提供独立日期字段。')
    elif name=='ifind' and expected and table:
        serialized=json.dumps(raw,ensure_ascii=False)
        assumed=expected if compact_date(expected) in serialized else None
    else:assumed=None
    data,matched=map_rows(table,q['fields'],symbol,cat,assumed)
    if table and not matched:raise RuntimeError('返回有内容，但尚未识别所需字段')
    if not table:
        txt=json.dumps(raw,ensure_ascii=False)
        if any(t in txt for t in ['无数据','暂无','无结果','rows": []','Rows": []']):data=[]
        else:raise RuntimeError('无法解析上游结果；可能是权限提示或返回格式变化')
    if cat=='daily_prices':
        if any(not r['date'] for r in data):raise RuntimeError('行情缺少日期，不能用于日期区间查询')
        data=[r for r in data if q['start_date']<=r['date']<=q['end_date']]
    elif expected:
        if any(not r['date'] for r in data):raise RuntimeError('返回未提供可核对的日期／报告期')
        data=[r for r in data if r['date']==expected]
    else:notes.append('最新快照／最新可用数据；来源未提供日期时日期为空。')
    # Multiple indicator blocks describe one security/date; nulls never erase values.
    merged={}
    for row in data:
        target=merged.setdefault((row['symbol'],row['date']),dict(row))
        for f in q['fields']:
            if target.get(f) is None and row.get(f) is not None:target[f]=row[f]
    data=list(merged.values())
    return {'data':data,'raw':raw,'notes':notes,'upstream':name}
