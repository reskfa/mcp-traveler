import asyncio
import json
import pytest
from fastapi.testclient import TestClient

from gateway import accounts, config
from gateway.app import app


@pytest.fixture
def home(tmp_path, monkeypatch):
    original = config.ROOT
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    (tmp_path / 'config.json').write_text((original / 'config.example.json').read_text())
    (tmp_path / '.env').write_text('TUSHARE_TOKEN="test-tushare-token"\nWIND_API_KEY="test-wind-key"\n')
    accounts._LOGIN_STATES.clear()
    accounts._LOGIN_TASKS.clear()
    return tmp_path


def test_parse_wind_balance_and_expiry():
    payload = {
        'code': 0,
        'data': {
            'balance': 1240.5,
            'availableTotal': 1240.5,
            'rechargeTotal': 1000,
            'subscribeTotal': 200,
            'giftTotal': 40.5,
            'segments': [
                {'name': '春季赠送包', 'balance': 40.5, 'expireTime': '2026-12-31'},
            ],
        },
    }
    info = {'code': 0, 'data': {'mobile': '13812345678', 'planName': '开发者版'}}
    res = accounts.parse_wind_balance(payload, info)
    assert res['remaining_credits'] == 1240.5
    assert res['account_label'] == '138****5678'
    assert res['plan_name'] == '开发者版'
    assert res['expires_at'] == '2026-12-31'
    assert res['session_state'] == 'active'
    assert res['source'] == 'official_web'
    assert len(res['breakdown']) >= 4

    with pytest.raises(accounts.SessionExpiredError):
        accounts.parse_wind_balance({'errorCode': 10010, 'message': 'E_950003：缺少SessionId，请携带SessionId！'})


def test_parse_ifind_user_info_and_expiry():
    payload = {
        'errcode': 0,
        'data': {
            'account': '13900008888',
            'userTypeName': '个人版',
            'productExpire': '2027-01-15',
            'userMcpUsages': {
                'plans': [
                    {
                        'planType': 'total',
                        'planName': '月度总调用额度',
                        'usage': 180,
                        'limit': 500,
                        'validTo': '2026-11-30',
                        'usagePeriod': {'start': '2026-11-01', 'end': '2026-11-30'},
                    },
                    {
                        'planType': 'csv',
                        'planName': 'CSV 导出次数',
                        'usage': 12,
                        'limit': 100,
                        'validTo': '2026-11-30',
                    },
                ],
                'addonGroups': [{'name': '高频加油包', 'usage': 5, 'limit': 50}],
            },
        },
    }
    res = accounts.parse_ifind_user_info(payload)
    assert res['account_mode'] == 'quota'
    assert res['account_label'] == '139****8888'
    assert res['plan_name'] == '个人版'
    assert res['total_calls'] == 650
    assert res['used_calls'] == 197
    assert res['remaining_calls'] == 453
    assert res['remaining_credits'] is None
    assert res['expires_at'] == '2027-01-15'
    assert len(res['breakdown']) == 3

    # Exhausted trial plan + active paid plan should show paid plan's remaining quota, not 0
    paid_payload = {
        'errcode': 0,
        'data': {
            'account': 'mo_123456789',
            'userTypeName': '个人基础版',
            'productExpire': '2026-12-22',
            'userMcpUsages': {
                'plans': [
                    {'planName': '试用版', 'usage': 2012, 'limit': 2000, 'validTo': '2026-11-30'},
                    {'planName': '个人基础版套餐', 'usage': 427, 'limit': 3000, 'validTo': '2026-12-22'},
                ],
            },
        },
    }
    paid_res = accounts.parse_ifind_user_info(paid_payload)
    assert paid_res['account_label'] == 'mo_12****89'
    assert paid_res['remaining_calls'] == 2573
    assert paid_res['total_calls'] == 3000
    assert paid_res['used_calls'] == 427

    with pytest.raises(accounts.SessionExpiredError):
        accounts.parse_ifind_user_info({'errorcode': -1010, 'errmsg': 'your account has been loggout out.'})


def test_parse_tdx_increment_and_recent():
    inc = {
        'ErrorCode': 0,
        'tables': [[{'incrementValue': '10,000', 'consumeValue': '2,350'}]],
    }
    recent = {
        'ErrorCode': 0,
        'tables': [[{'leftTime': '18.5', 'usedTime': '410'}]],
    }
    res = accounts.parse_tdx_increment(inc, recent)
    assert res['total_credits'] == 10000.0
    assert res['used_credits'] == 2350.0
    assert res['remaining_credits'] == 7650.0
    assert res['estimated_days_left'] == 18.5
    assert '410' in res['recent_usage_note']

    with pytest.raises(accounts.SessionExpiredError):
        accounts.parse_tdx_increment({'ErrorCode': -1, 'ErrorInfo': '账号未登陆,功能禁止调用'})


