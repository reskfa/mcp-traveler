import json
import os
import tempfile
import threading
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(os.environ.get('MCP_TRAVELER_HOME', Path(__file__).resolve().parents[1]))
LOCK = threading.RLock()
PROVIDERS = ['tushare', 'akshare', 'wind', 'ifind', 'tdx']
KEYS = {'tushare': 'TUSHARE_TOKEN', 'wind': 'WIND_API_KEY', 'ifind': 'IFIND_API_KEY', 'tdx': 'TDX_API_KEY'}
OFFICIAL = {'tushare': 'https://tushare.pro/', 'akshare': 'https://akshare.akfamily.xyz/', 'wind': 'https://market.windalice.com/', 'ifind': 'https://mcp.51ifind.com/', 'tdx': 'https://vip.tdx.com.cn/'}

def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def write_json(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')

def load():
    c=json.loads((ROOT / 'config.json').read_text())
    if 'global_route' not in c:
        c['global_route']=list(c['routes']['valuations'])
        c['route_overrides']={k:v for k,v in c['routes'].items() if v!=c['global_route']}
    c['routes']={k:c.get('route_overrides',{}).get(k,c['global_route']) for k in ['daily_prices','valuations','financials','technical_indicators','basic_info']}
    return c

def secrets():
    return {k: v or '' for k, v in dotenv_values(ROOT / '.env').items()}

def redact(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    for secret in secrets().values():
        if secret: text = text.replace(secret, '[REDACTED]')
    return json.loads(text)

def public():
    c = load()
    keys = secrets()
    for name, p in c['providers'].items():
        key = keys.get(KEYS.get(name, ''), '')
        p['configured'] = name == 'akshare' or bool(key and '填写' not in key)
        p['key_hint'] = ('••••' + key[-4:]) if p['configured'] and key else ''
        p['official_url'] = OFFICIAL[name]
    return redact(c)

def save_provider(name, patch):
    from urllib.parse import urlsplit
    with LOCK:
        c = load()
        p = c['providers'][name]
        if 'enabled' in patch: p['enabled'] = bool(patch['enabled'])
        if 'timeout_seconds' in patch:
            p['timeout_seconds'] = max(5, min(180, int(patch['timeout_seconds'])))
        if 'url' in patch and 'mcp_servers' in p:
            u = patch['url'].strip()
            if u and urlsplit(u).scheme != 'https': raise ValueError('上游 MCP 地址须使用 HTTPS')
            server = next(iter(p['mcp_servers'].values()))
            server['url'] = u
            for field in ['auth_header', 'auth_prefix']:
                if field in patch:
                    if '\n' in patch[field] or '\r' in patch[field]: raise ValueError('请求头不能包含换行')
                    server[field] = patch[field]
        if patch.get('key') and name in KEYS:
            key = patch['key'].strip()
            if '\n' in key or '\r' in key: raise ValueError('密钥不能包含换行')
            # Preserve comments and unrelated variables, replace only the selected key.
            lines = (ROOT / '.env').read_text().splitlines() if (ROOT / '.env').exists() else []
            prefix = KEYS[name] + '='
            replacement = prefix + "'" + key.replace('\\', '\\\\').replace("'", "\\'") + "'"
            found = False
            for i, line in enumerate(lines):
                if line.strip().startswith(prefix): lines[i] = replacement; found = True
            if not found: lines.append(replacement)
            atomic(ROOT / '.env', '\n'.join(lines) + '\n')
        write_json(ROOT / 'config.json', c)
    return public()

def save_routes(routes):
    with LOCK:
        c = load()
        def valid(order):
            return isinstance(order,list) and bool(order) and all(isinstance(n,str) for n in order) and len(order)==len(set(order)) and not set(order)-set(PROVIDERS)
        if 'global_route' in routes:
            order=routes['global_route'];overrides=routes.get('route_overrides',{})
            if not valid(order) or not isinstance(overrides,dict) or any(k not in c['routes'] or not valid(v) for k,v in overrides.items()):raise ValueError('来源顺序无效')
            c['global_route']=order;c['route_overrides']=overrides
        else:
            for category,order in routes.items():
                if category not in c['routes'] or not valid(order):raise ValueError('路由类别或来源顺序无效')
                c['route_overrides'][category]=order
        c['routes']={k:c['route_overrides'].get(k,c['global_route']) for k in c['routes']}
        write_json(ROOT / 'config.json', c)
    return public()

def status():
    p = ROOT / 'local/account_status.json'
    return json.loads(p.read_text()) if p.exists() else {'schema_version': 1, 'providers': {}}

def update_status(name, patch):
    with LOCK:
        s = status()
        s.setdefault('providers', {}).setdefault(name, {}).update(patch)
        write_json(ROOT / 'local/account_status.json', s)
    return s
