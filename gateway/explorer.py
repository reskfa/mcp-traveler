"""Unified multi-provider field explorer (Probe -> Agent/User Judge -> Multi-Source Save).

Replaces brittle regex-based auto-validation with:
1. `explore_field`: Direct, non-blocking, controlled-concurrency probe across ALL enabled
   providers (4 MCPs + Tushare Pro SDK catalog + AKShare local SDK catalog), never re-checking
   the dictionary or short-circuiting on similar fields.
2. `save_explored_field`: Evidence-anchored multi-source declarative persistence into
   `local/user_dictionary.json`, enabling cross-source fill on subsequent regular queries.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import config, core, dictionary
from .generic_data import (
    cached_tools,
    ensure_tools,
    market_tool_score,
    relevance,
    runnable,
)
from .market_provider import normalize_symbol
from .panel import PANEL_DEFAULT_ROUTE
from .parsing import tables
from .sdk_catalog import (
    build_akshare_params,
    build_tushare_params,
    load_akshare_catalog,
    load_tushare_catalog,
    search_sdk_candidates,
)
from .tabular import describe_rows, preview_notice, write_csv_and_describe

Scope = Literal['stock', 'convertible_bond']


def _iso(value):
    if value and date.fromisoformat(value).isoformat() != value:
        raise ValueError('日期使用 YYYY-MM-DD')
    return value


def _infer_scope(symbol: str | None, symbols: list[str] | None, scope: Scope | None) -> Scope:
    sample = symbol or (symbols[0] if symbols else None)
    if sample:
        is_cb = bool(re.fullmatch(r'(?:11\d{4}\.SH|12\d{4}\.SZ)', sample.upper()))
        inferred: Scope = 'convertible_bond' if is_cb else 'stock'
        if scope and scope != inferred:
            raise ValueError(f'证券代码 {sample} 与 scope={scope} 不一致')
        return inferred
    return scope or 'stock'


class ExploreFieldRequest(BaseModel):
    request: str = Field(min_length=1, max_length=300)
    symbol: str | None = None
    symbols: list[str] | None = None
    scope: Scope | None = None
    date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    period: str | None = None
    providers: list[str] | None = None
    tool_overrides: dict[str, str] | None = None
    stop_on_first: bool = False
    per_provider_timeout: int = Field(default=25, ge=5, le=90)

    @model_validator(mode='after')
    def valid(self):
        self.request = self.request.strip()
        if not self.request:
            raise ValueError('请描述要探索的指标或数据需求')
        if self.symbol and self.symbols:
            raise ValueError('symbol 与 symbols 请二选一')
        if self.symbol:
            norm = normalize_symbol(self.symbol)
            if not norm or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', norm):
                raise ValueError(f'证券代码格式不正确：{self.symbol}')
            self.symbol = norm
        if self.symbols is not None:
            if not self.symbols:
                raise ValueError('symbols 不能为空数组；省略 symbol/symbols 表示全市场探索')
            cleaned = []
            for s in self.symbols:
                norm = normalize_symbol(s)
                if not norm or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', norm):
                    raise ValueError(f'证券代码格式不正确：{s}')
                if norm not in cleaned:
                    cleaned.append(norm)
            self.symbols = cleaned
        self.scope = _infer_scope(self.symbol, self.symbols, self.scope)
        for v in (self.date, self.start_date, self.end_date, self.period):
            _iso(v)
        if bool(self.start_date) != bool(self.end_date):
            raise ValueError('start_date 与 end_date 须成对提供')
        if self.start_date and self.start_date > self.end_date:
            raise ValueError('开始日期不能晚于结束日期')
        if self.providers is not None:
            bad = [p for p in self.providers if p not in config.PROVIDERS]
            if bad:
                raise ValueError(f'未知数据源：{", ".join(bad)}')
            self.providers = list(dict.fromkeys(self.providers))
        return self


class ExploredSourceBinding(BaseModel):
    provider: Literal['tushare', 'akshare', 'wind', 'ifind', 'tdx', 'choice']
    tool: str = Field(min_length=1, max_length=80)
    columns: list[str] = Field(min_length=1)
    source_unit: str = ''
    multiplier: float = 1.0
    prompt: str | None = None
    note: str = ''


class SaveExploredFieldRequest(BaseModel):
    exploration_id: str = Field(pattern=r'^exp_[a-f0-9]{16}$')
    kind: Literal['metric', 'records'] = 'metric'
    dataset: Literal['daily_prices', 'valuations', 'financials', 'basic_info', 'cb_basic', 'cb_daily', 'cb_clauses'] | None = None
    field_id: str | None = None
    label: str = Field(min_length=1, max_length=100)
    unit: str = Field(default='未注明', max_length=40)
    description: str = Field(default='', max_length=400)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    sources: list[ExploredSourceBinding] = Field(min_length=1)

    @model_validator(mode='after')
    def valid(self):
        if self.kind == 'metric':
            if not self.dataset:
                raise ValueError('保存数值指标 (kind=metric) 必须指定 dataset')
            if not self.field_id or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', self.field_id):
                raise ValueError('field_id 须为小写英文字母开头，仅含小写字母、数字和下划线')
            if self.field_id in {'symbol', 'date'}:
                raise ValueError('field_id 不能为保留字 symbol 或 date')
        if not self.description:
            self.description = f'{self.label}（通过 explore_field 探索入库）'
        return self


def _query_mode(req: ExploreFieldRequest) -> Literal['single', 'batch', 'market']:
    if req.symbol:
        return 'single'
    if req.symbols:
        return 'batch'
    return 'market'


def _resolve_route(c: dict, req: ExploreFieldRequest) -> list[str]:
    if req.providers:
        return list(req.providers)
    mode = _query_mode(req)
    if mode == 'single':
        base = list(c.get('global_route') or config.PROVIDERS)
    else:
        base = list((c.get('market') or {}).get('panel_route') or PANEL_DEFAULT_ROUTE)
    for p in config.PROVIDERS:
        if p not in base:
            base.append(p)
    return base


def _build_mcp_prompt(req: ExploreFieldRequest, provider: str = '') -> str:
    market = '可转债' if req.scope == 'convertible_bond' else 'A股'
    mode = _query_mode(req)
    def fmt_sym(s: str) -> str:
        # TDX NLP tools expect 6-digit codes without .SH/.SZ/.BJ suffix
        return s.split('.')[0] if provider == 'tdx' else s
    if mode == 'single':
        entity = f'{market} {fmt_sym(req.symbol)}'
    elif mode == 'batch':
        entity = f'{market} {"、".join(fmt_sym(s) for s in req.symbols[:20])}' + (f' 等{len(req.symbols)}只' if len(req.symbols) > 20 else '')
    else:
        entity = f'全部{market}'
    if req.period:
        when = f'报告期 {req.period}'
    elif req.start_date and req.end_date:
        when = f'{req.start_date}至{req.end_date}'
    elif req.date:
        when = f'{req.date}'
    else:
        when = '最新可用日期或最新报告期'
    return (
        f'查询{entity}，{when}的{req.request}。'
        '返回结构化数据表，每行包含证券代码、实际日期/报告期及指标数值与单位，保留原始口径说明；'
        '无法提供请留空，不要编造。'
    )


def _pick_mcp_tool(provider: str, req: ExploreFieldRequest, tools: list[dict]) -> dict | None:
    market_hint = 'convertible_bond' if req.scope == 'convertible_bond' else 'equity'
    mode = _query_mode(req)
    scored = []
    for t in tools:
        if not runnable(t):
            continue
        name = t.get('name', '')
        # Never pick futures/option/summary-only tools for A-share or CB indicator queries
        if re.search(r'futures|option|summary|macro|edb', name, re.I):
            continue
        score = relevance(req.request, t)
        if score <= 0:
            corpus = (name + ' ' + t.get('description', '')).lower()
            if market_hint == 'convertible_bond' and any(w in corpus for w in ('bond', '债券', '转债')):
                score = 5.0
            elif any(w in corpus for w in ('fundamental', 'indicator', 'ashare_finance', 'screener', '基本面', '指标')):
                score = 2.0
            else:
                score = 0.5
        if mode != 'single':
            score = market_tool_score(score, t, market_hint, 'daily')
        elif market_hint == 'convertible_bond':
            if re.search(r'mx_(?:ashare|hk|us|fund|macro)_|tdx_indicator_select|tdx_screener', name, re.I):
                score = 0
            elif re.search(r'bond|债券|转债', name + ' ' + t.get('description', ''), re.I):
                score += 50
        elif market_hint == 'equity':
            if re.search(r'mx_(?:bond|hk|us|fund|macro)_', name, re.I):
                score = 0
            elif re.search(r'ashare|a股|股票|tdx_indicator_select|get_security_indicators', name + ' ' + t.get('description', ''), re.I):
                score += 20
        if score > 0:
            scored.append((score, t))
    scored.sort(key=lambda x: (-x[0], x[1]['name']))
    return scored[0][1] if scored else None


def _build_mcp_args(tool: dict, req: ExploreFieldRequest, provider: str = '') -> dict:
    props = tool.get('inputSchema', {}).get('properties', {})
    required = set(tool.get('inputSchema', {}).get('required', []))
    nl_key = next((k for k in ('question', 'query', 'message') if k in props), 'query')
    args = {nl_key: _build_mcp_prompt(req, provider=provider)}
    if 'rang' in props:
        args['rang'] = 'AG'
    market_val = '债券' if req.scope == 'convertible_bond' else 'A股'
    if 'market' in props:
        enum_vals = props['market'].get('enum') or []
        if enum_vals:
            if req.scope == 'convertible_bond':
                args['market'] = '债券' if '债券' in enum_vals else enum_vals[0]
            else:
                args['market'] = 'A股' if 'A股' in enum_vals else ('股票' if '股票' in enum_vals else enum_vals[0])
        else:
            args['market'] = market_val
    if 'symbol' in props and req.symbol:
        args['symbol'] = req.symbol
    if 'bdate' in props and (req.start_date or req.date or req.period):
        args['bdate'] = req.start_date or req.date or req.period
    if 'edate' in props and (req.end_date or req.date or req.period):
        args['edate'] = req.end_date or req.date or req.period
    missing = required - set(args)
    if missing:
        raise ValueError(f'工具 {tool["name"]} 存在未适配必填参数：{", ".join(sorted(missing))}')
    return args


def _summarize_rows(rows: list[dict], units_map: dict | None = None, preview_limit: int = 10) -> dict:
    columns = []
    for r in rows[:200]:
        for k in r.keys():
            if k not in columns:
                columns.append(k)
    return {
        'row_count': len(rows),
        'columns': columns,
        'units': units_map or {},
        'preview': rows[:preview_limit],
    }


def _detect_market_truncation(req: ExploreFieldRequest, desc: dict, cov_status: str) -> tuple[bool, str | None]:
    if cov_status == 'partial':
        return True, '【警告】上游明确提示数据已截断，切勿当作完整样本。'
    if _query_mode(req) != 'market' or desc['row_count'] == 0:
        return False, None
    uniq = desc.get('unique_symbols') or 0
    row_count = desc.get('row_count') or 0
    if row_count == 100 or (req.scope == 'stock' and 0 < uniq < 200) or (req.scope == 'convertible_bond' and 0 < uniq < 150):
        return True, f'【警告】全市场请求仅返回 {uniq} 只证券（{row_count} 行），疑似被上游静默截断，切勿当作全市场完整样本！'
    return False, None


async def _probe_mcp_provider(provider: str, req: ExploreFieldRequest, p: dict, timeout: int, exp_id: str) -> tuple[dict, dict]:
    tick = time.monotonic()
    tools, err = await ensure_tools(provider)
    if not tools:
        return ({
            'provider': provider, 'kind': 'mcp', 'status': 'error',
            'error': err or '未获取到上游工具目录', 'elapsed_seconds': round(time.monotonic() - tick, 2),
        }, {'provider': provider, 'error': err})
    available_tools = [t['name'] for t in tools if runnable(t)]
    override = (req.tool_overrides or {}).get(provider)
    if override:
        tool = next((t for t in tools if t['name'] == override and runnable(t)), None)
    else:
        tool = _pick_mcp_tool(provider, req, tools)
    if not tool:
        return ({
            'provider': provider, 'kind': 'mcp', 'status': 'unsupported',
            'available_tools': available_tools[:10],
            'error': f'未找到匹配的只读自然语言工具（override={override}）' if override else '未找到匹配的只读自然语言工具',
            'elapsed_seconds': round(time.monotonic() - tick, 2),
        }, {'provider': provider, 'status': 'unsupported'})
    try:
        args = _build_mcp_args(tool, req, provider=provider)
        raw = await core.invoke(provider, {'generic_call': {'tool': tool['name'], 'arguments': args}}, timeout)
        extracted = tables(raw)
        rows = [r for r, _ in extracted]
        units = extracted[0][1] if extracted else {}
        text_preview = ''
        if not rows and isinstance(raw, dict):
            parts = [c.get('text', '') for c in (raw.get('content') or []) if isinstance(c, dict) and c.get('type') == 'text']
            text_preview = '\n'.join(parts)[:600]
            if re.search(r'HTTP\s*error\s*:\s*[45]\d\d|请求过于频繁|用量已耗尽|积分已用完', text_preview, re.I):
                core.record(provider, False, text_preview[:160])
                return ({
                    'provider': provider, 'kind': 'mcp', 'status': 'error',
                    'tool': tool['name'], 'available_tools': available_tools[:10],
                    'error': text_preview[:240], 'elapsed_seconds': round(time.monotonic() - tick, 2),
                }, {'provider': provider, 'tool': tool['name'], 'arguments': args, 'raw': raw})
        from .coverage import coverage
        cov = coverage(raw, rows)
        core.record(provider, bool(rows))
        summary = _summarize_rows(rows, units, preview_limit=10)
        csv_path = None
        desc = describe_rows(rows, summary['columns'])
        if rows:
            csv_path, desc = write_csv_and_describe(rows, config.ROOT / 'local/explorations' / f'{exp_id}_{provider}.csv', summary['columns'])
        truncated, trunc_warn = _detect_market_truncation(req, desc, cov.get('status', ''))
        entry = {
            'provider': provider,
            'kind': 'mcp',
            'status': 'ok' if rows else 'empty',
            'tool': tool['name'],
            'available_tools': available_tools[:10],
            'elapsed_seconds': round(time.monotonic() - tick, 2),
            **summary,
            'describe': desc,
            'csv_path': csv_path,
            'truncated': truncated,
        }
        if trunc_warn:
            entry['truncation_warning'] = trunc_warn
        if len(rows) > len(summary['preview']):
            entry['preview_notice'] = preview_notice(len(summary['preview']), len(rows), csv_path)
        if text_preview:
            entry['text_preview'] = text_preview
        return entry, {'provider': provider, 'tool': tool['name'], 'arguments': args, 'raw': raw}
    except Exception as exc:
        msg = config.redact(str(exc) or '请求超时')
        core.record(provider, False, msg)
        return ({
            'provider': provider, 'kind': 'mcp', 'status': 'error',
            'tool': tool['name'], 'available_tools': available_tools[:10],
            'error': msg, 'elapsed_seconds': round(time.monotonic() - tick, 2),
        }, {'provider': provider, 'tool': tool['name'], 'error': msg})


async def _probe_sdk_provider(provider: str, req: ExploreFieldRequest, p: dict, timeout: int, exp_id: str) -> tuple[dict, dict]:
    tick = time.monotonic()
    override = (req.tool_overrides or {}).get(provider)
    candidates = search_sdk_candidates(provider, req.request, scope=req.scope or 'stock', limit=5)
    if override:
        catalog_all = load_tushare_catalog() if provider == 'tushare' else load_akshare_catalog()
        forced = next((c for c in catalog_all if c['api_name'] == override), None)
        if forced:
            candidates = [{**forced, 'score': 999.0}, *[c for c in candidates if c['api_name'] != override]]
        else:
            candidates = [{'api_name': override, 'title': override, 'params': ['ts_code', 'trade_date', 'start_date', 'end_date', 'period'], 'fields': [], 'score': 999.0}, *candidates]
    compact_candidates = [
        {
            'api_name': c['api_name'],
            'title': c.get('title', ''),
            'params': c.get('params', []),
            'fields': (c.get('fields') or [])[:16],
            'signature': c.get('signature'),
            'score': c.get('score'),
            'fallback_hint': bool(c.get('fallback_hint')),
        }
        for c in candidates[:5]
    ]
    if not candidates:
        return ({
            'provider': provider, 'kind': 'sdk', 'status': 'no_catalog_match',
            'candidate_apis': [], 'elapsed_seconds': round(time.monotonic() - tick, 2),
            'note': f'{provider} 本地接口目录为空。',
        }, {'provider': provider, 'candidates': []})

    top = candidates[0]
    if top.get('fallback_hint') and not override:
        return ({
            'provider': provider, 'kind': 'sdk', 'status': 'catalog_hint_only',
            'tool': None, 'candidate_apis': compact_candidates,
            'elapsed_seconds': round(time.monotonic() - tick, 2),
            'note': f'未直接命中关键词，已返回 {provider} 同市场常用候选接口；可使用 tool_overrides={{"{provider}": "<api_name>"}} 指定调用。',
        }, {'provider': provider, 'candidates': compact_candidates})

    req_dict = req.model_dump()
    kwargs = build_tushare_params(top, req_dict) if provider == 'tushare' else build_akshare_params(top, req_dict)
    if kwargs is None:
        return ({
            'provider': provider, 'kind': 'sdk', 'status': 'catalog_hint_only',
            'tool': top['api_name'], 'candidate_apis': compact_candidates,
            'elapsed_seconds': round(time.monotonic() - tick, 2),
            'note': f'已匹配候选接口 {top["api_name"]}，但参数无法自动映射，未发起实时调用；Agent 可参考 candidate_apis。',
        }, {'provider': provider, 'candidates': compact_candidates})

    try:
        res = await core.invoke(provider, {'sdk_probe': {'api_name': top['api_name'], 'kwargs': kwargs}}, timeout)
        rows = res.get('rows') or []
        core.record(provider, True)
        summary = _summarize_rows(rows, preview_limit=10)
        csv_path = None
        desc = describe_rows(rows, summary['columns'])
        if rows:
            csv_path, desc = write_csv_and_describe(rows, config.ROOT / 'local/explorations' / f'{exp_id}_{provider}.csv', summary['columns'])
        entry = {
            'provider': provider,
            'kind': 'sdk',
            'status': 'ok' if rows else 'empty',
            'tool': top['api_name'],
            'sdk_kwargs': kwargs,
            'candidate_apis': compact_candidates,
            'elapsed_seconds': round(time.monotonic() - tick, 2),
            **summary,
            'describe': desc,
            'csv_path': csv_path,
            'truncated': False,
        }
        if len(rows) > len(summary['preview']):
            entry['preview_notice'] = preview_notice(len(summary['preview']), len(rows), csv_path)
        return entry, {'provider': provider, 'api_name': top['api_name'], 'kwargs': kwargs, 'row_count': len(rows), 'csv_path': csv_path}
    except Exception as exc:
        msg = config.redact(str(exc) or 'SDK 调用失败')
        core.record(provider, False, msg)
        return ({
            'provider': provider, 'kind': 'sdk', 'status': 'error',
            'tool': top['api_name'], 'sdk_kwargs': kwargs, 'candidate_apis': compact_candidates,
            'error': msg, 'elapsed_seconds': round(time.monotonic() - tick, 2),
        }, {'provider': provider, 'api_name': top['api_name'], 'kwargs': kwargs, 'error': msg})


async def explore_field(request: ExploreFieldRequest) -> dict:
    """Direct, non-blocking multi-provider probe. Never re-checks or short-circuits on dictionary."""
    request = ExploreFieldRequest.model_validate(request.model_dump())
    c = config.load()
    start = time.monotonic()
    exp_id = 'exp_' + uuid.uuid4().hex[:16]
    mode = _query_mode(request)
    route = _resolve_route(c, request)

    sem = asyncio.Semaphore(4)

    async def probe_one(name: str):
        p = c['providers'][name]
        if not core.available(name, p):
            return ({'provider': name, 'kind': 'mcp' if name in config.MCP_PROVIDERS else 'sdk',
                     'status': 'skipped', 'error': '来源未启用或未配置'},
                    {'provider': name, 'status': 'skipped'})
        timeout = min(request.per_provider_timeout, int(p.get('timeout_seconds', 60)))
        async with sem:
            if name in config.MCP_PROVIDERS:
                return await _probe_mcp_provider(name, request, p, timeout, exp_id)
            return await _probe_sdk_provider(name, request, p, timeout, exp_id)

    results = []
    raws = []
    if request.stop_on_first:
        for name in route:
            entry, raw = await probe_one(name)
            results.append(entry)
            raws.append(raw)
            if entry.get('status') == 'ok':
                break
    else:
        pairs = await asyncio.gather(*(probe_one(name) for name in route))
        for entry, raw in pairs:
            results.append(entry)
            raws.append(raw)

    ok_providers = [r['provider'] for r in results if r.get('status') == 'ok']
    folder = config.ROOT / 'local/explorations'
    folder.mkdir(parents=True, exist_ok=True)
    raw_path = folder / f'{exp_id}.raw.json'
    data_path = folder / f'{exp_id}.json'
    answer = {
        'exploration_id': exp_id,
        'status': 'success' if ok_providers else 'no_data',
        'mode': mode,
        'scope': request.scope,
        'request': request.model_dump(),
        'route': route,
        'ok_providers': ok_providers,
        'results': results,
        'elapsed_seconds': round(time.monotonic() - start, 2),
        'fetched_at': core.now(),
        'data_path': str(data_path),
        'raw_path': str(raw_path),
        'next_step': '请横向比对各来源的列名、单位、口径与数值；确认采纳后调用 save_explored_field 一次将一家或多家来源入库。',
    }
    config.write_json(raw_path, config.redact(raws))
    config.write_json(data_path, config.redact(answer))
    return answer


def _verify_evidence(exploration_id: str, sources: list[ExploredSourceBinding]) -> dict:
    path = config.ROOT / 'local/explorations' / f'{exploration_id}.json'
    if not path.exists():
        raise ValueError(f'找不到探索证据 {exploration_id}，请先调用 explore_field')
    evidence = json.loads(path.read_text())
    by_provider = {r['provider']: r for r in evidence.get('results') or []}
    for src in sources:
        rec = by_provider.get(src.provider)
        if not rec:
            raise ValueError(f'探索证据 {exploration_id} 中不包含来源 {src.provider}')
        valid_tools = {rec.get('tool')} | {c.get('api_name') for c in (rec.get('candidate_apis') or [])}
        if src.tool not in valid_tools:
            raise ValueError(f'来源 {src.provider} 在探索证据中未使用工具/接口 {src.tool}')
        if rec.get('status') not in {'ok', 'catalog_hint_only'}:
            raise ValueError(f'来源 {src.provider} 在本次探索状态为 {rec.get("status")}，不能入库')
    return evidence


def save_explored_field(request: SaveExploredFieldRequest) -> dict:
    """Persist one or multiple verified provider bindings from an explore_field run."""
    request = SaveExploredFieldRequest.model_validate(request.model_dump())
    evidence = _verify_evidence(request.exploration_id, request.sources)
    base = dictionary.validate(json.loads(dictionary.PATH.read_text()))

    with config.LOCK:
        user = dictionary._read_user()
        if request.kind == 'metric':
            ds = request.dataset
            field_id = request.field_id
            if field_id in base['datasets'][ds]['fields']:
                raise ValueError(f'字段 {ds}.{field_id} 已存在于内置字典，不允许覆盖')
            recipes = user.setdefault('recipes', {})
            sources_map = {p: [] for p in config.PROVIDERS}
            for src in request.sources:
                rid = f'exp_{src.provider}_{hashlib.sha256(f"{ds}|{field_id}|{src.tool}".encode()).hexdigest()[:12]}'
                if src.provider in config.MCP_PROVIDERS:
                    recipes[rid] = {
                        'provider': src.provider,
                        'dataset': ds,
                        'kind': 'mcp_nl',
                        'method': src.tool,
                        'template': dictionary.EXPLORATION_TEMPLATE,
                        'qualifier': dictionary.EXPLORATION_QUALIFIER,
                        'explored': True,
                    }
                else:
                    recipes[rid] = {
                        'provider': src.provider,
                        'dataset': ds,
                        'kind': 'sdk',
                        'method': src.tool,
                        'params': {},
                        'explored': True,
                    }
                sources_map[src.provider].append({
                    'recipe': rid,
                    'columns': list(src.columns),
                    'multiplier': src.multiplier,
                    'source_unit': src.source_unit or request.unit,
                    'prompt': src.prompt or request.label,
                    'note': src.note or f'{src.provider} ({src.tool}) 探索入库',
                })
            field_def = {
                'label': request.label,
                'unit': request.unit,
                'aliases': list(dict.fromkeys([request.label, *request.aliases])),
                'description': request.description,
                'sources': sources_map,
                'unavailable': {},
                'default_selected': False,
                'evidence_id': request.exploration_id,
            }
            user.setdefault('datasets', {}).setdefault(ds, {}).setdefault('fields', {})[field_id] = field_def
            dictionary._merge(base, user)
            config.write_json(dictionary.user_dictionary_path(), user)
            dictionary.reload_dictionary()
            return {
                'saved': True,
                'kind': 'metric',
                'dataset': ds,
                'field_id': field_id,
                'providers': [s.provider for s in request.sources],
                'evidence_id': request.exploration_id,
            }

        # kind == 'records'
        identity = '|'.join([request.label, evidence.get('scope', 'stock'), *(f'{s.provider}:{s.tool}' for s in request.sources)])
        query_id = 'data_' + hashlib.sha256(identity.encode()).hexdigest()[:16]
        primary = request.sources[0]
        market_hint = 'convertible_bond' if evidence.get('scope') == 'convertible_bond' else 'equity'
        mode = evidence.get('mode', 'single')
        definition = {
            'id': query_id,
            'label': request.label,
            'request': request.label,
            'provider': primary.provider,
            'tool': primary.tool,
            'providers': [{'provider': s.provider, 'tool': s.tool, 'columns': s.columns, 'note': s.note} for s in request.sources],
            'kind': 'mcp_records',
            'columns': {col: {'label': col, 'unit': request.unit} for col in primary.columns},
            'created_at': core.now(),
            'evidence_id': request.exploration_id,
            'schema_version': 2 if mode == 'market' else 1,
            'market_hint': market_hint,
            'query_scope': 'market' if mode == 'market' else 'security',
            'market_kind': 'daily' if mode == 'market' else None,
        }
        user.setdefault('data_queries', {})[query_id] = definition
        dictionary._merge(base, user)
        config.write_json(dictionary.user_dictionary_path(), user)
        dictionary.reload_dictionary()
        return {
            'saved': True,
            'kind': 'records',
            'query_id': query_id,
            'providers': [s.provider for s in request.sources],
            'evidence_id': request.exploration_id,
        }
