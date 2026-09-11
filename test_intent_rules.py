"""Synthetic language regression cases; not a measured production accuracy claim."""
import json
import tempfile
import unittest
from pathlib import Path

import clubops as app
from intent_rules import RULESET_VERSION


class IntentRuleTests(unittest.TestCase):
    def test_social_requests_are_not_automatically_buyers(self):
        for raw in ['无畏契约找个搭子开黑', '无畏契约新手找个人带我', 'VALORANT来个队友', '无畏契约求带一下', '无畏契约最多3人组队']:
            with self.subTest(raw=raw):
                self.assertEqual(app.classify(raw)['category'], 'uncertain')
        for raw in ['无畏契约不找收费，只找队友', '瓦陪玩不花钱，找个搭子', '无畏契约免费组队', '无畏契约不付费，找陪练']:
            with self.subTest(raw=raw):
                self.assertEqual(app.classify(raw)['category'], 'social')

    def test_buyer_seller_and_recruit_signals(self):
        cases = [
            ('无畏契约找个靠谱陪玩', 'buyer'),
            ('无畏契约新手找个陪练，练枪怎么收费', 'buyer'),
            ('VALORANT排位组队，预算200，找两个人', 'buyer'),
            ('瓦陪玩复盘多少钱', 'buyer'),
            ('无畏契约付费找个搭子', 'buyer'),
            ('无畏契约接单陪练，来个老板', 'seller'),
            ('无畏契约陪练接单，有老板吗', 'seller'),
            ('无畏契约教练求职', 'seller'),
            ('无畏契约俱乐部招陪练', 'recruit'),
            ('无畏契约自己接单，也想找个陪玩', 'uncertain'),
            ('无畏契约只找队友，预算200', 'uncertain'),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(app.classify(raw)['category'], expected)

    def test_negation_and_questions_do_not_invert_roles(self):
        for raw, expected in [
            ('无畏契约不是找陪玩，只是问问', 'uncertain'),
            ('无畏契约不接单，找个陪练', 'buyer'),
            ('无畏契约不招人，找陪练', 'buyer'),
            ('无畏契约陪练，你接单吗', 'buyer'),
            ('无畏契约不需要陪玩', 'uncertain'),
            ('无畏契约需要的不是陪玩', 'uncertain'),
            ('原来陪玩还能玩无畏契约', 'uncertain'),
            ('未来无畏契约陪玩行业如何', 'uncertain'),
            ('他说无畏契约找陪玩，不是我', 'uncertain'),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(app.classify(raw)['category'], expected)
        result = app.classify('无畏契约不是找陪玩，只是问问')
        self.assertTrue(any(e['kind'] == 'request' and e['negated'] for e in result['facts']['evidence']))

    def test_pricing_requires_service_context_and_explicit_game_is_primary(self):
        self.assertEqual(app.classify('多少钱')['category'], 'uncertain')
        self.assertEqual(app.classify('多少钱', '无畏契约陪练')['category'], 'buyer')
        self.assertEqual(app.classify('这个皮肤多少钱', '无畏契约陪练')['category'], 'noise')
        self.assertEqual(app.classify('无畏契约这个鼠标多少钱')['category'], 'noise')
        self.assertEqual(app.classify('无畏契约找陪玩', '三角洲视频')['game'], '无畏契约')
        result = app.classify('多少钱', '三角洲视频', parent_context='无畏契约陪练预算200')
        self.assertEqual(result['game'], '无畏契约')
        self.assertEqual(result['facts']['game_source'], 'parent')
        self.assertEqual(result['facts']['budget'], '')
        self.assertEqual(result['facts']['service_type'], '')
        self.assertTrue(any(e['source'] == 'parent' and e['kind'] == 'service_context' for e in result['facts']['evidence']))
        result = app.classify('无畏契约和三角洲找陪玩')
        self.assertEqual(result['game'], '')
        self.assertEqual(result['category'], 'uncertain')

    def test_fact_extraction_preserves_qualifiers_and_rejects_negative_values(self):
        result = app.classify('无畏契约找陪练，今晚不要，明晚20:00-22:00，不要国服，亚服')
        facts = result['facts']
        self.assertEqual(facts['time'], '明晚 / 20:00-22:00')
        self.assertEqual(facts['region'], '亚服')
        facts = app.classify('无畏契约最多3人组队，最高段位白银')['facts']
        self.assertEqual(facts['party_size'], '最多3人')
        self.assertEqual(facts['budget'], '')
        self.assertFalse(any(x['kind'] == 'budget' for x in facts['evidence']))
        self.assertEqual(app.classify('无畏契约找陪练，预算100-200元')['facts']['budget'], '100-200')
        self.assertEqual(app.classify('无畏契约找陪练，最多200元')['facts']['budget'], '200')

    def test_conflicting_fields_remain_unselected(self):
        facts = app.classify('无畏契约找陪练，国服和亚服都可以，预算100，预算200')['facts']
        self.assertEqual(facts['region'], '')
        self.assertEqual(facts['budget'], '')
        self.assertEqual(len(facts['warnings']), 2)

    def test_evidence_spans_are_literal_and_bounded_not_probabilities(self):
        inputs = {'comment': '不是国服，亚服找陪练，今晚不要，明晚可以，预算200', 'parent': '无畏契约教练', 'video': '其他视频标题'}
        result = app.classify(inputs['comment'], inputs['video'], parent_context=inputs['parent'])
        self.assertIsNone(result['confidence'])
        self.assertEqual(result['facts']['rules_version'], RULESET_VERSION)
        for item in result['facts']['evidence']:
            self.assertEqual(inputs[item['source']][item['start']:item['end']], item['text'])
        result = app.classify('无畏契约找陪练，' * 300)
        self.assertLessEqual(len(result['facts']['evidence']), 40)

    def test_version_upgrade_keeps_historical_rules_human_and_followup(self):
        old_dir = app.DATA_DIR
        with tempfile.TemporaryDirectory(prefix='intent-version-test-') as td:
            try:
                app.DATA_DIR = Path(td)
                app.init()
                app.ingest({'records': [dict(comment_id=str(i), video_id='test-video', user_id=str(i), text='无畏契约找个搭子') for i in (1, 2)]})
                app.analyze()
                state = app.state()
                human = state['comments'][0]
                lead = state['leads'][0]
                app.mutate('review', {'id': human['id'], 'category': 'social', 'reason': '合成人工判断'})
                app.mutate('lead', {'id': lead['id'], 'stage': 'reviewed', 'owner': '合成负责人'})
                with app.db() as c:
                    # Store a genuine older snapshot marker before testing startup.
                    for row in c.execute("SELECT id,facts FROM comments WHERE analysis_method='rules'").fetchall():
                        facts = json.loads(row['facts']); facts['rules_version'] = 'rules-v2'
                        c.execute('UPDATE comments SET facts=? WHERE id=?', (json.dumps(facts), row['id']))
                    before = [tuple(r) for r in c.execute('SELECT * FROM comments ORDER BY id')]
                    c.execute("UPDATE settings SET value=? WHERE key='ruleset_version'", (json.dumps('rules-v1'),))
                    c.execute("DELETE FROM settings WHERE key='parent_context_version'")
                app.init()
                state = app.state()
                self.assertEqual(state['stats']['pending'], 0)
                with app.db() as c:
                    self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM comments ORDER BY id')], before)
                self.assertEqual(next(x for x in state['comments'] if x['id'] == human['id'])['analysis_method'], 'human')
                self.assertEqual(next(x for x in state['leads'] if x['id'] == lead['id'])['owner'], '合成负责人')
                self.assertEqual(app.analyze()['analyzed'], 0)
                app.init()
                self.assertEqual(app.state()['stats']['pending'], 0)
                app.ingest({'records': [dict(comment_id='new', video_id='test-video', user_id='new', text='付钱请教练复盘一场无畏契约')]})
                self.assertEqual(app.analyze()['analyzed'], 1)
                new = next(x for x in app.state()['comments'] if x['external_id'] == 'new')
                self.assertEqual(new['facts']['rules_version'], RULESET_VERSION)
                self.assertEqual(new['category'], 'buyer')
            finally:
                app.DATA_DIR = old_dir

    def test_reported_or_hypothetical_fields_are_not_assigned_to_this_user(self):
        for text in ['据说他找无畏契约陪练，国服，预算500，今晚，段位白银',
                     '假如找无畏契约陪练，国服，预算500，今晚，段位白银']:
            result = app.classify(text)
            self.assertEqual(result['category'], 'uncertain')
            for key in ('budget', 'party_size', 'time', 'region', 'rank_label', 'service_type'):
                self.assertEqual(result['facts'][key], '')
            fields = [e for e in result['facts']['evidence'] if e['kind'] in ('budget', 'time', 'region', 'rank_label', 'service_type')]
            self.assertTrue(fields)
            self.assertTrue(all(e['attribution'] == 'unconfirmed' for e in fields))

    def test_free_payment_negation_and_mixed_scope_remain_distinct(self):
        for text, category in [('无畏契约不付钱，找个陪练', 'social'),
                               ('无畏契约互学互带，谢绝收费', 'social'),
                               ('无畏契约免费讲座，我想找收费陪练', 'uncertain'),
                               ('无畏契约我不提供陪练，只想预约复盘', 'buyer')]:
            self.assertEqual(app.classify(text)['category'], category, text)

    def test_nearest_pricing_object_prevents_borrowing_video_service_context(self):
        self.assertEqual(app.classify('这个多少钱', '无畏契约陪练', parent_context='鼠标测评')['category'], 'noise')
        self.assertEqual(app.classify('这个多少钱', '鼠标测评', parent_context='无畏契约陪练')['category'], 'buyer')
        self.assertEqual(app.classify('这个多少钱', '无畏契约陪练和鼠标测评')['category'], 'uncertain')
        self.assertEqual(app.classify('想找无畏契约陪练，我电脑修好了', '鼠标测评')['category'], 'buyer')

    def test_bounded_repeated_evidence_retains_field_and_negation_anchors(self):
        text = ('无畏契约找陪练，多少钱，' * 70) + '国服不要，亚服，预算300，今晚'
        result = app.classify(text)
        facts = result['facts']
        self.assertEqual(facts['budget'], '300')
        self.assertEqual(facts['region'], '亚服')
        self.assertLessEqual(len(facts['evidence']), 40)
        for kind in ('budget', 'region', 'time'):
            self.assertTrue(any(e['kind'] == kind for e in facts['evidence']))
        self.assertTrue(any(e['kind'] == 'region' and e['negated'] for e in facts['evidence']))
        for e in facts['evidence']:
            self.assertEqual(text[e['start']:e['end']], e['text'])


if __name__ == '__main__':
    unittest.main()
