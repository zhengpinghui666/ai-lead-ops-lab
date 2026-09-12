import json
import sqlite3
from contextlib import closing
import unittest
from unittest.mock import patch
import clubops as app
import analysis_store as store
import asset_keywords as words
import comment_keywords as comments
import semantic
import asset_verticality as assets
import test_asset_verticality as fixtures
from test_semantic import prediction


class CommentKeywordTests(unittest.TestCase):
    setUp=fixtures.RoutingTests.setUp
    comment=fixtures.RoutingTests.comment
    room=fixtures.RoutingTests.room
    message=fixtures.RoutingTests.message

    def model(self, record, category='buyer'):
        class Adapter:
            def __init__(self,settings):pass
            def predict(self,source):return prediction(source,category),'a'*64
        with app.db() as c:source,_=store.inputs(c,'comment',record)
        result=semantic.analyze_one(dict(evidence_type='comment',id=record,input_hash=store.digest(source),request_id='word-test-'+str(record)+'-'+category),adapter_factory=Adapter)
        self.assertEqual(result['status'],'completed')

    def approve(self,term,scope='message',status='approved'):
        row=words.detail(term)
        words.review(dict(term=term,revision=row['revision'],scope=scope,kind='service',status=status,reason='合成评审：此原文短语属于游戏陪同服务。'))

    def test_rules_accumulate_pending_phrases_without_model_or_duplicate_sources(self):
        record,_=self.comment('想找个靠谱的陪玩')
        detail=words.detail('想找个靠谱的陪玩')
        self.assertEqual((detail['scope'],detail['status']),('message','pending'))
        self.assertEqual(detail['comment_sources'][0]['method'],'rules')
        with app.db() as c:
            for _ in range(3):comments.observe(c,record)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM comment_keyword_sources').fetchone()[0],1)
            self.assertEqual(words.active(c,'message')['service'],[])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM intent_results WHERE method='model'").fetchone()[0],0)

    def test_model_hook_discovers_new_literal_phrase_and_human_rejection_withdraws_source(self):
        record,_=self.comment('想找个声伴')
        self.assertEqual(words.state(scope='message')['counts']['total'],0)
        self.model(record)
        detail=words.detail('想找个声伴');source=detail['comment_sources'][0]
        self.assertEqual(source['method'],'model');self.assertTrue(source['judgment_key'].startswith('model:'))
        self.assertEqual(source['raw_text'],'想找个声伴');self.assertEqual(detail['status'],'pending')
        app.mutate('review',dict(id=record,category='noise',reason='合成人工纠正：并非服务需求。'))
        self.assertFalse(words.detail('想找个声伴')['comment_sources'][0]['current'])
        # A later model completion cannot override the current human rejection.
        self.model(record,'seller')
        self.assertFalse(words.detail('想找个声伴')['comment_sources'][0]['current'])

    def test_human_confirmation_adds_literal_new_phrase_and_revision_invalidates_evidence(self):
        record,_=self.comment('求个瓦搭')
        app.mutate('review',dict(id=record,category='buyer',reason='合成人工确认陪同服务需求。'))
        detail=words.detail('求个瓦搭')
        self.assertEqual(detail['comment_sources'][0]['method'],'human')
        app.ingest({'records':[dict(comment_id='c1',video_id='v1',video_title='无畏契约陪玩',text='看比赛啦')]})
        self.assertFalse(words.detail('求个瓦搭')['comment_sources'][0]['current'])

    def test_only_original_category_evidence_and_no_contacts_titles_or_invented_terms(self):
        result=dict(analysis_method='model',category='buyer',facts={'evidence':[
            dict(kind='category',source='comment',text='求个瓦搭'),dict(kind='category',source='video',text='标题服务词'),
            dict(kind='budget',source='comment',text='预算两百'),dict(kind='category',source='comment',text='不存在的词')]})
        self.assertEqual(comments.phrases('求个瓦搭，预算两百',result),['求个瓦搭'])
        for text in ('加我微信abc陪玩','陪玩123456','https://example.com/陪玩','@某人找陪玩','这是很长的原文表达但并没有必要把它完整沉淀到自动关键词匹配词库陪玩'):
            self.assertEqual(comments.phrases(text,dict(analysis_method='human')),[],text)

    def test_pending_rejected_and_asset_scope_never_change_comment_matching(self):
        words.propose(dict(term='求个瓦搭',scope='message'))
        with app.db() as c:self.assertFalse(words.message_relevance(c,'求个瓦搭')['passed'])
        self.approve('求个瓦搭')
        ordinary,_=self.comment('求个瓦搭','无畏契约赛事')
        with app.db() as c:
            relevance=words.message_relevance(c,'求个瓦搭')
            self.assertTrue(relevance['passed']);self.assertEqual(relevance['learned_keywords'],['求个瓦搭'])
            self.assertFalse(assets.routing(c,'comment',ordinary)['model_allowed'])
            self.assertEqual(words.queries(c),[])
        self.approve('求个瓦搭',scope='asset')
        with app.db() as c:self.assertFalse(words.message_relevance(c,'求个瓦搭')['passed'])
        self.approve('求个瓦搭',status='rejected')
        app.mutate('review',dict(id=ordinary,category='buyer',reason='合成人工确认原文需求。'))
        self.assertEqual(words.detail('求个瓦搭')['status'],'rejected')
        with app.db() as c:self.assertFalse(words.message_relevance(c,'求个瓦搭')['passed'])

    def test_comment_evidence_never_silently_widens_existing_asset_scope(self):
        words.propose(dict(term='求个瓦搭'));self.approve('求个瓦搭',scope='asset')
        record,_=self.comment('求个瓦搭')
        app.mutate('review',dict(id=record,category='buyer',reason='合成人工确认服务需求。'))
        self.assertEqual(words.detail('求个瓦搭')['scope'],'asset')
        with app.db() as c:self.assertEqual(words.active(c,'message')['service'],[])
        result=words.state(scope='message')
        self.assertEqual(result['counts']['total'],1);self.assertFalse(result['rows'][0]['active'])

    def test_scope_and_status_filter_before_limit_and_backfill_is_idempotent(self):
        with app.db() as c:
            for i in range(505):words.observe(c,'work:'+str(i),'#发现样本'+str(i))
        record,_=self.comment('我想找陪练')
        self.assertEqual([r['term'] for r in words.state(scope='message',status='pending')['rows']],['我想找陪练'])
        with app.db() as c:
            before=c.execute('SELECT COUNT(*) FROM comment_keyword_sources').fetchone()[0]
            comments.backfill(c);comments.backfill(c)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM comment_keyword_sources').fetchone()[0],before)
        self.approve('我想找陪练')
        self.assertEqual(words.state(scope='message',status='pending')['matched'],0)
        self.assertEqual(words.state(scope='message',status='active')['matched'],1)

    def test_migration_backs_up_existing_database_before_new_table(self):
        record,_=self.comment('想找陪玩')
        with app.db() as c:
            c.execute('DROP TABLE comment_keyword_sources')
            c.execute("DELETE FROM settings WHERE key='comment_keyword_corpus_v1'")
        app.init()
        backups=list((app.DATA_DIR/'backups').glob('*before-comment-keywords-*.bak'))
        self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='comment_keyword_sources'").fetchone())
            self.assertEqual(c.execute('SELECT raw_text FROM comments WHERE id=?',(record,)).fetchone()[0],'想找陪玩')
        self.assertEqual(words.detail('想找陪玩')['comment_sources'][0]['method'],'rules')


if __name__=='__main__':unittest.main()
