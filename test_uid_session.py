"""Synthetic IM/identity fixtures; local DPAPI tests never contact a platform."""
import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import uid_protocol as wire
import uid_session as session
import uid_transport

SENDER, RECEIVER = '10000000000000001', '10000000000000002'


def capture():
    context = b''.join(wire.field(n, v) for n, v in {
        3: 'synthetic', 4: '', 5: 3, 6: 1, 7: 'test', 9: '0', 11: 'douyin_pc',
        14: 'test', 15: wire.field(1, 'app_name') + wire.field(2, 'synthetic'),
        18: 1, 21: 'douyin_web', 22: 'web_sdk'}.items())
    body = wire.field(1000, wire.field(1, 0) + wire.field(2, 20) + wire.field(3, 1))
    return dict(expected_account='synthetic_account', identity_cookie='sessionid=synthetic-identity-secret',
                headers={'cookie': 'sessionid=synthetic-im-secret; sessionid_ss=synthetic-ss-secret; page_state=synthetic-ui-data', 'user-agent': 'Synthetic browser',
                         'origin': 'https://www.douyin.com'},
                payload=base64.b64encode(wire.field(1, 1001) + wire.field(2, 12345) + context + wire.field(8, body)).decode())


def data():
    value = session.from_capture(capture(), dict(sender_uid=SENDER, expected_account='synthetic_account'))
    value['im_verified'] = True  # Synthetic proof for prepare-only tests.
    return value


