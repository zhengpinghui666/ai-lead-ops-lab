"""Isolated author discovery → selection → collection and recovery contracts."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import clubops as app
import collector
import collection_scheduler as scheduler
import monitoring
import collector_http as http
import collector_http_worker as worker
import video_discovery as discovery
import video_metadata
from test_video_discovery import item, AUTHOR
from test_collector_http import VIDEO, body, record

NEW='7682684948067890553'
OTHER='7678936399852233590'


class AuthorWorkerTests(unittest.TestCase):
    def run_worker(self, *, rows=None, failure=None, resume=False):
        calls=[]
        class Client:
            def page(self,operation,**kw):
                calls.append((operation,kw))
                if operation=='detail':
                    return discovery.parse_discovery({'status_code':0,'aweme_detail':item()},operation,video=VIDEO)
                if operation=='author':
                    if failure:raise http.ReadError(failure)
                    return discovery.parse_discovery({'status_code':0,'aweme_list':rows,'has_more':0},operation,sec_uid=AUTHOR)
                if operation!='comments':raise AssertionError('unexpected expansion')
                return http.parse_page(body([record(cid=kw['video'],aweme_id=kw['video'])]),operation,kw['video'],title=kw['title'])
        config=dict(kind='author',target=VIDEO,video_limit=1,comment_limit=1,page_concurrency=1,
                    resolve_video_titles=True,refresh_video_metrics=True)
        if resume:
            config['resume_targets']=[dict(video_id=NEW,video_title='无畏契约',video_url='https://www.douyin.com/video/'+NEW,
                                          metrics=video_metadata.extract({}))]
        events=[]
        worker.collect(config,events.append,threading.Event(),client=Client())
        return calls,events

    def test_newest_relevant_within_returned_candidates_then_comments(self):
        calls,events=self.run_worker(rows=[item(create_time=1),item(OTHER,desc='其他游戏',create_time=30),item(NEW,create_time=20)])
        self.assertEqual([op for op,_ in calls],['detail','author','comments'])
        self.assertEqual(calls[-1][1]['video'],NEW)
        audit=next(e['snapshot']['responses'][0] for e in events if e.get('stage')=='author_discovery')
        self.assertEqual((audit['candidate_count'],audit['relevant_count'],audit['selected_count']),(3,2,1))
        self.assertFalse(audit['all_douyin'])
        self.assertEqual(next(e['record']['video_id'] for e in events if e['type']=='comment'),NEW)
        self.assertEqual(events[-1]['status'],'completed')

    def test_no_relevant_candidate_does_not_fall_back_to_search(self):
        calls,events=self.run_worker(rows=[item(desc='无相关内容')])
        self.assertEqual([op for op,_ in calls],['detail','author'])
        self.assertEqual(events[-1]['status'],'no_data')
        self.assertFalse(any(e['type']=='targets' for e in events))

    def test_failed_author_read_stops_before_comment_reading(self):
        for failure in ('needs_verification','rate_limited','schema_changed','resource_limited'):
            calls,events=self.run_worker(failure=failure)
            self.assertEqual([op for op,_ in calls],['detail','author'])
            self.assertEqual(events[-1]['status'],failure)
            self.assertFalse(any(e['type']=='targets' for e in events))

    def test_resume_uses_selected_targets_without_author_rediscovery(self):
        calls,events=self.run_worker(resume=True)
        self.assertEqual([op for op,_ in calls],['comments'])
        self.assertEqual(events[-1]['status'],'completed')

    def test_options_author_seed_limit_http_only_and_selection_limit(self):
        base=dict(kind='author',target=VIDEO,transport='http',request_id='fixture',video_limit=4)
        self.assertEqual(collector.options(base)[2],4)
        self.assertEqual(collector.options({**base,'target':VIDEO+'\n'+VIDEO})[1],'https://www.douyin.com/video/'+VIDEO)
        for change in ({'transport':'local_browser'},{'target':'\n'.join([VIDEO,NEW,OTHER,'760000000000000001'])}):
            with self.assertRaisesRegex(ValueError,'作者作品发现'):
                collector.options({**base,**change})


class AuthorLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old=app.DATA_DIR;app.DATA_DIR=Path(self.temp.name)
        self.addCleanup(setattr,app,'DATA_DIR',old)
        app.init();collector.ACTIVE.clear();self.addCleanup(collector.ACTIVE.clear)
        threads=patch('collector.threading.Thread.start');threads.start();self.addCleanup(threads.stop)

    def test_author_plan_and_monitor_save_paused_then_dispatch_preserves_scope(self):
        config=dict(kind='author',target=VIDEO,video_limit=2,transport='http',comment_limit=10,run_limit=1)
        monitor=monitoring.save(config)
        pid=scheduler.save(config)['id']
        self.assertFalse(monitor['enabled'])
        self.assertIsNone(scheduler.tick())
        baseline=collector.start(dict(kind='video',target=VIDEO,transport='http',request_id='baseline'))['id']
        collector.update(baseline,status='completed',comments=1,finished_at=app.now());collector.ACTIVE.clear()
        scheduler.command(pid,'start')
        dispatched=scheduler.tick()
        with app.db() as c:
            task=dict(c.execute('SELECT * FROM collection_tasks WHERE id=?',(dispatched,)).fetchone())
        self.assertEqual((task['kind'],task['transport'],task['video_limit']),('author','http',2))
        self.assertEqual(task['target'],'https://www.douyin.com/video/'+VIDEO)

    def test_author_resume_keeps_pending_selection_and_original_filters(self):
        tid=collector.start(dict(kind='author',target=VIDEO,video_limit=2,transport='http',request_id='discovery'),lookback_hours=1)['id']
        collector.checkpoint(tid,dict(type='targets',records=collector.video_targets(NEW+'\n'+OTHER)))
        collector.checkpoint(tid,dict(type='checkpoint',video_id=NEW,status='done'))
        collector.update(tid,status='cancelled',finished_at=app.now());collector.ACTIVE.clear()
        resumed=collector.resume(tid,'resume')['id']
        with app.db() as c:
            old=dict(c.execute('SELECT * FROM collection_tasks WHERE id=?',(tid,)).fetchone())
            new=dict(c.execute('SELECT * FROM collection_tasks WHERE id=?',(resumed,)).fetchone())
            self.assertEqual([r[0] for r in c.execute('SELECT video_id FROM collection_checkpoints WHERE task_id=?',(resumed,))],[OTHER])
            for key in ('kind','target','transport','comment_since','lookback_hours'):
                self.assertEqual(new[key],old[key],key)
        with self.assertRaises(ValueError):
            collector.checkpoint(resumed,dict(type='targets',records=collector.video_targets(NEW)))

    def test_new_fixed_video_batch_reuses_source_scoped_metadata_without_checkpoints(self):
        tid=collector.start(dict(kind='video',target=VIDEO,transport='http',request_id='old'))['id']
        source=collector.ACTIVE[tid]['source_id']
        metrics=video_metadata.extract({'statistics':{'digg_count':17}})
        collector.observe(tid,source,dict(type='video',record=dict(video_id=VIDEO,video_title='无畏契约作品',metrics=metrics)))
        collector.update(tid,status='completed',finished_at=app.now());collector.ACTIVE.clear()
        fresh=collector.start(dict(kind='video',target=VIDEO,transport='http',request_id='fresh'))['id']
        with app.db() as c:
            task=dict(c.execute('SELECT * FROM collection_tasks WHERE id=?',(fresh,)).fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_checkpoints WHERE task_id=?',(fresh,)).fetchone()[0],0)
            titles,cache=collector.cached_video_metadata(c,task,source,[])
            self.assertEqual(titles[VIDEO],'无畏契约作品')
            self.assertEqual(cache[VIDEO]['likes'],17)
            self.assertEqual(collector.cached_video_metadata(c,task,-1,[]),({},{}))
