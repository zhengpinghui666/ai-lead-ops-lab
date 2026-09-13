"""Queue integration with temporary data and synthetic local model adapters."""
from contextlib import closing
import http.server
import sqlite3
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import clubops as app
import analysis_store as store
import semantic
import semantic_queue as queue
from test_semantic import SyntheticAdapter, prediction


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='model-queue-test-')
        self.addCleanup(self.temp.cleanup)
        self.old, app.DATA_DIR = app.DATA_DIR, Path(self.temp.name)
        self.addCleanup(setattr, app, 'DATA_DIR', self.old)
        queue.STOP.clear()
        self.assertFalse(queue.ACTIVE)
        app.init()
        self.settings = dict(semantic.DEFAULTS, enabled=True, auto_analyze=True, model='synthetic:1')
        semantic.save(self.settings)

    def add(self, key='first', text='无畏契约找陪练，预算100元',title='无畏契约陪练服务'):
        app.ingest({'records':[dict(comment_id=key, video_id='video1',video_title=title, user_id='12345', text=text)]})
        return app.analyze()

    def jobs(self):
        with app.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM semantic_jobs ORDER BY id')]

    def test_match_discussion_never_enters_model_queue(self):
        result=self.add(text='预测一手 tyloo 2:0 jdg 1:2，刚好完成所有比分',title='无畏契约赛事预测')
        self.assertEqual(result['model_queue']['queued'],0)
        self.assertEqual(self.jobs(),[])
        with patch.object(SyntheticAdapter,'predict') as predict:
            self.assertFalse(queue.run_one(adapter_factory=SyntheticAdapter))
            predict.assert_not_called()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM comments').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM intent_results WHERE method='model'").fetchone()[0],0)

    def test_relevance_gate_keeps_source_and_covers_manual_and_old_queued_jobs(self):
        text='预测一手 tyloo 2:0 jdg 1:2'
        self.add(text=text,title='无畏契约赛事预测')
        with app.db() as c:
            record_id=c.execute('SELECT id FROM comments').fetchone()[0]
            source,row=store.inputs(c,'comment',record_id)
            fingerprint=store.digest(source)
            facts=json.loads(row['facts'])
            self.assertFalse(facts['companion_relevance']['passed'])
            self.assertEqual(row['raw_text'],text)
            self.assertEqual(row['analysis_method'],'rules')
            c.execute("INSERT INTO semantic_jobs(evidence_type,record_id,input_hash,engine,config_json,status,created_at) VALUES('comment',?,?,?,?, 'queued',?)",
                      (record_id,fingerprint,semantic.state()['engine'],json.dumps(self.settings,sort_keys=True),app.now()))
        with patch.object(SyntheticAdapter,'predict') as predict:
            with self.assertRaisesRegex(ValueError,'非垂直对口'):
                semantic.analyze_one(dict(evidence_type='comment',id=record_id,input_hash=fingerprint,request_id='relevance-manual'),adapter_factory=SyntheticAdapter)
            self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
            predict.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'],'skipped')
        self.assertIn('未调用模型',self.jobs()[0]['detail'])
        current=app.state()['comments'][0]
        self.assertFalse(current['companion_relevance']['passed'])
        self.assertFalse(current['model_routing']['model_allowed'])
        with app.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM intent_results WHERE method='model'").fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0],0)

    def test_related_negation_and_sellers_still_reach_intent_stage(self):
        for n,text in enumerate(('不要陪玩，只找队友','陪玩接单','多少钱一小时','求带','昨天刚被骗，来个靠谱的陪玩')):
            self.assertEqual(self.add(str(n),text)['model_queue']['queued'],1,text)

    def test_rules_enqueue_once_and_worker_commits_result_and_job_together(self):
        with patch('http.client.HTTPConnection') as network:
            result = self.add()
            self.assertEqual(result['model_queue']['queued'], 1)
            record_id = self.jobs()[0]['record_id']
            self.assertEqual(queue.enqueue('comment',[record_id])['existing'],1)
            network.assert_not_called()
        self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
        self.assertFalse(queue.run_one(adapter_factory=SyntheticAdapter))
        job = self.jobs()[0]
        self.assertEqual(job['status'], 'completed')
        with app.db() as c:
            model = c.execute('SELECT * FROM intent_results WHERE id=?', (job['result_id'],)).fetchone()
            self.assertEqual((model['status'], model['input_hash']), ('completed', job['input_hash']))
        current = app.state()
        self.assertEqual(current['comments'][0]['analysis_method'],'model')
        self.assertEqual((current['messages'],current['jobs'],current['leads'][0]['contact_basis']),([],[],''))
        self.assertEqual(queue.cancel_all(),{'cancelled':0,'cancelling':0})
        self.assertEqual(self.jobs()[0]['status'],'completed')

    def test_saving_auto_does_not_backfill_history_or_call_service(self):
        semantic.save(dict(self.settings, auto_analyze=False))
        self.add()
        with patch('http.client.HTTPConnection') as network:
            semantic.save(self.settings)
            self.assertEqual(self.jobs(),[])
            network.assert_not_called()
        self.add('new')
        self.assertEqual(len(self.jobs()),1)

    def test_recall_upgrade_only_requeues_current_uncertain_records(self):
        from datetime import datetime, timezone, timedelta
        ids = []
        with patch.object(semantic, 'PROMPT_VERSION', 'intent-prompt-v7'):
            for i, category in enumerate(('buyer', 'noise', 'uncertain', 'uncertain')):
                self.add(str(i))
                with patch.object(SyntheticAdapter, 'predict', side_effect=lambda s, k=category:(prediction(s, k), 'a'*64)):
                    self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
                ids.append(self.jobs()[-1]['record_id'])
        with app.db() as c:
            c.execute('UPDATE comments SET published_at=?', (app.now(),))
            c.execute('UPDATE comments SET published_at=? WHERE id=?', ((datetime.now(timezone.utc)-timedelta(hours=25)).isoformat(), ids[-1]))
            history = [tuple(r) for r in c.execute('SELECT * FROM intent_results')]
        with patch.object(semantic, 'PROMPT_VERSION', 'intent-prompt-v8'):
            self.assertEqual(queue.enqueue('comment', ids)['queued'], 1)
            self.assertEqual(self.jobs()[-1]['record_id'], ids[2])
            self.assertEqual(queue.enqueue('comment', ids)['queued'], 0)
        with app.db() as c:
            self.assertEqual(history, [tuple(r) for r in c.execute('SELECT * FROM intent_results')])

    def test_capacity_overflow_keeps_rules_and_reports_backpressure(self):
        with patch.object(queue,'CAPACITY',1):
            self.add('one')
            result=self.add('two')
            self.assertEqual(result['model_queue']['full'],1)
            self.assertEqual(queue.state()['active'],1)
        self.assertEqual(len(app.state()['comments']),2)
        self.assertTrue(all(r['analysis_method']=='rules' for r in app.state()['comments']))

    def test_live_receive_queues_only_accepted_records_and_skips_human_review(self):
        import live_monitor as live
        import live_workflow
        config=dict(live.DEFAULTS,room_url='https://live.douyin.com/12345',exclude_keywords='接单')
        with app.db() as c:
            import live_room_pool
            live_room_pool.ingest(c,[dict(room_url=config['room_url'],title='无畏契约陪练')],'valorant_category',app.now())
            sid=c.execute('INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                          ('live-queue',config['room_url'],'10000000000000001',json.dumps(config),'running','',app.now(),app.now())).lastrowid
        for index,text in enumerate(('无畏契约找陪练','接单找工作')):
            live.receive(sid,{'type':'message','record':dict(room_id='10000000000000001',uid='10000000000000002',
                          message_id=str(10000000000000003+index),nickname='夹具',text=text,published_at=None)})
        self.assertEqual(len(self.jobs()),1)
        self.assertEqual(self.jobs()[0]['evidence_type'],'live')
        row=live_workflow.detail(self.jobs()[0]['record_id'])
        live_workflow.review(dict(id=row['id'],review_token=row['review_token'],category='uncertain',reason='人工核对'))
        with patch.object(SyntheticAdapter,'predict') as predict:
            queue.run_one(adapter_factory=SyntheticAdapter)
            predict.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'],'skipped')

    def test_changed_source_is_not_sent_using_queued_version(self):
        self.add()
        self.add(text='无畏契约免费组队')
        with patch.object(SyntheticAdapter,'predict') as predict:
            queue.run_one(adapter_factory=SyntheticAdapter)
            predict.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'],'stale')
        self.assertEqual(self.jobs()[1]['status'],'queued')
        queue.run_one(adapter_factory=SyntheticAdapter)
        self.assertEqual(self.jobs()[1]['status'],'completed')

    def test_human_review_before_dispatch_skips_job(self):
        self.add()
        record_id=self.jobs()[0]['record_id']
        app.mutate('review',dict(id=record_id,category='social',reason='人工核对'))
        with patch.object(SyntheticAdapter,'predict') as predict:
            queue.run_one(adapter_factory=SyntheticAdapter)
            predict.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'],'skipped')
        self.assertEqual(app.state()['comments'][0]['category'],'social')

    def test_failure_is_not_retried_and_rules_remain(self):
        self.add()
        with patch.object(SyntheticAdapter,'predict',side_effect=TimeoutError('private secret')) as predict:
            queue.run_one(adapter_factory=SyntheticAdapter)
            queue.run_one(adapter_factory=SyntheticAdapter)
            queue.enqueue('comment',[self.jobs()[0]['record_id']])
            self.assertEqual(predict.call_count,1)
        self.assertEqual(self.jobs()[0]['status'],'failed')
        self.assertNotIn('private secret',str(queue.state()))
        self.assertEqual(app.state()['comments'][0]['analysis_method'],'rules')

    def test_cancel_running_discards_late_prediction_and_pending_jobs(self):
        self.add('one');self.add('two')
        entered=threading.Event();release=threading.Event()
        def delayed(source):
            entered.set()
            self.assertTrue(release.wait(3))
            return prediction(source),'a'*64
        with patch.object(SyntheticAdapter,'predict',side_effect=delayed):
            worker=threading.Thread(target=queue.run_one,kwargs={'adapter_factory':SyntheticAdapter})
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(queue.cancel_all(),dict(cancelled=1,cancelling=1))
            finally:
                release.set();worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual([j['status'] for j in self.jobs()],['cancelled','cancelled'])
        self.assertTrue(all(r['analysis_method']=='rules' for r in app.state()['comments']))

    def test_config_change_stops_inflight_and_waiting_old_settings(self):
        self.add('one');self.add('two')
        def change(source):
            semantic.save(dict(self.settings,model='synthetic:2'))
            return prediction(source),'a'*64
        with patch.object(SyntheticAdapter,'predict',side_effect=change):
            queue.run_one(adapter_factory=SyntheticAdapter)
        self.assertEqual([j['status'] for j in self.jobs()],['cancelled','cancelled'])

    def test_manual_slot_contention_keeps_job_waiting(self):
        self.add()
        with semantic.GUARD:
            self.assertFalse(queue.run_one(adapter_factory=SyntheticAdapter))
        with patch.object(semantic,'analyze_one',side_effect=semantic.ModelBusy()):
            self.assertFalse(queue.run_one(adapter_factory=SyntheticAdapter))
        self.assertEqual(self.jobs()[0]['status'],'queued')
        queue.run_one(adapter_factory=SyntheticAdapter)
        self.assertEqual(self.jobs()[0]['status'],'completed')

    def test_restart_does_not_replay_pending_jobs_and_migration_backs_up(self):
        self.add()
        queue.recover()
        self.assertFalse(queue.run_one(adapter_factory=SyntheticAdapter))
        self.assertEqual(self.jobs()[0]['status'],'interrupted')
        with app.db() as c:c.execute('DROP TABLE semantic_jobs')
        app.init()
        backup=next((app.DATA_DIR/'backups').glob('*before-semantic-queue-*'))
        with closing(sqlite3.connect(backup)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM comments').fetchone()[0],1)
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_jobs'").fetchone())

    def test_current_source_change_during_prediction_stays_historical(self):
        self.add()
        def change(source):
            app.ingest({'records':[dict(comment_id='first',video_id='video1',user_id='12345',text='无畏契约免费组队')]})
            return prediction(source),'a'*64
        with patch.object(SyntheticAdapter,'predict',side_effect=change):
            queue.run_one(adapter_factory=SyntheticAdapter)
        self.assertEqual(self.jobs()[0]['status'],'stale')
        self.assertNotEqual(app.state()['comments'][0]['analysis_method'],'model')


class SocketCancellationTests(unittest.TestCase):
    def test_cancellation_interrupts_local_response_wait(self):
        entered=threading.Event();release=threading.Event();cancel=threading.Event();errors=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                entered.set();release.wait(3)
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads=True
        server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
        adapter=semantic.OllamaAdapter(dict(semantic.DEFAULTS,port=server.server_port,timeout_seconds=5))
        adapter.cancel_event=cancel
        def request():
            try:adapter.request('/api/status')
            except Exception as exc:errors.append(type(exc).__name__)
        worker=threading.Thread(target=request);worker.start()
        try:
            self.assertTrue(entered.wait(2));cancel.set();worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(errors)
        finally:
            release.set();worker.join(3);server.shutdown();server.server_close();server_thread.join(2)


if __name__=='__main__':unittest.main()
