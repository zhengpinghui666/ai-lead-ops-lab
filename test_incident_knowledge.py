"""All cases use isolated SQLite and fake send transport; no platform or customer activity."""
from contextlib import closing
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from incident_bridge import Bridge
from test_incident_bridge import report, THREAD, QUEUE


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patch=patch('incident_bridge.BASE',self.root);self.patch.start()
        (self.root/'artifacts').mkdir()
        (self.root/'source.py').write_text('fixed implementation',encoding='utf-8')
        (self.root/'source-files.json').write_text('["source.py"]',encoding='utf-8')
        (self.root/'artifacts/test.log').write_text('synthetic regression OK',encoding='utf-8')
        (self.root/'artifacts/runtime.json').write_text('{"completed":5,"next":6}',encoding='utf-8')
        exe=self.root/'codex.exe';exe.write_bytes(b'fixture')
        self.now=1000.;self.bridge=Bridge(self.root/'data',clock=lambda:self.now)
        self.bridge.configure(THREAD,exe)
        self.runner=Mock(return_value=subprocess.CompletedProcess([],0,f'Queued message {QUEUE} for thread {THREAD}.',''))

    def tearDown(self):
        self.patch.stop();self.temp.cleanup()

    def incident(self,task=1):
        self.bridge.observe(report('attention',task))
        did=self.bridge.dispatch(runner=self.runner)
        self.bridge.acknowledge(did,'received')
        iid=self.bridge.reviews(did)['incidents'][0]['incident_id']
        return did,iid

    def record(self,iid,**kw):
        value=dict(incident_id=iid,cause_key='fixture-connection-phase',kind='software',disposition='fixed',
            root_cause='Synthetic exact connection phase failed.',match_conditions='Same phase and same fixture diagnostic, not all network errors.',
            remedy='Apply the narrowly scoped synthetic correction.',prevention='Keep regression coverage for phase matching and real gate exclusions.',
            next_action='',regressions=[dict(path='artifacts/test.log',result='passed')],
            runtime_evidence=[dict(path='artifacts/runtime.json')],changes=['source.py'])
        value.update(kw);return value

    def test_missing_postmortem_cannot_close_healthy_incident(self):
        did,iid=self.incident()
        with self.assertRaisesRegex(ValueError,'Record root cause'):
            self.bridge.acknowledge(did,'resolved',report())
        self.assertEqual(self.bridge.status()['last_delivery']['status'],'received')

    def test_fixed_requires_test_runtime_and_source_evidence(self):
        did,iid=self.incident()
        for override in [dict(regressions=[]),dict(runtime_evidence=[]),dict(changes=[]),dict(root_cause='Error'),
                         dict(regressions=[dict(path='artifacts/test.log',result='failed')])]:
            with self.assertRaises(ValueError):self.bridge.learn(did,[self.record(iid,**override)])
        self.bridge.learn(did,[self.record(iid)])
        self.bridge.acknowledge(did,'resolved',report())
        self.assertEqual(self.bridge.reviews(did)['incidents'][0]['review']['disposition'],'fixed')

    def test_postmortem_does_not_override_current_login_gate_or_unavailable_health(self):
        did,iid=self.incident();self.bridge.learn(did,[self.record(iid)])
        for current in [{},report('attention',2,'needs_verification'),dict(report(),issues=['probe:OperationalError'])]:
            with self.assertRaises(ValueError):self.bridge.acknowledge(did,'resolved',current)

    def test_unknown_root_and_external_gate_never_fixed(self):
        did,iid=self.incident()
        for kind in ['external','unknown']:
            with self.assertRaises(ValueError):self.bridge.learn(did,[self.record(iid,kind=kind)])
        self.bridge.learn(did,[self.record(iid,kind='unknown',cause_key=None,disposition='mitigated',regressions=[],changes=[],
            next_action='Capture exact failed operation and connection phase before assigning a root cause.')])
        self.bridge.acknowledge(did,'resolved',report())
        self.assertEqual(self.bridge.reviews()['causes'],[])
        self.assertEqual(self.bridge.reviews(did)['incidents'][0]['review']['disposition'],'mitigated')

    def test_investigating_or_needs_user_cannot_be_closed_as_restored(self):
        did,iid=self.incident()
        for disposition in ['investigating','needs_user']:
            self.bridge.learn(did,[self.record(iid,disposition=disposition,next_action='Continue exact failure reproduction before closing.')])
            with self.assertRaises(ValueError):self.bridge.acknowledge(did,'resolved',report())

    def test_idempotent_reports_and_append_only_revisions(self):
        did,iid=self.incident();record=self.record(iid)
        self.bridge.learn(did,[record]);self.bridge.learn(did,[record])
        self.assertEqual(self.bridge.reviews(did)['incidents'][0]['review']['revision'],1)
        self.bridge.learn(did,[self.record(iid,prevention='An additional narrowly scoped guard and its negative regression cases.')])
        with closing(self.bridge.connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM incident_review_history').fetchone()[0],2)
        other=Bridge(self.root/'data')
        self.assertEqual(other.reviews(did)['incidents'][0]['review']['revision'],2)

    def test_similar_symptom_after_recovery_is_reference_not_confirmed_cause(self):
        did,iid=self.incident();self.bridge.learn(did,[self.record(iid)])
        self.bridge.acknowledge(did,'resolved',report());self.now+=60
        second,other=self.incident(22)
        record=self.bridge.reviews(second)['incidents'][0]
        self.assertEqual(record['similar_episodes'],2)
        self.assertEqual(record['cases'][0]['cause_key'],'fixture-connection-phase')
        self.assertIsNone(record['review'])
        self.assertEqual(self.bridge.reviews()['causes'][0]['confirmed_recurrences'],0)
        prompt=self.runner.call_args.args[0][-1]
        self.assertIn('只作线索',prompt);self.assertIn('fixture-connection-phase',prompt)

    def test_confirmed_recurrence_cannot_reuse_old_test_report(self):
        did,iid=self.incident();self.bridge.learn(did,[self.record(iid)])
        self.bridge.acknowledge(did,'resolved',report());self.now+=60
        second,other=self.incident(22)
        with self.assertRaisesRegex(ValueError,'fresh regression'):
            self.bridge.learn(second,[self.record(other)])
        # A recurrence under investigation immediately reopens the known cause.
        self.bridge.learn(second,[self.record(other,disposition='investigating',next_action='Reproduce the recurrence and add its missing trigger to regression tests.')])
        cause=self.bridge.reviews()['causes'][0]
        self.assertEqual(cause['confirmed_recurrences'],1);self.assertEqual(cause['disposition'],'investigating')
        (self.root/'artifacts/test.log').write_text('new synthetic run after recurrence: OK',encoding='utf-8')
        self.bridge.learn(second,[self.record(other)]);self.bridge.acknowledge(second,'resolved',report())

    def test_task_id_churn_during_fault_is_one_episode(self):
        did,iid=self.incident();self.bridge.learn(did,[self.record(iid,disposition='investigating',next_action='Reproduce this still-active connection failure first.')])
        self.bridge.acknowledge(did,'failed')
        second,other=self.incident(2)
        self.bridge.learn(second,[self.record(other)])
        self.assertEqual(self.bridge.reviews(second)['incidents'][0]['similar_episodes'],1)
        self.assertEqual(self.bridge.reviews()['causes'][0]['confirmed_recurrences'],0)

    def test_changed_evidence_or_source_invalidates_closure(self):
        for filename in ['source.py','artifacts/runtime.json','artifacts/test.log']:
            did,iid=self.incident();self.bridge.learn(did,[self.record(iid)])
            path=self.root/filename;original=path.read_bytes();path.write_bytes(original+b' changed')
            with self.assertRaisesRegex(ValueError,'changed after'):self.bridge.acknowledge(did,'resolved',report())
            path.write_bytes(original);self.bridge.acknowledge(did,'resolved',report());self.now+=1
            (self.root/'artifacts/test.log').write_text('new passing run '+str(self.now),encoding='utf-8')

    def test_invalid_private_outside_missing_evidence_rejected(self):
        did,iid=self.incident();(self.root/'data/secret.json').write_text('private')
        for name in ['data/secret.json','artifacts/../data/secret.json',str(self.root/'artifacts/test.log'),'artifacts/missing.log',None]:
            with self.assertRaises(ValueError):
                self.bridge.learn(did,[self.record(iid,regressions=[dict(path=name,result='passed')])])
        with self.assertRaises(ValueError):self.bridge.learn(did,[self.record(iid,root_cause='Cookie: secret')])

    def test_multi_incident_write_is_atomic_and_all_require_reports(self):
        value=report('attention');value['live']=dict(issue='live:attention',last_task_id=2,task_status='network_error')
        self.bridge.observe(value);did=self.bridge.dispatch(runner=self.runner);self.bridge.acknowledge(did,'received')
        first,second=self.bridge.reviews(did)['incidents']
        with self.assertRaises(ValueError):
            self.bridge.learn(did,[self.record(first['incident_id']),self.record('unrelated')])
        self.assertIsNone(self.bridge.reviews(did)['incidents'][0]['review'])
        self.bridge.learn(did,[self.record(first['incident_id'])])
        with self.assertRaises(ValueError):self.bridge.acknowledge(did,'resolved',report())

    def test_self_test_bypasses_learning_and_is_not_a_fault_case(self):
        self.bridge.self_test();did=self.bridge.dispatch(runner=self.runner);self.bridge.acknowledge(did,'received')
        iid=self.bridge.reviews(did)['incidents'][0]['incident_id']
        with self.assertRaises(ValueError):self.bridge.learn(did,[self.record(iid)])
        self.bridge.acknowledge(did,'resolved',report('attention'))
        self.assertEqual(self.bridge.reviews()['incidents'],[])


if __name__=='__main__':unittest.main()
