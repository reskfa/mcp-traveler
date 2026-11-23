"""Account credit, quota, and session synchronization for configured data providers.

Supports:
- AKShare: built-in free (no login, no quota).
- Tushare: SDK-first (`pro.query('user')`) with optional web session fallback.
- Wind: one-time official web login -> saved `wind.sessionid` header -> fast httpx refresh.
- iFinD: one-time official web login -> saved web cookies -> fast httpx refresh.
- TDX: one-time official mall login -> persistent browser profile -> headless TQL response interception
  (`PMT.get_current_increment` & `PMT.calRecentConsumeInfo`, never reading/storing `secretKey`).
- Choice (East Money Miaoxiang): one-time official web login -> persistent browser profile with
  best-effort points extraction + manual fallback.

All saved web sessions stay strictly local under `local/account_sessions/` (0o600) and
`local/browser_sessions/` (0o700) and never touch the user's everyday browser.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import config

LOGIN_TIMEOUT_SECONDS = 180
HEADLESS_REFRESH_TIMEOUT_SECONDS = 25

PROVIDER_ACCOUNT_META: dict[str, dict[str, Any]] = {
    'tushare': {
        'account_mode': 'permission_points',
        'unit_label': '权限积分',
        'supports_sdk_refresh': True,
        'supports_web_login': True,
        'supports_browser_popup': True,
        'supports_session_import': True,
        'login_url': 'https://tushare.pro/login',
        'overview_url': 'https://tushare.pro/user/token',
        'import_target': 'user/token 页面请求（或复制 Cookie）',
        'auth_note': '优先用已配置 Token 直连查询积分；若接口受限，可在日常浏览器登录官网后粘贴 Copy as cURL / Cookie 导入。',
    },
    'akshare': {
        'account_mode': 'free',
        'unit_label': '开源免费',
        'supports_sdk_refresh': False,
        'supports_web_login': False,
        'supports_browser_popup': False,
        'supports_session_import': False,
        'login_url': None,
        'overview_url': 'https://akshare.akfamily.xyz/',
        'import_target': None,
        'auth_note': '开源免费数据源，无需登录，无积分消耗。',
    },
    'wind': {
        'account_mode': 'points',
        'unit_label': '积分',
        'supports_sdk_refresh': False,
        'supports_web_login': True,
        'supports_browser_popup': True,
        'supports_session_import': True,
        'login_url': 'https://market.windalice.com/#/signin',
        'overview_url': 'https://market.windalice.com/#/user/overview',
        'import_target': 'balance/current 请求（右键 Copy as cURL）',
        'auth_note': '在日常浏览器登录 Wind AliceMarket 官网后，粘贴 balance/current 的 Copy as cURL 即可长期一键刷新积分；也支持弹窗登录。',
    },
    'ifind': {
        'account_mode': 'quota',
        'unit_label': '次',
        'supports_sdk_refresh': False,
        'supports_web_login': True,
        'supports_browser_popup': False,
        'supports_session_import': True,
        'login_url': 'https://mcp.51ifind.com/',
        'overview_url': 'https://mcp.51ifind.com/',
        'import_target': 'get_user_info 请求（右键 Copy as cURL 或复制 Cookie）',
        'auth_note': '请在日常浏览器登录 iFinD MCP 官网，复制 get_user_info 的 Copy as cURL 粘贴导入，之后即可一键自动刷新套餐与剩余次数。',
    },
    'tdx': {
        'account_mode': 'points',
        'unit_label': 'TDCP 积分',
        'supports_sdk_refresh': False,
        'supports_web_login': True,
        'supports_browser_popup': True,
        'supports_session_import': False,
        'login_url': 'https://vip.tdx.com.cn/site/app/pc-mall/main.html#/aiKey',
        'overview_url': 'https://vip.tdx.com.cn/site/app/pc-mall/main.html#/aiKey',
        'import_target': None,
        'auth_note': '登录一次通达信会员商城后，自动读取 AI 积分池余额与用量（不读取密钥列表）。',
    },
    'choice': {
        'account_mode': 'points',
        'unit_label': '积分',
        'supports_sdk_refresh': False,
        'supports_web_login': True,
        'supports_browser_popup': False,
        'supports_session_import': True,
        'login_url': 'https://choice.eastmoney.com/mcp/case',
        'overview_url': 'https://choice.eastmoney.com/mcp/case',
        'import_target': '点击「当前用户积分查询」后触发的 summary 请求（右键 Copy as cURL）',
        'auth_note': '请在日常浏览器打开 choice.eastmoney.com/mcp/case 登录，点击「当前用户积分查询」，复制 summary 请求的 Copy as cURL 粘贴导入，之后即可一键刷新真实积分。',
    },
}

_LOGIN_TASKS: dict[str, asyncio.Task] = {}
_LOGIN_STATES: dict[str, dict[str, Any]] = {}
_LOGIN_CONFIRM_EVENTS: dict[str, asyncio.Event] = {}


class SessionExpiredError(RuntimeError):
    """Raised when a saved vendor web session is rejected (e.g. HTTP 401)."""


class PlaywrightUnavailableError(RuntimeError):
    """Raised when playwright is not installed in the current environment."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
        return True
    except ImportError:
        return False


def session_path(name: str) -> Path:
    return config.account_sessions_dir() / f'{name}.json'


def profile_dir(name: str) -> Path:
    return config.browser_profiles_dir() / name


def has_saved_session(name: str) -> bool:
    return session_path(name).exists()


def load_session(name: str) -> dict[str, Any] | None:
    path = session_path(name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_session(name: str, data: dict[str, Any]) -> None:
    folder = config.account_sessions_dir()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    payload = {**data, 'saved_at': _now()}
    config.write_json(session_path(name), payload)
    try:
        os.chmod(session_path(name), 0o600)
    except OSError:
        pass


def clear_session(name: str) -> dict[str, Any]:
    if name not in config.PROVIDERS:
        raise ValueError(f'未知数据源：{name}')
    cancel_login(name)
    spath = session_path(name)
    if spath.exists():
        spath.unlink()
    pdir = profile_dir(name)
    if pdir.exists():
        shutil.rmtree(pdir, ignore_errors=True)
    meta = PROVIDER_ACCOUNT_META[name]
    patch = {
        'session_state': 'not_needed' if not meta['supports_web_login'] else 'unbound',
        'last_sync_error': None,
    }
    config.update_status(name, patch)
    return enrich_status(config.status())


def _safe_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        cleaned = str(value).replace(',', '').strip()
        num = float(cleaned)
        return num if num == num else None
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    num = _safe_float(value)
    return int(round(num)) if num is not None else None


def _normalize_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    m = re.search(r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})', text)
    if m:
        return f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    m8 = re.fullmatch(r'(\d{4})(\d{2})(\d{2})', text)
    if m8:
        return f'{m8.group(1)}-{m8.group(2)}-{m8.group(3)}'
    return text[:20]


def _mask_account_label(label: Any) -> str | None:
    if not label:
        return None
    text = str(label).strip()
    if not text:
        return None
    if re.fullmatch(r'1\d{10}', text):
        return text[:3] + '****' + text[-4:]
    m = re.fullmatch(r'(mo_)(\d{6,})', text)
    if m:
        digits = m.group(2)
        return m.group(1) + digits[:2] + '****' + digits[-2:]
    return text[:40]


# ---------------------------------------------------------------------------
# Pure provider response parsers (easy to unit test)
# ---------------------------------------------------------------------------