def test_parse_tushare_user_and_web_text():
    res = accounts.parse_tushare_user([{'id': '123456', 'nick': 'quant_user', 'credits': 8000}])
    assert res['permission_points'] == 8000.0
    assert res['account_label'] == 'quant_user'
    assert res['source'] == 'official_sdk'

    web = accounts.parse_tushare_web_text('个人主页 当前积分：5,200 到期时间：2027-06-30')
    assert web is not None
    assert web['permission_points'] == 5200.0
    assert web['expires_at'] == '2027-06-30'


def test_session_storage_permissions_and_redaction(home):
    secret_sid = 'super-secret-wind-session-id-99999'
    accounts.save_session('wind', {'headers': {'wind.sessionid': secret_sid}})
    spath = accounts.session_path('wind')
    assert spath.exists()
    assert (spath.stat().st_mode & 0o777) == 0o600

    leaked = {'note': f'header={secret_sid}', 'ok': True}
    redacted = config.redact(leaked)
    assert secret_sid not in json.dumps(redacted)
    assert '[REDACTED]' in redacted['note']

    cleared = accounts.clear_session('wind')
    assert not spath.exists()
    assert cleared['providers']['wind']['session_state'] == 'unbound'


def test_account_api_endpoints_and_refresh(home, monkeypatch):
    async def fake_tushare_sdk():
        return accounts.parse_tushare_user([{'id': '888', 'nick': 'ts_tester', 'credits': 8000}])

    async def fake_wind_http(sess):
        return accounts.parse_wind_balance({'code': 0, 'data': {'availableTotal': 321.5}})

    monkeypatch.setattr(accounts, '_refresh_tushare_sdk', fake_tushare_sdk)
    monkeypatch.setattr(accounts, '_refresh_wind_http', fake_wind_http)
    accounts.save_session('wind', {'headers': {'wind.sessionid': 'wind-sid-abcdef123456'}})

    client = TestClient(app, raise_server_exceptions=True)
    # Enriched status includes account_meta for all 6 providers
    st = client.get('/api/status').json()
    assert set(st['providers'].keys()) >= set(config.PROVIDERS)
    assert st['providers']['akshare']['account_meta']['account_mode'] == 'free'
    assert st['providers']['wind']['session_state'] == 'active'

    # AKShare refresh
    r_ak = client.post('/api/accounts/akshare/refresh').json()
    assert r_ak['status'] == 'ok'

    # Tushare SDK refresh
    r_ts = client.post('/api/accounts/tushare/refresh').json()
    assert r_ts['status'] == 'ok'
    assert r_ts['account']['permission_points'] == 8000.0

    # Wind HTTP fast-path refresh
    r_wd = client.post('/api/accounts/wind/refresh').json()
    assert r_wd['status'] == 'ok'
    assert r_wd['account']['remaining_credits'] == 321.5

    # iFinD without session -> login_required
    r_if = client.post('/api/accounts/ifind/refresh').json()
    assert r_if['status'] == 'login_required'

    # Refresh all
    r_all = client.post('/api/accounts/refresh-all').json()
    assert r_all['refreshed_count'] >= 3
    assert 'wind-sid-abcdef123456' not in json.dumps(r_all)

    # Manual PATCH remains backwards compatible
    r_patch = client.patch('/api/accounts/choice', json={'remaining_credits': 500, 'expires_at': '2026-12-31'})
    assert r_patch.status_code == 200
    assert r_patch.json()['providers']['choice']['remaining_credits'] == 500.0
    assert r_patch.json()['providers']['choice']['source'] == 'manual'

    # Logout clears Wind session
    r_del = client.delete('/api/accounts/wind/session')
    assert r_del.status_code == 200
    assert r_del.json()['providers']['wind']['session_state'] == 'unbound'
    assert not accounts.session_path('wind').exists()

    # Login status & akshare not_needed
    assert client.get('/api/accounts/wind/login').json()['status'] == 'idle'
    assert client.post('/api/accounts/akshare/login').json()['status'] == 'not_needed'


def test_wind_session_expiry_marks_expired(home, monkeypatch):
    accounts.save_session('wind', {'headers': {'wind.sessionid': 'expired-sid-12345678'}})

    async def fake_expired(sess):
        raise accounts.SessionExpiredError('Wind 官网会话已过期（HTTP 401）')

    monkeypatch.setattr(accounts, '_refresh_wind_http', fake_expired)
    res = asyncio.run(accounts.refresh_account('wind'))
    assert res['status'] == 'session_expired'
    assert config.status()['providers']['wind']['session_state'] == 'expired'


