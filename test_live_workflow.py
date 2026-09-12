"""Synthetic local workflow tests; no browser or platform messages."""
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import clubops as app
import live_monitor as live
import live_workflow as flow

ROOM, UID = '10000000000000001', '10000000000000002'


class LiveWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clubops-live-flow-')
        self.old_dir, app.DATA_DIR = app.DATA_DIR, Path(self.temp.name)
        app.init()
        self.config = dict(live.DEFAULTS, room_url='https://live.douyin.com/12345')
        with app.db() as c:
            self.sid = c.execute('INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                ('synthetic', self.config['room_url'], ROOM, json.dumps(self.config), 'running', '', app.now(), app.now())).lastrowid

    def tearDown(self):
        app.DATA_DIR = self.old_dir
        self.temp.cleanup()

    def receive(self, n=1, **changes):
        row = dict(room_id=ROOM, message_id=str(10000000000000100+n), outer_message_id=str(10000000000000200+n),
                   uid=UID, nickname='合成用户', text='无畏契约国服找陪练，预算100元', published_at='2026-09-10T00:00:00Z')
        live.receive(self.sid, dict(type='message', record={**row, **changes}))
        with app.db() as c:
            return c.execute('SELECT MAX(id) FROM live_messages').fetchone()[0]

    def review(self, mid, **changes):
        row = flow.detail(mid)
        return flow.review({**dict(id=mid, category='uncertain', reason='合成测试：按原文核对', review_token=row['review_token']), **changes})

    def test_automatic_link_is_typed_without_fabricated_comments_or_permission(self):
        mid = self.receive()
        state = app.state()
        lead = state['leads'][0]
        self.assertEqual(lead['external_id'], UID)
        self.assertEqual((lead['comment_count'], lead['live_count'], lead['evidence_count']), (0, 1, 1))
        self.assertEqual(lead['latest']['evidence_type'], 'live')
        self.assertEqual(lead['latest']['source_url'], self.config['room_url'])
        self.assertNotIn('video_id', lead['latest'])
        self.assertNotIn('video_url', lead['latest'])
        self.assertEqual((lead['category'], lead['game']), ('buyer', '无畏契约'))
        self.assertEqual(lead['contact_basis'], '')
        self.assertEqual((state['videos'], state['comments'], state['messages'], state['jobs']), ([], [], [], []))
        self.assertEqual(live.state()['rows'][0]['lead_id'], lead['id'])
        self.assertEqual(flow.detail(mid)['person_id'], lead['person_id'])

    def test_bulk_projection_reads_current_model_config_once_per_response(self):
        self.receive()
        self.receive(2,uid='10000000000000003')
        with app.db() as c,patch('semantic.state',return_value={'engine':None}) as state:
            rows,counts=flow.latest_by_person(c)
            self.assertEqual(len(rows),2)
            self.assertEqual(state.call_count,1)
            rows2=flow.records(c)
            self.assertEqual(len(rows2),2)
            self.assertEqual(state.call_count,2)  # A new read sees new config, no cross-request cache.

    def test_numeric_uid_shared_with_browser_comment_preserves_refusal(self):
        self.receive()
        lead = app.state()['leads'][0]
        app.mutate('contact', dict(lead_id=lead['id'], contact_basis='', contact_note='合成禁止联系', do_not_contact=True))
        with app.db() as c:
            source = c.execute("SELECT id FROM sources WHERE kind='browser'").fetchone()[0]
        app.ingest(dict(source_id=source, records=[dict(comment_id='test-comment', video_id='test-video', user_id=UID,
            text='无畏契约免费组队', published_at='2026-09-11T00:00:00Z')]), allow_browser_source=True)
        app.analyze()
        self.receive(2, nickname='同 UID 另一个昵称')
        state = app.state()
        self.assertEqual(len(state['leads']), 1)
        lead = state['leads'][0]
        self.assertEqual((lead['comment_count'], lead['live_count']), (1, 2))
        self.assertTrue(lead['do_not_contact'])
        self.assertEqual(lead['contact_note'], '合成禁止联系')
        self.assertEqual(lead['latest']['evidence_type'], 'comment')
        self.assertEqual(lead['category'], 'social')

    def test_import_namespace_and_nickname_do_not_merge_identities(self):
        self.receive()
        self.receive(2, uid='10000000000000003')
        app.ingest(dict(records=[dict(comment_id='import', video_id='import-video', user_id=UID, text='合成样例', nickname='合成用户')]))
        self.assertEqual(len(app.state()['leads']), 3)
        self.assertEqual(len({l['person_id'] for l in app.state()['leads']}), 3)

    def test_missing_uid_and_keyword_rejection_do_not_auto_create_leads(self):
        missing = self.receive(uid=None)
        self.assertEqual(app.state()['leads'], [])
        self.review(missing)
        self.assertIsNone(flow.detail(missing)['lead_id'])
        with app.db() as c:
            self.config['exclude_keywords'] = '陪练'
            c.execute('UPDATE live_sessions SET config=? WHERE id=?', (json.dumps(self.config), self.sid))
        excluded = self.receive(2)
        self.assertEqual(app.state()['leads'], [])
        self.assertEqual(flow.detail(excluded)['filter_reason'], 'filtered_blocked')
        self.review(excluded)
        self.assertEqual(len(app.state()['leads']), 1, 'Explicit review may admit a filtered record')
        self.assertEqual(flow.detail(excluded)['filter_reason'], 'filtered_blocked')
        self.assertEqual(app.state()['leads'][0]['contact_basis'], '')

    def test_manual_overlay_keeps_original_rules_identity_and_source(self):
        mid = self.receive()
        original = flow.detail(mid)
        self.review(mid, manual_fields={'game': '', 'budget': '', 'region': '亚服'})
        reviewed = flow.detail(mid)
        self.assertEqual(reviewed['analysis_method'], 'human')
        self.assertEqual(reviewed['facts']['budget'], '')
        self.assertEqual(reviewed['facts']['region'], '亚服')
        self.assertEqual(reviewed['game'], '')
        for key in ('raw_text', 'published_at', 'observed_at', 'rule_facts', 'rule_category', 'rule_game', 'message_id', 'uid'):
            self.assertEqual(reviewed[key], original[key])
        app.analyze()
        app.init()
        self.assertEqual(flow.detail(mid)['manual_fields'], reviewed['manual_fields'])
        self.assertEqual(app.state()['leads'][0]['latest']['facts'], reviewed['facts'])
        with app.db() as c:
            stored = c.execute('SELECT category,analysis_method FROM live_messages WHERE id=?', (mid,)).fetchone()
            self.assertEqual(tuple(stored), ('buyer', 'rules'))

    def test_review_version_reason_and_fields_are_atomic(self):
        mid = self.receive()
        old = flow.detail(mid)
        for values in ({'reason': ''}, {'category': 'invalid'}, {'manual_fields': {'uid': UID}}, {'manual_fields': {'game': 'unknown'}}):
            with self.assertRaises(ValueError):
                self.review(mid, **values)
        self.assertEqual(flow.detail(mid)['review_history'], [])
        self.review(mid)
        with self.assertRaisesRegex(ValueError, '已更新'):
            flow.review(dict(id=mid, category='buyer', reason='旧表单', review_token=old['review_token']))
        self.assertEqual(len(flow.detail(mid)['review_history']), 1)
        for value in (True, None, -1, '1.2', '１'):
            with self.assertRaises(ValueError):
                flow.detail(value)

    def test_category_only_preserves_overrides_and_reset_restores_rule_values(self):
        mid = self.receive()
        original = flow.detail(mid)
        self.review(mid, manual_fields={'budget': ''})
        self.review(mid, category='social')
        self.assertEqual(flow.detail(mid)['manual_fields'], {'budget': ''})
        self.review(mid, manual_fields={})
        self.assertEqual(flow.detail(mid)['facts'], original['facts'])
        self.assertEqual(len(flow.detail(mid)['review_history']), 3)

    def test_history_is_paginated_and_state_only_has_one_live_representative(self):
        for n in range(1, 53):
            self.receive(n)
        state = app.state()
        lead = state['leads'][0]
        self.assertEqual(len(state['live_messages']), 1)
        self.assertEqual(lead['live_count'], 52)
        first, second = flow.history(lead['id']), flow.history(lead['id'], 50)
        self.assertEqual((len(first['rows']), len(second['rows']), first['total']), (50, 2, 52))
        self.assertFalse({r['id'] for r in first['rows']} & {r['id'] for r in second['rows']})
        with self.assertRaises(ValueError):
            flow.history(lead['id'], -1)

    def test_upgrade_backup_backfill_is_idempotent_and_retains_unknown_game(self):
        mid = self.receive()
        lead = app.state()['leads'][0]
        app.mutate('contact', dict(lead_id=lead['id'], contact_basis='', do_not_contact=True))
        original = flow.detail(mid)
        with app.db() as c:
            c.executescript('DROP TABLE live_links; DROP TABLE live_judgments; DROP TABLE live_reviews;')
            c.execute('ALTER TABLE live_messages DROP COLUMN game')
        with patch('subprocess.Popen') as network_process:
            app.init()
            app.init()
            network_process.assert_not_called()
        backup = list((app.DATA_DIR / 'backups').glob('*.before-live-workflow-*.bak'))
        self.assertEqual(len(backup), 1)
        with closing(sqlite3.connect(backup[0])) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='live_links'").fetchone())
            self.assertEqual(c.execute('SELECT raw_text FROM live_messages').fetchone()[0], original['raw_text'])
        self.assertEqual(flow.detail(mid)['game'], '')
        self.assertEqual(flow.detail(mid)['rule_facts'], original['rule_facts'])
        self.assertEqual(len(app.state()['leads']), 1)
        self.assertTrue(app.state()['leads'][0]['do_not_contact'])

    def test_review_to_draft_and_followup_does_not_authorize_or_send(self):
        mid = self.receive()
        self.review(mid)
        lead = app.state()['leads'][0]
        with patch('uid_transport.send') as send, patch('subprocess.Popen') as process:
            draft = app.mutate('draft', dict(lead_id=lead['id'], request_id='local-draft', content='合成草稿，未发送'))
            self.assertEqual(app.mutate('send', {'id': draft['id']})['status'], 'blocked')
            app.mutate('lead', dict(id=lead['id'], stage='reviewed', outcome_note='合成内部核对记录'))
            send.assert_not_called()
            process.assert_not_called()
        self.assertEqual(app.state()['messages'], [])
        self.assertEqual(app.state()['leads'][0]['contact_basis'], '')

    def test_local_http_detail_review_history_and_csrf(self):
        import server
        mid = self.receive()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(server, 'PORT', port):
                base = f'http://127.0.0.1:{port}'
                with urlopen(base + f'/api/live-message?id={mid}') as response:
                    row = json.load(response)
                body = json.dumps(dict(id=str(mid), category='uncertain', reason='本地 HTTP 合成核对', review_token=row['review_token'])).encode()
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(Request(base + '/api/live-review', data=body, headers={'Content-Type': 'application/json'}))
                self.assertEqual(rejected.exception.code, 403)
                with urlopen(Request(base + '/api/live-review', data=body, headers={'Content-Type': 'application/json', 'X-ClubOps-Token': server.CSRF})) as response:
                    self.assertTrue(json.load(response)['ok'])
                with urlopen(base + f"/api/lead-live-history?lead_id={row['lead_id']}&offset=0") as response:
                    history = json.load(response)
                self.assertEqual(history['total'], 1)
                self.assertEqual(history['rows'][0]['analysis_method'], 'human')
                with urlopen(base + '/api/state') as response:
                    state = json.load(response)
                self.assertEqual(state['leads'][0]['latest']['evidence_type'], 'live')
                self.assertEqual((state['comments'], state['messages'], state['jobs']), ([], [], []))
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()


if __name__ == '__main__':
    unittest.main()
