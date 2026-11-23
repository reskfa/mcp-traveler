"""Local SDK API catalogs for Tushare Pro (syncable from waditu-tushare/skills)
and AKShare (introspected from the locally installed package).

Used by `gateway/explorer.py` so Tushare and AKShare participate in `explore_field`
alongside MCP providers, saving Agents from hunting through external docs.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from . import config

# Curated baseline of high-frequency Tushare Pro read-only APIs (subset of the
# 220+ endpoints documented in https://github.com/waditu-tushare/skills).
TUSHARE_BASELINE_APIS = [
    {'api_name': 'daily', 'title': 'A股日线行情', 'category': 'stock', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_chg', 'vol', 'amount'],
     'description': 'A股未复权日线开高低收成交量成交额涨跌幅'},
    {'api_name': 'daily_basic', 'title': '每日指标(估值/换手率/市值)', 'category': 'stock', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'trade_date', 'close', 'turnover_rate', 'turnover_rate_f', 'volume_ratio', 'pe', 'pe_ttm', 'pb', 'ps', 'ps_ttm', 'dv_ratio', 'dv_ttm', 'total_share', 'float_share', 'free_share', 'total_mv', 'circ_mv'],
     'description': '每日换手率、量比、市盈率PE、市净率PB、市销率PS、股息率、总股本、流通股本、总市值、流通市值'},
    {'api_name': 'fina_indicator', 'title': '财务指标数据', 'category': 'stock', 'params': ['ts_code', 'ann_date', 'start_date', 'end_date', 'period'],
     'fields': ['ts_code', 'end_date', 'eps', 'dt_eps', 'bps', 'roe', 'roe_waa', 'roe_dt', 'roa', 'grossprofit_margin', 'netprofit_margin', 'debt_to_assets', 'current_ratio', 'quick_ratio', 'netprofit_yoy', 'or_yoy'],
     'description': '每股收益EPS、ROE、ROA、毛利率、净利率、资产负债率、流动比率、速动比率、营收与净利润同比增速'},
    {'api_name': 'income', 'title': '利润表', 'category': 'stock', 'params': ['ts_code', 'ann_date', 'start_date', 'end_date', 'period'],
     'fields': ['ts_code', 'end_date', 'total_revenue', 'revenue', 'int_income', 'operate_profit', 'total_profit', 'n_income', 'n_income_attr_p', 'basic_eps'],
     'description': '营业总收入、营业收入、利息收入、营业利润、利润总额、净利润、归母净利润'},
    {'api_name': 'balancesheet', 'title': '资产负债表', 'category': 'stock', 'params': ['ts_code', 'ann_date', 'start_date', 'end_date', 'period'],
     'fields': ['ts_code', 'end_date', 'total_assets', 'total_liab', 'total_hldr_eqy_exc_min_int', 'money_cap', 'accounts_receiv', 'inventories'],
     'description': '总资产、总负债、归母净资产、货币资金、应收账款、存货、股东权益'},
    {'api_name': 'cashflow', 'title': '现金流量表', 'category': 'stock', 'params': ['ts_code', 'ann_date', 'start_date', 'end_date', 'period'],
     'fields': ['ts_code', 'end_date', 'n_cashflow_act', 'n_cashflow_inv_act', 'n_cash_flows_fnc_act', 'free_cashflow'],
     'description': '经营活动现金流净额、投资活动现金流净额、筹资活动现金流净额、自由现金流'},
    {'api_name': 'fina_mainbz', 'title': '主营业务构成', 'category': 'stock', 'params': ['ts_code', 'period', 'start_date', 'end_date'],
     'fields': ['ts_code', 'end_date', 'bz_item', 'bz_sales', 'bz_profit', 'bz_cost', 'curr_type'],
     'description': '按产品或地区拆分的主营业务收入、主营利润、主营成本构成'},
    {'api_name': 'forecast', 'title': '业绩预告', 'category': 'stock', 'params': ['ts_code', 'ann_date', 'start_date', 'end_date', 'period'],
     'fields': ['ts_code', 'ann_date', 'end_date', 'type', 'p_change_min', 'p_change_max', 'net_profit_min', 'net_profit_max', 'summary'],
     'description': '上市公司业绩预告、预增预减、预计净利润区间与变动幅度'},
    {'api_name': 'express', 'title': '业绩快报', 'category': 'stock', 'params': ['ts_code', 'ann_date', 'start_date', 'end_date', 'period'],
     'fields': ['ts_code', 'ann_date', 'end_date', 'revenue', 'operate_profit', 'total_profit', 'n_income', 'total_assets', 'diluted_roe', 'yoy_net_profit'],
     'description': '上市公司业绩快报营收、净利润、总资产与净资产收益率'},
    {'api_name': 'dividend', 'title': '分红送股数据', 'category': 'stock', 'params': ['ts_code', 'ann_date', 'ex_date'],
     'fields': ['ts_code', 'end_date', 'ann_date', 'div_proc', 'stk_div', 'cash_div', 'cash_div_tax', 'record_date', 'ex_date', 'pay_date'],
     'description': '分红派息、送股转增、每股派现、除权除息日、股权登记日'},
    {'api_name': 'top10_holders', 'title': '前十大股东', 'category': 'stock', 'params': ['ts_code', 'period', 'ann_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'ann_date', 'end_date', 'holder_name', 'hold_amount', 'hold_ratio', 'hold_change'],
     'description': '上市公司前十大股东持股数量、持股比例及变动'},
    {'api_name': 'top10_floatholders', 'title': '前十大流通股东', 'category': 'stock', 'params': ['ts_code', 'period', 'ann_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'ann_date', 'end_date', 'holder_name', 'hold_amount', 'hold_ratio', 'hold_float_ratio'],
     'description': '上市公司前十大流通股东名单、持股数、占流通股比例'},
    {'api_name': 'stk_holdernumber', 'title': '股东人数', 'category': 'stock', 'params': ['ts_code', 'enddate', 'start_date', 'end_date'],
     'fields': ['ts_code', 'ann_date', 'end_date', 'holder_num'],
     'description': '上市公司股东户数、股东人数历史变化'},
    {'api_name': 'stk_holdertrade', 'title': '股东增减持', 'category': 'stock', 'params': ['ts_code', 'ann_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'ann_date', 'holder_name', 'holder_type', 'in_de', 'change_vol', 'change_ratio', 'after_share', 'avg_price'],
     'description': '大股东高管增持减持公告、变动股数、变动比例、成交均价'},
    {'api_name': 'margin_detail', 'title': '融资融券交易明细', 'category': 'stock', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['trade_date', 'ts_code', 'rzye', 'rqye', 'rzmre', 'rqyl', 'rzche', 'rqchl', 'rqmcl', 'rzrqye'],
     'description': '个股融资余额、融券余额、融资买入额、融资偿还额、融券余量、两融余额'},
    {'api_name': 'moneyflow', 'title': '个股资金流向', 'category': 'stock', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'trade_date', 'buy_sm_amount', 'sell_sm_amount', 'buy_md_amount', 'buy_lg_amount', 'buy_elg_amount', 'net_mf_vol', 'net_mf_amount'],
     'description': '个股主力资金流向、大单超大单小单净流入流出金额与成交量'},
    {'api_name': 'moneyflow_hsgt', 'title': '沪深港通资金流向(北向/南向)', 'category': 'stock', 'params': ['trade_date', 'start_date', 'end_date'],
     'fields': ['trade_date', 'ggt_ss', 'ggt_sz', 'hgt', 'sgt', 'north_money', 'south_money'],
     'description': '北向资金、陆股通、沪股通、深股通、南向资金每日净流入'},
    {'api_name': 'hk_hold', 'title': '沪深股通持股明细(北向持股)', 'category': 'stock', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['code', 'trade_date', 'ts_code', 'name', 'vol', 'ratio', 'exchange'],
     'description': '北向资金陆股通个股持股数量与占流通股比例'},
    {'api_name': 'top_list', 'title': '龙虎榜每日明细', 'category': 'stock', 'params': ['trade_date', 'ts_code'],
     'fields': ['trade_date', 'ts_code', 'name', 'close', 'pct_change', 'turnover_rate', 'amount', 'l_sell', 'l_buy', 'l_amount', 'net_amount', 'reason'],
     'description': '龙虎榜上榜原因、买入额、卖出额、净买入额'},
    {'api_name': 'top_inst', 'title': '龙虎榜机构交易明细', 'category': 'stock', 'params': ['trade_date', 'ts_code'],
     'fields': ['trade_date', 'ts_code', 'exalter', 'side', 'buy', 'buy_rate', 'sell', 'sell_rate', 'net_buy', 'reason'],
     'description': '龙虎榜营业部及机构专用席位买卖金额'},
    {'api_name': 'block_trade', 'title': '大宗交易', 'category': 'stock', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'trade_date', 'price', 'vol', 'amount', 'buyer', 'seller'],
     'description': '大宗交易成交价、成交量、成交额、买方卖方营业部'},
    {'api_name': 'pledge_stat', 'title': '股权质押统计', 'category': 'stock', 'params': ['ts_code', 'end_date'],
     'fields': ['ts_code', 'end_date', 'pledge_count', 'unrest_pledge', 'rest_pledge', 'total_share', 'pledge_ratio'],
     'description': '上市公司股权质押笔数、质押股数、质押比例'},
    {'api_name': 'repurchase', 'title': '股票回购', 'category': 'stock', 'params': ['ann_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'ann_date', 'end_date', 'proc', 'exp_date', 'vol', 'amount', 'high_limit', 'low_limit'],
     'description': '上市公司股份回购进度、回购数量、回购金额与价格上下限'},
    {'api_name': 'share_float', 'title': '限售股解禁', 'category': 'stock', 'params': ['ts_code', 'ann_date', 'float_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'ann_date', 'float_date', 'float_share', 'float_ratio', 'holder_name', 'share_type'],
     'description': '限售股解禁日期、解禁股数、解禁占比、股东名称'},
    {'api_name': 'stk_limit', 'title': '每日涨跌停价格', 'category': 'stock', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['trade_date', 'ts_code', 'pre_close', 'up_limit', 'down_limit'],
     'description': '每日涨停价、跌停价、前收盘价'},
    {'api_name': 'suspend_d', 'title': '每日停复牌信息', 'category': 'stock', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'trade_date', 'suspend_timing', 'suspend_type'],
     'description': '股票停牌、复牌日期与停牌类型'},
    {'api_name': 'cb_basic', 'title': '可转债基本信息', 'category': 'convertible_bond', 'params': ['ts_code'],
     'fields': ['ts_code', 'bond_full_name', 'bond_short_name', 'stk_code', 'stk_short_name', 'maturity', 'par', 'issue_size', 'remain_size', 'conv_price', 'conv_start_date', 'conv_end_date', 'list_date', 'delist_date', 'rating'],
     'description': '可转债代码、简称、正股代码、发行规模、剩余规模、转股价、到期期限、评级'},
    {'api_name': 'cb_daily', 'title': '可转债日线行情', 'category': 'convertible_bond', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'trade_date', 'pre_close', 'open', 'high', 'low', 'close', 'change', 'pct_chg', 'vol', 'amount', 'bond_value', 'bond_over_rate', 'cb_value', 'cb_over_rate'],
     'description': '可转债日线开高低收、纯债价值、纯债溢价率、转股价值、转股溢价率'},
    {'api_name': 'cb_call', 'title': '可转债赎回信息', 'category': 'convertible_bond', 'params': ['ts_code', 'ann_date'],
     'fields': ['ts_code', 'call_type', 'is_call', 'ann_date', 'call_date', 'call_price', 'call_price_tax', 'call_vol', 'call_amount'],
     'description': '可转债强赎、有条件赎回、到期赎回公告日期、赎回价格与状态'},
    {'api_name': 'cb_price_chg', 'title': '可转债转股价变动', 'category': 'convertible_bond', 'params': ['ts_code'],
     'fields': ['ts_code', 'bond_short_name', 'publish_date', 'change_date', 'convert_price_initial', 'convertprice_bef', 'convertprice_aft'],
     'description': '可转债转股价下修、分红调整前后转股价格与变动日期'},
    {'api_name': 'cb_share', 'title': '可转债转股结果', 'category': 'convertible_bond', 'params': ['ts_code', 'ann_date'],
     'fields': ['ts_code', 'bond_short_name', 'publish_date', 'end_date', 'issue_size', 'convert_price', 'convert_val', 'convert_vol', 'convert_ratio', 'acc_convert_ratio', 'remain_size'],
     'description': '可转债累计转股金额、转股数量、转股比例、剩余规模'},
    {'api_name': 'cb_rate', 'title': '可转债票面利率', 'category': 'convertible_bond', 'params': ['ts_code'],
     'fields': ['ts_code', 'rate_freq', 'rate_start_date', 'rate_end_date', 'coupon_rate'],
     'description': '可转债各年度票面利率与付息周期'},
    {'api_name': 'index_daily', 'title': '指数日线行情', 'category': 'index', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'trade_date', 'close', 'open', 'high', 'low', 'pre_close', 'change', 'pct_chg', 'vol', 'amount'],
     'description': '指数日线开高低收涨跌幅成交量成交额'},
    {'api_name': 'index_dailybasic', 'title': '大盘指数每日指标(PE/PB/换手率)', 'category': 'index', 'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
     'fields': ['ts_code', 'trade_date', 'total_mv', 'float_mv', 'turnover_rate', 'pe', 'pe_ttm', 'pb'],
     'description': '大盘指数市盈率PE、市净率PB、换手率、总市值'},
    {'api_name': 'shibor', 'title': 'Shibor利率数据', 'category': 'macro', 'params': ['date', 'start_date', 'end_date'],
     'fields': ['date', 'on', '1w', '2w', '1m', '3m', '6m', '9m', '1y'],
     'description': '上海银行间同业拆放利率Shibor隔夜至1年期限'},
    {'api_name': 'cn_cpi', 'title': '中国居民消费价格指数CPI', 'category': 'macro', 'params': ['m', 'start_m', 'end_m'],
     'fields': ['month', 'nt_val', 'nt_yoy', 'nt_mom', 'nt_accu'],
     'description': '中国CPI同比环比累计宏观通胀数据'},
]

# Never execute write/auth/session helpers in SDK introspection
BLOCKED_SDK_PREFIXES = ('_', 'set_', 'get_token', 'pro_api', 'login', 'logout', 'init', 'tool_trade_date')


def _compact(text):
    return re.sub(r'[\s，,。:：；;（）()\-_/]+', '', str(text or '')).casefold()


def _tushare_cache_path() -> Path:
    return config.ROOT / 'local/schemas/tushare_sdk.json'


def _akshare_cache_path() -> Path:
    return config.ROOT / 'local/schemas/akshare_sdk.json'


def load_tushare_catalog() -> list[dict]:
    path = _tushare_cache_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            apis = data.get('apis')
            if isinstance(apis, list) and apis:
                return apis
        except (ValueError, OSError):
            pass
    return list(TUSHARE_BASELINE_APIS)


def sync_tushare_skills_catalog(timeout: int = 20) -> dict:
    """Sync Tushare Pro API catalog from official https://github.com/waditu-tushare/skills."""
    import requests
    merged = {item['api_name']: dict(item) for item in TUSHARE_BASELINE_APIS}
    synced_files = 0
    errors = []
    tree_url = 'https://api.github.com/repos/waditu-tushare/skills/git/trees/main?recursive=1'
    try:
        r = requests.get(tree_url, timeout=timeout, headers={'Accept': 'application/vnd.github+json'})
        if r.status_code != 200:
            raise RuntimeError(f'GitHub API HTTP {r.status_code}')
        tree = r.json().get('tree') or []
        md_paths = [node['path'] for node in tree if node.get('type') == 'blob' and node.get('path', '').endswith('.md')]
        for md_path in md_paths[:60]:
            raw_url = f'https://raw.githubusercontent.com/waditu-tushare/skills/main/{md_path}'
            try:
                resp = requests.get(raw_url, timeout=timeout)
                if resp.status_code != 200:
                    continue
                synced_files += 1
                text = resp.text
                # Extract markdown table rows or api mentions: `api_name` + Chinese description
                for line in text.splitlines():
                    m = re.search(r'\|\s*`?([a-z][a-z0-9_]{2,40})`?\s*\|\s*([^|\n]{2,120})\|', line)
                    if not m:
                        continue
                    api_name, title = m.group(1).strip(), m.group(2).strip()
                    if api_name in {'api_name', 'name', 'field', 'param', 'type', 'string', 'float', 'int'}:
                        continue
                    cat = 'convertible_bond' if api_name.startswith('cb_') or '转债' in title else (
                        'index' if api_name.startswith('index_') else 'stock'
                    )
                    entry = merged.setdefault(api_name, {
                        'api_name': api_name, 'title': title, 'category': cat,
                        'params': ['ts_code', 'trade_date', 'start_date', 'end_date'],
                        'fields': [], 'description': title,
                    })
                    if title and title not in entry['description']:
                        entry['description'] = f"{entry['description']}；{title}"
            except Exception as exc:
                errors.append(f'{md_path}: {exc}')
    except Exception as exc:
        errors.append(str(exc))

    apis = sorted(merged.values(), key=lambda x: x['api_name'])
    payload = {
        'source': 'https://github.com/waditu-tushare/skills',
        'count': len(apis),
        'synced_files': synced_files,
        'errors': errors[:5],
        'apis': apis,
    }
    config.write_json(_tushare_cache_path(), payload)
    return payload


