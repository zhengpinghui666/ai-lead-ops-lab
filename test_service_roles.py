"""Role direction, author ownership and cross-channel projection regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clubops as app
import author_roles
import service_roles
import semantic
import analysis_store
from intent_rules import companion_relevance
from test_semantic import prediction


class RoleTests(unittest.TestCase):
    def test_pure_teamup_is_not_a_customer_even_after_legacy_model_false_positive(self):
        from intent_rules import classify_comment
        for text in ('现在有打的吗？','现在有打么','有人打不','来个搭子',
                     '黄金白银有人打么','有人能带我打排位吗','下三有人玩嘛，不压力',
                     '超1有点菜，有人一起打吗？','xol524匹配有人玩吗','黄金白银，有人打吗'):
            with self.subTest(text=text):
                row=dict(category='buyer',analysis_method='model')
                service_roles.project(row,dict(text=text,title='瓦搭子群',parent=''))
                self.assertEqual(row['category'],'social')
                self.assertEqual(classify_comment(text,'无畏契约陪玩','',app.GAMES,app.TARGET_GAME)['category'],'social')
                self.assertFalse(companion_relevance(text,'瓦搭子群')['passed'])
        for text in ('太菜了，想找个厉害的人带我打瓦','现在有女陪可以点吗','来个搭子，预算100',
                     '黄金白银有人打么，付费','有人能带我打排位吗，想点女陪','超1想找技术陪',
                     'xol524匹配有人玩吗，一局10元','有人打吗，想了解一下陪玩价格'):
            self.assertFalse(service_roles.ordinary_teamup(text))

    def test_personal_supply_variants_share_gate_and_are_never_buyers(self):
        for text in ('求老板点我','蹲老板','女陪找单','打手求职','谁来点我','等单','老板滴滴','找个老板','有老板吗'):
            with self.subTest(text=text):
                self.assertTrue(companion_relevance(text,'无畏契约')['passed'])
                result=service_roles.classify(text,'无畏契约')
                self.assertIsNotNone(result)
                self.assertEqual(result['category'],'seller')
        for text in ('招打手','收女陪','招陪玩','招聘技术陪'):
            with self.subTest(text=text):
                self.assertTrue(companion_relevance(text,'无畏契约')['passed'])
                self.assertEqual(service_roles.classify(text,'无畏契约')['category'],'recruit')

    def test_buyer_and_reported_examples_do_not_become_providers(self):
        for text in ('想点个女陪','找技术陪带我打','老板怎么下单','你接单吗','不是我等单，是他说等单','不接单了','如果有老板点我就好了'):
            with self.subTest(text=text):self.assertIsNone(service_roles.classify(text,'无畏契约陪玩'))

    def test_club_identity_uses_own_evidence_and_keeps_individual_employees(self):
        for args in ({'nickname':'样例电竞俱乐部'},{'signature':'我们是样例陪玩俱乐部，主营国服陪玩'},{'text':'本店招女陪，老板也可以下单'}):
            self.assertIsNotNone(author_roles.evidence(**args))
        for args in ({'nickname':'电竞爱好者'},{'nickname':'样例俱乐部受害者'},{'signature':'我在样例俱乐部当陪玩'},{'text':'听说本店招女陪'},{'text':'这个俱乐部怎么下单'}):
            self.assertIsNone(author_roles.evidence(**args))

    def test_model_club_requires_own_identity_evidence(self):
        source=dict(kind='comment',text='等单',title='无畏契约陪玩俱乐部',parent='')
        result=semantic.validate_result(prediction(source,'club'),source)
        self.assertEqual(result['category'],'uncertain','The video owner is not the commenter')
        source['author']=dict(uid='123456',nickname='样例电竞俱乐部',signature='')
        result=semantic.validate_result(prediction(source,'club'),source)
        self.assertEqual(result['category'],'club')

    def test_confirmed_club_uid_applies_across_channels_without_sending(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'DATA_DIR',Path(directory)):
            app.init()
            with app.db() as c:
                author_roles.remember(c,'123456',nickname='样例电竞俱乐部')
                source=dict(kind='comment',text='等单',title='无畏契约',parent='',author=dict(uid='123456',nickname='样例电竞俱乐部',signature=''))
                candidate=dict(uid='123456',category='buyer',analysis_method='model')
                author_roles.project(c,candidate,source)
                self.assertEqual(candidate['category'],'uncertain','Profile clue alone awaits model identity review')
                author_roles.confirm(c,source,{'category':'club'})
                for kind,text in (('comment','求老板'),('group','找打手'),('live','怎么下单')):
                    row=dict(uid='123456',category='buyer',analysis_method='model')
                    author_roles.project(c,row,dict(kind=kind,text=text,title='无畏契约',parent=''))
                    self.assertEqual(row['category'],'club')
                other=dict(uid='123457',category='buyer',analysis_method='model')
                author_roles.project(c,other,dict(kind='group',text='怎么下单',title='样例电竞俱乐部',parent=''))
                self.assertEqual(other['category'],'buyer')
                self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0],0)

    def test_model_and_keyword_review_both_describe_shared_gate(self):
        import keyword_learning
        self.assertIn('俱乐部',semantic.SYSTEM if hasattr(semantic,'SYSTEM') else semantic.PROMPT)
        self.assertIn('三类共用初筛',keyword_learning.PROMPT)


if __name__=='__main__':unittest.main()
