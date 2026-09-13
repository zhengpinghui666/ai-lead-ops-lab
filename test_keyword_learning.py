import json
import unittest
from unittest.mock import patch
import clubops as app
import semantic
import asset_keywords as words
import keyword_learning as learning
from test_asset_verticality import RoutingTests


def approval(source,settings=None):
    return dict(decision='approved',reason='不同来源均用该词表达国服端游陪玩，适合作品发现。',
        evidence=[dict(source_id=r['source_id'],quote=source['term']) for r in source['sources'][:2]])


class LearningTests(unittest.TestCase):
    def setUp(self):
        RoutingTests.setUp(self)
        learning.STOP.clear()
        self.addCleanup(learning.STOP.clear)
        self.enabled=patch.object(learning,'enabled',return_value=True);self.enabled.start();self.addCleanup(self.enabled.stop)

    def seed(self,term='寻陪启事',scope='asset'):
        with app.db() as c:
            for i in range(2):words.observe(c,'work:'+str(i),'无畏契约陪玩 #'+term)
            c.execute('UPDATE asset_keywords SET scope=? WHERE term=?',(scope,term))
        return term

    def test_approved_search_persists_evidence_and_does_not_change_model_gate(self):
        self.seed()
        self.assertTrue(learning.tick(predictor=approval))
        with app.db() as c:
            self.assertIn('寻陪启事',words.active(c)['search'])
            self.assertNotIn('寻陪启事',words.active(c)['service'])
            self.assertEqual(words.active(c,'message')['service'],[])
            self.assertEqual(c.execute('SELECT keyword FROM discovery_queries').fetchone()[0],'无畏契约 寻陪启事')
            self.assertEqual(c.execute('SELECT status FROM keyword_model_reviews').fetchone()[0],'approved')
        self.assertFalse(learning.tick(predictor=lambda *_:self.fail('repeated model call')))
        self.assertEqual(words.state()['rows'][0]['model_review_status'],'approved')
        self.assertEqual(words.detail('寻陪启事')['model_reviews'][0]['status'],'approved')

    def test_single_source_repetition_is_not_corroboration(self):
        with app.db() as c:
            for _ in range(5):words.observe(c,'work:1','无畏契约 #寻陪启事')
        self.assertFalse(learning.tick(predictor=lambda *_:self.fail('not enough sources')))

    def test_mobile_foreign_and_contact_words_are_not_auto_evaluated(self):
        for title,term in [('无畏契约手游 #寻陪启事','寻陪启事'),('无畏契约港服 #找陪排','找陪排'),('无畏契约 #加我微信','加我微信')]:
            with app.db() as c:
                for i in range(2):words.observe(c,'work:'+str(i),title)
                row=c.execute('SELECT * FROM asset_keywords WHERE term=?',(term,)).fetchone()
                self.assertIsNone(learning.inputs(c,row))

    def test_uncertain_remains_visible_without_activation_or_repeated_spending(self):
        self.seed('快乐游戏')
        def uncertain(source,settings):return dict(decision='uncertain',reason='词义过于宽泛，暂不启用。',evidence=[])
        self.assertTrue(learning.tick(predictor=uncertain))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT status FROM asset_keywords').fetchone()[0],'pending')
            c.execute("UPDATE keyword_model_reviews SET created_at='2026-01-01T00:00:00+00:00'")
        self.assertFalse(learning.tick(predictor=lambda *_:self.fail('identical evidence reviewed twice')))

    def test_manual_rejection_wins_even_when_more_sources_arrive(self):
        self.seed()
        row=words.detail('寻陪启事')
        words.review(dict(term=row['term'],revision=row['revision'],scope='asset',kind='search',status='rejected',reason='人工确认暂不使用此词。'))
        self.assertFalse(learning.tick(predictor=lambda *_:self.fail('manual choice overwritten')))

    def test_inflight_manual_review_or_stop_prevents_activation(self):
        for change in ('review','stop','source'):
            with self.subTest(change=change):
                term=self.seed('寻陪'+dict(review='甲',stop='乙',source='丙')[change])
                with app.db() as c:
                    c.execute("UPDATE keyword_model_reviews SET created_at='2026-01-01T00:00:00+00:00'")
                    c.execute('UPDATE asset_keywords SET status=\'rejected\' WHERE term!=?',(term,))
                learning.STOP.clear()
                def predictor(source,settings):
                    if change=='stop':learning.STOP.set()
                    else:
                        with app.db() as c:
                            if change=='review':c.execute('UPDATE asset_keywords SET revision=revision+1 WHERE term=?',(term,))
                            else:c.execute('UPDATE asset_keyword_sources SET title=\'手瓦手游\' WHERE term=?',(term,))
                    return approval(source)
                self.assertTrue(learning.tick(predictor=predictor))
                with app.db() as c:
                    self.assertEqual(c.execute('SELECT status FROM keyword_model_reviews WHERE term=?',(term,)).fetchone()[0],'stale')
                    self.assertEqual(c.execute('SELECT status FROM asset_keywords WHERE term=?',(term,)).fetchone()[0],'pending')

    def test_fabricated_or_duplicate_evidence_never_activates(self):
        self.seed()
        def fabricated(source,settings):
            value=approval(source);value['evidence'][1]['source_id']=value['evidence'][0]['source_id'];return value
        self.assertTrue(learning.tick(predictor=fabricated))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT status FROM asset_keywords').fetchone()[0],'pending')
            self.assertEqual(c.execute('SELECT status FROM keyword_model_reviews').fetchone()[0],'failed')

    def test_waits_for_real_demand_queue_and_respects_daily_budget(self):
        self.seed()
        with app.db() as c:
            c.execute("INSERT INTO semantic_jobs(evidence_type,record_id,input_hash,engine,config_json,status,created_at) VALUES('comment',1,'x','x','{}','queued',?)",(app.now(),))
        self.assertFalse(learning.tick(predictor=lambda *_:self.fail('jumped demand queue')))
        with app.db() as c:c.execute('DELETE FROM semantic_jobs')
        with patch.object(learning,'DAILY_BUDGET',0):self.assertFalse(learning.tick(predictor=lambda *_:self.fail('over budget')))


if __name__=='__main__':unittest.main()
