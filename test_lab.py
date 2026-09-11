import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import clubops as app
import server


class ClubOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clubops-test-')
        self.old_dir = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()

    def tearDown(self):
        app.DATA_DIR = self.old_dir
        self.temp.cleanup()

    def record(self, **changes):
        return dict(comment_id='c1', video_id='v1', user_id='u1', nickname='用户甲', text='三角洲今晚找两个人带一下，预算200', published_at='2026-09-09T10:00:00+08:00', **changes) if not changes else {**self.record(), **changes}

    def ingest(self, rows=None):
        return app.ingest({'records': rows or [self.record()]})

    def lead(self):
        self.ingest()
        app.analyze()
        return app.state()['leads'][0]

    def draft(self, lead_id, mode='live', request='test-request'):
        return app.mutate('draft', {'lead_id': lead_id, 'content': '你好，请确认具体需求。', 'request_id': request}, mode)

    def test_live_starts_empty_and_demo_is_isolated(self):
        self.assertEqual(app.state()['stats']['comments'], 0)
        self.assertEqual(app.state()['members'], [])
        app.seed_demo()
        demo = app.state('demo')
        self.assertGreater(demo['stats']['comments'], 0)
        self.assertTrue(all(x['external_id'].startswith('demo-') for x in demo['comments']))
        self.assertEqual(app.state()['stats']['comments'], 0)
        app.seed_demo()
        self.assertEqual(app.state('demo')['stats']['comments'], demo['stats']['comments'])

    def test_rules_separate_buyer_provider_free_recruit(self):
        cases = [('三角洲找两个人带一下，预算200', 'buyer'), ('三角洲陪玩接单，有老板吗', 'seller'), ('王者不找收费，只找队友', 'social'), ('俱乐部招陪玩', 'recruit'), ('自己接单，也想找个陪玩', 'uncertain'), ('哈哈太厉害了', 'noise')]
        for raw, category in cases:
            self.assertEqual(app.classify(raw)['category'], category)
        facts = app.classify('多少钱？')['facts']
        self.assertEqual(facts['budget'], '')
        self.assertEqual(facts['party_size'], '')
        self.assertEqual(facts['region'], '')
        self.assertEqual(app.classify('今晚找两个人，预算200')['facts']['budget'], '200')

    def test_duplicate_preserves_separate_comment_events_and_latest_time(self):
        self.ingest()
        self.assertEqual(self.ingest()['duplicate'], 1)
        self.ingest([self.record(comment_id='c2', text='昨天问过价格', published_at='2026-09-08T10:00:00+08:00')])
        state = app.state()
        self.assertEqual(len(state['leads']), 1)
        self.assertEqual(state['leads'][0]['comment_count'], 2)
        self.assertEqual(state['leads'][0]['latest']['external_id'], 'c1')

    def test_latest_comment_uses_actual_instant_for_legacy_timezone_offsets(self):
        self.ingest([self.record(comment_id='earlier'), self.record(comment_id='later')])
        # Legacy timestamps can retain offsets even though new imports normalize UTC.
        with app.db() as c:
            c.execute("UPDATE comments SET published_at='2026-09-09T10:00:00+08:00' WHERE external_id='earlier'")
            c.execute("UPDATE comments SET published_at='2026-09-09T03:00:00+00:00' WHERE external_id='later'")
        state = app.state()
        self.assertEqual([r['external_id'] for r in state['comments']], ['later', 'earlier'])
        self.assertEqual(state['leads'][0]['latest']['external_id'], 'later')
        self.assertEqual(state['comments'][1]['published_at'], '2026-09-09T10:00:00+08:00')
        self.assertEqual(len(state['leads']), 1)

    def test_unknown_publication_keeps_separate_observation_timestamp(self):
        self.ingest([self.record(published_at=None)])
        row = app.state()['comments'][0]
        self.assertIsNone(row['published_at'])
        self.assertTrue(row['discovered_at'])
        self.assertIsNone(app.state()['leads'][0]['latest']['published_at'])

    def test_revision_is_audited_and_does_not_overwrite_other_comments(self):
        self.lead()
        first = app.state()['comments'][0]
        app.mutate('review', {'id': first['id'], 'category': 'seller'})
        app.analyze()
        self.assertEqual(app.state()['comments'][0]['analysis_method'], 'human')
        result = self.ingest([self.record(text='不找陪玩，只找队友')])
        revised = app.state()['comments'][0]
        self.assertEqual(result['revised'], 1)
        self.assertEqual(revised['analysis_method'], 'pending')
        self.assertEqual(revised['game'], '')
        self.assertEqual(revised['discovered_at'], first['discovered_at'])
        with app.db() as db:
            revision = db.execute('SELECT * FROM comment_revisions').fetchone()
            self.assertEqual(revision['raw_text'], first['raw_text'])

    def test_invalid_batch_rolls_back_and_identity_conflicts_reject(self):
        with self.assertRaises(ValueError):
            self.ingest([self.record(), self.record(comment_id='c2', published_at='bad')])
        self.assertEqual(app.state()['comments'], [])
        self.ingest()
        with self.assertRaises(ValueError):
            self.ingest([self.record(comment_id='c2'), self.record(video_id='wrong-video')])
        self.assertEqual(len(app.state()['comments']), 1)
        with self.assertRaises(ValueError):
            self.ingest([self.record(user_id='different-user')])
        self.assertEqual(len(app.state()['leads']), 1)

    def test_metadata_can_be_enriched_without_inventing_users(self):
        self.ingest([self.record(user_id='', nickname='', published_at='')])
        self.assertEqual(app.state()['leads'], [])
        self.assertIsNone(app.state()['comments'][0]['published_at'])
        self.ingest([self.record(video_title='三角洲组队', video_url='https://www.douyin.com/video/123')])
        state = app.state()
        self.assertEqual(len(state['comments']), 1)
        self.assertEqual(len(state['leads']), 1)
        self.assertEqual(state['videos'][0]['title'], '三角洲组队')
        self.assertEqual(state['leads'][0]['nickname'], '用户甲')
        self.ingest([self.record(nickname='')])
        self.assertEqual(app.state()['leads'][0]['nickname'], '用户甲')

    def test_same_nickname_different_ids_are_not_merged(self):
        self.ingest([self.record(), self.record(comment_id='c2', user_id='u2')])
        self.assertEqual(len(app.state()['leads']), 2)

    def test_imported_video_title_enrichment_requeues_old_rules(self):
        self.ingest([self.record(video_title='', text='多少钱')])
        app.analyze()
        self.assertEqual(app.state()['comments'][0]['category'], 'uncertain')
        result = self.ingest([self.record(video_title='无畏契约陪练', text='多少钱')])
        self.assertEqual(result['duplicate'], 1)
        self.assertEqual(app.state()['stats']['pending'], 1)
        app.analyze()
        self.assertEqual(app.state()['comments'][0]['game'], '无畏契约')
        self.assertEqual(app.state()['comments'][0]['category'], 'buyer')

    def test_parent_comment_context_and_timestamp_parsing(self):
        self.ingest([self.record(text='无畏契约陪练分享'), self.record(comment_id='reply', user_id='u2', text='多少钱', parent_comment_id='c1')])
        app.analyze()
        reply = next(c for c in app.state()['comments'] if c['external_id'] == 'reply')
        self.assertEqual(reply['game'], '无畏契约')
        self.assertEqual(reply['category'], 'buyer')
        self.assertEqual(app.timestamp('2026-09-09T10:00:00'), '2026-09-09T02:00:00+00:00')
        self.assertEqual(app.timestamp(1700000000), app.timestamp(1700000000000))

    def test_csv_and_unsafe_links(self):
        result = app.ingest({'csv': 'comment_id,video_id,user_id,text\nc1,v1,u1,三角洲找陪玩\n'})
        self.assertEqual(result['inserted'], 1)
        with self.assertRaises(ValueError):
            self.ingest([self.record(video_url='javascript:alert(1)')])

    def test_parent_relations_reject_cross_video_self_and_cycles_atomically(self):
        for rows in [
            [self.record(parent_comment_id='c1')],
            [self.record(parent_comment_id='c2'), self.record(comment_id='c2', parent_comment_id='c1')],
            [self.record(parent_comment_id='c2'), self.record(comment_id='c2', video_id='v2')],
            [self.record(), self.record(comment_id='c2', video_id='v2', parent_comment_id='c1')],
        ]:
            with self.assertRaises(ValueError):
                self.ingest(rows)
            self.assertEqual(app.state()['comments'], [])
            self.assertEqual(app.state()['leads'], [])

    def test_parent_evidence_late_arrival_and_revision_requeues_rules(self):
        self.ingest([self.record(comment_id='child', text='多少钱', parent_comment_id='parent')])
        app.analyze()
        child = app.state()['comments'][0]
        self.assertEqual(child['parent_context'], {'status': 'missing', 'external_id': 'parent'})
        self.assertEqual(child['game'], '')
        self.ingest([self.record(comment_id='parent', user_id='u2', text='无畏契约新手陪练，预算200，国服')])
        child = next(r for r in app.state()['comments'] if r['external_id'] == 'child')
        self.assertEqual(child['analysis_method'], 'pending')
        self.assertEqual(child['parent_context']['status'], 'available')
        self.assertEqual(child['parent_context']['raw_text'], '无畏契约新手陪练，预算200，国服')
        app.analyze()
        child = next(r for r in app.state()['comments'] if r['external_id'] == 'child')
        self.assertEqual(child['game'], '无畏契约')
        for key in ('service_type', 'budget', 'region'):
            self.assertEqual(child['facts'][key], '', 'Do not copy another person\'s requirements')
        self.ingest([self.record(comment_id='parent', user_id='u2', text='三角洲陪玩')])
        app.analyze()
        child = next(r for r in app.state()['comments'] if r['external_id'] == 'child')
        self.assertEqual(child['game'], '三角洲行动')

    def test_parent_enrichment_preserves_human_and_rejects_changed_parent(self):
        self.ingest([self.record(comment_id='parent', text='无畏契约陪练'), self.record(comment_id='child', text='多少钱')])
        app.analyze()
        self.ingest([self.record(comment_id='child', text='多少钱', parent_comment_id='parent')])
        child = next(r for r in app.state()['comments'] if r['external_id'] == 'child')
        self.assertEqual(child['analysis_method'], 'pending')
        app.analyze()
        app.mutate('review', {'id': child['id'], 'category': 'uncertain', 'reason': '人工需要核对'})
        self.ingest([self.record(comment_id='parent', text='修订后的主评论')])
        app.analyze()
        child = next(r for r in app.state()['comments'] if r['external_id'] == 'child')
        self.assertEqual(child['analysis_method'], 'human')
        self.assertEqual(child['category'], 'uncertain')
        with self.assertRaises(ValueError):
            self.ingest([self.record(comment_id='child', text='多少钱', parent_comment_id='other')])
        self.assertEqual(next(r for r in app.state()['comments'] if r['external_id'] == 'child')['parent_external_id'], 'parent')

    def test_parent_source_isolation_and_legacy_cross_video_context(self):
        self.ingest([self.record(comment_id='parent', text='无畏契约陪练')])
        app.mutate('source', {'name': '另一独立测试来源', 'kind': 'import'})
        other_source = app.state()['sources'][-1]['id']
        app.ingest({'source_id': other_source, 'records': [self.record(comment_id='child', text='多少钱', parent_comment_id='parent')]})
        app.analyze()
        child = next(r for r in app.state()['comments'] if r['external_id'] == 'child')
        self.assertEqual(child['game'], '')
        self.assertEqual(child['parent_context']['status'], 'missing')
        self.ingest([self.record(comment_id='legacy', video_id='v2', text='多少钱')])
        with app.db() as c:
            c.execute("UPDATE comments SET parent_external_id='parent' WHERE external_id='legacy'")
        app.analyze()
        legacy = next(r for r in app.state()['comments'] if r['external_id'] == 'legacy')
        self.assertEqual(legacy['game'], '')
        self.assertEqual(legacy['parent_context']['status'], 'conflict')
        self.assertNotIn('raw_text', legacy['parent_context'])

    def test_parent_late_arrival_different_video_rolls_back(self):
        self.ingest([self.record(parent_comment_id='late')])
        with self.assertRaises(ValueError):
            self.ingest([self.record(comment_id='late', video_id='v2')])
        self.assertEqual(len(app.state()['comments']), 1)

    def test_nickname_without_uid_is_saved_without_creating_identity(self):
        self.ingest([self.record(user_id='', nickname='仅评论中读到的昵称')])
        row = app.state()['comments'][0]
        self.assertEqual(row['nickname'], '仅评论中读到的昵称')
        self.assertEqual(app.state()['leads'], [])
        self.assertIsNone(row['person_id'])
        self.ingest([self.record(user_id='u1', nickname='后来的昵称')])
        row = app.state()['comments'][0]
        self.assertEqual(row['observed_nickname'], '仅评论中读到的昵称')
        self.assertEqual(app.state()['leads'][0]['nickname'], '后来的昵称')

    def test_parent_context_migration_preserves_rows_and_human_decisions(self):
        self.ingest([self.record(comment_id='parent'), self.record(comment_id='child', parent_comment_id='parent'), self.record(comment_id='human', parent_comment_id='parent')])
        app.analyze()
        human = next(r for r in app.state()['comments'] if r['external_id'] == 'human')
        app.mutate('review', {'id': human['id'], 'category': 'uncertain'})
        with app.db() as c:
            preserved = [tuple(r) for r in c.execute('SELECT * FROM comments ORDER BY id')]
            c.execute("DELETE FROM settings WHERE key='parent_context_version'")
        app.init()
        rows = {r['external_id']: r for r in app.state()['comments']}
        self.assertEqual(rows['parent']['analysis_method'], 'rules')
        self.assertEqual(rows['child']['analysis_method'], 'rules')
        self.assertEqual(rows['human']['analysis_method'], 'human')
        with app.db() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM comments ORDER BY id')], preserved)
        self.assertEqual(app.analyze()['analyzed'], 0)
        app.init()
        self.assertEqual(next(r for r in app.state()['comments'] if r['external_id'] == 'child')['analysis_method'], 'rules')

    def test_valorant_services_and_unpaid_partner_intent(self):
        cases = [
            ('瓦陪玩怎么收费，国服娱乐开黑', 'buyer', '娱乐开黑'),
            ('无畏契约新手找个陪练，想练枪', 'buyer', '新手陪练'),
            ('VALORANT找个排位陪玩，段位白银，亚服', 'buyer', '排位组队'),
            ('国服无畏契约复盘多少钱', 'buyer', '对局复盘'),
            ('VALORANT找搭子开黑，有人吗', 'uncertain', '娱乐开黑'),
        ]
        for raw, category, service in cases:
            result = app.classify(raw)
            self.assertEqual(result['game'], '无畏契约')
            self.assertEqual(result['category'], category)
            self.assertEqual(result['facts']['service_type'], service)
        result = app.classify(cases[2][0])
        self.assertEqual(result['facts']['rank_label'], '白银')
        self.assertEqual(result['facts']['region'], '亚服')
        self.assertEqual(app.classify('房顶瓦片多少钱')['game'], '')
        self.assertEqual(app.classify('无畏契约找陪玩', '三角洲视频讨论')['game'], '无畏契约')

    def test_member_service_fields_are_validated_and_preserved(self):
        app.mutate('member', {'name': '无畏陪玩甲', 'game': '无畏契约', 'region': '国服', 'service_types': ['娱乐开黑', '新手陪练'], 'rank_label': '以当期核验为准'})
        member = app.state()['members'][0]
        self.assertEqual(member['service_types'], ['娱乐开黑', '新手陪练'])
        self.assertEqual(member['rank_label'], '以当期核验为准')
        app.mutate('member', {'id': member['id'], 'name': member['name'], 'game': member['game']})
        self.assertEqual(app.state()['members'][0]['service_types'], member['service_types'])
        with self.assertRaises(ValueError):
            app.mutate('member', {'name': 'invalid', 'game': '无畏契约', 'service_types': ['未定义服务']})

    def test_profile_upgrade_preserves_custom_settings_and_history(self):
        self.ingest()
        with app.db() as db:
            db.execute("DELETE FROM settings WHERE key='valorant_profile_version'")
            db.execute("UPDATE settings SET value=? WHERE key='club_name'", (json.dumps('用户自己的俱乐部'),))
            db.execute("UPDATE settings SET value=? WHERE key='keywords'", (json.dumps('用户自定义关键词'),))
        app.init()
        state = app.state()
        self.assertEqual(state['settings']['club_name'], '用户自己的俱乐部')
        self.assertEqual(state['settings']['keywords'], '用户自定义关键词')
        self.assertEqual(state['comments'][0]['raw_text'], self.record()['text'])
        self.assertEqual(state['profile']['game'], '无畏契约')
        self.assertEqual(len(state['profile']['services']), 4)

    def test_valorant_demo_uses_only_valorant_and_preserves_old_demo_file(self):
        legacy = app.DATA_DIR / 'clubops-demo.db'
        # An existing legacy file must not be touched by the new demo profile.
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute('CREATE TABLE old_data(note TEXT)')
            db.execute("INSERT INTO old_data VALUES('legacy')")
        app.seed_demo()
        demo = app.state('demo')
        self.assertTrue(all(c['game'] == '无畏契约' for c in demo['comments']))
        self.assertTrue(all(m['game'] == '无畏契约' for m in demo['members']))
        self.assertTrue(any(c['facts']['service_type'] == '对局复盘' for c in demo['comments']))
        with closing(sqlite3.connect(legacy)) as db, db:
            self.assertEqual(db.execute('SELECT note FROM old_data').fetchone()[0], 'legacy')

    def test_roster_and_follow_up_persist(self):
        lead = self.lead()
        app.mutate('member', {'name': '人员甲', 'game': '三角洲行动', 'price': 60, 'available': True})
        member = app.state()['members'][0]
        app.mutate('lead', {'id': lead['id'], 'stage': 'won', 'owner': '经理', 'assigned_member': member['id'], 'outcome_note': '手工记录'})
        self.assertEqual(app.state()['stats']['won'], 1)
        self.assertEqual(app.state()['leads'][0]['assigned_member'], member['id'])
        app.mutate('member-toggle', {'id': member['id']})
        self.assertFalse(app.state()['members'][0]['available'])
        for price in ('nan', -1, 'inf'):
            with self.assertRaises(ValueError):
                app.mutate('member', {'name': '无效', 'game': '三角洲行动', 'price': price})

    def test_referral_is_separate_from_legacy_sales_and_requires_confirmation(self):
        lead = self.lead()
        app.mutate('member', {'name': '历史人员', 'game': '三角洲行动', 'available': True})
        member = app.state()['members'][0]
        app.mutate('lead', {'id': lead['id'], 'stage': 'won', 'owner': '历史负责人',
            'assigned_member': member['id'], 'outcome_note': '历史成交记录'})
        self.assertEqual(app.state()['stats']['won'], 1)
        self.assertEqual(app.state()['stats']['referred'], 0)
        with self.assertRaisesRegex(ValueError, '导流确认记录'):
            app.mutate('lead', {'id': lead['id'], 'stage': 'referred', 'outcome_note': ''})
        self.assertEqual(app.state()['leads'][0]['stage'], 'won')
        self.assertEqual(app.state()['leads'][0]['outcome_note'], '历史成交记录')
        app.mutate('lead', {'id': lead['id'], 'stage': 'referred', 'outcome_note': '合成测试：用户确认已咨询承接俱乐部'})
        state = app.state()
        self.assertEqual(state['stats']['referred'], 1)
        self.assertEqual(state['stats']['won'], 0)
        self.assertEqual(state['leads'][0]['owner'], '历史负责人')
        self.assertEqual(state['leads'][0]['assigned_member'], member['id'])
        self.assertEqual(state['leads'][0]['contact_basis'], '')
        self.assertEqual(state['messages'], [])
        self.assertEqual(state['jobs'], [])
        app.seed_demo()
        self.assertEqual(app.state('demo')['stats']['referred'], 0)
        app.init()
        self.assertEqual(app.state()['stats']['referred'], 1)

    def test_import_cannot_grant_permission_real_send_is_never_faked(self):
        self.ingest([self.record(contact_basis='opt_in', consent=True)])
        lead = app.state()['leads'][0]
        self.assertEqual(lead['contact_basis'], '')
        draft = self.draft(lead['id'])
        self.assertEqual(app.mutate('send', {'id': draft['id']})['status'], 'blocked')
        app.mutate('contact', {'lead_id': lead['id'], 'contact_basis': 'inbound', 'contact_note': '内部测试咨询记录'})
        self.assertEqual(app.mutate('send', {'id': draft['id']})['status'], 'not_connected')
        self.assertEqual(app.state()['messages'], [])
        self.assertEqual(app.state()['stats']['submitted'], 0)
        with self.assertRaises(ValueError):
            app.mutate('contact', {'lead_id': lead['id'], 'contact_basis': 'test', 'contact_note': '不允许正式区测试身份'})

    def test_draft_is_idempotent_even_under_parallel_requests(self):
        lead = self.lead()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.draft(lead['id']), range(12)))
        self.assertEqual(len({r['id'] for r in results}), 1)
        self.assertEqual(len(app.state()['jobs']), 1)
        with self.assertRaises(ValueError):
            app.mutate('draft', {'lead_id': lead['id'], 'content': '另一条内容', 'request_id': 'test-request'})

    def test_demo_send_is_idempotent_and_do_not_contact_is_enforced(self):
        app.seed_demo()
        lead = next(l for l in app.state('demo')['leads'] if l['contact_basis'] == 'test')
        draft = self.draft(lead['id'], 'demo')
        baseline = len(app.state('demo')['messages'])
        self.assertEqual(app.mutate('send', {'id': draft['id']}, 'demo')['status'], 'demo_sent')
        app.mutate('send', {'id': draft['id']}, 'demo')
        self.assertEqual(len(app.state('demo')['messages']), baseline + 1)
        app.mutate('contact', {'lead_id': lead['id'], 'contact_basis': 'test', 'contact_note': '仅测试', 'do_not_contact': True}, 'demo')
        next_draft = self.draft(lead['id'], 'demo', 'second-request')
        self.assertEqual(app.mutate('send', {'id': next_draft['id']}, 'demo')['status'], 'blocked')
        self.assertEqual(app.state()['stats']['submitted'], 0)

    def test_http_security_and_frontend_routes(self):
        original_port = server.PORT
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        server.PORT = httpd.server_port
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        def request(method, path, data=None, headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
            conn.request(method, path, json.dumps(data) if data is not None else None, headers or {})
            response = conn.getresponse()
            status, result = response.status, response.read()
            conn.close()
            return status, result
        try:
            for path in ['/', '/app.css', '/app.js', '/vendor/lucide.min.js']:
                self.assertEqual(request('GET', path)[0], 200)
            status, raw = request('GET', '/api/state')
            state = json.loads(raw)
            self.assertEqual(status, 200)
            self.assertEqual(state['stats']['comments'], 0)
            self.assertEqual(request('POST', '/api/settings', {'club_name': 'wrong'})[0], 403)
            self.assertEqual(request('POST', '/api/settings', {'club_name': '测试俱乐部'}, {'X-ClubOps-Token': state['csrf']})[0], 200)
            self.assertEqual(request('POST', '/api/settings', {}, {'X-ClubOps-Token': state['csrf'], 'Origin': 'https://evil.invalid'})[0], 403)
            self.assertEqual(request('GET', '/api/state', headers={'Host': 'evil.invalid'})[0], 403)
            self.assertEqual(request('GET', '/api/state?mode=wrong')[0], 400)
            self.assertEqual(request('GET', '/leadops_lab.db')[0], 404)
            self.assertEqual(request('GET', '/api/export')[0], 200)
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=2)
            server.PORT = original_port


if __name__ == '__main__':
    unittest.main(verbosity=2)
