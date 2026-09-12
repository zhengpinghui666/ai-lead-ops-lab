"""Isolated sync scheduling, gap recovery, stop/race and HTTP contracts."""
import json
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
import clubops as app
import uid_inbox_sync as sync
import uid_inbox_store as inbox
import uid_messaging
import test_uid_inbox_store as fixture
from test_uid_inbox import SENDER,RECEIVER

class InboxSyncTests(unittest.TestCase):
    scan=fixture.InboxStoreTests.scan
    read=fixture.InboxStoreTests.read
    message=fixture.InboxStoreTests.message
    protected=fixture.InboxStoreTests.protected
    def setUp(self):
        fixture.InboxStoreTests.setUp(self)
        for key,value in (('STOP',threading.Event()),('ACTIVE',None),('THREAD',None)):
            p=patch.object(sync,key,value);p.start();self.addCleanup(p.stop)
        self.scan();self.read()
        self.body=dict(lead_id=self.lead,account_uid=SENDER,conversation_id=1,interval_seconds=60)
    def saved(self):
        return dict(self.body,id=sync.save(self.body)['id'])
    def row(self):
        return sync.state()['targets'][0]
    def ready(self):
        with app.db() as c:c.execute("UPDATE uid_inbox_sync SET next_run_at='2000-01-01T00:00:00+00:00'")
    def page(self,indices,more=False,cursor='0',effect=None):
        rows=[{**self.message(str(9007199254741200+i),f'合成新收件 {i}'),'index':str(i),'created_at_raw':str(1789214400000+i)} for i in indices]
        rows=[self.message() if r['index']=='8' else r for r in rows]
        result=dict(status='messages_observed',messages=rows,next_cursor=cursor,has_more=more,output_truncated=False)
        with patch.object(inbox.uid_inbox,'messages',side_effect=effect,return_value=result) as reader:
            self.ready();response=sync.tick()
        return response,reader
    def enable(self):
        rid=self.saved()['id'];sync.control({'id':rid},True);return rid

    def test_save_is_local_paused_and_requires_verified_matching_normal_conversation(self):
        before=self.protected()
        with patch.object(inbox.uid_inbox,'messages') as request:
            rid=self.saved()['id'];self.assertFalse(self.row()['enabled']);self.assertEqual(self.row()['watermark'],'8');request.assert_not_called()
            self.assertIsNone(sync.tick());request.assert_not_called()
        self.assertEqual(self.protected(),before)
        for update in ({'interval_seconds':29},{'interval_seconds':True},{'account_uid':'99'},{'conversation_id':2}):
            with self.assertRaises(ValueError):sync.save({**self.body,**update})
        with self.assertRaises(ValueError):sync.save(self.body,'demo')
        with app.db() as c:c.execute('UPDATE uid_inbox_conversations SET inbox=1')
        with self.assertRaises(ValueError):sync.control({'id':rid},True)

    def test_gap_pages_resume_independently_of_manual_cursor_and_do_not_mark_replies(self):
        rid=self.enable();before=self.protected()
        self.page([12,11],True,'11');self.assertEqual(self.row()['status'],'catching_up');self.assertEqual(self.row()['watermark'],'8')
        with app.db() as c:c.execute("UPDATE uid_inbox_conversations SET next_cursor='999'")
        _,reader=self.page([10,9],True,'9');self.assertEqual(reader.call_args.kwargs['cursor'],11)
        sync.control({'id':rid},False);self.assertIsNone(sync.tick());self.assertEqual(self.row()['resume_cursor'],'9')
        sync.control({'id':rid},True);self.page([8],True,'7')
        row=self.row();self.assertEqual(row['status'],'waiting');self.assertEqual(row['watermark'],'12');self.assertIsNone(row['resume_cursor'])
        self.assertEqual(row['read_count'],3);self.assertEqual(row['new_messages'],4)
        self.assertIsNone(sync.tick());self.assertEqual(self.protected(),before)

    def test_duplicate_page_dedupes_and_only_inbound_is_counted(self):
        self.enable();self.page([9]);self.assertEqual(self.row()['new_messages'],1)
        self.page([9]);self.assertEqual(self.row()['new_messages'],1)
        outbound={**self.message('9007199254741400','合成出站'),'direction':'outbound','sender_uid':SENDER,'index':'10','created_at_raw':'1789214400010'}
        self.page([],effect=lambda *a,**k:dict(status='messages_observed',messages=[outbound],has_more=False,next_cursor='0',output_truncated=False))
        self.assertEqual(self.row()['new_messages'],1)

    def test_stop_during_page_settles_results_but_prevents_next_dispatch(self):
        rid=self.enable()
        def stopped(*args,**kwargs):
            sync.control({'id':rid},False)
            self.assertEqual(self.row()['status'],'stopping')
            return dict(status='messages_observed',messages=[{**self.message('9007199254741999'),'index':'99'}],has_more=True,next_cursor='98',output_truncated=False)
        self.page([],effect=stopped);row=self.row()
        self.assertEqual(row['status'],'paused');self.assertFalse(row['enabled']);self.assertEqual(row['resume_cursor'],'98')
        self.assertEqual(row['new_messages'],1);self.assertIsNone(sync.ACTIVE);self.assertIsNone(sync.tick())

    def test_retry_is_bounded_and_session_failure_stops_immediately(self):
        self.enable()
        with patch.object(inbox.uid_inbox,'messages',return_value=dict(status='read_failed',messages=[],error='transport_failed')):
            for n in range(4):
                self.ready();sync.tick();self.assertEqual(bool(self.row()['enabled']),n<3)
        self.assertEqual(self.row()['failures'],4);self.assertEqual(self.row()['status'],'attention')
        sync.control({'id':1},True);self.provider.current.side_effect=ValueError('fixture session unavailable')
        self.ready();sync.tick();self.assertFalse(self.row()['enabled']);self.assertEqual(self.row()['failures'],1)

    def test_repeated_cursor_truncation_and_unknown_pagination_keep_watermark(self):
        for failure in ('loop','truncated','unknown'):
            with self.subTest(failure=failure):
                with app.db() as c:c.execute('DELETE FROM uid_inbox_sync')
                rid=self.enable()
                with app.db() as c:c.execute("UPDATE uid_inbox_sync SET watermark='8'")
                self.page([20],True,'19');self.page([18],True,'17')
                self.page([],effect=lambda *a,**k:dict(status='messages_observed',messages=[],has_more=None if failure=='unknown' else True,next_cursor='19',output_truncated=failure=='truncated'))
                self.assertEqual(self.row()['status'],'attention');self.assertFalse(self.row()['enabled']);self.assertEqual(self.row()['watermark'],'8')

    def test_account_or_lead_change_prevents_network_and_pauses(self):
        rid=self.enable();self.config_mock.return_value=({'sender_uid':'99'},[])
        with patch.object(inbox.uid_inbox,'messages') as request:self.ready();sync.tick();request.assert_not_called()
        self.assertFalse(self.row()['enabled'])
        self.config_mock.return_value=({'sender_uid':SENDER},[])
        sync.control({'id':rid},True)
        with self.assertRaises(ValueError):sync.save(self.body)
        with app.db() as c:c.execute("UPDATE people SET external_id='12345'")
        with patch.object(inbox.uid_inbox,'messages') as request:self.ready();sync.tick();request.assert_not_called()
        self.assertFalse(self.row()['enabled'])

    def test_recover_retains_cursor_and_start_requires_live_local_session(self):
        rid=self.enable();self.page([12],True,'11');sync.recover()
        self.assertFalse(self.row()['enabled']);self.assertEqual(self.row()['resume_cursor'],'11');self.assertIsNone(sync.tick())
        self.provider.current.side_effect=ValueError('not ready')
        with self.assertRaises(ValueError):sync.control({'id':rid},True)
        self.assertFalse(self.row()['enabled'])

    def test_shared_guard_and_shutdown_admission_prevent_overlap(self):
        self.enable()
        with uid_messaging.GUARD:self.assertIsNone(sync.tick())
        self.assertEqual(self.row()['read_count'],0)
        import server
        httpd=server.LocalHTTPServer(('127.0.0.1',0),server.Handler);httpd.active_writes=1
        try:
            with patch.object(sync,'ACTIVE',1):
                with self.assertRaises(ValueError):httpd.prepare_stop()
            self.assertFalse(httpd.stopping)
        finally:httpd.server_close()

    def test_new_table_migration_backs_up_old_inbox(self):
        before=self.protected()
        with app.db() as c:c.execute('DROP TABLE uid_inbox_sync')
        app.init();self.assertEqual(self.protected(),before)
        backups=list((app.DATA_DIR/'backups').glob('*before-uid-inbox-*'));self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='uid_inbox_sync'").fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM uid_inbox_messages').fetchone()[0],1)

    def test_http_save_start_stop_require_csrf_and_reject_demo(self):
        import urllib.request,urllib.error
        from http.server import ThreadingHTTPServer
        import server
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        def post(action,body,token=None,mode='live'):
            headers={'X-ClubOps-Token':token} if token else {}
            request=urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}/api/{action}?mode={mode}',data=json.dumps(body).encode(),headers=headers)
            try:
                with urllib.request.urlopen(request,timeout=4) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as error:
                with error:return error.code,json.load(error)
        try:
            self.assertEqual(post('uid-inbox-sync-save',self.body)[0],403)
            self.assertEqual(post('uid-inbox-sync-save',self.body,server.CSRF,'demo')[0],400)
            self.assertEqual(post('uid-inbox-sync-save',self.body,server.CSRF)[0],200)
            self.assertFalse(self.row()['enabled'])
            self.assertEqual(post('uid-inbox-sync-start',{'id':1},server.CSRF)[0],200)
            self.assertTrue(self.row()['enabled'])
            self.assertEqual(post('uid-inbox-sync-stop',{'id':1})[0],403)
            self.assertTrue(self.row()['enabled'])
            self.assertEqual(post('uid-inbox-sync-stop',{'id':1},server.CSRF)[0],200)
            self.assertFalse(self.row()['enabled'])
        finally:httpd.shutdown();thread.join(3);httpd.server_close()

    def test_background_worker_settles_and_shutdown_retains_closed_state(self):
        self.enable();arrived=threading.Event()
        def reply(*args,**kwargs):
            arrived.set();return dict(status='messages_observed',messages=[],has_more=False,next_cursor='0',output_truncated=False)
        with patch.object(inbox.uid_inbox,'messages',side_effect=reply):
            sync.start_service()
            try:self.assertTrue(arrived.wait(4))
            finally:sync.shutdown()
        self.assertFalse(sync.THREAD.is_alive());self.assertIsNone(sync.ACTIVE)
        self.assertEqual(self.row()['read_count'],1);self.assertFalse(self.row()['enabled'])

if __name__=='__main__':unittest.main()
