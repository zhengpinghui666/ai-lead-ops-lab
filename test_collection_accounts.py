import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
import clubops as app
import collection_accounts as accounts
import collector_http_session as sessions


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.override=patch.object(app,'DATA_DIR',self.root);self.override.start();app.init()
    def tearDown(self):self.override.stop();self.tmp.cleanup()
    def register(self,account,uid,storage,roles,enabled=True):
        with patch.object(sessions,'load',return_value=dict(account=account,sender_uid=uid)),patch.object(sessions,'status',return_value=dict(status='identity_verified')):
            return accounts.save(dict(account_id=account,sender_uid=uid,storage=storage,roles=roles,enabled=enabled,label=account))
    def task(self,c,n):
        c.execute("INSERT INTO collection_tasks(id,request_id,kind,target,video_limit,comment_limit,interactive,status,created_at,updated_at) VALUES(?,?,'video','fixture',1,30,0,'queued',?,?)",(n,str(n),app.now(),app.now()))
    def test_two_accounts_share_batches_once_and_running_bindings_do_not_change(self):
        self.register('7446','111','isolated',['comments','discovery'])
        self.register('9517','222','primary',['comments'])
        with app.db() as c:
            for i in range(1,5):self.task(c,i)
            first=accounts.bind(c,1,'video');second=accounts.bind(c,2,'video')
            self.assertNotEqual(first['account_id'],second['account_id'])
            self.assertEqual(accounts.bind(c,1,'video'),first)
            self.assertEqual(accounts.bind(c,3,'author')['account_id'],'7446')
            self.assertEqual(accounts.bind(c,4,'video',resume_from=1)['account_id'],first['account_id'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_task_accounts').fetchone()[0],4)
    def test_legacy_resume_keeps_primary_instead_of_switching_to_new_read_assignment(self):
        self.register('9517','222','primary',[],False)
        self.register('7446','111','isolated',['comments','discovery'])
        with app.db() as c:
            self.task(c,1);self.task(c,2)
            self.assertEqual(accounts.bind(c,2,'video',resume_from=1)['account_id'],'9517')
    def test_missing_assignment_does_not_silently_borrow_private_sender(self):
        self.register('7446','111','isolated',['discovery'])
        with app.db() as c:
            self.task(c,1)
            with self.assertRaises(ValueError):accounts.bind(c,1,'video')
    def test_wrong_saved_identity_is_rejected(self):
        with patch.object(sessions,'load',return_value=dict(account='different',sender_uid='222')):
            with self.assertRaises(ValueError):accounts.save(dict(account_id='7446',sender_uid='111',storage='isolated',roles=['comments'],enabled=True,label='fixture'))
    def test_account_vault_override_does_not_change_shared_business_db_directory(self):
        target=self.root/'collection-accounts/7446'
        with patch.dict(os.environ,{'CLUBOPS_COLLECTION_ACCOUNT_DATA_DIR':str(target)}):
            self.assertEqual(sessions.path(),target/'private/collection-http/session.dpapi')
            self.assertEqual(sessions.path(self.root),self.root/'private/collection-http/session.dpapi')
            self.assertEqual(app.DATA_DIR,self.root)
        with self.assertRaises(ValueError):accounts.directory(dict(storage='isolated',account_id='../outside'))
    def test_worker_rejects_misbound_account_before_platform_calls(self):
        import threading
        import collector_http_worker as worker
        probe=Mock();events=[]
        worker.collect(dict(page_concurrency=1,video_limit=1,kind='video',collection_account=dict(account_id='7446',sender_uid='111')),events.append,threading.Event(),session=dict(account='9517',sender_uid='222'),identity_probe=probe)
        probe.assert_not_called()
        self.assertTrue(any(e.get('status')=='identity_failed' for e in events))