def load_akshare_catalog(force_refresh: bool = False) -> list[dict]:
    """Introspect the locally installed akshare package and cache function signatures + docstrings."""
    import akshare as ak
    version = getattr(ak, '__version__', 'unknown')
    path = _akshare_cache_path()
    if not force_refresh and path.exists():
        try:
            cached = json.loads(path.read_text())
            if cached.get('version') == version and isinstance(cached.get('apis'), list):
                return cached['apis']
        except (ValueError, OSError):
            pass

    apis = []
    for name in sorted(dir(ak)):
        if name.startswith('_') or any(name.startswith(p) for p in BLOCKED_SDK_PREFIXES):
            continue
        obj = getattr(ak, name, None)
        if not callable(obj) or inspect.isclass(obj):
            continue
        try:
            sig = inspect.signature(obj)
            params = []
            required = []
            for pname, p in sig.parameters.items():
                if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                    continue
                params.append(pname)
                if p.default is inspect.Parameter.empty:
                    required.append(pname)
            sig_str = str(sig)
        except (TypeError, ValueError):
            params, required, sig_str = [], [], '()'
        doc = inspect.getdoc(obj) or ''
        first_line = next((ln.strip() for ln in doc.splitlines() if ln.strip()), '')
        summary = first_line[:160]
        doc_head = ' '.join(ln.strip() for ln in doc.splitlines()[:6] if ln.strip())[:320]
        cat = 'convertible_bond' if ('cov' in name or '转债' in doc_head) else (
            'stock' if name.startswith('stock_') else (
                'bond' if name.startswith('bond_') else 'other'
            )
        )
        apis.append({
            'api_name': name,
            'title': summary or name,
            'category': cat,
            'signature': sig_str,
            'params': params,
            'required_params': required,
            'description': doc_head or summary or name,
        })
    config.write_json(path, {'version': version, 'count': len(apis), 'apis': apis})
    return apis


