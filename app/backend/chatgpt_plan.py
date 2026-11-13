"""ChatGPT 套餐授权及制作计划草案。凭据按平台用户隔离，仅存后端私有目录。"""
import base64
import fcntl
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from config import DB_PATH

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
TOKEN_URL = ISSUER + '/api/accounts/oauth/token'
SCOPE = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'


class ChatGPTError(ValueError):
    pass


def _directory():
    path = DB_PATH.parent / 'chatgpt'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@contextmanager
def _lock(name):
    # flock 同时覆盖 Web 线程和多个后端进程。普通状态更新只短暂持锁。
    fd = os.open(_directory() / (name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(name):
    path = _directory() / (name + '.json')
    return json.loads(path.read_text()) if path.exists() else {}


def _write(name, value):
    path = _directory() / (name + '.json')
    temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _json_request(url, data=None, token=None, form=False):
    headers = {'Accept': 'application/json'}
    body = None
    if data is not None:
        body = urlencode(data).encode() if form else json.dumps(data).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        with urlopen(Request(url, body, headers), timeout=25) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ChatGPTError('授权服务返回内容过大')
            return json.loads(raw) if raw else {}
    except (HTTPError, URLError, OSError, ValueError) as exc:
        # 供应商响应可能带凭据，不把响应体和完整 URL 放进错误、日志。
        code = exc.code if isinstance(exc, HTTPError) else '连接失败'
        raise ChatGPTError(f'ChatGPT 请求失败（{code}），请检查代理或重新授权') from None


def _account(user_id):
    return f'user-{int(user_id)}'


def status(user_id):
    with _lock(_account(user_id)):
        data = _read(_account(user_id))
    pending = data.get('pending', {})
    return {'connected': bool(data.get('access_token')), 'registered': bool(data.get('client_id')),
            'sharing': 'chatgpt.tokens.use.direct' in data.get('scope', '').split(),
            'account': data.get('email', ''),
            'pending': pending.get('expires', 0) > time.time(),
            'error': data.get('error', '')}


def _verify(tokens, client_id, nonce):
    import jwt
    from jwt import PyJWKClient
    try:
        encoded = tokens['id_token']
        key = PyJWKClient(ISSUER + '/.well-known/jwks.json', timeout=15).get_signing_key_from_jwt(encoded)
        claims = jwt.decode(encoded, key.key, algorithms=['RS256'], audience=client_id,
                            issuer=ISSUER, options={'require': ['exp', 'iss', 'aud', 'sub', 'nonce']})
        if not secrets.compare_digest(str(claims['nonce']), nonce):
            raise ChatGPTError('登录 nonce 不匹配，请重新连接')
        return claims
    except (jwt.PyJWTError, KeyError, TypeError):
        raise ChatGPTError('无法验证 ChatGPT 身份，请重新连接') from None


def start_login(user_id, new_account=False):
    name = _account(user_id)
    with _lock('host'):
        host = _read('host') or {'id': 'urn:uuid:' + str(uuid.uuid4())}
        _write('host', host)
    server = HTTPServer(('127.0.0.1', 0), _Callback)
    server.timeout = 1
    pending = {'state': secrets.token_urlsafe(32), 'nonce': secrets.token_urlsafe(32),
               'verifier': secrets.token_urlsafe(48), 'expires': time.time() + 300,
               'redirect': f'http://127.0.0.1:{server.server_port}/auth/callback'}
    with _lock(name):
        data = _read(name)
        pending['client_id'] = '' if new_account else data.get('client_id', '')
        pending['subject'] = '' if new_account else data.get('subject', '')
        data['pending'] = pending
        data['error'] = ''
        _write(name, data)
    params = dict(client_id=pending['client_id'] or 'dynamic_agent_client',
                  ext_agent_host_id=host['id'], response_type='code',
                  redirect_uri=pending['redirect'], scope=SCOPE, resource=RESOURCE,
                  state=pending['state'], nonce=pending['nonce'], code_challenge_method='S256',
                  code_challenge=base64.urlsafe_b64encode(hashlib.sha256(pending['verifier'].encode()).digest()).decode().rstrip('='))
    if not pending['client_id']:
        params['agent_name_hint'] = 'AI 视频生产平台'
    server.user_id = user_id
    server.pending_state = pending['state']
    server.complete = False

    def listen():
        try:
            while not server.complete and time.time() < pending['expires']:
                if _read(name).get('pending', {}).get('state') != pending['state']:
                    break
                server.handle_request()
        finally:
            server.server_close()
    threading.Thread(target=listen, daemon=True).start()
    return {'url': ISSUER + '/api/accounts/authorize?' + urlencode(params)}


class _Callback(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 回调中有授权码，禁止记录 query string。

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path != '/auth/callback' or query.get('state', [''])[0] != self.server.pending_state:
            self.send_error(400)
            return
        try:
            finish_login(self.server.user_id, query)
            message = 'ChatGPT connected. You can close this window and return to the platform.'
        except ChatGPTError:
            message = 'ChatGPT connection failed. Return to the platform for details.'
        self.server.complete = True
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(message.encode())


def finish_login(user_id, query):
    name = _account(user_id)
    with _lock(name):
        data = _read(name)
        pending = data.get('pending', {})
        if (pending.get('expires', 0) < time.time() or pending.get('consumed')
                or not secrets.compare_digest(pending.get('state', '').encode(), query.get('state', [''])[0].encode())):
            raise ChatGPTError('授权已过期或已使用，请重新连接')
        pending['consumed'] = True
        _write(name, data)
    try:
        if query.get('error'):
            raise ChatGPTError('授权被取消，请重新连接并允许使用 ChatGPT 套餐')
        if not query.get('code', [''])[0]:
            raise ChatGPTError('授权回调缺少授权码，请重新连接')
        client_id = query.get('client_id', [pending['client_id']])[0]
        if not client_id or client_id == 'dynamic_agent_client' or (pending['client_id'] and client_id != pending['client_id']):
            raise ChatGPTError('授权未返回匹配的客户端标识')
        # 注册已经成功时先保留 client_id，授权码交换失败后仍可复用。
        with _lock(name):
            data = _read(name)
            if data.get('pending', {}).get('state') != pending['state']:
                raise ChatGPTError('授权已被更新的连接替代')
            if not data.get('client_id'):
                data['client_id'] = client_id
                _write(name, data)
        tokens = _json_request(TOKEN_URL, dict(grant_type='authorization_code', client_id=client_id,
            code=query.get('code', [''])[0], code_verifier=pending['verifier'],
            redirect_uri=pending['redirect'], resource=RESOURCE), form=True)
        claims = _verify(tokens, client_id, pending['nonce'])
        if pending['subject'] and claims['sub'] != pending['subject']:
            raise ChatGPTError('返回的账号与原连接不一致，请选择更换账号')
        if not tokens.get('access_token'):
            raise ChatGPTError('授权未返回访问令牌')
        with _lock(name):
            data = _read(name)
            if data.get('pending', {}).get('state') != pending['state']:
                raise ChatGPTError('授权已取消或被另一连接替代')
            _write(name, {**tokens, 'client_id': client_id, 'subject': claims['sub'],
                          'email': claims.get('email', ''), 'expires_at': time.time() + tokens.get('expires_in', 3600)})
    except ChatGPTError as exc:
        with _lock(name):
            data = _read(name)
            if data.get('pending', {}).get('state') == pending['state']:
                data.pop('pending', None)
                data['error'] = str(exc)
                _write(name, data)
        raise


def access_token(user_id):
    name = _account(user_id)
    # 旋转 refresh token 必须串行到新凭据落盘，跨进程锁覆盖这一次网络交换。
    with _lock(name):
        data = _read(name)
        if not data.get('access_token') or 'chatgpt.tokens.use.direct' not in data.get('scope', '').split():
            raise ChatGPTError('请先连接 ChatGPT 并授权使用套餐')
        if data.get('expires_at', 0) < time.time() + 60:
            if not data.get('refresh_token'):
                raise ChatGPTError('登录已过期，请重新授权')
            tokens = _json_request(TOKEN_URL, dict(grant_type='refresh_token', client_id=data['client_id'],
                refresh_token=data['refresh_token'], resource=RESOURCE), form=True)
            if not tokens.get('access_token') or not tokens.get('refresh_token'):
                raise ChatGPTError('授权刷新未完成，请重新连接')
            data.update(tokens)
            data['expires_at'] = time.time() + tokens.get('expires_in', 3600)
            _write(name, data)
        if 'chatgpt.tokens.use.direct' not in data.get('scope', '').split():
            raise ChatGPTError('套餐使用授权已撤回，请重新连接')
        return data['access_token']


def disconnect(user_id):
    name = _account(user_id)
    with _lock(name):
        data = _read(name)
        confirmed = True
        if data.get('refresh_token'):
            try:
                _json_request(ISSUER + '/api/accounts/oauth/revoke', dict(token=data['refresh_token'],
                    token_type_hint='refresh_token', client_id=data['client_id']), form=True)
            except ChatGPTError:
                confirmed = False
        _write(name, {k: data[k] for k in ('client_id', 'subject', 'email') if k in data})
    return {'note': '已断开连接' if confirmed else '本机已断开，远端撤销未确认，请在 ChatGPT 设置中断开此应用'}


def models(user_id):
    data = _json_request(RESOURCE + '/models', token=access_token(user_id))
    return [{'id': item['slug'], 'name': item.get('display_name') or item['slug']}
            for item in data.get('models', []) if item.get('visibility') == 'list' and item.get('slug')]


def infer(user_id, model, prompt):
    token = access_token(user_id)
    body = {'model': model, 'store': False, 'stream': True,
            'instructions': '根据用户提供的生产工具清单返回制作计划，只输出 JSON 数组。',
            'input': [{'role': 'user', 'content': prompt}]}
    request = Request(RESOURCE + '/responses', json.dumps(body).encode(),
                      {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    chunks = []
    started = time.monotonic()
    try:
        with urlopen(request, timeout=120) as response:
            size = 0
            for raw in response:
                size += len(raw)
                if time.monotonic() - started > 120:
                    raise ChatGPTError('计划生成超时，请稍后查询结果或检查套餐用量')
                if size > 2_000_000:
                    raise ChatGPTError('计划输出过长，请缩小目标后重新生成')
                line = raw.decode('utf-8').strip()
                if not line.startswith('data: '):
                    continue
                event = json.loads(line[6:])
                kind = event.get('type')
                if kind == 'response.output_text.delta':
                    chunks.append(event.get('delta', ''))
                elif kind == 'response.completed':
                    return ''.join(chunks)
                elif kind in ('response.failed', 'response.incomplete', 'error'):
                    code = (event.get('response', {}).get('error') or event.get('error') or {}).get('code', '')
                    raise ChatGPTError('ChatGPT 生成未完成，可能已达套餐额度或授权失效。请在 ChatGPT 设置查看用量。' if 'subscription' in code else 'ChatGPT 未完成本次计划，请检查账号状态')
    except (HTTPError, URLError, OSError, ValueError, UnicodeError):
        raise ChatGPTError('ChatGPT 连接中断，结果未确认。本次请求不会自动重发，请查看套餐用量后决定是否重新生成') from None
    raise ChatGPTError('连接结束但未收到完成确认，请重新检查账号状态')


def draft_name(user_id, key):
    return f'draft-{int(user_id)}-{uuid.UUID(key)}'
