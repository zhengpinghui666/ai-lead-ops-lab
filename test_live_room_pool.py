"""Synthetic catalog/scheduler tests; never connect to a platform."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clubops as app
import live_monitor as live
import live_room_pool as pool
import live_tracking as tracking

request_discovery = pool.request_discovery


class RoomPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clubops-room-pool-')
        self.original = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()
        live.ACTIVE.clear()
        tracking.STOP.clear()
        self.config = {**live.DEFAULTS, 'room_url': 'https://live.douyin.com/12345'}
        live.save(self.config)
        self.discovery_patch = patch.object(pool, 'request_discovery')
        self.discovery_patch.start()

    def tearDown(self):
        self.discovery_patch.stop()
        live.ACTIVE.clear()
        tracking.STOP.clear()
        app.DATA_DIR = self.original
        self.temp.cleanup()

    def begin(self, key='library'):
        return tracking.start({'request_id': key, 'scope': 'library'})

    def tick(self):
        with patch('threading.Thread'):
            tracking.tick()
        return tracking.state()['last_session_id']

    def finish(self, sid, status='completed'):
        live.ACTIVE.pop(sid, None)
        live.record_status(sid, status, final=True)
        tracking.tick()

    def due(self):
        with app.db() as c:
            c.execute("UPDATE live_tracks SET next_run_at='2020-01-01T00:00:00+00:00' WHERE status='enabled'")

    def test_catalog_is_durable_deduplicated_and_reads_do_not_start(self):
        with app.db() as c:
            pool.ingest(c, [{'room_url': '23456', 'title': '瓦陪玩'}, {'room_url': 'https://evil.test/1'}], 'valorant_category', app.now())
        with patch('subprocess.Popen') as spawn:
            self.begin()
            self.assertEqual(pool.state()['counts']['total'], 2)
            self.assertEqual(pool.state()['counts']['due'], 2)
            live.state()
            spawn.assert_not_called()
        pool.toggle({'room_url': '23456', 'enabled': False})
        with app.db() as c:
            pool.ingest(c, [{'room_url': '23456', 'title': '新标题'}], 'valorant_category', app.now())
        row = next(r for r in pool.state()['rows'] if r['room_url'].endswith('23456'))
        self.assertEqual(row['title'], '新标题')
        self.assertEqual(row['enabled'], 0)
        self.assertEqual(pool.state()['counts']['total'], 2)

    def test_rotation_after_end_does_not_end_whole_monitor(self):
        self.begin()
        with app.db() as c:
            pool.ingest(c, [{'room_url': '23456'}, {'room_url': '34567'}], 'valorant_category', app.now())
        first = self.tick()
        first_url = live.state()['current']['room_url']
        self.finish(first, 'ended')
        self.assertTrue(tracking.state()['enabled'])
        self.due()
        second = self.tick()
        self.assertNotEqual(second, first)
        self.assertNotEqual(live.state()['current']['room_url'], first_url)
        self.finish(second)
        self.due()
        self.tick()
        self.assertEqual(tracking.state()['run_count'], 3)
        self.assertEqual(pool.state()['counts']['total'], 3)

    def test_empty_room_backoff_and_settlement_are_idempotent(self):
        self.begin()
        for failure in range(1, 4):
            self.due()
            with app.db() as c:
                c.execute("UPDATE live_rooms SET next_check_at='2020-01-01T00:00:00+00:00'")
            sid = self.tick()
            self.finish(sid, 'no_data')
            row = pool.state()['rows'][0]
            self.assertEqual(row['failures'], failure)
            self.assertEqual(row['next_check_at'], pool.later(row['last_checked_at'], 300 * 2 ** (failure - 1)))
            self.assertTrue(tracking.state()['enabled'])
            tracking.tick()
            self.assertEqual(pool.state()['rows'][0]['failures'], failure)

    def test_access_and_integrity_problems_pause_entire_rotation(self):
        for status in ('needs_login', 'needs_verification', 'rate_limited', 'access_denied', 'schema_changed', 'room_changed', 'failed'):
            self.begin(status)
            with app.db() as c:
                c.execute("UPDATE live_rooms SET next_check_at='2020-01-01T00:00:00+00:00'")
            sid = self.tick()
            self.finish(sid, status)
            self.due()
            with patch('threading.Thread') as thread:
                tracking.tick()
                thread.return_value.start.assert_not_called()
            self.assertFalse(tracking.state()['enabled'])
            self.assertEqual(pool.state()['rows'][0]['last_status'], status)

    def test_stop_and_restart_block_admission_and_keep_catalog(self):
        track = self.begin()
        sid = self.tick()
        tracking.stop(track['id'])
        self.assertTrue(live.ACTIVE[sid]['stop'])
        self.finish(sid, 'cancelled')
        self.assertEqual(pool.state()['rows'][0]['last_status'], 'cancelled')
        tracking.recover()
        with patch('threading.Thread') as thread:
            tracking.tick()
            self.begin()
            thread.return_value.start.assert_not_called()
        self.assertFalse(tracking.state()['enabled'])
        self.assertEqual(pool.state()['counts']['total'], 1)

    def test_all_cooling_rooms_wait_without_fake_sessions(self):
        self.begin()
        sid = self.tick()
        self.finish(sid, 'ended')
        self.due()
        with patch('threading.Thread') as thread:
            tracking.tick()
            thread.return_value.start.assert_not_called()
        self.assertEqual(tracking.state()['run_count'], 1)
        self.assertTrue(tracking.state()['enabled'])
        self.assertIn('等待复查', tracking.state()['detail'])

    def discovery_job(self, track):
        with patch('threading.Thread') as thread:
            request_discovery(track['id'], track['config'])
        return thread.call_args.kwargs['target']

    def test_discovery_is_background_bounded_and_stop_discards_late_catalog(self):
        track = self.begin()
        job = self.discovery_job(track)
        with patch('threading.Thread') as thread:
            request_discovery(track['id'], track['config'])
            thread.assert_not_called()
        tracking.stop(track['id'])
        result = {'status': 'ready', 'rows': [{'room_url': '23456'}], 'fetched_at': app.now(), 'detail': 'Synthetic category'}
        with patch('live_discovery.discover', return_value=result):
            job()
        self.assertEqual(pool.state()['counts']['total'], 1)
        self.assertFalse(tracking.state()['enabled'])

    def test_discovery_adds_rooms_without_mutating_frozen_config(self):
        track = self.begin()
        job = self.discovery_job(track)
        result = {'status': 'ready', 'rows': [{'room_url': '23456'}], 'fetched_at': app.now(), 'detail': 'Synthetic category'}
        with patch('live_discovery.discover', return_value=result):
            job()
        self.assertEqual(pool.state()['counts']['total'], 2)
        self.assertEqual(tracking.state()['config'], track['config'])

    def test_discovery_access_restriction_stops_active_stream(self):
        track = self.begin()
        job = self.discovery_job(track)
        sid = self.tick()
        with patch('live_discovery.discover', return_value={'status': 'failed', 'error': 'http_403', 'detail': 'Synthetic refusal'}):
            job()
        self.assertFalse(tracking.state()['enabled'])
        self.assertTrue(live.ACTIVE[sid]['stop'])

    def test_single_room_behavior_remains_explicit_and_options_are_validated(self):
        single = tracking.start({'request_id': 'single'})
        self.assertNotIn('scope', single['config'])
        for field, invalid in [('discovery_interval_minutes', 1), ('offline_retry_minutes', 0), ('empty_retry_minutes', True)]:
            with self.assertRaises(ValueError):
                live.options({**self.config, field: invalid})
        with self.assertRaises(ValueError):
            pool.toggle({'room_url': '12345', 'enabled': 1})

    def test_migration_has_backup_once_and_preserves_session(self):
        self.begin()
        sid = self.tick()
        self.finish(sid)
        with app.db() as c:
            c.execute('DROP TABLE live_pool_checks')
            c.execute('DROP TABLE live_rooms')
        app.init()
        app.init()
        self.assertEqual(len(list((app.DATA_DIR/'backups').glob('*.before-live-room-pool-*.bak'))), 1)
        self.assertEqual(live.state()['current']['id'], sid)


if __name__ == '__main__':
    unittest.main()