def _score_text(query: str, corpus: str, api_name: str = '', fields: list[str] | None = None) -> float:
    q = _compact(query)
    c = _compact(corpus)
    if not q or not c:
        return 0.0
    score = 0.0
    q_lower = query.casefold()
    # Strong exact signals: api_name or exact output field name mentioned anywhere in query
    if api_name and re.search(rf'(?<![a-z0-9_]){re.escape(api_name.casefold())}(?![a-z0-9_])', q_lower):
        score += 200.0
    if fields:
        for fld in fields:
            if len(fld) >= 3 and re.search(rf'(?<![a-z0-9_]){re.escape(fld.casefold())}(?![a-z0-9_])', q_lower):
                score += 150.0
                break
    if q in c:
        score += 100.0 + len(q)
    # Token overlap (English words + Chinese bigrams/trigrams) — NO hard ratio cutoff!
    en_tokens = [t.casefold() for t in re.findall(r'[A-Za-z0-9_]{2,}', query) if t.casefold() not in {'tushare', 'akshare'}]
    zh_runs = re.findall(r'[\u4e00-\u9fff]+', query)
    zh_chars = ''.join(zh_runs)
    bigrams = {zh_chars[i:i + 2] for i in range(len(zh_chars) - 1)} if len(zh_chars) >= 2 else ({zh_chars} if zh_chars else set())
    trigrams = {zh_chars[i:i + 3] for i in range(len(zh_chars) - 2)} if len(zh_chars) >= 3 else set()
    # Filter ultra-generic framing bigrams so long NL prompts don't drown real business terms
    stop = {'全市场', '市场', '查询', '全部', '百分', '分数', '列为', '数据', '指标', '信息', '情况'}
    bi_hits = sum(1 for tok in (bigrams - stop) if tok in c)
    tri_hits = sum(1 for tok in (trigrams - stop) if tok in c)
    en_hits = sum(1 for tok in set(en_tokens) if tok in c)
    score += en_hits * 25.0 + tri_hits * 18.0 + bi_hits * 8.0
    return score


