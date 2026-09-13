"""Offline evidence only: no real credentials, platform calls or test messages."""
import json
import http.client
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer

import clubops as app
import uid_messaging as channel
import uid_protocol as wire
import uid_transport
import server

SENDER, RECEIVER = '10000000000000001', '10000000000000002'
CID = '0:1:' + SENDER + ':' + RECEIVER


def envelope(command, info, sender=SENDER, code=0):
    return wire.field(1, command) + wire.field(2, 123) + wire.field(3, code) + wire.field(4, 'OK') + wire.field(6, wire.field(command, info)) + wire.field(13, int(sender))


def created():
    return envelope(609, wire.field(1, wire.field(1, CID) + wire.field(2, 9223372036854775000)))


def sent(client_id, status=0):
    return envelope(100, wire.field(1, 9223372036854775001) + wire.field(4, client_id)
                    + wire.field(2, json.dumps({'status_code': status})))


class Provider:
    transport = 'http'

    def prepare(self, operation, body, metadata):
        return {'payload': b'' if operation == 'identity' else wire.field(1, metadata['command']) + wire.field(2, 123) + wire.field(8, body),
                'headers': {}, 'query': {}}

    def ticket(self, *args):
        return 'synthetic-ticket'


class ProtocolTests(unittest.TestCase):
    def test_structured_rejection_preserves_exact_reason_without_envelope_secrets(self):
        explanation=dict(status_code=7173,status_msg=dict(msg_type=1,msg_content=dict(tips='对方设置了仅互关可发消息',template=[])),token='private-token')
        raw=envelope(100,wire.field(4,'client')+wire.field(3,3)+wire.field(5,2)+wire.field(6,json.dumps(explanation,ensure_ascii=False)))
        result=wire.result(raw,100,SENDER,RECEIVER,'client')
        self.assertEqual(result['platform_reason_code'],'7173')
        self.assertEqual(result['platform_message'],'对方设置了仅互关可发消息')
        self.assertNotIn('private-token',json.dumps(result))
        self.assertEqual(result['status'],'failed')

    def test_create_rejection_retains_business_code_without_sending_or_private_data(self):
        raw = envelope(609, b'', code=4) + wire.field(10, 'synthetic-private-response')
        calls = []
        def exchange(op, prepared):
            calls.append(op)
            return (200, 'application/json', json.dumps({'user': {'uid': SENDER}}).encode()) if op == 'identity' else (200, 'application/x-protobuf', raw)
        result = uid_transport.send({'sender_uid': SENDER}, RECEIVER, 'synthetic', 'client',
                                    lambda: self.fail('must not submit'), provider=Provider(), exchange=exchange)
        self.assertEqual(calls, ['identity', 'create'])
        self.assertEqual((result['status'], result['phase'], result['platform_code']), ('failed', 'create', '4'))
        self.assertEqual(result['response_bytes'], len(raw))
        self.assertEqual(len(result['response_sha256']), 64)
        self.assertNotIn('synthetic-private', json.dumps(result))

    def test_response_sequence_and_private_inbox_must_match(self):
        self.assertEqual(wire.result(created(), 609, SENDER, RECEIVER, sequence=124)['status'], 'unknown')
        self.assertEqual(wire.result(sent('client'), 100, SENDER, RECEIVER, 'client', sequence=124)['status'], 'unknown')
        for raw in [created().replace(wire.field(2, 123), b'', 1), created() + wire.field(2, 123)]:
            self.assertEqual(wire.result(raw, 609, SENDER, RECEIVER, sequence=123)['status'], 'unknown')
        wrong_inbox = envelope(609, wire.field(1, wire.field(1, CID) + wire.field(2, 1234567) + wire.field(9, 1)))
        self.assertEqual(wire.result(wrong_inbox, 609, SENDER, RECEIVER)['status'], 'unknown')

    def test_http2_pseudo_headers_and_invalid_names_fail_before_connecting(self):
        for name in (':authority', ':method', ':path', ':scheme', 'bad header', '', '中文'):
            with self.subTest(name=name), patch('http.client.HTTPSConnection') as connection:
                with self.assertRaises(ValueError):
                    uid_transport.request('identity', dict(headers={name: 'synthetic'}, payload=b'', query={}))
                connection.assert_not_called()

    def test_header_encoding_failure_does_not_open_connection(self):
        with patch('http.client.HTTPSConnection') as connection:
            with self.assertRaises(ValueError):
                uid_transport.request('identity', dict(headers={'X-Test': '合成'}, payload=b'', query={}))
            connection.assert_not_called()

    def test_wire_unsigned_roundtrip_and_truncation(self):
        value = 9223372036854775001
        self.assertEqual(wire.one(wire.decode(wire.field(1, value)), 1, 0), value)
        for raw in (b'\x00', b'\x08\x80', b'\x0a\x05abc', b'\x08' + b'\xff' * 11, b'\x0b'):
            with self.assertRaises(ValueError):
                wire.decode(raw)

    def test_numeric_identifiers_are_strings(self):
        for value in (123, '', '0123', 'MS4wExample', '１２３', str(2**63)):
            with self.assertRaises(ValueError):
                wire.numeric_uid(value)

    def test_creation_uses_current_web_integer_type_and_repeated_uids(self):
        nested = wire.decode(wire.one(wire.decode(wire.create_body('123', '456')), 609, 2))
        self.assertEqual(wire.one(nested, 1, 0), 1)
        self.assertEqual(nested[2], [(0, 456), (0, 123)])

    def test_conversation_requires_sender_and_both_participants(self):
        value = wire.result(created(), 609, SENDER, RECEIVER)
        self.assertEqual(value['conversation_short_id'], '9223372036854775000')
        self.assertEqual(wire.result(created(), 609, '999', RECEIVER)['status'], 'unknown')
        self.assertEqual(wire.result(created(), 609, SENDER, '999')['status'], 'unknown')

    def test_receipt_requires_message_id_and_correlation(self):
        self.assertEqual(wire.result(sent('client'), 100, SENDER, RECEIVER, 'client')['status'], 'accepted')
        for raw in (b'', b'OK', sent('other'), envelope(100, wire.field(4, 'client')),
                    sent('client') + wire.field(4, 'OK')):
            self.assertEqual(wire.result(raw, 100, SENDER, RECEIVER, 'client')['status'], 'unknown')

    def test_business_errors_never_whitelisted_as_success(self):
        for code in (1, 8101, 7174):
            self.assertEqual(wire.result(sent('client', code), 100, SENDER, RECEIVER, 'client')['status'], 'failed')

    def test_current_web_plain_check_message_is_not_parsed_as_json_or_exposed(self):
        base = wire.field(1, 9223372036854775001) + wire.field(4, 'client')
        for extra in (b'', wire.field(2, ''), wire.field(2, '{"status_code":0}')):
            raw = envelope(100, base + extra + wire.field(6, 'synthetic-private-check-message'))
            result = wire.result(raw, 100, SENDER, RECEIVER, 'client')
            self.assertEqual(result['status'], 'accepted')
            self.assertNotIn('synthetic-private', json.dumps(result))

    def test_send_status_and_check_code_reject_even_with_unstructured_extra(self):
        base = wire.field(1, 9223372036854775001) + wire.field(4, 'client')
        for number in (3, 5):
            raw = envelope(100, base + wire.field(number, 1) + wire.field(2, 'unstructured'))
            self.assertEqual(wire.result(raw, 100, SENDER, RECEIVER, 'client')['status'], 'failed')

    def test_ambiguous_extra_info_or_check_message_cannot_confirm_acceptance(self):
        base = wire.field(1, 9223372036854775001) + wire.field(4, 'client')
        for extra in (wire.field(2, 'unstructured'), wire.field(2, '[]'),
                      wire.field(2, '{}') + wire.field(2, '{}'),
                      wire.field(6, 'a') + wire.field(6, 'b'), wire.field(6, 1)):
            self.assertEqual(wire.result(envelope(100, base + extra), 100, SENDER, RECEIVER, 'client')['status'], 'unknown')

    def test_transport_flow_and_changed_login(self):
        calls = []

        def exchange(op, prepared):
            calls.append(op)
            if op == 'identity':
                return 200, 'application/json', json.dumps({'user': {'uid': SENDER}}).encode()
            return 200, 'application/x-protobuf', created() if op == 'create' else sent('client')

        result = uid_transport.send({'sender_uid': SENDER}, RECEIVER, 'synthetic', 'client',
                                    lambda: calls.append('recheck'), provider=Provider(), exchange=exchange)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(calls, ['identity', 'create', 'recheck', 'send'])
        calls.clear()
        result = uid_transport.send({'sender_uid': '999'}, RECEIVER, 'synthetic', 'client',
                                    lambda: None, provider=Provider(), exchange=exchange)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(calls, ['identity'])

    def test_send_timeout_is_unknown_and_not_retried(self):
        calls = []

        def exchange(op, prepared):
            calls.append(op)
            if op == 'identity':
                return 200, 'application/json', json.dumps({'user': {'uid': SENDER}}).encode()
            if op == 'create':
                return 200, 'application/x-protobuf', created()
            raise TimeoutError('synthetic-secret-must-not-escape')

        result = uid_transport.send({'sender_uid': SENDER}, RECEIVER, 'synthetic', 'client',
                                    lambda: None, provider=Provider(), exchange=exchange)
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(calls.count('send'), 1)

    def test_provider_cannot_change_message_body(self):
        class Wrong(Provider):
            def prepare(self, op, body, metadata):
                return super().prepare(op, body + (wire.field(99, 'changed') if op == 'send' else b''), metadata)
        calls = []

        def exchange(op, prepared):
            calls.append(op)
            return (200, 'application/json', json.dumps({'user': {'uid': SENDER}}).encode()) if op == 'identity' else (200, 'application/x-protobuf', created())

        result = uid_transport.send({'sender_uid': SENDER}, RECEIVER, 'synthetic', 'client',
                                    lambda: None, provider=Wrong(), exchange=exchange)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('send', calls)

    def test_destination_is_fixed_and_headers_reject_injection(self):
        with patch('http.client.HTTPSConnection') as connection:
            for prepared in ({'headers': {'Cookie': 'x\r\ny'}}, {'headers': {'Host': 'elsewhere'}}, {'query': {'a': '\n'}}):
                with self.assertRaises(ValueError):
                    uid_transport.request('send', prepared)
            connection.assert_not_called()


