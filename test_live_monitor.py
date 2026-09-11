"""Local synthetic evidence; these tests never connect to Douyin."""
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

import clubops as app
import live_monitor as live

ROOM, UID, MID = '10000000000000001', '10000000000000002', '10000000000000003'


class LiveMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clubops-live-test-')
        self.original = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()
        live.ACTIVE.clear()
        self.config = dict(live.DEFAULTS, room_url='https://live.douyin.com/12345')
        live.save(self.config)

    def tearDown(self):
        live.ACTIVE.clear()
        app.DATA_DIR = self.original
        self.temp.cleanup()

    def session(self, key='first'):
        with app.db() as c:
            return c.execute('INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                             (key, self.config['room_url'], ROOM, json.dumps(self.config), 'connecting', '', app.now(), app.now())).lastrowid

    def event(self, mid=MID, **changes):
        return {'type': 'message', 'record': dict(room_id=ROOM, uid=UID, message_id=mid, nickname='离线样例', text='无畏契约找陪练，预算100元', published_at='2026-09-10T00:00:00Z', **changes)}

    def test_config_is_inert_and_strict(self):
        with patch('subprocess.Popen') as process:
            live.save(self.config)
            self.assertEqual(live.state()['rows'], [])
            process.assert_not_called()
        for value in ('http://live.douyin.com/12345', 'https://live.douyin.com.evil/12345', 'https://user@live.douyin.com/12345', 'https://live.douyin.com/12345/x', 'MS4wSecUID', 12345):
            with self.assertRaises(ValueError):
                live.room_url(value)
        for values in ({'max_messages': True}, {'duration_seconds': 0}, {'include_keywords': ['a']}, {'interactive': 1}):
            with self.assertRaises(ValueError):
                live.save({**self.config, **values})

    def test_numeric_id_duplicate_conflict_and_no_fake_comments(self):
        sid = self.session()
        live.receive(sid, self.event())
        live.receive(sid, self.event())
        altered = self.event()
        altered['record']['uid'] = '10000000000000009'
        live.receive(sid, altered)
        state = live.state()
        self.assertEqual((state['current']['observed'], state['current']['duplicate'], state['current']['invalid']), (3, 1, 1))
        self.assertEqual(state['rows'][0]['uid'], UID)
        self.assertEqual(state['rows'][0]['analysis_method'], 'rules')
        self.assertEqual((app.state()['comments'], app.state()['videos'], app.state()['messages'], app.state()['jobs']), ([], [], [], []))

    def test_global_message_identity_and_unknown_not_invented(self):
        first = self.session()
        live.receive(first, self.event())
        second = self.session('second')
        live.receive(second, self.event())
        value = self.event(None)
        value['record']['uid'] = None
        value['record']['published_at'] = None
        live.receive(second, value)
        live.receive(second, value)
        state = live.state()
        self.assertEqual(len(state['rows']), 2, 'Without a message ID, identical text is not proof of a duplicate')
        self.assertIsNone(state['rows'][0]['message_id'])
        self.assertIsNone(state['rows'][0]['uid'])
        self.assertIsNone(state['rows'][0]['published_at'])
        self.assertEqual(state['current']['duplicate'], 1)

    def test_distinct_outer_id_does_not_corrupt_message_identity(self):
        sid = self.session()
        first = self.event()
        first['record']['outer_message_id'] = '99999'
        live.receive(sid, first)
        second = self.event()
        second['record']['outer_message_id'] = '88888'
        live.receive(sid, second)
        state = live.state()
        self.assertEqual(len(state['rows']), 1)
        self.assertEqual(state['rows'][0]['message_id'], MID)
        self.assertEqual(state['rows'][0]['outer_message_id'], '99999')
        self.assertEqual(state['current']['duplicate'], 1)

    def test_wrong_room_missing_stream_or_budget_rejected(self):
        self.config['max_messages'] = 1
        sid = self.session()
        value = self.event()
        value['record']['room_id'] = '99999'
        with self.assertRaises(ValueError):
            live.receive(sid, value)
        with self.assertRaises(ValueError):
            live.receive(sid, {'type': 'stream', 'room_id': '99999'})
        live.receive(sid, self.event())
        with self.assertRaises(ValueError):
            live.receive(sid, self.event())

    def test_filter_snapshot_and_blocked_priority(self):
        self.config.update(include_keywords='陪练', exclude_keywords='免费')
        sid = self.session()
        live.save({**self.config, 'include_keywords': '新配置'})
        value = self.event()
        value['record']['text'] = '免费陪练'
        live.receive(sid, value)
        row = live.state()['rows'][0]
        self.assertEqual(row['filter_reason'], 'filtered_blocked')
        self.assertEqual(row['analysis_method'], 'not_analyzed')
        self.assertEqual(row['include_matches'], ['陪练'])
        self.assertEqual(live.state()['current']['config']['include_keywords'], '陪练')

    def test_stop_drops_queued_messages_and_does_not_stop_collector(self):
        sid = self.session()
        fake = type('Process', (), {'stdin': io.StringIO(), 'poll': lambda self: None})()
        live.ACTIVE[sid] = {'process': fake, 'stop': False}
        with patch('collector.command') as command:
            live.stop(sid)
            live.receive(sid, self.event())
            live.record_status(sid, 'running')
            command.assert_not_called()
        self.assertEqual(live.state()['rows'], [])
        self.assertEqual(live.state()['current']['status'], 'stopping')
        self.assertIn('stop', fake.stdin.getvalue())
        live.receive(sid, {'type': 'diagnostic', 'methods': {'WebcastChatMessage': 1}, 'timestamps': {'milliseconds': 1}})
        self.assertEqual(live.state()['events'][0]['status'], 'diagnostic', 'Stopping preserves final diagnostics, but discards queued chat')

    def test_page_diagnostics_are_bounded_and_reject_raw_browser_data(self):
        sid = self.session()
        page = dict(navigation='loaded', http_status=200, body_chars=351, gate='none')
        event = dict(type='diagnostic', methods={}, page=page, sockets=[dict(host='live.douyin.com', path='/webcast/im/push/v2/')])
        live.receive(sid, event)
        self.assertIn('"body_chars": 351', live.state()['events'][0]['detail'])
        for invalid in ({**page, 'html': '<secret>'}, {**page, 'body_chars': 25001}, {**page, 'gate': 'unknown raw text'}, [], ''):
            with self.assertRaises(ValueError):
                live.receive(sid, {**event, 'page': invalid})
        for invalid in ([dict(host='live.douyin.com', path='/path?token=secret')], [dict(host=None, path='/')], event['sockets'] * 9):
            with self.assertRaises(ValueError):
                live.receive(sid, {**event, 'sockets': invalid})
        network = dict(requests=80, failures={'ERR_CONNECTION_CLOSED': 3}, script_errors={'TypeError': 2}, live_responses={'200': 4})
        live.receive(sid, {**event, 'network': network})
        for invalid in ({**network, 'requests': True}, {**network, 'failures': {'https://secret': 1}}, {**network, 'script_errors': {'raw secret error': 1}}):
            with self.assertRaises(ValueError):
                live.receive(sid, {**event, 'network': invalid})
        live.receive(sid, {**event, 'poll': {'decoded': 3, 'empty_messages': 3, 'max_bytes': 4194304}, 'poll_fields': {'f2': 3, 'f8': 3}})
        for invalid in ({'raw_body': 1}, {'decoded': True}, {'max_bytes': 4194305}):
            with self.assertRaises(ValueError):
                live.receive(sid, {**event, 'poll': invalid})
        for invalid in ({'token=secret': 1}, {'f536870912': 1}, {'f1': True}):
            with self.assertRaises(ValueError):
                live.receive(sid, {**event, 'poll_fields': invalid})

    def test_http_stream_uses_existing_room_and_message_checks(self):
        sid = self.session()
        live.receive(sid, dict(type='stream', room_id=ROOM, transport='browser_http_poll'))
        self.assertIn('HTTP', live.state()['events'][0]['detail'])
        live.receive(sid, self.event())
        self.assertEqual(len(live.state()['rows']), 1)
        with self.assertRaises(ValueError):
            live.receive(sid, dict(type='stream', room_id='99999', transport='browser_http_poll'))
        with self.assertRaises(ValueError):
            live.receive(sid, dict(type='stream', room_id=ROOM, transport='unknown'))

    def test_gaps_recovery_and_terminal_immutability(self):
        sid = self.session()
        live.record_status(sid, 'running')
        live.record_status(sid, 'disconnected')
        live.record_status(sid, 'disconnected')
        live.record_status(sid, 'reconnected')
        self.assertEqual(live.state()['current']['gaps'], 1)
        live.record_status(sid, 'running')
        live.record_status(sid, 'reconnected')
        live.record_status(sid, 'reconnected')
        with patch('subprocess.Popen') as process:
            live.recover()
            live.recover()
            process.assert_not_called()
        live.receive(sid, self.event())
        state = live.state()
        self.assertEqual((state['current']['status'], state['current']['gaps']), ('interrupted', 2))
        self.assertEqual(state['rows'], [])

    def test_start_request_id_and_active_session_protection(self):
        with patch('threading.Thread') as thread:
            one = live.start({'request_id': 'one'})
            self.assertEqual(live.start({'request_id': 'one'})['id'], one['id'])
            with self.assertRaises(ValueError):
                live.start({'request_id': 'two'})
            thread.return_value.start.assert_called_once()
        with self.assertRaises(ValueError):
            live.start({'request_id': 'demo'}, 'demo')

    def test_live_schema_upgrade_backup_preserves_existing_uid_table(self):
        with app.db() as c:
            c.executescript('DROP TABLE live_session_events; DROP TABLE live_messages; DROP TABLE live_sessions;')
            c.execute("INSERT INTO settings VALUES('backup_test','123')")
        app.init()
        backups = list((app.DATA_DIR / 'backups').glob('*.before-live-stream-*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertEqual(c.execute("SELECT value FROM settings WHERE key='backup_test'").fetchone()[0], '123')
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='live_sessions'").fetchone())

    def test_worker_protocol_through_fake_child_pipe(self):
        sid = self.session()
        lines = [dict(type='stream', room_id=ROOM), dict(type='status', status='running'), self.event(), dict(type='final', status='completed', frames=2, invalid=0)]
        class Process:
            stdin = io.StringIO()
            stdout = io.StringIO(''.join(json.dumps(x)+'\n' for x in lines))
            def poll(self): return 0
            def wait(self, timeout=None): return 0
            def kill(self): pass
        control = {'process': None, 'stop': False}
        live.ACTIVE[sid] = control
        with patch('collector.dependencies', return_value=('node', app.DATA_DIR)), patch('subprocess.Popen', return_value=Process()):
            live.worker(sid, self.config, control)
        self.assertNotIn(sid, live.ACTIVE)
        self.assertEqual(live.state()['current']['status'], 'completed')
        self.assertEqual(len(live.state()['rows']), 1)

    def test_outer_id_migration_backs_up_existing_records(self):
        sid = self.session()
        live.receive(sid, self.event())
        with app.db() as c:
            c.execute('ALTER TABLE live_messages DROP COLUMN outer_message_id')
        app.init()
        backups = list((app.DATA_DIR / 'backups').glob('*.before-live-stream-*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertNotIn('outer_message_id', [r[1] for r in c.execute('PRAGMA table_info(live_messages)')])
            self.assertEqual(c.execute('SELECT message_id FROM live_messages').fetchone()[0], MID)
        row = live.state()['rows'][0]
        self.assertEqual(row['message_id'], MID)
        self.assertIsNone(row['outer_message_id'])


if __name__ == '__main__':
    unittest.main()
