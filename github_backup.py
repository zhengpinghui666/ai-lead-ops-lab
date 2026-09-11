"""Back up the explicit source manifest to a private, personal GitHub repository."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import manage

BASE = Path(__file__).resolve().parent
BRANCH = 'codex/backup'
MANIFEST = 'SOURCE-MANIFEST.json'
PRIVATE_PARTS = {'data', 'private', 'artifacts', 'archive', 'node_modules', 'dist',
                 '.git', '.tools', '.wrangler', '__pycache__'}
SECRET_PATTERNS = [
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    re.compile(rb'\bsk-[A-Za-z0-9_-]{32,}\b'),
    re.compile(rb'(?i)(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)'
               rb'[\s\x22\x27]*[:=][\s\x22\x27]*([A-Za-z0-9_./+\-]{24,})'),
]


class BackupError(Exception):
    pass


def safe_content(name, raw):
    """Report only filenames, never matched values."""
    parts = Path(name).parts
    if (not parts or any(c in name for c in '\r\n\t\0\\:') or name.startswith('/')
            or any(p in PRIVATE_PARTS or p in ('..', '.') for p in parts)
            or any(p.startswith('.') and p != '.gitignore' for p in parts)
            or Path(name).suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.bak', '.pem', '.key'}):
        raise BackupError(f'禁止备份此路径：{name}')
    if len(raw) > 8 * 1024 * 1024:
        raise BackupError(f'源码文件超过 8 MiB：{name}')
    for pattern in SECRET_PATTERNS:
        match = pattern.search(raw)
        if match and (not match.lastindex or len(set(match.group(1))) > 8):
            raise BackupError(f'疑似凭据，停止上传；请在本机检查：{name}')


def snapshot(base=BASE):
    paths = manage.source_files(base)
    files = {}
    for name, path in paths:
        raw = path.read_bytes()
        safe_content(name, raw)
        files[name] = raw
    if [name for name, _ in manage.source_files(base)] != list(files):
        raise BackupError('源码清单在备份期间改变，请重试')
    if any(path.read_bytes() != files[name] for name, path in paths):
        raise BackupError('源码正在修改，本次暂不提交；请稍后重试')
    entries = [{'name': n, 'sha256': hashlib.sha256(b).hexdigest(), 'bytes': len(b)}
               for n, b in sorted(files.items())]
    files[MANIFEST] = (json.dumps({'format': 'clubops-source-v1', 'files': entries},
                                 ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    return files


def command(args, *, cwd=BASE, env=None, data=None, timeout=90, check=True):
    try:
        result = subprocess.run(args, cwd=cwd, input=data, capture_output=True, timeout=timeout,
                                env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackupError(f'{args[0]} 未完成：{type(error).__name__}') from None
    if check and result.returncode:
        raise BackupError(f'{args[0]} {args[1] if len(args)>1 else ""} 失败，退出码 {result.returncode}')
    return result


def github_environment(owner):
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'Never',
           'GH_PROMPT_DISABLED': '1', 'GH_HOST': 'github.com'}
    if command(['gh', 'auth', 'status', '--hostname', 'github.com'], env=env, check=False).returncode == 0:
        return env
    result = command(['git', '-c', 'credential.interactive=false', 'credential', 'fill'],
                     env=env, data=f'protocol=https\nhost=github.com\nusername={owner}\n\n'.encode(), check=False)
    credentials = dict(line.split('=', 1) for line in result.stdout.decode().splitlines() if '=' in line)
    if result.returncode or not credentials.get('password'):
        raise BackupError('GitHub 未登录；请通过 gh auth login 或 Git 凭据管理器登录后重试')
    # Standard Git credential lookup, in memory only; never persist or print the token.
    env['GH_TOKEN'] = credentials['password']
    return env


def api(endpoint, env, payload=None, *, check=True):
    args = ['gh', 'api', '--hostname', 'github.com', endpoint]
    if payload is not None:
        args += ['--method', 'POST', '--input', '-']
    result = command(args, env=env, data=json.dumps(payload).encode() if payload is not None else None,
                     check=check)
    if result.returncode:
        return None
    return json.loads(result.stdout)


def private_repository(repo, env, create=False):
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repo):
        raise BackupError('仓库格式必须是个人账号/仓库名')
    owner, name = repo.split('/')
    user = api('user', env)
    if user['login'].lower() != owner.lower():
        raise BackupError('登录账号与指定的个人备份仓库所有者不同')
    info = api('repos/' + repo, env, check=not create)
    if info is None and create:
        info = api('user/repos', env, {'name': name, 'private': True, 'auto_init': False,
                    'description': 'ClubOps AI 获客系统源码备份（不含业务数据、登录态和密钥）'})
    if (not info or info.get('private') is not True or info.get('fork')
            or info.get('full_name', '').lower() != repo.lower()
            or info.get('owner', {}).get('id') != user['id']):
        raise BackupError('备份仅允许推送到当前账号名下的独立私有仓库')
    return info


@contextmanager
def backup_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'backup.lock'
    try:
        handle = path.open('x', encoding='utf-8')
    except FileExistsError:
        raise BackupError('已有备份锁；先确认旧进程是否仍在运行，不自动抢锁') from None
    try:
        with handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        path.unlink()


def write_receipt(folder, value):
    target = folder / 'last-run.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(target)


def backup_git(files, folder, url, repository_id, env, *, branch=BRANCH):
    """A dedicated bare repo and temporary index never touch the user's checkout."""
    gitdir = folder / 'repository.git'
    marker = folder / 'repository.json'
    identity = {'format': 'clubops-github-source-v1', 'repository_id': repository_id,
                'url': url, 'branch': branch}
    if gitdir.exists():
        if not marker.is_file() or json.loads(marker.read_text()) != identity:
            raise BackupError('本地备份仓库身份不匹配，停止推送')
    else:
        command(['git', 'init', '--bare', '--initial-branch=' + branch, str(gitdir)], env=env)
        marker.write_text(json.dumps(identity), encoding='utf-8')
        command(['git', '--git-dir=' + str(gitdir), 'remote', 'add', 'origin', url], env=env)
    git = ['git', '--git-dir=' + str(gitdir), '-c', 'core.autocrlf=false', '-c', 'credential.interactive=false']
    def run(*args, **kwargs):
        return command([*git, *args], env=env, **kwargs)
    if run('remote', 'get-url', '--push', 'origin').stdout.decode().strip() != url:
        raise BackupError('备份远端地址被改变，停止推送')
    ref = 'refs/heads/' + branch
    local = run('rev-parse', '--verify', ref, check=False)
    head = local.stdout.decode().strip() if local.returncode == 0 else None
    remote_line = run('ls-remote', 'origin', ref).stdout.decode().strip()
    remote = remote_line.split()[0] if remote_line else None
    if remote and (not head or run('merge-base', '--is-ancestor', remote, head, check=False).returncode):
        raise BackupError('远端存在本机未包含的提交；保留双方历史，需先处理分叉')
    if not head and run('ls-remote', 'origin').stdout.strip():
        raise BackupError('首次备份要求空仓库，不自动覆盖已有仓库')

    with tempfile.TemporaryDirectory(prefix='index-', dir=folder) as temp:
        index_env = {**env, 'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        entries = []
        for name, raw in sorted(files.items()):
            object_id = run('hash-object', '-w', '--stdin', data=raw).stdout.decode().strip()
            entries.append(f'100644 {object_id}\t{name}\0'.encode('utf-8'))
        command([*git, 'read-tree', '--empty'], env=index_env)
        command([*git, 'update-index', '-z', '--index-info'], env=index_env, data=b''.join(entries))
        tree = command([*git, 'write-tree'], env=index_env).stdout.decode().strip()
    old_tree = run('rev-parse', head + '^{tree}').stdout.decode().strip() if head else None
    if tree != old_tree:
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        commit = ['git', '--git-dir=' + str(gitdir), '-c', 'user.name=ClubOps Backup',
                  '-c', 'user.email=clubops-backup@users.noreply.github.com',
                  'commit-tree', tree, '-m', 'Source backup ' + stamp]
        if head:
            commit += ['-p', head]
        new_head = command(commit, env=env).stdout.decode().strip()
        run('update-ref', ref, new_head, head or '0' * 40)
        head = new_head

    if remote == head:
        return {'status': 'unchanged', 'commit': head, 'tree': tree, 'files': len(files) - 1}
    # Validate every unpublished snapshot, including commits left by a failed push.
    pending = run('rev-list', head, *(['^' + remote] if remote else [])).stdout.decode().splitlines()
    for commit_id in pending:
        verify_commit(git, commit_id, env)
    run('push', 'origin', ref + ':' + ref, timeout=120)
    confirmed = run('ls-remote', 'origin', ref).stdout.decode().split()
    if not confirmed or confirmed[0] != head:
        raise BackupError('推送结果未确认；保留本地提交，下次核对后重试')
    return {'status': 'pushed', 'commit': head, 'tree': tree, 'files': len(files) - 1}


def verify_commit(git, commit_id, env):
    rows = command([*git, 'ls-tree', '-rz', commit_id], env=env).stdout.split(b'\0')
    blobs = {}
    for row in filter(None, rows):
        meta, name = row.split(b'\t', 1)
        mode, kind, oid = meta.split()
        if mode != b'100644' or kind != b'blob':
            raise BackupError('未发布的备份含非普通文件，停止推送')
        blobs[name.decode('utf-8')] = oid.decode()
    def read(name):
        return command([*git, 'cat-file', 'blob', blobs[name]], env=env).stdout
    if MANIFEST not in blobs or 'source-files.json' not in blobs:
        raise BackupError('备份提交缺少源码清单')
    names = json.loads(read('source-files.json'))
    manifest = json.loads(read(MANIFEST))
    if set(blobs) != set(names) | {MANIFEST} or set(names) != {x['name'] for x in manifest['files']}:
        raise BackupError('未发布的备份含清单之外的文件')
    for entry in manifest['files']:
        raw = read(entry['name'])
        safe_content(entry['name'], raw)
        if len(raw) != entry['bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise BackupError('未发布的备份文件与清单哈希不符')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='Personal GitHub owner/repository')
    parser.add_argument('--create-private', action='store_true', help='Create the authorized private repository if absent')
    parser.add_argument('--dry-run', action='store_true', help='Check source only; no authentication or network')
    args = parser.parse_args()
    folder = BASE / 'data' / 'github-backup'
    try:
        files = snapshot()
        if args.dry_run:
            print(json.dumps({'status': 'checked', 'files': len(files) - 1,
                              'bytes': sum(map(len, files.values()))}, ensure_ascii=False))
            return 0
        with backup_lock(folder):
            env = github_environment(args.repo.split('/')[0])
            info = private_repository(args.repo, env, args.create_private)
            url = 'https://github.com/' + info['full_name'] + '.git'
            result = backup_git(files, folder, url, info['id'], env)
            result.update(repository=info['html_url'], private=True, branch=BRANCH,
                          checked_at=datetime.now(timezone.utc).isoformat())
            write_receipt(folder, result)
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (BackupError, ValueError, OSError, KeyError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
