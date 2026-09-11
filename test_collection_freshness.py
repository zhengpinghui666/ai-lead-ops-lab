"""Isolated incremental collection; no browser, real model, or platform request."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import clubops as app
import collector
import monitoring
import semantic
import semantic_queue as queue
from test_semantic import SyntheticAdapter

NOW = '2026-09-11T03:00:00+00:00'
VIDEO = '7600000000000000001'


class FreshCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fresh-collection-')
        self.addCleanup(self.temp.cleanup)
        self.old = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        self.addCleanup(setattr, app, 'DATA_DIR', self.old)
        app.init()
        self.clock = patch('clubops.now', return_value=NOW)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.threads = patch('collector.threading.Thread.start')
        self.threads.start()
        self.addCleanup(self.threads.stop)
        collector.ACTIVE.clear()
        self.addCleanup(collector.ACTIVE.clear)
        queue.STOP.clear()
        semantic.save(dict(semantic.DEFAULTS, enabled=True, auto_analyze=True, model='synthetic:1'))
        self.task = collector.start(dict(kind='video', target=VIDEO, request_id='fresh-fixture',
            video_limit=1, comment_limit=20), lookback_hours=1)['id']
        self.source = collector.ACTIVE[self.task]['source_id']
        collector.observe(self.task, self.source, dict(type='video', record=dict(video_id=VIDEO,
            video_title='无畏契约陪练合成视频')))

    def comment(self, suffix=2, **changes):
        row = dict(video_id=VIDEO, comment_id=str(7600000000000000000+suffix),
            user_id='123456789012', text='无畏契约找陪练，预算100元', published_at=NOW,
            video_title='无畏契约陪练合成视频')
        row.update(changes)
        collector.observe(self.task, self.source, dict(type='comment', record=row))

    def jobs(self):
        with app.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM semantic_jobs ORDER BY id')]

    def test_model_completes_while_collection_remains_active_and_deduplicates(self):
        with patch('http.client.HTTPConnection') as network:
            self.comment()
            self.assertEqual(self.jobs()[0]['status'], 'queued')
            self.assertIn(self.task, collector.ACTIVE)
            with app.db() as c:
                self.assertIsNone(c.execute('SELECT finished_at FROM collection_tasks').fetchone()[0])
                self.assertEqual(c.execute('SELECT analysis_method FROM comments').fetchone()[0], 'rules')
            self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
            self.assertEqual(monitoring.results()['rows'][0]['analysis_method'], 'model')
            self.assertIn(self.task, collector.ACTIVE)
            self.comment()
            collector.analyze_observed(self.task, self.source)
            self.assertEqual(len(self.jobs()), 1)
            network.assert_not_called()
            with app.db() as c:
                for table in ('messages','message_jobs','uid_message_attempts'):
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)

    def test_old_unknown_future_and_keyword_filtered_records_never_enqueue(self):
        for suffix, published in enumerate(('2026-09-11T01:59:59+00:00', None,
                                           '2026-09-11T03:00:01+00:00'), 2):
            self.comment(suffix, published_at=published)
        with app.db() as c:
            c.execute("UPDATE collection_tasks SET include_keywords='不匹配词'")
        self.comment(8)
        self.assertEqual(self.jobs(), [])
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM comments').fetchone()[0], 0)
        self.assertEqual(monitoring.results()['timeliness']['measured'], 0)

    def test_local_screen_failure_preserves_observation_and_final_sweep_recovers(self):
        with patch('clubops.classify', side_effect=RuntimeError('secret=do-not-log')):
            self.comment()
        self.assertEqual(self.jobs(), [])
        collector.analyze_observed(self.task, self.source)
        self.assertEqual(len(self.jobs()), 1)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM comments').fetchone()[0], 1)
            self.assertIsNone(c.execute('SELECT finished_at FROM collection_tasks').fetchone()[0])
            self.assertNotIn('do-not-log', str([tuple(r) for r in c.execute('SELECT * FROM events')]))

    def test_late_parent_invalidates_old_input_and_queues_current_context(self):
        self.comment(3, text='多少钱', parent_comment_id='7600000000000000002')
        first_hash = self.jobs()[0]['input_hash']
        self.comment(2, text='无畏契约陪练')
        hashes = {j['input_hash'] for j in self.jobs() if j['record_id'] == 1}
        self.assertIn(first_hash, hashes)
        self.assertEqual(len(hashes), 2)
        self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
        self.assertEqual(self.jobs()[0]['status'], 'stale')
        while queue.run_one(adapter_factory=SyntheticAdapter):
            pass
        rows = monitoring.results()['rows']
        self.assertTrue(all(r['analysis_method'] == 'model' for r in rows))

    def test_real_stage_timestamps_exclude_duplicate_batch_and_preserve_unknown(self):
        self.now.return_value = '2026-09-11T03:00:20+00:00'
        self.comment()
        self.now.return_value = '2026-09-11T03:00:25+00:00'
        now = self.now
        class TimedAdapter(SyntheticAdapter):
            def predict(self, source):
                now.return_value = '2026-09-11T03:00:48+00:00'
                return super().predict(source)
        self.assertTrue(queue.run_one(adapter_factory=TimedAdapter))
        result = monitoring.results()
        value = result['rows'][0]['timing']
        self.assertEqual(value, dict(collection_seconds=20, rule_seconds=0, queue_seconds=5,
            model_seconds=23, end_to_end_seconds=48, fresh_at_collection=True, within_target=True))
        self.assertEqual((result['timeliness']['measured'],result['timeliness']['within_target']), (1,1))
        self.assertFalse(result['coverage']['all_douyin'])
        self.assertFalse(result['coverage']['latest_first_verified'])
        collector.update(self.task, status='completed', finished_at=app.now())
        collector.ACTIVE.clear()
        self.task = collector.start(dict(kind='video',target=VIDEO,request_id='repeat',comment_limit=20),lookback_hours=1)['id']
        collector.observe(self.task,self.source,dict(type='video',record=dict(video_id=VIDEO)))
        self.comment()
        self.assertEqual(monitoring.results()['timeliness']['new_comments'],0)
        self.assertEqual(len(self.jobs()),1)
        self.assertIsNone(monitoring.timing(dict(published_at=NOW,first_seen_at='2026-09-11T02:59:59+00:00'))['collection_seconds'])
        self.assertIsNone(monitoring.elapsed_seconds('2026-09-11T03:00:00', NOW))

    def test_recent_comment_has_queue_priority_over_old_unknown_and_future(self):
        for i, published in enumerate(('2026-09-10T00:00:00+00:00',None,'2026-09-12T00:00:00+00:00')):
            app.ingest({'records':[dict(comment_id='backlog-'+str(i),video_id='backlog',text='无畏契约找陪练',published_at=published)]})
        with app.db() as c:
            ids = [r[0] for r in c.execute('SELECT id FROM comments')]
        app.analyze(comment_ids=ids)
        self.comment()
        newest = self.jobs()[-1]['id']
        self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
        self.assertEqual([j['id'] for j in self.jobs() if j['status']=='completed'], [newest])
        self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
        self.assertEqual(self.jobs()[0]['status'],'completed')

    def test_collection_target_does_not_wait_for_model_and_uses_current_setting(self):
        self.now.return_value = '2026-09-11T03:00:45+00:00'
        self.comment()
        result = monitoring.results()
        self.assertEqual(result['timeliness']['metric'], 'publication_to_collection')
        self.assertEqual(result['timeliness']['p50_seconds'], 45)
        self.assertTrue(result['rows'][0]['timing']['within_target'])
        self.assertIsNone(result['rows'][0]['timing']['end_to_end_seconds'])
        with app.db() as c:
            c.execute("INSERT INTO settings(key,value) VALUES('collection_freshness_target_seconds','30')")
        self.assertEqual(monitoring.results()['timeliness']['within_target'], 0)
        self.assertFalse(monitoring.results()['rows'][0]['timing']['within_target'])
        row=dict(published_at=NOW,first_seen_at='2026-09-11T03:00:30+00:00',model_finished_at='2026-09-11T03:05:00+00:00')
        self.assertTrue(monitoring.timing(row,30)['within_target'])
        self.assertEqual(monitoring.timing(row,30)['end_to_end_seconds'],300)

    def test_migration_preserves_old_observations_and_leaves_timing_unknown(self):
        self.comment()
        with app.db() as c:
            c.execute('ALTER TABLE collection_observations DROP COLUMN ingest_disposition')
            old = [tuple(r) for r in c.execute('SELECT * FROM collection_observations')]
        app.init()
        with app.db() as c:
            self.assertEqual([tuple(r)[:-1] for r in c.execute('SELECT * FROM collection_observations')],old)
            self.assertTrue(all(r[0]=='' for r in c.execute('SELECT ingest_disposition FROM collection_observations')))
        backups = list((app.DATA_DIR/'backups').glob('*before-collection-timing-*.bak'))
        self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertEqual(c.execute('SELECT * FROM collection_observations').fetchall(),old)
        self.assertEqual(monitoring.results()['timeliness']['new_comments'],0)
        app.init()
        self.assertEqual(len(list((app.DATA_DIR/'backups').glob('*before-collection-timing-*.bak'))),1)

    def test_single_batch_accepts_explicit_time_window_without_enabling_monitor(self):
        collector.ACTIVE.clear()
        body = dict(kind='video',target=VIDEO,request_id='single-window',lookback_hours=1)
        task = collector.start(body)['id']
        with app.db() as c:
            row = c.execute('SELECT lookback_hours,comment_since FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertEqual(tuple(row),(1,'2026-09-11T02:00:00+00:00'))
        self.assertFalse(monitoring.state()['enabled'])
        collector.ACTIVE.clear()
        for invalid in (0, True, '1', 8761):
            with self.assertRaises(ValueError):
                collector.start({**body,'request_id':'bad-window','lookback_hours':invalid})


if __name__ == '__main__':
    unittest.main()
