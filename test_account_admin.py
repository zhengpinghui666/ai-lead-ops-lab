"""Synthetic account management: no real credentials, login windows or sends."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import clubops as app
import account_admin as admin
import account_scope
import collection_accounts as accounts
import collector_http_session as sessions
import uid_session
import uid_messaging


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(app,'DATA_DIR',self.root),
            patch.object(sessions,'status',return_value=dict(ready=True,account='7446',status='identity_verified',expires_at=1)),
            patch.object(accounts,'prepared',return_value=[]),
            patch.object(uid_session,'local_status',return_value=dict(im_read_verified=False)),
            patch.object(uid_messaging,'config',return_value=({'sender_uid':'222'},[]))]
        for p in self.patches:p.start()
        app.init()
    def tearDown(self):
        admin.ACTIVE.clear()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def register(self,account='7446',uid='111',roles=None,storage='isolated'):
        with patch.object(sessions,'load',return_value=dict(account=account,sender_uid=uid)):
            accounts.save(dict(account_id=account,sender_uid=uid,label=account,storage=storage,roles=roles or [],enabled=bool(roles)))
        return admin.state()['accounts'][-1]
    def body(self,row,roles):
        return dict(account_id=row['account_id'],label='新备注',roles=roles,enabled=bool(roles),revision=row['revision'])
    def test_create_is_disabled_draft_without_external_action(self):
        with patch.object(admin,'start_login') as login,patch.object(admin.subprocess,'Popen') as spawn:
            row=admin.create(dict(account_id='7446',label='评论号'))['accounts'][0]
        self.assertFalse(row['enabled']);self.assertEqual(row['sender_uid'],'');self.assertEqual(row['roles'],[])
        login.assert_not_called();spawn.assert_not_called()
        with self.assertRaises(ValueError):admin.create(dict(account_id='../invalid',label='x'))
        with self.assertRaises(ValueError):admin.create(dict(account_id='7446',label='duplicate'))
    def test_draft_cannot_be_assigned_before_identity_check(self):
        row=admin.create(dict(account_id='7446',label=''))['accounts'][0]
        with self.assertRaisesRegex(ValueError,'先登录'):admin.save(self.body(row,['comments']))
    def test_revision_rejects_lost_updates(self):
        row=self.register(roles=['comments'])
        admin.save(self.body(row,['comments']))
        with self.assertRaisesRegex(ValueError,'已更新'):admin.save(self.body(row,[]))
    def test_removal_works_after_session_expiry(self):
        row=self.register(roles=['comments'])
        with patch.object(sessions,'load',side_effect=ValueError('expired')):
            result=admin.save(self.body(row,[]))['accounts'][0]
        self.assertFalse(result['enabled']);self.assertEqual(result['roles'],[])
    def test_new_role_requires_own_verified_identity(self):
        row=self.register()
        with patch.object(sessions,'load',return_value=dict(account='9517',sender_uid='222')):
            with self.assertRaisesRegex(ValueError,'不一致'):admin.save(self.body(row,['comments']))
    def test_group_role_requires_this_account_im_vault(self):
        row=self.register()
        with patch.object(uid_session,'load',return_value=dict(account='9517',sender_uid='222',im_verified=True)):
            with self.assertRaisesRegex(ValueError,'核对'):admin.save(self.body(row,['groups']))
        with patch.object(uid_session,'load',return_value=dict(account='7446',sender_uid='111',im_verified=True)):
            self.assertEqual(admin.save(self.body(row,['groups']))['accounts'][0]['roles'],['groups'])
    def test_non_sender_cannot_silently_borrow_primary_send_channel(self):
        row=self.register()
        with self.assertRaisesRegex(ValueError,'发送账号'):admin.save(self.body(row,['outreach']))
    def test_stale_im_status_is_not_ready(self):
        self.register()
        with patch.object(uid_session,'local_status',return_value=dict(im_read_verified=True,checked_at=1)),patch.object(uid_session,'load',side_effect=ValueError('expired')):
            self.assertFalse(admin.state()['accounts'][0]['im_session']['im_read_verified'])
    def test_primary_login_uses_existing_flow(self):
        self.register('9517','222',storage='primary')
        with patch.object(threading.Thread,'start') as start:
            with self.assertRaisesRegex(ValueError,'既有'):admin.start_login(dict(account_id='9517'))
            start.assert_not_called()
    def test_login_cannot_take_profile_from_unfinished_verification(self):
        self.register(roles=['comments'])
        with app.db() as c:
            c.execute("INSERT INTO collection_tasks(request_id,kind,target,video_limit,comment_limit,interactive,status,created_at,updated_at) VALUES('held','search','fixture',1,30,1,'needs_verification',?,?)",(app.now(),app.now()))
            task=c.execute('SELECT MAX(id) FROM collection_tasks').fetchone()[0]
            c.execute('INSERT INTO collection_task_accounts VALUES(?,?,?,?,?,?)',(task,'7446','111','isolated','comments',app.now()))
        with patch.object(threading.Thread,'start') as start:
            with self.assertRaisesRegex(ValueError,'在途'):admin.start_login(dict(account_id='7446'))
            start.assert_not_called()
    def test_recover_only_finishes_interrupted_login_jobs(self):
        with app.db() as c:c.execute("INSERT INTO account_login_jobs VALUES('fixture','7446','manual_required','fixture',?,?,NULL)",(app.now(),app.now()))
        admin.recover()
        with app.db() as c:
            row=c.execute("SELECT * FROM account_login_jobs WHERE id='fixture'").fetchone()
            self.assertEqual(row['status'],'interrupted');self.assertTrue(row['finished_at'])
    def test_other_roles_alternate_once_and_freeze_assignments(self):
        self.register('7446','111',['live']);self.register('9517','222',['live'])
        with app.db() as c:
            a=accounts.select_role(c,'live','live:1');b=accounts.select_role(c,'live','live:2')
            self.assertNotEqual(a['account_id'],b['account_id'])
            self.assertEqual(accounts.select_role(c,'live','live:1')['account_id'],a['account_id'])
            with self.assertRaises(ValueError):accounts.select_role(c,'groups','live:1')
    def test_thread_scopes_do_not_leak_credentials_or_move_business_db(self):
        barrier=threading.Barrier(2);seen=[]
        def run(account):
            with account_scope.use(dict(account_id=account,sender_uid=account,storage='isolated')):
                barrier.wait(timeout=5)
                seen.append((account,str(uid_session.vault_path()),str(app.DATA_DIR)))
        threads=[threading.Thread(target=run,args=(a,)) for a in ('7446','9517')]
        for t in threads:t.start()
        for t in threads:t.join(timeout=5)
        self.assertEqual(len(seen),2)
        for account,vault,directory in seen:
            self.assertEqual(Path(vault),self.root/'collection-accounts'/account/'private/uid-http/session.dpapi')
            self.assertEqual(directory,str(self.root))
        self.assertIsNone(account_scope.current())


if __name__=='__main__':unittest.main()
