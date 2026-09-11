import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import messaging_http as dm


class MessagingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clubops-im-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {dm.TOKEN_ENV: 'synthetic-secret-never-print'}
        self.context = dict(sender_douyin_id=dm.SENDER, recipient_douyin_id=dm.RECIPIENT,
            sender_open_id='synthetic-sender-openid', recipient_open_id='synthetic-recipient-openid',
            sender_authorized=True, identity_verification_note='合成映射，不对应任何真实用户',
            scopes=['im.direct_message'], scene='im_reply_msg',
            event_time=datetime.now(timezone.utc).isoformat(), msg_id='synthetic-message+id=',
            conversation_id='synthetic-conversation/id=')
        self.ok = dm.interpret(200, 'application/json', b'{"data":{"error_code":0},"extra":{"error_code":0}}')

    def configure(self, **changes):
        (self.root / dm.CONTEXT_FILE).write_text(json.dumps({**self.context, **changes}), encoding='utf-8')

    def test_empty_configuration_is_truthful_read_only_and_fixed_pair(self):
        value = dm.state(self.root, environ={})
        self.assertEqual((value['sender'], value['recipient']), ('1267597446', '34575459517'))
        self.assertEqual(value['status'], 'not_configured')
        self.assertFalse(value['can_send'])
        self.assertIsNone(value['attempt'])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIsNone(dm.state(self.root, mode='demo', environ={}))

    def test_preflight_does_not_send_or_claim_platform_verified(self):
        self.configure()
        with patch.object(dm, 'http_post') as transport:
            value = dm.state(self.root, environ=self.env)
        transport.assert_not_called()
        self.assertEqual(value['status'], 'configured_unverified')
        self.assertTrue(value['can_send'])
        self.assertFalse((self.root / dm.LEDGER_FILE).exists())
        self.assertNotIn(self.env[dm.TOKEN_ENV], json.dumps(value))
        self.assertNotIn('synthetic-sender-openid', json.dumps(value))

    def test_invalid_identity_permission_context_or_token_never_sends(self):
        changes = [dict(sender_douyin_id='other'), dict(recipient_douyin_id=dm.SENDER),
            dict(sender_open_id=dm.SENDER), dict(recipient_open_id=dm.RECIPIENT),
            dict(recipient_open_id=self.context['sender_open_id']), dict(sender_authorized=False),
            dict(identity_verification_note=''), dict(scopes=[]), dict(scopes=[{}]),
            dict(scene=[]), dict(scene='im_b2b_direct_message'), dict(scene='im_authorize_message'),
            dict(event_time='not-time'), dict(event_time='2020-01-01T00:00:00Z'),
            dict(event_time=datetime.now().isoformat()),
            dict(event_time=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()),
            dict(msg_id=''), dict(conversation_id='bad\nheader')]
        for change in changes:
            with self.subTest(change=change):
                self.configure(**change)
                transport = Mock(return_value=self.ok)
                self.assertFalse(dm.send_one(self.root, environ=self.env, transport=transport)['can_send'])
                transport.assert_not_called()
                self.assertFalse((self.root / dm.LEDGER_FILE).exists())
        self.configure()
        for token in ('', 'bad\r\nHeader: value', 'token with space', '非ASCII', None):
            transport = Mock()
            dm.send_one(self.root, environ={dm.TOKEN_ENV: token}, transport=transport)
            transport.assert_not_called()

    def test_untrusted_config_shape_is_not_exposed_or_sent(self):
        for content in ('[1,2]', '{"access_token":"synthetic-secret"}', '{broken secret', ' ' * 32769):
            (self.root / dm.CONTEXT_FILE).write_text(content, encoding='utf-8')
            result = dm.state(self.root, environ=self.env)
            self.assertEqual(result['status'], 'not_configured')
            self.assertNotIn('synthetic-secret', json.dumps(result))
            self.assertFalse(result['can_send'])

    def test_exactly_one_attempt_persists_across_calls_and_no_customer_db_created(self):
        self.configure()
        transport = Mock(return_value=self.ok)
        first = dm.send_one(self.root, environ=self.env, transport=transport)
        second = dm.send_one(self.root, environ=self.env, transport=transport)
        self.assertEqual(first['status'], 'api_accepted')
        self.assertIn('尚未确认', first['attempt']['detail'])
        self.assertEqual(first, second)
        self.assertFalse(second['can_send'])
        transport.assert_called_once()
        self.assertFalse((self.root / 'clubops-live.db').exists())
        self.assertNotIn(self.env[dm.TOKEN_ENV], json.dumps(first))
        self.assertNotIn('synthetic-conversation', json.dumps(first))

    def test_concurrent_submissions_claim_only_one_attempt(self):
        self.configure()
        transport = Mock(return_value=self.ok)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: dm.send_one(self.root, environ=self.env, transport=transport), range(20)))
        self.assertEqual(transport.call_count, 1)
        self.assertFalse(dm.state(self.root, environ=self.env)['can_send'])

    def test_network_failure_is_unknown_and_not_retried_or_logged_raw(self):
        self.configure()
        transport = Mock(side_effect=TimeoutError(self.env[dm.TOKEN_ENV]))
        result = dm.send_one(self.root, environ=self.env, transport=transport)
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn(self.env[dm.TOKEN_ENV], json.dumps(result))
        dm.send_one(self.root, environ=self.env, transport=transport)
        transport.assert_called_once()

    def test_interrupted_process_retains_duplicate_barrier(self):
        self.configure()
        with self.assertRaises(SystemExit):
            dm.send_one(self.root, environ=self.env, transport=Mock(side_effect=SystemExit()))
        result = dm.state(self.root, environ=self.env)
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['can_send'])
        transport = Mock()
        dm.send_one(self.root, environ=self.env, transport=transport)
        transport.assert_not_called()

    def test_result_write_failure_does_not_allow_resend(self):
        self.configure()
        def transport(*_):
            patcher = patch.object(dm.sqlite3, 'connect', side_effect=sqlite3.OperationalError('secret'))
            patcher.start()
            self.addCleanup(patcher.stop)
            return self.ok
        result = dm.send_one(self.root, environ=self.env, transport=transport)
        self.assertEqual(result['status'], 'storage_error')
        self.assertFalse(result['can_send'])

    def test_corrupt_ledger_fails_closed(self):
        self.configure()
        (self.root / dm.LEDGER_FILE).write_bytes(b'broken ledger')
        transport = Mock()
        result = dm.send_one(self.root, environ=self.env, transport=transport)
        self.assertEqual(result['status'], 'storage_error')
        transport.assert_not_called()

    def test_demo_and_body_override_cannot_send(self):
        self.configure()
        with self.assertRaises(ValueError):
            dm.send_one(self.root, mode='demo', environ=self.env)
        for body in ({'recipient': 'other'}, {'text': 'override'}, {'batch': []}, []):
            with self.assertRaises(ValueError):
                dm.send_one(self.root, body=body, environ=self.env)
        self.assertFalse((self.root / dm.LEDGER_FILE).exists())

    def test_http_200_alone_is_never_success(self):
        for raw in (b'', b'{}', b'[]', b'{"extra":{"error_code":0}}', b'false', b'{"data":{"error_code":false}}', b'{"success":true}', b'{broken'):
            self.assertEqual(dm.interpret(200, 'application/json', raw)['status'], 'unknown')
        self.assertEqual(dm.interpret(200, 'text/html', b'{"err_no":0}')['status'], 'unknown')
        self.assertEqual(dm.interpret(200, 'notjson', b'{"err_no":0}')['status'], 'unknown')
        for raw in (b'{"err_no":0}', b'{"data":{"error_code":"0"}}'):
            self.assertEqual(dm.interpret(200, 'application/json', raw)['status'], 'api_accepted')
        self.assertEqual(dm.interpret(200, 'application/json', b'{"data":{"error_code":0},"extra":{"error_code":28001018}}')['status'], 'rejected')

    def test_http_transport_uses_fixed_https_endpoint_and_nested_text(self):
        response = Mock(status=200)
        response.getheader.return_value = 'application/json'
        response.read1.side_effect = [b'{"data":{"error_code":0}}', b'']
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(dm.http.client, 'HTTPSConnection', return_value=connection) as factory:
            result = dm.http_post(self.context, self.env[dm.TOKEN_ENV])
        self.assertEqual(factory.call_args.args, ('open.douyin.com',))
        method, path = connection.request.call_args.args
        self.assertEqual(method, 'POST')
        self.assertTrue(path.startswith('/im/send/msg/?open_id='))
        request = connection.request.call_args.kwargs
        self.assertEqual(request['headers']['access-token'], self.env[dm.TOKEN_ENV])
        body = json.loads(request['body'])
        self.assertEqual(body['to_user_id'], self.context['recipient_open_id'])
        self.assertEqual(body['content'], {'msg_type': 1, 'text': {'text': dm.TEXT}})
        self.assertEqual(body['channel'], 3)
        self.assertEqual(result['status'], 'api_accepted')
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_redirect_auth_and_rate_limits_are_not_retried(self):
        for status in (302, 401, 403, 429, 500):
            response = Mock(status=status)
            connection = Mock()
            connection.getresponse.return_value = response
            with patch.object(dm.http.client, 'HTTPSConnection', return_value=connection):
                value = dm.http_post(self.context, self.env[dm.TOKEN_ENV])
            self.assertNotEqual(value['status'], 'api_accepted')
            response.read1.assert_not_called()
            connection.request.assert_called_once()

    def test_oversized_response_is_unknown(self):
        response = Mock(status=200)
        response.read1.return_value = b'x' * (dm.MAX_BYTES + 1)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(dm.http.client, 'HTTPSConnection', return_value=connection):
            self.assertEqual(dm.http_post(self.context, self.env[dm.TOKEN_ENV])['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
