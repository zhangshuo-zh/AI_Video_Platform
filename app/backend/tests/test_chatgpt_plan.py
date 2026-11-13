"""ChatGPT OAuth 与草案路径。模拟外部响应，不使用真实账号额度。"""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import agents
import chatgpt_plan as cg
from repositories.agents import agent_runs
from repositories.users import users


@pytest.fixture(autouse=True)
def private_store(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, 'DB_PATH', tmp_path / 'platform.db')


def payload(seeded):
    return dict(request_id=str(uuid.uuid4()), project_id=seeded['project_id'],
                episode_id=seeded['episode_id'], model='test-model', goal='盘点当前集缺少的资产', confirmed=True)


def wire(monkeypatch):
    monkeypatch.setattr(cg, 'models', lambda uid: [{'id': 'test-model', 'name': 'Test'}])
    monkeypatch.setattr(cg, 'infer', lambda *a, **kw: json.dumps([
        {'tool': 'asset_register', 'args': {'kind': 'character', 'name': '待确认角色'}, 'why': '补全角色'}]))


def test_draft_confirmation_scope_and_paused_accept(as_role, seeded, monkeypatch):
    wire(monkeypatch)
    body = payload(seeded)
    with as_role('editor') as c:
        denied = c.post('/api/agent/chatgpt/drafts', json={**body, 'confirmed': False})
        assert denied.status_code == 422
        result = c.post('/api/agent/chatgpt/drafts', json=body)
        assert result.status_code == 200, result.text
        assert result.json()['state'] == 'ready'
        url = '/api/agent/chatgpt/drafts/' + body['request_id'] + '/accept'
        first = c.post(url, json={})
        assert first.status_code == 200, first.text
        rid = first.json()['run_id']
        assert c.post(url, json={}).json()['run_id'] == rid
        run = agent_runs.get(rid)
        assert run['paused'] and run['plan_status'] == 'ready' and run['plan_job_id'] is None
        steps = agent_runs.steps(rid)
        assert len(steps) == 1 and steps[0]['status'] == 'pending'
        monkeypatch.setattr(agents, 'plan_route', lambda: {'ok': True, 'provider': 'platform-text',
            'model': 'default-model', 'cost_note': '平台文本默认轨计价'})
        approval = agents.approval_view(steps[0]['id'], users.get_by_id(as_role.id('editor')))
        assert approval['model_snapshot']['provider'] == 'platform-text'
        assert approval['cost_note'] == '平台文本默认轨计价'
        agents._tick_run(run)
        assert agent_runs.steps(rid)[0]['status'] == 'pending'
    with as_role('outsider') as c:
        assert c.post('/api/agent/chatgpt/drafts', json=payload(seeded)).status_code in (403, 404)
        assert c.get('/api/agent/chatgpt/drafts/' + body['request_id']).status_code == 404


