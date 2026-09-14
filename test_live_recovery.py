import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import clubops as app
import live_monitor as live
import live_tracking as tracking
import live_recovery as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()
        live.ACTIVE.clear()
        tracking.STOP.clear()
        with app.db() as c:
            for account, uid in [('100','111'),('200','222')]:
                c.execute('INSERT INTO collection_accounts VALUES(?,?,?,?,?,?,?)',
                          (account, uid, account, 'primary' if account=='100' else 'isolated', 1, '["live"]', app.now()))
        self.config = dict(live.DEFAULTS, room_url='https://live.douyin.com/12345', duration_seconds=180, max_messages=37, include_keywords='陪玩')
        live.save(self.config)
        self.track = tracking.start({'request_id':'fixture'})['id']
        with patch('threading.Thread'):
            tracking.tick()
        self.sid = tracking.state()['last_session_id']
        live.ACTIVE.clear()
        self.info = dict(methods={}, errors={}, timestamps={}, sockets=[],
            page=dict(navigation='failed', http_status=None, body_chars=0, gate='unknown'),
            network=dict(requests=1, failures={'ERR_FAILED':1}, script_errors={}, live_responses={}, live_routes={}, navigation_attempts=1),
            poll=dict(responses=0), poll_fields={})
        self.diagnostic()
        live.record_status(self.sid, 'failed', final=True)
        tracking.tick()
        self.assertEqual(tracking.state()['status'], 'attention')

    def test_legacy_expired_preflight_requires_matching_local_age_and_zero_data(self):
        import time,collector_http_session
        with app.db() as c:
            c.execute("DELETE FROM live_session_events WHERE session_id=? AND status='diagnostic'",(self.sid,))
            row=dict(c.execute('SELECT * FROM live_sessions WHERE id=?',(self.sid,)).fetchone())
            row['finished_at']=row['started_at']
            value=dict(account='100',sender_uid='111',captured_at=time.time()-collector_http_session.MAX_AGE-120)
            with patch('collector_http_session.load',return_value=value):
                self.assertTrue(recovery.eligible(c,row))
                self.assertFalse(recovery.eligible(c,dict(row,frames=1)))
                self.assertFalse(recovery.eligible(c,dict(row,observed=1)))
            for changed in [dict(value,account='200'),dict(value,sender_uid='222'),dict(value,captured_at=time.time())]:
                with patch('collector_http_session.load',return_value=changed):self.assertFalse(recovery.eligible(c,row))
            with patch('collector_http_session.load',side_effect=ValueError('invalid credential')):self.assertFalse(recovery.eligible(c,row))

    def test_legacy_expired_retry_retains_original_account_and_request_id(self):
        import time,collector_http_session
        with app.db() as c:
            c.execute("DELETE FROM live_session_events WHERE session_id=? AND status='diagnostic'",(self.sid,))
            c.execute('UPDATE live_sessions SET finished_at=started_at WHERE id=?',(self.sid,))
        value=dict(account='100',sender_uid='111',captured_at=time.time()-collector_http_session.MAX_AGE-120)
        with patch('collector_http_session.load',return_value=value):
            child=self.retry()['id'];self.assertEqual(self.retry()['id'],child)
        with app.db() as c:
            old=json.loads(c.execute('SELECT config FROM live_sessions WHERE id=?',(self.sid,)).fetchone()[0])
            new=json.loads(c.execute('SELECT config FROM live_sessions WHERE id=?',(child,)).fetchone()[0])
            self.assertEqual(new,old)
            self.assertEqual(c.execute('SELECT status FROM live_sessions WHERE id=?',(self.sid,)).fetchone()[0],'failed')

    def diagnostic(self):
        with app.db() as c:
            c.execute('INSERT INTO live_session_events(session_id,status,detail,observed_at) VALUES(?,?,?,?)',
                (self.sid, 'diagnostic', '连接、页面与消息计数：'+json.dumps(self.info), app.now()))

    def tearDown(self):
        live.ACTIVE.clear()
        app.DATA_DIR = self.old
        self.temp.cleanup()

    def retry(self):
        with patch('threading.Thread'):
            return recovery.retry({'id':self.track, 'session_id':self.sid})

    def test_original_account_scope_idempotency_and_next_scheduler_batch(self):
        live.save(dict(self.config,room_url='https://live.douyin.com/99999',max_messages=10))
        child = self.retry()['id']
        self.assertEqual(self.retry()['id'], child)
        with app.db() as c:
            old = json.loads(c.execute('SELECT config FROM live_sessions WHERE id=?',(self.sid,)).fetchone()[0])
            new = json.loads(c.execute('SELECT config FROM live_sessions WHERE id=?',(child,)).fetchone()[0])
            self.assertEqual(new, old)
            self.assertEqual(c.execute('SELECT account_id FROM account_role_runs WHERE run_key=?',('live:'+str(child),)).fetchone()[0],'100')
        live.ACTIVE.clear()
        live.record_status(child,'completed',final=True)
        tracking.tick()
        self.assertTrue(tracking.state()['enabled'])
        with app.db() as c:
            c.execute("UPDATE live_tracks SET next_run_at='2000-01-01T00:00:00+00:00' WHERE id=?",(self.track,))
        with patch('threading.Thread'):
            tracking.tick()
        self.assertNotEqual(tracking.state()['last_session_id'], child)

    def test_no_recovery_for_manual_stop_or_superseded_track(self):
        tracking.stop(self.track)
        with self.assertRaises(ValueError):self.retry()

    def test_account_change_does_not_fall_back_to_other_account(self):
        with app.db() as c:c.execute("UPDATE collection_accounts SET enabled=0 WHERE account_id='100'")
        with self.assertRaises(ValueError):self.retry()
        self.assertEqual(tracking.state()['last_session_id'], self.sid)

    def test_platform_response_gate_and_unrecognized_error_are_not_retryable(self):
        original = json.loads(json.dumps(self.info))
        for status in ['needs_login','needs_verification','access_denied','rate_limited','interrupted','cancelled']:
            with app.db() as c:
                c.execute('UPDATE live_sessions SET status=? WHERE id=?',(status,self.sid))
            with self.assertRaises(ValueError):self.retry()
        with app.db() as c:c.execute("UPDATE live_sessions SET status='failed' WHERE id=?",(self.sid,))
        for section,key,value in [('page','http_status',403),('page','gate','verification'),('page','body_chars',12),('network','failures',{'ERR_BLOCKED_BY_CLIENT':1}),('poll','responses',1)]:
            self.info=json.loads(json.dumps(original));self.info[section][key]=value;self.diagnostic()
            with self.assertRaises(ValueError):self.retry()

    def test_repeated_request_after_stop_never_reenables(self):
        child=self.retry()['id'];tracking.stop(self.track)
        self.assertEqual(self.retry()['id'],child)
        self.assertFalse(tracking.state()['enabled'])

    def test_network_diagnostic_new_counter_and_legacy(self):
        with app.db() as c:c.execute('UPDATE live_sessions SET finished_at=NULL WHERE id=?',(self.sid,))
        live.receive(self.sid,dict(type='diagnostic',**self.info))
        for bad in [True,-1,4,'3']:
            info=json.loads(json.dumps(self.info));info['network']['navigation_attempts']=bad
            with self.assertRaises(ValueError):live.receive(self.sid,dict(type='diagnostic',**info))
        del self.info['network']['navigation_attempts']
        live.receive(self.sid,dict(type='diagnostic',**self.info))

    def test_demo_and_caller_scope_changes_rejected(self):
        for body,mode in [({'id':self.track,'session_id':self.sid},'demo'),({'id':self.track,'session_id':self.sid,'room_url':'other'},'live'),({'id':True,'session_id':self.sid},'live')]:
            with self.assertRaises(ValueError):recovery.retry(body,mode)

    def test_http_csrf_origin_and_original_failure_binding(self):
        import threading
        import server
        from http.server import ThreadingHTTPServer
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{httpd.server_port}'
        body=json.dumps({'id':self.track,'session_id':self.sid}).encode()
        try:
            for headers in [{},{'X-ClubOps-Token':server.CSRF,'Origin':'https://unrelated.invalid'}]:
                with self.assertRaises(HTTPError) as caught:urlopen(Request(base+'/api/live-connection-retry',data=body,headers=headers))
                self.assertEqual(caught.exception.code,403);caught.exception.close()
            headers={'Content-Type':'application/json','Origin':base,'X-ClubOps-Token':server.CSRF}
            with patch.object(live,'worker',lambda sid,config,control:None):
                with urlopen(Request(base+'/api/live-connection-retry',data=body,headers=headers)) as response:
                    child=json.load(response)['result']['id']
            self.assertNotEqual(child,self.sid)
            self.assertEqual(tracking.state()['last_session_id'],child)
        finally:
            httpd.shutdown();thread.join(timeout=3);httpd.server_close()

if __name__ == '__main__':unittest.main()
