import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clubops as app
import collector as col
import collection_scheduler as scheduler
import monitor_board

VIDEO='760000000000000001'
NOW='2026-09-11T04:00:00+00:00'


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.previous=app.DATA_DIR
        app.DATA_DIR=Path(self.folder.name)
        app.init()
        self.clock=patch('clubops.now',return_value=NOW);self.clock.start()
        self.threads=patch('collector.threading.Thread.start');self.threads.start()
        col.ACTIVE.clear()

    def tearDown(self):
        col.ACTIVE.clear();self.threads.stop();self.clock.stop()
        app.DATA_DIR=self.previous;self.folder.cleanup()

    def snapshot(self):
        return monitor_board.build(col.state()['tasks'],scheduler.state())

    def create_task(self):
        task=col.start(dict(kind='video',target=VIDEO,transport='http',video_limit=1,comment_limit=10,request_id='board-test'))['id']
        col.checkpoint(task,dict(type='targets',records=[dict(video_id=VIDEO,video_title='无畏契约作品',video_url=col.canonical_video(VIDEO))]))
        col.observe(task,col.state()['source_id'],dict(type='video',record=dict(video_id=VIDEO,video_title='无畏契约作品')))
        return task

    def test_enabled_archive_is_not_a_continuously_watched_work(self):
        task=self.create_task()
        col.update(task,status='completed',finished_at=NOW)
        col.ACTIVE.clear()
        result=self.snapshot()
        self.assertEqual(result['summary']['tracked'],0)
        self.assertEqual(result['rows'][0]['state'],'history')
        self.assertIsNone(result['rows'][0]['next_check_at'])

    def test_fixed_plan_pause_changes_tracking_without_hiding_history(self):
        self.create_task();col.ACTIVE.clear()
        plan=scheduler.save(dict(kind='video',target=VIDEO,transport='http'))['id']
        with app.db() as c:c.execute("UPDATE collection_plans SET status='running' WHERE id=?",(plan,))
        result=self.snapshot()
        self.assertEqual(result['summary']['tracked'],1)
        self.assertEqual(result['rows'][0]['state'],'monitoring')
        scheduler.command(plan,'pause')
        result=self.snapshot()
        self.assertEqual(result['summary']['tracked'],0)
        self.assertEqual(result['rows'][0]['state'],'paused')

    def test_search_batch_does_not_promise_fixed_video_monitoring(self):
        task=self.create_task()
        plan=scheduler.save(dict(kind='search',target='无畏契约',transport='http'))['id']
        with app.db() as c:c.execute("UPDATE collection_plans SET status='running' WHERE id=?",(plan,))
        col.checkpoint(task,dict(type='checkpoint',video_id=VIDEO,status='reading'))
        result=self.snapshot()
        self.assertEqual(result['summary']['tracked'],0)
        self.assertEqual(result['summary']['reading'],1)
        self.assertEqual(result['rows'][0]['active_task_id'],task)

    def test_new_comment_counts_use_publication_and_first_collection_times(self):
        task=self.create_task();source=col.state()['source_id']
        for i,stamp in enumerate(('2026-09-11T03:59:00+00:00','2026-09-10T03:00:00+00:00','2026-09-11T05:00:00+00:00')):
            col.observe(task,source,dict(type='comment',record=dict(video_id=VIDEO,comment_id=str(760000000000000010+i),
                text='普通讨论',user_id='123456',published_at=stamp)))
        result=self.snapshot()
        self.assertEqual(result['summary']['fresh_comments'],1)
        self.assertEqual(result['rows'][0]['archived_comments'],3)
        self.assertEqual(result['rows'][0]['observed_in_last_batch'],3)

    def test_configured_unread_work_is_listed_without_fabricated_check_time(self):
        scheduler.save(dict(kind='video',target=VIDEO,transport='http'))
        result=self.snapshot()
        row=result['rows'][0]
        self.assertEqual(row['state'],'paused')
        self.assertIsNone(row['id'])
        self.assertIsNone(row['last_checked_at'])
        self.assertEqual(row['archived_comments'],0)


if __name__=='__main__':unittest.main()
