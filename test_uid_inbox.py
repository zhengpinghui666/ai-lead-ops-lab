"""Synthetic selected-recipient reads; no real platform or private messages."""
import json
import unittest

import uid_inbox as inbox
import uid_protocol as wire
import uid_session
import uid_transport
from test_uid_session import data, SENDER, RECEIVER

OTHER = '10000000000000003'
CID = '0:1:' + SENDER + ':' + RECEIVER
CONVERSATION = dict(conversation_id=CID, conversation_short_id='9007199254740999', peer_uid=RECEIVER, inbox=0)


def reply(operation, prepared, body, **changes):
    request = wire.decode(prepared['payload'])
    command, field = inbox.OPERATIONS[operation]
    values = {1: command, 2: wire.one(request, 2, 0), 3: 0, 4: 'OK', 5: wire.one(request, 6, 0), 13: int(SENDER), 6: wire.field(field, body)}
    values.update({int(k): v for k, v in changes.items()})
    return 200, 'application/x-protobuf', b''.join(wire.field(k, v) for k, v in values.items())


def conversation(peer=RECEIVER, stranger=False):
    cid = '0:1:' + SENDER + ':' + peer
    if stranger:
        return wire.field(1, int(CONVERSATION['conversation_short_id'])) + wire.field(4, cid)
    return (wire.field(1, cid) + wire.field(2, int(CONVERSATION['conversation_short_id']))
            + wire.field(3, 1) + wire.field(4, 'synthetic-ticket-secret') + wire.field(9, 0))


