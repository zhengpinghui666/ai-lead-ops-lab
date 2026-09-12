"""Isolated inbox persistence and identity tests; no platform requests."""
import json
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
import sqlite3
import threading
import urllib.request
import urllib.error
from unittest.mock import patch,Mock
import clubops as app
import uid_inbox_store as store
import uid_messaging
from test_uid_inbox import CONVERSATION,SENDER,RECEIVER


class InboxStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.patch=patch.object(app,'DATA_DIR',Path(self.temp.name));self.patch.start();self.addCleanup(self.patch.stop)
        app.init()
        self.config=patch.object(uid_messaging,'config',return_value=({'sender_uid':SENDER,'enabled':False},['disabled']))
        self.config_mock=self.config.start();self.addCleanup(self.config.stop)
        self.provider=Mock();self.provider.current.return_value={'sender_uid':SENDER,'im_verified':True}
        self.local=patch.object(store.uid_session,'Provider',return_value=self.provider);self.local.start();self.addCleanup(self.local.stop)
        with app.db() as c:
            source=c.execute("INSERT INTO sources(name,kind,notes) VALUES('synthetic','browser','')").lastrowid
            person=c.execute("INSERT INTO people(source_id,external_id,nickname) VALUES(?,?,?)",(source,RECEIVER,'合成用户')).lastrowid
            self.lead=c.execute("INSERT INTO leads(person_id,updated_at) VALUES(?,?)",(person,app.now())).lastrowid

    def scan(self,status='checked'):
        result={'status':status,'targets':{RECEIVER:{'conversations':[CONVERSATION]}},'scopes':[],'evidence':[]}
        with patch.object(store.uid_inbox,'scan',return_value=result):
            return store.read(dict(lead_id=self.lead,account_uid=SENDER,operation='scan'),provider=self.provider)

    def message(self,mid='9007199254741001',content='历史入站文字'):
        return dict(server_message_id=mid,sender_uid=RECEIVER,direction='inbound',content=content,index='8',created_at_raw='1780000000')

    def read(self,rows=None,older=False,more=False,cursor='0'):
        with app.db() as c:cid=c.execute('SELECT id FROM uid_inbox_conversations').fetchone()[0]
        result=dict(status='messages_observed',messages=rows if rows is not None else [self.message()],next_cursor=cursor,has_more=more,output_truncated=False)
        with patch.object(store.uid_inbox,'messages',return_value=result) as request:
            response=store.read(dict(lead_id=self.lead,account_uid=SENDER,operation='messages',conversation_id=cid,older=older),provider=self.provider)
        return response,request

    def protected(self):
        with app.db() as c:return {table:[tuple(r) for r in c.execute('SELECT * FROM '+table)] for table in ('people','leads','messages','message_jobs','uid_message_attempts')}

    def test_history_deduplicates_without_authorizing_or_advancing_send_jobs(self):
        before=self.protected();self.scan();first,_=self.read();second,_=self.read()
        self.assertEqual(first['new_messages'],1);self.assertEqual(second['new_messages'],0)
        state=store.history(self.lead);self.assertEqual(len(state['messages']),1)
        self.assertEqual(state['messages'][0]['server_message_id'],'9007199254741001')
        self.assertEqual(state['messages'][0]['created_at_raw'],'1780000000')
        self.assertEqual(state['messages'][0]['direction'],'inbound')
        self.assertEqual(self.protected(),before)

    def test_account_and_lead_scopes_reject_mismatch_and_self(self):
        with self.assertRaises(ValueError):store.read(dict(lead_id=self.lead,account_uid='99',operation='scan'))
        self.provider.current.return_value={'sender_uid':'99','im_verified':True}
        with patch.object(store.uid_inbox,'scan') as scan:
            result=store.read(dict(lead_id=self.lead,account_uid=SENDER,operation='scan'))
        self.assertEqual(result['status'],'session_unavailable');scan.assert_not_called()
        self.config_mock.return_value=({'sender_uid':RECEIVER},[])
        with self.assertRaises(ValueError):store.read(dict(lead_id=self.lead,account_uid=RECEIVER,operation='scan'))

    def test_bad_pages_rollback_new_rows_and_keep_old_content(self):
        self.scan();self.read();initial=store.history(self.lead)['messages']
        with self.assertRaises(ValueError):self.read([self.message('9007199254741002'),self.message(content='改变后的文字')])
        self.assertEqual(store.history(self.lead)['messages'],initial)
        self.assertEqual(store.history(self.lead)['last_read']['status'],'read_failed')

    def test_cursor_only_from_saved_platform_page_and_cannot_loop(self):
        self.scan()
        with self.assertRaises(ValueError):self.read(older=True)
        self.read(more=True,cursor='88')
        _,request=self.read([self.message('9007199254741002')],older=True,more=True,cursor='77')
        self.assertEqual(request.call_args.kwargs['cursor'],88)
        with self.assertRaises(ValueError):self.read(older=True,more=True,cursor='77')
        self.assertEqual(store.history(self.lead)['conversations'][0]['next_cursor'],'77')

    def test_local_pagination_does_not_read_platform_or_mix_accounts(self):
        self.scan()
        for batch in range(3):self.read([self.message(str(9007199254741001+batch*20+i)) for i in range(20)])
        first=store.history(self.lead);self.assertEqual(len(first['messages']),50);self.assertTrue(first['has_more'])
        second=store.history(self.lead,before=first['next_before']);self.assertEqual(len(second['messages']),10)
        self.assertFalse(second['has_more'])
        self.config_mock.return_value=({'sender_uid':'12345'},[])
        self.assertEqual(store.history(self.lead)['messages'],[])

    def test_partial_scan_keeps_positive_evidence_and_failure(self):
        self.scan(status='read_failed');state=store.history(self.lead)
        self.assertEqual(len(state['conversations']),1)
        self.assertEqual(state['last_read']['status'],'read_failed')
        self.scan();self.assertEqual(len(store.history(self.lead)['conversations']),1)

    def test_cancelled_service_record_is_not_replayed(self):
        self.scan()
        with app.db() as c:c.execute("UPDATE uid_inbox_reads SET status='reading'")
        store.recover();self.assertEqual(store.history(self.lead)['last_read']['status'],'interrupted')

    def test_shared_guard_blocks_conflicting_reads_and_sends(self):
        with uid_messaging.GUARD:
            with self.assertRaises(ValueError):self.scan()
        self.scan();self.assertFalse(uid_messaging.GUARD.locked())

    def test_changed_lead_during_request_prevents_persistence(self):
        def changed(*args,**kwargs):
            with app.db() as c:c.execute("UPDATE people SET external_id='12345'")
            return dict(status='checked',targets={RECEIVER:{'conversations':[CONVERSATION]}})
        with patch.object(store.uid_inbox,'scan',side_effect=changed):
            with self.assertRaises(ValueError):store.read(dict(lead_id=self.lead,account_uid=SENDER,operation='scan'),provider=self.provider)
        self.assertEqual(store.history(self.lead)['conversations'],[])

    def test_migration_backs_up_history_and_preserves_business(self):
        before=self.protected()
        with app.db() as c:
            for table in ('uid_inbox_messages','uid_inbox_conversations','uid_inbox_reads'):c.execute('DROP TABLE '+table)
        app.init();self.assertEqual(self.protected(),before)
        backups=list((app.DATA_DIR/'backups').glob('*before-uid-inbox-*'))
        self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='uid_inbox_reads'").fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM leads').fetchone()[0],1)

    def test_http_history_is_local_only_and_reads_require_csrf(self):
        from http.server import ThreadingHTTPServer
        import server
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        def request(path,body=None,token=None):
            headers={'X-ClubOps-Token':token} if token else {}
            req=urllib.request.Request('http://127.0.0.1:'+str(httpd.server_port)+path,
                data=json.dumps(body).encode() if body is not None else None,headers=headers)
            try:
                with urllib.request.urlopen(req,timeout=3) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as error:return error.code,json.load(error)
        try:
            with patch.object(store.uid_inbox,'scan') as scan:
                self.assertEqual(request('/api/uid-inbox?lead_id='+str(self.lead))[0],200);scan.assert_not_called()
                body=dict(lead_id=self.lead,account_uid=SENDER,operation='scan')
                self.assertEqual(request('/api/uid-inbox-read',body)[0],403);scan.assert_not_called()
                scan.return_value=dict(status='checked',targets={RECEIVER:{'conversations':[CONVERSATION]}})
                self.assertEqual(request('/api/uid-inbox-read',body,server.CSRF)[1]['result']['status'],'checked')
                self.assertEqual(scan.call_count,1)
                self.assertEqual(request('/api/uid-inbox-read?mode=demo',body,server.CSRF)[0],400)
        finally:httpd.shutdown();thread.join(3);httpd.server_close()


if __name__=='__main__':unittest.main()
