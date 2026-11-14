FIELDS = {
 'daily_prices': {'open':'开盘价','high':'最高价','low':'最低价','close':'收盘价','volume':'成交量','amount':'成交额','turnover_rate':'换手率'},
 'valuations': {'pe_ttm':'市盈率TTM','pb':'市净率','total_mv':'总市值','circ_mv':'流通市值'},
 'financials': {'revenue':'营业收入','net_profit_parent':'归母净利润','net_profit_excl':'扣非净利润','total_assets':'总资产','total_liab':'总负债','equity_parent':'归母权益','operating_cashflow':'经营现金流','eps':'基本每股收益','roe':'净资产收益率'}
}
UNITS = {k: ('股' if k == 'volume' else '%' if k in ['turnover_rate','roe'] else '倍' if k in ['pe_ttm','pb'] else '元/股' if k == 'eps' else '元') for group in FIELDS.values() for k in group}
CAPABILITIES = {
 'tushare': '历史日线、历史估值、三大报表及财务指标',
 'akshare': '东财／新浪历史日线、百度 PE/PB 历史、新浪财务报表及关键指标',
 'wind': '固定参数 K 线；自然语言历史估值和财务，部分财务值为亿元展示精度',
 'ifind': '自然语言历史量价、估值和财务；Markdown 表格解析',
 'tdx': '固定参数日线和财务；历史估值使用自然语言工具；K 线按偏移分页'
}
def catalog():
    return {'datasets': {g:{'fields':{k:{'label':v,'unit':UNITS[k]} for k,v in f.items()}} for g,f in FIELDS.items()}, 'providers':CAPABILITIES,
            'notes':['自动路由逐字段补齐，保留已有值；field_sources 与数据行对应，记录每个数值的来源。','默认不复权；财务默认合并报表、报告期累计值。','来源间口径未严格对齐；原始返回可追溯。']}
