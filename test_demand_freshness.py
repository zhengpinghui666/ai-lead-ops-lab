"""Demand expiry boundaries and dispatch races; no external requests."""
import json
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch, Mock

import clubops as app
import demand_freshness as freshness
import intent_outreach as outreach
import uid_messaging as channel
import test_uid_messaging as fixtures
SENDER, RECEIVER = fixtures.SENDER, fixtures.RECEIVER


class TimeWindowTests(unittest.TestCase):
    def test_exact_day_timezone_and_unverifiable_times(self):
        now = datetime(2026, 9, 13, 13, 0, tzinfo=timezone.utc)
        for age, expected in ((0, 'current'), (86399.999, 'current'), (86400, 'current'), (86400.001, 'expired'), (-.001, 'future')):
            stamp = (now - timedelta(seconds=age)).isoformat()
            self.assertEqual(freshness.assess(stamp, now=now)['status'], expected)
        self.assertTrue(freshness.assess('2026-09-13T21:00:00+08:00', now=now)['eligible'])
        self.assertTrue(freshness.assess('2026-09-13T13:00:00Z', now=now)['eligible'])
        for stamp in (None, '', '2026-09-13', '2026-09-13T13:00:00', 'bad', 1789304400, True):
            result = freshness.assess(stamp, now=now)
            self.assertEqual(result['status'], 'unknown')
            self.assertFalse(result['eligible'])


