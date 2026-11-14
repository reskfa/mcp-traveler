import json
import math
from decimal import Decimal
import re
from datetime import date

ALIASES = {
 'open':['OPEN','Open','开盘','开盘价'], 'high':['HIGH','High','最高','最高价'],
 'low':['LOW','Low','最低','最低价'], 'close':['MATCH','Close','收盘','收盘价'],
 'volume':['VOLUME','Volume','成交量'], 'amount':['TURNOVER','Amount','成交额'],
 'turnover_rate':['CHANGEHANDRATE','换手率'], 'pe_ttm':['市盈率TTM','市盈率(TTM)','市盈率(PE,TTM)'],
 'pb':['市净率','市净率(PB,最新)','市净率(PB,MRQ)','市净率(LF)'], 'total_mv':['总市值'], 'circ_mv':['流通市值','A股流通市值'],
 'revenue':['营业收入'], 'net_profit_parent':['归属母公司净利润','归属于母公司所有者的净利润','归属母公司股东的净利润','归母净利润'],
 'net_profit_excl':['扣非净利润','扣除非经常性损益后的归属母公司净利润','扣除非经常性损益后的归属母公司股东净利润','扣除非经常性损益后归属于母公司股东的净利润','扣除非经常性损益后归母净利润'],
 'total_assets':['资产总计','总资产','资产合计'], 'total_liab':['负债合计','总负债'],
 'equity_parent':['归属于母公司股东权益合计','归属母公司股东权益合计','归属于母公司所有者权益合计','归母权益','其中：母公司股东权益'],
 'operating_cashflow':['经营活动产生的现金流量净额','经营现金流','经营活动现金流量净额'], 'eps':['基本每股收益'], 'roe':['净资产收益率','ROE','净资产收益率ROE(加权,公布值)']
}

def scale_factor(unit):
    return 10**12 if '万亿' in unit else 10**8 if '亿' in unit else 10**4 if '万' in unit else 1

def number(value, unit=''):
    if value is None: return None
    if isinstance(value, (int,float)):
        if not math.isfinite(value): return None
        n = float(value)
        return float(Decimal(str(n)) * scale_factor(unit))
    s = str(value).strip().replace(',','')
    if s in ('','--','—','None','nan','N/A'): return None
    m = re.fullmatch(r'([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(万亿|亿|万)?(?:人民币|元|股|手|倍|%)?', s)
    if not m: return None
    scale = m[2] or unit
    return float(Decimal(m[1]) * scale_factor(scale))

def norm_date(value):
    if value is None: return None
    s = str(value)
    if re.match(r'^\d{8}$', s): return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    if re.match(r'^\d{4}-\d{2}-\d{2}',s): return s[:10]
    return None

def json_objects(text):
    dec = json.JSONDecoder(); pos = 0
    while pos < len(text):
        candidates = [x for x in [text.find('{',pos),text.find('[',pos)] if x >= 0]
        if not candidates: break
        start = min(candidates)
        try:
            obj, end = dec.raw_decode(text[start:]); yield obj; pos = start+end
        except ValueError: pos = start+1

def markdown_rows(text):
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith('|')]
    if len(lines)<2: return []
    cols = [x.strip() for x in lines[0].strip('|').split('|')]
    return [dict(zip(cols,[x.strip() for x in l.strip('|').split('|')])) for l in lines[2:] if not re.fullmatch(r'[| :\-]+',l)]

def tables(obj):
    """Read actual data blocks, excluding vendor UI/presentation metadata."""
    result=[]
    def visit(x):
        if isinstance(x,dict):
            if isinstance(x.get('columns'),list) and isinstance(x.get('rows'),list):
                cols=x['columns']; units={c['name']:c.get('unit','') for c in cols}
                for k,v in x.get('indicator_units',{}).items(): units[k.split(' ')[0]]=v
                result.extend([(dict(zip([c['name'] for c in cols],r)),units) for r in x['rows']]);return
            if 'Rows' in x and isinstance(x['Rows'],list):
                result.extend((r,{}) for r in x['Rows'] if isinstance(r,dict));return
            if 'rows' in x and isinstance(x['rows'],list) and all(isinstance(r,dict) for r in x['rows']):
                result.extend((r,{}) for r in x['rows']);return
            if isinstance(x.get('answer'),str): result.extend((r,{}) for r in markdown_rows(x['answer']));return
            if isinstance(x.get('text'),str):
                if x['text'].startswith('[MCP App'):return
                for o in json_objects(x['text']):visit(o)
                return
            for k,v in x.items():
                if k not in ['structuredContent','_meta','dataCard','dataFunction','request','candidateEntities','outputSchema']:visit(v)
        elif isinstance(x,list):
            if x and all(isinstance(v,dict) and ('code' in v or '证券代码' in v) for v in x):
                result.extend((v,{}) for v in x)
            else:
                for v in x:visit(v)
    visit(obj)
    return result

def compact(s):return re.sub(r'[\s（）(),，:：]','',str(s)).lower()

def map_rows(table_rows, fields, symbol, category, requested_date=None):
    output=[];matched=set()
    for raw,units in table_rows:
        d=next((norm_date(raw[k]) for k in ['date','trade_date','end_date','日期','报告日','截止日期','TIME','Data'] if k in raw and norm_date(raw[k])),None)
        if not d:
            dates={m.group(1) for k in raw for m in re.finditer(r'交易日[:：](\d{8})',str(k))}
            if len(dates)==1:d=norm_date(next(iter(dates)))
        if not d:
            dates={f'{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}' for k in raw for m in re.finditer(r'(20\d{2})年(\d{1,2})月(\d{1,2})日',str(k))}
            if len(dates)==1:d=next(iter(dates))
        row={'symbol':symbol,'date':d or requested_date}
        for field in fields:
            value=None
            for key,val in raw.items():
                key_without_date=re.sub(r'^20\d{2}年(?:\d{1,2}月\d{1,2}日|年报)?','',str(key))
                clean=compact(re.sub(r'[（(]交易日[:：]\d{8}[）)]','',key_without_date))
                aliases=ALIASES.get(field,[field])
                exact=any(clean==compact(a) or clean.startswith(compact(a)+'单位') for a in aliases)
                if exact and not any(x in clean for x in ['同比','增长','报表类型','单季度']):
                    unit=units.get(key,'')
                    if not unit and '单位' in str(key):unit=str(key).split('单位',1)[1]
                    value=number(val,unit);matched.add(field);break
            row[field]=value
        if category=='daily_prices' and 'RawVolume' in raw and 'volume' in fields:row['volume']=number(raw['RawVolume']);matched.add('volume')
        output.append(row)
    return output,matched
