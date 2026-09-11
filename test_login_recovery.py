"""Isolated own-account recovery orchestration; no real browser, SMS or network."""
import io
import json
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import queue
import subprocess
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import clubops as app
import collector
import login_recovery as recovery
import login_relay
import collection_scheduler as scheduler
import monitoring

ORIGIN = 'https://recovery.example.test'
PAIR = {'paired': True, 'ready': True, 'origin': ORIGIN}


class Output:
    def __init__(self, delay=0):
        self.lines = queue.Queue()
        self.delay = delay

    def put(self, value):
        self.lines.put(json.dumps(value) + '\n' if value is not None else '')

    def readline(self, limit):
        if self.delay:
            time.sleep(self.delay)
            self.delay = 0
        return self.lines.get(timeout=3)

    def close(self):
        pass


class Input(io.StringIO):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def flush(self):
        value = json.loads(self.getvalue())
        self.seek(0)
        self.truncate()
        self.owner.receive(value)


class BrowserHelper:
    def __init__(self, mode='normal'):
        self.mode = mode
        self.stdout = Output(.3 if mode == 'early_exit' else 0)
        self.stdin = Input(self)
        self.commands = []
        self.returncode = 0 if mode == 'early_exit' else None
        self.killed = False

    def finish(self, status='completed', **fields):
        self.stdout.put({'type': 'result', 'status': status, **fields})
        self.stdout.put(None)
        self.returncode = 0

    def receive(self, value):
        self.commands.append(value)
        command = value['command']
        if command == 'start':
            if self.mode == 'early_exit':
                self.finish(browser_closed_before_http=True, credential_file_created=True)
            else:
                self.stdout.put({'type': 'status', 'status': 'arming_relay'})
        elif command == 'otp':
            self.stdout.put({'type': 'status', 'status': 'code_filled'})
            self.finish(browser_closed_before_http=self.mode != 'unverified', credential_file_created=True)
        elif command == 'complete':
            self.finish(browser_closed_before_http=True, credential_file_created=True)
        elif command == 'cancel':
            if self.mode == 'hung':
                raise BrokenPipeError()
            self.finish('cancelled')

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('fixture', timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = 1
        self.stdout.put(None)


class LoginRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='login-manager-test-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(app, 'DATA_DIR', Path(self.temp.name)).start()
        patch.object(recovery, 'ACTIVE', None).start()
        patch.object(collector, 'ACTIVE', {}).start()
        patch.object(login_relay, 'state', return_value=PAIR).start()
        patch.object(recovery.sessions, 'status', return_value={'ready': False, 'account': ''}).start()
        self.reuse = patch.object(recovery, '_reuse', return_value=False).start()
        self.remote = Mock(origin=ORIGIN)
        patch.object(login_relay, 'Relay', return_value=self.remote).start()
        app.init()

    def control(self, kind='login'):
        with patch.object(threading.Thread, 'start'):
            recovery.start({'kind': kind})
        control = recovery.ACTIVE
        self.remote.call.side_effect = lambda route, body=None: self.reply(control, route, body)
        return control

    def reply(self, control, route, body):
        if route == 'login':
            return {'id': control['public']['id'], 'expires_at': (time.time() + 180) * 1000,
                    'expires_in_seconds': 180}
        if route == 'take':
            return {'id': control['public']['id'], 'status': 'received',
                    'code': control['test_code'] or '314159'}
        return {'ok': True}

    def run_helper(self, control, helper=None):
        helper = helper or BrowserHelper()
        with patch.object(recovery.runtime, 'dependencies', return_value=('node-fixture', Path(self.temp.name))), \
             patch.object(recovery.subprocess, 'Popen', return_value=helper) as launch:
            recovery._run(control)
        args = launch.call_args
        if args:
            self.assertNotIn('314159', json.dumps(args.args))
        self.assertFalse(recovery.busy())
        return helper

    def assert_not_saved(self, values):
        for file in Path(self.temp.name).rglob('*.json'):
            text = file.read_text(encoding='utf-8')
            for value in values:
                self.assertNotIn(value, text)

    def test_full_protocol_fills_once_then_verifies_and_cleans(self):
        control = self.control()
        helper = self.run_helper(control)
        self.assertEqual([c['command'] for c in helper.commands], ['start', 'ready', 'otp'])
        self.assertEqual(control['public']['status'], 'completed')
        self.assertEqual([c.args[0] for c in self.remote.call.call_args_list], ['login', 'take', 'cancel'])
        self.assert_not_saved(['314159', 'phone_token', 'backend_token'])

    def test_process_exit_does_not_lose_delayed_final_stdout(self):
        control = self.control()
        self.run_helper(control, BrowserHelper('early_exit'))
        self.assertEqual(control['public']['status'], 'completed')
        self.remote.call.assert_not_called()

    def test_browser_result_requires_http_identity_evidence(self):
        control = self.control()
        self.run_helper(control, BrowserHelper('unverified'))
        self.assertEqual(control['public']['status'], 'identity_failed')

    def test_helper_identity_failure_diagnostics_are_bounded_and_redacted(self):
        self.assertEqual(recovery._identity_diagnostic({'preparation_status':'invalid_input',
            'bootstrap_phase':'validation','bootstrap_error':'invalid_client_context','cookie':'SECRET_FIXTURE'}),
            {'preparation_status':'invalid_input','bootstrap_phase':'validation','bootstrap_error':'invalid_client_context'})
        for reason in ('connection_failed','tls_verification_failed','invalid_response','response_exceeds_bound','transport_failed','timeout'):
            self.assertEqual(recovery._identity_diagnostic({'identity_evidence':{'transport_error':reason}}),
                             {'identity_evidence':{'transport_error':reason}})
        control = self.control()
        helper = BrowserHelper('early_exit')
        def diagnosed(_):
            helper.finish('identity_failed', browser_closed_before_http=True, credential_file_created=False,
                          browser_identity_matched=True, sms_step_used=False, code_filled=False,
                          identity_status='http_failed', identity_evidence={'transport_phase':'connect',
                          'transport_error':'connection_failed', 'response_bytes':0, 'cookie':'SECRET_FIXTURE',
                          'http_status':True, 'response_sha256':'SECRET_FIXTURE'})
        helper.receive = diagnosed
        self.run_helper(control, helper)
        self.assertEqual(control['public']['identity_evidence'], {'transport_phase':'connect', 'transport_error':'connection_failed', 'response_bytes':0})
        self.assertFalse(control['public']['sms_step_used'])
        self.assertTrue(control['public']['browser_identity_matched'])
        self.assert_not_saved(['SECRET_FIXTURE'])

    def test_valid_session_reuse_never_launches_or_requests_sms(self):
        control = self.control()
        self.reuse.return_value = True
        with patch.object(recovery.subprocess, 'Popen') as launch:
            recovery._run(control)
        launch.assert_not_called()
        self.remote.call.assert_not_called()
        self.assertEqual(control['public']['status'], 'completed')

    def test_phone_test_uses_only_synthetic_challenge_and_records_success(self):
        control = self.control('phone_test')
        code = control['test_code']
        self.assertIn(code, recovery.state()['test_message'])
        with patch.object(recovery.subprocess, 'Popen') as launch:
            recovery._run(control)
        launch.assert_not_called()
        self.assertEqual(control['public']['status'], 'phone_tested')
        self.assertTrue(recovery.checks()['phone_test_at'])
        self.assertEqual(control['test_code'], '')
        self.assert_not_saved([code])
        self.assertNotIn('test_message', recovery.state())

    def test_wrong_job_code_is_never_filled(self):
        control = self.control()
        def reply(route, body=None):
            result = self.reply(control, route, body)
            if route == 'take':
                result['id'] = 'f' * 32
            return result
        self.remote.call.side_effect = reply
        helper = self.run_helper(control)
        self.assertNotIn('otp', [c['command'] for c in helper.commands])
        self.assertEqual(control['public']['status'], 'browser_failed')

    def test_relay_poll_failure_keeps_page_for_manual_completion(self):
        control = self.control()
        def reply(route, body=None):
            if route == 'take':
                control['manual'].set()
                raise login_relay.RelayError('fixture')
            return self.reply(control, route, body)
        self.remote.call.side_effect = reply
        helper = self.run_helper(control)
        self.assertEqual([c['command'] for c in helper.commands], ['start', 'ready', 'complete'])
        self.assertEqual(control['public']['status'], 'completed')

    def test_lost_login_response_still_cancels_owned_remote_job(self):
        control = self.control()
        def reply(route, body=None):
            if route == 'login':
                raise login_relay.RelayError('private-fixture',reason='timeout',route='login',stage='response')
            return {'ok': True}
        self.remote.call.side_effect = reply
        self.run_helper(control)
        self.assertEqual(control['public']['status'], 'relay_unavailable')
        self.assertEqual(control['public']['relay_error'], {'reason':'timeout','route':'login','stage':'response'})
        self.assertNotIn('private-fixture', json.dumps(recovery.state()))
        self.assertEqual([c.args[0] for c in self.remote.call.call_args_list], ['login', 'cancel'])

    def test_cancel_stops_owned_hung_helper_and_clears_local_job(self):
        control = self.control()
        def reply(route, body=None):
            if route == 'take':
                recovery.command({'id': control['public']['id']}, 'cancel')
                return {'status': 'waiting'}
            return self.reply(control, route, body)
        self.remote.call.side_effect = reply
        helper = self.run_helper(control, BrowserHelper('hung'))
        self.assertTrue(helper.killed)
        self.assertEqual(control['public']['status'], 'cancelled')

    def test_cleanup_failure_remains_visible_without_secret_details(self):
        control = self.control('phone_test')
        def reply(route, body=None):
            if route == 'cancel':
                raise login_relay.RelayError('private-fixture-error')
            return self.reply(control, route, body)
        self.remote.call.side_effect = reply
        recovery._run(control)
        self.assertTrue(control['public']['relay_cleanup_pending'])
        self.assertEqual(control['public']['status'], 'phone_tested')
        self.assert_not_saved(['private-fixture-error'])

    def test_start_failure_rolls_back_busy_and_leaves_failure_record(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('private-fixture-error')):
            with self.assertRaisesRegex(ValueError, '未能启动'):
                recovery.start({})
        self.assertFalse(recovery.busy())
        self.assertEqual(recovery.state()['history'][-1]['status'], 'browser_failed')
        self.assert_not_saved(['private-fixture-error'])

    def test_corrupt_local_recovery_file_does_not_break_public_state(self):
        recovery._write(recovery._file('jobs.json'), {'wrong': 'shape'})
        recovery.recover()
        self.assertFalse(recovery.state()['available'])
        self.assertFalse(recovery.busy())

    def test_collection_and_login_admissions_are_mutually_exclusive(self):
        control = self.control()
        with self.assertRaisesRegex(ValueError, '恢复登录'):
            self.failed_task()
        with self.assertRaisesRegex(ValueError, '已有登录'):
            recovery.start({})
        control['cancel'].set()
        self.run_helper(control)

    def test_wrong_synthetic_code_does_not_confirm_phone(self):
        control = self.control('phone_test')
        def reply(route, body=None):
            value = self.reply(control, route, body)
            if route == 'take':
                value['code'] = '0000'  # Challenge is always six digits.
            return value
        self.remote.call.side_effect = reply
        recovery._run(control)
        self.assertEqual(control['public']['status'], 'identity_failed')
        self.assertNotIn('phone_test_at', recovery.checks())

    def test_expired_phone_test_stops_without_launching_browser(self):
        control = self.control('phone_test')
        wall = time.time()
        clock = Mock()
        clock.time.return_value = wall
        clock.monotonic.return_value = 0
        def reply(route, body=None):
            if route == 'login':
                return {'id': control['public']['id'], 'expires_at': (wall + 180) * 1000,
                        'expires_in_seconds': 180}
            if route == 'take':
                clock.time.return_value = wall + 181
                clock.monotonic.return_value = 181
                return {'status': 'waiting'}
            return {'status': 'cleared'}
        self.remote.call.side_effect = reply
        # The second poll is due after two seconds; advance the fixture clock
        # when the event queue waits, without sleeping for a real code timeout.
        real_get = queue.Queue.get
        def get(q, *args, **kwargs):
            if clock.monotonic.return_value:
                clock.monotonic.return_value = 184
            return real_get(q, *args, **kwargs)
        with patch.object(recovery, 'time', clock), patch.object(queue.Queue, 'get', get):
            recovery._run(control)
        self.assertEqual(control['public']['status'], 'timeout')
        self.assertFalse(recovery.busy())

    def test_wall_clock_skew_and_jump_do_not_expire_a_fresh_phone_response(self):
        for skew in (-3600,3600):
            with self.subTest(skew=skew):
                control=self.control('phone_test')
                clock=Mock();clock.time.return_value=1800000000+skew;clock.monotonic.return_value=10
                def reply(route,body=None):
                    if route=='login':
                        return {'id':control['public']['id'],'expires_at':1800000180000,'expires_in_seconds':179}
                    if route=='take':
                        clock.time.return_value+=86400
                        clock.monotonic.return_value=12
                    return self.reply(control,route,body)
                self.remote.call.side_effect=reply
                with patch.object(recovery,'time',clock): recovery._run(control)
                self.assertEqual(control['public']['status'],'phone_tested')

    def test_late_received_code_is_not_accepted_after_monotonic_expiry(self):
        control=self.control('phone_test')
        code=control['test_code']
        clock=Mock();clock.time.return_value=1800000000;clock.monotonic.return_value=0
        def reply(route,body=None):
            if route=='take':
                clock.time.return_value-=86400
                clock.monotonic.return_value=181
            return self.reply(control,route,body)
        self.remote.call.side_effect=reply
        with patch.object(recovery,'time',clock): recovery._run(control)
        self.assertEqual(control['public']['status'],'timeout')
        self.assertNotIn('phone_test_at',recovery.checks())
        self.assert_not_saved([code])

    def test_cancel_during_poll_discards_received_code_before_sending_to_browser(self):
        control=self.control()
        def reply(route,body=None):
            if route=='take': control['cancel'].set()
            return self.reply(control,route,body)
        self.remote.call.side_effect=reply
        helper=self.run_helper(control)
        self.assertEqual(control['public']['status'],'cancelled')
        self.assertNotIn('otp',[message['command'] for message in helper.commands])

    def test_cancel_during_arm_never_signals_browser_to_request_sms(self):
        control=self.control()
        def reply(route,body=None):
            if route=='login': control['cancel'].set()
            return self.reply(control,route,body)
        self.remote.call.side_effect=reply
        helper=self.run_helper(control)
        self.assertEqual(control['public']['status'],'cancelled')
        self.assertEqual([message['command'] for message in helper.commands],['start','cancel'])
        self.assertEqual([c.args[0] for c in self.remote.call.call_args_list],['login','cancel'])

    def test_slow_arm_and_invalid_lifetimes_never_signal_ready(self):
        for remaining in (float('nan'),float('inf'),181,True,None,1):
            with self.subTest(remaining=remaining):
                control=self.control('phone_test')
                clock=Mock();clock.time.return_value=1800000000;clock.monotonic.return_value=0
                def reply(route,body=None):
                    if route=='login':
                        clock.monotonic.return_value=2
                        return {'id':control['public']['id'],'expires_in_seconds':remaining}
                    return {'status':'cleared'}
                self.remote.call.reset_mock();self.remote.call.side_effect=reply
                with patch.object(recovery,'time',clock): recovery._run(control)
                self.assertIn(control['public']['status'],('timeout','browser_failed'))
                self.assertEqual([c.args[0] for c in self.remote.call.call_args_list],['login','cancel'])
                self.assertNotIn('phone_test_at',recovery.checks())

    def test_save_never_starts_recovery_and_auto_requires_phone_evidence(self):
        with self.assertRaises(ValueError):
            recovery.save({**recovery.DEFAULTS, 'auto_recover': True})
        recovery._write(recovery._file('checks.json'), {'origin': ORIGIN, 'health_at': time.time(), 'phone_test_at': time.time()})
        with patch.object(threading.Thread, 'start') as launch:
            recovery.save({**recovery.DEFAULTS, 'auto_recover': True})
        launch.assert_not_called()
        self.assertTrue(recovery.config()['auto_recover'])

    def failed_task(self, status='needs_login', request_id='fixture-task'):
        with patch.object(threading.Thread, 'start'):
            task = collector.start({'kind': 'video', 'target': 'https://www.douyin.com/video/7600000000000000001',
                'request_id': request_id, 'transport': 'http', 'comment_limit': 7, 'video_limit': 1},
                lookback_hours=24, include_keywords='陪玩', exclude_keywords='招募', recovery_since='2026-09-01T00:00:00+00:00')
        collector.ACTIVE.clear()
        collector.update(task['id'], status=status, finished_at=app.now())
        return task['id']

    def test_search_verification_cannot_trigger_login_recovery(self):
        task_id = self.failed_task('needs_verification')
        with self.assertRaisesRegex(ValueError, '只恢复'):
            recovery.start({'task_id': task_id})
        self.assertFalse(recovery.busy())

    def test_resume_preserves_original_scope_cutoff_and_is_idempotent(self):
        task_id = self.failed_task()
        with patch.object(threading.Thread, 'start'):
            recovery.start({'task_id': task_id})
        control = recovery.ACTIVE
        recovery.ACTIVE = None
        with patch.object(threading.Thread, 'start'):
            resumed = recovery._resume_task(control)
            same = recovery._resume_task(control)
        self.assertEqual(resumed['id'], same['id'])
        with app.db() as db:
            original = db.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            actual = db.execute('SELECT * FROM collection_tasks WHERE id=?', (resumed['id'],)).fetchone()
        for key in ('kind', 'target', 'transport', 'comment_limit', 'video_limit', 'comment_since', 'lookback_hours', 'include_keywords', 'exclude_keywords'):
            self.assertEqual(actual[key], original[key], key)

    def test_auto_recovery_has_cooldown_and_does_not_repeat_task_chain(self):
        task_id = self.failed_task()
        recovery._write(app.DATA_DIR / recovery.CONFIG, {**recovery.DEFAULTS, 'auto_recover': True})
        recovery._write(recovery._file('jobs.json'), [{'id': 'prior', 'status': 'completed', 'task_id': task_id,
            'resumed_task_id': task_id + 1, 'updated_at': time.time() - 1000}])
        self.assertIsNone(recovery.start({'task_id': task_id}, automatic=True))
        recovery._write(recovery._file('jobs.json'), [{'id': 'prior', 'status': 'cancelled', 'task_id': 999, 'updated_at': time.time()}])
        self.assertIsNone(recovery.start({'task_id': task_id}, automatic=True))
        self.assertFalse(recovery.busy())

    def test_restart_settles_waiting_task_without_starting_browser(self):
        control = self.control()
        recovery.ACTIVE = None
        with patch.object(threading.Thread, 'start') as launch:
            recovery.recover()
        launch.assert_not_called()
        self.assertEqual(recovery.state()['history'][-1]['status'], 'interrupted')
        with self.assertRaises(ValueError):
            recovery.command({'id': control['public']['id']}, 'complete')

    def monitor_failure(self, *, settle=True, finite=False, checkpoints=False):
        baseline = self.failed_task('completed', 'synthetic-http-baseline-' + str(time.time_ns()))
        collector.update(baseline, comments=1)
        body = {'kind': 'video', 'target': '7600000000000000001\n7600000000000000002',
                'transport': 'http', 'video_limit': 2, 'comment_limit': 7, 'page_concurrency': 1,
                'interval_seconds': 600, 'lookback_hours': 24, 'include_keywords': '陪玩', 'exclude_keywords': '招募'}
        if finite:
            plan_id = scheduler.save({**body, 'run_limit': 1})['id']
            scheduler.command(plan_id, 'start')
        else:
            plan_id = monitoring.save(body)['id']
            monitoring.command('start')
        with patch.object(threading.Thread, 'start'):
            task_id = scheduler.tick()
        self.assertIsNotNone(task_id)
        if checkpoints:
            collector.checkpoint(task_id, {'type': 'targets', 'records': [
                {'video_id': '7600000000000000001'}, {'video_id': '7600000000000000002'}]})
            with app.db() as db:
                db.execute("UPDATE collection_checkpoints SET status='done' WHERE task_id=? AND video_id='7600000000000000001'", (task_id,))
        collector.ACTIVE.clear()
        collector.update(task_id, status='session_expired', finished_at=app.now())
        if settle:
            scheduler.tick()
        with patch.object(threading.Thread, 'start'):
            recovery.start({'task_id': task_id})
        control = recovery.ACTIVE
        self.remote.call.side_effect = lambda route, body=None: self.reply(control, route, body)
        return plan_id, task_id, control

    def run_collection_recovery(self, control, helper=None):
        # Only the login helper protocol runs. Collector worker is an inert mock.
        with patch.object(collector, 'run') as worker:
            self.run_helper(control, helper)
        return worker

    def test_login_continuation_rejoins_monitor_and_next_batch_keeps_scope(self):
        for settled in (False, True):
            with self.subTest(already_settled=settled):
                # Each branch gets a separate monitor activation and failure.
                if monitoring.state()['id']:
                    monitoring.command('stop')
                    collector.ACTIVE.clear()
                    with app.db() as db:
                        # Retain IDs and prior batch request keys as production does.
                        db.execute('UPDATE collection_plans SET continuous=0')
                plan_id, task_id, control = self.monitor_failure(settle=settled)
                worker = self.run_collection_recovery(control)
                child = control['public']['resumed_task_id']
                worker.assert_called_once()
                plan = next(p for p in scheduler.state() if p['id'] == plan_id)
                self.assertEqual((plan['last_task_id'], plan['run_count'], plan['settled_count'], plan['status']), (child, 1, 0, 'running'))
                with app.db() as db:
                    old = dict(db.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone())
                    new = dict(db.execute('SELECT * FROM collection_tasks WHERE id=?', (child,)).fetchone())
                for key in ('kind','target','transport','video_limit','comment_limit','page_concurrency','comment_since','lookback_hours','include_keywords','exclude_keywords'):
                    self.assertEqual(new[key], old[key], key)
                # Same recovery request cannot schedule a second child or change counters.
                self.assertEqual(recovery._resume_task(control)['id'], child)
                collector.ACTIVE.clear()
                collector.update(child, status='completed', finished_at='2026-09-11T00:00:00+00:00')
                self.assertIsNone(scheduler.tick('2026-09-11T00:09:59+00:00'))
                with patch.object(threading.Thread, 'start'):
                    next_id = scheduler.tick('2026-09-11T00:10:00+00:00')
                self.assertIsNotNone(next_id)
                with app.db() as db:
                    next_task = dict(db.execute('SELECT * FROM collection_tasks WHERE id=?', (next_id,)).fetchone())
                for key in ('target','transport','video_limit','comment_limit','include_keywords','exclude_keywords'):
                    self.assertEqual(next_task[key], old[key], key)
                self.assertEqual(monitoring.state()['run_count'], 2)

    def test_finite_plan_recovery_does_not_spend_or_expand_run_budget(self):
        plan_id, task_id, control = self.monitor_failure(finite=True)
        self.run_collection_recovery(control)
        child = control['public']['resumed_task_id']
        collector.ACTIVE.clear()
        collector.update(child, status='completed', finished_at=app.now())
        self.assertIsNone(scheduler.tick())
        plan = next(p for p in scheduler.state() if p['id'] == plan_id)
        self.assertEqual((plan['run_count'], plan['settled_count'], plan['status']), (1, 1, 'completed'))

    def test_checkpoint_recovery_only_reads_unfinished_videos_then_restores_pool(self):
        plan_id, task_id, control = self.monitor_failure(checkpoints=True)
        self.run_collection_recovery(control)
        child = control['public']['resumed_task_id']
        with app.db() as db:
            rows = db.execute('SELECT video_id FROM collection_checkpoints WHERE task_id=?', (child,)).fetchall()
            row = db.execute('SELECT video_limit FROM collection_tasks WHERE id=?', (child,)).fetchone()
        self.assertEqual([r[0] for r in rows], ['7600000000000000002'])
        self.assertEqual(row[0], 1)
        self.assertEqual(monitoring.state()['video_limit'], 2)

    def test_stop_during_login_rolls_back_child_and_never_reopens_monitor(self):
        _, _, control = self.monitor_failure()
        with app.db() as db:
            count = db.execute('SELECT COUNT(*) FROM collection_tasks').fetchone()[0]
        monitoring.command('stop')
        worker = self.run_collection_recovery(control)
        worker.assert_not_called()
        self.assertEqual(control['public']['status'], 'resume_pending')
        self.assertFalse(monitoring.state()['enabled'])
        with app.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_tasks').fetchone()[0], count)

    def test_restart_invalidates_attention_plan_continuation(self):
        _, _, control = self.monitor_failure()
        scheduler.recover()
        worker = self.run_collection_recovery(control)
        worker.assert_not_called()
        self.assertEqual(control['public']['status'], 'resume_pending')
        self.assertFalse(monitoring.state()['enabled'])

    def test_scheduler_waits_for_login_without_failing_other_plans(self):
        baseline = self.failed_task('completed')
        collector.update(baseline, comments=1)
        plan_id = scheduler.save({'kind':'video','target':'7600000000000000001','transport':'http'})['id']
        scheduler.command(plan_id, 'start')
        control = self.control()
        with patch.object(collector, 'start') as dispatch:
            self.assertIsNone(scheduler.tick())
        dispatch.assert_not_called()
        self.assertEqual(scheduler.state()[0]['status'], 'running')
        control['cancel'].set()
        self.run_helper(control)

    def test_login_failure_does_not_rejoin_monitor(self):
        _, _, control = self.monitor_failure()
        worker = self.run_collection_recovery(control, BrowserHelper('unverified'))
        worker.assert_not_called()
        self.assertEqual(control['public']['status'], 'identity_failed')
        self.assertFalse(monitoring.state()['enabled'])

    def test_paused_plan_rejects_automatic_login_but_manual_batch_stays_paused(self):
        _, task_id, control = self.monitor_failure()
        control['cancel'].set()
        self.run_helper(control)
        monitoring.command('stop')
        recovery._write(app.DATA_DIR/recovery.CONFIG, {**recovery.DEFAULTS, 'auto_recover': True})
        self.assertIsNone(recovery.start({'task_id': task_id}, automatic=True))
        with patch.object(threading.Thread, 'start'):
            recovery.start({'task_id': task_id})
        control = recovery.ACTIVE
        self.remote.call.side_effect = lambda route, body=None: self.reply(control, route, body)
        self.run_collection_recovery(control)
        self.assertIn('resumed_task_id', control['public'])
        self.assertFalse(monitoring.state()['enabled'])

    def test_monitor_simulated_day_crosses_login_failure_without_catchup_burst(self):
        # Advances a synthetic clock; does not claim 24 hours of platform uptime.
        start = datetime.fromisoformat('2026-09-11T00:00:00+00:00')
        with patch.object(app, 'now', return_value=start.isoformat()) as clock:
            baseline = self.failed_task('completed')
            collector.update(baseline, comments=1)
            monitoring.save({'kind':'video', 'target':'7600000000000000001', 'transport':'http',
                             'interval_seconds':600, 'lookback_hours':24})
            monitoring.command('start')
            for slot in range(145):
                clock.return_value = (start+timedelta(minutes=10*slot)).isoformat()
                with patch.object(threading.Thread, 'start'):
                    task_id = scheduler.tick()
                self.assertIsNotNone(task_id, slot)
                collector.ACTIVE.clear()
                if slot == 72:
                    collector.update(task_id, status='session_expired', finished_at=app.now())
                    scheduler.tick()
                    with patch.object(threading.Thread, 'start'):
                        recovery.start({'task_id':task_id})
                    control = recovery.ACTIVE
                    self.remote.call.side_effect = lambda route, body=None: self.reply(control, route, body)
                    self.run_collection_recovery(control)
                    task_id = control['public']['resumed_task_id']
                    collector.ACTIVE.clear()
                collector.update(task_id, status='completed', finished_at=app.now())
                self.assertIsNone(scheduler.tick())
                self.assertIsNone(scheduler.tick())
            plan = monitoring.state()
            self.assertEqual((plan['run_count'], plan['settled_count'], plan['enabled']), (145, 145, True))
            clock.return_value = (start+timedelta(hours=27)).isoformat()
            with patch.object(threading.Thread, 'start'):
                self.assertIsNotNone(scheduler.tick())
            self.assertIsNone(scheduler.tick())
            self.assertEqual(monitoring.state()['run_count'], 146)

    def test_plan_intent_migration_backs_up_and_preserves_existing_rows(self):
        monitoring.save({'kind':'video','target':'7600000000000000001','transport':'http'})
        with app.db() as db:
            db.execute('ALTER TABLE collection_plans DROP COLUMN intent_version')
            original = dict(db.execute('SELECT * FROM collection_plans').fetchone())
        app.init()
        backups = list((app.DATA_DIR/'backups').glob('*before-monitor-recovery-*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(dict(db.execute('SELECT * FROM collection_plans').fetchone()), original)
        with app.db() as db:
            current = dict(db.execute('SELECT * FROM collection_plans').fetchone())
        self.assertEqual(current.pop('intent_version'), 0)
        self.assertEqual(current, original)
        app.init()
        self.assertEqual(len(list((app.DATA_DIR/'backups').glob('*before-monitor-recovery-*.bak'))), 1)


if __name__ == '__main__':
    unittest.main()
