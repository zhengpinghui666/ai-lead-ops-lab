import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import clubops as app
import collector as col


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='collector-test-')
        self.old_dir = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()
        self.thread_patch = patch('collector.threading.Thread.start')
        self.thread_patch.start()
        col.ACTIVE.clear()

    def tearDown(self):
        self.thread_patch.stop()
        col.ACTIVE.clear()
        app.DATA_DIR = self.old_dir
        self.temp.cleanup()

    def start(self, **changes):
        return col.start({'kind': 'search', 'target': '无畏契约陪玩', 'request_id': 'fixture-request', 'video_limit': 2, 'comment_limit': 2, 'interactive': False, **changes})['id']

    def source(self):
        return col.state()['source_id']

    def video(self, task_id, vid='7600000000000000001'):
        col.observe(task_id, self.source(), {'type': 'video', 'record': {'video_id': vid, 'video_title': '测试夹具：无畏契约陪玩'}})

    def comment(self, task_id, cid='7600000000000000002', **changes):
        row = {'video_id': '7600000000000000001', 'comment_id': cid, 'user_id': '123456789012', 'nickname': '测试夹具用户', 'text': '国服找个陪练，预算100', 'video_title': '测试夹具：无畏契约陪玩', **changes}
        col.observe(task_id, self.source(), {'type': 'comment', 'record': row})

    def test_options_and_url_allowlist(self):
        self.assertEqual(col.canonical_video('https://www.douyin.com/video/7600000000000000001?secret=drop'), 'https://www.douyin.com/video/7600000000000000001')
        for url in ['http://localhost/video/12345', 'https://evil.example/video/12345', 'https://www.douyin.com.evil.example/video/12345', 'https://u:p@www.douyin.com/video/12345', 'https://www.douyin.com:8443/video/12345', 'https://v.douyin.com/test', 'file:///12345']:
            with self.assertRaises(ValueError): col.canonical_video(url)
        for changes in [{'target':''}, {'video_limit':999}, {'comment_limit':0}, {'interactive':'true'}, {'request_id':''}]:
            with self.assertRaises(ValueError): self.start(**changes)
        app.init('demo')
        with self.assertRaises(ValueError): col.start({},'demo')

    def test_http_video_pool_is_canonical_bounded_and_idempotent(self):
        one, two = '7600000000000000001', '7600000000000000003'
        target = one + '\r\nhttps://douyin.com/video/' + two + '?share=removed\n' + one
        task = self.start(kind='video', target=target, transport='http', page_concurrency=4)
        with app.db() as c:
            row = c.execute('SELECT target,video_limit,page_concurrency FROM collection_tasks WHERE id=?', (task,)).fetchone()
            self.assertEqual(tuple(row), ('https://www.douyin.com/video/'+one+'\nhttps://www.douyin.com/video/'+two, 2, 2))
        self.assertEqual(self.start(kind='video', target=target, transport='http', page_concurrency=4), task)
        with self.assertRaises(ValueError):
            self.start(kind='video', target=two+'\n'+one, transport='http', page_concurrency=4)
        for invalid in ('', '\n'.join(str(7600000000000000000+i) for i in range(6)), one+'\nhttps://example.com/video/'+two, 'x'*2001):
            with self.assertRaises(ValueError): col.video_targets(invalid)
        with self.assertRaisesRegex(ValueError, '后端 HTTP'):
            col.options({'kind':'video','target':one+'\n'+two,'request_id':'browser-pool','transport':'local_browser'})

    def test_start_idempotency_single_session_and_no_fabricated_data(self):
        task_id = self.start()
        self.assertEqual(self.start(),task_id)
        with self.assertRaises(ValueError): self.start(target='其他关键词')
        with self.assertRaises(ValueError): self.start(request_id='another')
        s=app.state()
        self.assertEqual(s['stats']['comments'],0)
        self.assertEqual(s['stats']['videos'],0)
        self.assertIsNone(col.state()['last_received'])

    def test_page_concurrency_validation_progress_and_recovery(self):
        for value in [0, 5, 1.5, True, '2.0', None]:
            with self.assertRaises(ValueError): col.page_options({'page_concurrency': value})
        task = self.start(page_concurrency=2)
        self.assertEqual(self.start(page_concurrency='2'), task)
        with self.assertRaises(ValueError): self.start(page_concurrency=1)
        col.parallel_progress(task, {'active_pages':2,'peak_pages':2,'page_concurrency':2})
        self.assertEqual(col.state()['tasks'][0]['peak_pages'], 2)
        for change in [{'active_pages':3}, {'peak_pages':1}, {'page_concurrency':4}, {'active_pages':True}]:
            with self.assertRaises(ValueError): col.parallel_progress(task, {'active_pages':1,'peak_pages':2,'page_concurrency':2,**change})
        col.ACTIVE.clear(); col.recover()
        row = col.state()['tasks'][0]
        self.assertEqual(row['active_pages'], 0); self.assertEqual(row['peak_pages'], 2)
        with self.assertRaises(ValueError): col.parallel_progress(task, {'active_pages':0,'peak_pages':2,'page_concurrency':2})

    def test_atomic_evidence_dedupe_and_no_contact_permission(self):
        task_id=self.start(); self.video(task_id); self.comment(task_id); self.comment(task_id)
        s=app.state(); t=col.state()['tasks'][0]
        self.assertEqual(s['stats']['comments'],1)
        self.assertEqual(t['comments'],1)
        self.assertEqual(t['inserted'],1)
        self.assertEqual(s['leads'][0]['contact_basis'],'')
        self.assertIsNone(s['comments'][0]['published_at'])
        with app.db() as c:
            rows=c.execute('SELECT * FROM collection_observations').fetchall()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(len(r['payload_hash'])==64 for r in rows))
        col.ACTIVE.clear();col.update(task_id,status='completed',finished_at=app.now())
        second=self.start(request_id='second');self.video(second);self.comment(second)
        self.assertEqual(app.state()['stats']['comments'],1)
        self.assertEqual(col.state()['tasks'][0]['duplicate'],1)

    def test_scope_caps_and_invalid_record_rollback(self):
        task_id=self.start();
        with self.assertRaises(ValueError): self.comment(task_id)
        self.video(task_id);self.comment(task_id)
        with self.assertRaises(ValueError): self.comment(task_id,cid='7600000000000000003',published_at='not a timestamp')
        self.assertEqual(col.state()['tasks'][0]['comments'],1)
        self.comment(task_id,cid='7600000000000000003')
        with self.assertRaises(ValueError): self.comment(task_id,cid='7600000000000000004')
        self.video(task_id,'7600000000000000009')
        with self.assertRaises(ValueError): self.video(task_id,'7600000000000000008')
        self.assertEqual(app.state()['stats']['comments'],2)

    def test_observed_video_title_enrichment_requeues_rules_not_human(self):
        task = self.start()
        vid = '7600000000000000001'
        col.checkpoint(task, {'type':'targets','records':[{'video_id':vid}]})
        col.observe(task, self.source(), {'type':'video','record':{'video_id':vid}})
        self.comment(task, text='多少钱', video_title=vid)
        self.comment(task, cid='7600000000000000003', text='有人吗', video_title=vid)
        self.thread_patch.stop()
        app.analyze()
        human = next(c for c in app.state()['comments'] if c['external_id']=='7600000000000000003')
        app.mutate('review', {'id':human['id'],'category':'uncertain','reason':'合成人工记录'})
        col.ACTIVE.clear();col.update(task,status='cancelled',finished_at=app.now())
        self.thread_patch.start()
        second = self.start(request_id='title-enrichment')
        col.checkpoint(second, {'type':'targets','records':[{'video_id':vid}]})
        self.video(second, vid)
        self.thread_patch.stop()
        self.assertEqual(app.state()['stats']['pending'],1)
        app.analyze()
        rules = next(c for c in app.state()['comments'] if c['external_id']=='7600000000000000002')
        self.assertEqual(rules['game'],'无畏契约')
        self.assertEqual(rules['category'],'buyer')
        self.assertEqual(next(c for c in app.state()['comments'] if c['id']==human['id'])['analysis_method'],'human')
        self.assertEqual(col.state()['tasks'][0]['checkpoints'][0]['video_title'],'测试夹具：无畏契约陪玩')
        self.assertEqual(app.state()['stats']['comments'],2)

    def test_browser_source_cannot_be_spoofed_by_import(self):
        self.start()
        with self.assertRaises(ValueError):
            app.ingest({'source_id':self.source(),'records':[{'comment_id':'c-test','video_id':'v-test','text':'伪装为浏览器来源'}]})
        self.assertEqual(app.state()['stats']['comments'],0)

    def test_reply_evidence_and_invalid_relation_are_atomic(self):
        task_id = self.start()
        self.video(task_id)
        root_id, reply_id = '7600000000000000002', '7600000000000000003'
        self.comment(task_id, cid=root_id, text='合成主评论：无畏契约陪练', user_id='', nickname='合成主评论作者')
        with self.assertRaises(ValueError):
            self.comment(task_id, cid=reply_id, parent_comment_id=reply_id)
        self.assertEqual(col.state()['tasks'][0]['comments'], 1)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_observations').fetchone()[0], 2)
        self.comment(task_id, cid=reply_id, parent_comment_id=root_id, text='合成回复：多少钱')
        self.comment(task_id, cid=reply_id, parent_comment_id=root_id, text='合成回复：多少钱')
        # The collector thread stub also affects ThreadPoolExecutor; no collector
        # is launched after this point, so allow the real local classifier threads.
        self.thread_patch.stop()
        app.analyze()
        s = app.state()
        reply = next(r for r in s['comments'] if r['external_id'] == reply_id)
        self.assertEqual(reply['parent_context']['status'], 'available')
        self.assertEqual(reply['parent_context']['nickname'], '合成主评论作者')
        self.assertEqual(s['stats']['comments'], 2)
        self.assertEqual(len(s['leads']), 1)
        self.assertEqual(s['leads'][0]['contact_basis'], '')
        self.assertEqual(col.state()['tasks'][0]['comments'], 2)

    def test_restart_recovery_preserves_records_and_never_restarts(self):
        task_id=self.start();self.video(task_id);self.comment(task_id)
        col.ACTIVE.clear();col.recover()
        self.assertEqual(col.state()['tasks'][0]['status'],'interrupted')
        self.assertEqual(app.state()['stats']['comments'],1)
        self.assertFalse(col.state()['active'] if 'active' in col.state() else col.ACTIVE)

    def test_cancel_before_process_launch_and_resume_validation(self):
        task_id=self.start()
        with self.assertRaises(ValueError): col.command(task_id,'resume')
        col.update(task_id,status='needs_login')
        self.assertEqual(col.command(task_id,'resume')['action'],'resume')
        col.command(task_id,'cancel')
        self.assertTrue(col.ACTIVE[task_id]['cancel'])
        self.assertEqual(col.state()['tasks'][0]['status'],'cancelling')

    def test_worker_failure_is_not_success_and_secrets_are_not_logged(self):
        task_id=self.start()
        with patch('collector.subprocess.Popen',side_effect=RuntimeError('token=SECRET')):
            col.run(task_id,col.ACTIVE[task_id])
        t=col.state()['tasks'][0]
        self.assertEqual(t['status'],'failed')
        self.assertNotIn('SECRET',t['detail'])
        self.assertFalse(t['active'])

    def test_worker_http_failure_evidence_survives_diagnostic_storage(self):
        task_id=self.start()
        messages=[{'type':'diagnostic','stage':'finished-error','snapshot':{
            'navigation_http_status':502,'navigation_retry_after_seconds':90,
            'responses':[{'kind':'search','status':503,'retry_after_seconds':180}],
            'headers':'PRIVATE_SENTINEL'}},
            {'type':'status','status':'network_error','detail':'Synthetic upstream failure'}]
        process=Mock(stdin=io.StringIO(),stdout=io.StringIO(''.join(json.dumps(m)+'\n' for m in messages)))
        process.poll.return_value=0;process.wait.return_value=0
        with patch('collector.subprocess.Popen',return_value=process):
            col.run(task_id,col.ACTIVE[task_id])
        with app.db() as c:
            row=c.execute('SELECT snapshot FROM collection_diagnostics WHERE task_id=?',(task_id,)).fetchone()
        self.assertIsNotNone(row)
        evidence=json.loads(row[0])
        self.assertEqual(evidence['navigation_http_status'],502)
        self.assertEqual(evidence['navigation_retry_after_seconds'],90)
        self.assertEqual(evidence['responses'][0]['retry_after_seconds'],180)
        self.assertNotIn('PRIVATE_SENTINEL',row[0])
        self.assertEqual(col.state()['tasks'][0]['status'],'network_error')
        self.assertFalse(col.state()['tasks'][0]['active'])

    def test_automatic_analysis_does_not_spend_batch_on_unrelated_backlog(self):
        app.ingest({'records': [dict(comment_id=f'backlog-{i}', video_id='import-video',
            text='合成历史积压', user_id=f'import-user-{i}') for i in range(1000)]})
        task_id=self.start();self.video(task_id);self.comment(task_id)
        control=col.ACTIVE[task_id]
        self.thread_patch.stop()
        # No browser or external request: fail after one previously observed row.
        with patch('collector.subprocess.Popen',side_effect=RuntimeError('synthetic failure')):
            col.run(task_id,control)
        state=app.state()
        observed=next(r for r in state['comments'] if r['external_id']=='7600000000000000002')
        self.assertEqual(observed['analysis_method'],'rules')
        self.assertEqual(observed['category'],'buyer')
        self.assertEqual(state['stats']['pending'],1000)
        self.assertTrue(all(r['analysis_method']=='pending' for r in state['comments'] if r['external_id'].startswith('backlog-')))
        task=col.state()['tasks'][0]
        self.assertEqual(task['status'],'failed')
        self.assertEqual(task['analysis']['status'],'completed')
        self.assertEqual(task['analysis']['analyzed'],1)
        self.assertEqual(state['messages'],[])

    def test_checkpoint_resume_skips_finished_video(self):
        task_id=self.start()
        rows=[{'video_id':'7600000000000000001','video_title':'夹具一'}, {'video_id':'7600000000000000009','video_title':'夹具二'}]
        col.checkpoint(task_id,{'type':'targets','records':rows})
        col.checkpoint(task_id,{'type':'checkpoint','video_id':rows[0]['video_id'],'status':'done'})
        col.checkpoint(task_id,{'type':'checkpoint','video_id':rows[1]['video_id'],'status':'reading'})
        with self.assertRaises(ValueError): col.resume(task_id,'too-early')
        col.ACTIVE.clear();col.recover()
        child=col.resume(task_id,'resume-fixture')['id']
        t=col.state()['tasks'][0]
        self.assertEqual(t['parent_task_id'],task_id)
        self.assertEqual(t['video_limit'],1)
        self.assertEqual(t['checkpoints'][0]['video_id'],rows[1]['video_id'])
        self.assertEqual(t['checkpoints'][0]['status'],'pending')
        self.assertEqual(col.resume(task_id,'resume-fixture')['id'],child)
        self.assertEqual(app.state()['stats']['comments'],0)
        self.assertIsNone(col.state()['last_received'],'Pending targets are not observed data')

    def test_checkpoint_scope_validation_and_finished_states(self):
        task_id=self.start(kind='video',target='7600000000000000001')
        with self.assertRaises(ValueError): col.checkpoint(task_id,{'type':'targets','records':[{'video_id':'7600000000000000009'}]})
        with self.assertRaises(ValueError): col.checkpoint(task_id,{'type':'targets','records':[{'video_id':'https://www.douyin.com/video/7600000000000000001'}]})
        col.checkpoint(task_id,{'type':'targets','records':[{'video_id':'7600000000000000001'}]})
        col.checkpoint(task_id,{'type':'checkpoint','video_id':'7600000000000000001','status':'done'})
        with self.assertRaises(ValueError): col.checkpoint(task_id,{'type':'checkpoint','video_id':'7600000000000000001','status':'reading'})
        col.ACTIVE.clear();col.recover()
        self.assertFalse(col.state()['tasks'][0]['resumable'])
        with self.assertRaises(ValueError): col.resume(task_id,'no-work')

    def test_unavailable_checkpoint_is_preserved_and_not_retried_as_pending(self):
        task_id=self.start()
        rows=[{'video_id':'7600000000000000001'},{'video_id':'7600000000000000009'}]
        col.checkpoint(task_id,{'type':'targets','records':rows})
        col.checkpoint(task_id,{'type':'checkpoint','video_id':rows[0]['video_id'],'status':'unavailable','detail':'平台明确提示作品不存在'})
        with self.assertRaises(ValueError):
            col.checkpoint(task_id,{'type':'checkpoint','video_id':rows[0]['video_id'],'status':'reading'})
        col.ACTIVE.clear();col.recover()
        child=col.resume(task_id,'resume-without-unavailable')['id']
        row=next(r for r in col.state()['tasks'] if r['id']==child)
        self.assertEqual([r['video_id'] for r in row['checkpoints']],[rows[1]['video_id']])

    def test_all_verified_visibility_reasons_retire_only_the_affected_work(self):
        from video_discovery import WORK_RESTRICTION_DETAILS
        ids=['7600000000000000001','7600000000000000009']
        for reason in [*WORK_RESTRICTION_DETAILS,'unknown']:
            with self.subTest(reason=reason):
                task=self.start(request_id='visibility-'+reason)
                col.checkpoint(task,{'type':'targets','records':[{'video_id':v} for v in ids]})
                with app.db() as c:
                    for vid in ids:
                        c.execute('''INSERT OR REPLACE INTO discovery_works(video_id,title,relevant,source_kind,source_target,
                            first_seen_at,last_seen_at,next_check_at) VALUES(?,'合成瓦作品',1,'search','瓦',?,?,?)''',
                            (vid,app.now(),app.now(),app.now()))
                col.checkpoint(task,{'type':'checkpoint','video_id':ids[0],'status':'unavailable','reason':reason,'detail':'合成可见性状态'})
                with app.db() as c:
                    rows=[tuple(r) for r in c.execute('SELECT video_id,enabled,next_check_at FROM discovery_works ORDER BY video_id')]
                    self.assertEqual(rows[0][1],0 if reason in WORK_RESTRICTION_DETAILS else 1)
                    if reason in WORK_RESTRICTION_DETAILS:self.assertIsNone(rows[0][2])
                    self.assertEqual(rows[1][1],1)
                    self.assertEqual(c.execute('SELECT status FROM collection_checkpoints WHERE task_id=? AND video_id=?',(task,ids[0])).fetchone()[0],'unavailable')
                col.ACTIVE.clear();col.recover()


if __name__=='__main__': unittest.main()
