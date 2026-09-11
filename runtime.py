"""Portable dependency discovery and one-server-per-data-directory lock."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

BASE = Path(__file__).resolve().parent


def data_dir():
    return Path(os.environ.get('CLUBOPS_DATA_DIR') or BASE / 'data').expanduser().resolve()


def dependencies():
    # An explicit override is authoritative: never silently use another runtime.
    node = os.environ.get('CLUBOPS_NODE') or shutil.which('node') or ''
    package = Path(os.environ.get('CLUBOPS_PLAYWRIGHT') or BASE / 'node_modules/playwright').expanduser().resolve()
    return node, package


def chrome_path():
    override = os.environ.get('CLUBOPS_CHROME')
    if override:
        return str(Path(override).expanduser().resolve())
    if sys.platform == 'win32':
        candidates = [Path(os.environ[key]) / 'Google/Chrome/Application/chrome.exe'
                      for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA') if os.environ.get(key)]
    elif sys.platform == 'darwin':
        candidates = [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]
    else:
        candidates = [Path('/opt/google/chrome/chrome')]
    return next((str(p) for p in candidates if p.is_file()), '')


def doctor():
    node, package = dependencies()
    result = {'python': {'path': sys.executable, 'version': sys.version.split()[0], 'ok': sys.version_info >= (3, 10)},
              'sqlite': {'version': sqlite3.sqlite_version, 'ok': sqlite3.sqlite_version_info >= (3, 35, 0)},
              'node': {'path': node, 'ok': False},
              'playwright': {'path': str(package), 'ok': False},
              'chrome': {'path': chrome_path(), 'ok': False},
              'data_dir': str(data_dir()), 'external_connections_tested': False}
    result['chrome']['ok'] = bool(result['chrome']['path']) and Path(result['chrome']['path']).is_file()
    if node:
        try:
            proc = subprocess.run([node, '-e',
                "process.stdout.write(JSON.stringify({node:process.versions.node,playwright:require(process.argv[1]+'/package.json').version,loadable:!!require(process.argv[1]).chromium}))",
                str(package)], capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if proc.returncode == 0:
                info = json.loads(proc.stdout)
                result['node'].update(version=info['node'], ok=int(info['node'].split('.')[0]) >= 20)
                result['playwright'].update(version=info['playwright'], ok=info['loadable'] is True and info['playwright'] == '1.62.1')
            else:
                version = subprocess.run([node, '--version'], capture_output=True, text=True, timeout=5,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.strip()
                result['node'].update(version=version, ok=int(version.lstrip('v').split('.')[0]) >= 20)
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            result['node']['detail'] = '无法确认 Node 或 Playwright；检查显式路径与项目依赖'
    result['core_ready'] = result['python']['ok'] and result['sqlite']['ok']
    result['browser_dependencies_ready'] = result['core_ready'] and all(result[k]['ok'] for k in ('node', 'playwright', 'chrome'))
    result['browser_launch_tested'] = False
    return result


@contextmanager
def data_lock(directory):
    """Nonblocking OS lock. The persistent lock file must never be deleted."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / '.clubops.lock').open('a+b')
    locked = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError('该数据目录已由另一份 ClubOps 服务使用；请使用独立数据目录') from exc
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
