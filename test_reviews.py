import json
import tempfile
import unittest
from pathlib import Path

import clubops as app


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clubops-review-')
        self.old_dir, app.DATA_DIR = app.DATA_DIR, Path(self.temp.name)
        app.init()
        self.record = dict(comment_id='review-c1', video_id='review-v1', user_id='review-u1',
            text='无畏契约国服找个陪玩，周末，预算100', video_title='合成测试视频')
        app.ingest({'records': [self.record]})
        app.analyze()

    def tearDown(self):
        app.DATA_DIR = self.old_dir
        self.temp.cleanup()

    def comment(self):
        return app.state()['comments'][0]

    def review(self, fields, reason='隔离测试：根据合成原文人工核对', **changes):
        c = self.comment()
        return app.mutate('review', dict(id=c['id'], category='buyer', reason=reason,
            review_token=c['review_token'], manual_fields=fields, **changes))

    def test_fields_overlay_preserves_rules_source_and_downstream_values(self):
        before = self.comment()
        fields = {'game': '无畏契约', 'service_type': '对局复盘', 'region': '亚服',
            'budget': '', 'party_size': '最多3人', 'rank_label': '白银', 'time': '周末晚上'}
        self.review(fields)
        c = self.comment()
        self.assertEqual(c['rule_facts'], before['rule_facts'])
        self.assertEqual(c['raw_text'], self.record['text'])
        self.assertEqual(c['facts']['service_type'], '对局复盘')
        self.assertEqual(c['facts']['budget'], '')
        self.assertEqual(c['manual_fields'], fields)
        self.assertEqual(c['review_history'][0]['manual_fields'], fields)
        self.assertEqual(app.state()['leads'][0]['latest']['facts'], c['facts'])
        app.analyze()
        app.init()
        self.assertEqual(self.comment()['manual_fields'], fields)
        self.assertEqual(app.state()['messages'], [])
        self.assertEqual(app.state()['leads'][0]['contact_basis'], '')

    def test_invalid_fields_and_missing_reason_are_atomic(self):
        before = self.comment()
        invalid = [None, [], {'nickname': '不允许'}, {'budget': 200}, {'time': 'x' * 121},
            {'game': '不存在的游戏'}, {'service_type': '不存在的服务'}]
        for fields in invalid:
            with self.assertRaises(ValueError):
                self.review(fields)
        with self.assertRaises(ValueError):
            self.review({'budget': ''}, reason='')
        with self.assertRaises(ValueError):
            app.mutate('review', {'id': before['id'], 'category': 'buyer', 'reason': '合成测试', 'manual_fields': {}})
        self.assertEqual(self.comment()['review_token'], before['review_token'])
        self.assertEqual(self.comment()['review_history'], [])

    def test_stale_form_cannot_overwrite_newer_review(self):
        old = self.comment()
        self.review({'region': '亚服'})
        with self.assertRaisesRegex(ValueError, '已更新'):
            app.mutate('review', {'id': old['id'], 'category': 'social', 'reason': '旧弹窗',
                'review_token': old['review_token'], 'manual_fields': {'region': '国服'}})
        self.assertEqual(self.comment()['manual_fields'], {'region': '亚服'})
        self.assertEqual(len(self.comment()['review_history']), 1)

    def test_revision_clears_current_overrides_and_keeps_historical_evidence(self):
        self.review({'service_type': '新手陪练'})
        old = self.comment()
        app.ingest({'records': [{**self.record, 'text': '无畏契约免费组队，不找收费的'}]})
        c = self.comment()
        self.assertEqual(c['manual_fields'], {})
        self.assertEqual(c['analysis_method'], 'pending')
        self.assertEqual(c['review_history'][0]['raw_text'], self.record['text'])
        with self.assertRaises(ValueError):
            app.mutate('review', {'id': old['id'], 'category': 'buyer', 'review_token': old['review_token']})
        app.analyze()
        self.assertEqual(self.comment()['category'], 'social')
        self.assertEqual(len(self.comment()['review_history']), 1)

    def test_category_only_preserves_fields_and_explicit_reset_restores_rules(self):
        before = self.comment()
        self.review({'game': '', 'service_type': '', 'region': '', 'budget': ''})
        self.assertEqual(app.state()['leads'][0]['game'], '')
        app.mutate('review', {'id': before['id'], 'category': 'uncertain', 'reason': '只改分类'})
        self.assertEqual(self.comment()['manual_fields']['budget'], '')
        self.review({})
        self.assertEqual(self.comment()['facts'], before['facts'])
        self.assertEqual(self.comment()['game'], before['game'])
        self.assertEqual(len(self.comment()['review_history']), 3)

    def test_history_response_is_bounded_but_database_history_is_not_deleted(self):
        for i in range(12):
            self.review({'budget': str(100+i)})
        c = self.comment()
        self.assertEqual(len(c['review_history']), 10)
        self.assertEqual(c['review_history'][0]['manual_fields']['budget'], '111')
        with app.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM comment_reviews').fetchone()[0], 12)
        app.seed_demo()
        self.assertTrue(all(not row['review_history'] for row in app.state('demo')['comments']))

    def test_legacy_schema_adds_empty_overrides_without_changing_source_or_human(self):
        old = self.comment()
        app.mutate('review', {'id': old['id'], 'category': 'uncertain', 'reason': '旧人工判断'})
        with app.db() as c:
            c.execute('ALTER TABLE comments DROP COLUMN manual_fields')
        app.init()
        c = self.comment()
        self.assertEqual(c['manual_fields'], {})
        self.assertEqual(c['analysis_method'], 'human')
        self.assertEqual(c['reason'], '旧人工判断')
        self.assertEqual(c['raw_text'], old['raw_text'])
        self.assertEqual(c['rule_facts'], old['rule_facts'])


if __name__ == '__main__':
    unittest.main()
