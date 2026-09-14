import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import clubops as app
import collector as col
import collection_accounts as accounts
import collection_recovery as recovery
import collection_scheduler as scheduler
import monitoring


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='manual-recovery-test-')
        self.addCleanup(self.tmp.cleanup)
        self.override = patch.object(app, 'DATA_DIR', Path(self.tmp.name)); self.override.start()
        self.addCleanup(self.override.stop)
        app.init()
        self.threads = patch('collector.threading.Thread.start'); self.threads.start()
        self.addCleanup(self.threads.stop)
        col.ACTIVE.clear(); self.addCleanup(col.ACTIVE.clear)
        with app.db() as c:
            c.execute("INSERT INTO collection_accounts VALUES('7446','111','fixture','isolated',1,?,?)",
                      (json.dumps(['comments','discovery']), app.now()))
            c.execute("INSERT INTO collection_accounts VALUES('9517','222','fixture','primary',1,?,?)",
                      (json.dumps(['comments','discovery']), app.now()))

    def original(self, status='needs_verification', transport='local_browser'):
        task = col.start(dict(target='测试夹具：瓦陪', request_id='original', interactive=False,
                              transport=transport, video_limit=3, comment_limit=25, page_concurrency=2),
                         lookback_hours=24, include_keywords='陪玩', exclude_keywords='免费',
                         recovery_since='2026-09-13T10:00:00+00:00')['id']
        col.update(task, status=status, finished_at=app.now()); col.ACTIVE.pop(task)
        return task

    def plan(self, task):
        pid = monitoring.save({'target':'测试夹具：瓦陪','lookback_hours':24})['id']
        with app.db() as c:
            c.execute("UPDATE collection_plans SET status='attention',activated_at=?,last_task_id=?,settled_task_id=?,run_count=1,settled_count=1 WHERE id=?",
                      (app.now(), task, task, pid))
        return pid

    def start(self, parent, request_id='manual-fixture'):
        return recovery.request(dict(id=parent, request_id=request_id))['id']

    def finish(self, task, status='completed'):
        col.update(task, status=status, finished_at=app.now()); col.ACTIVE.pop(task, None)
        with app.db() as c: recovery.settle(c, task)

    def test_preserves_frozen_account_scope_and_idempotency(self):
        parent = self.original(); child = self.start(parent)
        self.assertEqual(self.start(parent), child)
        with app.db() as c:
            before = c.execute('SELECT * FROM collection_tasks WHERE id=?',(parent,)).fetchone()
            after = c.execute('SELECT * FROM collection_tasks WHERE id=?',(child,)).fetchone()
            for key in ('kind','target','transport','video_limit','comment_limit','page_concurrency','lookback_hours','comment_since','include_keywords','exclude_keywords'):
                self.assertEqual(before[key], after[key], key)
            self.assertEqual(after['interactive'], 1)
            self.assertEqual(accounts.binding(c,child)['account_id'], '7446')
            self.assertEqual(recovery.record(c,child)['parent_task_id'], parent)
        # A generic new task would rotate to 9517; recovery must not.
        self.assertTrue(col.ACTIVE[child]['manual_verification'])
        with self.assertRaises(ValueError):
            col.start(dict(target='测试夹具：瓦陪', request_id='manual-fixture'))

    def test_pending_video_scope_is_copied_without_completed_targets(self):
        parent = self.original()
        with app.db() as c:
            for vid, status in [('76000000000001','done'),('76000000000002','partial')]:
                c.execute('INSERT INTO collection_checkpoints(task_id,video_id,video_title,video_url,status,updated_at) VALUES(?,?,?,?,?,?)',
                          (parent,vid,'fixture',f'https://www.douyin.com/video/{vid}',status,app.now()))
        child=self.start(parent)
        with app.db() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT video_id FROM collection_checkpoints WHERE task_id=?',(child,))],['76000000000002'])
            self.assertEqual(tuple(c.execute('SELECT video_limit,page_concurrency FROM collection_tasks WHERE id=?',(child,)).fetchone()),(1,1))
        self.assertEqual(self.start(parent),child)

    def test_no_monitor_resume_on_open_wait_or_failed_read(self):
        parent=self.original();pid=self.plan(parent);child=self.start(parent)
        for status in ('queued','needs_verification','running'):
            col.update(child,status=status)
            with app.db() as c:
                recovery.settle(c,child)
                self.assertEqual(c.execute('SELECT status FROM collection_plans WHERE id=?',(pid,)).fetchone()[0],'attention')
                self.assertIsNone(recovery.record(c,child)['settled_at'])
        self.finish(child,'partial')
        with app.db() as c:
            self.assertEqual(recovery.record(c,child)['state'],'needs_user')
            self.assertEqual(c.execute('SELECT last_task_id FROM collection_plans WHERE id=?',(pid,)).fetchone()[0],parent)

    def test_success_reconnects_same_batch_then_real_scheduler_sets_next_due(self):
        parent=self.original();pid=self.plan(parent);child=self.start(parent)
        self.finish(child)
        with app.db() as c:
            self.assertEqual(recovery.record(c,child)['state'],'recovered')
            self.assertEqual(tuple(c.execute('SELECT status,last_task_id,run_count,settled_count FROM collection_plans WHERE id=?',(pid,)).fetchone()),('running',child,1,0))
            recovery.settle(c,child)
        scheduler.tick()
        with app.db() as c:
            p=c.execute('SELECT * FROM collection_plans WHERE id=?',(pid,)).fetchone()
            self.assertEqual(p['settled_count'],1);self.assertEqual(p['settled_task_id'],child)
            self.assertIsNotNone(p['next_run_at'])
            self.assertEqual(c.execute('SELECT status FROM collection_tasks WHERE id=?',(parent,)).fetchone()[0],'needs_verification')

    def test_stop_controls_actual_recovery_child_and_prevents_restart(self):
        parent=self.original();pid=self.plan(parent);child=self.start(parent)
        self.assertEqual(monitoring.state()['active_task_id'],child)
        monitoring.command('stop')
        self.assertTrue(col.ACTIVE[child]['cancel'])
        self.finish(child)  # Even a late completed worker must not undo stop.
        with app.db() as c:
            self.assertEqual(recovery.record(c,child)['state'],'plan_changed')
            self.assertEqual(c.execute('SELECT status FROM collection_plans WHERE id=?',(pid,)).fetchone()[0],'paused')

    def test_changed_plan_or_server_restart_invalidates_resume(self):
        parent=self.original();pid=self.plan(parent);child=self.start(parent)
        scheduler.recover();self.finish(child)
        with app.db() as c:
            self.assertEqual(recovery.record(c,child)['state'],'plan_changed')
            self.assertNotEqual(c.execute('SELECT status FROM collection_plans WHERE id=?',(pid,)).fetchone()[0],'running')

    def test_unrelated_manual_batch_never_enables_a_plan(self):
        parent=self.original();child=self.start(parent);self.finish(child)
        with app.db() as c:
            self.assertEqual(recovery.record(c,child)['state'],'completed')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM collection_plans WHERE status='running'").fetchone()[0],0)

    def test_identity_mismatch_does_not_resume_plan(self):
        parent=self.original();pid=self.plan(parent);child=self.start(parent)
        with app.db() as c:c.execute("UPDATE collection_task_accounts SET account_id='9517',sender_uid='222',storage='primary' WHERE task_id=?",(child,))
        self.finish(child)
        with app.db() as c:self.assertEqual(recovery.record(c,child)['state'],'needs_user')

    def test_refuses_other_gates_unbound_tasks_and_request_parameter_injection(self):
        parent=self.original()
        for status in ('rate_limited','needs_login','access_denied','identity_failed','failed'):
            col.update(parent,status=status)
            with self.assertRaises(ValueError):self.start(parent)
        col.update(parent,status='needs_verification')
        for extra in ({'target':'changed'},{'account_id':'9517'},{'interactive':False}):
            with self.assertRaises(ValueError):recovery.request(dict(id=parent,request_id='fixture',**extra))
        with self.assertRaises(ValueError):recovery.request(dict(id=parent,request_id='fixture'),'demo')
        with app.db() as c:c.execute('DELETE FROM collection_task_accounts WHERE task_id=?',(parent,))
        with self.assertRaises(ValueError):self.start(parent)

    def test_worker_uses_original_profile_and_manual_mode_without_solver(self):
        parent=self.original();child=self.start(parent)
        class Input(io.StringIO):
            def close(self): pass
        incoming=Input()
        process=Mock(stdin=incoming,stdout=io.StringIO(json.dumps(dict(type='status',status='completed',detail='synthetic valid read'))+'\n'))
        process.poll.return_value=0;process.wait.return_value=0
        with patch.object(col,'dependencies',return_value=('fixture-node',Path(self.tmp.name))),patch.object(col.subprocess,'Popen',return_value=process),patch.object(col,'analyze_observed'),patch('login_recovery.after_collection'),patch('collection_scheduler.notify_finished'):
            col.run(child,col.ACTIVE[child])
        config=json.loads(incoming.getvalue().splitlines()[0])
        self.assertEqual(config['captcha'],{'mode':'manual'})
        self.assertEqual(config['collection_account']['account_id'],'7446')
        self.assertEqual(Path(config['profile_dir']),Path(self.tmp.name)/'collection-accounts/7446/browser-profile')

    def archive(self, task):
        root=app.DATA_DIR/'private';root.mkdir(exist_ok=True)
        (root/'monitor-policy.json').write_text(json.dumps({'captcha_retry_seconds':300}))
        attempt=str(uuid.uuid4());case=f'{task:064x}'
        for part in ('attempts','cases'):(root/'captcha-learning'/part).mkdir(parents=True,exist_ok=True)
        (root/'captcha-learning/attempts'/(attempt+'.json')).write_text(json.dumps(dict(task_id=task,outcome='needs_review',passed=False,case_id=case)))
        (root/'captcha-learning/cases'/(case+'.json')).write_text(json.dumps(dict(latest_attempt_id=attempt,latest_result='not_passed')))
        # Synthetic diagnostics are inserted only into this temporary fixture DB.
        with app.db() as c:
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                      (task,'captcha_workflow',json.dumps({'verification':dict(phase='needs_review',reason='recognition_declined',attempt_id=attempt)}),app.now()))

    def auto(self, parent, request_id):
        return col.start(dict(target='this unrelated discovery target must not be used',request_id=request_id),verification_from=parent)['id']

    def test_two_extra_batches_only_with_original_account_target_and_absolute_cutoff(self):
        root=self.original();self.archive(root)
        first=self.auto(root,'retry-one');self.finish(first,'needs_verification');self.archive(first)
        second=self.auto(first,'retry-two');self.finish(second,'needs_verification');self.archive(second)
        with app.db() as c:
            self.assertEqual(recovery.retry_state(c,second),dict(root_task_id=root,retry_number=2,max_retries=2,remaining=0))
            self.assertEqual(scheduler.verification_retry_seconds(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(second,)).fetchone()),0)
            original=c.execute('SELECT * FROM collection_tasks WHERE id=?',(root,)).fetchone()
            for tid in (first,second):
                actual=c.execute('SELECT * FROM collection_tasks WHERE id=?',(tid,)).fetchone()
                for field in ('target','transport','comment_since','lookback_hours','include_keywords','exclude_keywords','comment_limit','video_limit'):
                    self.assertEqual(actual[field],original[field],field)
                self.assertEqual(actual['interactive'],0)
                self.assertEqual(accounts.binding(c,tid)['account_id'],'7446')
        scheduler.recover()  # The persisted allowance does not reset on restart.
        with self.assertRaises(ValueError):self.auto(second,'forbidden-third')
        with app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_verification_retries').fetchone()[0],2)

    def test_automatic_retry_does_not_branch_or_reclassify_other_platform_gates(self):
        root=self.original();self.archive(root)
        first=self.auto(root,'retry-one');self.finish(first,'rate_limited')
        with self.assertRaises(ValueError):self.auto(first,'not-a-captcha')
        with self.assertRaises(ValueError):self.auto(root,'must-not-create-another-branch')
        with app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_verification_retries').fetchone()[0],1)

    def test_scheduler_stops_after_second_extra_batch(self):
        from datetime import datetime,timedelta
        parent=self.original();self.archive(parent);pid=self.plan(parent)
        with app.db() as c:
            c.execute("UPDATE collection_plans SET status='running',settled_task_id=NULL,settled_count=0 WHERE id=?",(pid,))
        instant=app.now()
        for number in (1,2):
            self.assertIsNone(scheduler.tick(instant))
            with app.db() as c: due=c.execute('SELECT next_run_at FROM collection_plans WHERE id=?',(pid,)).fetchone()[0]
            self.assertIsNotNone(due)
            self.assertIsNone(scheduler.tick((datetime.fromisoformat(due)-timedelta(seconds=1)).isoformat()))
            with patch('clubops.now',return_value=due):
                child=scheduler.tick(due);self.assertIsNotNone(child)
                self.finish(child,'needs_verification');self.archive(child)
            with app.db() as c:self.assertEqual(recovery.retry_state(c,child)['retry_number'],number)
            instant=due
        self.assertIsNone(scheduler.tick((datetime.fromisoformat(instant)+timedelta(minutes=10)).isoformat()))
        with app.db() as c:
            self.assertEqual(tuple(c.execute('SELECT status,next_run_at FROM collection_plans WHERE id=?',(pid,)).fetchone()),('attention',None))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_verification_retries').fetchone()[0],2)


if __name__=='__main__': unittest.main()
