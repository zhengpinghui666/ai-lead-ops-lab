import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import clubops as app
import collector as col
import collection_scheduler as sch

NOW='2026-09-09T10:00:00+00:00'


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='scheduler-test-')
        self.old=app.DATA_DIR;app.DATA_DIR=Path(self.temp.name);app.init()
        self.threads=patch('collector.threading.Thread.start');self.threads.start()
        self.clock=patch('clubops.now',return_value=NOW);self.clock.start()
        col.ACTIVE.clear()

    def tearDown(self):
        col.ACTIVE.clear();self.threads.stop();self.clock.stop();app.DATA_DIR=self.old;self.temp.cleanup()

    def healthy_fixture(self):
        # A clearly synthetic success record confined to this test DB, not a live proof.
        task=col.start({'kind':'search','target':'测试夹具','request_id':'healthy-fixture','interactive':False})['id']
        source=col.state()['source_id']
        col.observe(task,source,{'type':'video','record':{'video_id':'7600000000000000001','video_title':'无畏契约测试夹具'}})
        col.observe(task,source,{'type':'comment','record':{'video_id':'7600000000000000001','comment_id':'7600000000000000002','text':'测试夹具评论'}})
        self.finish(task)

    def finish(self,task,status='completed'):
        col.update(task,status=status,finished_at=NOW,detail='合成测试结果')
        col.ACTIVE.pop(task,None)

    def plan(self,**changes):
        return sch.save({'name':'测试计划','kind':'search','target':'无畏契约陪玩','video_limit':1,'comment_limit':10,'interval_seconds':600,'run_limit':2,**changes})['id']

    def test_plans_default_paused_and_require_actual_comment_baseline(self):
        pid=self.plan()
        self.assertEqual(sch.state()[0]['status'],'paused')
        self.assertIsNone(sch.tick(NOW))
        self.assertEqual(col.state()['tasks'],[])
        with self.assertRaises(ValueError):sch.command(pid,'start')
        self.healthy_fixture();sch.command(pid,'start')
        self.assertEqual(sch.state()[0]['status'],'running')

    def test_priority_serial_execution_and_idempotent_dispatch(self):
        self.healthy_fixture()
        low=self.plan(priority=1,run_limit=1);high=self.plan(priority=3,run_limit=1)
        sch.command(low,'start');sch.command(high,'start')
        first=sch.tick(NOW)
        self.assertEqual(sch.state()[0]['last_task_id'],first)
        self.assertIsNone(sch.tick(NOW))
        self.assertEqual(len(col.ACTIVE),1)
        self.finish(first)
        second=sch.tick(NOW)
        self.assertNotEqual(second,first)
        self.assertEqual(next(p for p in sch.state() if p['id']==high)['status'],'completed')
        self.assertEqual(next(p for p in sch.state() if p['id']==low)['last_task_id'],second)

    def test_plan_carries_page_concurrency_without_multiple_sessions(self):
        self.healthy_fixture()
        pid=self.plan(video_limit=3,page_concurrency=2)
        self.assertEqual(sch.state()[0]['page_concurrency'],2)
        sch.command(pid,'start');task=sch.tick(NOW)
        row=next(t for t in col.state()['tasks'] if t['id']==task)
        self.assertEqual(row['page_concurrency'],2);self.assertEqual(len(col.ACTIVE),1)
        with self.assertRaises(ValueError):self.plan(page_concurrency=5)

    def test_intervals_finite_budget_and_no_extra_runs(self):
        self.healthy_fixture();pid=self.plan();sch.command(pid,'start')
        first=sch.tick(NOW);self.finish(first)
        self.assertIsNone(sch.tick('2026-09-09T10:09:59+00:00'))
        second=sch.tick('2026-09-09T10:10:00+00:00');self.assertIsNotNone(second);self.finish(second)
        self.assertIsNone(sch.tick('2026-09-10T10:10:00+00:00'))
        p=sch.state()[0];self.assertEqual((p['run_count'],p['settled_count'],p['status']),(2,2,'completed'))
        with self.assertRaises(ValueError):sch.command(pid,'start')

    def test_verification_pauses_all_running_plans_without_retry(self):
        self.healthy_fixture();a=self.plan();b=self.plan();sch.command(a,'start');sch.command(b,'start')
        task=sch.tick(NOW);col.update(task,status='needs_verification')
        self.assertIsNone(sch.tick(NOW))
        self.assertTrue(all(p['status']=='attention' for p in sch.state()))
        self.assertEqual(len(col.ACTIVE),1)
        with self.assertRaises(ValueError):sch.command(b,'start')

    def test_mixed_timezones_never_dispatch_before_due(self):
        self.healthy_fixture();pid=self.plan();sch.command(pid,'start')
        first=sch.tick('2026-09-09T18:00:00+08:00');self.assertIsNotNone(first)
        col.update(first,status='completed',finished_at='2026-09-09T18:00:00+08:00')
        col.ACTIVE.pop(first,None)
        self.assertIsNone(sch.tick('2026-09-09T18:09:59+08:00'))
        self.assertEqual(sch.state()[0]['run_count'],1)
        self.assertIsNotNone(sch.tick('2026-09-09T18:10:00+08:00'))

    def test_failures_pause_and_restart_does_not_autorun(self):
        self.healthy_fixture();pid=self.plan();sch.command(pid,'start');task=sch.tick(NOW)
        self.finish(task,'schema_changed');self.assertIsNone(sch.tick(NOW))
        self.assertEqual(sch.state()[0]['status'],'attention')
        # Recovery preserves counts and never starts a replacement browser.
        sch.recover();self.assertIsNone(sch.tick(NOW));self.assertFalse(col.ACTIVE)

    def test_validation_pause_edit_and_demo_isolation(self):
        for changes in [{'interval_seconds':1},{'run_limit':100},{'priority':99}]:
            with self.assertRaises(ValueError):self.plan(**changes)
        app.init('demo')
        with self.assertRaises(ValueError):sch.save({},'demo')
        self.healthy_fixture();pid=self.plan();sch.command(pid,'start')
        with self.assertRaises(ValueError):sch.save({'id':pid,'target':'无畏契约陪玩'})
        sch.command(pid,'pause');self.assertIsNone(sch.tick(NOW))
        sch.save({'id':pid,'target':'无畏契约陪玩','name':'已修改'})
        self.assertEqual(sch.state()[0]['name'],'已修改')


if __name__=='__main__':unittest.main()
