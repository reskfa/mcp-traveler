import json
import math
from decimal import Decimal
import re
from datetime import date

from .dictionary import DICTIONARY
ALIASES = {ds:{f:m['aliases'] for f,m in g['fields'].items()} for ds,g in DICTIONARY['datasets'].items()}

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
    # Each table owns its header, including adjacent tables with no blank line.
    lines=[line.strip() for line in text.splitlines()];result=[];columns=None;i=0
    def cells(line):return [x.strip() for x in line.strip('|').split('|')]
    while i<len(lines):
        line=lines[i]
        if not line.startswith('|'):columns=None;i+=1;continue
        if i+1<len(lines) and lines[i+1].startswith('|') and all(re.fullmatch(r':?-{3,}:?',x) for x in cells(lines[i+1])):
            columns=cells(line);i+=2;continue
        values=cells(line)
        if columns is not None and len(values)==len(columns):result.append(dict(zip(columns,values)))
        i+=1
    return result

def choice_identity(text):
    m=re.search(r'(\d{6}\.(?:SH|SZ|BJ))',str(text or ''),re.I)
    return m.group(1).upper() if m else None

def choice_date(header):
    s=re.sub(r'[（(]日[）)]$','',str(header).strip())
    if s in ('','值','最新','-','--'):return None
    if re.match(r'^\d{4}-\d{2}-\d{2}',s):return s[:10]
    rules=[(r'^(20\d{2})(?:年)?年报$','12-31'),(r'^(20\d{2})(?:年)?(?:一季报|一季度|Q1)$','03-31'),
           (r'^(20\d{2})(?:年)?(?:中报|半年报|二季报|二季度|Q2)$','06-30'),(r'^(20\d{2})(?:年)?(?:三季报|三季度|Q3)$','09-30')]
    for pattern,month_day in rules:
        m=re.match(pattern,s,re.I)
        if m:return f'{m[1]}-{month_day}'
    return norm_date(s)

def is_choice_sheet(block):
    columns=block.get('columns') if isinstance(block,dict) else None
    items=block.get('items') if isinstance(block,dict) else None
    return bool(isinstance(columns,list) and columns and isinstance(columns[0],str) and isinstance(items,list)
                and all(isinstance(item,list) and item for item in items))

def unpivot_choice_sheet(sheet):
    """Turn Choice data-browser sheets (indicator rows × date columns) into record rows."""
    columns=list(sheet.get('columns') or [])
    identity=choice_identity(columns[0] if columns else '') or choice_identity(sheet.get('sheetName',''))
    headers=columns[1:] if len(columns)>1 else ['值']
    rows=[]
    for i,header in enumerate(headers):
        row={}
        if identity:row['证券代码']=identity
        day=choice_date(header)
        if day:row['date']=day
        for item in sheet.get('items') or []:
            if i+1<len(item):row[str(item[0])]=item[i+1]
        if any(k not in ('证券代码','date') for k in row):rows.append(row)
    return rows

def tables(obj):
    """Read actual data blocks, excluding vendor UI/presentation metadata."""
    result=[]
    def visit(x):
        if isinstance(x,dict):
            data=x.get('data')
            if isinstance(data,list) and data and all(is_choice_sheet(sheet) for sheet in data):
                rows=[]
                for sheet in data:rows.extend(unpivot_choice_sheet(sheet))
                codes={row.get('证券代码') for row in rows if row.get('证券代码')}
                if len(codes)==1:
                    code=next(iter(codes))
                    for row in rows:row.setdefault('证券代码',code)
                result.extend((row,{}) for row in rows);return
            if is_choice_sheet(x):
                result.extend((row,{}) for row in unpivot_choice_sheet(x));return
            if isinstance(x.get('columns'),list) and isinstance(x.get('rows'),list):
                cols=x['columns']; units={c['name']:c.get('unit','') for c in cols}
                for k,v in x.get('indicator_units',{}).items(): units[k.split(' ')[0]]=v
                result.extend([(dict(zip([c['name'] for c in cols],r)),units) for r in x['rows']]);return
            if 'Rows' in x and isinstance(x['Rows'],list):
                result.extend((r,{}) for r in x['Rows'] if isinstance(r,dict));return
            if 'rows' in x and isinstance(x['rows'],list) and all(isinstance(r,dict) for r in x['rows']):
                result.extend((r,{}) for r in x['rows']);return
            if isinstance(x.get('answer'),str): result.extend((r,{'__indicator_params__':x.get('indicators_params',{})}) for r in markdown_rows(x['answer']));return
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

