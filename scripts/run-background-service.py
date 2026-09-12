"""Console-free Windows Task Scheduler entry point with normal HTTP logging."""
from pathlib import Path
import argparse
import os
import runpy
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--node', help='Explicit installed Node path for the scheduled-task environment')
args = parser.parse_args()
if args.node:
    node = Path(args.node).resolve(strict=True)
    if not node.is_file():
        raise ValueError('Node path must be a file')
    os.environ['CLUBOPS_NODE'] = str(node)
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
