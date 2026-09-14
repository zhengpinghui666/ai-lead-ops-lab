import json,unittest
from unittest.mock import Mock,patch
import live_identity as identity,collection_session_refresh as refresh
from test_live_recovery import RecoveryTests

FAILED=dict(status='http_failed',http_attempts=1,transport_error='connection_failed',transport_phase='connect')
OK=dict(status='identity_verified',sender_uid='111',http_attempts=1,http_status=200)

class ProbeTests(unittest.TestCase):
    def test_same_input_two_retries_then_success(self):
        probe=Mock(side_effect=[FAILED,FAILED,OK]);reports=[];sleep=Mock();value={'expected_account':'100','cookie':'fixture-only','user_agent':'fixture'}
        result=identity.probe(value,exchange=probe,sleep=sleep,report=lambda reason,safe:reports.append(safe))
        self.assertEqual(result['http_attempts'],3);self.assertEqual(result['status'],'identity_verified')
        self.assertEqual([call.args[0] for call in probe.call_args_list],[value]*3)
        self.assertEqual([r['attempt'] for r in reports],[1,2,3]);self.assertNotIn('fixture-only',json.dumps(reports))
        self.assertEqual(sleep.call_count,40)

    def test_exhaustion_stops_at_three(self):
        probe=Mock(return_value=FAILED)
        result=identity.probe({},exchange=probe,sleep=lambda _:None)
        self.assertEqual(probe.call_count,3);self.assertEqual(result['status'],'http_failed')

    def test_no_retry_for_platform_gates_or_ambiguous_errors(self):
        for changed in [{'http_status':x} for x in [200,401,403,429]]+[{'verification_indicated':True},
            {'transport_error':'tls_verification_failed'},{'transport_phase':'response_body'},
            {'status':'needs_login'},{'status':'unrecognized_response'},{'status':'account_mismatch'},
            {'http_attempts':0},{'http_attempts':True}]:
            with self.subTest(changed=changed):
                probe=Mock(return_value=dict(FAILED,**changed))
                identity.probe({},exchange=probe,sleep=lambda _:self.fail('must not retry'))
                self.assertEqual(probe.call_count,1)

    def test_cancel_before_during_request_and_backoff(self):
        probe=Mock()
        with self.assertRaisesRegex(refresh.RefreshError,'cancelled'):identity.probe({},exchange=probe,cancelled=lambda:True)
        probe.assert_not_called()
        for cancel_in in ['request','backoff']:
            stop=[False]
            def request(_):
                if cancel_in=='request':stop[0]=True
                return FAILED
            def sleep(_):stop[0]=True
            probe=Mock(side_effect=request)
            with self.assertRaisesRegex(refresh.RefreshError,'cancelled'):
                identity.probe({},exchange=probe,cancelled=lambda:stop[0],sleep=sleep)
            self.assertEqual(probe.call_count,1)

class LegacyRecoveryTests(RecoveryTests):
    def test_worker_exhaustion_records_three_attempts_and_never_starts_browser(self):
        import clubops as app,live_monitor as live,live_recovery as recovery
        with app.db() as c:
            config=json.loads(c.execute('SELECT config FROM live_sessions WHERE id=?',(self.sid,)).fetchone()[0])
            c.execute('DELETE FROM live_session_events WHERE session_id=?',(self.sid,))
            c.execute("UPDATE live_sessions SET finished_at=NULL,status='connecting' WHERE id=?",(self.sid,))
        real_probe=identity.probe
        with patch('collection_session_refresh.ensure',return_value=({'user_agent':'fixture','cookies':{}},None)), \
                patch('collector_http_session.cookie_header',return_value='fixture-cookie'), \
                patch('uid_bootstrap.probe',return_value=FAILED) as request, \
                patch.object(identity,'probe',side_effect=lambda value,**kw:real_probe(value,sleep=lambda _:None,**kw)), \
                patch('collector.dependencies') as dependencies:
            live.worker(self.sid,config,dict(stop=False,process=None))
            self.assertEqual(request.call_count,3);dependencies.assert_not_called()
        with app.db() as c:
            row=dict(c.execute('SELECT * FROM live_sessions WHERE id=?',(self.sid,)).fetchone())
            self.assertEqual(row['status'],'failed');self.assertFalse(recovery.eligible(c,row))
            proofs=[json.loads(r[0]) for r in c.execute("SELECT detail FROM live_session_events WHERE session_id=? AND status='preflight'",(self.sid,))]
            self.assertEqual([p['attempt'] for p in proofs if p['reason']=='identity_attempt'],[1,2,3])
            self.assertEqual(proofs[-1]['http_attempts'],3)

    def test_old_preflight_can_retry_only_same_frozen_account(self):
        import clubops as app,live_recovery as recovery
        proof=dict(version='live-preflight-v1',stage='identity',reason='identity_checked',**FAILED)
        with app.db() as c:
            c.execute("DELETE FROM live_session_events WHERE session_id=?",(self.sid,))
            for status,detail in [('preflight',json.dumps(proof)),('failed','failed')]:
                c.execute('INSERT INTO live_session_events(session_id,status,detail,observed_at) VALUES(?,?,?,?)',(self.sid,status,detail,app.now()))
            old=dict(c.execute('SELECT * FROM live_sessions WHERE id=?',(self.sid,)).fetchone())
            self.assertTrue(recovery.eligible(c,old))
            for change in [dict(http_status=429),dict(verification_indicated=True),dict(http_attempts=3),dict(reason='identity_attempt')]:
                c.execute("UPDATE live_session_events SET detail=? WHERE session_id=? AND status='preflight'",(json.dumps(dict(proof,**change)),self.sid))
                self.assertFalse(recovery.eligible(c,old))
            c.execute("UPDATE live_session_events SET detail=? WHERE session_id=? AND status='preflight'",(json.dumps(proof),self.sid))
        child=self.retry()['id'];self.assertEqual(child,self.retry()['id'])
        with app.db() as c:
            self.assertEqual(json.loads(c.execute('SELECT config FROM live_sessions WHERE id=?',(child,)).fetchone()[0]),json.loads(old['config']))

if __name__=='__main__':unittest.main()