def map_rows(table_rows, fields, symbol, category, requested_date=None, bindings=None, expected_date=None, require_indicator_date=False, prefer_row_date=False):
    output=[];matched=set()
    for raw,units in table_rows:
        d=next((norm_date(raw[k]) for k in ['date','trade_date','end_date','数据日期','REPORT_DATE','日期','报告期','报告日','截止日期','TIME','Data'] if k in raw and norm_date(raw[k])),None)
        if not d:
            dates={m.group(1) for k in raw for m in re.finditer(r'交易日[:：](\d{8})',str(k))}
            if len(dates)==1:d=norm_date(next(iter(dates)))
        if not d:
            dates={f'{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}' for k in raw for m in re.finditer(r'(20\d{2})年(\d{1,2})月(\d{1,2})日',str(k))}
            if len(dates)==1:d=next(iter(dates))
        row={'symbol':symbol,'date':d or requested_date};row_matched=False
        for field in fields:
            value=None
            for key,val in raw.items():
                # A row can contain multiple years. A neighbouring column cannot date this value.
                stamp=re.match(r'^(20\d{2})年(?:(\d{1,2})月(\d{1,2})日)?',str(key))
                if stamp and expected_date:
                    column_date=f'{int(stamp[1]):04d}-{int(stamp[2] or 12):02d}-{int(stamp[3] or 31):02d}'
                    if column_date!=expected_date:continue
                column_stamp=re.search(r'(?:交易日|截止日期|报告期)[:：](\d{8})',str(key))
                column_day=norm_date(column_stamp[1]) if column_stamp else None
                if column_day and expected_date and column_day!=expected_date:continue
                key_without_date=re.sub(r'^20\d{2}年(?:\d{1,2}月\d{1,2}日|年报)?','',str(key))
                key_without_date=re.sub(r'[（(]复权方式[:：]不复权[,，]交易日[:：]\d{8}[）)]','',key_without_date)
                clean=compact(re.sub(r'[（(](?:交易日|截止日期|报告期)[:：]\d{8}[）)]','',key_without_date))
                aliases=(bindings[field]['columns']+bindings[field].get('extra_columns',[]) if bindings is not None else ALIASES.get(category,{}).get(field,[field]))
                exact=any(clean==compact(a) or clean.startswith(compact(a)+'单位') for a in aliases)
                if exact and (bindings is not None or not any(x in clean for x in ['同比','增长','报表类型','单季度'])):
                    if column_day:row['date']=column_day
                    if require_indicator_date:
                        params=units.get('__indicator_params__',{})
                        own=[v for k,v in params.items() if clean==compact(k) or clean.startswith(compact(k)+'单位')]
                        dates={norm_date(v) for p in own if isinstance(p,dict) for k,v in p.items() if any(w in k for w in ['日期','报告期']) and norm_date(v)}
                        if not (prefer_row_date and d==expected_date) and dates!={expected_date}:continue
                        if row['date'] is None:row['date']=expected_date
                    unit=units.get(key,'')
                    if not unit and '单位' in str(key):unit=str(key).split('单位',1)[1].strip(' ：:（）()')
                    if not unit and isinstance(val,str) and val.strip().endswith('%'):unit='%'
                    rule=bindings[field] if bindings is not None else {}
                    if 'accepted_units' in rule and unit not in rule['accepted_units']:
                        continue
                    value=number(val,unit)
                    if value is not None and 'unit_multipliers' in rule:
                        if unit not in rule['unit_multipliers']:continue
                        value=float(Decimal(str(value))*Decimal(str(rule['unit_multipliers'][unit])))
                    matched.add(field);row_matched=True;break
            if bindings is not None and value is not None:
                rule=bindings[field]
                value=float(Decimal(str(value))*Decimal(str(rule.get('multiplier',1))))
                if 'round_digits' in rule:value=round(value,rule['round_digits'])
            row[field]=value
        if row_matched:output.append(row)
    return output,matched
