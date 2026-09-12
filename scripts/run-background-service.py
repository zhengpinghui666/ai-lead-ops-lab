"""Console-free Windows Task Scheduler entry point with normal HTTP logging."""
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parents[1]
logs = root / 'data' / 'service-logs'
logs.mkdir(parents=True, exist_ok=True)
sys.stdout = (logs / 'stdout.log').open('a', encoding='utf-8', buffering=1)
sys.stderr = (logs / 'stderr.log').open('a', encoding='utf-8', buffering=1)
sys.path.insert(0, str(root))
try:
    runpy.run_path(str(root / 'server.py'), run_name='__main__')
except BaseException:
    import traceback
    traceback.print_exc()
    raise