class TransportDiagnosticsTests(unittest.TestCase):
    def test_timeout_phase_is_precise_and_connection_is_closed_without_retry(self):
        for phase, method in [('connect', 'connect'), ('request', 'request'),
                              ('response_headers', 'getresponse'), ('response_body', 'read1')]:
            connection = Mock()
            response = Mock(status=200)
            connection.getresponse.return_value = response
            target = response if method == 'read1' else connection
            getattr(target, method).side_effect = TimeoutError('synthetic-cookie-do-not-expose')
            with self.subTest(phase=phase), patch('http.client.HTTPSConnection', return_value=connection):
                with self.assertRaises(uid_transport.TransportError) as raised:
                    uid_transport.request('identity', dict(headers={}, query={}, payload=b''))
                evidence = raised.exception.evidence
                self.assertEqual(evidence['transport_phase'], phase)
                self.assertEqual(evidence['transport_error'], 'timeout')
                self.assertNotIn('synthetic-cookie', str(raised.exception) + json.dumps(evidence))
                self.assertEqual('http_status' in evidence, phase == 'response_body')
                self.assertLessEqual(connection.request.call_count, 1)
                connection.close.assert_called_once()

    def test_oversized_body_keeps_only_status_and_bounded_count(self):
        connection = Mock()
        response = Mock(status=200)
        response.read1.side_effect = lambda n: b'x' * n
        connection.getresponse.return_value = response
        with patch('http.client.HTTPSConnection', return_value=connection):
            with self.assertRaises(uid_transport.TransportError) as raised:
                uid_transport.request('identity', dict(headers={}, query={}, payload=b''))
        self.assertEqual(raised.exception.evidence,
            dict(transport_phase='response_body', transport_error='response_exceeds_bound',
                 http_status=200, response_bytes=262145))
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_identity_returns_safe_transport_diagnostics_without_success(self):
        error = uid_transport.TransportError('timeout', 'response_headers')
        result = uid_transport.verify_identity({'sender_uid': SENDER}, provider=Provider(),
                                              exchange=Mock(side_effect=error))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['transport_phase'], 'response_headers')
        self.assertFalse(result['can_send'])
        injected = uid_transport.TransportError('synthetic-secret', 'synthetic-secret',
                                                http_status=True, response_bytes=-1)
        self.assertNotIn('synthetic-secret', json.dumps(injected.evidence) + str(injected))

    def test_send_transport_timeout_stays_unknown(self):
        calls = []
        def exchange(operation, prepared):
            calls.append(operation)
            if operation == 'identity':
                return 200, 'application/json', json.dumps({'user': {'uid': SENDER}}).encode()
            if operation == 'create':
                return 200, 'application/x-protobuf', created()
            raise uid_transport.TransportError('timeout', 'response_headers')
        result = uid_transport.send({'sender_uid': SENDER}, RECEIVER, 'synthetic', 'client',
                                    lambda: None, provider=Provider(), exchange=exchange)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['phase'], 'send')
        self.assertEqual(result['transport_phase'], 'response_headers')
        self.assertEqual(calls, ['identity', 'create', 'send'])


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        self.addCleanup(setattr, app, 'DATA_DIR', self.original)
        app.init()
        self.provider = app.DATA_DIR / 'synthetic-provider.py'
        self.provider.write_text('raise RuntimeError("provider must not execute during state checks")')
        self.settings = dict(enabled=True, sender_uid=SENDER, allowed_recipient_uids=[RECEIVER], provider_file=str(self.provider))
        self.configure()
        self.lead_id = channel.register_target({'uid': RECEIVER, 'nickname': '合成测试', 'contact_note': '模拟同意依据'})['lead_id']
        self.job = self.draft('first')

    def configure(self):
        (app.DATA_DIR / channel.CONFIG_FILE).write_text(json.dumps(self.settings), encoding='utf-8')

    def draft(self, request, content='纯本地模拟消息'):
        return app.mutate('draft', {'lead_id': self.lead_id, 'request_id': request, 'content': content})

    def accepted(self, config, receiver, message, client, before):
        before()
        return {'status': 'accepted', 'phase': 'send', 'server_message_id': '12345'}

    def outreach_grant(self):
        import hashlib
        with app.db() as c:
            person = c.execute('SELECT * FROM people').fetchone()
            c.execute("UPDATE people SET contact_basis='',contact_note=''")
            video = c.execute("INSERT INTO videos(source_id,external_id,title,created_at) VALUES(?,'synthetic','合成',?)", (person['source_id'], app.now())).lastrowid
            comment = c.execute("INSERT INTO comments(source_id,external_id,video_id,person_id,raw_text,published_at,discovered_at) VALUES(?,'synthetic',?,?,'找陪玩',?,?)", (person['source_id'], video, person['id'], app.now(), app.now())).lastrowid
        return dict(job_id=self.job['id'], sender_uid=SENDER, recipient_uid=RECEIVER,
                    content_sha256=hashlib.sha256(self.job['content'].encode()).hexdigest(),
                    comment_id=comment, comment_sha256=hashlib.sha256('找陪玩'.encode()).hexdigest(),
                    instruction='合成本地操作授权，不代表收件人同意', granted_at=app.now())

    def test_operator_grant_is_bound_audited_and_never_invents_consent(self):
        grant = self.outreach_grant()
        with patch('monitoring.observation_analysis', return_value=dict(category='buyer', analysis_method='model')):
            with self.assertRaises(ValueError):
                channel.send_one(self.job['id'], transport=self.accepted)
            transport = Mock(wraps=self.accepted)
            result = channel.send_one(self.job['id'], transport=transport, operator_authorization=grant)
            self.assertEqual(result['status'], 'accepted')
            self.assertEqual(result['evidence']['operator_authorization'], grant)
            channel.send_one(self.job['id'], transport=transport, operator_authorization=grant)
            transport.assert_called_once()
        with app.db() as c:
            person = c.execute('SELECT * FROM people').fetchone()
            self.assertEqual((person['contact_basis'], person['contact_note']), ('', ''))

    def test_operator_grant_rejects_mismatch_stale_intent_and_do_not_contact(self):
        grant = self.outreach_grant()
        transport = Mock(wraps=self.accepted)
        with patch('monitoring.observation_analysis', return_value=dict(category='buyer', analysis_method='model')):
            for key in ('job_id', 'sender_uid', 'recipient_uid', 'content_sha256', 'comment_id', 'comment_sha256'):
                wrong = {**grant, key: 999999 if key.endswith('_id') else 'mismatch'}
                with self.subTest(key=key), self.assertRaises(ValueError):
                    channel.send_one(self.job['id'], transport=transport, operator_authorization=wrong)
            with app.db() as c:
                c.execute('UPDATE people SET do_not_contact=1')
            with self.assertRaises(ValueError):
                channel.send_one(self.job['id'], transport=transport, operator_authorization=grant)
            with app.db() as c:
                c.execute('UPDATE people SET do_not_contact=0')
        with patch('monitoring.observation_analysis', return_value=dict(category='seller', analysis_method='human')):
            with self.assertRaises(ValueError):
                channel.send_one(self.job['id'], transport=transport, operator_authorization=grant)
        transport.assert_not_called()

    def test_operator_grant_checks_intent_again_before_submission(self):
        grant = self.outreach_grant()
        with patch('monitoring.observation_analysis', side_effect=[dict(category='buyer', analysis_method='model'), dict(category='seller', analysis_method='human')]):
            result = channel.send_one(self.job['id'], transport=self.accepted, operator_authorization=grant)
        self.assertEqual(result['status'], 'unknown')
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_preparation_failure_keeps_reason_and_never_claims_platform_rejection(self):
        result=channel.send_one(self.job['id'],transport=Mock(return_value=dict(status='failed',
            phase='identity',identity_reason='preparation_failed',detail='synthetic-private-exception')))
        self.assertEqual(result['detail'],'账号认证未通过，消息尚未提交')
        self.assertEqual(result['evidence']['identity_reason'],'preparation_failed')
        self.assertIs(result['evidence']['submission_reserved'],False)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)
            stored=dict(c.execute('SELECT * FROM uid_message_attempts').fetchone())
        self.assertNotIn('synthetic-private-exception',json.dumps(stored))
        self.assertNotIn('平台未接受',stored['detail'])

    def test_rejected_message_retry_is_delayed_bounded_and_preserves_history(self):
        from datetime import datetime,timedelta,timezone
        def reject(config, receiver, message, client, before):
            before()
            return dict(status='failed',phase='send',http_status=200,send_status='3',check_code='2')
        channel.send_one(self.job['id'],transport=reject)
        with self.assertRaises(ValueError):
            channel.send_one(self.job['id'],transport=reject,retry_note='用户授权重试')
        for _ in range(2):
            with app.db() as c:
                c.execute('UPDATE uid_message_attempts SET updated_at=?', ((datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat(),))
            channel.send_one(self.job['id'],transport=reject,retry_note='用户授权重试')
        with app.db() as c:
            row=c.execute('SELECT * FROM uid_message_attempts').fetchone()
            history=json.loads(row['evidence'])['delivery_history']
            self.assertEqual(len(history),2)
            self.assertEqual(len({row['client_message_id'],*[x['client_message_id'] for x in history]}),3)
        with self.assertRaises(ValueError):
            channel.send_one(self.job['id'],transport=reject,retry_note='用户授权重试')

    def test_accepted_and_unknown_never_retry_even_with_operator_note(self):
        channel.send_one(self.job['id'],transport=self.accepted)
        for status in ('accepted','unknown'):
            with app.db() as c:
                c.execute('UPDATE uid_message_attempts SET status=?',(status,))
                c.execute('UPDATE message_jobs SET status=?',(status,))
            with self.assertRaises(ValueError):
                channel.send_one(self.job['id'],transport=self.accepted,retry_note='用户授权重试')

    def test_explicit_pre_submit_continuation_keeps_identity_and_failure_history(self):
        first = Mock(return_value=dict(status='failed', phase='create', platform_code='4'))
        failed = channel.send_one(self.job['id'], transport=first)
        self.assertIs(failed['evidence']['submission_reserved'], False)
        with app.db() as c:
            initial = dict(c.execute('SELECT * FROM uid_message_attempts').fetchone())
        channel.send_one(self.job['id'], transport=first)
        first.assert_called_once()
        result = channel.send_one(self.job['id'], transport=self.accepted, resume_note='合成测试：修复请求字段，原消息从未提交')
        self.assertEqual(result['status'], 'accepted')
        self.assertIs(result['evidence']['submission_reserved'], True)
        self.assertEqual(result['evidence']['preparation_history'][0]['evidence']['platform_code'], '4')
        with app.db() as c:
            current = dict(c.execute('SELECT * FROM uid_message_attempts').fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM uid_message_attempts').fetchone()[0], 1)
        for key in ('client_message_id', 'dedupe_key', 'sender_uid', 'recipient_uid', 'job_id'):
            self.assertEqual(initial[key], current[key])
        with self.assertRaises(ValueError):
            channel.send_one(self.job['id'], transport=self.accepted, resume_note='不能再次发送')

    def test_legacy_unknown_or_any_send_reservation_cannot_continue(self):
        channel.send_one(self.job['id'], transport=Mock(return_value=dict(status='failed', phase='create')))
        for status, phase, evidence in [('failed', 'create', {}), ('unknown', 'create', {'submission_reserved': False}),
                                         ('failed', 'send', {'submission_reserved': False}), ('failed', 'prepare_send', {'submission_reserved': True})]:
            with self.subTest(status=status, phase=phase, evidence=evidence), app.db() as c:
                c.execute('UPDATE uid_message_attempts SET status=?,phase=?,evidence=?', (status, phase, json.dumps(evidence)))
            transport = Mock()
            with self.assertRaises(ValueError):
                channel.send_one(self.job['id'], transport=transport, resume_note='合成维护')
            transport.assert_not_called()

    def test_continuation_rechecks_content_and_contact_and_is_bounded(self):
        fail = Mock(return_value=dict(status='failed', phase='create'))
        channel.send_one(self.job['id'], transport=fail)
        with app.db() as c:
            c.execute('UPDATE message_jobs SET content=? WHERE id=?', ('改变内容', self.job['id']))
        with self.assertRaises(ValueError):
            channel.send_one(self.job['id'], transport=fail, resume_note='合成维护')
        with app.db() as c:
            c.execute('UPDATE message_jobs SET content=? WHERE id=?', (self.job['content'], self.job['id']))
            c.execute('UPDATE people SET do_not_contact=1')
        with self.assertRaises(ValueError):
            channel.send_one(self.job['id'], transport=fail, resume_note='合成维护')
        with app.db() as c:
            c.execute('UPDATE people SET do_not_contact=0')
        for _ in range(3):
            channel.send_one(self.job['id'], transport=fail, resume_note='合成维护')
        with self.assertRaises(ValueError):
            channel.send_one(self.job['id'], transport=fail, resume_note='合成维护')
        self.assertEqual(fail.call_count, 4)

    def test_before_submit_error_or_duplicate_callback_never_reopens_submission(self):
        def transport(config, receiver, message, client, before):
            before()
            with self.assertRaises(ValueError):
                before()
            return {'status': 'failed', 'phase': 'prepare_send'}
        result = channel.send_one(self.job['id'], transport=transport)
        self.assertIs(result['evidence']['submission_reserved'], True)
        with self.assertRaises(ValueError):
            channel.send_one(self.job['id'], transport=transport, resume_note='合成维护')

    def test_state_never_loads_provider_or_claims_verified(self):
        state = channel.state()
        self.assertTrue(state['can_attempt'])
        self.assertFalse(state['live_verified'])
        self.assertNotIn(str(self.provider), json.dumps(state))

    def test_transport_failure_evidence_survives_and_is_not_retried(self):
        transport = Mock(return_value=dict(status='unknown', phase='send',
                         transport_phase='response_body', transport_error='response_exceeds_bound',
                         http_status=200, response_bytes=262145))
        result = channel.send_one(self.job['id'], transport=transport)
        self.assertEqual(result['status'], 'unknown')
        channel.send_one(self.job['id'], transport=transport)
        transport.assert_called_once()
        with app.db() as c:
            saved = json.loads(c.execute('SELECT evidence FROM uid_message_attempts WHERE job_id=?',
                                         (self.job['id'],)).fetchone()['evidence'])
        self.assertEqual(saved['transport_phase'], 'response_body')
        self.assertEqual(saved['response_bytes'], 262145)

    def test_probe_cannot_use_disabled_config_or_demo(self):
        self.settings['enabled'] = False
        self.configure()
        with patch('uid_transport.verify_identity') as verify:
            self.assertEqual(channel.probe_identity()['status'], 'not_configured')
            with self.assertRaises(ValueError):
                channel.probe_identity('demo')
            verify.assert_not_called()

    def test_probe_is_read_only_and_does_not_grant_send_permission(self):
        with app.db() as c:
            before = list(c.iterdump())
        result = dict(status='identity_verified', can_send=False, live_verified=False)
        with patch('uid_transport.verify_identity', return_value=result) as verify:
            self.assertEqual(channel.probe_identity()['status'], 'identity_verified')
            verify.assert_called_once_with(self.settings)
        with app.db() as c:
            self.assertEqual(list(c.iterdump()), before)
        self.assertFalse(channel.state()['live_verified'])
        self.assertEqual(channel.state()['status'], 'configured_unverified')

    def test_probe_shares_lock_with_send_and_releases_on_failure(self):
        with channel.GUARD, patch('uid_transport.verify_identity') as verify:
            with self.assertRaises(ValueError):
                channel.probe_identity()
            verify.assert_not_called()
        with patch('uid_transport.verify_identity', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                channel.probe_identity()
        self.assertFalse(channel.GUARD.locked())

    def test_configuration_reports_each_missing_part_without_import(self):
        self.settings.update(sender_uid='', allowed_recipient_uids=[], provider_file='private/missing.py')
        self.configure()
        state = channel.state()
        self.assertFalse(state['can_attempt'])
        self.assertEqual(len(state['issues']), 2)  # Empty manual scope is valid and authorizes nobody.
        self.assertIn('提供器文件不存在', ' '.join(state['issues']))
        self.settings.update(sender_uid=SENDER, allowed_recipient_uids=[RECEIVER, RECEIVER], provider_file=str(self.provider))
        self.configure()
        self.assertFalse(channel.state()['can_attempt'])

    def test_send_claim_once_and_dedup_across_new_drafts(self):
        with patch('uid_transport.send', wraps=self.accepted) as transport:
            self.assertEqual(channel.send_one(self.job['id'])['status'], 'accepted')
            self.assertEqual(channel.send_one(self.job['id'])['status'], 'accepted')
            self.assertEqual(transport.call_count, 1)
            duplicate = self.draft('different-key')
            with self.assertRaises(ValueError):
                channel.send_one(duplicate['id'])
        self.assertEqual(len(app.state()['messages']), 1)
        self.assertEqual(app.state()['stats']['submitted'], 0, 'Test receipts must not enter customer metrics')
        self.assertEqual(app.mutate('send', {'id': self.job['id']})['status'], 'accepted')

    def test_unknown_and_restart_do_not_resend(self):
        def crash(*args):
            raise TimeoutError('credentials-do-not-log')
        self.assertEqual(channel.send_one(self.job['id'], transport=crash)['status'], 'unknown')
        with app.db() as c:
            c.execute("UPDATE uid_message_attempts SET status='submitting'")
            c.execute("UPDATE message_jobs SET status='submitting'")
        with patch('uid_transport.send') as transport:
            channel.recover()
            self.assertEqual(channel.send_one(self.job['id'])['status'], 'unknown')
            transport.assert_not_called()
        self.assertEqual(app.state()['messages'], [])

    def test_revoked_contact_after_create_prevents_send(self):
        def change(config, receiver, message, client, before):
            app.mutate('contact', {'lead_id': self.lead_id, 'contact_basis': 'opt_in', 'contact_note': '拒绝后停止', 'do_not_contact': True})
            before()
            self.fail('must not submit')
        result = channel.send_one(self.job['id'], transport=change)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(app.state()['messages'], [])

    def test_blocked_and_not_allowed_do_not_create_attempts(self):
        self.settings['allowed_recipient_uids'] = ['999999']
        self.configure()
        with self.assertRaises(ValueError):
            channel.send_one(self.job['id'], transport=self.accepted)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM uid_message_attempts').fetchone()[0], 0)

    def test_old_message_cannot_repeat(self):
        for test_id in ('CO-DM-01', 'CO-HTTP-02'):
            old = self.draft('old-' + test_id, '测试编号：' + test_id + '。')
            with self.assertRaises(ValueError):
                channel.send_one(old['id'], transport=self.accepted)

    def test_read_only_and_disabled_config_do_not_dispatch(self):
        self.settings['enabled'] = False
        self.configure()
        with patch('uid_transport.send') as transport:
            self.assertEqual(channel.send_one(self.job['id'])['status'], 'not_connected')
            channel.state()
            transport.assert_not_called()

    def test_one_inflight_task(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        def slow(*args):
            entered.set()
            release.wait(3)
            return {'status': 'unknown', 'phase': 'send'}
        thread = threading.Thread(target=lambda: result.append(channel.send_one(self.job['id'], transport=slow)))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError):
                channel.send_one(self.job['id'], transport=self.accepted)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(result[0]['status'], 'unknown')

    def test_migration_backups_preserve_existing_jobs(self):
        with app.db() as c:
            c.execute('DROP TABLE uid_message_attempts')
        app.init()
        backup = next((app.DATA_DIR / 'backups').glob('*.bak'))
        with closing(sqlite3.connect(backup)) as c:
            self.assertEqual(c.execute('SELECT content FROM message_jobs').fetchone()[0], '纯本地模拟消息')
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='uid_message_attempts'").fetchone())

    def test_http_api_uses_queue_and_retired_test_is_blocked(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        def post(path, body):
            connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
            try:
                connection.request('POST', path, json.dumps(body), {'X-ClubOps-Token': server.CSRF, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            with patch.object(server, 'PORT', httpd.server_port), patch('uid_transport.send', wraps=self.accepted) as transport:
                with patch('uid_messaging.probe_identity', return_value={'status': 'identity_verified'}) as probe:
                    self.assertEqual(post('/api/uid-http-probe', {'uid': RECEIVER})[0], 400)
                    probe.assert_not_called()
                    self.assertEqual(post('/api/uid-http-probe', {})[1]['result']['status'], 'identity_verified')
                    probe.assert_called_once_with('live')
                    transport.assert_not_called()
                self.assertEqual(post('/api/uid-http-send', {'id': True})[0], 400)
                self.assertEqual(post('/api/uid-http-send', {'id': self.job['id'], 'content': 'override'})[0], 400)
                self.assertEqual(post('/api/uid-http-send', {'id': self.job['id']})[1]['result']['status'], 'accepted')
                self.assertEqual(post('/api/uid-http-send', {'id': self.job['id']})[0], 200)
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(post('/api/dm-test-send', {})[0], 400)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(3)

    def test_post_send_storage_failure_keeps_attempt_reserved(self):
        with patch('clubops.event', side_effect=sqlite3.OperationalError('synthetic storage failure')):
            with self.assertRaises(sqlite3.Error):
                channel.send_one(self.job['id'], transport=self.accepted)
        with patch('uid_transport.send') as transport:
            self.assertEqual(channel.send_one(self.job['id'])['status'], 'unknown')
            transport.assert_not_called()


class IdentityProbeTests(unittest.TestCase):
    def probe(self, data, status=200, mime='application/json'):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        with patch('uid_transport.request', return_value=(status, mime, raw)) as exchange:
            result = uid_transport.verify_identity({'sender_uid': SENDER}, provider=Provider())
            exchange.assert_called_once_with('identity', {'payload': b'', 'headers': {}, 'query': {}})
        return result

    def test_success_is_only_identity_and_large_uid_is_exact(self):
        for uid in (SENDER, int(SENDER)):
            result = self.probe({'status_code': 0, 'user': {'uid': uid, 'nickname': 'private-field'}})
            self.assertEqual(result['status'], 'identity_verified')
            self.assertEqual(result['sender_uid'], SENDER)
            self.assertFalse(result['can_send'])
            self.assertFalse(result['live_verified'])
            self.assertNotIn('private-field', json.dumps(result))

    def test_wrong_identity_or_business_error_is_not_verified(self):
        for data in ({'status_code': 1, 'user': {'uid': SENDER}},
                     {'status_code': False, 'user': {'uid': SENDER}},
                     {'user': {'uid': RECEIVER}}, {'user': {'uid': float(SENDER)}},
                     {'user': {'uid': True}}, {'user': None}, [], b'not json'):
            self.assertEqual(self.probe(data)['status'], 'failed')

    def test_http_200_html_and_http_rejections_are_not_login(self):
        for status, mime in ((200, 'text/html'), (302, 'application/json'),
                             (403, 'application/json'), (429, 'application/json')):
            self.assertEqual(self.probe({'user': {'uid': SENDER}}, status, mime)['status'], 'failed')

    def test_provider_error_and_timeout_do_not_leak_or_retry(self):
        for target in ('uid_transport.load_provider', 'uid_transport.request'):
            with patch(target, side_effect=TimeoutError('private-token-value')) as call:
                result = uid_transport.verify_identity({'sender_uid': SENDER, 'provider_file': 'missing'},
                                                       provider=Provider() if target.endswith('request') else None)
                call.assert_called_once()
                self.assertEqual(result['status'], 'failed')
                self.assertNotIn('private-token-value', json.dumps(result))

    def test_nonempty_identity_body_is_rejected_before_network(self):
        provider = Provider()
        provider.prepare = lambda *args: {'payload': b'not-empty'}
        with patch('uid_transport.request') as request:
            self.assertEqual(uid_transport.verify_identity({'sender_uid': SENDER}, provider=provider)['status'], 'failed')
            request.assert_not_called()

    def test_send_stops_at_identity_business_error(self):
        with patch('uid_transport.request', return_value=(200, 'application/json',
                   json.dumps({'status_code': 1, 'user': {'uid': SENDER}}).encode())) as request:
            result = uid_transport.send({'sender_uid': SENDER}, RECEIVER, 'synthetic', 'client',
                                        lambda: self.fail('must not submit'), provider=Provider())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual([c.args[0] for c in request.call_args_list], ['identity'])


if __name__ == '__main__':
    unittest.main()
