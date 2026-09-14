"""Account assignment regression tests, using synthetic reads and no platform writes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import clubops as app
import group_monitor as monitor
import group_accounts as groups
import group_discovery as discovery
import group_profiles
import account_scope
import collection_accounts
import uid_inbox_store
from test_group_monitor import GROUP,msg,page

class GroupAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.object(app,'DATA_DIR',Path(self.temp.name));p.start();self.addCleanup(p.stop)
        app.init();monitor.STOP.clear()
        self.a=dict(account_id='7446',sender_uid='111',storage='isolated')
        self.b=dict(account_id='9517',sender_uid='222',storage='primary')
        with app.db() as c:
            for row in (self.a,self.b):
                c.execute('INSERT INTO collection_accounts VALUES(?,?,?,?,?,?,?)',
                    (row['account_id'],row['sender_uid'],row['account_id'],row['storage'],1,'["groups"]',app.now()))
    def catalog(self,row,member=True):
        with account_scope.use(row):
            return monitor.discover(reader=lambda account,**kw:dict(groups=[dict(GROUP,member=member)],has_more=False,evidence=[]))
    def group(self,c,uid):return dict(c.execute('SELECT * FROM monitored_groups WHERE account_uid=?',(uid,)).fetchone())
    def test_membership_is_never_inherited_from_other_account(self):
        self.catalog(self.a)
        with groups.selected('9517'):
            self.assertEqual(uid_inbox_store._account(),'222')
            with app.db() as c:self.assertIsNone(c.execute("SELECT 1 FROM monitored_groups WHERE account_uid='222'").fetchone())
            with self.assertRaises(ValueError):monitor.control(dict(id=1,enabled=True))
        self.assertIsNone(account_scope.current())
    def test_one_group_one_owner_even_when_both_accounts_are_members(self):
        self.catalog(self.a);self.catalog(self.b)
        with groups.selected('7446'):monitor.control(dict(id=1,enabled=True))
        with groups.selected('9517'):monitor.control(dict(id=2,enabled=True))
        with app.db() as c:
            self.assertFalse(self.group(c,'111')['enabled']);self.assertTrue(self.group(c,'111')['member'])
            self.assertTrue(self.group(c,'222')['enabled'])
            self.assertEqual(groups.owner(c,GROUP['conversation_id']),'222')
    def test_removing_role_queues_public_owner_for_missing_membership(self):
        self.catalog(self.a)
        with groups.selected('7446'):monitor.control(dict(id=1,enabled=True))
        with app.db() as c:
            discovery.record(c,'111',[dict(group_id=GROUP['conversation_id'],owner_sec_uid='MS4wLjABAAAAfixtureOwner',name=GROUP['name'],description='',participants=10,list_status=0)])
            c.execute("UPDATE collection_accounts SET roles='[]',enabled=0 WHERE account_id='7446'")
            owners=groups.sources_for(c,'222')
            self.assertEqual(owners,['MS4wLjABAAAAfixtureOwner'])
            self.assertFalse(self.group(c,'111')['enabled']);self.assertTrue(self.group(c,'111')['member'])
            self.assertIsNone(c.execute("SELECT 1 FROM monitored_groups WHERE account_uid='222'").fetchone())
            self.assertEqual(groups.owner(c,GROUP['conversation_id']),'222')
    def test_join_is_confirmed_only_by_receiving_accounts_catalog(self):
        self.catalog(self.a)
        with app.db() as c:
            groups.claim(c,GROUP['conversation_id'],'222')
            c.execute("INSERT INTO public_group_attempts VALUES(?,?,?,?,?,'{}')",('222',GROUP['conversation_id'],app.now(),app.now(),'accepted'))
        with groups.selected('9517'):
            discovery.reconcile('222',reader=lambda account,**kw:dict(groups=[],has_more=False,evidence=[]))
            with app.db() as c:self.assertFalse(c.execute("SELECT 1 FROM monitored_groups WHERE account_uid='222'").fetchone())
            discovery.reconcile('222',reader=lambda account,**kw:dict(groups=[GROUP],has_more=False,evidence=[]))
        with app.db() as c:
            self.assertTrue(self.group(c,'222')['member']);self.assertTrue(self.group(c,'222')['enabled'])
            self.assertEqual(c.execute("SELECT status FROM public_group_attempts WHERE account_uid='222'").fetchone()[0],'joined')
    def test_handoff_does_not_duplicate_existing_message(self):
        self.catalog(self.a);self.catalog(self.b)
        with app.db() as c:
            a=self.group(c,'111');b=self.group(c,'222')
            self.assertEqual(len(monitor.ingest(c,a,page([msg()]))),1)
            self.assertEqual(monitor.ingest(c,b,page([msg()])),[])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM group_messages').fetchone()[0],1)
    def test_model_routing_uses_collecting_role_not_dm_account(self):
        self.catalog(self.a)
        with groups.selected('7446'):monitor.control(dict(id=1,enabled=True))
        with app.db() as c:
            mid=monitor.ingest(c,self.group(c,'111'),page([msg()]))[0]
            with patch('uid_messaging.config',return_value=({'sender_uid':'222'},[])):
                self.assertTrue(monitor.routing(c,mid)['model_allowed'])
            c.execute("UPDATE collection_accounts SET roles='[]',enabled=0 WHERE account_id='7446'")
            self.assertFalse(monitor.routing(c,mid)['model_allowed'])
    def test_profile_throttle_is_separate_per_account(self):
        self.catalog(self.a);self.catalog(self.b);seen=[]
        with app.db() as c:
            for row in (self.a,self.b):
                c.execute('UPDATE monitored_groups SET enabled=1 WHERE account_uid=?',(row['sender_uid'],))
                monitor.ingest(c,self.group(c,row['sender_uid']),page([msg(index=int(row['sender_uid']))]))
                c.execute("UPDATE group_profiles SET sec_uid='MS4wLjABAAAAfixture' WHERE account_uid=?",(row['sender_uid'],))
        for row in (self.a,self.b):
            with account_scope.use(row):
                group_profiles.tick(profile_reader=lambda account,targets: (seen.append(account) or dict(profiles=[],proof={})))
        self.assertEqual(seen,['111','222'])
        with app.db() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'group_profile_next_run:%'").fetchone()[0],2)
    def test_unknown_or_unassigned_account_cannot_start_group_work(self):
        with self.assertRaises(ValueError):groups.dispatch('group-discover',dict(account_id='missing'),'live')
        with app.db() as c:c.execute("UPDATE collection_accounts SET roles='[]',enabled=0 WHERE account_id='7446'")
        with patch.object(monitor,'refresh') as read:
            with self.assertRaises(ValueError):groups.dispatch('group-discover',dict(account_id='7446'),'live')
            read.assert_not_called()
    def test_dispatch_scopes_account_and_restores_context_after_failure(self):
        def check(mode):
            self.assertEqual(uid_inbox_store._account(),'111');raise ValueError('fixture')
        with patch.object(monitor,'refresh',side_effect=check):
            with self.assertRaises(ValueError):groups.dispatch('group-discover',dict(account_id='7446'),'live')
        self.assertIsNone(account_scope.current())
    def test_global_business_db_is_shared_under_scoped_credentials(self):
        with groups.selected('7446'):
            with app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_accounts').fetchone()[0],2)

if __name__=='__main__':unittest.main()
