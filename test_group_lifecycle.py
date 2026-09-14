import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clubops as app
import group_inbox
import group_monitor as monitor
import group_lifecycle as exits
import uid_protocol as wire
import uid_session
from test_uid_session import data,SENDER,RECEIVER

GROUP=dict(account_uid=SENDER,conversation_id='7124614724230103565',conversation_short_id='7124614724230103565',
           name='瓦群',description='',notice='',member=True,participants=217,inbox=0)


class ProtocolTests(unittest.TestCase):
    def test_admin_only_requires_both_explicit_platform_flags(self):
        core=wire.field(1,GROUP['conversation_id'])+wire.field(2,int(GROUP['conversation_short_id']))+wire.field(3,2)
        for status,normal,expected in [(1,1,True),(1,0,False),(0,1,False),(2,1,False),(3,1,False)]:
            row=core+wire.field(8,1)+wire.field(50,core+wire.field(14,status)+wire.field(15,normal))
            self.assertEqual(group_inbox.group_row(wire.decode(row))['admin_only'],expected)

    def test_leave_is_self_only_group_command_and_response_bound(self):
        for wrong in (False,True):
            provider=uid_session.Provider(memory=data());calls=[]
            def exchange(operation,prepared):
                calls.append(operation)
                if operation=='identity':return 200,'application/json',json.dumps({'status_code':0,'user':{'uid':SENDER}}).encode()
                self.assertEqual(operation,'group_leave')
                req=wire.decode(prepared['payload']);self.assertEqual(wire.one(req,6,0),0)
                body=wire.one(req,8,2);self.assertEqual(exits.validate_body(body)['conversation_id'],GROUP['conversation_id'])
                fields={1:652,2:wire.one(req,2,0),3:0,4:'OK',5:0,13:int(RECEIVER if wrong else SENDER)}
                return 200,'application/x-protobuf',b''.join(wire.field(k,v) for k,v in fields.items())
            if wrong:
                with self.assertRaises(ValueError):exits.leave(SENDER,GROUP,provider=provider,exchange=exchange)
            else:self.assertEqual(exits.leave(SENDER,GROUP,provider=provider,exchange=exchange)['status'],'accepted')
            self.assertEqual(calls,['identity','group_leave'])
        with self.assertRaises(ValueError):exits.validate_body(wire.field(652,wire.field(1,GROUP['conversation_id'])+wire.field(2,123)+wire.field(3,1)))
        with self.assertRaises(ValueError):exits.leave(RECEIVER,GROUP)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        old=app.DATA_DIR;app.DATA_DIR=Path(temp.name);self.addCleanup(setattr,app,'DATA_DIR',old)
        p=patch('uid_inbox_store._account',return_value=SENDER);p.start();self.addCleanup(p.stop)
        app.init();monitor.STOP.clear()
        monitor.discover(reader=lambda *a,**kw:dict(groups=[GROUP],has_more=False,evidence=[]))

    def enable(self):
        return exits.control(dict(account_uid=SENDER,conversation_id=GROUP['conversation_id'],admin_only=True,apply_to_similar=True))

    def test_accepted_is_not_left_and_partial_catalog_cannot_prove_exit(self):
        self.enable();calls=[]
        def leaver(*a):calls.append(a);return dict(status='accepted',proof={'platform_code':0})
        self.assertTrue(exits.process_one(SENDER,leaver=leaver));self.assertFalse(exits.process_one(SENDER,leaver=leaver))
        with app.db() as c:
            exits.observe(c,SENDER,[],False)
            self.assertEqual(c.execute('SELECT status FROM group_exits').fetchone()[0],'accepted')
            self.assertTrue(exits.excluded(c,SENDER,GROUP['conversation_id']))
            self.assertFalse(exits.excluded(c,RECEIVER,GROUP['conversation_id']))
            exits.observe(c,SENDER,[],True)
            self.assertEqual(c.execute('SELECT status FROM group_exits').fetchone()[0],'left')
        self.assertEqual(len(calls),1)

    def test_unknown_submission_not_retried_or_reenabled(self):
        self.enable()
        def failed(*a):raise TimeoutError('credential must never be persisted')
        exits.process_one(SENDER,leaver=failed)
        self.assertFalse(exits.process_one(SENDER,leaver=failed))
        monitor.discover(reader=lambda *a,**kw:dict(groups=[GROUP],has_more=False,evidence=[]))
        with self.assertRaisesRegex(ValueError,'排除'):monitor.control(dict(id=1,enabled=True))
        with app.db() as c:
            row=c.execute('SELECT * FROM group_exits').fetchone()
            self.assertEqual(row['status'],'unknown');self.assertNotIn('credential',row['proof'])
            self.assertEqual(c.execute('SELECT enabled FROM monitored_groups WHERE id=1').fetchone()[0],0)

    def test_similar_rule_ignores_empty_history_and_unrelated_accounts(self):
        self.enable()
        other=dict(GROUP,conversation_id='7124614724230103566',name='演员群',admin_only=False)
        admin=dict(other,conversation_id='7124614724230103567',admin_only=True)
        monitor.discover(reader=lambda *a,**kw:dict(groups=[GROUP,other,admin],has_more=False,evidence=[]))
        with app.db() as c:
            self.assertFalse(exits.excluded(c,SENDER,other['conversation_id']))
            self.assertTrue(exits.excluded(c,SENDER,admin['conversation_id']))
        with self.assertRaises(ValueError):exits.control(dict(account_uid=RECEIVER,conversation_id=GROUP['conversation_id'],admin_only=True))