def test_repeated_and_concurrent_request_consumes_once(as_role, seeded, monkeypatch):
    wire(monkeypatch)
    entered, release = Event(), Event()
    calls = []
    def infer(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return '[{"tool":"episode_preflight","args":{"episode_id":0}}]'
    monkeypatch.setattr(cg, 'infer', infer)
    body = payload(seeded)
    with as_role('editor') as c, ThreadPoolExecutor(2) as pool:
        first = pool.submit(c.post, '/api/agent/chatgpt/drafts', json=body)
        assert entered.wait(5)
        second = c.post('/api/agent/chatgpt/drafts', json=body)
        release.set()
        assert second.json()['state'] == 'generating'
        assert first.result().json()['state'] == 'ready'
        assert c.post('/api/agent/chatgpt/drafts', json=body).json()['state'] == 'ready'
        assert len(calls) == 1
        assert c.get('/api/agent/chatgpt/usage').json()['total'] == 1
        assert c.post('/api/agent/chatgpt/drafts', json={**body, 'goal': '不同的新目标'}).status_code == 409


def test_failed_stream_cannot_create_run_or_auto_retry(as_role, seeded, monkeypatch):
    wire(monkeypatch)
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise cg.ChatGPTError('连接中断')
    monkeypatch.setattr(cg, 'infer', fail)
    body = payload(seeded)
    with as_role('editor') as c:
        assert c.post('/api/agent/chatgpt/drafts', json=body).json()['state'] == 'failed'
        assert c.post('/api/agent/chatgpt/drafts', json=body).json()['state'] == 'failed'
        assert c.post('/api/agent/chatgpt/drafts/' + body['request_id'] + '/accept', json={}).status_code == 409
        assert len(calls) == 1


def test_oauth_verifies_identity_nonce_and_replay(monkeypatch):
    name = cg._account(7)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    class JWK:
        def __init__(self, *a, **kw): pass
        def get_signing_key_from_jwt(self, token): return type('Key', (), {'key': key.public_key()})
    monkeypatch.setattr(jwt, 'PyJWKClient', JWK)
    def prepare():
        cg._write(name, {'pending': {'client_id': '', 'subject': '', 'state': 'state', 'nonce': 'nonce',
            'verifier': 'verifier', 'redirect': 'http://127.0.0.1:1234/auth/callback', 'expires': time.time() + 60}})
    def tokens(url, data=None, **kw):
        assert data['client_id'] == 'oaiapp_test'
        claims = {'iss': cg.ISSUER, 'aud': 'oaiapp_test', 'sub': 'subject', 'nonce': 'nonce', 'exp': time.time()+60}
        return {'access_token': 'access', 'refresh_token': 'refresh', 'scope': cg.SCOPE,
                'id_token': jwt.encode(claims, key, algorithm='RS256')}
    monkeypatch.setattr(cg, '_json_request', tokens)
    query = {'state': ['state'], 'client_id': ['oaiapp_test'], 'code': ['code']}
    prepare()
    with pytest.raises(cg.ChatGPTError): cg.finish_login(7, {**query, 'state': ['wrong']})
    cg.finish_login(7, query)
    assert cg.status(7)['connected'] and cg.status(7)['sharing']
    assert 'access_token' not in cg.status(7)
    with pytest.raises(cg.ChatGPTError): cg.finish_login(7, query)
    assert (cg._directory() / (name + '.json')).stat().st_mode & 0o777 == 0o600
    encoded = tokens('', {'client_id': 'oaiapp_test'})
    with pytest.raises(cg.ChatGPTError): cg._verify(encoded, 'other-audience', 'nonce')
    with pytest.raises(cg.ChatGPTError): cg._verify(encoded, 'oaiapp_test', 'other-nonce')


def test_refresh_rotation_serialized(monkeypatch):
    cg._write(cg._account(4), dict(client_id='client', scope=cg.SCOPE, access_token='old', refresh_token='old-refresh', expires_at=0))
    calls = []
    def token(*args, **kw):
        calls.append(1)
        time.sleep(.04)
        return {'access_token': 'new', 'refresh_token': 'rotated', 'expires_in': 3600}
    monkeypatch.setattr(cg, '_json_request', token)
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(cg.access_token, [4, 4])) == ['new', 'new']
    assert len(calls) == 1
    assert cg._read(cg._account(4))['refresh_token'] == 'rotated'


def test_inference_requires_completed_event(monkeypatch):
    monkeypatch.setattr(cg, 'access_token', lambda uid: 'test')
    class Stream:
        def __init__(self, completed): self.completed = completed
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def __iter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"[]"}\n'
            if self.completed: yield b'data: {"type":"response.completed"}\n'
    monkeypatch.setattr(cg, 'urlopen', lambda *a, **kw: Stream(False))
    with pytest.raises(cg.ChatGPTError): cg.infer(7, 'test', 'prompt')
    monkeypatch.setattr(cg, 'urlopen', lambda *a, **kw: Stream(True))
    assert cg.infer(7, 'test', 'prompt') == '[]'


