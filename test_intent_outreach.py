"""Synthetic, offline automatic outreach integration checks."""
import unittest
from unittest.mock import patch
import clubops as app
import intent_outreach as outreach
import uid_messaging as channel
import test_uid_messaging as fixtures
SENDER = fixtures.SENDER


class OutreachTests(unittest.TestCase):
    def test_empty_manual_allowlist_does_not_allow_manual_messages_but_keeps_scoped_grants(self):
        self.settings['allowed_recipient_uids']=[];self.configure()
        with app.db() as c:c.execute("UPDATE people SET contact_basis='opt_in',contact_note='合成测试同意'")
        with patch('uid_transport.send') as transport:
            with self.assertRaisesRegex(ValueError,'授权发送范围'):
                channel.send_one(self.job['id'])
            transport.assert_not_called()
        with patch('uid_transport.send',wraps=self.accepted) as transport:
            outreach.tick()
            transport.assert_called_once()

    def test_replacement_account_preserves_prior_contact_exclusions_without_rewriting_receipts(self):
        channel.send_one(self.job['id'],operator_authorization=self.grant,transport=self.accepted)
        with app.db() as c:
            c.execute("UPDATE uid_message_attempts SET sender_uid='99999999999'")
            saved=tuple(c.execute('SELECT * FROM uid_message_attempts').fetchone())
            policy=outreach.read(c,outreach.POLICY_KEY);policy['prior_sender_uids']=['99999999999']
            policy['content']=c.execute('SELECT content FROM message_jobs WHERE id=?',(self.job['id'],)).fetchone()[0]
            outreach.write(c,outreach.POLICY_KEY,policy)
            self.assertIsNone(outreach.candidate(c,policy))
            grant={**self.grant,'policy_revision':policy['revision']}
            job,person,source=channel.snapshot(c,self.job['id'])
            settings,_=channel.config(authorized_recipient=person['external_id'])
            with self.assertRaisesRegex(ValueError,'换号前'):
                channel.authorized_outreach(c,grant,job,person,settings)
            self.assertEqual(saved,tuple(c.execute('SELECT * FROM uid_message_attempts').fetchone()))

    def test_non_domestic_source_blocks_saved_intent_and_old_grant(self):
        with app.db() as c:c.execute("UPDATE videos SET title='无畏契约亚服陪玩'")
        with patch('uid_transport.send') as transport:
            outreach.tick();transport.assert_not_called()
            with self.assertRaisesRegex(ValueError,'非国服'):
                channel.send_one(self.job['id'],operator_authorization=self.grant)
            transport.assert_not_called()

    def test_saved_buyer_on_mobile_source_never_creates_an_outreach_job(self):
        with app.db() as c:
            c.execute("UPDATE videos SET title='无畏契约手游陪玩'")
            count=c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0]
        with patch('uid_transport.send') as transport:
            outreach.tick();transport.assert_not_called()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0],count)

    def test_existing_grant_rechecks_scope_before_transport(self):
        with app.db() as c:c.execute("UPDATE videos SET title='手瓦陪玩'")
        with patch('uid_transport.send') as transport:
            with self.assertRaisesRegex(ValueError,'手游'):
                channel.send_one(self.job['id'],operator_authorization=self.grant)
            transport.assert_not_called()

    def test_scope_change_during_preparation_blocks_submission(self):
        submitted=[]
        def prepare(config,receiver,message,client,before):
            with app.db() as c:c.execute("UPDATE videos SET title='手瓦陪玩'")
            try:before()
            except ValueError as exc:
                self.assertIn('手游',str(exc))
                return dict(status='failed',phase='prepare_send',detail='scope changed')
            submitted.append(True)
            return dict(status='accepted',phase='send',server_message_id='12345')
        channel.send_one(self.job['id'],operator_authorization=self.grant,transport=prepare)
        self.assertEqual(submitted,[])

    configure = fixtures.QueueTests.configure
    draft = fixtures.QueueTests.draft
    accepted = fixtures.QueueTests.accepted
    outreach_grant = fixtures.QueueTests.outreach_grant

    def setUp(self):
        fixtures.QueueTests.setUp(self)
        self.grant = self.outreach_grant()
        with app.db() as c:
            c.execute("UPDATE sources SET kind='browser'")
        outreach.STOP.clear()
        outreach.authorize('点陪🥣看我主业', '合成测试操作授权', SENDER)
        self.analysis = patch('monitoring.observation_analysis', return_value=dict(category='buyer', analysis_method='model'))
        self.analysis.start()
        self.addCleanup(self.analysis.stop)

    def test_new_intent_sends_once_and_persists_exact_text_and_grant(self):
        with patch('uid_transport.send', wraps=self.accepted) as transport:
            outreach.tick()
            outreach.tick()
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args[2], '点陪🥣看我主业')
        self.assertEqual(outreach.state()['counts'], {'accepted': 1})
        with app.db() as c:
            self.assertEqual(c.execute('SELECT contact_basis FROM people').fetchone()[0], '')
            self.assertIn('policy_revision', c.execute('SELECT evidence FROM uid_message_attempts').fetchone()[0])

    def test_disabled_or_do_not_contact_never_sends(self):
        with patch('uid_transport.send') as transport:
            outreach.control(False)
            outreach.tick()
            outreach.control(True)
            with app.db() as c:
                c.execute('UPDATE people SET do_not_contact=1')
            outreach.tick()
            transport.assert_not_called()

    def test_rule_only_buyer_never_sends(self):
        with patch('monitoring.observation_analysis',return_value=dict(category='buyer',analysis_method='rules')),patch('uid_transport.send') as transport:
            outreach.tick()
            transport.assert_not_called()

    def test_live_intent_uses_its_own_evidence_and_same_user_dedupe(self):
        import json
        import live_monitor
        config = dict(live_monitor.DEFAULTS, room_url='https://live.douyin.com/12345')
        with app.db() as c:
            sid = c.execute('INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                ('synthetic-live', config['room_url'], '12345', json.dumps(config), 'connecting', '', app.now(), app.now())).lastrowid
        live_monitor.receive(sid, {'type':'message','record':dict(room_id='12345',uid=fixtures.RECEIVER,message_id='123456789',nickname='合成',text='无畏契约找陪练，预算100元',published_at=app.now())})
        with app.db() as c:
            # A human-confirmed buyer is eligible; a keyword-only live record is not.
            c.execute("UPDATE live_messages SET analysis_method='human'")
        self.analysis.stop()
        with patch('monitoring.observation_analysis', return_value=dict(category='noise', analysis_method='rules')), patch('uid_transport.send', wraps=self.accepted) as transport:
            outreach.tick()
            outreach.tick()
        transport.assert_called_once()
        with app.db() as c:
            evidence = json.loads(c.execute('SELECT evidence FROM uid_message_attempts').fetchone()[0])
        self.assertIn('live_id', evidence['operator_authorization'])
        self.assertNotIn('comment_id', evidence['operator_authorization'])

    def test_transport_uncertainty_pauses_and_does_not_repeat(self):
        with patch('uid_transport.send', return_value=dict(status='unknown', phase='send')) as transport:
            outreach.tick()
            outreach.tick()
        transport.assert_called_once()
        self.assertEqual(outreach.state()['status'], 'attention')

    def test_rejection_retries_when_due_but_never_for_known_mutual_follow_limit(self):
        from datetime import datetime,timedelta,timezone
        def reject(config,receiver,message,client,before):
            before();return dict(status='failed',phase='send',http_status=200,check_code='2')
        outreach.authorize_retries('合成：明确拒绝后重试')
        with patch('uid_transport.send',side_effect=reject) as transport:
            outreach.tick();outreach.tick()
            self.assertEqual(transport.call_count,1)
            with app.db() as c:
                c.execute('UPDATE uid_message_attempts SET updated_at=?',((datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat(),))
            outreach.tick()
            self.assertEqual(transport.call_count,2)
            import json
            with app.db() as c:
                e=json.loads(c.execute('SELECT evidence FROM uid_message_attempts').fetchone()[0]);e['platform_reason_code']='7173'
                c.execute('UPDATE uid_message_attempts SET evidence=?,updated_at=?',(json.dumps(e),(datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat()))
            outreach.tick()
            self.assertEqual(transport.call_count,2)

    def test_operator_turns_off_while_preparing_prevents_submission(self):
        def changed(config, receiver, message, client, before):
            outreach.control(False)
            before()
            self.fail('disabled policy must not submit')
        with patch('uid_transport.send', side_effect=changed):
            outreach.tick()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
