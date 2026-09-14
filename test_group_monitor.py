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
import uid_transport
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
    def test_oversized_catalog_shrinks_at_same_cursor_with_fresh_sequence(self):
        limits=[];sequences=[]
        def read(operation,prepared,body):
            limit=wire.one(body,4,0);limits.append(limit)
            sequences.append(wire.one(wire.decode(prepared['payload']),2,0))
            self.assertEqual(wire.one(body,2,0),42)
            if limit>2:
                raise uid_transport.TransportError('response_exceeds_bound','response_body',http_status=200,response_bytes=262145)
            return reply(operation,prepared,b'')
        result=group_inbox.catalog(SENDER,cursor=42,limit=20,provider=self.provider,exchange=self.exchange(read))
        self.assertEqual(limits,[20,10,5,2])
        self.assertEqual(len(set(sequences)),4)
        self.assertEqual(self.calls.count('identity'),1)
        self.assertEqual(result['page_limit'],2)
        self.assertEqual(sum(p.get('transport_error')=='response_exceeds_bound' for p in result['evidence']),3)
        self.assertFalse(result['has_more'])

    def test_catalog_does_not_retry_other_errors_or_oversized_single_entry(self):
        for reason,phase,status,limit in [('timeout','response_body',200,5),
                ('response_exceeds_bound','response_body',403,5),
                ('response_exceeds_bound','response_body',429,5),
                ('response_exceeds_bound','response_headers',200,5),
                ('response_exceeds_bound','response_body',200,1)]:
            with self.subTest(reason=reason,phase=phase,status=status,limit=limit):
                self.calls.clear()
                def read(*args):raise uid_transport.TransportError(reason,phase,http_status=status)
                with self.assertRaises(uid_transport.TransportError):
                    group_inbox.catalog(SENDER,limit=limit,provider=self.provider,exchange=self.exchange(read))
                self.assertEqual(self.calls,['identity','conversations'])

    def test_reduced_catalog_still_validates_response_identity_and_row_count(self):
        for invalid in ('identity','count'):
            with self.subTest(invalid=invalid):
                self.calls.clear()
                def read(operation,prepared,body):
                    if wire.one(body,4,0)>1:
                        raise uid_transport.TransportError('response_exceeds_bound','response_body',http_status=200)
                    if invalid=='identity':return reply(operation,prepared,b'',**{'13':int(RECEIVER)})
                    core=wire.field(1,GROUP['conversation_id'])+wire.field(2,int(GROUP['conversation_short_id']))+wire.field(3,2)
                    row=wire.field(1,core+wire.field(8,1)+wire.field(50,core))
                    return reply(operation,prepared,row+row)
                with self.assertRaises(ValueError):
                    group_inbox.catalog(SENDER,limit=2,provider=self.provider,exchange=self.exchange(read))
                self.assertEqual(self.calls,['identity','conversations','conversations'])

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
        ids=self.add([msg(),msg(11,'今天天气不错'),msg(12,uid=SENDER),msg(13,age=86401),msg(14,age=-60)])
        self.assertEqual(len(ids),1)
        self.assertEqual(queue.enqueue('group',ids)['queued'],1)
        with self.assertRaisesRegex(ValueError,'符合范围'):monitor.control(dict(id=2,enabled=True))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM group_messages').fetchone()[0],5)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM people').fetchone()[0],1)
            raw=self.raw(c)
            self.assertFalse(monitor.eligible(c,raw,semantic.state()['engine'],SENDER))
        self.assertEqual(self.add([msg()]),[])

    def test_verified_matching_group_coverage_is_not_capped_at_five(self):
        groups=[dict(GROUP,conversation_id=str(int(GROUP['conversation_id'])+i),conversation_short_id=str(int(GROUP['conversation_short_id'])+i),name=f'瓦搭子群{i}') for i in range(6)]
        monitor.discover(reader=lambda *a,**kw:dict(groups=groups,has_more=False,evidence=[]))
        with app.db() as c:
            ids=[r[0] for r in c.execute('SELECT id FROM monitored_groups WHERE matched=1 AND member=1')]
        self.assertEqual(len(ids),6)
        for rid in ids:monitor.control(dict(id=rid,enabled=True))
        self.assertEqual(monitor.state()['enabled'],6)

    def test_small_catalog_pages_cover_more_than_five_pages(self):
        calls=[]
        def read(account,*,cursor,limit):
            calls.append((cursor,limit))
            group=dict(GROUP,conversation_id=str(int(GROUP['conversation_id'])+cursor),
                       conversation_short_id=str(int(GROUP['conversation_short_id'])+cursor))
            return dict(groups=[group],has_more=cursor<11,next_cursor=str(cursor+1),page_limit=1,evidence=[])
        result=monitor.discover(reader=read)
        self.assertTrue(result['complete']);self.assertEqual(result['discovered'],12)
        self.assertEqual(calls,[(0,5)]+[(i,1) for i in range(1,12)])
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM monitored_groups WHERE member=1').fetchone()[0],12)

    def test_partial_catalog_keeps_unseen_members_and_rejects_nonadvancing_page(self):
        def read(account,*,cursor,limit):
            group=dict(GROUP,conversation_id=str(8000000000000000000+cursor),
                       conversation_short_id=str(8000000000000000000+cursor))
            return dict(groups=[group],has_more=True,next_cursor=str(cursor+1),page_limit=1,evidence=[])
        result=monitor.discover(reader=read)
        self.assertFalse(result['complete']);self.assertEqual(result['discovered'],100)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT member,enabled FROM monitored_groups WHERE id=1').fetchone()[:],(1,1))
            before=c.execute('SELECT COUNT(*) FROM group_reads').fetchone()[0]
        with self.assertRaisesRegex(ValueError,'nonadvancing_cursor'):
            monitor.discover(reader=lambda *a,**k:dict(groups=[GROUP],has_more=True,next_cursor='0',evidence=[]))
        with app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM group_reads').fetchone()[0],before)

    def test_catalog_transport_failure_records_stage_without_consuming_message_progress(self):
        def read(*args,**kwargs):
            raise uid_transport.TransportError('response_exceeds_bound','response_body',http_status=200,response_bytes=262145)
        with patch('group_inbox.messages') as messages:
            monitor.tick(catalog_reader=read)
            messages.assert_not_called()
        with app.db() as c:
            group=dict(c.execute('SELECT * FROM monitored_groups WHERE id=1').fetchone())
            record=dict(c.execute('SELECT * FROM group_reads ORDER BY id DESC LIMIT 1').fetchone())
        self.assertEqual((group['status'],group['failures'],group['watermark'],group['cursor']),('retrying',1,'0','0'))
        self.assertEqual(record['operation'],'catalog')
        self.assertEqual(json.loads(record['detail'])['evidence']['transport_error'],'response_exceeds_bound')

    def test_idle_groups_slow_down_and_new_text_restores_fast_polling(self):
        with app.db() as c:
            group=dict(c.execute('SELECT * FROM monitored_groups WHERE id=1').fetchone())
            self.assertEqual(monitor.read_cadence(c,group)[0],60)
            group['last_read_at']=app.now()
            for hours,delay in ((7,300),(25,900)):
                c.execute('DELETE FROM group_reads')
                c.execute("INSERT INTO group_reads(group_id,account_uid,operation,status,detail,created_at) VALUES(1,?,'messages','completed','',?)",(SENDER,monitor.stamp_after(-hours*3600)))
                self.assertEqual(monitor.read_cadence(c,group)[0],delay)
        self.add([msg(text='有人打不')])
        with app.db() as c:
            self.assertEqual(monitor.read_cadence(c,group)[0],60)
            self.assertEqual(c.execute('SELECT member FROM monitored_groups WHERE id=1').fetchone()[0],1)

    def test_group_game_quote_requires_whole_alias_and_never_implies_buyer(self):
        source=dict(kind='group',text='有没有一起玩的啊',title='瓦搭子群',parent='')
        value=prediction(source,'uncertain','uncertain');value['game']=app.TARGET_GAME
        value['evidence'].append(dict(field='game',source='title',text='瓦'))
        with self.assertRaises(ValueError):semantic.validate_result(value,source)
        value['evidence'][-1]['text']='瓦搭子'
        self.assertEqual(semantic.validate_result(value,source)['category'],'uncertain')
        self.assertEqual(semantic.validate_result(value,dict(source,kind='comment'))['category'],'uncertain')

    def test_missed_group_demand_within_day_reaches_model_and_older_stays_history(self):
        ids = self.add([msg(age=23*3600), msg(11, age=86401)])
        self.assertEqual(len(ids), 1)
        self.assertEqual(queue.enqueue('group', ids)['queued'], 1)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM group_messages').fetchone()[0], 2)
            self.assertFalse(monitor.routing(c, 2)['model_allowed'])
            self.assertIn('超过一天', c.execute('SELECT filter_reason FROM group_messages WHERE id=2').fetchone()[0])

    def test_legacy_hour_filter_is_reconsidered_once_with_old_evidence_preserved(self):
        self.add([msg(age=7200), msg(11, age=86401), msg(12, '今天天气不错', age=7200)])
        with app.db() as c:
            c.execute("UPDATE group_messages SET filter_reason='时间缺失或消息超过 1 小时',person_id=NULL")
            before = [tuple(r) for r in c.execute('SELECT id,raw_text,published_at,observed_at,category,reason,facts FROM group_messages ORDER BY id')]
        self.assertEqual(monitor.reconsider_legacy_time_filters(), [1, 3])
        self.assertEqual(monitor.reconsider_legacy_time_filters(), [])
        with app.db() as c:
            after = [tuple(r) for r in c.execute('SELECT id,raw_text,published_at,observed_at,category,reason,facts FROM group_messages ORDER BY id')]
            self.assertEqual(before, after)
            self.assertEqual(c.execute('SELECT filter_reason FROM group_messages WHERE id=1').fetchone()[0], '')
            self.assertEqual(c.execute('SELECT filter_reason FROM group_messages WHERE id=3').fetchone()[0], '未通过陪玩需求初筛')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM group_reads WHERE operation='rescreen'").fetchone()[0], 2)
            history = json.loads(c.execute("SELECT detail FROM group_reads WHERE operation='rescreen' ORDER BY id LIMIT 1").fetchone()[0])
            self.assertEqual(history['previous_filter_reason'], '时间缺失或消息超过 1 小时')
            self.assertIsNotNone(c.execute('SELECT person_id FROM group_messages WHERE id=1').fetchone()[0])

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

    def test_old_invitation_gate_rescreens_once_and_waits_for_model(self):
        texts = ('现在有打的吗？', '白银局来个q男两个妹子', '黄金局缺个大佬', '我青铜')
        self.add([msg(20+i, text, age=7200) for i, text in enumerate(texts)])
        self.add([msg(30, '有人打不', age=86401), msg(31, '有人打不', uid=SENDER)])
        with app.db() as c:
            old = dict(version='comment-relevance-v2', passed=False, reason='old gate', evidence=[])
            c.execute("UPDATE group_messages SET filter_reason='未通过陪玩需求初筛',relevance=?,person_id=NULL", (json.dumps(old),))
            before = [tuple(r) for r in c.execute('SELECT id,raw_text,published_at,observed_at,category,reason,facts FROM group_messages ORDER BY id')]
        self.assertEqual(set(monitor.reconsider_keyword_filters()), {1, 2, 3, 4, 6})
        self.assertEqual(monitor.reconsider_keyword_filters(), [])
        with app.db() as c:
            after = [tuple(r) for r in c.execute('SELECT id,raw_text,published_at,observed_at,category,reason,facts FROM group_messages ORDER BY id')]
            self.assertEqual(before, after)
            passed = [r[0] for r in c.execute("SELECT id FROM group_messages WHERE filter_reason=''")]
            self.assertEqual(passed, [1, 2, 3])
            self.assertFalse(monitor.eligible(c, self.raw(c), semantic.state()['engine'], SENDER))
            audit = [json.loads(r[0]) for r in c.execute("SELECT detail FROM group_reads WHERE operation='rescreen'")]
            self.assertTrue(all(r['previous_relevance']==old for r in audit))
            self.assertEqual(len(audit), 5)
        self.assertEqual(queue.enqueue('group', passed)['queued'], 3)

    def test_pc_notice_excluding_mobile_is_readable_but_commercial_ban_blocks_outreach(self):
        from game_scope import record_exclusion
        notice='本群仅面向PC端无畏契约，手游手瓦玩家请勿加入，群内不交流手游相关内容。\n严格禁止人员：所有陪玩、代练，无论私聊还是群发广告，一经发现拉黑。'
        self.assertTrue(monitor.match(dict(GROUP,notice=notice)))
        self.add([msg()])
        with app.db() as c:
            c.execute('UPDATE monitored_groups SET notice=?',(notice,))
            self.assertEqual(record_exclusion(c,'group',1),'')
            self.assertTrue(monitor.routing(c,1)['model_allowed'])
            with patch.object(monitor,'project',return_value=dict(analysis_method='model',category='buyer',game=app.TARGET_GAME)):
                self.assertFalse(monitor.eligible(c,self.raw(c),semantic.state()['engine'],SENDER))
                c.execute("UPDATE monitored_groups SET notice='仅限端游玩家'")
                self.assertTrue(monitor.eligible(c,self.raw(c),semantic.state()['engine'],SENDER))
            c.execute("UPDATE group_messages SET raw_text='手瓦找陪玩'")
            self.assertTrue(record_exclusion(c,'group',1))
            self.assertFalse(monitor.routing(c,1)['model_allowed'])

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