class OutreachWindowTests(unittest.TestCase):
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
        outreach.authorize('点陪🥣看我主业', '合成测试', SENDER)
        analysis = patch('monitoring.observation_analysis', return_value=dict(category='buyer', analysis_method='model'))
        analysis.start()
        self.addCleanup(analysis.stop)

    def timestamp(self, age):
        return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()

    def change_time(self, value):
        with app.db() as c:
            c.execute('UPDATE comments SET published_at=?,discovered_at=?', (value, app.now()))

    def test_freshly_collected_old_or_unknown_comment_is_skipped_without_job_or_history_changes(self):
        for value in (self.timestamp(86401), self.timestamp(-60), None, 'invalid'):
            self.change_time(value)
            with patch('uid_transport.send') as send:
                outreach.tick()
                send.assert_not_called()
            with app.db() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0], 1)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM uid_message_attempts').fetchone()[0], 0)
                row = c.execute('SELECT raw_text,published_at FROM comments').fetchone()
                self.assertEqual(tuple(row), ('找陪玩', value))
            self.assertEqual(outreach.state()['status'], 'waiting')

    def test_missed_comment_within_day_can_still_be_selected(self):
        self.change_time(self.timestamp(23 * 3600))
        with patch('uid_transport.send', wraps=self.accepted) as send:
            outreach.tick()
            send.assert_called_once()
        with app.db() as c:
            evidence = json.loads(c.execute('SELECT evidence FROM uid_message_attempts').fetchone()[0])
            self.assertEqual(evidence['demand_time_check']['time_basis'], 'published_at')
            self.assertTrue(evidence['demand_time_check']['eligible'])

    def test_old_explicit_grant_cannot_bypass_selection_window(self):
        self.change_time(self.timestamp(86401))
        with patch('uid_transport.send') as send:
            with self.assertRaises(freshness.FreshnessError):
                channel.send_one(self.job['id'], operator_authorization=self.grant)
            send.assert_not_called()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM uid_message_attempts').fetchone()[0], 0)

    def test_expiry_after_candidate_selection_is_not_global_channel_failure(self):
        original = channel.send_one
        def expire(*args, **kwargs):
            self.change_time(self.timestamp(86401))
            return original(*args, **kwargs)
        with patch.object(channel, 'send_one', side_effect=expire), patch('uid_transport.send') as send:
            outreach.tick()
            send.assert_not_called()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT status FROM message_jobs ORDER BY id DESC LIMIT 1').fetchone()[0], 'blocked')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM uid_message_attempts').fetchone()[0], 0)
        self.assertEqual(outreach.state()['status'], 'waiting')

    def test_expiry_during_preparation_blocks_send_without_marking_platform_rejection(self):
        submitted = []
        def prepared(config, receiver, text, client, before):
            self.change_time(self.timestamp(86401))
            try:
                before()
            except freshness.FreshnessError:
                return dict(status='failed', phase='prepare_send')
            submitted.append(True)
            return dict(status='accepted', phase='send', server_message_id='synthetic')
        with patch('uid_transport.send', side_effect=prepared):
            outreach.tick()
        self.assertEqual(submitted, [])
        self.assertEqual(outreach.state()['status'], 'waiting')
        with app.db() as c:
            attempt = c.execute('SELECT * FROM uid_message_attempts').fetchone()
            evidence = json.loads(attempt['evidence'])
            self.assertEqual((attempt['status'], attempt['phase']), ('failed', 'prepare_send'))
            self.assertFalse(evidence['submission_reserved'])
            self.assertEqual(evidence['demand_freshness']['status'], 'expired')
            self.assertIn('超过一天', attempt['detail'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_live_uses_publication_instead_of_recent_observation(self):
        import live_monitor
        config = dict(live_monitor.DEFAULTS, room_url='https://live.douyin.com/12345')
        with app.db() as c:
            sid = c.execute('INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                ('freshness-live', config['room_url'], '12345', json.dumps(config), 'connecting', '', app.now(), app.now())).lastrowid
        live_monitor.receive(sid, dict(type='message', record=dict(room_id='12345', uid=RECEIVER, message_id='123456789',
            nickname='合成', text='无畏契约找陪练，预算100元', published_at=self.timestamp(86401))))
        with app.db() as c:
            c.execute("UPDATE live_messages SET analysis_method='human'")
        with patch('monitoring.observation_analysis', return_value=dict(category='noise', analysis_method='rules')), patch('uid_transport.send', wraps=self.accepted) as send:
            outreach.tick()
            send.assert_not_called()
            with app.db() as c:
                c.execute('UPDATE live_messages SET published_at=?', (self.timestamp(22 * 3600),))
            outreach.tick()
            send.assert_called_once()

    def test_retry_and_preparation_resume_recheck_original_time(self):
        def failed(config, receiver, text, client, before):
            return dict(status='failed', phase='identity')
        channel.send_one(self.job['id'], transport=failed, operator_authorization=self.grant)
        self.change_time(self.timestamp(86401))
        with self.assertRaises(freshness.FreshnessError):
            channel.send_one(self.job['id'], transport=Mock(), operator_authorization=self.grant, resume_note='合成继续')
        # Also protect the retry-candidate path for a definite past rejection.
        with app.db() as c:
            c.execute("UPDATE uid_message_attempts SET phase='send',updated_at=?,evidence=?", (self.timestamp(1200), json.dumps(dict(
                http_status=200, submission_reserved=True, operator_authorization=self.grant))))
            c.execute("UPDATE message_jobs SET request_id='intent-outreach-v1-synthetic'")
        outreach.authorize_retries('合成重试授权')
        with patch('uid_transport.send') as send:
            outreach.tick()
            send.assert_not_called()

    def test_live_missing_publication_uses_original_reception_only(self):
        import live_monitor
        config = dict(live_monitor.DEFAULTS, room_url='https://live.douyin.com/12345')
        with app.db() as c:
            sid = c.execute('INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                ('live-received', config['room_url'], '12345', json.dumps(config), 'connecting', '', app.now(), app.now())).lastrowid
        live_monitor.receive(sid, dict(type='message', record=dict(room_id='12345', uid=RECEIVER, message_id='123456789',
            nickname='合成', text='无畏契约找陪练，预算100元', published_at=None)))
        with app.db() as c:
            rid = c.execute('SELECT id FROM live_messages').fetchone()[0]
            result = freshness.record(c, 'live', rid)
            self.assertTrue(result['eligible'])
            self.assertEqual(result['time_basis'], 'received_at')
            self.assertNotIn('published_at', result)
            c.execute('UPDATE live_messages SET observed_at=?', (self.timestamp(86401),))
            self.assertFalse(freshness.record(c, 'live', rid)['eligible'])
            self.assertIsNone(c.execute('SELECT published_at FROM live_messages').fetchone()[0])
            # An explicit old publication still wins over a newly received copy.
            c.execute('UPDATE live_messages SET observed_at=?,published_at=?', (app.now(), self.timestamp(86401)))
            result = freshness.record(c, 'live', rid)
            self.assertFalse(result['eligible'])
            self.assertEqual(result['time_basis'], 'published_at')

    def test_direct_consented_reply_without_proactive_source_grant_is_unchanged(self):
        with app.db() as c:
            c.execute("UPDATE people SET contact_basis='inbound',contact_note='合成主动咨询'")
        self.change_time(self.timestamp(86401))
        result = channel.send_one(self.job['id'], transport=self.accepted)
        self.assertEqual(result['status'], 'accepted')


if __name__ == '__main__':
    unittest.main()