def parse_wind_balance(balance_payload: dict[str, Any], info_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(balance_payload, dict):
        raise ValueError('Wind 余额返回格式无效')
    err_code = balance_payload.get('errorCode')
    code = balance_payload.get('code')
    msg = str(balance_payload.get('message') or balance_payload.get('errorMessage') or balance_payload.get('msg') or '')
    if err_code == 10010 or code == 401 or 'SessionId' in msg or '未登录' in msg or '过期' in msg:
        raise SessionExpiredError(msg or 'Wind 官网会话已过期，请重新登录')
    if code not in (0, '0', None) and not balance_payload.get('data'):
        raise RuntimeError(msg or f'Wind 余额接口返回异常（code={code}）')

    data = balance_payload.get('data') or {}
    available = _safe_float(data.get('availableTotal'))
    if available is None:
        available = _safe_float(data.get('balance'))
    recharge = _safe_float(data.get('rechargeTotal'))
    subscribe = _safe_float(data.get('subscribeTotal'))
    recharge_pack = _safe_float(data.get('rechargePackTotal'))
    gift = _safe_float(data.get('giftTotal'))

    breakdown: list[dict[str, Any]] = []
    for label, val in (
        ('充值积分', recharge),
        ('订阅积分', subscribe),
        ('加油包', recharge_pack),
        ('赠送积分', gift),
    ):
        if val is not None and val > 0:
            breakdown.append({'name': label, 'remaining': val, 'unit': '积分'})

    expires_candidates: list[str] = []
    segments = data.get('segments')
    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_name = str(seg.get('name') or seg.get('typeName') or seg.get('type') or '积分包')
            seg_bal = _safe_float(seg.get('balance') or seg.get('available') or seg.get('remaining'))
            seg_exp = _normalize_date(seg.get('expireTime') or seg.get('validTo') or seg.get('expiresAt'))
            if seg_exp:
                expires_candidates.append(seg_exp)
            if seg_bal is not None:
                breakdown.append({'name': seg_name, 'remaining': seg_bal, 'expires_at': seg_exp, 'unit': '积分'})

    account_label = None
    plan_name = None
    if isinstance(info_payload, dict) and info_payload.get('code') in (0, '0'):
        info = info_payload.get('data') or {}
        if isinstance(info, dict):
            account_label = _mask_account_label(info.get('userName') or info.get('account') or info.get('mobile'))
            plan_name = info.get('planName') or info.get('userType') or info.get('levelName')

    return {
        'account_mode': 'points',
        'session_state': 'active',
        'source': 'official_web',
        'remaining_credits': available,
        'total_credits': None,
        'used_credits': None,
        'account_label': account_label,
        'plan_name': plan_name,
        'expires_at': min(expires_candidates) if expires_candidates else None,
        'breakdown': breakdown,
        'last_sync_error': None,
        'account_updated_at': _now(),
    }


def parse_ifind_user_info(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError('iFinD 账户返回格式无效')
    err_code = payload.get('errcode', payload.get('errorcode'))
    err_msg = str(payload.get('errmsg') or payload.get('message') or '')
    if err_code in (-1010, 401, -401) or 'loggout' in err_msg.lower() or 'logout' in err_msg.lower() or '未登录' in err_msg:
        raise SessionExpiredError(err_msg or 'iFinD 官网会话已过期，请重新登录')
    if err_code not in (0, '0'):
        raise RuntimeError(err_msg or f'iFinD 账户接口返回异常（errcode={err_code}）')

    data = payload.get('data') or {}
    account_label = _mask_account_label(data.get('account') or data.get('userName'))
    plan_name = data.get('userTypeName') or data.get('userType')
    product_expire = _normalize_date(data.get('productExpire'))

    usages = data.get('userMcpUsages') or {}
    plans = usages.get('plans') if isinstance(usages, dict) else []
    if not isinstance(plans, list):
        plans = []

    breakdown: list[dict[str, Any]] = []
    valid_dates: list[str] = []

    for idx, plan in enumerate(plans):
        if not isinstance(plan, dict):
            continue
        p_name = str(plan.get('planName') or plan.get('planType') or f'套餐 {idx + 1}')
        used = _safe_int(plan.get('usage'))
        limit = _safe_int(plan.get('limit'))
        remaining = max(limit - used, 0) if (limit is not None and used is not None) else None
        valid_to = _normalize_date(plan.get('validTo'))
        if valid_to:
            valid_dates.append(valid_to)
        period = plan.get('usagePeriod') if isinstance(plan.get('usagePeriod'), dict) else {}
        period_str = None
        if period.get('start') or period.get('end'):
            period_str = f"{_normalize_date(period.get('start')) or ''} ~ {_normalize_date(period.get('end')) or ''}".strip(' ~')
        breakdown.append({
            'name': p_name,
            'used': used,
            'limit': limit,
            'remaining': remaining,
            'expires_at': valid_to,
            'period': period_str,
            'unit': '次',
        })

    addon_groups = usages.get('addonGroups') if isinstance(usages, dict) else []
    if isinstance(addon_groups, list):
        for group in addon_groups:
            if not isinstance(group, dict):
                continue
            g_name = str(group.get('name') or group.get('groupName') or '加油包')
            g_used = _safe_int(group.get('usage'))
            g_limit = _safe_int(group.get('limit'))
            g_rem = max(g_limit - g_used, 0) if (g_limit is not None and g_used is not None) else _safe_int(group.get('remaining'))
            if g_limit is not None or g_rem is not None:
                breakdown.append({
                    'name': g_name,
                    'used': g_used,
                    'limit': g_limit,
                    'remaining': g_rem,
                    'expires_at': _normalize_date(group.get('validTo')),
                    'unit': '次',
                })

    # Aggregate active (non-exhausted) plans for headline quota so an expired/exhausted trial plan doesn't hide paid quota.
    countable = [item for item in breakdown if item.get('remaining') is not None]
    active_items = [item for item in countable if (item.get('remaining') or 0) > 0]
    summary_items = active_items or countable
    primary_remaining = sum(item['remaining'] for item in countable) if countable else None
    primary_total = sum(item['limit'] for item in summary_items if item.get('limit') is not None) if summary_items else None
    primary_used = sum(item['used'] for item in summary_items if item.get('used') is not None) if summary_items else None

    return {
        'account_mode': 'quota',
        'session_state': 'active',
        'source': 'official_web',
        'account_label': account_label,
        'plan_name': str(plan_name) if plan_name else None,
        'total_calls': primary_total,
        'used_calls': primary_used,
        'remaining_calls': primary_remaining,
        'remaining_credits': None,
        'expires_at': product_expire or (max(valid_dates) if valid_dates else None),
        'breakdown': breakdown,
        'last_sync_error': None,
        'account_updated_at': _now(),
    }


def parse_tdx_increment(increment_payload: dict[str, Any], recent_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(increment_payload, dict):
        raise ValueError('通达信积分返回格式无效')
    err_code = increment_payload.get('ErrorCode')
    err_info = str(increment_payload.get('ErrorInfo') or increment_payload.get('msg') or '')
    if err_code in (-1, -5105, -5102) or '未登陆' in err_info or '未登录' in err_info:
        raise SessionExpiredError(err_info or '通达信会员商城会话已过期，请重新登录')
    if err_code not in (0, '0'):
        raise RuntimeError(err_info or f'通达信积分查询失败（ErrorCode={err_code}）')

    tables = increment_payload.get('tables') or []
    first_table = tables[0] if tables else []
    row = first_table[0] if isinstance(first_table, list) and first_table else (first_table if isinstance(first_table, dict) else {})
    total = _safe_float(row.get('incrementValue')) if isinstance(row, dict) else 0.0
    used = _safe_float(row.get('consumeValue')) if isinstance(row, dict) else 0.0
    total = total if total is not None else 0.0
    used = used if used is not None else 0.0
    remaining = max(total - used, 0.0)

    estimated_days = None
    recent_note = None
    if isinstance(recent_payload, dict) and recent_payload.get('ErrorCode') in (0, '0'):
        rtables = recent_payload.get('tables') or []
        rt0 = rtables[0] if rtables else []
        rrow = rt0[0] if isinstance(rt0, list) and rt0 else (rt0 if isinstance(rt0, dict) else {})
        if isinstance(rrow, dict):
            left_days = _safe_float(rrow.get('leftTime'))
            used_recent = _safe_float(rrow.get('usedTime'))
            if left_days is not None and left_days > 0:
                estimated_days = round(left_days, 1)
            if used_recent is not None:
                recent_note = f'近 10 日消耗 {used_recent:g} 积分'

    breakdown = [
        {'name': 'TDCP AI 积分池', 'used': used, 'limit': total, 'remaining': remaining, 'unit': '积分'},
    ]
    return {
        'account_mode': 'points',
        'session_state': 'active',
        'source': 'official_web',
        'total_credits': total,
        'used_credits': used,
        'remaining_credits': remaining,
        'estimated_days_left': estimated_days,
        'recent_usage_note': recent_note,
        'breakdown': breakdown,
        'last_sync_error': None,
        'account_updated_at': _now(),
    }


def parse_tdx_dom_text(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    m = re.search(
        r'账户可用积分\s*([\d,]+(?:\.\d+)?)\s*/\s*([\d,]+(?:\.\d+)?)\s*总量',
        text,
        re.S,
    )
    if not m:
        return None
    remaining = _safe_float(m.group(1))
    total = _safe_float(m.group(2))
    if remaining is None or total is None:
        return None
    used = max(total - remaining, 0.0)
    estimated_days = None
    recent_note = None
    rm = re.search(r'近\s*10\s*天消耗\s*([\d,]+(?:\.\d+)?)\s*积分.*?预计还可以使用\s*([\d,]+(?:\.\d+)?)\s*天', text, re.S)
    if rm:
        used_recent = _safe_float(rm.group(1))
        left_days = _safe_float(rm.group(2))
        if used_recent is not None:
            recent_note = f'近 10 日消耗 {used_recent:g} 积分'
        if left_days is not None and left_days > 0:
            estimated_days = round(left_days, 1)
    breakdown = [
        {'name': 'TDCP AI 积分池', 'used': used, 'limit': total, 'remaining': remaining, 'unit': '积分'},
    ]
    return {
        'account_mode': 'points',
        'session_state': 'active',
        'source': 'official_web',
        'total_credits': total,
        'used_credits': used,
        'remaining_credits': remaining,
        'estimated_days_left': estimated_days,
        'recent_usage_note': recent_note,
        'breakdown': breakdown,
        'last_sync_error': None,
        'account_updated_at': _now(),
    }


def parse_choice_authority(
    auth_payload: dict[str, Any],
    user_payload: dict[str, Any] | None = None,
    credit_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(auth_payload, dict):
        raise ValueError('妙想权限返回格式无效')
    data = auth_payload.get('data') if isinstance(auth_payload.get('data'), dict) else auth_payload
    if data.get('needLogin') is True:
        raise SessionExpiredError('东方财富妙想官网会话已过期，请重新登录')

    account_label = None
    if isinstance(user_payload, dict):
        udata = user_payload.get('data') if isinstance(user_payload.get('data'), dict) else user_payload
        account_label = _mask_account_label(udata.get('uAlias') or udata.get('uName') or udata.get('maskedMobile'))

    total_calls = _safe_int(data.get('todayTotalNum'))
    used_calls = _safe_int(data.get('todayUsedNum'))
    remaining_calls = max(total_calls - used_calls, 0) if (total_calls is not None and used_calls is not None) else None

    remaining_credits = None
    for payload in credit_payloads or []:
        if not isinstance(payload, dict):
            continue
        blob = json.dumps(payload, ensure_ascii=False)
        m = re.search(r'"(?:remainPoints|remainingPoints|leftPoints|availablePoints|mxCredit|points|balance|credits)"\s*:\s*"?(\d+(?:\.\d+)?)"?', blob)
        if m:
            remaining_credits = _safe_float(m.group(1))
            if remaining_credits is not None:
                break

    breakdown: list[dict[str, Any]] = []
    if total_calls is not None:
        breakdown.append({
            'name': '今日妙想 AI 额度',
            'used': used_calls,
            'limit': total_calls,
            'remaining': remaining_calls,
            'period': '当日有效',
            'unit': '次',
        })
    if remaining_credits is not None:
        breakdown.append({'name': '妙想积分余额', 'remaining': remaining_credits, 'unit': '积分'})

    return {
        'account_mode': 'points' if remaining_credits is not None else 'quota',
        'session_state': 'active',
        'source': 'official_web',
        'account_label': account_label,
        'plan_name': '妙想 AI（今日额度）' if total_calls is not None else '妙想 AI',
        'total_calls': total_calls,
        'used_calls': used_calls,
        'remaining_calls': remaining_calls,
        'remaining_credits': remaining_credits,
        'breakdown': breakdown,
        'last_sync_error': None,
        'account_updated_at': _now(),
    }


def parse_choice_points_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError('妙想积分返回格式无效')
    msg = str(payload.get('message') or payload.get('msg') or payload.get('errmsg') or '')
    code = payload.get('code', payload.get('status'))
    if code in (401, -401, 10001) or '未登录' in msg or '过期' in msg or 'login' in msg.lower():
        raise SessionExpiredError(msg or '妙想官网会话已过期，请重新导入')

    # First-class parser for choice.eastmoney.com/mcp/case `summary` response
    data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    if isinstance(data, dict) and 'remainingCredit' in data:
        remaining = _safe_float(data.get('remainingCredit'))
        monthly_added = _safe_float(data.get('monthlyAdded'))
        monthly_purchased = _safe_float(data.get('monthlyPurchased'))
        monthly_other = _safe_float(data.get('monthlyOtherAdded'))
        monthly_consumed = _safe_float(data.get('monthlyConsumed'))
        monthly_exec = _safe_float(data.get('monthlyExecutionConsumed'))
        monthly_expired = _safe_float(data.get('monthlyExpired'))
        today_consumed = _safe_float(data.get('todayConsumed'))
        today_exec = _safe_float(data.get('todayExecutionConsumed'))
        today_expired = _safe_float(data.get('todayExpired'))

        notes = []
        if today_consumed is not None:
            notes.append(f'今日消耗 {today_consumed:g}')
        if monthly_consumed is not None:
            notes.append(f'本月消耗 {monthly_consumed:g}')
        if monthly_added is not None:
            notes.append(f'本月新增 +{monthly_added:g}')

        breakdown: list[dict[str, Any]] = []
        if remaining is not None:
            breakdown.append({'name': '当前剩余积分', 'remaining': remaining, 'unit': '积分'})
        if monthly_added is not None:
            detail_parts = []
            if monthly_purchased is not None:
                detail_parts.append(f'购买 {monthly_purchased:g}')
            if monthly_other is not None:
                detail_parts.append(f'赠送/其他 {monthly_other:g}')
            breakdown.append({'name': '本月新增积分', 'limit': monthly_added, 'remaining': monthly_added, 'period': ' + '.join(detail_parts) or '本月', 'unit': '积分'})
        if monthly_consumed is not None:
            detail_parts = []
            if monthly_exec is not None:
                detail_parts.append(f'调用 {monthly_exec:g}')
            if monthly_expired is not None:
                detail_parts.append(f'过期 {monthly_expired:g}')
            breakdown.append({'name': '本月消耗积分', 'used': monthly_consumed, 'period': ' + '.join(detail_parts) or '本月', 'unit': '积分'})
        if today_consumed is not None:
            detail_parts = []
            if today_exec is not None:
                detail_parts.append(f'调用 {today_exec:g}')
            if today_expired is not None:
                detail_parts.append(f'过期 {today_expired:g}')
            breakdown.append({'name': '今日消耗积分', 'used': today_consumed, 'period': ' + '.join(detail_parts) or '今日', 'unit': '积分'})

        return {
            'account_mode': 'points',
            'session_state': 'active',
            'source': 'official_web',
            'remaining_credits': remaining,
            'total_credits': None,
            'used_credits': monthly_consumed,
            'remaining_calls': None,
            'total_calls': None,
            'used_calls': None,
            'plan_name': '妙想 MCP 积分',
            'recent_usage_note': ' · '.join(notes) if notes else None,
            'breakdown': breakdown,
            'last_sync_error': None,
            'account_updated_at': _now(),
        }

    # Generic fallback for other East Money points payloads
    found: dict[str, float] = {}
    def _walk(obj: Any):
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                num = _safe_float(v) if not isinstance(v, (bool, dict, list)) else None
                if num is not None:
                    if any(t in kl for t in ('remain', 'available', 'left', 'balance', 'usable', 'curr')) and any(t in kl for t in ('point', 'credit', 'score', 'integral', 'balance', 'coin', 'amount')):
                        found.setdefault('remaining', num)
                    elif kl in ('points', 'point', 'credits', 'credit', 'integral', 'balance', 'score', 'mxcredit'):
                        found.setdefault('remaining', num)
                    elif any(t in kl for t in ('total', 'sum', 'all', 'limit')) and any(t in kl for t in ('point', 'credit', 'score', 'integral')):
                        found.setdefault('total', num)
                    elif any(t in kl for t in ('used', 'consume', 'cost')) and any(t in kl for t in ('point', 'credit', 'score', 'integral')):
                        found.setdefault('used', num)
                else:
                    _walk(v)
        elif isinstance(obj, list):
            for item in obj[:20]:
                _walk(item)

    _walk(payload)
    remaining = found.get('remaining')
    total = found.get('total')
    used = found.get('used')
    if remaining is None and total is not None and used is not None:
        remaining = max(total - used, 0.0)
    if remaining is None:
        raise RuntimeError('该接口返回中未识别到积分字段，请确认复制的是妙想“积分余额”对应的网络请求 cURL。')
    if total is not None and used is None:
        used = max(total - remaining, 0.0)
    breakdown = [{'name': '妙想积分余额', 'used': used, 'limit': total, 'remaining': remaining, 'unit': '积分'}]
    return {
        'account_mode': 'points',
        'session_state': 'active',
        'source': 'official_web',
        'remaining_credits': remaining,
        'total_credits': total,
        'used_credits': used,
        'remaining_calls': None,
        'total_calls': None,
        'used_calls': None,
        'plan_name': '妙想积分',
        'breakdown': breakdown,
        'last_sync_error': None,
        'account_updated_at': _now(),
    }


def _parse_cookie_string(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    cleaned = raw.strip()
    if cleaned.lower().startswith('cookie:'):
        cleaned = cleaned.split(':', 1)[1].strip()
    for part in cleaned.split(';'):
        part = part.strip()
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        k = k.strip()
        v = v.strip()
        if k and v:
            cookies[k] = v
    return cookies


def parse_imported_session(name: str, raw_text: str) -> dict[str, Any]:
    from urllib.parse import urlsplit
    text = (raw_text or '').strip()
    if not text:
        raise ValueError('请粘贴从浏览器复制的 cURL 命令或 Cookie 字符串')
    # Normalize line continuations from Copy as cURL
    normalized = re.sub(r'\\\r?\n\s*', ' ', text)

    headers: dict[str, str] = {}
    all_headers: dict[str, str] = {}
    cookies: dict[str, str] = {}

    for m in re.finditer(r"""(?:-H|--header)\s+(['"])(.*?)\1""", normalized, re.S):
        hdr = m.group(2)
        if ':' not in hdr:
            continue
        hk, hv = hdr.split(':', 1)
        hk_clean = hk.strip()
        hk_low = hk_clean.lower()
        hv = hv.strip()
        if hk_low == 'cookie':
            cookies.update(_parse_cookie_string(hv))
        elif hk_low in ('wind.sessionid', 'windsessionid', 'x-wcms-sessionid') and hv:
            headers[hk_clean] = hv
        elif hk_low not in ('host', 'content-length', 'accept-encoding', 'connection') and hv:
            all_headers[hk_clean] = hv

    for m in re.finditer(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", normalized, re.S):
        cookies.update(_parse_cookie_string(m.group(2)))

    if name == 'choice':
        url_m = re.search(r"""--url\s+(['"]?)(https://[^\s'"]+)\1""", normalized)
        if not url_m:
            url_m = re.search(r"""curl\s+(?:--?[A-Za-z0-9-]+\s+(?:['"][^'"]*['"]|\S+)\s+)*(['"]?)(https://[^\s'"]+)\1""", normalized)
        if not url_m:
            raise ValueError('请粘贴妙想积分接口的完整 Copy as cURL（须包含 https://*.eastmoney.com 或 *.dfcfs.com 请求地址）。')
        req_url = url_m.group(2)
        host = (urlsplit(req_url).hostname or '').lower()
        if not (host.endswith('.eastmoney.com') or host.endswith('.dfcfs.com')):
            raise ValueError('仅允许导入 eastmoney.com / dfcfs.com 官方域名的只读积分接口 cURL。')
        method_m = re.search(r"""(?:-X|--request)\s+(['"]?)([A-Z]+)\1""", normalized)
        data_m = re.search(r"""(?:--data-raw|--data-binary|--data|-d)\s+(['"])(.*?)\1""", normalized, re.S)
        method = method_m.group(2) if method_m else ('POST' if data_m else 'GET')
        body = data_m.group(2) if data_m else None
        return {
            'mode': 'curl_replay',
            'url': req_url,
            'method': method,
            'headers': all_headers,
            'cookies': cookies,
            'body': body,
        }

    if not cookies and not headers:
        # Try raw key=value cookie string or single Wind sessionId
        if '=' in text and not text.lstrip().startswith('curl '):
            cookies.update(_parse_cookie_string(text))
        elif name == 'wind' and len(text.split()) == 1 and len(text) >= 8:
            headers = {'wind.sessionid': text, 'windsessionid': text}

    if name == 'wind':
        if not headers:
            raise ValueError('未在粘贴内容中识别到 Wind 的 wind.sessionid 请求头，请复制 balance/current 的 Copy as cURL。')
        if 'wind.sessionid' in headers and 'windsessionid' not in headers:
            headers['windsessionid'] = headers['wind.sessionid']
        return {'headers': headers}

    if not cookies:
        raise ValueError('未在粘贴内容中识别到有效的 Cookie，请复制浏览器 DevTools 的 Copy as cURL 或 Cookie 请求头。')
    return {'cookies': cookies}


async def import_session(name: str, raw_text: str) -> dict[str, Any]:
    if name not in config.PROVIDERS:
        raise ValueError(f'未知数据源：{name}')
    if not PROVIDER_ACCOUNT_META[name]['supports_web_login']:
        raise ValueError(f'{name} 不需要导入官网会话')
    parsed = parse_imported_session(name, raw_text)
    save_session(name, parsed)
    res = await refresh_account(name)
    res['state'] = config.redact(enrich_status(config.status()))
    return res


def parse_tushare_user(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise RuntimeError('Tushare user 接口未返回账户行')
    row = rows[0] if isinstance(rows[0], dict) else {}
    points = _safe_float(row.get('credits') or row.get('points') or row.get('score'))
    if points is None:
        raise RuntimeError('Tushare user 接口未包含积分字段')
    label = _mask_account_label(row.get('nick') or row.get('username') or row.get('id'))
    expires = _normalize_date(row.get('expire_date') or row.get('expires_at'))
    return {
        'account_mode': 'permission_points',
        'session_state': 'not_needed',
        'source': 'official_sdk',
        'permission_points': points,
        'account_label': label,
        'expires_at': expires,
        'last_sync_error': None,
        'account_updated_at': _now(),
    }


def parse_tushare_web_text(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    points = None
    for pat in (
        r'(?:当前积分|总积分|我的积分|有效积分|积分)[^\d]{0,16}(\d[\d,]*(?:\.\d+)?)',
        r'"(?:credits|points|score)"\s*:\s*"?(\d+(?:\.\d+)?)"?',
    ):
        m = re.search(pat, text)
        if m:
            points = _safe_float(m.group(1))
            if points is not None:
                break
    if points is None:
        return None
    exp_m = re.search(r'(?:到期|有效期)[^\d]{0,16}(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2})', text)
    expires = _normalize_date(exp_m.group(1)) if exp_m else None
    return {
        'account_mode': 'permission_points',
        'session_state': 'active',
        'source': 'official_web',
        'permission_points': points,
        'expires_at': expires,
        'last_sync_error': None,
        'account_updated_at': _now(),
    }


def parse_choice_web_text(text: str, json_payloads: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    remaining = None
    for payload in json_payloads or []:
        if not isinstance(payload, dict):
            continue
        blob = json.dumps(payload, ensure_ascii=False)
        m = re.search(r'"(?:remainPoints|remainingPoints|leftPoints|availablePoints|points|balance|credits)"\s*:\s*"?(\d+(?:\.\d+)?)"?', blob)
        if m:
            remaining = _safe_float(m.group(1))
            if remaining is not None:
                break
    if remaining is None and text:
        for pat in (
            r'(?:剩余积分|可用积分|我的积分|当前积分)[^\d]{0,16}(\d[\d,]*(?:\.\d+)?)',
        ):
            m = re.search(pat, text)
            if m:
                remaining = _safe_float(m.group(1))
                if remaining is not None:
                    break
    if remaining is None:
        return None
    return {
        'account_mode': 'points',
        'session_state': 'active',
        'source': 'official_web',
        'remaining_credits': remaining,
        'last_sync_error': None,
        'account_updated_at': _now(),
    }


# ---------------------------------------------------------------------------
# Fast HTTP refresh helpers
# ---------------------------------------------------------------------------

async def _refresh_tushare_sdk() -> dict[str, Any] | None:
    token = config.secrets().get(config.KEYS['tushare'], '').strip()
    if not token or '填写' in token:
        return None

    def _call():
        import tushare as ts
        pro = ts.pro_api(token)
        df = pro.query('user', fields='id,nick,credits')
        if df is None or getattr(df, 'empty', True):
            return []
        return df.to_dict(orient='records')

    rows = await asyncio.to_thread(_call)
    return parse_tushare_user(rows)


async def _refresh_wind_http(sess: dict[str, Any]) -> dict[str, Any]:
    headers = dict(sess.get('headers') or {})
    if not headers:
        raise SessionExpiredError('未找到保存的 Wind SessionId')
    headers.setdefault('Accept', 'application/json')
    headers.setdefault('User-Agent', 'Mozilla/5.0')
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        r_bal = await client.get(
            'https://market.windalice.com/Wind.AIMarket.Service/mcp-goods/api/balance/current',
            params={'usePointsPlatform': 'true'},
            headers=headers,
        )
        if r_bal.status_code == 401:
            raise SessionExpiredError('Wind 官网会话已过期（HTTP 401），请重新登录')
        bal_json = r_bal.json()
        info_json = None
        try:
            r_info = await client.get(
                'https://market.windalice.com/Wind.AIMarket.Service/mcp-config/info',
                headers=headers,
            )
            if r_info.status_code == 200:
                info_json = r_info.json()
        except Exception:
            info_json = None
    return parse_wind_balance(bal_json, info_json)


async def _refresh_ifind_http(sess: dict[str, Any]) -> dict[str, Any]:
    cookies = dict(sess.get('cookies') or {})
    if not cookies:
        raise SessionExpiredError('未找到保存的 iFinD 官网 Cookie')
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://mcp.51ifind.com/'}
    async with httpx.AsyncClient(timeout=20.0, cookies=cookies, headers=headers, follow_redirects=True) as client:
        r = await client.get('https://mcp.51ifind.com/quantapigw/ds_service_customization/mcpweb/get_user_info')
        if r.status_code == 401:
            raise SessionExpiredError('iFinD 官网会话已过期（HTTP 401），请重新登录')
        return parse_ifind_user_info(r.json())


async def _refresh_tushare_http(sess: dict[str, Any]) -> dict[str, Any]:
    cookies = dict(sess.get('cookies') or {})
    if not cookies:
        raise SessionExpiredError('未找到保存的 Tushare 官网 Cookie')
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://tushare.pro/'}
    async with httpx.AsyncClient(timeout=20.0, cookies=cookies, headers=headers, follow_redirects=True) as client:
        for url in ('https://tushare.pro/user/token', 'https://tushare.pro/user/index'):
            r = await client.get(url)
            if '/login' in str(r.url):
                raise SessionExpiredError('Tushare 官网会话已过期，请重新登录')
            parsed = parse_tushare_web_text(r.text)
            if parsed:
                return parsed
    raise SessionExpiredError('未能从 Tushare 官网个人中心解析积分，请重新登录')


# ---------------------------------------------------------------------------
# Playwright browser lifecycle & interactive login
# ---------------------------------------------------------------------------

async def _launch_persistent_context(pw: Any, name: str, headless: bool):
    pdir = profile_dir(name)
    pdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config.browser_profiles_dir(), 0o700)
        os.chmod(pdir, 0o700)
    except OSError:
        pass

    last_exc: Exception | None = None
    for channel in ('chrome', 'msedge', None):
        kwargs: dict[str, Any] = {
            'user_data_dir': str(pdir),
            'headless': headless,
            'viewport': {'width': 1220, 'height': 820},
        }
        if channel:
            kwargs['channel'] = channel
        try:
            return await pw.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f'无法启动本地浏览器：{last_exc}')


async def _capture_provider_in_browser(name: str, headless: bool, timeout_seconds: int) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise PlaywrightUnavailableError('未安装 playwright，请先执行 `pip install playwright` 后重试。') from exc

    meta = PROVIDER_ACCOUNT_META[name]
    start_url = meta['overview_url'] if headless else meta['login_url']
    captured: dict[str, Any] = {}
    choice_jsons: list[dict[str, Any]] = []
    done_event = asyncio.Event()
    confirm_event = _LOGIN_CONFIRM_EVENTS.get(name)

    async with async_playwright() as pw:
        ctx = await _launch_persistent_context(pw, name, headless=headless)
        try:
            page = await ctx.new_page()

            async def on_response(response):
                url = response.url
                try:
                    if name == 'wind':
                        req_headers = response.request.headers
                        sid_headers = {
                            k: v for k, v in req_headers.items()
                            if k.lower() in ('wind.sessionid', 'windsessionid', 'x-wcms-sessionid') and v
                        }
                        if sid_headers:
                            captured['wind_headers'] = sid_headers
                        if '/Wind.AIMarket.Service/mcp-goods/api/balance/current' in url and response.status == 200:
                            data = await response.json()
                            if isinstance(data, dict) and data.get('code') in (0, '0'):
                                captured['wind_balance'] = data
                                done_event.set()
                        elif '/Wind.AIMarket.Service/mcp-config/info' in url and response.status == 200:
                            data = await response.json()
                            if isinstance(data, dict) and data.get('code') in (0, '0'):
                                captured['wind_info'] = data

                    elif name == 'ifind':
                        if '/mcpweb/get_user_info' in url and response.status == 200:
                            data = await response.json()
                            if isinstance(data, dict) and data.get('errcode', data.get('errorcode')) in (0, '0'):
                                captured['ifind_user'] = data
                                done_event.set()

                    elif name == 'tdx':
                        # Strictly ignore PMT.selectToken so secretKey is never read or stored.
                        if 'PMT.get_current_increment' in url and response.status == 200:
                            text = await response.text()
                            data = json.loads(text)
                            if isinstance(data, dict) and data.get('ErrorCode') in (0, '0'):
                                captured['tdx_increment'] = data
                                done_event.set()
                        elif 'PMT.calRecentConsumeInfo' in url and response.status == 200:
                            text = await response.text()
                            data = json.loads(text)
                            if isinstance(data, dict) and data.get('ErrorCode') in (0, '0'):
                                captured['tdx_recent'] = data

                    elif name == 'choice':
                        ctype = response.headers.get('content-type', '')
                        if 'json' in ctype and response.status == 200:
                            if 'getAuthority' in url:
                                data = await response.json()
                                if isinstance(data, dict):
                                    captured['choice_auth'] = data
                                    d = data.get('data') if isinstance(data.get('data'), dict) else data
                                    if d.get('needLogin') is False:
                                        done_event.set()
                            elif 'getUserByToken' in url:
                                data = await response.json()
                                if isinstance(data, dict):
                                    captured['choice_user'] = data
                            elif any(k in url.lower() for k in ('point', 'credit', 'balance', 'quota', 'wallet', 'coin', 'benefit', 'mxds')):
                                data = await response.json()
                                if isinstance(data, dict):
                                    choice_jsons.append(data)
                except Exception:
                    pass

            # Listen across ALL tabs/frames in the browser context
            ctx.on('response', lambda resp: asyncio.create_task(on_response(resp)))
            await page.goto(start_url, wait_until='domcontentloaded', timeout=30000)

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if name in _LOGIN_STATES and _LOGIN_STATES[name].get('status') == 'cancelled':
                    raise asyncio.CancelledError('用户取消了登录')
                active_pages = [p for p in ctx.pages if not p.is_closed()]
                if not active_pages:
                    raise RuntimeError('登录窗口已关闭，未完成同步')

                # User clicked "我已登录，立即读取": force navigate latest tab to overview_url
                if confirm_event and confirm_event.is_set():
                    confirm_event.clear()
                    try:
                        target_page = active_pages[-1]
                        await target_page.goto(meta['overview_url'], wait_until='domcontentloaded', timeout=20000)
                        await asyncio.sleep(2.5)
                    except Exception:
                        pass

                if done_event.is_set():
                    await asyncio.sleep(1.2)
                    break

                # Fast-fail if site explicitly blocks automated browser with Nginx forbidden
                if name == 'ifind':
                    for p in active_pages:
                        try:
                            txt = await p.inner_text('body')
                            if 'Nginx forbidden' in txt or '403 Forbidden' in txt:
                                raise RuntimeError('iFinD 官网拒绝了自动化浏览器窗口（Nginx forbidden）。请在自己日常浏览器登录官网，并使用卡片中的「导入官网会话」粘贴 Copy as cURL 或 Cookie。')
                        except RuntimeError:
                            raise
                        except Exception:
                            pass

                # For Wind: once signed in (left #/signin), navigate to overview to trigger balance/current
                if name == 'wind' and not headless:
                    cur_url = active_pages[-1].url
                    if '#/signin' not in cur_url and 'windalice.com' in cur_url and '#/user/overview' not in cur_url:
                        try:
                            await active_pages[-1].goto(meta['overview_url'], wait_until='domcontentloaded', timeout=15000)
                        except Exception:
                            pass

                # For TDX: check DOM text on all tabs (TDX loads TDCP points over internal session, not plain HTTP TQLEX)
                if name == 'tdx':
                    for p in active_pages:
                        try:
                            if 'vip.tdx.com.cn' in (p.url or ''):
                                body_text = await p.inner_text('body')
                                parsed = parse_tdx_dom_text(body_text)
                                if parsed:
                                    captured['tdx_dom'] = parsed
                                    done_event.set()
                                    break
                        except Exception:
                            pass
                    if done_event.is_set():
                        break

                # For Tushare: once logged in and not on /login, inspect personal page text
                if name == 'tushare':
                    for p in active_pages:
                        cur_url = p.url or ''
                        if 'tushare.pro' in cur_url and '/login' not in cur_url:
                            if '/user/' not in cur_url:
                                try:
                                    await p.goto(meta['overview_url'], wait_until='domcontentloaded', timeout=15000)
                                except Exception:
                                    pass
                            try:
                                body_text = await p.inner_text('body')
                                parsed = parse_tushare_web_text(body_text)
                                if parsed:
                                    captured['tushare_parsed'] = parsed
                                    done_event.set()
                                    break
                            except Exception:
                                pass
                    if done_event.is_set():
                        break

                # For Choice: check visible text on all non-blank tabs in addition to getAuthority
                if name == 'choice':
                    for p in active_pages:
                        if not p.url or p.url == 'about:blank':
                            continue
                        try:
                            body_text = await p.inner_text('body')
                            parsed = parse_choice_web_text(body_text, choice_jsons)
                            if parsed:
                                captured['choice_parsed'] = parsed
                                done_event.set()
                                break
                        except Exception:
                            pass
                    if done_event.is_set():
                        break

                await asyncio.sleep(1.0)

            # Extract cookies / session credentials after capture
            cookies_list = await ctx.cookies()
            cookie_map = {c['name']: c['value'] for c in cookies_list if c.get('name') and c.get('value')}

            if name == 'wind':
                if 'wind_balance' not in captured:
                    raise SessionExpiredError('未能获取 Wind 积分余额，请确认已完成官网登录')
                if captured.get('wind_headers'):
                    save_session('wind', {'headers': captured['wind_headers']})
                else:
                    active = [p for p in ctx.pages if not p.is_closed()]
                    sid = None
                    if active:
                        sid = await active[-1].evaluate("""() => {
                            for (const s of [localStorage, sessionStorage]) {
                                for (const k of Object.keys(s)) {
                                    if (k.toLowerCase().includes('sessionid')) return s.getItem(k);
                                }
                            }
                            return null;
                        }""")
                    if sid:
                        save_session('wind', {'headers': {'wind.sessionid': sid, 'windsessionid': sid}})
                return parse_wind_balance(captured['wind_balance'], captured.get('wind_info'))

            if name == 'ifind':
                if 'ifind_user' not in captured:
                    raise SessionExpiredError('未能获取 iFinD 账户额度，请确认已完成官网登录或使用「导入官网会话」')
                if cookie_map:
                    save_session('ifind', {'cookies': cookie_map})
                return parse_ifind_user_info(captured['ifind_user'])

            if name == 'tdx':
                if 'tdx_increment' in captured:
                    save_session('tdx', {'mode': 'browser_profile', 'bound': True})
                    return parse_tdx_increment(captured['tdx_increment'], captured.get('tdx_recent'))
                if 'tdx_dom' in captured:
                    save_session('tdx', {'mode': 'browser_profile', 'bound': True})
                    return captured['tdx_dom']
                raise SessionExpiredError('未能获取通达信 TDCP 积分余额，请确认已完成会员商城登录并进入「词元 Key 管理」页')

            if name == 'tushare':
                if 'tushare_parsed' not in captured:
                    raise SessionExpiredError('未能从 Tushare 官网个人中心读取积分，请确认已登录')
                if cookie_map:
                    save_session('tushare', {'cookies': cookie_map})
                return captured['tushare_parsed']

            if name == 'choice':
                if 'choice_auth' in captured:
                    res = parse_choice_authority(captured['choice_auth'], captured.get('choice_user'), choice_jsons)
                    save_session('choice', {'mode': 'browser_profile', 'bound': True})
                    return res
                if 'choice_parsed' in captured:
                    save_session('choice', {'mode': 'browser_profile', 'bound': True})
                    return captured['choice_parsed']
                raise SessionExpiredError('未能检测到东方财富妙想的已登录会话，请在官网完成登录后重试')

            raise ValueError(f'暂不支持 {name} 的网页登录同步')
        finally:
            await ctx.close()


# ---------------------------------------------------------------------------
# Public refresh & interactive login API
# ---------------------------------------------------------------------------

async def _refresh_choice_http(sess: dict[str, Any]) -> dict[str, Any]:
    url = sess.get('url')
    if not url:
        raise SessionExpiredError('尚未导入妙想积分接口 cURL，请按卡片指引粘贴导入。')
    method = str(sess.get('method') or 'GET').upper()
    headers = dict(sess.get('headers') or {})
    cookies = dict(sess.get('cookies') or {})
    body = sess.get('body')
    async with httpx.AsyncClient(timeout=20.0, cookies=cookies, headers=headers, follow_redirects=True) as client:
        r = await client.request(method, url, content=body.encode('utf-8') if isinstance(body, str) else None)
        if r.status_code == 401:
            raise SessionExpiredError('妙想官网会话已过期（HTTP 401），请重新复制积分接口 cURL 导入')
        return parse_choice_points_payload(r.json())


async def refresh_account(name: str) -> dict[str, Any]:
    if name not in config.PROVIDERS:
        raise ValueError(f'未知数据源：{name}')
    meta = PROVIDER_ACCOUNT_META[name]

    if name == 'akshare':
        patch = {
            'account_mode': 'free',
            'session_state': 'not_needed',
            'source': 'builtin_free',
            'last_sync_error': None,
            'account_updated_at': _now(),
        }
        config.update_status(name, patch)
        return {'status': 'ok', 'message': 'AKShare 为开源免费数据源，无积分消耗。', 'provider': name, 'account': patch}

    if name == 'tushare':
        sdk_err = None
        try:
            res = await _refresh_tushare_sdk()
            if res:
                config.update_status(name, res)
                return {'status': 'ok', 'message': f"已通过 Tushare SDK 同步：{res['permission_points']:g} 权限积分", 'provider': name, 'account': res}
        except Exception as exc:
            sdk_err = config.redact(str(exc))

        sess = load_session('tushare')
        if sess and sess.get('cookies'):
            try:
                res = await _refresh_tushare_http(sess)
                config.update_status(name, res)
                return {'status': 'ok', 'message': f"已从 Tushare 官网同步：{res['permission_points']:g} 权限积分", 'provider': name, 'account': res}
            except SessionExpiredError as exc:
                msg = str(exc)
                config.update_status(name, {'session_state': 'expired', 'last_sync_error': msg})
                return {'status': 'session_expired', 'message': msg, 'provider': name, 'url': meta['login_url']}
            except Exception as exc:
                msg = config.redact(str(exc))
                config.update_status(name, {'last_sync_error': msg})
                return {'status': 'error', 'message': msg, 'provider': name}

        msg = f'SDK 直连查询受限（{sdk_err or "未返回积分"}），请点击「登录绑定」从 Tushare 官网同步一次。'
        config.update_status(name, {'session_state': 'unbound', 'last_sync_error': msg})
        return {'status': 'login_required', 'message': msg, 'provider': name, 'url': meta['login_url']}

    if name in ('wind', 'ifind'):
        sess = load_session(name)
        if not sess:
            msg = f'尚未绑定 {name.upper()} 官网登录会话，请先点击「登录绑定」完成一次登录。'
            return {'status': 'login_required', 'message': msg, 'provider': name, 'url': meta['login_url']}
        try:
            res = await (_refresh_wind_http(sess) if name == 'wind' else _refresh_ifind_http(sess))
            config.update_status(name, res)
            summary = (
                f"{res['remaining_credits']:g} 积分" if res.get('remaining_credits') is not None
                else f"剩余 {res['remaining_calls']} 次" if res.get('remaining_calls') is not None
                else '同步成功'
            )
            return {'status': 'ok', 'message': f'已刷新 {name.upper()} 官方账户：{summary}', 'provider': name, 'account': res}
        except SessionExpiredError as exc:
            msg = str(exc)
            config.update_status(name, {'session_state': 'expired', 'last_sync_error': msg})
            return {'status': 'session_expired', 'message': msg, 'provider': name, 'url': meta['login_url']}
        except Exception as exc:
            msg = config.redact(str(exc))
            config.update_status(name, {'last_sync_error': msg})
            return {'status': 'error', 'message': msg, 'provider': name}

    if name == 'choice':
        sess = load_session('choice')
        if not sess or sess.get('mode') != 'curl_replay':
            msg = '请按卡片指引，在日常浏览器登录妙想/技能市场后复制积分余额接口的 Copy as cURL 粘贴导入。'
            return {'status': 'login_required', 'message': msg, 'provider': name, 'url': meta['overview_url']}
        try:
            res = await _refresh_choice_http(sess)
            config.update_status(name, res)
            summary = f"{res['remaining_credits']:g} 积分" if res.get('remaining_credits') is not None else '同步完成'
            return {'status': 'ok', 'message': f'已刷新 妙想 官方积分：{summary}', 'provider': name, 'account': res}
        except SessionExpiredError as exc:
            msg = str(exc)
            config.update_status(name, {'session_state': 'expired', 'last_sync_error': msg})
            return {'status': 'session_expired', 'message': msg, 'provider': name, 'url': meta['overview_url']}
        except Exception as exc:
            msg = config.redact(str(exc))
            config.update_status(name, {'last_sync_error': msg})
            return {'status': 'error', 'message': msg, 'provider': name}

    if name == 'tdx':
        if not has_saved_session(name):
            msg = '尚未绑定通达信官网登录会话，请先点击「登录绑定」完成一次登录。'
            return {'status': 'login_required', 'message': msg, 'provider': name, 'url': meta['login_url']}
        try:
            res = await _capture_provider_in_browser(name, headless=True, timeout_seconds=HEADLESS_REFRESH_TIMEOUT_SECONDS)
            config.update_status(name, res)
            summary = f"{res['remaining_credits']:g} 积分" if res.get('remaining_credits') is not None else '同步完成'
            return {'status': 'ok', 'message': f'已刷新通达信官方账户：{summary}', 'provider': name, 'account': res}
        except PlaywrightUnavailableError as exc:
            return {'status': 'playwright_missing', 'message': str(exc), 'provider': name}
        except SessionExpiredError as exc:
            msg = str(exc)
            config.update_status(name, {'session_state': 'expired', 'last_sync_error': msg})
            return {'status': 'session_expired', 'message': msg, 'provider': name, 'url': meta['login_url']}
        except Exception as exc:
            msg = config.redact(str(exc))
            config.update_status(name, {'last_sync_error': msg})
            return {'status': 'error', 'message': msg, 'provider': name}

    return {'status': 'login_required', 'message': '暂不支持自动刷新，请手动记录。', 'provider': name, 'url': config.OFFICIAL.get(name, '')}


async def refresh_all_accounts() -> dict[str, Any]:
    cfg = config.load()
    names = [n for n in config.PROVIDERS if cfg['providers'].get(n, {}).get('enabled', True)]

    async def _one(n: str):
        if n in ('wind', 'ifind', 'tdx', 'choice') and not has_saved_session(n):
            return n, {'status': 'skipped', 'message': '尚未登录绑定', 'provider': n}
        return n, await refresh_account(n)

    pairs = await asyncio.gather(*(_one(n) for n in names))
    results = dict(pairs)
    refreshed = sum(1 for r in results.values() if r.get('status') == 'ok')
    expired = [n for n, r in results.items() if r.get('status') == 'session_expired']
    return {
        'results': results,
        'refreshed_count': refreshed,
        'expired_providers': expired,
        'status': config.redact(enrich_status(config.status())),
    }


def login_state(name: str) -> dict[str, Any]:
    if name not in config.PROVIDERS:
        raise ValueError(f'未知数据源：{name}')
    state = _LOGIN_STATES.get(name)
    if not state:
        return {'provider': name, 'status': 'idle', 'message': ''}
    return dict(state)


def cancel_login(name: str) -> dict[str, Any]:
    task = _LOGIN_TASKS.get(name)
    _LOGIN_CONFIRM_EVENTS.pop(name, None)
    if name in _LOGIN_STATES and _LOGIN_STATES[name].get('status') in ('launching', 'waiting_login', 'syncing'):
        _LOGIN_STATES[name] = {'provider': name, 'status': 'cancelled', 'message': '已取消登录'}
    if task and not task.done():
        task.cancel()
    return login_state(name)


def confirm_login(name: str) -> dict[str, Any]:
    if name not in config.PROVIDERS:
        raise ValueError(f'未知数据源：{name}')
    ev = _LOGIN_CONFIRM_EVENTS.get(name)
    if ev:
        ev.set()
    return login_state(name)


def start_login(name: str) -> dict[str, Any]:
    if name not in config.PROVIDERS:
        raise ValueError(f'未知数据源：{name}')
    meta = PROVIDER_ACCOUNT_META[name]
    if not meta['supports_web_login']:
        return {'provider': name, 'status': 'not_needed', 'message': '该数据源无需登录绑定。'}
    if not playwright_available():
        return {
            'provider': name,
            'status': 'playwright_missing',
            'message': '当前环境尚未安装 playwright，无法弹出本地登录窗口。请运行 `pip install playwright` 后重启服务。',
        }
    existing = _LOGIN_TASKS.get(name)
    if existing and not existing.done():
        return login_state(name)

    _LOGIN_CONFIRM_EVENTS[name] = asyncio.Event()
    _LOGIN_STATES[name] = {
        'provider': name,
        'status': 'waiting_login',
        'message': '已打开本机 Chrome 窗口，请在官网完成登录，成功后将自动同步积分并关闭窗口…',
        'login_url': meta['login_url'],
        'started_at': _now(),
    }

    async def _runner():
        try:
            res = await _capture_provider_in_browser(name, headless=False, timeout_seconds=LOGIN_TIMEOUT_SECONDS)
            config.update_status(name, res)
            _LOGIN_STATES[name] = {
                'provider': name,
                'status': 'success',
                'message': '登录并同步账户额度成功',
                'finished_at': _now(),
            }
        except asyncio.CancelledError:
            _LOGIN_STATES[name] = {
                'provider': name,
                'status': 'cancelled',
                'message': '已取消登录绑定',
                'finished_at': _now(),
            }
        except Exception as exc:
            msg = config.redact(str(exc) or '登录同步失败')
            _LOGIN_STATES[name] = {
                'provider': name,
                'status': 'failed',
                'message': msg,
                'finished_at': _now(),
            }

    _LOGIN_TASKS[name] = asyncio.create_task(_runner())
    return login_state(name)


def enrich_status(raw_status: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(raw_status or {'schema_version': 1, 'providers': {}}))
    providers = out.setdefault('providers', {})
    pw_ok = playwright_available()
    for name in config.PROVIDERS:
        meta = PROVIDER_ACCOUNT_META[name]
        entry = providers.setdefault(name, {})
        bound = has_saved_session(name)
        if not meta['supports_web_login']:
            entry['session_state'] = 'not_needed'
            entry['account_mode'] = meta['account_mode']
            if not entry.get('source'):
                entry['source'] = 'builtin_free'
        else:
            current_state = entry.get('session_state')
            if current_state == 'expired' and bound:
                pass
            elif bound:
                entry['session_state'] = 'active'
            elif name == 'tushare' and entry.get('source') == 'official_sdk':
                entry['session_state'] = 'not_needed'
            else:
                entry['session_state'] = 'unbound'
            entry.setdefault('account_mode', meta['account_mode'])
        entry['account_meta'] = {
            'account_mode': meta['account_mode'],
            'unit_label': meta['unit_label'],
            'supports_sdk_refresh': meta['supports_sdk_refresh'],
            'supports_web_login': meta['supports_web_login'],
            'supports_browser_popup': meta.get('supports_browser_popup', False),
            'supports_session_import': meta.get('supports_session_import', False),
            'import_target': meta.get('import_target'),
            'login_url': meta['login_url'],
            'overview_url': meta['overview_url'],
            'auth_note': meta['auth_note'],
            'has_saved_session': bound,
            'playwright_available': pw_ok,
        }
        ls = _LOGIN_STATES.get(name)
        if ls and ls.get('status') in ('launching', 'waiting_login', 'syncing'):
            entry['login_in_progress'] = True
        else:
            entry['login_in_progress'] = False
    return out
