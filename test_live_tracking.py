"""Local lifecycle, frozen scope and archive tests; no external platform requests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clubops as app
import live_monitor as live
import live_tracking as tracking


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clubops-tracking-')
        self.original = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()
        live.ACTIVE.clear()
        tracking.STOP.clear()
        self.config = dict(live.DEFAULTS, room_url='https://live.douyin.com/12345')
        live.save(self.config)

    def tearDown(self):
        live.ACTIVE.clear()
        tracking.STOP.clear()
        app.DATA_DIR = self.original
        self.temp.cleanup()

    def begin(self):
        result = tracking.start({'request_id': 'test-track'})
        with patch('threading.Thread'):
            tracking.tick()
        return result['id'], tracking.state()['last_session_id']

    def finish(self, sid, status='completed'):
        live.ACTIVE.pop(sid, None)
        live.record_status(sid, status, final=True)

    def test_save_and_reads_are_inert_default_is_background(self):
        with patch('subprocess.Popen') as spawn:
            live.save(self.config)
            self.assertFalse(live.settings()['interactive'])
            self.assertFalse(tracking.state()['enabled'])
            live.state()
            live.history({})
            spawn.assert_not_called()
        for value in (0, 29, 3601, True, 30.0):
            with self.assertRaises(ValueError):
                live.options({**self.config, 'interval_seconds': value})

    def test_start_deduplication_and_frozen_background_config(self):
        tracking.start({'request_id': 'test-track'})
        live.save({**self.config, 'room_url': 'https://live.douyin.com/99999', 'interactive': True})
        with patch('threading.Thread') as thread:
            tracking.tick()
            tracking.tick()
            tracking.start({'request_id': 'test-track'})
            self.assertEqual(thread.return_value.start.call_count, 1)
        snapshot = live.state()['current']['config']
        self.assertEqual(snapshot['room_url'], self.config['room_url'])
        self.assertFalse(snapshot['interactive'])
        self.assertEqual(tracking.state()['run_count'], 1)
        with self.assertRaises(ValueError):
            live.start({'request_id': 'manual'})
        with self.assertRaises(ValueError):
            tracking.start({'request_id': 'second'})

    def test_cooldown_then_next_batch_with_same_scope(self):
        tid, sid = self.begin()
        self.finish(sid)
        with patch('threading.Thread') as thread:
            tracking.tick()
            thread.return_value.start.assert_not_called()
        self.assertIsNotNone(tracking.state()['next_run_at'])
        with app.db() as c:
            c.execute("UPDATE live_tracks SET next_run_at='2020-01-01T00:00:00+00:00' WHERE id=?", (tid,))
        with patch('threading.Thread'):
            tracking.tick()
        self.assertEqual(tracking.state()['run_count'], 2)
        self.assertNotEqual(tracking.state()['last_session_id'], sid)

    def test_failure_and_end_pause_without_retry(self):
        for status in ('needs_verification', 'needs_login', 'rate_limited', 'access_denied', 'no_data', 'disconnected', 'failed', 'ended'):
            tracking.start({'request_id': status})
            with patch('threading.Thread'):
                tracking.tick()
            sid = tracking.state()['last_session_id']
            self.finish(sid, status)
            with patch('threading.Thread') as thread:
                tracking.tick()
                tracking.tick()
                thread.return_value.start.assert_not_called()
            self.assertFalse(tracking.state()['enabled'])
            self.assertEqual(tracking.state()['status'], 'ended' if status == 'ended' else 'attention')

    def test_stop_active_waiting_and_stale_request_never_restarts(self):
        tid, sid = self.begin()
        tracking.stop(tid)
        self.assertTrue(live.ACTIVE[sid]['stop'])
        self.finish(sid, 'cancelled')
        with patch('threading.Thread') as thread:
            tracking.start({'request_id': 'test-track'})
            tracking.tick()
            thread.return_value.start.assert_not_called()
        self.assertFalse(tracking.state()['enabled'])

    def test_restart_preserves_archive_but_disables_tracking(self):
        tid, sid = self.begin()
        live.recover()
        live.ACTIVE.clear()
        tracking.recover()
        with patch('threading.Thread') as thread:
            tracking.tick()
            thread.return_value.start.assert_not_called()
        self.assertEqual(live.state()['current']['status'], 'interrupted')
        self.assertFalse(tracking.state()['enabled'])

    def test_shutdown_admission_gate_and_thread_start_failure(self):
        tracking.start({'request_id': 'gate'})
        tracking.STOP.set()
        with patch('threading.Thread') as thread:
            tracking.tick()
            thread.return_value.start.assert_not_called()
        tracking.STOP.clear()
        with patch('threading.Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('synthetic')
            tracking.tick()
        self.assertFalse(live.ACTIVE)
        self.assertEqual(tracking.state()['status'], 'attention')
        self.assertEqual(live.state()['current']['status'], 'failed')

    def test_migration_backup_once_preserves_records(self):
        tid, sid = self.begin()
        with app.db() as c:
            c.execute('DROP TABLE live_tracks')
        app.init()
        app.init()
        self.assertEqual(len(list((app.DATA_DIR / 'backups').glob('*.before-live-tracking-*.bak'))), 1)
        self.assertEqual(live.state()['current']['id'], sid)

    def test_archive_pages_cross_sessions_with_stable_snapshot(self):
        _, sid = self.begin()
        with app.db() as c:
            # Minimal archival rows exercise 500+ paging without analysis calls.
            for n in range(510):
                c.execute('INSERT INTO live_messages(session_id,room_id,message_id,uid,nickname,raw_text,observed_at,filter_reason,category,analysis_method,reason,facts,include_matches,exclude_matches,payload_hash,game) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (sid, '123', str(10000+n), '456', '合成测试', f'原文 {n}', app.now(), 'filtered_blocked' if n % 2 else '', 'uncertain', 'rules', '', '{}', '[]', '[]', str(n), ''))
        first = live.history({'limit': 25})
        last = live.history({'limit': 25, 'offset': 500, 'anchor_id': first['anchor_id']})
        self.assertEqual((first['total'], len(last['rows'])), (510, 10))
        self.assertFalse(last['has_more'])
        self.assertEqual(live.history({'q': '原文 0'})['total'], 1)
        self.assertEqual(live.history({'filter': 'filtered'})['total'], 255)
        self.assertEqual(live.history({'anchor_id': first['anchor_id'] - 10})['total'], 500)
        self.assertEqual(live.history({'session_id': sid + 100})['total'], 0)
        for query in ({'limit': 101}, {'session_id': 'abc'}, {'filter': 'invalid'}, {'q': 'x' * 201}):
            with self.assertRaises(ValueError):
                live.history(query)

    def test_http_tracking_controls_require_csrf_and_archive_is_read_only(self):
        import threading
        import server
        from http.server import ThreadingHTTPServer
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{httpd.server_port}'
        try:
            with urlopen(base + '/api/live-history?limit=25') as response:
                self.assertEqual(json.load(response)['total'], 0)
            body = json.dumps({'request_id': 'http-test'}).encode()
            with self.assertRaises(HTTPError) as denied:
                urlopen(Request(base + '/api/live-track-start', data=body))
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
            headers = {'Content-Type': 'application/json', 'X-ClubOps-Token': server.CSRF, 'Origin': base}
            with urlopen(Request(base + '/api/live-track-start', data=body, headers=headers)) as response:
                track = json.load(response)['result']
            self.assertTrue(track['enabled'])
            self.assertFalse(live.ACTIVE, 'API admission itself does not launch before scheduler tick')
            with urlopen(Request(base + '/api/live-track-stop', data=json.dumps({'id': track['id']}).encode(), headers=headers)) as response:
                self.assertFalse(json.load(response)['result']['enabled'])
            self.assertFalse(live.ACTIVE)
        finally:
            httpd.shutdown()
            thread.join(timeout=3)
            httpd.server_close()


if __name__ == '__main__':
    unittest.main()
