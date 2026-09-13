"""Synthetic account/IM responses only; real local encrypted storage and locks."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import clubops as app
import runtime
import uid_messaging
import uid_protocol as wire
import uid_session as session
import uid_session_renewal as renewal
import uid_transport
from test_uid_session import data, SENDER, RECEIVER


def response(operation, prepared, *, bad=None):
    if operation == 'identity':
        return 200, 'application/json', json.dumps({'status_code': 0, 'user': {'uid': RECEIVER if bad == 'identity' else SENDER}}).encode()
    assert operation == 'im_check', 'renewal must never send, create or join'
    request = wire.decode(prepared['payload'])
    query = wire.decode(wire.one(wire.decode(wire.one(request, 8, 2)), 1000, 2))
    assert wire.one(query, 2, 0) == 1
    raw = (wire.field(1, 1001) + wire.field(2, wire.one(request, 2, 0) + (1 if bad == 'sequence' else 0))
           + wire.field(3, 409 if bad == 'business' else 0) + wire.field(4, 'OK')
           + wire.field(5, 0 if bad == 'inbox' else 1) + wire.field(13, int(RECEIVER if bad == 'im_uid' else SENDER))
           + wire.field(6, wire.field(1000, b'')))
    return 200, 'application/x-protobuf', raw


class RevalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'session.dpapi'
        self.value = data()
        self.value['captured_at'] -= session.MAX_AGE + 10
        self.store()

    def store(self):
        with runtime.data_lock(self.path.parent):
            session.save(self.value, self.path)

    def test_expired_credentials_only_reenter_after_two_bound_read_checks(self):
        with self.assertRaises(ValueError):
            session.Provider(path=self.path).current()
        calls = []
        def exchange(op, prepared):
            calls.append(op)
            self.assertFalse(session.load(self.path, check_age=False)['im_verified'])
            with self.assertRaises(RuntimeError):
                with runtime.data_lock(self.path.parent):
                    self.fail('credential lock must exclude parallel bootstrap')
            return response(op, prepared)
        result = session.revalidate(SENDER, path=self.path, exchange=exchange)
        self.assertEqual(calls, ['identity', 'im_check'])
        self.assertEqual(result['status'], 'im_read_verified')
        current = session.Provider(path=self.path).current()
        self.assertEqual(current['captured_at'], self.value['captured_at'])
        self.assertGreater(current['last_verified_at'], current['captured_at'])
        for key in ('account', 'context', 'headers', 'identity_cookie', 'sender_uid'):
            self.assertEqual(current[key], self.value[key])
        self.assertEqual(current['sequence'], self.value['sequence'] + 1)
        self.assertFalse(result['can_send'])
        self.assertNotIn('synthetic-', json.dumps(result))
        self.assertNotIn(b'synthetic-', self.path.read_bytes())
        self.assertTrue(json.loads(self.path.with_name('status.json').read_text('utf-8'))['im_read_verified'])

    def test_any_failed_binding_revokes_old_proof_without_advancing_freshness(self):
        for bad in ('identity', 'sequence', 'business', 'inbox', 'im_uid'):
            with self.subTest(bad=bad):
                self.store()
                result = session.revalidate(SENDER, path=self.path, exchange=lambda op, p: response(op, p, bad=bad))
                self.assertNotEqual(result['status'], 'im_read_verified')
                current = session.load(self.path, check_age=False)
                self.assertFalse(current['im_verified'])
                self.assertNotIn('last_verified_at', current)
                self.assertEqual(current['captured_at'], self.value['captured_at'])
                self.assertEqual(result['http_attempts'], 1 if bad == 'identity' else 2)
                self.assertFalse(json.loads(self.path.with_name('status.json').read_text('utf-8'))['im_read_verified'])

    def test_provider_cannot_use_old_context_for_writes_or_unbounded_reads(self):
        provider = session.ReadOnlyRevalidationProvider(self.value)
        for op in ('create', 'send', 'group_join', 'conversations', 'messages', 'stranger_messages'):
            with self.assertRaises(ValueError):
                provider.prepare(op, b'', {'sender_uid': SENDER})
        with self.assertRaises(ValueError):
            provider.ticket({}, SENDER, RECEIVER)
        with self.assertRaises(ValueError):
            provider.prepare('im_check', wire.field(1000, wire.field(2, 20)), dict(sender_uid=SENDER, command=1001))

    def test_timeout_and_exception_do_not_leak_secrets_or_leave_send_permission(self):
        for exc in (uid_transport.TransportError('timeout', 'connect'), ValueError('synthetic-secret')):
            self.store()
            with patch.object(uid_transport, 'request', side_effect=exc):
                result = session.revalidate(SENDER, path=self.path)
            self.assertNotEqual(result['status'], 'im_read_verified')
            self.assertNotIn('synthetic', json.dumps(result))
            self.assertFalse(session.load(self.path, check_age=False)['im_verified'])

    def test_cancel_after_im_response_never_renews(self):
        stop = threading.Event()
        def exchange(op, p):
            if op == 'im_check':
                stop.set()
            return response(op, p)
        result = session.revalidate(SENDER, path=self.path, exchange=exchange, cancel=stop)
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(session.load(self.path, check_age=False)['im_verified'])
        with patch.object(uid_transport, 'request') as request:
            session.revalidate(SENDER, path=self.path, cancel=stop)
            request.assert_not_called()

    def test_account_change_does_not_overwrite_or_query_other_credentials(self):
        before = self.path.read_bytes()
        with patch.object(uid_transport, 'request') as request:
            self.assertEqual(session.revalidate(RECEIVER, path=self.path)['status'], 'account_mismatch')
            request.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_verification_timestamp_validation_and_new_expiry(self):
        for t in (True, float('nan'), time.time() + 120, self.value['captured_at'] - 1):
            with self.assertRaises(ValueError):
                session.validate({**self.value, 'last_verified_at': t}, check_age=False)
        refreshed = {**self.value, 'last_verified_at': time.time()}
        session.validate(refreshed)
        with patch.object(session.time, 'time', return_value=refreshed['last_verified_at'] + session.MAX_AGE + 1):
            with self.assertRaises(ValueError):
                session.validate(refreshed)


class RenewalWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        for obj, key, value in ((app, 'DATA_DIR', self.directory), (renewal, 'STOP', threading.Event()),
                                (renewal, 'ACTIVE', False), (renewal, 'THREAD', None)):
            p = patch.object(obj, key, value)
            p.start()
            self.addCleanup(p.stop)
        app.init()
        self.path = self.directory / 'private' / 'uid-http' / 'session.dpapi'
        self.value = data()
        self.value['captured_at'] -= session.MAX_AGE - 100
        self.store()
        (self.directory / 'uid-http.json').write_text(json.dumps(dict(enabled=True, sender_uid=SENDER,
            provider_file=str(Path(session.__file__).resolve()), allowed_recipient_uids=[RECEIVER])), 'utf-8')
        with app.db() as c:
            c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('intent_outreach_policy', json.dumps(dict(enabled=True, sender_uid=SENDER))))

    def store(self):
        with runtime.data_lock(self.path.parent):
            session.save(self.value, self.path)

    def tick(self, exchange=response):
        with patch.object(uid_transport, 'request', side_effect=exchange) as request:
            renewal.tick()
        return request

    def test_due_check_is_serialized_and_success_waits_until_next_expiry(self):
        def exchange(op, p):
            self.assertTrue(uid_messaging.GUARD.locked())
            self.assertTrue(renewal.ACTIVE)
            self.assertTrue(renewal.pending())
            return response(op, p)
        request = self.tick(exchange)
        self.assertEqual(request.call_count, 2)
        state = renewal.state()
        self.assertEqual(state['status'], 'waiting')
        self.assertEqual(state['failures'], 0)
        self.assertGreater(state['next_run_at'], time.time() + 40000)
        self.assertNotIn('generation', state)
        self.assertNotIn('synthetic-', json.dumps(state))
        self.assertEqual(self.tick().call_count, 0)
        self.assertFalse(uid_messaging.GUARD.locked())
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0], 0)

    def test_network_failures_have_persisted_bounded_backoff_and_no_hot_loop(self):
        current = time.time()
        for attempt in range(4):
            with patch.object(renewal.time, 'time', return_value=current):
                request = self.tick(lambda *a: (_ for _ in ()).throw(uid_transport.TransportError('timeout', 'connect')))
                self.assertEqual(request.call_count, 1)
                state = renewal.state()
                self.assertEqual(state['failures'], attempt + 1)
                self.assertEqual(state['status'], 'retry_wait' if attempt < 3 else 'attention')
                self.assertEqual(self.tick().call_count, 0)
                renewal.recover()
                self.assertEqual(renewal.state()['failures'], attempt + 1)
                if attempt < 3:
                    self.assertEqual(state['next_run_at'] - current, renewal.RETRIES[attempt])
                    current = state['next_run_at']
        self.assertFalse(session.load(self.path, check_age=False)['im_verified'])

    def test_definite_rejection_stops_until_new_credentials_are_proved(self):
        self.tick(lambda op, p: response(op, p, bad='business'))
        self.assertEqual(renewal.state()['status'], 'attention')
        self.assertEqual(self.tick().call_count, 0)
        self.value['captured_at'] = time.time()
        self.store()
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(renewal.state()['status'], 'waiting')

    def test_stop_busy_and_disabled_work_never_issue_requests(self):
        renewal.STOP.set()
        self.assertEqual(self.tick().call_count, 0)
        renewal.STOP.clear()
        with uid_messaging.GUARD:
            self.assertEqual(self.tick().call_count, 0)
        import intent_outreach
        with intent_outreach.GUARD:
            self.assertEqual(self.tick().call_count, 0)
        with app.db() as c:
            c.execute("UPDATE settings SET value='{}' WHERE key='intent_outreach_policy'")
        self.assertEqual(self.tick().call_count, 0)

    def test_retry_wait_defers_existing_jobs_instead_of_disabling_them(self):
        renewal.write(dict(status='retry_wait'))
        import intent_outreach, uid_inbox_sync, group_monitor
        with patch.object(intent_outreach, 'candidate') as candidate, patch.object(uid_inbox_sync.inbox, 'read') as read, patch.object(group_monitor, 'discover') as discover:
            intent_outreach.tick()
            uid_inbox_sync.tick()
            group_monitor.tick()
            candidate.assert_not_called()
            read.assert_not_called()
            discover.assert_not_called()

    def test_crash_does_not_reset_attempt_budget(self):
        for failures in range(4):
            renewal.write(dict(status='checking', failures=failures, generation=renewal.generation(self.value)))
            renewal.recover()
            state = renewal.state()
            self.assertEqual(state['failures'], failures + 1)
            self.assertEqual(state['status'], 'retry_wait' if failures < 3 else 'attention')

    def test_service_stop_rejects_inflight_renewal_without_interruption(self):
        import server
        httpd = server.LocalHTTPServer(('127.0.0.1', 0), server.Handler)
        self.addCleanup(httpd.server_close)
        httpd.active_writes = 1
        def exchange(op, p):
            with self.assertRaises(ValueError):
                httpd.prepare_stop()
            self.assertFalse(httpd.stopping)
            self.assertFalse(renewal.STOP.is_set())
            return response(op, p)
        self.assertEqual(self.tick(exchange).call_count, 2)


if __name__ == '__main__':
    unittest.main()
