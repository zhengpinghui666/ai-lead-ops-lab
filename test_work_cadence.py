"""Signal identity, bounded weights and time semantics; all data is synthetic."""
import sqlite3
import unittest
from datetime import datetime,timedelta
import work_cadence as cadence

NOW='2026-09-14T12:00:00+00:00'
VID='7600000000000000001'
def at(seconds):return (datetime.fromisoformat(NOW)+timedelta(seconds=seconds)).isoformat()


class WorkCadenceTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.c.executescript(cadence.SCHEMA)
    def tearDown(self):self.c.close()
    def add(self,cid='comment-1',text='合成普通评论',published=NOW,observed=NOW,vid=VID):
        cadence.record(self.c,vid,cid,text,published,observed)
    def stats(self,now=NOW):return cadence.statistics(self.c,now).get(VID,{})
    def policy(self,stats,vertical=False,quiet=0):
        return cadence.assess(vertical=vertical,quiet=quiet,base_interval=60,stats=stats)

    def test_repeated_pages_and_same_id_on_another_work(self):
        self.add(text='想点女陪',published=at(-120),observed=at(-60))
        self.add(text='想点女陪',published=at(-120))
        self.add(vid=str(int(VID)+1),text='找老板点我',published=at(-120))
        s=self.stats();self.assertEqual((s['samples'],s['hits'],s['recent_hour']),(1,1,1))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM work_comment_signals').fetchone()[0],2)
        self.assertEqual(self.c.execute('SELECT first_observed_at FROM work_comment_signals WHERE video_id=?',(VID,)).fetchone()[0],at(-60))

    def test_missing_fields_do_not_erase_known_publication(self):
        self.add(published=at(-3500),observed=at(-3000))
        self.add(published=None)
        self.assertEqual(self.stats()['recent_hour'],1)
        self.assertEqual(self.stats(at(101))['recent_hour'],0)
        self.add(published=at(300),observed=at(101))
        self.assertEqual(self.stats(at(101))['recent_hour'],0)

    def test_unknown_future_blank_and_clock_boundaries(self):
        for cid,pub,obs,text in [('a',None,NOW,'无时间'),('b',at(1),NOW,'未来'),
            ('c',at(-1),at(-2),'发布时间晚于观察'),('d',NOW,at(1),'未来观察'),('e',NOW,NOW,' '),
            ('f','2026-09-14T19:00:00+08:00',NOW,'边界')]:self.add(cid,text,pub,obs)
        self.assertEqual(self.stats()['recent_hour'],1)
        self.assertEqual(self.stats(at(2))['recent_hour'],1)  # only d enters; f ages out
        self.assertEqual(self.stats()['total_samples'],4)

    def test_service_counts_include_supply_but_never_claim_buyer(self):
        texts=['想点个女陪','老板怎么下单','打手找老板','本店俱乐部招募','现在有人打吗','钻超有人打吗']
        for i,text in enumerate(texts):self.add(str(i),text)
        s=self.stats();self.assertEqual((s['samples'],s['hits']),(6,4))
        p=self.policy(s);self.assertFalse(p['sufficient_samples']);self.assertFalse(p['complete_comment_coverage'])
        self.assertNotIn('category',p)

    def test_seven_day_sample_excludes_old_and_unknown_times(self):
        for i in range(30):self.add(str(i),'想点女陪' if i<3 else '普通评论',at(-600))
        for i in range(100):self.add('old'+str(i),'普通评论',at(-604801))
        self.add('unknown','普通评论',None)
        p=self.policy(self.stats());self.assertEqual((p['sample_count'],p['service_hits'],p['ratio']),(30,3,.1))
        self.assertEqual(p['sample_basis'],'published_last_7_days')

    def test_history_is_labelled_and_sparse_evidence_cannot_be_monthly(self):
        p=self.policy(dict(samples=0,total_samples=99,total_hits=0),quiet=5)
        self.assertEqual(p['sample_basis'],'observed_history');self.assertNotEqual(p['tier'],'monthly')
        p=self.policy(dict(samples=0,total_samples=2,total_hits=0),quiet=5)
        self.assertEqual(p['tier'],'explore');self.assertLessEqual(p['interval_seconds'],86400)

    def test_ratio_levels_and_service_title_floor(self):
        for hits,tier,interval in [(30,'high',60),(10,'relevant',180),(3,'potential',900),(2,'low',21600),(1,'monthly',2592000)]:
            p=self.policy(dict(samples=100,hits=hits))
            self.assertEqual((p['tier'],p['interval_seconds']),(tier,interval))
        p=self.policy(dict(samples=100,hits=0),vertical=True)
        self.assertEqual((p['tier'],p['interval_seconds']),('title_watch',3600))

    def test_activity_boost_depends_on_publication_and_decays(self):
        for i in range(5):self.add(str(i),'想点女陪',at(-60))
        p=self.policy(self.stats(),vertical=True)
        self.assertEqual((p['boost'],p['interval_seconds']),('burst',30))
        for i in range(5):self.add(str(i),'想点女陪',at(-60),at(4000))
        p=self.policy(self.stats(at(4000)),vertical=True,quiet=5)
        self.assertEqual(p['boost'],'none');self.assertGreater(p['interval_seconds'],30)

    def test_sparse_recent_comment_yields_after_two_quiet_checks(self):
        stats=dict(total_samples=1,total_hits=0,samples=1,hits=0,recent_hour=1)
        first=self.policy(stats,vertical=True,quiet=0)
        second=self.policy(stats,vertical=True,quiet=1)
        quiet=self.policy(stats,vertical=True,quiet=2)
        self.assertEqual((first['interval_seconds'],second['interval_seconds']),(60,60))
        self.assertEqual(quiet['interval_seconds'],240)
        self.assertTrue(quiet['quiet_priority_expired'])
        self.assertFalse(second['quiet_priority_expired'])
        self.assertLessEqual(self.policy(stats,vertical=True,quiet=5)['interval_seconds'],3600)
        # A new unique recent comment resets quiet, restoring the first probe.
        self.assertEqual(self.policy(dict(stats,recent_hour=2),vertical=True,quiet=0)['interval_seconds'],60)

    def test_sustained_activity_and_strong_relevance_keep_their_cadence(self):
        busy=self.policy(dict(samples=3,hits=0,total_samples=3,recent_hour=3),vertical=True,quiet=5)
        self.assertEqual(busy['boost'],'active');self.assertEqual(busy['interval_seconds'],60)
        self.assertFalse(busy['quiet_priority_expired'])
        strong=self.policy(dict(samples=100,hits=30,recent_hour=1),vertical=True,quiet=5)
        self.assertEqual(strong['interval_seconds'],60,'Strong service evidence keeps its weight interval even after leaving the active pool')
        self.assertTrue(strong['quiet_priority_expired'])

    def test_fast_irrelevant_chatter_cannot_get_a_burst_boost(self):
        p=self.policy(dict(samples=100,hits=0,recent_five_minutes=50,recent_hour=100,previous_hour=2))
        self.assertEqual(p['boost'],'none');self.assertEqual(p['tier'],'monthly')

    def test_migration_is_idempotent_and_does_not_rewrite_the_archive(self):
        self.c.execute('CREATE TABLE collection_observations(task_id,kind,external_id,page_url,comment_text,published_at,observed_at)')
        for task,text,observed in [(1,'普通评论',at(-120)),(2,'想点女陪',NOW)]:
            self.c.execute('INSERT INTO collection_observations VALUES(?,?,?,?,?,?,?)',(task,'comment','x','https://www.douyin.com/video/'+VID,text,at(-150),observed))
        before=[tuple(r) for r in self.c.execute('SELECT * FROM collection_observations')]
        cadence.initialize(self.c);cadence.initialize(self.c)
        self.assertEqual(before,[tuple(r) for r in self.c.execute('SELECT * FROM collection_observations')])
        self.assertEqual((self.stats()['total_samples'],self.stats()['total_hits']),(1,1))
        self.assertEqual(self.c.execute('SELECT first_observed_at FROM work_comment_signals').fetchone()[0],at(-120))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM work_cadence_migrations').fetchone()[0],1)


if __name__=='__main__':unittest.main()
