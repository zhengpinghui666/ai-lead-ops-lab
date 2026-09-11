import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import clubops as app
import live_discovery as discovery


HTML = '<a href="https://live.douyin.com/12345"><span>1万</span><b>无畏契约教学</b></a>'


class Response(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, raw=HTML.encode()):
        self.raw, self.calls = raw, []
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return Response(self.raw)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clubops-live-discovery-')
        self.original = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()

    def tearDown(self):
        app.DATA_DIR = self.original
        self.temp.cleanup()

    def test_public_links_have_exact_domain_and_stable_deduplication(self):
        rows = discovery.parse(HTML + HTML + '<a href="https://live.douyin.com.evil/123">bad</a><a href="javascript:alert(1)">bad</a>')
        self.assertEqual(rows, [dict(room_url='https://live.douyin.com/12345', title='无畏契约教学', popularity_text='1万')])

    def test_read_is_fixed_bounded_no_credentials_and_save_does_not_start(self):
        opener = Opener()
        with patch('subprocess.Popen') as spawn:
            result = discovery.discover(opener=opener)
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(discovery.state()['rows'], result['rows'])
            spawn.assert_not_called()
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, discovery.SOURCE_URL)
        self.assertEqual(timeout, 15)
        self.assertIsNone(request.get_header('Cookie'))
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(app.state()['comments'], [])

    def test_sixty_second_cache_prevents_duplicate_reads(self):
        opener = Opener()
        discovery.discover(opener=opener)
        self.assertTrue(discovery.discover(opener=opener)['cached'])
        self.assertEqual(len(opener.calls), 1)

    def test_candidates_report_latest_actual_observation_without_starting(self):
        discovery.discover(opener=Opener())
        with app.db() as c:
            for key, status in [('first', 'ended'), ('second', 'no_data')]:
                c.execute('INSERT INTO live_sessions(request_id,room_url,config,status,detail,started_at,updated_at,finished_at) VALUES(?,?,?,?,?,?,?,?)',
                    (key, 'https://live.douyin.com/12345', '{}', status, 'Synthetic observation', app.now(), app.now(), app.now()))
        with patch('subprocess.Popen') as spawn:
            result = discovery.state()
            spawn.assert_not_called()
        evidence = result['observations']['https://live.douyin.com/12345']
        self.assertEqual(evidence['id'], 2)
        self.assertEqual(evidence['status'], 'no_data')
        self.assertEqual(evidence['inserted'], 0)

    def test_failure_preserves_old_candidates_and_success_timestamp(self):
        old = discovery.discover(opener=Opener())
        old['attempted_at'] = '2020-01-01T00:00:00+00:00'
        with app.db() as c:
            c.execute("UPDATE settings SET value=? WHERE key='live_discovery'", (json.dumps(old),))
        result = discovery.discover(opener=Opener(b'<html>empty</html>'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'no_candidates')
        self.assertEqual(result['fetched_at'], old['fetched_at'])
        self.assertEqual(result['rows'], old['rows'])

    def test_oversized_response_and_network_error_are_not_candidates(self):
        with patch.object(discovery, 'LIMIT', 10):
            result = discovery.discover(opener=Opener())
        self.assertEqual(result['error'], 'response_too_large')
        with app.db() as c:
            c.execute("DELETE FROM settings WHERE key='live_discovery'")
        opener = Opener()
        opener.open = lambda *a, **k: (_ for _ in ()).throw(OSError('sensitive upstream text'))
        result = discovery.discover(opener=opener)
        self.assertEqual(result['error'], 'read_failed')
        self.assertNotIn('sensitive', json.dumps(result))

    def test_demo_and_concurrent_admission_are_inert(self):
        with self.assertRaises(ValueError):
            discovery.discover('demo', opener=Opener())
        with discovery.GUARD:
            with self.assertRaises(ValueError):
                discovery.discover(opener=Opener())


if __name__ == '__main__':
    unittest.main()
