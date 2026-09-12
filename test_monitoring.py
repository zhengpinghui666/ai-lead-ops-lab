"""Synthetic, isolated monitor tests. Never launches a browser or sends messages."""
import tempfile
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import clubops as app
import collector as col
import collection_scheduler as sch
import monitoring as mon
import comment_filters as filters

NOW = '2026-09-09T10:00:00+00:00'
VIDEO = '7600000000000000001'


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='monitor-test-')
        self.old = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()
        self.threads = patch('collector.threading.Thread.start')
        self.threads.start()
        self.clock = patch('clubops.now', return_value=NOW)
        self.now = self.clock.start()
        col.ACTIVE.clear()

    def tearDown(self):
        col.ACTIVE.clear()
        self.threads.stop()
        self.clock.stop()
        app.DATA_DIR = self.old
        self.temp.cleanup()

    def task(self, **kw):
        return col.start({'kind': 'search', 'target': '无畏契约陪玩', 'request_id': 'fixture-' + str(len(col.state()['tasks'])), 'video_limit': 1, 'comment_limit': 20}, **kw)['id']

    def video(self, task):
        col.observe(task, col.state()['source_id'], {'type': 'video', 'record': {'video_id': VIDEO, 'video_title': '无畏契约陪玩合成测试'}})

    def comment(self, task, cid='7600000000000000002', published=NOW, text='找无畏契约陪练，预算100'):
        col.observe(task, col.state()['source_id'], {'type': 'comment', 'record': {'video_id': VIDEO, 'comment_id': cid,
            'user_id': '123456789012', 'text': text, 'published_at': published}})

    def finish(self, task, status='completed'):
        col.update(task, status=status, finished_at=app.now())
        col.ACTIVE.pop(task, None)

    def baseline(self):
        task = self.task()
        self.video(task)
        self.comment(task)
        self.finish(task)
        return task

    def test_defaults_and_reads_do_not_create_or_dispatch(self):
        for _ in range(3):
            state = mon.state()
            self.assertFalse(state['enabled'])
            self.assertIsNone(state['id'])
            self.assertEqual(state['lookback_hours'], 1)
            self.assertEqual(state['interval_seconds'], 30)
            self.assertEqual(state['transport'], 'local_browser')
            self.assertIsNone(sch.tick())
        mon.command('stop')
        self.assertEqual(sch.state(), [])
        self.assertEqual(col.state()['tasks'], [])

    def upstream_failure(self, task, snapshot=None, stage='finished-error'):
        if snapshot is None:
            snapshot = {'navigation_http_status': 502, 'responses': []}
        with app.db() as c:
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                      (task, stage, json.dumps(snapshot), app.now()))
        self.finish(task, 'network_error')

    def at(self, instant):
        self.now.return_value = instant
        return sch.tick(instant)

    def test_gateway_retries_three_times_then_stops_without_changing_scope(self):
        self.baseline();mon.save({});mon.command('start');task=sch.tick(NOW)
        for delay in (60, 120, 240):
            finished=app.now();self.upstream_failure(task)
            self.assertIsNone(sch.tick(finished))
            due=(datetime.fromisoformat(finished)+timedelta(seconds=delay)).isoformat()
            self.assertEqual(mon.state()['next_run_at'],due)
            self.assertTrue(mon.state()['enabled'])
            self.assertIsNone(self.at((datetime.fromisoformat(due)-timedelta(seconds=1)).isoformat()))
            task=self.at(due)
            self.assertIsNotNone(task)
            with app.db() as c:
                row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertEqual((row['kind'],row['target'],row['transport'],row['interactive']),
                             ('search','无畏契约陪玩','local_browser',0))
            self.assertEqual((row['video_limit'],row['comment_limit'],row['page_concurrency'],row['lookback_hours']), (3,30,1,1))
        self.upstream_failure(task);self.assertIsNone(sch.tick())
        self.assertEqual(mon.state()['status'],'attention');self.assertFalse(mon.state()['enabled'])
        self.assertIsNone(self.at('2026-09-10T10:00:00+00:00'))
        self.assertEqual(mon.state()['run_count'],4)

    def test_successful_batch_resets_gateway_backoff(self):
        self.baseline();mon.command('start');task=sch.tick(NOW)
        self.upstream_failure(task);sch.tick();task=self.at(mon.state()['next_run_at'])
        self.finish(task);sch.tick();task=self.at(mon.state()['next_run_at'])
        finished=app.now();self.upstream_failure(task);sch.tick()
        self.assertEqual(mon.state()['next_run_at'],(datetime.fromisoformat(finished)+timedelta(seconds=60)).isoformat())

    def reply_evidence(self):
        return [dict(operation='identity',transport='http',http_status=200,status='identity_verified',verification_indicated=False),
                dict(operation='comments',transport='http',http_status=200,status='valid_page',video_id=VIDEO,rows=10,skipped_reasons={'invalid_record':0}),
                dict(operation='replies',transport='http',http_status=200,status='schema_changed',video_id=VIDEO,
                     parent_comment_id='7600000000000000002',reason='invalid_page_container',
                     response_shape=dict(version='http-page-shape-v1',status_code_type='int',status_code=0,verification_indicated=False))]

    def reply_failure(self,task,evidence=None):
        with app.db() as c:
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                      (task,'http_read',json.dumps({'responses':evidence or self.reply_evidence()}),app.now()))
        self.finish(task,'schema_changed')

    def http_baseline(self):
        task=col.start(dict(kind='video',target=VIDEO,transport='http',request_id='http-baseline'))['id']
        self.video(task);self.comment(task);self.finish(task)
        mon.save(dict(kind='video',target=VIDEO,transport='http'))
        mon.command('start')

    def test_reply_envelope_retries_three_times_and_keeps_failure_history(self):
        self.http_baseline();task=sch.tick(NOW);history=[]
        for delay in (60,120,240):
            history.append(task);self.reply_failure(task);finished=app.now();sch.tick()
            due=(datetime.fromisoformat(finished)+timedelta(seconds=delay)).isoformat()
            self.assertTrue(mon.state()['enabled']);self.assertEqual(mon.state()['next_run_at'],due)
            task=self.at(due)
            with app.db() as c:row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertEqual((row['kind'],row['target'],row['transport']),('video',col.canonical_video(VIDEO),'http'))
        self.reply_failure(task);sch.tick()
        self.assertEqual(mon.state()['status'],'attention')
        with app.db() as c:
            self.assertTrue(all(c.execute('SELECT status FROM collection_tasks WHERE id=?',(t,)).fetchone()[0]=='schema_changed' for t in history))

    def test_reply_retry_stop_and_success_reset(self):
        self.http_baseline();task=sch.tick(NOW);self.reply_failure(task);sch.tick()
        task=self.at(mon.state()['next_run_at']);self.finish(task);sch.tick()
        task=self.at(mon.state()['next_run_at']);self.reply_failure(task);finished=app.now();sch.tick()
        self.assertEqual(mon.state()['next_run_at'],(datetime.fromisoformat(finished)+timedelta(seconds=60)).isoformat())
        mon.command('stop');self.assertIsNone(self.at('2026-09-10T10:00:00+00:00'))

    def test_page_progress_receipts_do_not_hide_http_retry_evidence(self):
        self.http_baseline();task=sch.tick(NOW);self.reply_failure(task)
        with app.db() as c:
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                (task,'comment_paging',json.dumps({'processing':{'version':'comment-page-rotation-v1','revision':1}}),NOW))
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertEqual(sch.transient_reply_wait(c,row),0)
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                (task,'needs_verification','{}',NOW))
            self.assertIsNone(sch.transient_reply_wait(c,row))

    def test_discovery_reply_retry_keeps_http_job_in_browser_search_monitor(self):
        import discovery_tracking as discovery
        self.baseline();mon.save({});discovery.save({'enabled':True});mon.command('start')
        task=sch.tick(NOW)
        with app.db() as c:frozen=discovery.worker_config(c,task)
        self.assertEqual((frozen['kind'],frozen['transport']),('video','http'))
        self.reply_failure(task);sch.tick()
        self.assertTrue(mon.state()['enabled']);due=mon.state()['next_run_at']
        discovery.save({'keywords':['无畏契约复盘'],'work_interval':120})
        retried=self.at(due)
        with app.db() as c:
            self.assertEqual(discovery.worker_config(c,retried),frozen)
            self.assertEqual(c.execute('SELECT transport FROM collection_tasks WHERE id=?',(retried,)).fetchone()[0],'http')
        self.assertEqual(mon.state()['target'],'无畏契约陪玩')

    def test_reply_retry_rejects_gates_missing_evidence_and_bad_records(self):
        task=col.start(dict(kind='video',target=VIDEO,transport='http',request_id='reply-rejections'))['id']
        self.finish(task,'schema_changed')
        cases=[[],self.reply_evidence()[1:]]
        for change in ({'status':'needs_verification'},{'http_status':403},{'operation':'comments'},
                       {'video_id':'7600000000000000999'},{'parent_comment_id':''},{'reason':'invalid_json'},
                       {'reason':'invalid_record'},{'response_shape':{}},
                       {'response_shape':dict(version='http-page-shape-v1',status_code_type='int',status_code=0,verification_indicated=True)}):
            value=self.reply_evidence();value[-1].update(change);cases.append(value)
        value=self.reply_evidence();value[1]['skipped_reasons']['invalid_record']=1;cases.append(value)
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            for evidence in cases:
                c.execute('DELETE FROM collection_diagnostics WHERE task_id=?',(task,))
                c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                          (task,'http_read',json.dumps({'responses':evidence}),NOW))
                self.assertIsNone(sch.transient_reply_wait(c,row))
            self.assertEqual(sch.transient_retry_seconds(c,{'continuous':0},row),0)

    def identity_network_failure(self,task,**changes):
        e=dict(operation='identity',transport='http',status='http_failed',identity_check_version='identity-check-v2',
               http_attempts=1,transport_error='timeout',transport_phase='response_headers',response_bytes=0)
        e.update(changes)
        with app.db() as c:
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                      (task,'http_read',json.dumps({'responses':[e]}),app.now()))
        self.finish(task,'network_error')

    def test_identity_network_retries_frozen_discovery_scope_then_stops(self):
        import discovery_tracking as discovery
        self.baseline();mon.save({});discovery.save({'enabled':True});mon.command('start')
        task=sch.tick(NOW)
        with app.db() as c:frozen=discovery.worker_config(c,task)
        history=[]
        for delay in (60,120,240):
            history.append(task);self.identity_network_failure(task);finished=app.now();sch.tick()
            due=(datetime.fromisoformat(finished)+timedelta(seconds=delay)).isoformat()
            self.assertTrue(mon.state()['enabled']);self.assertEqual(mon.state()['next_run_at'],due)
            self.assertIsNone(self.at((datetime.fromisoformat(due)-timedelta(seconds=1)).isoformat()))
            task=self.at(due)
            with app.db() as c:self.assertEqual(discovery.worker_config(c,task),frozen)
        self.identity_network_failure(task);sch.tick();self.assertEqual(mon.state()['status'],'attention')
        with app.db() as c:
            self.assertTrue(all(c.execute('SELECT status FROM collection_tasks WHERE id=?',(t,)).fetchone()[0]=='network_error' for t in history))

    def test_identity_network_recovery_resets_delay_and_honors_stop(self):
        self.http_baseline();task=sch.tick(NOW);self.identity_network_failure(task);sch.tick()
        task=self.at(mon.state()['next_run_at']);self.finish(task);sch.tick()
        task=self.at(mon.state()['next_run_at']);self.identity_network_failure(task);finished=app.now();sch.tick()
        self.assertEqual(mon.state()['next_run_at'],(datetime.fromisoformat(finished)+timedelta(seconds=60)).isoformat())
        mon.command('stop');self.assertIsNone(self.at('2026-09-10T10:00:00+00:00'))

    def test_identity_network_retry_requires_transport_evidence_only(self):
        task=col.start(dict(kind='video',target=VIDEO,transport='http',request_id='identity-rejections'))['id']
        self.identity_network_failure(task)
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            valid=json.loads(c.execute('SELECT snapshot FROM collection_diagnostics WHERE task_id=?',(task,)).fetchone()[0])['responses'][0]
            self.assertEqual(sch.transient_identity_wait(c,row),0)
            for change in ({'http_status':401},{'http_status':403},{'http_status':429},{'http_status':'200'},
                    {'transport_error':'tls_verification_failed'},{'transport_error':'transport_failed'},
                    {'transport_phase':'unknown'},{'identity_check_version':'old'},{'http_attempts':True},
                    {'verification_indicated':True},{'business_code':0},{'user_present':True},
                    {'status':'account_mismatch'},{'operation':'comments'},{'response_bytes':None}):
                with self.subTest(change=change):
                    c.execute('UPDATE collection_diagnostics SET snapshot=? WHERE task_id=?',
                              (json.dumps({'responses':[{**valid,**change}]}),task))
                    self.assertIsNone(sch.transient_identity_wait(c,row))
            c.execute('UPDATE collection_diagnostics SET snapshot=? WHERE task_id=?',(json.dumps({'responses':[valid]}),task))
            self.assertIsNone(sch.transient_identity_wait(c,{**dict(row),'status':'identity_failed'}),'Old failed identity cannot be reclassified')
            self.assertIsNone(sch.transient_identity_wait(c,{**dict(row),'videos':1}))
            self.assertEqual(sch.transient_retry_seconds(c,{'continuous':0},row),0)

    def test_explicit_comment_document_timeout_uses_bounded_recovery(self):
        self.baseline();mon.command('start');task=sch.tick(NOW)
        snapshot={'navigation_error':'navigation_timeout','navigation_http_status':None,
                  'page_url':'https://www.douyin.com/video/'+VIDEO,'responses':[{'kind':'search','status':200}]}
        self.upstream_failure(task,snapshot,stage='document-timeout');sch.tick()
        self.assertEqual(mon.state()['next_run_at'],'2026-09-09T10:01:00+00:00')
        self.assertTrue(mon.state()['enabled'])
        self.assertIsNone(self.at('2026-09-09T10:00:59+00:00'))
        self.assertIsNotNone(self.at('2026-09-09T10:01:00+00:00'))

    def incomplete_page(self, task):
        col.checkpoint(task,{'type':'targets','records':[{'video_id':VIDEO}]})
        self.video(task);self.comment(task)
        col.checkpoint(task,{'type':'checkpoint','video_id':VIDEO,'status':'partial','detail':'取得部分评论，但页面未能继续加载'})
        self.finish(task,'partial')

    def test_incomplete_pages_retry_with_backoff_and_preserve_partial_history(self):
        self.baseline();mon.command('start');task=sch.tick(NOW)
        history=[]
        for delay in (60,120,240):
            history.append(task);self.incomplete_page(task);finished=app.now();sch.tick()
            due=(datetime.fromisoformat(finished)+timedelta(seconds=delay)).isoformat()
            self.assertEqual(mon.state()['next_run_at'],due)
            self.assertTrue(mon.state()['enabled'])
            self.assertIn('部分评论',mon.state()['detail'])
            self.assertIsNone(self.at((datetime.fromisoformat(due)-timedelta(seconds=1)).isoformat()))
            task=self.at(due)
        self.incomplete_page(task);sch.tick();self.assertFalse(mon.state()['enabled'])
        with app.db() as c:
            self.assertTrue(all(c.execute('SELECT status FROM collection_tasks WHERE id=?',(t,)).fetchone()[0]=='partial' for t in history))

    def test_incomplete_page_retry_rejects_parser_gates_and_unread_targets(self):
        task=self.task();self.incomplete_page(task)
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertEqual(sch.transient_batch_wait(c,row),0)
            self.assertIsNone(sch.transient_batch_wait(c,{**dict(row),'skipped':2}))
            for stage,snapshot in [('needs_verification',{}),('finished-error',{'responses':[{'status':403}]}),
                    ('finished-error',{'responses':[{'body_error':'invalid_json','status':200}]}),
                    ('finished-error',{'navigation_error':'navigation_timeout'})]:
                c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',(task,stage,json.dumps(snapshot),NOW))
                self.assertIsNone(sch.transient_batch_wait(c,row))
                c.execute('DELETE FROM collection_diagnostics WHERE task_id=?',(task,))
            for status,detail in [('reading',''),('partial','1 次响应解析失败'),('done','已读完本批预算')]:
                c.execute('UPDATE collection_checkpoints SET status=?,detail=? WHERE task_id=?',(status,detail,task))
                self.assertIsNone(sch.transient_batch_wait(c,row))

    def test_restart_and_explicit_stop_do_not_auto_resume_incomplete_page(self):
        self.baseline();mon.command('start');task=sch.tick(NOW)
        self.incomplete_page(task);sch.tick();mon.command('stop')
        self.assertIsNone(self.at('2026-09-09T11:00:00+00:00'))
        mon.command('start');self.assertTrue(mon.state()['enabled'])
        sch.recover();self.assertFalse(mon.state()['enabled'])
        self.assertIsNone(self.at('2026-09-09T12:00:00+00:00'))

    def test_nontext_quality_allows_retry_but_requires_complete_consistent_evidence(self):
        task=self.task();self.incomplete_page(task)
        page='https://www.douyin.com/video/'+VIDEO
        info={'version':'comment-quality-v1','recognized':True,'skipped':2,'non_text_skipped':2,'invalid_records':0,'parse_errors':0}
        with app.db() as c:
            row={**dict(c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()),'skipped':2}
            def evidence(snapshot):
                c.execute('DELETE FROM collection_diagnostics WHERE task_id=?',(task,))
                c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                          (task,'comment-read',json.dumps(snapshot),NOW))
            valid={'page_url':page,'processing':info,'navigation_http_status':200,'responses':[{'status':200}]}
            evidence(valid);self.assertEqual(sch.transient_batch_wait(c,row),0)
            for changes in [{'version':'unknown'},{'recognized':False},{'skipped':'2'},{'non_text_skipped':1},
                            {'invalid_records':1},{'parse_errors':1}]:
                evidence({**valid,'processing':{**info,**changes}})
                self.assertIsNone(sch.transient_batch_wait(c,row))
            evidence({**valid,'page_url':page+'9'});self.assertIsNone(sch.transient_batch_wait(c,row))
            evidence(valid)
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                      (task,'comment-read',json.dumps(valid),NOW))
            self.assertIsNone(sch.transient_batch_wait(c,row))
            evidence(valid)
            c.execute('INSERT INTO collection_checkpoints(task_id,video_id,video_title,video_url,status,detail,updated_at) VALUES(?,?,?,?,?,?,?)',
                      (task,VIDEO+'9','synthetic',page+'9','done','read',NOW))
            self.assertIsNone(sch.transient_batch_wait(c,row))

    def test_timeout_recovery_rejects_ambiguous_or_gated_evidence(self):
        task=self.task();self.finish(task,'network_error')
        base={'navigation_error':'navigation_timeout','navigation_http_status':None,
              'page_url':'https://www.douyin.com/video/'+VIDEO,'responses':[]}
        cases=[('finished-error',base),('document-timeout',{**base,'navigation_error':'navigation_failed'}),
               ('document-timeout',{**base,'page_url':'https://www.douyin.com/search/test'}),
               ('document-timeout',{**base,'navigation_http_status':200}),
               ('document-timeout',{**base,'responses':[{'kind':'comment','status':200}]}),
               ('document-timeout',{**base,'responses':[{'status':403}]}),
               ('document-timeout',{**base,'responses':[{'status':'needs_verification'}]})]
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            for stage,snapshot in cases:
                with self.subTest(stage=stage,snapshot=snapshot):
                    c.execute('DELETE FROM collection_diagnostics WHERE task_id=?',(task,))
                    c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                              (task,stage,json.dumps(snapshot),NOW))
                    self.assertIsNone(sch.transient_http_wait(c,row))

    def test_gateway_wait_respects_retry_after_and_configured_interval(self):
        self.baseline();mon.save({'interval_seconds':300});mon.command('start');task=sch.tick(NOW)
        self.upstream_failure(task,{'responses':[{'status':503,'retry_after_seconds':900}]});sch.tick()
        self.assertEqual(mon.state()['next_run_at'],'2026-09-09T10:15:00+00:00')
        task=self.at(mon.state()['next_run_at']);self.finish(task);sch.tick()
        task=self.at(mon.state()['next_run_at']);finished=app.now();self.upstream_failure(task);sch.tick()
        self.assertEqual(mon.state()['next_run_at'],(datetime.fromisoformat(finished)+timedelta(seconds=300)).isoformat())

    def test_gateway_cooldown_stop_and_restart_do_not_resume(self):
        self.baseline();mon.command('start');task=sch.tick(NOW)
        self.upstream_failure(task);sch.tick();mon.command('stop')
        self.assertIsNone(self.at('2026-09-09T10:10:00+00:00'));self.assertFalse(mon.state()['enabled'])
        self.baseline();mon.command('start');task=sch.tick()
        self.upstream_failure(task);sch.tick();sch.recover()
        self.assertIsNone(self.at('2026-09-09T11:00:00+00:00'));self.assertFalse(mon.state()['enabled'])

    def test_gateway_retry_requires_numeric_unambiguous_eligible_evidence(self):
        task=self.task();self.finish(task,'network_error')
        rejected=[{}, {'title':'502 Bad Gateway','navigation_http_status':200},
                  {'navigation_http_status':'502'}, {'navigation_http_status':501},
                  {'navigation_http_status':502,'navigation_retry_after_seconds':3601},
                  {'navigation_http_status':502,'navigation_retry_after_seconds':-1},
                  {'navigation_http_status':502,'navigation_retry_after_seconds':'120'}]
        # Build mixed evidence separately so each response is independently checked.
        rejected.extend({'navigation_http_status':502,'responses':[{'status':s}]} for s in (401,403,429,'needs_verification','needs_login',501))
        rejected.extend([{'navigation_http_status':502,'responses':{}}, {'navigation_http_status':502,'responses':[None]}])
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            for snapshot in rejected:
                with self.subTest(snapshot=snapshot):
                    c.execute('DELETE FROM collection_diagnostics WHERE task_id=?',(task,))
                    c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',(task,'finished-error',json.dumps(snapshot),NOW))
                    self.assertIsNone(sch.transient_http_wait(c,row))
            c.execute('DELETE FROM collection_diagnostics WHERE task_id=?',(task,))
            for stage in ('captcha_workflow','captcha_dom','needs_login','needs_verification'):
                c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',(task,stage,'{"navigation_http_status":502}',NOW))
                self.assertIsNone(sch.transient_http_wait(c,row))
            c.execute('DELETE FROM collection_diagnostics WHERE task_id=?',(task,))
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',(task,'finished-error','{broken',NOW))
            self.assertIsNone(sch.transient_http_wait(c,row))

    def test_gateway_retry_excludes_finite_manual_and_other_transport(self):
        self.baseline()
        plan=sch.save({'kind':'search','target':'无畏契约陪玩','interval_seconds':300,'run_limit':5})
        sch.command(plan['id'],'start');task=sch.tick(NOW)
        self.upstream_failure(task);sch.tick()
        self.assertEqual(sch.state()[0]['status'],'attention')
        manual=self.task();self.upstream_failure(manual)
        self.assertIsNone(self.at('2026-09-09T11:00:00+00:00'))
        with app.db() as c:
            row=dict(c.execute('SELECT * FROM collection_tasks WHERE id=?',(manual,)).fetchone())
            row['transport']='http';self.assertIsNone(sch.transient_http_wait(c,row))

    def test_gateway_wait_is_cancelled_by_new_account_gate(self):
        self.baseline();mon.command('start');task=sch.tick(NOW)
        self.upstream_failure(task);sch.tick()
        manual=self.task();self.finish(manual,'rate_limited')
        self.assertIsNone(self.at('2026-09-09T10:01:00+00:00'))
        self.assertEqual(mon.state()['status'],'attention');self.assertFalse(mon.state()['enabled'])

    def test_explicit_monitor_start_after_gateway_failure_requires_same_proven_scope(self):
        task=self.task();self.upstream_failure(task)
        with self.assertRaises(ValueError):mon.command('start')
        self.baseline();task=self.task()
        self.upstream_failure(task,{'navigation_http_status':503,'navigation_retry_after_seconds':180})
        mon.command('start')
        self.assertTrue(mon.state()['enabled'])
        self.assertEqual(mon.state()['next_run_at'],'2026-09-09T10:03:00+00:00')
        self.assertIsNone(self.at('2026-09-09T10:02:59+00:00'))
        task=self.at('2026-09-09T10:03:00+00:00')
        self.assertIsNotNone(task);self.finish(task);sch.tick();mon.command('stop')

    def test_gateway_start_rejects_unproven_target_and_excessive_retry_after(self):
        self.baseline();task=self.task()
        self.upstream_failure(task,{'navigation_http_status':502,'navigation_retry_after_seconds':3601})
        with self.assertRaises(ValueError):mon.command('start')
        self.upstream_failure(task)
        with app.db() as c:
            c.execute('DELETE FROM collection_diagnostics WHERE task_id=? AND snapshot LIKE ?',(task,'%3601%'))
            c.execute('UPDATE collection_tasks SET target=? WHERE id=?',('无畏契约陪练',task))
        mon.save({'target':'无畏契约陪练'})
        with self.assertRaises(ValueError):mon.command('start')

    def test_continuous_monitor_dispatches_without_a_browser_window(self):
        self.baseline()
        mon.save({})
        mon.command('start')
        task = sch.tick(NOW)
        with app.db() as c:
            row = c.execute('SELECT interactive,transport FROM collection_tasks WHERE id=?', (task,)).fetchone()
        self.assertEqual(row['interactive'], 0)
        self.assertEqual(row['transport'], 'local_browser')
        self.finish(task, 'needs_verification')
        self.assertIsNone(sch.tick(NOW))
        self.assertEqual(mon.state()['status'], 'attention')
        self.assertFalse(mon.state()['enabled'])

    def test_save_only_and_singleton_partial_updates(self):
        first = mon.save({'lookback_hours': 24, 'target': '无畏契约陪练'})
        second = mon.save({'interval_seconds': '300'})
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['lookback_hours'], 24)
        self.assertEqual(second['target'], '无畏契约陪练')
        self.assertFalse(second['enabled'])
        self.assertEqual(len(sch.state()), 1)
        self.assertFalse(col.ACTIVE)

    def archived_challenge(self, task):
        root=app.DATA_DIR/'private';root.mkdir(exist_ok=True)
        (root/'monitor-policy.json').write_text(json.dumps({'captcha_retry_seconds':300}))
        attempt='12345678-1234-1234-1234-123456789abc';case='a'*64
        for name in ('attempts','cases'):(root/'captcha-learning'/name).mkdir(parents=True,exist_ok=True)
        (root/'captcha-learning/attempts'/(attempt+'.json')).write_text(json.dumps({'task_id':task,'outcome':'needs_review','passed':False,'case_id':case}))
        (root/'captcha-learning/cases'/(case+'.json')).write_text(json.dumps({'latest_attempt_id':attempt,'latest_result':'not_passed'}))
        col.record_verification(task,{'phase':'needs_review','reason':'recognition_declined','attempt_id':attempt,
            'transport':'local_browser','submissions':0,'elapsed_ms':100,'platform_verdict':'unknown','verdict_source':'none'})

    def test_archived_challenge_waits_five_minutes_before_headless_reload(self):
        self.baseline();mon.command('start');task=sch.tick(NOW)
        self.archived_challenge(task);self.finish(task,'needs_verification')
        self.assertIsNone(sch.tick(NOW));self.assertTrue(mon.state()['enabled'])
        self.assertEqual(mon.state()['next_run_at'],'2026-09-09T10:05:00+00:00')
        self.assertIsNone(sch.tick('2026-09-09T10:04:59+00:00'))
        child=sch.tick('2026-09-09T10:05:00+00:00');self.assertIsNotNone(child)
        with app.db() as c:row=c.execute('SELECT interactive,target FROM collection_tasks WHERE id=?',(child,)).fetchone()
        self.assertEqual(row['interactive'],0);self.assertEqual(row['target'],'无畏契约陪玩')
        self.finish(child,'rate_limited');self.assertIsNone(sch.tick('2026-09-09T10:10:00+00:00'))
        self.assertFalse(mon.state()['enabled'])

    def test_stop_during_verification_cooldown_prevents_reload(self):
        self.baseline();mon.command('start');task=sch.tick(NOW)
        self.archived_challenge(task);self.finish(task,'needs_verification');sch.tick(NOW)
        mon.command('stop')
        self.assertIsNone(sch.tick('2026-09-09T10:06:00+00:00'));self.assertFalse(mon.state()['enabled'])

    def test_missing_sample_result_does_not_enable_automatic_reload(self):
        self.baseline();mon.command('start');task=sch.tick(NOW)
        self.archived_challenge(task);self.finish(task,'needs_verification')
        sample=app.DATA_DIR/'private/captcha-learning/cases'/('a'*64+'.json');sample.write_text('{}')
        sch.tick(NOW);self.assertFalse(mon.state()['enabled'])

    def test_freshness_target_is_persistent_independent_and_never_starts(self):
        self.assertEqual(mon.state()['freshness_target_seconds'], 60)
        for seconds in (30, 120, 95, 3600):
            saved = mon.save({'freshness_target_seconds': str(seconds)})
            self.assertEqual(mon.state()['freshness_target_seconds'], seconds)
            self.assertEqual((saved['interval_seconds'],saved['lookback_hours']), (30,1))
            self.assertFalse(saved['enabled'])
        self.assertEqual(col.state()['tasks'], [])
        for bad in (True, None, 9, 3601, '30.0', 12.5):
            with self.assertRaises(ValueError):
                mon.save({'freshness_target_seconds':bad})
            self.assertEqual(mon.state()['freshness_target_seconds'], 3600)

    def test_strict_validation_without_saved_mutations(self):
        for field, bad in [('lookback_hours', 0), ('lookback_hours', 8761), ('lookback_hours', True),
                ('lookback_hours', 1.5), ('lookback_hours', '2.0'), ('lookback_hours', None),
                ('interval_seconds', 29), ('interval_seconds', 86401), ('comment_limit', 101),
                ('video_limit', 6), ('page_concurrency', 5), ('target', '')]:
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                mon.save({field: bad})
        self.assertEqual(sch.state(), [])

    def test_enable_requires_baseline_and_is_idempotent(self):
        with self.assertRaises(ValueError):
            mon.command('start')
        self.assertFalse(mon.state()['enabled'])
        self.baseline()
        mon.command('start')
        task = sch.tick()
        self.assertIsNotNone(task)
        mon.command('start')
        self.assertIsNone(sch.tick())
        self.assertEqual(mon.state()['run_count'], 1)
        self.assertEqual(len(col.ACTIVE), 1)
        with self.assertRaises(ValueError):
            mon.save({'lookback_hours': 1})

    def test_close_cancels_own_worker_and_blocks_dispatch_before_cancel(self):
        self.baseline()
        mon.command('start')
        task = sch.tick()
        original = col.command
        def cancel(task_id, action):
            self.assertFalse(mon.state()['enabled'])
            self.assertIsNone(sch.tick())
            return original(task_id, action)
        with patch('collector.command', side_effect=cancel):
            result = mon.command('stop')
        self.assertTrue(result['stopping'])
        self.assertTrue(col.ACTIVE[task]['cancel'])
        self.assertIsNone(sch.tick())
        with self.assertRaises(ValueError):
            mon.save({})
        with self.assertRaises(ValueError):
            mon.command('start')
        self.finish(task, 'cancelled')
        sch.tick()
        self.assertEqual(mon.state()['status'], 'paused')
        mon.command('start')  # No needless new manual batch after deliberate stop.
        self.assertTrue(mon.state()['enabled'])

    def test_close_does_not_cancel_independent_manual_task(self):
        self.baseline()
        mon.command('start')
        manual = self.task()
        mon.command('stop')
        self.assertFalse(col.ACTIVE[manual]['cancel'])
        self.assertIsNone(sch.tick())

    def test_stop_ignores_queued_observations_and_preserves_history(self):
        self.baseline()
        mon.command('start')
        task = sch.tick()
        self.video(task)
        mon.command('stop')
        self.comment(task, '7600000000000000003')
        self.assertEqual(app.state()['stats']['comments'], 1)
        self.assertEqual(app.state()['messages'], [])

    def test_rolling_window_is_fixed_within_batch_and_advances_next_batch(self):
        self.baseline()
        mon.command('start')
        first = sch.tick()
        row = col.state()['tasks'][0]
        self.assertEqual(row['comment_since'], '2026-09-09T09:00:00+00:00')
        self.video(first)
        self.now.return_value = '2026-09-09T10:02:00+00:00'
        self.comment(first, '7600000000000000003', '2026-09-09T10:01:00+00:00')
        self.assertEqual(col.state()['tasks'][0]['comments'], 1, 'New comments during a batch remain eligible')
        self.finish(first)
        self.now.return_value = '2026-09-09T10:12:00+00:00'
        second = sch.tick()
        self.assertNotEqual(first, second)
        self.assertEqual(col.state()['tasks'][0]['comment_since'], '2026-09-09T09:12:00+00:00')

    def test_window_edges_unknown_future_and_dedupe(self):
        task = self.task(lookback_hours=168)
        self.video(task)
        for i, value in enumerate(['2026-09-02T18:00:00+08:00', '2026-09-02T09:59:59Z',
                None, 'not-a-date', '2026-09-09T10:00:01Z', NOW]):
            self.comment(task, str(7600000000000000010+i), value)
        self.comment(task, '7600000000000000011', '2026-09-02T09:59:59Z')
        row = col.state()['tasks'][0]
        self.assertEqual((row['comments'], row['filtered_old'], row['filtered_unknown'], row['filtered_future']), (2, 1, 2, 1))
        self.assertEqual(app.state()['stats']['comments'], 2)
        with app.db() as c:
            reasons = [r[0] for r in c.execute("SELECT filter_reason FROM collection_observations WHERE kind='comment'")]
        self.assertEqual(reasons.count('filtered_old'), 1)
        self.assertEqual(reasons.count(''), 2)

    def test_filtered_history_not_reanalyzed_or_deleted(self):
        historic = self.task()
        self.video(historic)
        self.comment(historic, published='2026-01-01T00:00:00Z')
        self.finish(historic)
        # Explicitly construct an old, unanalyzed backlog after initial ingest.
        with app.db() as c:
            c.execute("UPDATE comments SET analysis_method='pending'")
        recent = self.task(lookback_hours=168)
        self.video(recent)
        self.comment(recent, published='2026-01-01T00:00:00Z')
        with patch('collector.subprocess.Popen', side_effect=RuntimeError('synthetic')):
            col.run(recent, col.ACTIVE[recent])
        self.assertEqual(app.state()['stats']['comments'], 1)
        self.assertEqual(app.state()['stats']['pending'], 1)
        self.assertEqual(col.state()['tasks'][0]['analysis']['total'], 0)

    def test_continuous_monitor_exceeds_finite_plan_budget(self):
        self.baseline()
        mon.command('start')
        for i in range(26):
            self.now.return_value = (datetime.fromisoformat(NOW)+timedelta(minutes=10*i)).isoformat(timespec='seconds')
            task = sch.tick()
            self.assertIsNotNone(task)
            self.finish(task)
        self.assertEqual(mon.state()['run_count'], 26)
        self.assertTrue(mon.state()['enabled'])
        mon.command('stop')
        self.assertIsNone(sch.tick())

    def test_challenge_pauses_and_cannot_be_reenabled_until_validated(self):
        self.baseline()
        mon.command('start')
        task = sch.tick()
        self.finish(task, 'needs_verification')
        self.assertIsNone(sch.tick())
        self.assertEqual(mon.state()['status'], 'attention')
        with self.assertRaises(ValueError):
            mon.command('start')

    def test_recovery_does_not_auto_resume(self):
        self.baseline()
        mon.command('start')
        task = sch.tick()
        col.ACTIVE.clear()
        col.recover()
        sch.recover()
        self.assertFalse(mon.state()['enabled'])
        self.assertIsNone(sch.tick())
        self.assertEqual(col.state()['tasks'][0]['status'], 'interrupted')
        self.assertEqual(mon.state()['last_task_id'], task)

    def test_plan_routes_cannot_bypass_monitor_stop(self):
        mid = mon.save({})['id']
        with self.assertRaises(ValueError):
            sch.command(mid, 'pause')
        with self.assertRaises(ValueError):
            sch.save({'id': mid, 'target': '测试', 'kind': 'search'})
        self.assertFalse(mon.state()['enabled'])

    def test_demo_isolation_and_repeatable_migration(self):
        app.init('demo')
        with self.assertRaises(ValueError):
            mon.save({}, 'demo')
        with self.assertRaises(ValueError):
            mon.command('start', 'demo')
        mon.save({'lookback_hours': 72})
        app.init()
        app.init()
        self.assertEqual(mon.state()['lookback_hours'], 72)
        self.assertEqual(len(sch.state()), 1)
        self.assertFalse(mon.state('demo')['enabled'])

    def test_literal_keyword_normalization_and_limits(self):
        self.assertEqual(filters.normalize('陪练， ＶＡＬＯＲＡＮＴ ; valorant\r\n多少钱,, '), '陪练\nVALORANT\n多少钱')
        self.assertIsNone(filters.rejection('瓦找陪练', '陪练\n多少钱', '接单'))
        self.assertEqual(filters.rejection('找陪练，自己也接单', '陪练', '接单'), 'filtered_blocked')
        self.assertIsNone(filters.rejection('Ｖａｌｏｒａｎｔ', 'valorant', ''))
        self.assertEqual(filters.rejection('找陪练', '.*', ''), 'filtered_keyword', 'No regex execution')
        self.assertIsNone(filters.rejection('any text', '', ''))
        for bad in [None, [], True, 'a'*41, ','.join('k'+str(i) for i in range(51)), 'a\x00b', 'x'*4001]:
            with self.subTest(bad=str(bad)[:60]), self.assertRaises(ValueError):
                filters.normalize(bad)

    def test_settings_roundtrip_and_short_interval_dispatch(self):
        self.baseline()
        value = mon.save({'include_keywords': '陪练,多少钱', 'exclude_keywords': '接单；免费',
            'interval_seconds': 60, 'page_concurrency': 3, 'video_limit': 3})
        self.assertEqual(value['include_keywords'], '陪练\n多少钱')
        mon.save({'lookback_hours': 24})
        self.assertEqual(mon.state()['exclude_keywords'], '接单\n免费')
        mon.command('start')
        task = sch.tick()
        row = col.state()['tasks'][0]
        self.assertEqual(row['page_concurrency'], 3)
        self.assertEqual(row['include_keywords'], '陪练\n多少钱')
        self.assertEqual(row['exclude_keywords'], '接单\n免费')
        self.finish(task)
        self.assertIsNone(sch.tick('2026-09-09T10:00:59+00:00'))
        self.assertIsNotNone(sch.tick('2026-09-09T10:01:00+00:00'))

    def test_filters_apply_before_ingest_with_exclusion_priority_and_evidence(self):
        task = self.task(lookback_hours=168, include_keywords='陪练,多少钱', exclude_keywords='接单')
        self.video(task)
        self.comment(task, '7600000000000000002', text='国服找陪练')
        self.comment(task, '7600000000000000003', text='一小时多少钱')
        self.comment(task, '7600000000000000004', text='陪练接单')
        self.comment(task, '7600000000000000005', text='哈哈哈')
        self.comment(task, '7600000000000000006', text='接单')
        self.comment(task, '7600000000000000007', published='2026-01-01T00:00:00Z', text='陪练接单')
        self.comment(task, '7600000000000000004', text='陪练接单')
        row = col.state()['tasks'][0]
        self.assertEqual((row['comments'], row['filtered_keyword'], row['filtered_blocked'], row['filtered_old']), (2, 1, 2, 1))
        self.assertEqual(app.state()['stats']['comments'], 2)
        with app.db() as c:
            reasons = [r[0] for r in c.execute("SELECT filter_reason FROM collection_observations WHERE task_id=? AND kind='comment'", (task,))]
        self.assertEqual(len(reasons), 6)
        self.assertEqual(reasons.count('filtered_blocked'), 2)
        self.assertEqual(app.state()['messages'], [])

    def test_filter_snapshot_resume_idempotency_and_no_retroactive_deletion(self):
        task = self.task(include_keywords='陪练', exclude_keywords='接单')
        self.video(task)
        self.comment(task)
        col.checkpoint(task, {'type': 'targets', 'records': [{'video_id': VIDEO, 'video_title': '合成'}]})
        col.checkpoint(task, {'type': 'checkpoint', 'video_id': VIDEO, 'status': 'reading'})
        self.finish(task, 'interrupted')
        child = col.resume(task, 'filter-resume')['id']
        row = col.state()['tasks'][0]
        self.assertEqual((row['include_keywords'],row['exclude_keywords']), ('陪练','接单'))
        self.assertEqual(col.resume(task, 'filter-resume')['id'], child)
        self.finish(child)
        self.assertEqual(app.state()['stats']['comments'], 1)
        mon.save({'exclude_keywords': '陪练'})
        self.assertEqual(app.state()['stats']['comments'], 1)
        self.assertEqual(filters.rejection('哈哈哈', '陪练', ''), 'filtered_keyword')
        with app.db() as c:
            first = dict(c.execute('SELECT * FROM collection_tasks WHERE id=?', (task,)).fetchone())
        with self.assertRaises(ValueError):
            col.start({**first}, include_keywords='别的词', exclude_keywords='接单')

    def test_all_filtered_is_still_observed_not_automatic_retry(self):
        task = self.task(exclude_keywords='陪练')
        self.video(task)
        self.comment(task)
        self.finish(task)
        self.assertEqual(app.state()['stats']['comments'], 0)
        mon.command('start')
        self.assertTrue(mon.state()['enabled'])

    def test_result_snapshots_include_rejections_without_leads(self):
        self.assertEqual(mon.results()['counts']['observed'], 0)
        task = self.task(include_keywords='陪练', exclude_keywords='接单')
        self.video(task)
        self.comment(task, text='陪练接单 <script>')
        self.comment(task, cid='7600000000000000003', text='找陪练')
        rows = {r['external_id']:r for r in mon.results()['rows']}
        rejected = rows['7600000000000000002']
        self.assertEqual(rejected['text'], '陪练接单 <script>')
        self.assertEqual(rejected['text_origin'], 'observation')
        self.assertEqual(rejected['exclude_matches'], ['接单'])
        self.assertEqual(rejected['include_matches'], ['陪练'])
        self.assertEqual(rejected['filter_reason'], 'filtered_blocked')
        self.assertIsNone(rejected['comment_id'])
        self.assertEqual(rejected['user_identifier'], '123456789012')
        self.assertEqual(mon.results()['counts'], {'observed':2,'accepted':1,'filtered':1})
        self.assertEqual(app.state()['stats']['comments'], 1)
        app.init('demo')
        self.assertEqual(mon.results('demo')['rows'], [])
        with app.db() as c:
            c.execute("UPDATE comments SET raw_text='后来修订'")
        self.assertEqual(mon.results()['rows'][0]['text'], '找陪练')

    def test_legacy_results_never_invent_original_snapshots(self):
        task = self.task(exclude_keywords='接单')
        self.video(task)
        self.comment(task, text='接单')
        self.comment(task, cid='7600000000000000003', text='找陪练')
        with app.db() as c:
            c.execute("UPDATE collection_observations SET comment_text='',nickname='',user_identifier='',published_at=NULL")
        result = mon.results()
        rows = {r['external_id']:r for r in result['rows']}
        self.assertNotIn('7600000000000000002', rows)
        self.assertEqual(result['counts'], {'observed': 1, 'accepted': 1, 'filtered': 0})
        self.assertEqual(rows['7600000000000000003']['text_origin'], 'archive')
        self.assertEqual(rows['7600000000000000003']['text'], '找陪练')
        self.assertEqual(rows['7600000000000000003']['include_matches'], [])

    def test_filter_config_rejects_invalid_and_allows_explicit_clear(self):
        mon.save({'include_keywords': '陪练', 'exclude_keywords': '接单'})
        for field in ['include_keywords','exclude_keywords']:
            for bad in [None, ['接单'], 'k'*41]:
                with self.assertRaises(ValueError):
                    mon.save({field:bad})
        self.assertEqual(mon.state()['include_keywords'], '陪练')
        mon.save({'include_keywords':'', 'exclude_keywords':''})
        self.assertEqual(mon.state()['include_keywords'], '')
        self.assertEqual(mon.state()['exclude_keywords'], '')

    def analysis_fixture(self, status='completed', engine='fixture-model', parent=''):
        import analysis_store as store
        self.baseline()
        with app.db() as c:
            c.execute("UPDATE comments SET category='buyer',analysis_method='rules',reason='合成规则基线'")
            row = c.execute('SELECT id FROM comments LIMIT 1').fetchone()
            record_id = row['id']
            source, _ = store.inputs(c, 'comment', record_id)
            if parent:
                source['parent'] = parent
            fingerprint = store.digest(source)
            c.execute("""INSERT INTO intent_results(evidence_type,record_id,method,engine,
                request_id,input_hash,input_json,status,result_json,started_at,finished_at)
                VALUES('comment',?,'model',?,'fixture-model-result',?,?,?,?,?,?)""",
                (record_id, engine, fingerprint, json.dumps(source), status,
                 json.dumps(dict(category='noise', analysis_method='model', reason='合成分类',
                     facts={'evidence':[
                         dict(kind='category',source='comment',text='找无畏契约陪练'),
                         dict(kind='game',source='video',text=source['title']),
                         dict(kind='category',source='parent',text='上级评论中的需求'),
                         dict(kind='category',source='comment',text='不在原文中的错误引用')]}, game='无畏契约')),
                 NOW, NOW))
        return record_id, fingerprint

    def test_result_model_matches_detail_without_rewriting_rule_or_source(self):
        import monitor_comments
        record_id, _ = self.analysis_fixture()
        with app.db() as c:
            jobs_before = c.execute('SELECT COUNT(*) FROM semantic_jobs').fetchone()[0]
        with patch('semantic.state', return_value={'engine':'fixture-model','mode':'remote_api_configured'}):
            row = mon.results()['rows'][0]
            detail = next(x for x in app.state()['comments'] if x['id'] == record_id)
            history = monitor_comments.history({'filter':'accepted'})['rows'][0]
        self.assertEqual((row['category'], row['analysis_method']), (detail['category'], detail['analysis_method']))
        self.assertEqual((row['category'], row['analysis_state']), ('noise', 'model'))
        self.assertEqual(row['analysis_reason'], detail['reason'])
        self.assertEqual(row['analysis_evidence'], [dict(kind='category',source='comment',text='找无畏契约陪练')])
        for key in ('analysis_reason', 'analysis_evidence', 'analysis_method'):
            self.assertEqual(history[key], row[key])
        with app.db() as c:
            original = c.execute('SELECT category,analysis_method FROM comments WHERE id=?', (record_id,)).fetchone()
            self.assertEqual(tuple(original), ('buyer', 'rules'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM semantic_jobs').fetchone()[0], jobs_before)

    def test_result_human_and_pending_take_priority_over_model(self):
        record_id, _ = self.analysis_fixture()
        for method in ('human', 'pending'):
            with app.db() as c:
                c.execute('UPDATE comments SET analysis_method=?,category=? WHERE id=?', (method, 'social', record_id))
            with patch('semantic.state', return_value={'engine':'fixture-model'}):
                row = mon.results()['rows'][0]
            self.assertEqual((row['category'], row['analysis_state']), ('social', method))
            self.assertEqual(row['analysis_reason'], '合成规则基线' if method == 'human' else None)
            self.assertEqual(row['analysis_evidence'], [])

    def test_result_disabled_or_other_engine_does_not_adopt_model(self):
        self.analysis_fixture()
        for engine in (None, 'different-model'):
            with patch('semantic.state', return_value={'engine':engine}):
                row = mon.results()['rows'][0]
            self.assertEqual((row['category'], row['analysis_state']), ('buyer', 'rules'))
            self.assertEqual(row['analysis_reason'], '合成规则基线')
            self.assertEqual(row['analysis_evidence'], [])

    def test_result_changed_snapshot_does_not_attach_new_analysis(self):
        self.analysis_fixture()
        with app.db() as c:
            c.execute("UPDATE collection_observations SET comment_text='历史原文快照'")
        with patch('semantic.state', return_value={'engine':'fixture-model'}):
            row = mon.results()['rows'][0]
        self.assertEqual(row['text'], '历史原文快照')
        self.assertIsNone(row['category'])
        self.assertEqual(row['analysis_state'], 'snapshot_changed')
        self.assertIsNone(row['analysis_reason'])
        self.assertEqual(row['analysis_evidence'], [])

    def test_result_changed_context_does_not_adopt_old_model(self):
        self.analysis_fixture(parent='已经变化的上级原文')
        with patch('semantic.state', return_value={'engine':'fixture-model'}):
            row = mon.results()['rows'][0]
        self.assertEqual((row['category'], row['analysis_state']), ('buyer', 'rules'))
        self.assertEqual(row['analysis_reason'], '合成规则基线')
        self.assertEqual(row['analysis_evidence'], [])

    def test_result_queue_and_failure_states_keep_rule_category(self):
        record_id, fingerprint = self.analysis_fixture(status='failed')
        with patch('semantic.state', return_value={'engine':'fixture-model'}):
            row = mon.results()['rows'][0]
        self.assertEqual((row['category'], row['analysis_state']), ('buyer', 'failed'))
        with app.db() as c:
            c.execute("DELETE FROM intent_results WHERE method='model'")
            c.execute("""INSERT INTO semantic_jobs(evidence_type,record_id,input_hash,engine,
                config_json,status,created_at) VALUES('comment',?,?,'fixture-model','{}','queued',?)""",
                (record_id, fingerprint, NOW))
        for status in ('queued','running','cancelling','cancelled','interrupted','stale','failed'):
            with app.db() as c:
                c.execute('UPDATE semantic_jobs SET status=?', (status,))
            with patch('semantic.state', return_value={'engine':'fixture-model'}):
                row = mon.results()['rows'][0]
            self.assertEqual((row['category'],row['analysis_state']), ('buyer',status))
            self.assertEqual(row['analysis_reason'], '合成规则基线')
            self.assertEqual(row['analysis_evidence'], [])


if __name__ == '__main__':
    unittest.main()
