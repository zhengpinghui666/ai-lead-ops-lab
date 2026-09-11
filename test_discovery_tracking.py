import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import clubops as app
import collector as col
import collection_scheduler as scheduler
import discovery_tracking as discovery
import monitoring
import monitor_board

NOW='2026-09-12T02:00:00+00:00'
AUTHOR='MS4wLjABAAAA_SYNTHETIC_AUTHOR'
VID='7600000000000000100'


def work(n=0,related=True,author=AUTHOR):
    return dict(video_id=str(int(VID)+n),video_title='无畏契约陪玩合成作品' if related else '合成生活日常',
                author_sec_uid=author,author_nickname='合成作者',published_at='2026-09-12T01:59:00+00:00')


class DiscoveryTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.previous=app.DATA_DIR;app.DATA_DIR=Path(self.temp.name)
        app.init();self.clock=patch('clubops.now',return_value=NOW);self.clock.start()
        self.threads=patch('collector.threading.Thread.start');self.threads.start();col.ACTIVE.clear()
        self.base=col.start(dict(kind='video',target=VID,transport='http',request_id='discovery-baseline'))['id']
        self.source=col.state()['source_id']
        col.observe(self.base,self.source,dict(type='video',record=work()))
        col.observe(self.base,self.source,dict(type='comment',record=dict(video_id=VID,comment_id=str(int(VID)+500),text='合成普通评论')))
        col.update(self.base,status='completed',finished_at=NOW);col.ACTIVE.clear()

    def tearDown(self):
        col.ACTIVE.clear();self.threads.stop();self.clock.stop();app.DATA_DIR=self.previous;self.temp.cleanup()

    def save(self,**kw):return discovery.save({'enabled':True,**kw})
    def record(self,rows,source='author',task=None):
        with app.db() as c:discovery.record(c,rows,source=source,target=VID,task=task)
    def plan(self,**kw):return dict({'continuous':1,'kind':'search','transport':'local_browser','run_count':0,'video_limit':3},**kw)
    def choose(self,plan=None,instant=NOW):
        with app.db() as c:return discovery.choose(c,plan or self.plan(),instant)

    def test_durable_unique_library_and_unbiased_author_ratio(self):
        self.save()
        rows=[work(i,related=i<8) for i in range(10)]
        self.record(rows[:8],'search');self.record(rows[:8],'search')
        a=discovery.state()['authors'][0]
        self.assertEqual(a['sampled'],0);self.assertIsNone(a['ratio']);self.assertFalse(a['focused'])
        self.record(rows);self.record(rows)
        a=discovery.state()['authors'][0]
        self.assertEqual((a['sampled'],a['related'],a['ratio'],a['focused']),(10,8,80,True))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM discovery_works').fetchone()[0],10)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],8)
        self.save(enabled=False)
        self.assertEqual(discovery.state()['counts']['related'],8,'Disabling discovery retains assets')

    def test_read_only_state_and_master_stop_prevent_dispatch(self):
        self.save(seed_videos=[VID]);self.record([work()])
        self.assertFalse(discovery.state()['enabled'])
        monitor=monitoring.save(dict(transport='http'))
        monitoring.command('start');self.assertTrue(discovery.state()['enabled'])
        task=scheduler.tick(NOW);self.assertIsNotNone(task)
        with app.db() as c:
            self.assertIsNotNone(discovery.worker_config(c,task))
            self.assertEqual(c.execute('SELECT last_task_id FROM collection_plans WHERE id=?',(monitor['id'],)).fetchone()[0],task)
        monitoring.command('stop')
        self.assertTrue(col.ACTIVE[task]['cancel']);self.assertFalse(discovery.state()['enabled'])
        self.assertIsNone(scheduler.tick(NOW));self.assertEqual(len(col.ACTIVE),1)

    def test_rotation_keywords_authors_and_comments(self):
        self.save();self.record([work(i) for i in range(10)])
        for turn,expected in [(0,'work'),(1,'author'),(2,'work'),(3,'search')]:
            self.assertEqual(self.choose(self.plan(run_count=turn))[1]['channel'],expected)
        selected=[]
        for i in range(6):
            _,job=self.choose(self.plan(run_count=3));selected.append(job['target'])
            with app.db() as c:c.execute('UPDATE discovery_queries SET last_checked_at=?,next_check_at=? WHERE keyword=?',
                (NOW,discovery.future(NOW,300),job['key']))
        self.assertEqual(len(set(selected)),6)
        with app.db() as c:cfg=discovery.config(c)
        self.assertEqual(set(selected),set(cfg['keywords']))

    def test_initial_author_scan_and_focus_frequency(self):
        self.save();self.record([work(i) for i in range(10)])
        _,job=self.choose(self.plan(run_count=1));self.assertEqual(job['author_pages'],3)
        task=col.start(dict(kind='author',target=VID,transport='http',request_id='author-scan'),discovery_job=job)['id']
        col.update(task,status='completed',finished_at=NOW);col.ACTIVE.clear()
        with app.db() as c:
            discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
            discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
        author=discovery.state()['authors'][0]
        self.assertEqual(author['next_check_at'],discovery.future(NOW,90))
        _,next_job=self.choose(self.plan(run_count=1),discovery.future(NOW,91))
        self.assertEqual(next_job['author_pages'],1)
        with app.db() as c:self.assertEqual(discovery.worker_config(c,task)['author_pages'],3,'Existing batch budget is frozen')

    def test_focus_new_works_prioritized_and_author_pause_preserves_library(self):
        self.save();self.record([work(i,author='MS4wLjABAAAA_NORMAL_AUTHOR') for i in range(2)])
        self.record([work(i+10) for i in range(10)])
        _,job=self.choose();self.assertTrue(all(int(v)>=int(VID)+10 for v in job['target'].splitlines()))
        discovery.author_command(dict(sec_uid=AUTHOR,enabled=False,priority='auto'))
        _,job=self.choose();self.assertTrue(all(int(v)<int(VID)+10 for v in job['target'].splitlines()))
        self.assertEqual(discovery.state()['counts']['related'],12)

    def test_paused_fixed_work_is_not_automatically_reenabled(self):
        scheduler.save(dict(kind='video',target=VID,transport='http'))
        self.save();self.record([work()])
        with app.db() as c:self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?',(VID,)).fetchone()[0],0)
        _,job=self.choose();self.assertNotEqual(job['channel'],'work')

    def test_cancelled_and_failed_batches_do_not_claim_completed_checks(self):
        self.save();self.record([work()])
        _,job=self.choose(self.plan(run_count=1))
        task=col.start(dict(kind='author',target=VID,transport='http',request_id='cancelled-scan'),discovery_job=job)['id']
        col.command(task,'cancel')
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            discovery.record(c,[work(8)],source='author',target=VID,task=row)
            self.assertIsNone(c.execute('SELECT 1 FROM discovery_works WHERE video_id=?',(work(8)['video_id'],)).fetchone())
        col.update(task,status='needs_verification',finished_at=NOW);col.ACTIVE.clear()
        with app.db() as c:discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
        self.assertIsNone(discovery.state()['authors'][0]['last_checked_at'])

    def test_asset_identity_conflict_and_invalid_config_rejected(self):
        self.save();self.record([work()])
        with self.assertRaises(ValueError):self.record([work(author='MS4wLjABAAAA_CONFLICT_AUTHOR')])
        for cfg in [dict(keywords=['陪玩']),dict(initial_author_pages=4),dict(focus_ratio=101),dict(enabled='true'),dict(seed_videos=['https://unrelated.test'])]:
            with self.assertRaises(ValueError):self.save(**cfg)

    def test_retry_preserves_failed_job_and_frozen_scope(self):
        self.save();self.record([work()])
        _,job=self.choose(self.plan(run_count=3))
        task=col.start(dict(kind=job['kind'],target=job['target'],transport=job['transport'],request_id='retry-job'),discovery_job=job)['id']
        col.update(task,status='needs_verification',finished_at=NOW);col.ACTIVE.clear()
        enabled,next_job=self.choose(self.plan(run_count=0,last_task_id=task),discovery.future(NOW,300))
        self.assertTrue(enabled);self.assertEqual(next_job,job,'Retry cannot rotate to another keyword or author')

    def test_board_query_count_does_not_grow_with_library(self):
        self.save()
        with app.db() as c:
            for start in range(0,1000,50):discovery.record(c,[work(i,author='') for i in range(start,start+50)],source='search',target='无畏契约')
        queries=[];original=app.db
        @contextmanager
        def traced(mode='live'):
            with original(mode) as connection:
                connection.set_trace_callback(queries.append);yield connection
        with patch('clubops.db',side_effect=traced):board=monitor_board.build([],[])
        self.assertEqual(len(board['rows']),1000)
        self.assertLess(len(queries),20,'Board SQL count must stay independent of library size')


if __name__=='__main__':unittest.main()
