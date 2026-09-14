import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('watch_script',Path(__file__).parent/'scripts/incident-watch.py')
watch=importlib.util.module_from_spec(spec);spec.loader.exec_module(watch)


class WatcherTests(unittest.TestCase):
    def test_busy_status_file_does_not_end_watcher_and_next_pulse_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with patch.object(Path,'replace',side_effect=PermissionError('synthetic busy file')),patch.object(watch.time,'sleep'):
                self.assertFalse(watch.publish_status(root,dict(pid=1)))
            self.assertEqual(list(root.glob('*.tmp')),[])
            self.assertTrue(watch.publish_status(root,dict(pid=2)))
            self.assertEqual(json.loads((root/'status.json').read_text())['pid'],2)

    def test_ledger_read_error_is_reported_without_crash_or_exception_details(self):
        with tempfile.TemporaryDirectory() as td:
            bridge=Mock(directory=Path(td),data_dir=Path(td))
            bridge.status.side_effect=OSError('must-not-persist-detail')
            with patch.object(watch,'health',return_value={}):
                self.assertTrue(watch.pulse(bridge,8765))
            value=(Path(td)/'status.json').read_text()
            self.assertNotIn('must-not-persist-detail',value)
            self.assertEqual(json.loads(value)['watcher_error'],'OSError')
            self.assertFalse(json.loads(value)['enabled'])

if __name__=='__main__':unittest.main()
