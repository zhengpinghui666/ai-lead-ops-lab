"""No actual Codex sends, browser, platform requests, production DB or model calls."""
from pathlib import Path
from contextlib import closing
import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
from incident_bridge import Bridge, faults, feedback

THREAD='01a09666-68bb-7860-979d-b3415b851bed'
QUEUE='01a09e2b-3dd4-7a81-a0d7-7cd5d9b59842'


def report(state='running',task=1,reason='network_error'):
    item=dict(state=state,last_task_id=task,task_status=reason)
    if state=='attention':item['issue']='comments:attention'
    return dict(service='running',comments=item,issues=[])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.exe=self.root/'codex.exe';self.exe.write_bytes(b'synthetic fixture')
        self.now=1000.;self.bridge=Bridge(self.root,clock=lambda:self.now)
        self.bridge.configure(THREAD,self.exe)
        self.runner=Mock(return_value=subprocess.CompletedProcess([],0,f'Queued message {QUEUE} for thread {THREAD}.\n',''))

    def tearDown(self):
        self.temp.cleanup()

    def test_manual_pause_and_expected_retry_do_not_notify(self):
        for state in ('paused','running','unconfigured'):
            self.bridge.observe(report(state))
            self.assertIsNone(self.bridge.dispatch(runner=self.runner))
        self.runner.assert_not_called()

    def test_durable_queue_receipt_deduplication_and_resolution(self):
        failure=report('attention')
        self.bridge.observe(failure)
        did=self.bridge.dispatch(runner=self.runner)
        state=self.bridge.status();self.assertFalse(state['receiver_verified'])
        self.assertEqual(state['last_delivery']['status'],'queued')
        other=Bridge(self.root,clock=lambda:self.now)
        other.observe(failure);self.assertIsNone(other.dispatch(runner=self.runner))
        self.runner.assert_called_once()
        with self.assertRaises(ValueError):other.acknowledge(did,'resolved',report())
        other.acknowledge(did,'received');self.assertTrue(other.status()['receiver_verified'])
        with self.assertRaises(ValueError):other.acknowledge(did,'resolved',failure)
        with self.assertRaises(ValueError):other.acknowledge(did,'resolved',{})
        with self.assertRaises(ValueError):other.acknowledge(did,'resolved',dict(report(),issues=['probe:OperationalError']))
        other.acknowledge(did,'resolved',report());other.acknowledge(did,'resolved',report())
        other.observe(failure);self.assertIsNotNone(other.dispatch(runner=self.runner))
        self.assertEqual(self.runner.call_count,2,'A new failure episode may notify after recovery')

    def test_recovered_before_send_and_new_fault_while_busy(self):
        self.bridge.observe(report('attention'));self.bridge.observe(report())
        self.assertIsNone(self.bridge.dispatch(runner=self.runner))
        self.bridge.observe(report('attention',2));did=self.bridge.dispatch(runner=self.runner)
        self.bridge.observe(report('attention',3))
        self.assertIsNone(self.bridge.dispatch(runner=self.runner),'One outstanding delivery at a time')
        self.bridge.acknowledge(did,'received');self.bridge.acknowledge(did,'needs_user')
        self.assertIsNotNone(self.bridge.dispatch(runner=self.runner))

    def test_timeout_unknown_must_not_duplicate_on_restart(self):
        self.bridge.observe(report('attention'))
        runner=Mock(side_effect=subprocess.TimeoutExpired('codex',20,output='SECRET'))
        self.bridge.dispatch(runner=runner)
        self.assertEqual(self.bridge.status()['last_delivery']['status'],'unknown')
        self.assertIsNone(Bridge(self.root).dispatch(runner=self.runner))
        self.runner.assert_not_called()
        self.assertNotIn('SECRET',self.bridge.path.read_bytes().decode('latin1'))

    def test_not_started_retry_and_cooldown(self):
        self.bridge.observe(report('attention'))
        missing=Mock(side_effect=FileNotFoundError('PRIVATE_SENTINEL'))
        did=self.bridge.dispatch(runner=missing)
        self.assertEqual(self.bridge.status()['last_delivery']['status'],'retry')
        self.assertIsNone(self.bridge.dispatch(runner=self.runner))
        self.now+=20
        self.assertEqual(self.bridge.dispatch(runner=self.runner),did)
        self.assertEqual(self.bridge.status()['last_delivery']['status'],'queued')

    def test_metadata_only_prompt_and_no_permission_model_override(self):
        value=report('attention');value['comments']['detail']='PRIVATE_SENTINEL'
        self.bridge.observe(value);self.bridge.dispatch(runner=self.runner)
        args,kw=self.runner.call_args
        command=args[0];text=command[-1]
        self.assertEqual(command[1:5],['queue','--thread',THREAD,'--message'])
        self.assertNotIn('PRIVATE_SENTINEL',text)
        self.assertNotIn('--model',command);self.assertNotIn('--sandbox',command)
        self.assertNotIn('shell',kw)
        self.assertIn('--state received',text)

    def test_service_grace_and_maintenance_hold(self):
        self.bridge.observe({});self.assertIsNone(self.bridge.dispatch(runner=self.runner))
        self.now+=16;self.bridge.hold(30)
        self.assertIsNone(self.bridge.dispatch(runner=self.runner))
        self.now+=31;self.assertIsNotNone(self.bridge.dispatch(runner=self.runner))

    def test_self_test_requires_receipt_and_does_not_resolve_other_faults(self):
        self.bridge.self_test();did=self.bridge.dispatch(runner=self.runner)
        self.bridge.acknowledge(did,'received')
        self.bridge.acknowledge(did,'resolved',report('attention'))
        self.assertEqual(self.bridge.status()['last_delivery']['status'],'resolved')
        self.assertTrue(self.bridge.status()['receiver_verified'])

    def test_old_sending_state_and_wrong_thread_receipt_are_not_success(self):
        self.bridge.observe(report('attention'))
        wrong=Mock(return_value=subprocess.CompletedProcess([],0,f'Queued message {QUEUE} for thread {QUEUE}.','PRIVATE_SENTINEL'))
        self.bridge.dispatch(runner=wrong)
        self.assertEqual(self.bridge.status()['last_delivery']['status'],'unknown')
        self.assertIsNone(self.bridge.dispatch(runner=self.runner))

    def test_group_inbox_faults_coalesce_and_missing_service_avoids_storm(self):
        value=report();value['issues']=['groups:1:attention','groups:2:retry_wait','inbox_sync:1:attention','PRIVATE_SENTINEL']
        self.assertEqual(set(faults(value)),{'groups','inbox'})
        value['service']='stopping';self.assertEqual(set(faults(value)),{'service'})

    def test_interrupted_sender_is_visible_unknown_and_never_replayed(self):
        self.bridge.observe(report('attention'));did=self.bridge.dispatch(runner=self.runner)
        with closing(self.bridge.connect()) as c, c:
            c.execute("UPDATE deliveries SET status='sending' WHERE id=?",(did,))
        self.now+=61
        self.assertIsNone(self.bridge.dispatch(runner=self.runner))
        self.assertEqual(self.bridge.status()['last_delivery']['status'],'unknown')
        self.runner.assert_called_once()

    def test_feedback_requires_fresh_process_and_actual_receiver_receipt(self):
        self.assertEqual(feedback(self.root)['mode'],'local_only')
        import time
        state=dict(heartbeat_at=time.time(),enabled=True,receiver_verified=False,last_delivery={'status':'queued'})
        path=self.bridge.directory/'status.json'
        path.write_text(json.dumps(state));self.assertFalse(feedback(self.root)['codex_push_connected'])
        state['receiver_verified']=True
        path.write_text(json.dumps(state));self.assertFalse(feedback(self.root)['codex_push_connected'],'An older queued receipt does not prove realtime push')
        state.update(mode='event_push',realtime_receiver_verified=True,last_delivery={'status':'resolved','transport':'app_push'})
        path.write_text(json.dumps(state));self.assertTrue(feedback(self.root)['codex_push_connected'])
        state['heartbeat_at']-=60
        path.write_text(json.dumps(state));self.assertFalse(feedback(self.root)['codex_push_connected'])

    def test_app_push_receipt_stays_separate_from_receiver_ack(self):
        self.bridge.configure_app(self.exe,self.exe,r'\\.\pipe\codex-browser-use-test')
        self.bridge.self_test()
        did=self.bridge.dispatch(runner=self.runner,pusher=lambda *a:dict(status='pushed',error=None))
        self.runner.assert_not_called()
        self.assertFalse(self.bridge.status()['realtime_receiver_verified'])
        self.assertIsNone(self.bridge.dispatch(runner=self.runner))
        self.bridge.acknowledge(did,'received')
        self.assertTrue(self.bridge.status()['realtime_receiver_verified'])
        self.bridge.acknowledge(did,'resolved',report())

    def test_app_push_unknown_never_falls_back_to_duplicate_queue(self):
        self.bridge.configure_app(self.exe,self.exe,r'\\.\pipe\codex-browser-use-test')
        self.bridge.self_test()
        self.bridge.dispatch(runner=self.runner,pusher=lambda *a:dict(status='unknown',error='app_push_outcome_unknown'))
        self.runner.assert_not_called()
        self.assertEqual(self.bridge.status()['last_delivery']['status'],'unknown')

    def test_app_unavailable_before_send_uses_existing_queue_as_degraded_delivery(self):
        self.bridge.configure_app(self.exe,self.exe,r'\\.\pipe\codex-browser-use-test')
        self.bridge.self_test()
        self.bridge.dispatch(runner=self.runner,pusher=lambda *a:dict(status='not_sent',error='app_push_unavailable'))
        self.runner.assert_called_once()
        self.assertEqual(self.bridge.status()['last_delivery']['transport'],'queue')

    def test_fast_receiver_ack_during_app_call_is_not_overwritten(self):
        self.bridge.configure_app(self.exe,self.exe,r'\\.\pipe\codex-browser-use-test')
        self.bridge.self_test()
        def receive(*args):
            did=self.bridge.status()['last_delivery']['id']
            self.bridge.acknowledge(did,'received');self.bridge.acknowledge(did,'resolved',report())
            return dict(status='pushed',error=None)
        self.bridge.dispatch(runner=self.runner,pusher=receive)
        self.assertEqual(self.bridge.status()['last_delivery']['status'],'resolved')
        self.assertTrue(self.bridge.status()['realtime_receiver_verified'])


if __name__=='__main__':unittest.main()
