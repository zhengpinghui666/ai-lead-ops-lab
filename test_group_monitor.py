"""Isolated group reads, screening, model gates and shared send ledger."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone,timedelta

import clubops as app
import group_monitor as monitor
import group_inbox
import analysis_store as store
import semantic
import semantic_queue as queue
import intent_outreach as outreach
import uid_protocol as wire
import uid_session
from test_uid_inbox import InboxTests,reply
from test_uid_session import data,SENDER,RECEIVER
from test_semantic import prediction

GROUP=dict(conversation_id='7657809277079257637',conversation_short_id='7657809277079257637',
           name='瓦搭子群',description='',notice='',member=True,participants=156,inbox=0)


def msg(index=10,text='无畏契约找陪练，预算100元',uid=RECEIVER,age=0):
    stamp=datetime.now(timezone.utc)-timedelta(seconds=age)
    return dict(message_id=str(9007199254741000+index),index=str(index),uid=uid,raw_text=text,
                created_at_raw=str(int(stamp.timestamp()*1000)))


def page(messages,more=False,cursor=0):
    indices=[int(m['index']) for m in messages]
    return dict(messages=messages,has_more=more,next_cursor=str(cursor),evidence=[],skipped=0,
                minimum_index=str(min(indices)) if indices else None,maximum_index=str(max(indices)) if indices else None,
                group_sent=False,read_marker_requested=False)


class ProtocolTests(InboxTests):
    def test_group_shape_and_no_group_write_operation(self):
        def read(operation,prepared,body):
            if operation=='conversations':
                self.assertEqual(wire.one(body,3,0),2)
                core=wire.field(1,GROUP['conversation_id'])+wire.field(2,int(GROUP['conversation_short_id']))+wire.field(3,2)+wire.field(5,GROUP['name'])
                row=core+wire.field(4,'SECRET')+wire.field(7,156)+wire.field(8,1)+wire.field(50,core)
                return reply(operation,prepared,wire.field(1,row))
            self.assertEqual(wire.one(body,2,0),2)
            self.assertEqual(wire.one(body,4,0),3)
            m=msg();row=b''.join(wire.field(k,v) for k,v in {1:GROUP['conversation_id'],2:2,3:int(m['message_id']),4:10,
              5:int(GROUP['conversation_short_id']),6:7,7:int(RECEIVER),8:json.dumps({'text':m['raw_text']}),10:int(m['created_at_raw'])}.items())
            return reply(operation,prepared,wire.field(1,row))
        groups=group_inbox.catalog(SENDER,provider=self.provider,exchange=self.exchange(read))
        self.assertNotIn('SECRET',json.dumps(groups))
        result=group_inbox.messages(SENDER,groups['groups'][0],provider=self.provider,exchange=self.exchange(read))
        self.assertEqual(result['messages'][0]['uid'],RECEIVER)
        self.assertEqual(self.calls,['identity','conversations','identity','messages'])
        with self.assertRaises(ValueError):group_inbox.messages(SENDER,dict(GROUP,member=False))

    def test_other_conversation_is_rejected(self):
        def read(operation,prepared,body):
            row=wire.field(1,'wrong-group')+wire.field(2,2)+wire.field(5,int(GROUP['conversation_short_id']))
            return reply(operation,prepared,wire.field(1,row))
        with self.assertRaisesRegex(ValueError,'message_conversation_mismatch'):
            group_inbox.messages(SENDER,GROUP,provider=self.provider,exchange=self.exchange(read))


class MonitorTests(unittest.TestCase):
    def test_foreign_region_group_context_blocks_existing_message(self):
        ids=self.add([msg()])
        with app.db() as c:
            c.execute("UPDATE monitored_groups SET notice='无畏契约亚服开黑'")
            self.assertFalse(monitor.routing(c,ids[0])['model_allowed'])
            self.assertFalse(monitor.eligible(c,self.raw(c),semantic.state()['engine'],SENDER))

    def test_mobile_message_and_changed_group_notice_block_model_and_outreach(self):
        ids=self.add([msg(text='手瓦找陪玩，预算100元')])
        with app.db() as c:
            self.assertFalse(monitor.routing(c,ids[0])['model_allowed'])
            self.assertFalse(monitor.eligible(c,self.raw(c),semantic.state()['engine'],SENDER))
        self.add([msg(index=11)])
        with app.db() as c:
            rid=c.execute('SELECT MAX(id) FROM group_messages').fetchone()[0]
            c.execute("UPDATE monitored_groups SET notice='本群只玩无畏契约手游'")
            self.assertFalse(monitor.routing(c,rid)['model_allowed'])
        with self.assertRaises(ValueError):monitor.control(dict(id=1,enabled=True))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        previous=app.DATA_DIR;app.DATA_DIR=Path(self.temp.name);self.addCleanup(setattr,app,'DATA_DIR',previous)
        app.init();monitor.STOP.clear();queue.STOP.clear();outreach.STOP.clear()
        p=patch('uid_inbox_store._account',return_value=SENDER);p.start();self.addCleanup(p.stop)
        settings=dict(semantic.DEFAULTS,enabled=True,auto_analyze=True,model='synthetic:1')
        semantic.save(settings)
        self.discover()
        monitor.control(dict(id=1,enabled=True))
        monitor.authorize_outreach('合成测试群聊私信授权',SENDER)
        outreach.authorize('点陪🥣看我主业','合成测试授权',SENDER)

    def catalog(self,account,**kwargs):
        self.assertEqual(account,SENDER)
        return dict(groups=[GROUP,dict(GROUP,conversation_id='7431071367245595175',conversation_short_id='7431071367245595175',name='生活朋友群')],has_more=False,evidence=[])

    def discover(self):return monitor.discover(reader=self.catalog)

    def add(self,messages):
        with app.db() as c:
            group=dict(c.execute('SELECT * FROM monitored_groups WHERE id=1').fetchone())
            return monitor.ingest(c,group,page(messages))

    def raw(self,c):return c.execute(monitor.SELECT+' WHERE m.filter_reason=\'\' ORDER BY m.id LIMIT 1').fetchone()

    def test_scope_time_self_and_keywords_precede_model(self):
        ids=self.add([msg(),msg(11,'今天天气不错'),msg(12,uid=SENDER),msg(13,age=3700),msg(14,age=-60)])
        self.assertEqual(len(ids),1)
        self.assertEqual(queue.enqueue('group',ids)['queued'],1)
        with self.assertRaisesRegex(ValueError,'符合范围'):monitor.control(dict(id=2,enabled=True))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM group_messages').fetchone()[0],5)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM people').fetchone()[0],1)
            raw=self.raw(c)
            self.assertFalse(monitor.eligible(c,raw,semantic.state()['engine'],SENDER))
        self.assertEqual(self.add([msg()]),[])

    def test_group_game_quote_requires_whole_alias_and_never_implies_buyer(self):
        source=dict(kind='group',text='有没有一起玩的啊',title='瓦搭子群',parent='')
        value=prediction(source,'uncertain','uncertain');value['game']=app.TARGET_GAME
        value['evidence'].append(dict(field='game',source='title',text='瓦'))
        with self.assertRaises(ValueError):semantic.validate_result(value,source)
        value['evidence'][-1]['text']='瓦搭子'
        self.assertEqual(semantic.validate_result(value,source)['category'],'uncertain')
        self.assertEqual(semantic.validate_result(value,dict(source,kind='comment'))['category'],'uncertain')

    def test_model_completed_buyer_required_and_disabled_or_stale_blocks(self):
        ids=self.add([msg()]);queue.enqueue('group',ids)
        class Buyer:
            def __init__(self,settings):pass
            def predict(self,source):
                result=prediction(source,'buyer');result['game']=app.TARGET_GAME
                result['evidence'].append(dict(field='game',source='text',text=app.TARGET_GAME))
                return result,'a'*64
        self.assertTrue(queue.run_one(adapter_factory=Buyer))
        with app.db() as c:
            job=c.execute('SELECT * FROM semantic_jobs').fetchone();self.assertEqual(job['status'],'completed',job['detail'])
            raw=self.raw(c);self.assertTrue(monitor.eligible(c,raw,semantic.state()['engine'],SENDER))
            self.assertFalse(monitor.eligible(c,raw,'another-engine',SENDER))
            c.execute('UPDATE monitored_groups SET member=0')
            self.assertFalse(monitor.eligible(c,raw,semantic.state()['engine'],SENDER))

    def test_paging_restarts_from_committed_cursor_and_never_reingests(self):
        calls=[]
        def read(account,group,cursor=0):
            calls.append(cursor)
            if cursor==0:return page([msg(30)],True,30)
            raise OSError('secret transport detail')
        monitor.tick(reader=read,catalog_reader=self.catalog)
        with app.db() as c:
            group=c.execute('SELECT * FROM monitored_groups WHERE id=1').fetchone()
            self.assertEqual((group['cursor'],group['cycle_head'],group['watermark']),('30','30','0'))
            self.assertNotIn('secret',group['detail'])
            c.execute('UPDATE monitored_groups SET next_run_at=?',(app.now(),))
        monitor.tick(reader=lambda *a,**k:page([msg(20)]),catalog_reader=self.catalog)
        with app.db() as c:
            group=c.execute('SELECT * FROM monitored_groups WHERE id=1').fetchone()
            self.assertEqual((group['cursor'],group['watermark']),('0','30'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM group_messages').fetchone()[0],2)
            c.execute('UPDATE monitored_groups SET next_run_at=?',(app.now(),))
        monitor.tick(reader=lambda *a,**k:page([msg(30)],True,30),catalog_reader=self.catalog)
        with app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM group_messages').fetchone()[0],2)

    def test_removed_membership_stops_reading_and_model(self):
        self.add([msg()])
        def empty(*args,**kwargs):return dict(groups=[],has_more=False,evidence=[])
        with patch('group_inbox.messages') as read:
            monitor.tick(catalog_reader=empty);read.assert_not_called()
        with app.db() as c:
            raw=self.raw(c);self.assertFalse(monitor.routing(c,raw['id'])['model_allowed'])
            self.assertEqual(c.execute('SELECT enabled FROM monitored_groups WHERE id=1').fetchone()[0],0)

    def test_backfilled_history_does_not_hide_new_messages_and_paging_is_stable(self):
        self.add([msg(i,age=i*3) for i in range(1,62)])
        first=monitor.state();self.assertEqual(len(first['messages']),50)
        self.assertEqual(first['messages'][0]['message_index'],'1')
        second=monitor.state(before=first['next_before'])
        self.assertEqual(len(second['messages']),11)
        self.assertFalse({m['id'] for m in first['messages']} & {m['id'] for m in second['messages']})

    def test_shared_ledger_and_actual_grant_validation(self):
        from test_uid_messaging import QueueTests
        self.add([msg()])
        with app.db() as c:
            raw=self.raw(c);source,_=store.inputs(c,'group',raw['id'])
            result=dict(category='buyer',game=app.TARGET_GAME,analysis_method='model',facts={},reason='合成模型结果')
            c.execute('''INSERT INTO intent_results(evidence_type,record_id,method,engine,request_id,input_hash,input_json,status,result_json,started_at)
              VALUES('group',?,'model',?,'synthetic',?,?,'completed',?,?)''',
              (raw['id'],semantic.state()['engine'],store.digest(source),json.dumps(source),json.dumps(result),app.now()))
        settings=dict(enabled=True,sender_uid=SENDER,allowed_recipient_uids=[RECEIVER])
        with patch('uid_messaging.config',return_value=(settings,[])),patch('uid_transport.send',side_effect=lambda *a:QueueTests.accepted(self,*a)) as send:
            outreach.tick();outreach.tick()
            send.assert_called_once()
        with app.db() as c:
            attempt=c.execute('SELECT * FROM uid_message_attempts').fetchone()
            self.assertEqual(attempt['status'],'accepted')
            self.assertIn('group_id',json.loads(attempt['evidence'])['operator_authorization'])
            self.assertIsNone(outreach.candidate(c,outreach.read(c,outreach.POLICY_KEY)))
            self.assertEqual(c.execute('SELECT contact_basis FROM people').fetchone()[0],'')


if __name__=='__main__':unittest.main()
