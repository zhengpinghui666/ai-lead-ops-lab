"""Deploy the own-account relay with existing encrypted pairing, never print keys."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import clubops as app
import login_recovery
import login_relay
import runtime

ACCOUNT = re.compile(r'[a-f0-9]{32}')


def require_idle():
    base = 'http://127.0.0.1:' + str(int(os.getenv('LEADOPS_PORT', '8765')))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + '/api/service', timeout=5) as response:
        instance = json.load(response)
    if instance.get('service') != 'ClubOps' or Path(instance.get('data_directory', '')).resolve() != app.DATA_DIR.resolve():
        raise ValueError('本机服务不属于当前工作区，未执行部署')
    with opener.open(base + '/api/login-recovery', timeout=5) as response:
        current = json.load(response)
    if not current.get('available') or current.get('active') or current.get('collection_busy'):
        raise ValueError('请先结束本机登录与采集任务，再部署中转')


def invoke(arguments, account, payload=None):
    node, _ = runtime.dependencies()
    cli = BASE / 'integrations/login-relay/node_modules/wrangler/bin/wrangler.js'
    if not node or not cli.is_file():
        raise ValueError('请先安装中转目录中锁定的 Node 依赖')
    environment = {**os.environ, 'CLOUDFLARE_ACCOUNT_ID': account, 'WRANGLER_SEND_METRICS': 'false'}
    result = subprocess.run([node, str(cli), *arguments], cwd=cli.parents[3],
        input=payload, capture_output=True, text=True, encoding='utf-8', timeout=180, env=environment,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    # Wrangler error output can contain request context; do not relay it.
    if result.returncode:
        if re.search(r'\[code:\s*10034\]', (result.stdout or '') + (result.stderr or '')):
            raise ValueError('Cloudflare 错误 10034：请先验证注册邮箱，再重新部署；部署授权无需重复申请')
        raise ValueError('Cloudflare 命令未完成；请检查部署授权、默认子域名和资源配置')
    return result.stdout


def phone_submit(origin, token, body):
    connection = login_relay.connection(origin)
    try:
        connection.request('POST', '/v1/otp', body=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096 or json.loads(raw) != {'status': 'received'}:
            raise ValueError('手机角色的 HTTPS 合成提交未确认成功')
    finally:
        connection.close()


def probe(origin, private):
    relay = login_relay.Relay(origin)
    if relay.call('health') != {'service': 'clubops-login-relay', 'version': 1}:
        raise ValueError('部署后的服务身份未确认')
    job_id, code = uuid.uuid4().hex, f'{secrets.randbelow(1000000):06d}'
    started = False
    try:
        started = True
        created = relay.call('login', {'id': job_id, 'account': login_recovery.config()['account']})
        if created.get('id') != job_id:
            raise ValueError('合成任务编号未确认')
        phone_submit(origin, private['phone_token'], {'id': job_id, 'code': code, 'received_at': int(time.time() * 1000)})
        taken = relay.call('take', {'id': job_id})
        if taken.get('id') != job_id or taken.get('status') != 'received' or taken.get('code') != code:
            raise ValueError('合成验证码往返未确认')
        if relay.call('take', {'id': job_id}) != {'status': 'taken'}:
            raise ValueError('一次取回检查未通过')
    finally:
        code = None
        if started:
            relay.call('cancel', {'id': job_id})
    # This is a computer-originated HTTPS probe, never phone acceptance.
    return {'health': True, 'synthetic_https_roundtrip': True, 'take_once': True, 'iphone_tested': False}


def deploy(account, verify_only=False):
    if not isinstance(account, str) or not ACCOUNT.fullmatch(account):
        raise ValueError('Cloudflare 账号 ID 无效')
    # A successful CLI exit alone does not prove authentication (whoami returns
    # zero for unauthenticated users). Require the selected account in its list.
    who = invoke(['whoami'], account)
    if account not in who or 'not authenticated' in who.lower():
        raise ValueError('Wrangler 尚未获准访问指定账号，请先完成有效的部署登录')
    require_idle()
    private = login_relay.load()
    if verify_only:
        origin = private['origin']
        if not origin:
            raise ValueError('尚未绑定已部署地址，不能仅复核')
        output = invoke(['deployments', 'list'], account)
        if not re.search(r'\(100%\)\s*[a-f0-9-]{36}', output):
            raise ValueError('未确认当前已部署版本')
    else:
        output = invoke(['deploy'], account)
        addresses = set(re.findall(r'https://clubops-login-relay\.[a-z0-9-]+\.workers\.dev', output))
        if len(addresses) != 1:
            raise ValueError('未确认唯一的固定 Worker 地址；请核对 Cloudflare 默认子域名')
        origin = addresses.pop()
        if private['origin'] and private['origin'] != origin:
            raise ValueError('部署地址与既有配对不同，未发送本机配对凭证')
        payload = json.dumps({'BACKEND_TOKEN': private['backend_token'], 'PHONE_TOKEN': private['phone_token']})
        invoke(['secret', 'bulk'], account, payload)
        payload = None
        login_relay.bind(origin)
    receipt = {'status': 'deployed_pending_https', 'origin': origin, 'account_id': account,
               'created_at': time.time(), 'worker_sha256': hashlib.sha256((BASE / 'integrations/login-relay/relay.mjs').read_bytes()).hexdigest(),
               'iphone_script_sha256': hashlib.sha256((BASE / 'static/clubops-iphone.js').read_bytes()).hexdigest(),
               'checks': {}, 'real_sms_login_tested': False, 'network': login_relay.network()}
    versions = re.findall(r'Current Version ID:\s*([a-f0-9-]{36})', output)
    receipt['initial_deploy_version'] = versions[-1] if versions else None
    if verify_only:
        receipt['current_version'] = re.findall(r'\(100%\)\s*([a-f0-9-]{36})', output)[-1]
    login_recovery._write(login_recovery._file('deployment.json'), receipt)
    try:
        receipt['checks'] = probe(origin, private)
        login_recovery.check_relay()
    except Exception:
        receipt['status'] = 'deployed_https_check_failed'
        login_recovery._write(login_recovery._file('deployment.json'), receipt)
        raise ValueError('Cloudflare 已部署，但 HTTPS 检查未通过；请检查本机中转网络配置，再用 --verify-only 复核') from None
    receipt['status'] = 'deployed_and_https_verified'
    login_recovery._write(login_recovery._file('deployment.json'), receipt)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--deploy', action='store_true', help='Deploy with previously granted Cloudflare access')
    action.add_argument('--verify-only', action='store_true', help='Verify the bound deployment without uploading code or secrets again')
    arguments = parser.parse_args()
    try:
        print(json.dumps(deploy(arguments.account, verify_only=arguments.verify_only), ensure_ascii=False, indent=2))
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else '部署或 HTTPS 检查未完成，请核对账号授权和连接状态'
        print(message, file=sys.stderr)
        raise SystemExit(2)
