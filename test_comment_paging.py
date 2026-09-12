"""Synthetic pagination, persistence, cancellation and migration checks."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import clubops as app
import collector
import collector_http as http
import collector_http_worker as worker
import comment_paging as paging
from test_collector_http import session,record,body,VIDEO,PARENT


def page(offset,more=True,parents=()):
    return dict(cursor=offset,has_more=more,reply_targets=list(parents))


class RotationTests(unittest.TestCase):
    def test_next_batch_refreshes_head_then_continues_main_and_reply(self):
        r=paging.Rotation()
        r.accept(r.next(),page(10,parents=[PARENT]),True)
        r.accept(r.next(),page(10),True)
        r.accept(r.next(),page(20),True)
        saved=r.snapshot();self.assertEqual(saved['main_cursor'],20)
        r=paging.Rotation(saved)
        self.assertEqual(r.next(),('comments','',0))
        r.accept(r.next(),page(10,parents=[PARENT]),True)
        self.assertEqual(r.next(),('replies',PARENT,10))
        r.accept(r.next(),page(20,False),True)
        self.assertEqual(r.next(),('comments','',20))

    def test_end_and_changed_head_keep_other_reply_work(self):
        r=paging.Rotation(dict(version=paging.VERSION,main_cursor=80,replies=[[PARENT,10]]))
        r.accept(r.next(),page(3,False),True)
        self.assertEqual(r.next(),('replies',PARENT,10))
        r.accept(r.next(),page(11,False),True)
        self.assertIsNone(r.next())
        r=paging.Rotation(r.snapshot())
        r.accept(r.next(),page(10),True)
        self.assertEqual(r.next(),('comments','',10))

    def test_partial_page_does_not_advance(self):
        r=paging.Rotation()
        r.accept(r.next(),page(10),False)
        self.assertEqual(r.snapshot()['main_cursor'],0)
        r=paging.Rotation(dict(version=paging.VERSION,main_cursor=20,replies=[[PARENT,10]]))
        r.accept(r.next(),page(10),True)
        r.accept(r.next(),page(20),False)
        self.assertEqual(r.snapshot()['replies'],[[PARENT,10]])
        self.assertEqual(r.snapshot()['main_cursor'],20)

    def test_capacity_defers_main_progress_and_prioritizes_reply_drain(self):
        targets=[str(7600000000000000000+i) for i in range(paging.MAX_REPLIES)]
        r=paging.Rotation(dict(version=paging.VERSION,main_cursor=20,replies=[[x,0] for x in targets]))
        r.accept(r.next(),page(10),True)
        self.assertEqual(r.next()[0],'replies')
        r.accept(r.next(),page(1,False),True)
        self.assertEqual(r.next()[0],'replies')
        receipt=r.accept(('comments','',20),page(30,parents=['7700000000000000001','7700000000000000002']),True)
        self.assertEqual(receipt['deferred_replies'],1)
        self.assertEqual(r.snapshot()['main_cursor'],20)
        self.assertEqual(len(r.snapshot()['replies']),paging.MAX_REPLIES)

    def test_nonadvancing_cursor_rejected_even_on_last_budget_page(self):
        for next_cursor in (0,-1,True):
            with self.subTest(cursor=next_cursor),self.assertRaises(ValueError):
                paging.Rotation().accept(('comments','',0),page(next_cursor),True)
        with self.assertRaises(ValueError):
            paging.Rotation().accept(('comments','',20),page(10),True)

    def test_invalid_or_duplicate_reply_identity_rejected(self):
        for replies in ([[PARENT,0],[PARENT,1]],[['https://example.test',0]],[[PARENT,True]]):
            with self.assertRaises(ValueError):paging.Rotation(dict(version=paging.VERSION,main_cursor=0,replies=replies))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=app.DATA_DIR;app.DATA_DIR=Path(self.temp.name)
        app.init();self.thread=patch('collector.threading.Thread.start');self.thread.start();collector.ACTIVE.clear()
        self.task=collector.start(dict(kind='video',target=VIDEO,transport='http',comment_limit=20,request_id='paging-fixture'))['id']
        self.source=collector.ACTIVE[self.task]['source_id'];self.tag=paging.session_tag(session())
        collector.update(self.task,status='running')
        collector.checkpoint(self.task,dict(type='targets',records=[dict(video_id=VIDEO,video_title='合成无畏契约陪玩')]))
        collector.checkpoint(self.task,dict(type='checkpoint',video_id=VIDEO,status='reading',detail='合成读取'))

    def tearDown(self):
        collector.ACTIVE.clear();self.thread.stop();app.DATA_DIR=self.old;self.temp.cleanup()

    def message(self):
        r=paging.Rotation();receipt=r.accept(r.next(),page(10),True)
        return dict(type='comment_paging',video_id=VIDEO,session_tag=self.tag,revision=1,state=r.snapshot(),page=receipt)

    def test_commit_reload_scope_expiry_and_revision(self):
        with app.db() as c:
            self.assertTrue(paging.save(c,self.task,self.source,self.message(),self.tag))
            self.assertFalse(paging.save(c,self.task,self.source,self.message(),self.tag))
            rev,state,reason=paging.load(c,self.source,VIDEO,self.tag,app.now())
            self.assertEqual((rev,state['main_cursor'],reason),(1,10,'continued'))
            self.assertEqual(paging.load(c,self.source,VIDEO,'changed',app.now())[1]['main_cursor'],0)
            c.execute("UPDATE collection_page_progress SET updated_at='2000-01-01T00:00:00+00:00'")
            self.assertEqual(paging.load(c,self.source,VIDEO,self.tag,app.now())[2],'expired')
            self.assertEqual(paging.load(c,self.source+1,VIDEO,self.tag,app.now())[0],0)

    def test_cancelled_finished_other_video_and_other_session_cannot_commit(self):
        with app.db() as c:
            m=self.message();m['video_id']='7700000000000000000'
            self.assertFalse(paging.save(c,self.task,self.source,m,self.tag))
            self.assertFalse(paging.save(c,self.task,self.source,self.message(),'changed'))
            c.execute("UPDATE collection_tasks SET status='cancelling'")
            self.assertFalse(paging.save(c,self.task,self.source,self.message(),self.tag))
            c.execute("UPDATE collection_tasks SET status='completed',finished_at=?",(app.now(),))
            self.assertFalse(paging.save(c,self.task,self.source,self.message(),self.tag))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_page_progress').fetchone()[0],0)

    def test_migration_backs_up_old_database_and_does_not_start_requests(self):
        with app.db() as c:
            c.execute('DROP TABLE collection_page_progress')
            before=[tuple(r) for r in c.execute('SELECT * FROM collection_tasks')]
        with patch('collector_http.Client') as network:app.init();network.assert_not_called()
        backups=list((app.DATA_DIR/'backups').glob('*before-comment-paging-*.bak'))
        self.assertEqual(len(backups),1)
        with app.db() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM collection_tasks')],before)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_page_progress').fetchone()[0],0)

    def test_worker_roundtrip_saves_only_after_rows_then_reads_deeper_next_batch(self):
        self.thread.stop()  # Allow the synthetic worker's executor, not a collector process.
        value=session();self.tag=paging.session_tag(value);calls=[];events=[]
        class Client:
            def page(inner,operation,**kw):
                offset=kw['cursor'];calls.append((operation,offset))
                rows=[record(cid=str(7600000000000002000+offset+i),reply_comment_total=0) for i in range(10)]
                return http.parse_page(body(rows,has_more=1,cursor=offset+10),operation,VIDEO)
        def emit(event):
            events.append(event)
            if event['type'] in ('video','comment'):collector.observe(self.task,self.source,event)
            if event['type'] in ('targets','checkpoint'):collector.checkpoint(self.task,event)
            if event['type']=='comment_paging':
                with app.db() as c:
                    self.assertTrue(paging.save(c,self.task,self.source,event,self.tag))
                    self.assertGreater(c.execute("SELECT COUNT(*) FROM collection_observations WHERE kind='comment'").fetchone()[0],0)
        cfg=dict(kind='video',target=VIDEO,page_concurrency=1,video_limit=1,comment_limit=20,
                 paging_source_id=self.source,paging_session_tag=self.tag)
        worker.collect(cfg,emit,threading.Event(),client=Client(),session=value)
        self.assertEqual(events[-1]['status'],'completed',[e for e in events if e['type'] in ('diagnostic','status')])
        self.assertEqual(calls,[('comments',0),('comments',10)])
        self.assertEqual(events[-1]['status'],'completed')
        collector.update(self.task,status='completed',finished_at=app.now());collector.ACTIVE.clear()
        with patch('collector.threading.Thread.start'):
            self.task=collector.start(dict(kind='video',target=VIDEO,transport='http',comment_limit=20,request_id='paging-fixture-next'))['id']
        collector.update(self.task,status='running')
        calls.clear();worker.collect(cfg,emit,threading.Event(),client=Client(),session=value)
        self.assertEqual(calls,[('comments',0),('comments',20)])
        with app.db() as c:self.assertEqual(paging.load(c,self.source,VIDEO,self.tag,app.now())[1]['main_cursor'],30)

    def test_worker_cancelled_after_a_row_emits_no_cursor_commit(self):
        self.thread.stop()
        value=session();cancel=threading.Event();events=[]
        class Client:
            def page(inner,operation,**kw):return http.parse_page(body([record()],has_more=1,cursor=10),operation,VIDEO)
        def emit(event):
            events.append(event)
            if event['type']=='comment':cancel.set()
        worker.collect(dict(kind='video',target=VIDEO,page_concurrency=1,video_limit=1,comment_limit=20,
            paging_source_id=self.source,paging_session_tag=paging.session_tag(value)),emit,cancel,client=Client(),session=value)
        self.assertFalse(any(e['type']=='comment_paging' for e in events))


if __name__=='__main__':unittest.main()
