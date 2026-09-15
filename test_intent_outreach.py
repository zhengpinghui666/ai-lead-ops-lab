"""Synthetic, offline automatic outreach integration checks."""
import unittest
import json
from unittest.mock import patch
import clubops as app
import intent_outreach as outreach
import uid_messaging as channel
import test_uid_messaging as fixtures
SENDER = fixtures.SENDER


class OutreachTests(unittest.TestCase):
    def test_greeting_uses_only_public_gender_and_unknown_is_neutral(self):
        outreach.replace_greeting('合成：替换模板，明确内容拒绝或风控时回退')
        from datetime import datetime,timedelta,timezone
        with app.db() as c:
            policy=outreach.read(c,outreach.POLICY_KEY)
            for value,expected in [(0,'你好呀'),(1,'你好小哥哥'),(2,'你好小姐姐')]:
                c.execute('UPDATE people SET profile_gender=?,profile_gender_observed_at=?',(value,app.now()))
                self.assertTrue(outreach.rendered_content(c,policy,fixtures.RECEIVER).startswith(expected+'，'))
            c.execute('UPDATE people SET profile_gender_observed_at=?',((datetime.now(timezone.utc)-timedelta(days=31)).isoformat(),))
            self.assertTrue(outreach.rendered_content(c,policy,fixtures.RECEIVER).startswith('你好呀，'))
        with patch('uid_transport.send',wraps=self.accepted) as transport:
            outreach.tick();outreach.tick()
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args[2],'你好呀，想点个陪陪吗？感兴趣可以看看我主业～')

    def test_template_change_preserves_account_exclusions_and_pause(self):
        outreach.control(False)
        with app.db() as c:
            policy=outreach.read(c,outreach.POLICY_KEY)
            policy['prior_sender_uids']=['99999999999']
            outreach.write(c,outreach.POLICY_KEY,policy)
        outreach.replace_greeting('合成：改模板并回退')
        with app.db() as c:
            current=outreach.read(c,outreach.POLICY_KEY)
        self.assertFalse(current['enabled'])
        self.assertEqual(current['prior_sender_uids'],policy['prior_sender_uids'])
        self.assertEqual(current['sender_uid'],policy['sender_uid'])
        with self.assertRaises(ValueError):outreach.replace_greeting('不覆盖已更改的模板')

    def test_explicit_content_rejection_rolls_back_without_replaying_recipient(self):
        import json
        outreach.authorize_retries('合成：旧授权允许有限重试')
        outreach.replace_greeting('合成：新文案被拒绝时回退旧版')
        def reject(config,receiver,message,client,before):
            before()
            return dict(status='failed',phase='send',http_status=200,check_code='2',platform_message='消息包含敏感词，未发送')
        with patch('uid_transport.send',side_effect=reject) as transport:
            outreach.tick();outreach.tick()
        transport.assert_called_once()
        self.assertEqual(outreach.state()['content'],outreach.LEGACY_CONTENT)
        self.assertEqual(outreach.state()['content_fallback']['reason'],'content_rejected')
        self.assertEqual(outreach.state()['status'],'waiting')
        with app.db() as c:
            job=c.execute('SELECT content FROM message_jobs WHERE request_id LIKE ?',('intent-outreach-v1-%',)).fetchone()
            self.assertIn('你好呀',job['content'])
            evidence=json.loads(c.execute('SELECT evidence FROM uid_message_attempts').fetchone()[0])
            self.assertEqual(evidence['platform_message'],'消息包含敏感词，未发送')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)

    def test_risk_rolls_back_and_stops_instead_of_trying_old_copy(self):
        outreach.replace_greeting('合成：风控回退')
        def reject(config,receiver,message,client,before):
            before();return dict(status='failed',phase='send',http_status=200,platform_message='账号存在风险，私信功能已被限制')
        with patch('uid_transport.send',side_effect=reject) as transport:
            outreach.tick();outreach.tick()
        transport.assert_called_once()
        self.assertEqual(outreach.state()['content'],outreach.LEGACY_CONTENT)
        self.assertEqual(outreach.state()['status'],'attention')

    def test_generic_failure_privacy_and_uncertainty_do_not_claim_content_rejection(self):
        base=dict(phase='send',http_status=200,submission_reserved=True)
        for status,evidence in [
            ('failed',base),
            ('failed',dict(base,platform_reason_code='7173',platform_message='对方只允许关注的人发消息')),
            ('failed',dict(base,http_status=403)),
            ('unknown',dict(base,platform_message='敏感词')),
            ('accepted',dict(base,server_message_id='123',platform_message='敏感词')),
            ('failed',dict(base,phase='identity',submission_reserved=False,platform_message='风险'))]:
            with self.subTest(status=status,evidence=evidence):
                self.assertIsNone(outreach.rejection_kind(dict(status=status,evidence=evidence)))
        self.assertEqual(outreach.rejection_kind(dict(status='failed',evidence=dict(base,http_status=429))),'rate_limit')

    def test_persisted_rejection_is_reconciled_before_next_dispatch(self):
        outreach.replace_greeting('合成：进程中断后继续回退')
        def reject(config,receiver,message,client,before):
            before();return dict(status='failed',phase='send',http_status=200,platform_message='内容违反规范')
        with patch.object(outreach,'rollback_rejected_template',return_value=None),patch('uid_transport.send',side_effect=reject):
            outreach.tick()
        self.assertEqual(outreach.state()['content_fallback']['status'],'armed')
        with patch('uid_transport.send') as transport:outreach.tick();transport.assert_not_called()
        self.assertEqual(outreach.state()['content_fallback']['status'],'rolled_back')

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
        self.analysis = patch('monitoring.observation_analysis', return_value=dict(category='buyer', game=app.TARGET_GAME, analysis_method='model'))
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

    def test_every_nonbuyer_class_is_blocked_and_old_draft_cancelled(self):
        for category in ('seller','recruit','club','social','noise','uncertain','pending',''):
            with self.subTest(category=category),patch('monitoring.observation_analysis',return_value=dict(
                    category=category,game=app.TARGET_GAME,analysis_method='model')),patch('uid_transport.send') as transport:
                self.assertFalse(channel.eligible_demand_copy(dict(category=category,game=app.TARGET_GAME,analysis_method='model'),'点陪'))
                outreach.tick()
                transport.assert_not_called()
        with app.db() as c:
            policy=outreach.read(c,outreach.POLICY_KEY)
            row=outreach.candidate(c,policy)
        job=outreach.prepare_draft(row,policy,'点陪🥣看我主业')
        with patch('monitoring.observation_analysis',return_value=dict(category='seller',game=app.TARGET_GAME,analysis_method='model')):
            with app.db() as c:
                self.assertEqual(outreach.cancel_nonbuyer_jobs(c,outreach.read(c,outreach.POLICY_KEY)),[job['id']])
                self.assertEqual(c.execute('SELECT status FROM message_jobs WHERE id=?',(job['id'],)).fetchone()[0],'cancelled')
                self.assertEqual(c.execute('SELECT COUNT(*) FROM uid_message_attempts').fetchone()[0],0)

    def test_reclassified_rejection_is_cancelled_without_changing_platform_receipt(self):
        from datetime import datetime,timedelta,timezone
        def reject(config,receiver,message,client,before):
            before();return dict(status='failed',phase='send',http_status=200,check_code='2')
        outreach.authorize_retries('合成：明确拒绝后重试')
        with patch('uid_transport.send',side_effect=reject):outreach.tick()
        with app.db() as c:
            c.execute('UPDATE uid_message_attempts SET updated_at=?',((datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat(),))
            receipt=dict(c.execute('SELECT * FROM uid_message_attempts').fetchone())
        with patch('monitoring.observation_analysis',return_value=dict(category='club',game=app.TARGET_GAME,analysis_method='human')),patch('uid_transport.send') as transport:
            outreach.tick();transport.assert_not_called()
        with app.db() as c:
            self.assertEqual(dict(c.execute('SELECT * FROM uid_message_attempts').fetchone()),receipt)
            self.assertEqual(c.execute('SELECT status FROM message_jobs WHERE id=?',(receipt['job_id'],)).fetchone()[0],'cancelled')
            self.assertIsNone(outreach.retry_candidate(c,outreach.read(c,outreach.POLICY_KEY)))

    def test_classification_change_during_preparation_never_submits(self):
        def change(config,receiver,message,client,before):
            with patch('monitoring.observation_analysis',return_value=dict(category='seller',game=app.TARGET_GAME,analysis_method='model')):
                before()
            self.fail('A nonbuyer must never reach submission')
        with patch('uid_transport.send',side_effect=change):outreach.tick()
        self.assertEqual(outreach.state()['status'],'waiting')
        with app.db() as c:
            attempt=c.execute('SELECT * FROM uid_message_attempts').fetchone()
            self.assertEqual((attempt['status'],attempt['phase']),('failed','prepare_send'))
            evidence=json.loads(attempt['evidence'])
            self.assertFalse(evidence['submission_reserved'])
            self.assertTrue(evidence['eligibility_blocked'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)

    def test_unknown_game_mentions_valorant_and_preserves_unknown_classification(self):
        outreach.replace_greeting('合成：游戏待确认时明确介绍瓦陪陪')
        with patch('monitoring.observation_analysis', return_value=dict(category='buyer', game='', analysis_method='model')), patch('uid_transport.send', wraps=self.accepted) as transport:
            outreach.tick();outreach.tick()
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args[2], '你好呀，想点个瓦陪陪吗？感兴趣可以看看我主业～')

    def test_other_game_is_excluded_and_unknown_does_not_use_legacy_copy(self):
        for game in ['', '王者荣耀', '三角洲行动']:
            with self.subTest(game=game), patch('monitoring.observation_analysis', return_value=dict(category='buyer', game=game, analysis_method='model')), patch('uid_transport.send') as transport:
                outreach.tick();transport.assert_not_called()
        outreach.replace_greeting('合成：游戏待确认时介绍瓦陪陪')
        with patch('monitoring.observation_analysis', return_value=dict(category='buyer', game='王者荣耀', analysis_method='model')), patch('uid_transport.send') as transport:
            outreach.tick();transport.assert_not_called()

    def test_channel_contention_keeps_waiting_and_reuses_draft_once(self):
        channel.GUARD.acquire()
        try:
            with patch('uid_transport.send') as transport:outreach.tick();transport.assert_not_called()
            self.assertEqual(outreach.state()['status'],'waiting')
            with app.db() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM uid_message_attempts').fetchone()[0],0)
                first=c.execute("SELECT id FROM message_jobs WHERE request_id LIKE 'intent-outreach-v1-%'").fetchone()[0]
        finally:channel.GUARD.release()
        with patch('uid_transport.send', wraps=self.accepted) as transport:outreach.tick();outreach.tick()
        transport.assert_called_once()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT job_id FROM uid_message_attempts').fetchone()[0],first)

    def test_final_gate_requires_unknown_game_copy_and_excludes_other_games(self):
        for game,content,allowed in [('', '你好呀，想点个瓦陪陪吗？感兴趣可以看看我主业～', True),
                ('','你好呀，想点个陪陪吗？感兴趣可以看看我主业～',False),
                (app.TARGET_GAME,'你好呀，想点个陪陪吗？感兴趣可以看看我主业～',True),
                ('王者荣耀','你好呀，想点个瓦陪陪吗？感兴趣可以看看我主业～',False)]:
            self.assertEqual(channel.eligible_demand_copy(dict(category='buyer',game=game,analysis_method='model'),content),allowed)

    def test_unattempted_draft_can_adopt_game_specific_copy_without_duplicate_job(self):
        outreach.replace_greeting('合成：待确认游戏说明瓦陪陪')
        channel.GUARD.acquire()
        try:outreach.tick()
        finally:channel.GUARD.release()
        with app.db() as c:
            job_id=c.execute("SELECT id FROM message_jobs WHERE request_id LIKE 'intent-outreach-v1-%'").fetchone()[0]
        with patch('monitoring.observation_analysis',return_value=dict(category='buyer',game='',analysis_method='model')),patch('uid_transport.send',wraps=self.accepted) as transport:
            outreach.tick();outreach.tick()
        transport.assert_called_once()
        self.assertIn('瓦陪陪',transport.call_args.args[2])
        with app.db() as c:self.assertEqual(c.execute('SELECT job_id FROM uid_message_attempts').fetchone()[0],job_id)

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
