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
        self.engine=patch('semantic.state',return_value={'engine':None,'can_analyze':False})
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

    def test_valuable_filter_uses_current_judgment_before_pagination(self):
        records=[(str(7600000000000000200+i),'找陪练 '+str(i)) for i in range(28)]
        records += [('7600000000000000291','我接单'),('7600000000000000292','普通讨论'),
                    ('7600000000000000293','找陪练 屏蔽样本')]
        self.batch(VIDEO,records,excluded='屏蔽样本')
        with app.db() as c: ids=[r[0] for r in c.execute('SELECT id FROM comments')]
        app.analyze(comment_ids=ids)
        first=comments.history({'filter':'valuable'})
        second=comments.history({'filter':'valuable','page':'2'})
        self.assertEqual((first['total'],first['valuable_count'],len(first['rows']),len(second['rows'])),(28,28,25,3))
        self.assertTrue(all(r['category']=='buyer' and not r['filter_reason'] for r in first['rows']+second['rows']))
        self.assertFalse({r['external_id'] for r in first['rows']}&{r['external_id'] for r in second['rows']})
        changed=first['rows'][0]
        app.mutate('review',dict(id=changed['comment_id'],category='noise',reason='合成人工核对'))
        self.assertEqual(comments.history({'filter':'valuable'})['total'],27)
        self.assertEqual(comments.history({'filter':'valuable','q':'屏蔽样本'})['total'],0)
        with app.db() as c:
            c.execute("UPDATE comments SET raw_text='修改后的原文' WHERE id=?",(first['rows'][1]['comment_id'],))
        self.assertEqual(comments.history({'filter':'valuable'})['total'],26,'Changed source text cannot keep an old buyer projection')
        self.assertFalse(collector.ACTIVE)

    def test_accepted_intent_survives_later_time_window_rejection_without_rewriting_observations(self):
        cid='7600000000000000401'
        first=self.batch(VIDEO,[(cid,'找陪练')])
        with app.db() as c: ident=c.execute('SELECT id FROM comments WHERE external_id=?',(cid,)).fetchone()[0]
        app.analyze(comment_ids=[ident])
        with patch('clubops.now',return_value='2026-09-11T05:00:00+00:00'):
            later=self.batch(VIDEO,[(cid,'找陪练')])
        # Same source text was accepted earlier; a later monitoring window is narrower.
        with app.db() as c:
            c.execute("UPDATE collection_observations SET filter_reason='filtered_old' WHERE task_id=? AND kind='comment'",(later,))
        row=comments.history({'filter':'valuable'})['rows'][0]
        self.assertEqual((row['external_id'],row['task_id'],row['collected_at']),(cid,first,NOW))
        self.assertEqual(comments.history({})['counts'],{'observed':1,'accepted':1,'filtered':0})
        with app.db() as c:
            self.assertEqual(c.execute("SELECT filter_reason FROM collection_observations WHERE task_id=? AND kind='comment'",(later,)).fetchone()[0],'filtered_old')
        app.mutate('review',dict(id=ident,category='noise',reason='人工撤回需求'))
        self.assertEqual(comments.history({'filter':'valuable'})['total'],0)
        self.assertEqual(comments.history({'filter':'accepted'})['total'],1)

    def test_new_filtered_text_or_identity_does_not_inherit_old_intent(self):
        cid='7600000000000000402'
        self.batch(VIDEO,[(cid,'找陪练')])
        with app.db() as c: ident=c.execute('SELECT id FROM comments WHERE external_id=?',(cid,)).fetchone()[0]
        app.analyze(comment_ids=[ident])
        later=self.batch(VIDEO,[(cid,'接单广告')],excluded='接单')
        self.assertEqual(comments.history({'filter':'valuable'})['total'],0)
        self.assertEqual(comments.history({'filter':'accepted'})['total'],0)
        row=comments.history({})['rows'][0]
        self.assertEqual((row['text'],row['task_id']),('接单广告',later))
        with app.db() as c:
            c.execute("UPDATE collection_observations SET comment_text='找陪练',user_identifier='987654321012' WHERE task_id=? AND kind='comment'",(later,))
        self.assertEqual(comments.history({'filter':'valuable'})['total'],0)