def search_sdk_candidates(provider: str, query: str, scope: str = 'stock', limit: int = 5) -> list[dict]:
    apis = load_tushare_catalog() if provider == 'tushare' else load_akshare_catalog()
    scored = []
    scope_fallbacks = []
    for item in apis:
        corpus = ' '.join([
            item.get('api_name', ''),
            item.get('title', ''),
            item.get('description', ''),
            ' '.join(item.get('fields') or []),
        ])
        cat = item.get('category', 'other')
        score = _score_text(query, corpus, api_name=item.get('api_name', ''), fields=item.get('fields'))
        if cat == scope:
            scope_fallbacks.append(item)
        if score <= 0:
            continue
        if scope == 'convertible_bond':
            if cat == 'convertible_bond':
                score += 40.0
            elif cat == 'stock':
                score *= 0.3
        elif scope == 'stock':
            if cat == 'stock':
                score += 20.0
            elif cat == 'convertible_bond':
                score *= 0.3
        scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], x[1]['api_name']))
    if scored:
        return [{**item, 'score': round(score, 2)} for score, item in scored[:limit]]
    # Never leave the Agent empty-handed: return top scope-relevant APIs as fallback hints
    return [{**item, 'score': 0.0, 'fallback_hint': True} for item in scope_fallbacks[:limit]]