def message(mid=9007199254741001, author=RECEIVER, cid=CID, short=None, kind=7, text='synthetic reply'):
    return (wire.field(1, cid) + wire.field(2, 1) + wire.field(3, mid) + wire.field(4, 3)
            + wire.field(5, int(short or CONVERSATION['conversation_short_id'])) + wire.field(6, kind)
            + wire.field(7, int(author)) + wire.field(8, json.dumps({'text': text}))
            + wire.field(9, wire.field(1, 'private-ext') + wire.field(2, 'synthetic-hidden-secret'))
            + wire.field(10, 1789059717))


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.provider = uid_session.Provider(memory=data())
        self.calls = []

    def exchange(self, read):
        def send(operation, prepared):
            self.calls.append(operation)
            self.assertNotIn(operation, ('create', 'send'))
            if operation == 'identity':
                return 200, 'application/json', json.dumps({'status_code': 0, 'user': {'uid': SENDER}}).encode()
            self.assertIn(operation, inbox.OPERATIONS)
            root = wire.decode(prepared['payload'])
            expected_inbox = 1 if operation.startswith('stranger_') else 0
            self.assertEqual(wire.one(root, 6, 0), expected_inbox)
            body = wire.decode(wire.one(wire.decode(wire.one(root, 8, 2)), inbox.OPERATIONS[operation][1], 2))
            return read(operation, prepared, body)
        return send

    def test_bounded_scan_returns_only_targets_and_never_proves_stranger_status(self):
        def read(operation, prepared, body):
            if operation == 'stranger_conversations':
                self.assertEqual(wire.one(body, 3, 0), 1)
                return reply(operation, prepared, wire.field(2, 0))
            self.assertEqual(wire.one(body, 3, 0), 1)
            cursor = wire.one(body, 2, 0)
            info = (wire.field(1, conversation(OTHER)) + wire.field(2, 1) + wire.field(3, 99)) if cursor == 0 else wire.field(1, conversation())
            return reply(operation, prepared, info)
        missing = '9007199254741111'
        result = inbox.scan(SENDER, [RECEIVER, missing], provider=self.provider, exchange=self.exchange(read))
        self.assertEqual(result['status'], 'checked')
        self.assertEqual(result['targets'][RECEIVER]['status'], 'existing_conversation')
        self.assertEqual(result['targets'][missing]['status'], 'not_observed')
        self.assertEqual(result['targets'][RECEIVER]['conversations'], [CONVERSATION])
        self.assertEqual(result['scopes'][0]['pages'], 2)
        self.assertEqual(self.calls, ['identity', 'conversations', 'conversations', 'stranger_conversations'])
        self.assertFalse(result['contacts_checked'])
        self.assertFalse(result['history_exhaustive'])
        self.assertNotIn(OTHER, json.dumps(result))
        self.assertNotIn('synthetic-', json.dumps(result))

    def test_page_budget_is_partial_and_repeated_cursor_is_a_failure(self):
        def read(operation, prepared, body):
            return reply(operation, prepared, wire.field(2, 1) + wire.field(1 if operation.startswith('stranger') else 3, 5))
        result = inbox.scan(SENDER, [RECEIVER], max_pages=1, provider=self.provider, exchange=self.exchange(read))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(self.calls), 3)
        result = inbox.scan(SENDER, [RECEIVER], provider=self.provider, exchange=self.exchange(read))
        self.assertEqual(result['status'], 'read_failed')
        self.assertEqual(result['error'], 'nonadvancing_cursor')

    def test_protocol_failure_never_becomes_a_successful_empty_list(self):
        for change in ({'1': 100}, {'2': 1}, {'13': int(OTHER)}, {'3': 409}, {'5': 1}, {'6': b''}):
            with self.subTest(change=change):
                self.calls.clear()
                result = inbox.scan(SENDER, [RECEIVER], provider=self.provider,
                    exchange=self.exchange(lambda op, prep, body: reply(op, prep, b'', **change)))
                self.assertEqual(result['status'], 'read_failed')
                self.assertEqual(result['targets'][RECEIVER]['status'], 'unknown')
                self.assertEqual(self.calls, ['identity', 'conversations'])

    def test_stranger_response_requires_inbox_one_not_zero(self):
        def read(operation, prepared, body):
            return reply(operation, prepared, b'', **({'5': 0} if operation == 'stranger_conversations' else {}))
        result = inbox.scan(SENDER, [RECEIVER], provider=self.provider, exchange=self.exchange(read))
        self.assertEqual(result['status'], 'read_failed')
        self.assertFalse(result['evidence'][-1]['inbox_matches'])

    def test_invalid_identity_and_input_never_read_messages(self):
        for targets in ([], [RECEIVER, RECEIVER], [SENDER], [123.0], ['00123']):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                inbox.scan(SENDER, targets, provider=self.provider, exchange=lambda *args: self.fail('network'))
        result = inbox.scan(SENDER, [RECEIVER], provider=self.provider,
                            exchange=lambda *args: (200, 'application/json', b'{"status_code":0,"user":{"uid":"123"}}'))
        self.assertEqual(result['error'], 'identity_check_failed')

    def test_selected_message_page_checks_participants_and_preserves_ids_as_strings(self):
        def read(operation, prepared, body):
            self.assertEqual(operation, 'messages')
            self.assertEqual(wire.one(body, 4, 0), 3)
            self.assertEqual(wire.text(body, 1), CID)
            info = (wire.field(1, message()) + wire.field(1, message(9007199254741002, author=SENDER, text='synthetic outgoing'))
                    + wire.field(1, message(9007199254741003, kind=99)) + wire.field(2, 2) + wire.field(3, 1))
            return reply(operation, prepared, info)
        result = inbox.messages(SENDER, CONVERSATION, provider=self.provider, exchange=self.exchange(read))
        self.assertEqual(result['status'], 'messages_observed')
        self.assertEqual(result['messages'][0]['server_message_id'], '9007199254741001')
        self.assertEqual([m['direction'] for m in result['messages']], ['inbound', 'outbound'])
        self.assertEqual(result['skipped_count'], 1)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['next_cursor'], '2')
        self.assertNotIn('synthetic-hidden-secret', json.dumps(result))
        self.assertFalse(result['read_marker_requested'])
        self.assertFalse(result['can_send'])

    def test_foreign_or_duplicate_message_aborts_whole_page(self):
        for bad in (message(author=OTHER), message(cid='0:1:'+SENDER+':'+OTHER), message(short='999')):
            result = inbox.messages(SENDER, CONVERSATION, provider=self.provider,
                exchange=self.exchange(lambda op, prep, body: reply(op, prep, wire.field(1, message()) + wire.field(1, bad))))
            self.assertEqual(result['status'], 'read_failed')
            self.assertEqual(result['messages'], [])
        result = inbox.messages(SENDER, CONVERSATION, provider=self.provider,
            exchange=self.exchange(lambda op, prep, body: reply(op, prep, wire.field(1, message()) * 2)))
        self.assertEqual(result['error'], 'duplicate_message_id')

    def test_stranger_read_explicitly_keeps_unread_count_and_has_no_fake_cursor(self):
        def read(operation, prepared, body):
            self.assertEqual(operation, 'stranger_messages')
            self.assertEqual(wire.one(body, 2, 0), 0)
            return reply(operation, prepared, wire.field(3, message()) + wire.field(3, message(9007199254741002)))
        result = inbox.messages(SENDER, dict(CONVERSATION, inbox=1), limit=1,
                                provider=self.provider, exchange=self.exchange(read))
        self.assertEqual(result['status'], 'messages_observed')
        self.assertEqual(len(result['messages']), 1)
        self.assertTrue(result['output_truncated'])
        self.assertIsNone(result['has_more'])
        self.assertIsNone(result['next_cursor'])
        self.assertFalse(result['unread_reset_requested'])

    def test_transport_error_does_not_retry_or_expose_exception_text(self):
        def fail(operation, prepared, body):
            raise uid_transport.TransportError('timeout', 'response_headers')
        result = inbox.scan(SENDER, [RECEIVER], provider=self.provider, exchange=self.exchange(fail))
        self.assertEqual(result['status'], 'read_failed')
        self.assertEqual(self.calls, ['identity', 'conversations'])
        self.assertEqual(result['transport']['transport_error'], 'timeout')
        def private_failure(*args): raise RuntimeError('synthetic-private-cookie')
        result = inbox.messages(SENDER, CONVERSATION, provider=self.provider, exchange=self.exchange(private_failure))
        self.assertNotIn('synthetic-private-cookie', json.dumps(result))
        def typed_failure(*args): raise inbox.ReadError('synthetic-private-cookie')
        result = inbox.messages(SENDER, CONVERSATION, provider=self.provider, exchange=self.exchange(typed_failure))
        self.assertEqual(result['error'], 'unrecognized_read_failure')
        self.assertNotIn('synthetic-private-cookie', json.dumps(result))

    def test_identity_transport_diagnostic_survives_without_reading_messages(self):
        for reason, expected in [('timeout', 'transport_failed'), ('connection_failed', 'transport_failed'),
                                 ('tls_verification_failed', 'identity_check_failed')]:
            with self.subTest(reason=reason):
                calls=[]
                def fail(operation, prepared):
                    calls.append(operation)
                    raise uid_transport.TransportError(reason, 'response_headers', http_status=200, response_bytes=0)
                result=inbox.messages(SENDER, CONVERSATION, provider=self.provider, exchange=fail)
                self.assertEqual(calls, ['identity'])
                self.assertEqual(result['error'], expected)
                proof=result['evidence'][0]
                self.assertEqual(proof['operation'], 'identity')
                self.assertFalse(proof['identity_verified'])
                self.assertEqual(proof['identity_reason'], 'transport_failed')
                self.assertEqual(proof['transport_error'], reason)
                self.assertEqual(proof['transport_phase'], 'response_headers')
                self.assertFalse(result['messages'])

    def test_identity_rejections_have_fixed_reasons_and_never_read_inboxes(self):
        cases=[(403, 'application/json', b'{}', 'http_status_rejected'),
               (200, 'text/html', b'synthetic-private-profile', 'unexpected_content_type'),
               (200, 'application/json', b'synthetic-private-profile', 'invalid_json'),
               (200, 'application/json', b'[]', 'invalid_profile'),
               (200, 'application/json', json.dumps({'status_code':409,'user':{'uid':SENDER}}).encode(), 'platform_rejected'),
               (200, 'application/json', json.dumps({'status_code':0,'user':{'uid':OTHER}}).encode(), 'sender_mismatch')]
        for status, mime, raw, reason in cases:
            with self.subTest(reason=reason):
                calls=[]
                def exchange(operation, prepared):
                    calls.append(operation);return status,mime,raw
                result=inbox.scan(SENDER,[RECEIVER],provider=self.provider,exchange=exchange)
                self.assertEqual(calls,['identity']);self.assertEqual(result['error'],'identity_check_failed')
                proof=result['evidence'][0]
                self.assertEqual(proof['identity_reason'],reason);self.assertEqual(proof['http_status'],status)
                self.assertEqual(len(proof['response_sha256']),64)
                self.assertNotIn('synthetic-private-profile',json.dumps(result));self.assertNotIn(OTHER,json.dumps(result))

    def test_identity_success_evidence_is_separate_from_message_evidence(self):
        result=inbox.messages(SENDER,CONVERSATION,provider=self.provider,
            exchange=self.exchange(lambda op,prep,body:reply(op,prep,wire.field(1,message()))))
        self.assertEqual(result['status'],'messages_observed')
        self.assertEqual([p['operation'] for p in result['evidence']],['identity','messages'])
        self.assertTrue(result['evidence'][0]['identity_verified'])
        self.assertEqual(result['evidence'][0]['identity_reason'],'identity_verified')
        self.assertTrue(result['evidence'][1]['sender_matches'])


if __name__ == '__main__':
    unittest.main()
