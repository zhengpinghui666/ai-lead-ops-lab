"""Isolated cross-batch reads; never contacts Douyin or the model."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import clubops as app
import collector
import monitor_comments as comments

VIDEO='7600000000000000001'
OTHER='7600000000000000002'
NOW='2026-09-11T03:00:00+00:00'


class CommentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old=app.DATA_DIR
        app.DATA_DIR=Path(self.temp.name)
        self.addCleanup(setattr,app,'DATA_DIR',old)
        app.init()
        collector.ACTIVE.clear()
        self.addCleanup(collector.ACTIVE.clear)
        self.clock=patch('clubops.now',return_value=NOW)
        self.clock.start();self.addCleanup(self.clock.stop)
        self.thread=patch('collector.threading.Thread.start')
        self.thread.start();self.addCleanup(self.thread.stop)
        self.engine=patch('semantic.state',return_value={'engine':None})
        self.engine.start();self.addCleanup(self.engine.stop)

    def batch(self,video,records,excluded=''):
        collector.ACTIVE.clear()
        task=collector.start(dict(kind='video',target=video,request_id='history-'+str(len(collector.state()['tasks'])),comment_limit=100),exclude_keywords=excluded)['id']
        source=collector.ACTIVE[task]['source_id']
        collector.observe(task,source,dict(type='video',record=dict(video_id=video,video_title='测试作品')))
        for cid,text in records:
            collector.observe(task,source,dict(type='comment',record=dict(video_id=video,comment_id=cid,text=text,published_at=NOW,user_id='123456789012')))
        collector.update(task,status='completed',finished_at=NOW)
        collector.ACTIVE.clear()
        return task

    def test_cross_batch_work_filter_dedupe_and_counts(self):
        self.batch(VIDEO,[('7600000000000000011','找陪练'),('7600000000000000012','接单')],excluded='接单')
        self.batch(OTHER,[('7600000000000000013','别的作品')])
        last=self.batch(VIDEO,[('7600000000000000011','找陪练')])
        result=comments.history({})
        self.assertEqual(result['counts'],dict(observed=3,accepted=2,filtered=1))
        self.assertEqual(next(r for r in result['rows'] if r['external_id']=='7600000000000000011')['task_id'],last)
        selected=comments.history({'video':'https://www.douyin.com/video/'+VIDEO})
        self.assertEqual(selected['total'],2)
        self.assertTrue(all(r['video_url'].endswith(VIDEO) for r in selected['rows']))
        self.assertEqual(comments.history({'filter':'filtered'})['total'],1)
        self.assertEqual(comments.history({'video':'missing'})['total'],0)
        self.assertFalse(collector.ACTIVE)

    def test_pagination_literal_search_and_empty_page(self):
        self.batch(VIDEO,[(str(7600000000000000100+i),'找陪练 '+('%' if i==0 else str(i))) for i in range(28)])
        first=comments.history({})
        second=comments.history({'page':'2'})
        self.assertEqual((first['total'],len(first['rows']),len(second['rows'])),(28,25,3))
        self.assertFalse({r['external_id'] for r in first['rows']}&{r['external_id'] for r in second['rows']})
        self.assertEqual(comments.history({'q':'%'})['total'],1)
        self.assertEqual(comments.history({'page':'100','q':'absent'})['page'],1)
        app.init('demo')
        self.assertEqual(comments.history({},'demo')['total'],0)

    def test_archive_without_observation_and_validation(self):
        app.ingest({'records':[dict(comment_id='archive',video_id=VIDEO,text='旧存档原文',published_at=NOW)]})
        row=comments.history({})['rows'][0]
        self.assertEqual(row['text_origin'],'archive')
        self.assertEqual(row['text'],'旧存档原文')
        self.assertIsNone(row['task_id'])
        for query in ({'page':'0'},{'page':True},{'q':'x'*201},{'filter':'wrong'},{'video':[]}):
            with self.assertRaises(ValueError):comments.history(query)

    def test_first_collection_descending_without_repeat_promoting_old_comments(self):
        old,blocked,new='7600000000000000071','7600000000000000072','7600000000000000073'
        self.batch(VIDEO,[(old,'旧入库'),(blocked,'接单')],excluded='接单')
        with patch('clubops.now',return_value='2026-09-11T11:01:00+08:00'):
            self.batch(OTHER,[(new,'新入库')])
        with patch('clubops.now',return_value='2026-09-11T03:05:00+00:00'):
            self.batch(VIDEO,[(old,'旧入库'),(blocked,'接单')],excluded='接单')
        # Publication and the most recent reread do not determine placement.
        with app.db() as c:
            c.execute("UPDATE collection_observations SET published_at='2026-09-11T03:04:00+00:00' WHERE external_id=?",(old,))
        result=comments.history({})
        self.assertEqual([r['external_id'] for r in result['rows']],[new,old,blocked])
        self.assertEqual(result['sort'],'collected_at_desc')
        self.assertEqual(next(r for r in result['rows'] if r['external_id']==blocked)['collected_at'],NOW)
        accepted=comments.history({'filter':'accepted','q':'入库'})
        self.assertEqual([r['external_id'] for r in accepted['rows']],[new,old])
        selected=comments.history({'video':'https://www.douyin.com/video/'+VIDEO})
        self.assertEqual([r['collected_at'] for r in selected['rows']],[NOW,NOW])
