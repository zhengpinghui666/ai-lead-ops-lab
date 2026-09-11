import json
from pathlib import Path
import tempfile
import threading
import unittest
import shutil
import subprocess
from unittest.mock import patch

import captcha_runtime
import clubops as app
import collector
import collector_http as http
import collector_http_worker as worker


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='captcha-workflow-')
        self.old_dir = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()
        collector.ACTIVE.clear()

    def tearDown(self):
        collector.ACTIVE.clear()
        app.DATA_DIR = self.old_dir
        self.temp.cleanup()

    def create(self, transport='local_browser'):
        with patch('collector.threading.Thread.start'):
            return collector.start({'kind':'search','target':'合成验证码测试','request_id':'test',
                                    'transport':transport})['id']

    def test_evidence_and_counts_persist_without_images_or_tokens(self):
        task = self.create()
        for phase in ('detected','recognizing','verifying','accepted'):
            collector.record_verification(task, {'phase':phase,'transport':'local_browser','attempt_id':'test-1',
                'submissions':int(phase in ('verifying','accepted')), 'platform_verdict':'passed' if phase=='accepted' else 'unknown',
                'verdict_source':'platform_response_message' if phase=='accepted' else 'none', 'cookie':'DO_NOT_STORE', 'image':'DO_NOT_STORE', 'result':'DO_NOT_STORE'})
        state = collector.state()
        self.assertEqual(state['tasks'][0]['verification_counts'], {'attempted':1,'recovered':1})
        self.assertEqual(state['tasks'][0]['verification']['phase'], 'accepted')
        with app.db() as c:
            self.assertNotIn('DO_NOT_STORE', ''.join(r[0] for r in c.execute('SELECT snapshot FROM collection_diagnostics')))

    def test_unknown_and_cross_transport_events_rejected(self):
        task = self.create()
        for value in ({'phase':'done','transport':'local_browser'},
                      {'phase':'accepted','transport':'local_browser','submissions':0},
                      {'phase':'detected','transport':'http'},
                      {'phase':'detected','transport':'local_browser','reason':'secret=https://example.invalid'}):
            with self.assertRaises(ValueError):
                collector.record_verification(task, value)

    def test_resume_after_attempt_keeps_cumulative_submission_count(self):
        task = self.create()
        for attempt_id, phase, reason in (
                ('original', 'verifying', None),
                ('original', 'needs_review', 'acceptance_not_observed'),
                ('resume-check', 'detected', None),
                ('resume-check', 'needs_review', 'batch_attempt_limit')):
            collector.record_verification(task, dict(phase=phase,
                transport='local_browser', attempt_id=attempt_id, submissions=1,
                **({'reason':reason} if reason else {})))
        self.assertEqual(collector.state()['tasks'][0]['verification_counts'],
                         {'attempted':1,'recovered':0})
        with app.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM collection_diagnostics WHERE task_id=?", (task,)).fetchone()[0],4)

    def test_http_gate_enters_workflow_without_replaying_requests(self):
        task = self.create('http')
        events = []
        class Client:
            def page(self, *args, **kwargs):
                raise http.ReadError('needs_verification')
        with patch('captcha_engine.solve') as solve:
            worker.collect({'kind':'search','target':'合成','video_limit':1,'comment_limit':1,'page_concurrency':1},
                           events.append,threading.Event(),client=Client())
            solve.assert_not_called()
        for event in events:
            if event['type']=='verification':
                collector.record_verification(task,event['event'])
        latest=collector.state()['tasks'][0]
        self.assertEqual(latest['verification']['reason'],'http_challenge_not_available')
        self.assertEqual(latest['verification_counts'],{'attempted':0,'recovered':0})
        self.assertEqual(events[-1]['status'],'needs_verification')

    def test_manual_switch(self):
        with patch.dict('os.environ', {'CLUBOPS_CAPTCHA_MODE':'manual'}):
            self.assertEqual(captcha_runtime.configuration()['mode'],'manual')

    def test_accepted_requires_platform_evidence(self):
        base = dict(phase='accepted', transport='local_browser', attempt_id='test-pass', submissions=1)
        for extra in ({}, {'platform_verdict':'unknown'}, {'platform_verdict':'failed'},
                      {'platform_verdict':'passed', 'verdict_source':'none'},
                      {'platform_verdict':'passed', 'verdict_source':'ocr_prediction'}):
            with self.assertRaises(ValueError):captcha_runtime.clean_event({**base, **extra})
        valid = captcha_runtime.clean_event({**base, 'platform_verdict':'passed', 'verdict_source':'visible_platform_result'})
        self.assertEqual(valid['platform_verdict'], 'passed')

    def test_real_image_worker_through_collector_ledger_and_analysis(self):
        if not Path(captcha_runtime.configuration()['python']).is_file() or not shutil.which('node'):
            self.skipTest('Optional ddddocr environment and Node are required')
        fixture = Path(__file__).resolve().parent / 'tests/fixtures/playwright_verification_fixture.cjs'
        real_popen = subprocess.Popen
        def isolated_popen(command, **kwargs):
            self.assertEqual(Path(command[-1]).name,'collector_worker.cjs')
            kwargs['env'] = {**kwargs['env'], 'CLUBOPS_PLAYWRIGHT':str(fixture),
                             'CLUBOPS_FIXTURE_SCENARIO':'accepted'}
            return real_popen(command, **kwargs)
        task = self.create()
        with patch.dict('os.environ', {'CLUBOPS_CAPTCHA_MODE':'auto'}), \
             patch('collector.dependencies',return_value=(shutil.which('node'),fixture.parent)), \
             patch('collector.subprocess.Popen',side_effect=isolated_popen):
            collector.run(task,collector.ACTIVE[task])
        result = collector.state()['tasks'][0]
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['verification_counts'],{'attempted':1,'recovered':1})
        self.assertEqual(result['comments'],1)
        self.assertEqual(result['analysis']['status'],'completed')
        self.assertFalse(result['active'])
        with app.db() as c:
            events = [json.loads(r[0])['verification'] for r in c.execute(
                "SELECT snapshot FROM collection_diagnostics WHERE task_id=? AND stage='captcha_workflow' ORDER BY id",(task,))]
            self.assertEqual([v['phase'] for v in events],['detected','capturing','recognizing','submitting','verifying','accepted'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()