def test_loopback_login_uses_pkce_and_callback(monkeypatch):
    from urllib.request import urlopen
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    out = cg.start_login(7)
    params = parse_qs(urlparse(out['url']).query)
    assert params['client_id'] == ['dynamic_agent_client']
    assert params['code_challenge_method'] == ['S256']
    assert params['resource'] == [cg.RESOURCE]
    first_host = params['ext_agent_host_id']
    monkeypatch.setattr(cg, '_json_request', lambda *a, **kw: {'access_token': 'test-access', 'scope': cg.SCOPE})
    monkeypatch.setattr(cg, '_verify', lambda *a: {'sub': 'test-sub', 'email': 'test@example.test'})
    callback = params['redirect_uri'][0] + '?state=' + params['state'][0] + '&client_id=oaiapp_test&code=test-code'
    with urlopen(callback) as response:
        assert b'connected' in response.read()
    assert cg.status(7)['connected']
    assert cg._read('host')['id'] == first_host[0]



def test_platform_usage_counts_once_and_preserves_missing_metrics(as_role, seeded, monkeypatch):
    import chatgpt_usage
    from datetime import datetime
    uid = as_role.id('editor')
    pid = seeded['project_id']
    now = datetime(2026, 11, 14, 12, tzinfo=chatgpt_usage.ZONE).timestamp()
    monkeypatch.setattr(chatgpt_usage.time, 'time', lambda: now)
    def record(owner, project, stamp, state, **extra):
        name = cg.draft_name(owner, str(uuid.uuid4()))
        cg._write(name, {'project_id': project, 'created_at': stamp,
            'state': state, 'model': 'test-model', **extra})
    record(uid, pid, now - 100, 'ready', duration_ms=2000, metrics={'input_tokens': 100, 'output_tokens': 20})
    record(uid, pid, now - 90, 'failed', duration_ms=4000, metrics={'response_state': 'unconfirmed'})
    record(uid, pid, now - 80, 'ready')  # 升级之前已有的记录，没有耗时和 Token。
    record(uid, pid, now - 200, 'generating')  # 中断后仍停留在生成中的记录。
    record(uid, pid, now - 10, 'generating')
    # 北京时间最近 7 天从 11/08 00:00 开始。
    first = datetime(2026, 11, 8, tzinfo=chatgpt_usage.ZONE).timestamp()
    record(uid, pid, first, 'failed')
    record(uid, pid, first - 1, 'ready')
    record(uid + 99999, pid, now, 'ready')
    record(uid, pid + 99999, now, 'ready')
    with as_role('editor') as c:
        out = c.get('/api/agent/chatgpt/usage', params={'days': 7, 'project_id': pid})
        assert out.status_code == 200, out.text
        data = out.json()
        assert data['total'] == 6 and data['succeeded'] == 2 and data['failed'] == 1
        assert data['unconfirmed'] == 2 and data['generating'] == 1
        assert data['input_tokens'] == 100 and data['output_tokens'] == 20
        assert data['input_samples'] == data['output_samples'] == 1
        assert data['avg_duration_ms'] == 3000 and data['duration_samples'] == 2
        assert len(data['daily']) == 7 and data['daily'][0]['date'] == '2026-11-08'
        assert sum(row['total'] for row in data['daily']) == data['total']
        assert data['models'] == [{'model': 'test-model', 'total': 6}]
        assert c.get('/api/agent/chatgpt/usage?days=8').status_code == 422
    with as_role('outsider') as c:
        assert c.get('/api/agent/chatgpt/usage', params={'project_id': pid}).status_code == 404
        assert c.get('/api/agent/chatgpt/usage').json()['total'] == 0
    empty = chatgpt_usage.summarize(uid + 88888, 30, None, now=now)
    assert empty['total'] == 0 and empty['input_tokens'] is None and empty['avg_duration_ms'] is None
    assert len(empty['daily']) == 30


def test_stream_records_only_reported_token_usage(monkeypatch):
    monkeypatch.setattr(cg, 'access_token', lambda uid: 'test')
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def __iter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"[]"}\n'
            yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":0,"output_tokens":42}}}\n'
    monkeypatch.setattr(cg, 'urlopen', lambda *a, **kw: Stream())
    metrics = {}
    assert cg.infer(7, 'test', 'prompt', metrics=metrics) == '[]'
    assert metrics == {'response_state': 'completed', 'input_tokens': 0, 'output_tokens': 42}
