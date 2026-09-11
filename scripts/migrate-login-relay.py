"""Offline, explicit migration from an owned Worker to its owned Pages entry.

Preview is read-only. Apply requires a stopped service, verifies Cloudflare's
production binding and shared remote state, then invalidates old acceptance.
No SMS, phone token, browser or platform request is needed for the probe.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import clubops as app
import login_recovery as recovery
import login_relay as relay
import runtime


def plan(account, project, previous):
    if not isinstance(account, str) or not re.fullmatch(r'[a-f0-9]{32}', account):
        raise ValueError('Cloudflare 账号 ID 无效')
    if not isinstance(project, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?', project):
        raise ValueError('Pages 项目名无效')
    previous = relay.origin(previous)
    labels = urlsplit(previous).hostname.split('.')
    if len(labels) != 4 or labels[-2:] != ['workers', 'dev']:
        raise ValueError('迁移仅支持已绑定的 workers.dev 地址')
    target = relay.origin('https://' + project + '.pages.dev')
    private = relay.load()
    if private['origin'] not in (previous, target):
        raise ValueError('原配对地址已变化，未执行迁移')
    return {'status': 'already_migrated' if private['origin'] == target else 'ready_to_verify',
            'cloudflare_account': account, 'project': project, 'worker': labels[0],
            'from_origin': previous, 'to_origin': target,
            'account': recovery.config()['account'], 'preserve_pairing': True,
            'network_after_apply': 'direct', 'phone_retest_required': True}


def verify_project(proposal):
    # Read live configuration through Wrangler's existing encrypted OAuth login;
    # never export, parse or print its authorization credentials.
    try:
        import tomllib
    except ImportError:
        raise ValueError('迁移工具需要 Python 3.11 或更新版本') from None
    node, _ = runtime.dependencies()
    cli = BASE / 'integrations/login-relay/node_modules/wrangler/bin/wrangler.js'
    if not node or not cli.is_file():
        raise ValueError('请先安装中转目录中锁定的 Node 依赖')
    with tempfile.TemporaryDirectory(prefix='clubops-pages-config-') as directory:
        result = subprocess.run([node, str(cli), 'pages', 'download', 'config', proposal['project']],
            cwd=directory, capture_output=True, text=True, encoding='utf-8', timeout=45,
            env={**os.environ, 'CLOUDFLARE_ACCOUNT_ID': proposal['cloudflare_account'], 'WRANGLER_SEND_METRICS': 'false'},
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('无法核对当前账号的 Pages 配置，未发送配对凭证')
        value = tomllib.loads((Path(directory) / 'wrangler.toml').read_text(encoding='utf-8'))
    services = value.get('env', {}).get('production', {}).get('services')
    expected = {'binding': 'RELAY', 'service': proposal['worker']}
    if (value.get('name') != proposal['project'] or not isinstance(services, list)
            or len(services) != 1 or not isinstance(services[0], dict)
            or any(services[0].get(k) != v for k, v in expected.items())
            or services[0].get('environment') not in (None, '') or services[0].get('entrypoint')):
        raise ValueError('Pages 正式入口未绑定原中转 Worker，未发送配对凭证')


def candidate_call(proposal, private, route, body=None):
    # Only called after verify_project. This transport is direct with verified
    # TLS, exact host and routes, bounded response, and no redirects or retries.
    if route not in ('health', 'take'):
        raise ValueError('迁移检查操作无效')
    connection = http.client.HTTPSConnection(urlsplit(proposal['to_origin']).hostname, 443,
        timeout=8, context=ssl.create_default_context())
    try:
        connection.request('GET' if body is None else 'POST', '/v1/' + route,
            body=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + private['backend_token'], 'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096:
            raise ValueError('新入口的直连检查未通过')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('新入口响应格式无效')
        return value
    except Exception:
        raise ValueError('新入口的直连检查未通过，未迁移配对') from None
    finally:
        connection.close()


def verify_shared_state(proposal, private):
    original = relay.Relay(proposal['from_origin'])
    health = {'service': 'clubops-login-relay', 'version': 1}
    if original.call('health') != health or candidate_call(proposal, private, 'health') != health:
        raise ValueError('中转服务身份未确认')
    job_id = uuid.uuid4().hex
    # A separate synthetic account keeps this maintenance probe outside the
    # iPhone's normal-account match, so an arriving real SMS cannot be forwarded.
    try:
        created = original.call('login', {'id': job_id, 'account': 'migration-' + job_id})
        if created.get('id') != job_id or candidate_call(proposal, private, 'take', {'id': job_id}) != {'status': 'waiting'}:
            raise ValueError('新旧入口没有读取到同一个待处理任务')
    finally:
        # Cancel only this ID even when the create response is ambiguous. Never
        # cancel someone else's pending login; a 409 leaves migration unapplied.
        if original.call('cancel', {'id': job_id}) != {'status': 'cleared'}:
            raise ValueError('迁移检查任务尚未确认清理，未迁移配对')


def migrate(account, project, previous):
    # The normal daemon holds this OS lock for its entire lifetime. An idle
    # daemon still has admission races, so checking its HTTP status is not enough.
    with runtime.data_lock(app.DATA_DIR), relay.GUARD, runtime.data_lock(relay.path().parent):
        proposal = plan(account, project, previous)
        if proposal['status'] == 'already_migrated':
            return proposal
        if any(job.get('status') not in recovery.FINISHED for job in recovery._history()):
            raise ValueError('存在未结算的登录任务，请先正常启动并关闭服务')
        private = relay.load()
        verify_project(proposal)
        verify_shared_state(proposal, private)
        # Fail closed: acceptance and auto-recovery are disabled before the
        # atomic pairing replacement. An interrupted commit can never retain
        # old phone acceptance for a changed origin. Account and keys survive.
        recovery._write(app.DATA_DIR / recovery.CONFIG, {**recovery.config(), 'auto_recover': False})
        recovery._write(recovery._file('checks.json'), {})
        recovery._write(app.DATA_DIR / 'login-relay-network.json', {'http_proxy': ''})
        relay.write({**private, 'origin': proposal['to_origin']})
        return {**proposal, 'status': 'migrated', 'shared_state_verified': True,
                'auto_recover': False, 'phone_tested': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--from-origin', required=True)
    parser.add_argument('--apply', action='store_true', help='正常关闭服务后执行；默认仅预览')
    args = parser.parse_args()
    try:
        operation = migrate if args.apply else plan
        print(json.dumps(operation(args.account_id, args.project, args.from_origin), ensure_ascii=False))
    except Exception:
        # Neither Wrangler output, network errors nor file payloads are printed.
        raise SystemExit('迁移未完成：请确认服务已正常关闭、原地址未变化、授权和生产绑定有效；请在账号登录页核对当前配置。') from None


if __name__ == '__main__':
    main()