class SessionProviderTests(unittest.TestCase):
    def test_private_conversation_inbox_is_not_copied_from_stranger_read(self):
        value = data()
        original = wire.decode(base64.b64decode(value['context']))
        self.assertEqual(wire.one(original, 6, 0), 1)
        provider = session.Provider(memory=value)
        for operation, command, body in [('create', 609, wire.create_body(SENDER, RECEIVER)),
                                         ('send', 100, wire.field(100, b'')),
                                         ('im_check', 1001, wire.field(1000, b''))]:
            root = wire.decode(provider.prepare(operation, body, dict(sender_uid=SENDER, command=command))['payload'])
            self.assertEqual(wire.one(root, 6, 0), 1 if operation == 'im_check' else 0)
            for number, values in original.items():
                if number != 6:
                    self.assertEqual(root[number], values)
        self.assertEqual(value['context'], data()['context'])

    def test_im_cookie_uses_actual_login_pair_without_other_page_state(self):
        value = data()
        expected = 'sessionid=synthetic-im-secret; sessionid_ss=synthetic-ss-secret'
        self.assertEqual(value['headers']['cookie'], expected)
        value['headers']['cookie'] += '; page_state=synthetic-ui-data'
        provider = session.Provider(memory=value)
        prepared = provider.prepare('create', wire.create_body(SENDER, RECEIVER), dict(sender_uid=SENDER, command=609))
        self.assertEqual(prepared['headers']['cookie'], expected)
        self.assertNotIn('synthetic-ui-data', prepared['headers']['cookie'])
        self.assertIn('synthetic-identity-secret', provider.prepare('identity', b'', dict(sender_uid=SENDER))['headers']['Cookie'])

    def test_missing_duplicate_or_malformed_login_cookie_is_rejected(self):
        for raw in ['', None, 'sessionid=a', 'sessionid_ss=b', 'sessionid=a; sessionid=a; sessionid_ss=b',
                    'sessionid=a; sessionid_ss=', 'sessionid=a b; sessionid_ss=c', 'sessionid=a; sessionid_ss=b\n']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                session.session_cookie(raw)

    def test_certificate_auth_is_not_accepted_as_session_auth(self):
        value = data()
        context = wire.decode(base64.b64decode(value['context']))
        altered = b''.join(wire.field(n, 4 if n == 18 else item) for n, fields in context.items() for kind, item in fields)
        with self.assertRaises(ValueError):
            session.validate(dict(value, context=base64.b64encode(altered).decode()))

    def test_context_excludes_old_command_sequence_and_business_body(self):
        value = data()
        context = wire.decode(base64.b64decode(value['context']))
        self.assertEqual(set(context), session.CONTEXT_FIELDS)
        self.assertFalse({1, 2, 8, 23, 24, 25} & set(context))
        raw = base64.b64decode(capture()['payload'])
        for altered in (raw + wire.field(25, 'synthetic-signature'), raw + wire.field(1, 100)):
            with self.assertRaises(ValueError):
                session.from_capture(dict(capture(), payload=base64.b64encode(altered).decode()),
                                     dict(sender_uid=SENDER, expected_account='synthetic_account'))

    def test_prepare_identity_and_business_are_distinct_and_exact(self):
        provider = session.Provider(memory=data())
        identity = provider.prepare('identity', b'', dict(sender_uid=SENDER))
        self.assertEqual(identity['query'], {'aid': '6383'})
        self.assertIn('synthetic-identity-secret', identity['headers']['Cookie'])
        body = wire.create_body(SENDER, RECEIVER)
        first = provider.prepare('create', body, dict(sender_uid=SENDER, command=609))
        second = provider.prepare('create', body, dict(sender_uid=SENDER, command=609))
        uid_transport.validate_envelope(first, 609, body)
        self.assertIn('synthetic-im-secret', first['headers']['cookie'])
        self.assertEqual(wire.one(wire.decode(second['payload']), 2, 0), wire.one(wire.decode(first['payload']), 2, 0) + 1)
        with self.assertRaises(ValueError):
            provider.prepare('create', body, dict(sender_uid=RECEIVER, command=609))
        with self.assertRaises(ValueError):
            provider.prepare('other', b'', {})

    def test_old_or_invalid_context_never_becomes_a_prepared_request(self):
        for captured_at in [time.time()-session.MAX_AGE-1, time.time()+120, True, float('nan')]:
            with self.assertRaises(ValueError):
                session.Provider(memory=dict(data(), captured_at=captured_at))
        for field, value in [('sender_uid', 123), ('sequence', True), ('identity_cookie', 'bad\nheader')]:
            with self.assertRaises(ValueError):
                session.Provider(memory=dict(data(), **{field: value}))

    def test_im_read_requires_command_sequence_uid_and_business_success(self):
        for bad in ['', 'uid', 'sequence', 'command', 'status', 'oversized_list']:
            provider = session.Provider(memory=data())
            def exchange(operation, prepared):
                self.assertEqual(operation, 'im_check')
                request = wire.decode(prepared['payload'])
                query = wire.decode(wire.one(wire.decode(wire.one(request, 8, 2)), 1000, 2))
                self.assertEqual(wire.one(query, 2, 0), 1)
                info = wire.field(1, 0) + (wire.field(4, 'private') * 2 if bad == 'oversized_list' else b'')
                raw = (wire.field(1, 100 if bad == 'command' else 1001)
                       + wire.field(2, 1 if bad == 'sequence' else wire.one(request, 2, 0))
                       + wire.field(3, 1 if bad == 'status' else 0) + wire.field(4, 'OK')
                       + wire.field(13, int(RECEIVER if bad == 'uid' else SENDER))
                       + wire.field(6, wire.field(1000, info)))
                return 200, 'application/x-protobuf', raw
            result = session.check_im(provider, SENDER, exchange=exchange)
            self.assertEqual(result['status'] == 'im_read_verified', bad == '')
            self.assertFalse(result['can_send'])
            self.assertNotIn('private', json.dumps(result))

    def test_ticket_is_server_issued_bound_to_both_participants_and_not_returned_as_receipt(self):
        cid = '0:1:' + SENDER + ':' + RECEIVER
        info = wire.field(1, cid) + wire.field(2, 123456789) + wire.field(3, 1) + wire.field(4, 'synthetic-ticket-secret')
        def created(check_code=0):
            return (wire.field(1, 609) + wire.field(4, 'OK') + wire.field(13, int(SENDER))
                    + wire.field(6, wire.field(609, wire.field(1, info) + wire.field(2, check_code))))
        conversation = wire.result(created(), 609, SENDER, RECEIVER)
        provider = session.Provider(memory=data())
        self.assertEqual(provider.ticket(conversation, SENDER, RECEIVER), 'synthetic-ticket-secret')
        self.assertEqual(wire.result(created(1), 609, SENDER, RECEIVER)['status'], 'failed')
        with self.assertRaises(ValueError):
            provider.ticket(conversation, SENDER, '999')
        calls = []
        def exchange(operation, prepared):
            calls.append(operation)
            if operation == 'identity':
                return 200, 'application/json', json.dumps({'user': {'uid': SENDER}}).encode()
            if operation == 'create':
                return 200, 'application/x-protobuf', created() + wire.field(2, wire.one(wire.decode(prepared['payload']), 2, 0))
            return 403, 'application/json', b'{}'
        result = uid_transport.send({'sender_uid': SENDER}, RECEIVER, 'synthetic only', 'client',
                                    lambda: None, provider=provider, exchange=exchange)
        self.assertEqual(calls, ['identity', 'create', 'send'])
        self.assertNotIn('synthetic-ticket-secret', json.dumps(result))

    def test_failed_identity_or_im_check_never_saves_credentials(self):
        with patch('uid_bootstrap.probe', return_value={'status': 'account_mismatch'}), patch.object(session, 'save') as save:
            self.assertEqual(session.bootstrap(capture())['status'], 'account_mismatch')
            save.assert_not_called()

    def test_bootstrap_failure_reports_phase_and_shape_without_private_values(self):
        identity = dict(status='identity_verified', sender_uid=SENDER, expected_account='synthetic_account', http_attempts=1)
        original = capture()
        raw = base64.b64decode(original['payload'])
        changed = dict(original, payload=base64.b64encode(raw + wire.field(25, 'synthetic-private-signature')).decode())
        with patch('uid_bootstrap.probe', return_value=identity), patch.object(session, 'save') as save, patch.object(session, 'check_im') as check:
            result = session.bootstrap(changed)
            self.assertEqual(result['bootstrap_phase'], 'read_context_validation')
            self.assertTrue(result['identity_verified'])
            self.assertEqual(result['http_attempts'], 1)
            self.assertEqual(result['unexpected_fields'], [25])
            self.assertEqual(result['auth_mode'], 1)
            self.assertTrue(result['cookie_pair_valid'])
            self.assertFalse(result['credential_file_created'])
            self.assertFalse(result['can_send'])
            self.assertNotIn('synthetic-', json.dumps(result))
            check.assert_not_called()
            save.assert_not_called()
        for payload in ('bad base64!', base64.b64encode(b'x' * 16385).decode()):
            result = session.capture_shape(dict(original, payload=payload))
            self.assertIn(result['capture_shape'], ('unrecognized', 'oversized'))
            self.assertNotIn('synthetic-', json.dumps(result))

    def test_bootstrap_storage_failure_preserves_verified_stage_without_success(self):
        identity = dict(status='identity_verified', sender_uid=SENDER, expected_account='synthetic_account', http_attempts=1)
        checked = dict(status='im_read_verified', http_attempts=1, can_send=False, live_verified=False)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CLUBOPS_DATA_DIR': directory}), patch('uid_bootstrap.probe', return_value=identity), patch.object(session, 'check_im', return_value=checked), patch.object(session, 'save', side_effect=OSError('synthetic-private-file-error')):
            result = session.bootstrap(capture())
            self.assertEqual(result['bootstrap_phase'], 'credential_save')
            self.assertEqual(result['status'], 'session_not_saved')
            self.assertEqual(result['http_attempts'], 2)
            self.assertFalse(result['credential_file_created'])
            self.assertNotIn('synthetic-', json.dumps(result))

    def test_rejected_or_uncertain_check_invalidates_previous_im_proof(self):
        for error in [TimeoutError('synthetic-secret'), uid_transport.TransportError('timeout', 'response_headers')]:
            provider = session.Provider(memory=data())
            result = session.check_im(provider, SENDER, exchange=Mock(side_effect=error))
            self.assertEqual(result['status'], 'im_check_failed')
            self.assertFalse(provider.current()['im_verified'])
            with self.assertRaises(ValueError):
                provider.prepare('create', wire.create_body(SENDER, RECEIVER), dict(sender_uid=SENDER, command=609))
        identity = dict(status='identity_verified', sender_uid=SENDER, expected_account='synthetic_account', http_attempts=1)
        with patch('uid_bootstrap.probe', return_value=identity), patch.object(session, 'check_im', return_value={'status': 'im_check_failed', 'http_attempts': 1}), patch.object(session, 'save') as save:
            self.assertEqual(session.bootstrap(capture())['status'], 'im_check_failed')
            save.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'DPAPI requires Windows')
    def test_dpapi_vault_never_writes_plaintext_and_sequence_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.dpapi'
            with session.runtime.data_lock(path.parent):
                session.save(data(), path)
            self.assertNotIn(b'synthetic-im-secret', path.read_bytes())
            self.assertEqual(session.load(path)['sender_uid'], SENDER)
            body = wire.create_body(SENDER, RECEIVER)
            first = session.Provider(path=path).prepare('create', body, dict(sender_uid=SENDER, command=609))
            second = session.Provider(path=path).prepare('create', body, dict(sender_uid=SENDER, command=609))
            self.assertEqual(wire.one(wire.decode(second['payload']), 2, 0), wire.one(wire.decode(first['payload']), 2, 0) + 1)
            self.assertFalse(list(path.parent.glob('.session-*.tmp')))

    @unittest.skipUnless(os.name == 'nt', 'DPAPI requires Windows')
    def test_success_creates_disabled_config_and_preserves_existing_config(self):
        identity = dict(status='identity_verified', sender_uid=SENDER, expected_account='synthetic_account', http_attempts=1)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CLUBOPS_DATA_DIR': directory}), patch('uid_bootstrap.probe', return_value=identity), patch.object(session, 'check_im', side_effect=lambda *args: dict(status='im_read_verified', http_attempts=1, can_send=False, live_verified=False)):
            result = session.bootstrap(capture())
            self.assertEqual(result['status'], 'session_ready')
            self.assertEqual(result['http_attempts'], 2)
            path = Path(directory) / 'uid-http.json'
            config = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(config['enabled'])
            self.assertEqual(config['allowed_recipient_uids'], [])
            preserved = b'{"existing":"leave untouched"}'
            path.write_bytes(preserved)
            self.assertEqual(session.bootstrap(capture())['status'], 'session_ready')
            self.assertEqual(path.read_bytes(), preserved)


if __name__ == '__main__':
    unittest.main()