def test_parse_tdx_dom_text():
    sample = """
    积分和Key管理
    账户可用积分
    23,068
    / 30,000 总量
    购买积分
    已使用 23%
    近10天消耗 420 积分,按当前使用频率,您的余额预计还可以使用 54.9 天
    """
    res = accounts.parse_tdx_dom_text(sample)
    assert res is not None
    assert res['remaining_credits'] == 23068.0
    assert res['total_credits'] == 30000.0
    assert res['used_credits'] == 6932.0
    assert res['estimated_days_left'] == 54.9
    assert '420' in res['recent_usage_note']


def test_parse_choice_authority():
    auth = {'code': 0, 'data': {'todayUsedNum': 12, 'todayTotalNum': 100, 'needLogin': False, 'hasMxCredit': True}}
    user = {'code': 0, 'data': {'uAlias': 'quant_em', 'maskedMobile': '138****9999'}}
    res = accounts.parse_choice_authority(auth, user)
    assert res['account_label'] == 'quant_em'
    assert res['total_calls'] == 100
    assert res['used_calls'] == 12
    assert res['remaining_calls'] == 88
    assert res['session_state'] == 'active'

    with pytest.raises(accounts.SessionExpiredError):
        accounts.parse_choice_authority({'data': {'needLogin': True}})


def test_import_session_curl_and_cookie(home, monkeypatch):
    curl_cmd = (
        "curl 'https://mcp.51ifind.com/quantapigw/ds_service_customization/mcpweb/get_user_info' \\\n"
        "  -H 'Accept: application/json' \\\n"
        "  -b 'userid=987654321; sid=secret-ifind-cookie-value-xyz'"
    )
    parsed = accounts.parse_imported_session('ifind', curl_cmd)
    assert parsed['cookies']['userid'] == '987654321'
    assert parsed['cookies']['sid'] == 'secret-ifind-cookie-value-xyz'

    async def fake_ifind_http(sess):
        assert sess['cookies']['userid'] == '987654321'
        return accounts.parse_ifind_user_info({
            'errcode': 0,
            'data': {
                'account': '13900008888',
                'userTypeName': '个人版',
                'userMcpUsages': {'plans': [{'planName': '月度总额度', 'usage': 10, 'limit': 500}]},
            },
        })

    monkeypatch.setattr(accounts, '_refresh_ifind_http', fake_ifind_http)
    client = TestClient(app, raise_server_exceptions=True)
    r = client.post('/api/accounts/ifind/session-import', json={'raw_text': curl_cmd})
    assert r.status_code == 200
    body = r.json()
    assert body['status'] == 'ok'
    assert body['account']['remaining_calls'] == 490
    assert 'secret-ifind-cookie-value-xyz' not in json.dumps(body)
    assert (accounts.session_path('ifind').stat().st_mode & 0o777) == 0o600


def test_choice_curl_points_import(home, monkeypatch):
    curl_cmd = (
        "curl --url 'https://llm-useraccessconfigzp.eastmoney.com/invResearch/api/clawCredit/summary' \\\n"
        "  -H 'accept: application/json' \\\n"
        "  -H 'content-type: application/json' \\\n"
        "  -H 'utoken: secret-em-token-888' \\\n"
        "  --data-raw '{\"querySource\":\"mcp\"}'"
    )
    parsed = accounts.parse_imported_session('choice', curl_cmd)
    assert parsed['mode'] == 'curl_replay'
    assert parsed['url'] == 'https://llm-useraccessconfigzp.eastmoney.com/invResearch/api/clawCredit/summary'
    assert parsed['method'] == 'POST'
    assert parsed['body'] == '{"querySource":"mcp"}'
    assert parsed['headers']['utoken'] == 'secret-em-token-888'

    with pytest.raises(ValueError):
        accounts.parse_imported_session('choice', "curl 'https://evil.example/api/points'")

    pts = accounts.parse_choice_points_payload({'code': 0, 'data': {'remainPoints': 1850, 'totalPoints': 2000}})
    assert pts['remaining_credits'] == 1850.0
    assert pts['total_credits'] == 2000.0
    assert pts['used_credits'] == 150.0
    assert pts['account_mode'] == 'points'

    summary = accounts.parse_choice_points_payload({
        'code': 0,
        'data': {
            'remainingCredit': 6195.28,
            'monthlyAdded': 6700,
            'monthlyPurchased': 6000,
            'monthlyOtherAdded': 700,
            'monthlyConsumed': 504.96,
            'monthlyExecutionConsumed': 188.26,
            'monthlyExpired': 316.7,
            'todayConsumed': 99.88,
            'todayExecutionConsumed': 4.72,
            'todayExpired': 95.16,
        },
    })
    assert summary['remaining_credits'] == 6195.28
    assert summary['used_credits'] == 504.96
    assert summary['remaining_calls'] is None
    assert '今日消耗 99.88' in summary['recent_usage_note']
    assert '本月消耗 504.96' in summary['recent_usage_note']
    assert len(summary['breakdown']) == 4


