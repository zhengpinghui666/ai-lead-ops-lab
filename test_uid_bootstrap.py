"""Synthetic profile responses only; no real accounts or network requests."""
import json
import unittest
from unittest.mock import Mock

import uid_bootstrap as boot


class IdentityBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.input = dict(expected_account='test_account', cookie='sessionid=synthetic-secret', user_agent='Synthetic browser')

    def response(self, user, code=0, mime='application/json', status=200):
        return Mock(return_value=(status, mime, json.dumps(dict(status_code=code, user=user)).encode()))

    def test_exact_account_and_large_numeric_uid_are_required(self):
        exchange = self.response(dict(unique_id='test_account', uid=9007199254740993123))
        result = boot.probe(self.input, exchange=exchange)
        self.assertEqual(result['status'], 'identity_verified')
        self.assertEqual(result['sender_uid'], '9007199254740993123')
        self.assertEqual(result['http_attempts'], 1)
        self.assertFalse(result['can_send'])
        self.assertFalse(result['live_verified'])
        operation, prepared = exchange.call_args.args
        self.assertEqual(operation, 'identity')
        self.assertEqual(prepared['payload'], b'')
        self.assertEqual(prepared['query'], {'aid': '6383'})
        self.assertNotIn('synthetic-secret', json.dumps(result))

    def test_mismatching_account_does_not_disclose_other_user(self):
        result = boot.probe(self.input, exchange=self.response(dict(unique_id='other', uid='123456789')))
        self.assertEqual(result['status'], 'account_mismatch')
        self.assertNotIn('sender_uid', result)
        self.assertNotIn('other', json.dumps(result))

    def test_invalid_inputs_and_absent_cookie_do_not_connect(self):
        for value in [None, {}, dict(self.input, cookie='bad\nheader'), dict(self.input, expected_account=True),
                      dict(self.input, user_agent='x'*513), dict(self.input, endpoint='elsewhere')]:
            exchange=Mock()
            self.assertEqual(boot.probe(value, exchange=exchange)['status'], 'invalid_input')
            exchange.assert_not_called()
        exchange=Mock()
        self.assertEqual(boot.probe(dict(self.input, cookie=''), exchange=exchange)['status'], 'needs_login')
        exchange.assert_not_called()

    def test_business_error_and_ambiguous_uid_are_not_login_proof(self):
        for code in [1, True, '0', None]:
            result=boot.probe(self.input, exchange=self.response(dict(unique_id='test_account', uid='12345'), code=code))
            self.assertNotEqual(result['status'], 'identity_verified')
        for uid in [True, 123.0, '', '0', '18446744073709551616']:
            result=boot.probe(self.input, exchange=self.response(dict(unique_id='test_account', uid=uid)))
            self.assertNotEqual(result['status'], 'identity_verified')

    def test_empty_html_and_http_rejection_are_not_success(self):
        for response in [(200,'application/json',b''),(200,'text/html',b'<html>verify</html>'),(403,'application/json',b'{}')]:
            exchange=Mock(return_value=response)
            result=boot.probe(self.input,exchange=exchange)
            self.assertNotEqual(result['status'],'identity_verified')
            exchange.assert_called_once()

    def test_exception_detail_never_leaks_session_material(self):
        result=boot.probe(self.input,exchange=Mock(side_effect=RuntimeError('synthetic-secret')))
        self.assertEqual(result['status'],'http_failed')
        self.assertNotIn('synthetic-secret',json.dumps(result))

    def test_business_diagnostic_retains_only_nonsecret_fields(self):
        body=dict(status_code=8,status_msg='synthetic-secret',verify_data='private-challenge',log_pb={'secret':'private'})
        result=boot.probe(self.input,exchange=Mock(return_value=(200,'application/json',json.dumps(body).encode())))
        self.assertEqual(result['status'],'unrecognized_response')
        self.assertEqual(result['business_code'],8)
        self.assertFalse(result['user_present'])
        self.assertTrue(result['verification_indicated'])
        self.assertNotIn('private',json.dumps(result))
        self.assertNotIn('synthetic-secret',json.dumps(result))


if __name__ == '__main__':
    unittest.main()
