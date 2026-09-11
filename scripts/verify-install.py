"""Smoke-test an installation with a fresh temporary DB and optional blank Chrome.

Never logs in, reads existing profiles, starts a collector or sends messages.
Only the child server created here is stopped. Its temporary directory is removed.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import runtime


def verify(with_browser=False):
    report = runtime.doctor()
    if not report['core_ready'] or with_browser and not report['browser_dependencies_ready']:
        raise RuntimeError('依赖未齐备；先运行 python manage.py doctor --require-browser')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    with tempfile.TemporaryDirectory(prefix='clubops-install-') as folder:
        root = Path(folder)
        data = root / '隔离 data'
        env = {**os.environ, 'CLUBOPS_DATA_DIR': str(data), 'LEADOPS_PORT': '0',
               'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
        log = root / 'server.log'
        with log.open('wb') as output:
            process = subprocess.Popen([sys.executable, str(BASE / 'server.py')], cwd=root, env=env,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=output, creationflags=flags)
            try:
                deadline = time.monotonic() + 20
                address = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('隔离服务未能启动；' + log.read_text(encoding='utf-8')[-1500:])
                    match = re.search(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})/', log.read_text(encoding='utf-8'))
                    if match:
                        address = match.group(0)
                        break
                    time.sleep(0.1)
                if address is None:
                    raise TimeoutError('隔离服务启动超过 20 秒')
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(address + 'api/state?mode=live', timeout=5) as response:
                    state = json.load(response)
                for name in ('videos', 'comments', 'leads', 'jobs', 'messages'):
                    if len(state[name]) != 0:
                        raise AssertionError('新建隔离库出现非空业务记录：' + name)
                if 'semantic' not in state or 'uid_messaging' not in state:
                    raise AssertionError('响应缺少当前版本的模型或 UID 通道状态')
                with opener.open(address, timeout=5) as response:
                    html = response.read(1024 * 1024).decode('utf-8')
                if 'app.js' not in html or 'app.css' not in html:
                    raise AssertionError('前端资源入口不完整')
                report['isolated_server'] = {'started': True, 'empty_business_tables': True,
                                             'current_api_fields': True, 'front_page': True}
                # Real process check: a duplicate instance must fail before touching the DB.
                duplicate_env = {**env, 'LEADOPS_PORT': '0'}
                duplicate = subprocess.run([sys.executable, str(BASE / 'server.py')], cwd=root, env=duplicate_env,
                                           capture_output=True, timeout=10, creationflags=flags)
                if duplicate.returncode == 0 or '数据目录' not in duplicate.stderr.decode('utf-8', errors='replace'):
                    raise AssertionError('数据目录互斥未生效')
                report['isolated_server']['duplicate_data_directory_rejected'] = True
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        report['isolated_server']['own_process_stopped'] = True
        if with_browser:
            node, package = runtime.dependencies()
            script = """const path=require('node:path');
const {chromium}=require(process.argv[1]);
const options=require(path.join(process.argv[2],'browser_config.cjs'))();
(async()=>{let browser;try{browser=await chromium.launch({...options,headless:true,timeout:25000});
const page=await browser.newPage();await page.goto('about:blank');
process.stdout.write(JSON.stringify({version:browser.version(),page:page.url(),existing_profile_used:false}));
}finally{await browser?.close();}})().catch(()=>{process.exitCode=2;});"""
            result = subprocess.run([node, '-e', script, str(package), str(BASE)], cwd=root, env=env,
                                    capture_output=True, timeout=40, creationflags=flags)
            if result.returncode:
                raise RuntimeError('隔离 Chrome 空白页启动失败；工作台测试已结束')
            report['blank_browser'] = json.loads(result.stdout)
            report['browser_launch_tested'] = True
        report['external_connections_tested'] = False
    report['temporary_data_removed'] = True
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-browser', action='store_true', help='Launch a fresh headless Chrome on about:blank')
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.with_browser), ensure_ascii=False, indent=2))
    except (OSError, RuntimeError, ValueError, AssertionError, subprocess.TimeoutExpired, TimeoutError) as exc:
        print(f'安装验证未完成：{exc}', file=sys.stderr)
        raise SystemExit(2)
