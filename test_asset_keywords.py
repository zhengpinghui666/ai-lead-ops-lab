import unittest
import json
import test_asset_verticality as fixtures
import clubops as app
import asset_keywords as words
import asset_references as refs
import asset_verticality as assets


class KeywordTests(unittest.TestCase):
    setUp=fixtures.RoutingTests.setUp
    comment=fixtures.RoutingTests.comment
    room=fixtures.RoutingTests.room
    message=fixtures.RoutingTests.message

    def approve(self,term,kind='service',status='approved',scope='asset'):
        row=words.propose(dict(term=term))
        return words.review(dict(term=term,revision=row['revision'],status=status,kind=kind,scope=scope,reason='合成评审：该词在测试语境中表示陪玩服务。'))

    def test_new_terms_accumulate_without_activation_and_sources_deduplicate(self):
        with app.db() as c:
            for _ in range(4):words.observe(c,'work:1','无畏契约 #瓦搭')
            words.observe(c,'work:2','无畏契约 #瓦搭 #找搭子')
            self.assertEqual(words.active(c)['service'],[])
        state=words.state();self.assertEqual(state['counts']['pending'],2)
        self.assertEqual(next(r for r in state['rows'] if r['term']=='瓦搭')['source_count'],2)
        self.assertFalse(assets.classify('无畏契约 #瓦搭',[])['matched'])

    def test_approved_service_updates_classification_and_live_gate_only_in_vertical_rooms(self):
        record,_=self.comment('普通聊天','无畏契约 #瓦搭')
        self.approve('瓦搭',scope='both')
        with app.db() as c:self.assertTrue(assets.routing(c,'comment',record)['model_allowed'])
        sid=self.room('无畏契约 #瓦搭');plain=self.message(sid,'你好',1);hit=self.message(sid,'瓦搭多少钱',2)
        with app.db() as c:
            self.assertFalse(assets.routing(c,'live',plain)['model_allowed'])
            route=assets.routing(c,'live',hit);self.assertTrue(route['model_allowed']);self.assertEqual(route['learned_keywords'],['瓦搭'])
            c.execute("UPDATE live_rooms SET title='无畏契约赛事'")
            self.assertFalse(assets.routing(c,'live',hit,refresh_asset=True)['model_allowed'])
        self.approve('瓦搭',status='rejected')
        with app.db() as c:
            self.assertFalse(assets.routing(c,'comment',record)['model_allowed'])
            words.observe(c,'work:3','无畏契约 #瓦搭')
            self.assertEqual(words.active(c)['service'],[])

    def test_search_words_keep_game_scope_and_game_terms_do_not_trigger_barrage_model(self):
        self.approve('寻陪招聘',kind='search');self.approve('端瓦',kind='game')
        with app.db() as c:
            self.assertIn('无畏契约 寻陪招聘',words.queries(c));self.assertNotIn('端瓦',words.queries(c))
        sid=self.room();record=self.message(sid,'端瓦真好玩')
        with app.db() as c:self.assertFalse(assets.routing(c,'live',record)['model_allowed'])

    def test_default_discovery_word_never_expands_message_model_gate(self):
        self.approve('瓦搭')
        sid=self.room('无畏契约 #瓦搭');record=self.message(sid,'瓦搭多少钱')
        with app.db() as c:
            self.assertTrue(assets.routing(c,'live',record)['asset']['matched'])
            self.assertFalse(assets.routing(c,'live',record)['model_allowed'])
            self.assertEqual(words.active(c,'message')['service'],[])

    def test_message_only_word_does_not_expand_discovery_or_work_classification(self):
        self.approve('瓦搭',scope='message')
        record,_=self.comment('普通讨论','无畏契约 #瓦搭')
        with app.db() as c:
            self.assertFalse(assets.routing(c,'comment',record)['model_allowed'])
            self.assertEqual(words.queries(c),[])

    def test_references_feed_vocabulary_and_withdrawal_removes_inherited_words(self):
        self.comment('普通文字','无畏契约 #寻陪启事',video='123456')
        row=refs.propose(dict(asset_key='123456'))['rows'][0]
        body=dict(id=row['id'],revision=row['revision'],status='approved',reason='明确的游戏与服务组合。',game_terms=['无畏契约'],service_terms=['寻陪启事'],focus_author=False)
        row=refs.review(body)['rows'][0]
        with app.db() as c:self.assertIn('寻陪启事',words.active(c)['service'])
        refs.review({**body,'revision':row['revision'],'status':'pending'})
        with app.db() as c:self.assertNotIn('寻陪启事',words.active(c)['service'])

    def test_search_covers_words_beyond_display_limit(self):
        with app.db() as c:
            for i in range(505):words.observe(c,'work:'+str(i),'#样本词'+str(i))
        self.assertEqual(words.state()['counts']['total'],505)
        self.assertEqual([r['term'] for r in words.state(q='样本词504')['rows']],['样本词504'])

if __name__=='__main__':unittest.main()
