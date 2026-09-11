"""Local maintenance only. No platform requests, credential reads or automatic tasks."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile

import runtime

BASE = Path(__file__).resolve().parent
DATABASES = ('clubops-live.db', 'clubops-demo-valorant.db', 'clubops-demo.db')


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def read_json(path):
    with Path(path).open('rb') as stream:
        value = stream.read(262145)
    if len(value) > 262144:
        raise ValueError('清单过大')
    return json.loads(value)


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def check_database(path):
    with closing(readonly(path)) as connection:
        deadline = time.monotonic() + 30
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('数据库完整性检查失败')


def fresh_directory(path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise ValueError('目标必须是尚不存在的新目录；不会覆盖已有数据')
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()


def backup(data, destination):
    """SQLite's backup API includes committed WAL data; each DB is its own snapshot."""
    data = Path(data).resolve()
    sources = [data / name for name in DATABASES if (data / name).exists()]
    if not sources:
        raise ValueError('没有找到 ClubOps 数据库；不会创建空数据库或空备份')
    if any(p.is_symlink() or p.resolve().parent != data or not p.is_file() for p in sources):
        raise ValueError('数据库必须是数据目录内的普通文件')
    output = fresh_directory(destination)
    entries = []
    for source in sources:
        target = output / source.name
        deadline = time.monotonic() + 30
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError('备份超过 30 秒；不完整目录无有效 manifest.json，请使用新目录重试')
        with closing(readonly(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            # Make the snapshot self-contained, even when the source uses WAL.
            dst.execute('PRAGMA journal_mode=DELETE')
        check_database(target)
        entries.append({'name': source.name, 'sha256': digest(target), 'bytes': target.stat().st_size})
    manifest = {'format': 'clubops-db-backup-v1', 'created_at': datetime.now(timezone.utc).isoformat(),
                'contains_private_business_data': True, 'credentials_included': False,
                'snapshot_scope': 'each database independently; committed records only', 'files': entries}
    write_json(output / 'manifest.json', manifest)
    return {'destination': str(output), 'files': entries, 'credentials_included': False}


def restore(source, destination):
    source = Path(source).resolve()
    manifest = read_json(source / 'manifest.json')
    if not isinstance(manifest, dict) or manifest.get('format') != 'clubops-db-backup-v1':
        raise ValueError('不是有效的 ClubOps 数据库备份')
    entries = manifest.get('files')
    if not isinstance(entries, list) or not 1 <= len(entries) <= len(DATABASES):
        raise ValueError('备份文件清单无效')
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or item.get('name') not in DATABASES or item['name'] in seen:
            raise ValueError('备份包含重复或非预期数据库名称')
        seen.add(item['name'])
        path = source / item['name']
        if path.is_symlink() or path.resolve().parent != source or not path.is_file():
            raise ValueError('备份文件缺失或不是目录内的普通文件')
        if not isinstance(item.get('bytes'), int) or isinstance(item['bytes'], bool) or item['bytes'] != path.stat().st_size:
            raise ValueError('备份文件长度不符')
        if not re.fullmatch('[0-9a-f]{64}', str(item.get('sha256', ''))) or digest(path) != item['sha256']:
            raise ValueError('备份哈希不符；停止恢复')
        check_database(path)
    output = fresh_directory(destination)
    for item in entries:
        target = output / item['name']
        with (source / item['name']).open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if digest(target) != item['sha256']:
            raise ValueError('复制期间备份内容发生变化；该目标目录不可使用，请检查后另选新目录')
        check_database(target)
    write_json(output / 'restore-receipt.json', {'format': 'clubops-restore-v1', 'files': entries,
               'restored_at': datetime.now(timezone.utc).isoformat(), 'services_started': False, 'credentials_restored': False})
    return {'destination': str(output), 'files': entries, 'services_started': False, 'credentials_restored': False}


def source_files(base):
    base = Path(base).resolve()
    names = read_json(base / 'source-files.json')
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise ValueError('源码白名单无效')
    paths = []
    for name in names:
        pure = PurePosixPath(name)
        if (pure.is_absolute() or '..' in pure.parts or '\\' in name or ':' in name or name != pure.as_posix()
                or pure.parts[0] in ('data', 'private', 'artifacts', 'archive', 'node_modules', 'dist', '.git')
                or any(p.startswith('.') and p != '.gitignore' for p in pure.parts)
                or pure.suffix in ('.db', '.bak', '.sqlite', '.sqlite3')):
            raise ValueError('源码白名单包含禁止打包的路径')
        path = base / name
        if not path.is_file() or not path.resolve().is_relative_to(base):
            raise ValueError(f'源码文件缺失或越出项目目录：{name}')
        if any(p.is_symlink() for p in (path, *path.parents) if p != base and p.is_relative_to(base)):
            raise ValueError(f'源码文件不能经过符号链接：{name}')
        paths.append((name, path))
    return paths


def package(destination, base=BASE):
    paths = source_files(base)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('源码包已存在；请选择新文件名')
    if destination.resolve() in [p.resolve() for _, p in paths]:
        raise ValueError('输出不能覆盖源码')
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in paths:
            raw = path.read_bytes()
            archive.writestr(name, raw)
            entries.append({'name': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
        archive.writestr('SOURCE-MANIFEST.json', json.dumps({'format': 'clubops-source-v1', 'files': entries}, indent=2))
    return {'destination': str(destination), 'files': len(entries), 'sha256': digest(destination),
            'database_included': False, 'credentials_included': False, 'runtime_binaries_included': False}


def run_tests(suite):
    commands = []
    if suite in ('all', 'python'):
        commands.append([sys.executable, '-m', 'unittest', 'discover', '-p', 'test_*.py', '-v'])
    node, _ = runtime.dependencies()
    if suite in ('all', 'node'):
        if not node:
            raise ValueError('未找到 Node；安装 Node 或设置 CLUBOPS_NODE')
        script = read_json(BASE / 'package.json')['scripts']['test']
        for segment in script.split(' && '):
            match = re.fullmatch(r'node (test_[a-z_]+\.cjs)', segment)
            if not match:
                raise ValueError('package.json 测试命令不在支持范围')
            commands.append([node, match[1]])
    env = {**os.environ, 'CLUBOPS_TEST_PYTHON': sys.executable, 'PYTHONIOENCODING': 'utf-8'}
    for command in commands:
        print('Running: ' + ' '.join(command), flush=True)
        result = subprocess.run(command, cwd=BASE, env=env)
        if result.returncode:
            return result.returncode
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('doctor', help='Read-only local dependency check; does not start browsers or services')
    check.add_argument('--require-browser', action='store_true')
    save = commands.add_parser('backup', help='Online SQLite snapshot; no login profiles or credentials')
    save.add_argument('--data-dir', type=Path, default=runtime.data_dir())
    save.add_argument('--to', type=Path, required=True)
    load = commands.add_parser('restore', help='Restore only into a new directory; never start services')
    load.add_argument('--from', dest='source', type=Path, required=True)
    load.add_argument('--to', type=Path, required=True)
    bundle = commands.add_parser('package', help='Build an allowlisted source ZIP without business data')
    bundle.add_argument('--to', type=Path, required=True)
    tests = commands.add_parser('test', help='Run local synthetic tests using this Python and configured Node')
    tests.add_argument('--suite', choices=['all', 'python', 'node'], default='all')
    args = parser.parse_args()
    try:
        if args.command == 'test':
            return run_tests(args.suite)
        if args.command == 'doctor':
            result = runtime.doctor()
        elif args.command == 'backup':
            result = backup(args.data_dir, args.to)
        elif args.command == 'restore':
            result = restore(args.source, args.to)
        else:
            result = package(args.to)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == 'doctor':
            return 0 if result['browser_dependencies_ready' if args.require_browser else 'core_ready'] else 2
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, TimeoutError) as exc:
        print(f'操作未完成：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