def build_tushare_params(api_entry: dict, req: dict) -> dict | None:
    """Safely map an ExploreFieldRequest dict to read-only Tushare Pro API kwargs."""
    allowed = set(api_entry.get('params') or [])
    kwargs = {}
    symbol = req.get('symbol')
    symbols = req.get('symbols') or []
    if symbol and 'ts_code' in allowed:
        kwargs['ts_code'] = symbol
    elif len(symbols) == 1 and 'ts_code' in allowed:
        kwargs['ts_code'] = symbols[0]

    def compact_d(d):
        return d.replace('-', '') if d else None

    if req.get('period') and 'period' in allowed:
        kwargs['period'] = compact_d(req['period'])
    if req.get('date'):
        if 'trade_date' in allowed:
            kwargs['trade_date'] = compact_d(req['date'])
        elif 'date' in allowed:
            kwargs['date'] = compact_d(req['date'])
        elif 'end_date' in allowed:
            kwargs['end_date'] = compact_d(req['date'])
    if req.get('start_date') and 'start_date' in allowed:
        kwargs['start_date'] = compact_d(req['start_date'])
    if req.get('end_date') and 'end_date' in allowed:
        kwargs['end_date'] = compact_d(req['end_date'])
    # Require at least one bounding parameter so we don't accidentally pull millions of rows
    if not kwargs and ('ts_code' in allowed or 'trade_date' in allowed):
        return None
    # Always request full documented fields for Tushare Pro APIs (same quota cost,
    # prevents dropping non-default columns like cb_over_rate / cb_value / bond_value).
    api_fields = api_entry.get('fields') or []
    if api_fields:
        kwargs['fields'] = ','.join(api_fields)
    return kwargs


def build_akshare_params(api_entry: dict, req: dict) -> dict | None:
    """Safely map an ExploreFieldRequest dict to read-only AKShare function kwargs."""
    params = set(api_entry.get('params') or [])
    required = set(api_entry.get('required_params') or [])
    kwargs = {}
    symbol = req.get('symbol') or ((req.get('symbols') or [None])[0])
    code6 = symbol.split('.')[0] if symbol else None

    if 'symbol' in params and code6:
        # Only auto-fill symbol when it is a stock/bond code parameter (not a Chinese enum)
        doc = api_entry.get('description') or ''
        if 'symbol=' not in doc or re.search(r'symbol\s*=\s*["\']\d{6}', doc) or 'symbol' in required:
            kwargs['symbol'] = code6
    if 'stock' in params and code6:
        kwargs['stock'] = code6
    if 'date' in params and req.get('date'):
        kwargs['date'] = req['date'].replace('-', '')
    if 'start_date' in params and req.get('start_date'):
        kwargs['start_date'] = req['start_date'].replace('-', '')
    if 'end_date' in params and req.get('end_date'):
        kwargs['end_date'] = req['end_date'].replace('-', '')
    if required - set(kwargs):
        return None
    return kwargs
