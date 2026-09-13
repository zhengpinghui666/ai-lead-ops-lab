"""Synthetic public catalogs, self-only requests and durable join outcomes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock

import clubops as app
import group_public as public
import group_discovery as discovery
import group_monitor as monitor
import uid_protocol as wire
import uid_session
from test_uid_session import data,SENDER,RECEIVER

GID='7657809277079257637'
SEC='synthetic_public_author'
CANDIDATE=dict(group_id=GID,owner_sec_uid=SEC,name='瓦开黑群',description='瓦搭子群',participants=24,list_status=2)
VERIFIED=dict(CANDIDATE,code=7602,category='2',question='',entry_limit=[],join_allowance='-1',ticket='synthetic-ticket-secret',inviter=RECEIVER)


class ProtocolTests(unittest.TestCase):
    def client(self,**kwargs):
        with patch('uid_inbox._identity'):
            return public.Client(SENDER,provider=uid_session.Provider(memory=data()),**kwargs)

    def response(self,*,business=0,code=None,changes=None):
        def exchange(operation,prepared):
            self.assertEqual(operation,'group_join')
            request=wire.decode(prepared['payload']);body=wire.field(3,business)
            if code is not None:body+=wire.field(6,json.dumps({'status_code':code}))
            root={1:650,2:wire.one(request,2,0),3:0,4:'OK',5:0,13:int(SENDER),6:wire.field(650,body)}
            root.update(changes or {})
            return 200,'application/x-protobuf',b''.join(wire.field(k,v) for k,v in root.items())
        return exchange

    def test_self_only_body_and_provider_reject_other_participants(self):
        body=public.join_body(GID,SENDER,VERIFIED['ticket'],RECEIVER)
        self.assertEqual(public.validate_join_body(body,SENDER),GID)
        request=wire.decode(self.client().provider.prepare('group_join',body,dict(sender_uid=SENDER,command=650))['payload'])
        self.assertEqual(wire.one(request,6,0),0)
        payload=wire.one(wire.decode(body),650,2)
        for changed in (payload+wire.field(4,int(RECEIVER)),payload+wire.field(9,'other'),payload.replace(wire.field(4,int(SENDER)),wire.field(4,int(RECEIVER)))):
            with self.assertRaises(ValueError):self.client().provider.prepare('group_join',wire.field(650,changed),dict(sender_uid=SENDER,command=650))
        with self.assertRaises(ValueError):public.validate_join_body(wire.field(100,b''),SENDER)

    def test_join_association_and_pending_are_distinct_from_membership(self):
        self.assertEqual(self.client(exchange=self.response()).join(VERIFIED)['status'],'accepted')
        self.assertEqual(self.client(exchange=self.response(business=1,code=7601)).join(VERIFIED)['status'],'pending')
        self.assertEqual(self.client(exchange=self.response(business=1,code=999)).join(VERIFIED)['status'],'rejected')
        for changes in ({1:100},{2:1},{13:int(RECEIVER)},{5:1},{4:'Error'},{6:wire.field(100,b'')}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.client(exchange=self.response(changes=changes)).join(VERIFIED)

    def test_unmet_conditions_never_reach_transport(self):
        for changes in ({'question':'多少岁？'},{'entry_limit':[{'status':0}]},{'category':'1'},{'join_allowance':'0'},{'code':7507}):
            exchange=Mock()
            with self.assertRaises(ValueError):self.client(exchange=exchange).join(dict(VERIFIED,**changes))
            exchange.assert_not_called()

    def test_empty_ticket_matches_official_wrapper_but_missing_ticket_fails_locally(self):
        self.assertEqual(self.client(exchange=self.response()).join(dict(VERIFIED,ticket=''))['status'],'accepted')
        exchange=Mock()
        client=self.client(exchange=exchange)
        with self.assertRaises(ValueError):client.join(dict(VERIFIED,ticket=None))
        exchange.assert_not_called()
        self.assertEqual(client.join_evidence,dict(phase='prepare',submission_started=False))

    def test_question_answer_is_encoded_only_for_current_self_join(self):
        answer='年龄信息暂未提供。'
        body=public.join_body(GID,SENDER,'',RECEIVER,answer)
        self.assertEqual(public.validate_join_body(body,SENDER),GID)
        self.assertIn(answer.encode(),body)
        self.assertEqual(self.client(exchange=self.response(code=7601)).join(dict(VERIFIED,ticket='',question='多少岁？',group_audit_answer=answer))['status'],'pending')

    def test_unconfirmed_response_keeps_safe_stage_and_digest(self):
        client=self.client(exchange=lambda *a:(200,'text/html',b'synthetic-secret'))
        with self.assertRaises(ValueError):client.join(VERIFIED)
        self.assertTrue(client.join_evidence['submission_started'])
        self.assertEqual(client.join_evidence['phase'],'response')
        self.assertEqual(client.join_evidence['response_bytes'],16)
        self.assertNotIn('synthetic-secret',json.dumps(client.join_evidence))

    def test_catalog_missing_or_malformed_is_not_empty_success(self):
        client=self.client()
        for rows in ({'group_list':{}},{'group_list':''},{}, {'group_list':[dict(group_id=GID)]}):
            with patch.object(client,'read',return_value=rows),self.assertRaises(ValueError):client.catalog(SEC)
        with patch.object(client,'read',return_value={'group_list':None}):self.assertEqual(client.catalog(SEC),[])

    def test_verification_binds_owner_and_both_group_ids(self):
        client=self.client()
        row=dict(conversation_id=GID,conversation_short_id=GID,group_owner_info={'sec_owner_id':SEC},group_name='瓦群',
                 group_member_count=24,group_category='2',ticket='secret',inviter_id=RECEIVER,ext={'join_allowance':'-1'})
        def response(value):return {'data':{'verification':{'status_code':7602,'data':value}}}
        with patch.object(client,'read',return_value=response(row)):self.assertEqual(client.verify(CANDIDATE)['code'],7602)
        for change in ({'conversation_id':'999'},{'conversation_short_id':'999'},{'group_owner_info':{'sec_owner_id':'other'}}):
            with patch.object(client,'read',return_value=response(dict(row,**change))),self.assertRaises(ValueError):client.verify(CANDIDATE)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        previous=app.DATA_DIR;app.DATA_DIR=Path(self.temp.name);self.addCleanup(setattr,app,'DATA_DIR',previous)
        app.init();monitor.STOP.clear()
        identity=patch('uid_inbox_store._account',return_value=SENDER);identity.start();self.addCleanup(identity.stop)
        discovery.control({'enabled':True})
        self.rows=[CANDIDATE];self.verification=VERIFIED;self.members=[];self.calls=[];self.outcome='pending';self.joined=False
        import discovery_tracking
        with app.db() as c:
            c.execute("INSERT INTO sources(name,kind,status,notes) VALUES('synthetic','browser','verified','')")
            discovery_tracking.record(c,[dict(video_id='7684133774791075707',video_title='瓦搭子群公开群',author_sec_uid=SEC)],source='search',target='瓦搭子群')

    def catalog(self,*args,**kwargs):return dict(groups=self.members,has_more=False,evidence=[])

    def group(self,status='available'):
        return dict(conversation_id=GID,conversation_short_id=GID,name='瓦开黑群',description='瓦搭子群',notice='',member=True,participants=24,inbox=0)

    def client(self,account):
        self.assertEqual(account,SENDER)
        outer=self
        class Fake:
            def catalog(self,sec):outer.calls.append('catalog');return outer.rows
            def verify(self,row):outer.calls.append('verify');return outer.verification
            def join(self,value):
                outer.calls.append('join')
                with app.db() as c:outer.assertEqual(c.execute('SELECT status FROM public_group_attempts').fetchone()[0],'uncertain')
                if outer.joined:outer.members=[outer.group()]
                if outer.outcome=='timeout':raise TimeoutError('synthetic-ticket-secret')
                return dict(status=outer.outcome,proof={'http_status':200})
        return Fake()

    def tick(self):
        with app.db() as c:
            cfg=discovery.config(c);cfg['next_run_at']='';discovery.save(c,cfg)
        discovery.tick(client_factory=self.client,catalog_reader=self.catalog)

    def test_pending_and_timeout_are_never_resubmitted_or_called_joined(self):
        for outcome in ('pending','accepted','timeout','rejected'):
            with self.subTest(outcome=outcome):
                with app.db() as c:c.execute('DELETE FROM public_group_attempts');c.execute('DELETE FROM public_group_owners');c.execute('DELETE FROM public_group_candidates')
                self.calls=[];self.outcome=outcome;self.tick();self.tick()
                self.assertEqual(self.calls.count('join'),1)
                value=discovery.state();candidate=value['candidates'][0]
                self.assertEqual(candidate['status'],'uncertain' if outcome=='timeout' else outcome)
                self.assertNotIn('synthetic-ticket-secret',json.dumps(value))
                self.assertEqual(monitor.state()['enabled'],0)

    def test_only_verified_membership_enables_monitor(self):
        self.joined=True;self.outcome='accepted';self.tick()
        self.assertEqual(discovery.state()['candidates'][0]['status'],'joined')
        self.assertEqual(monitor.state()['enabled'],1)
        monitor.control({'id':1,'enabled':False});self.tick()
        self.assertEqual(monitor.state()['enabled'],0)
        self.assertEqual(self.calls.count('join'),1)

    def test_later_approval_reconciles_without_resubmission(self):
        self.tick();self.members=[self.group()];self.tick()
        self.assertEqual(discovery.state()['candidates'][0]['status'],'joined')
        self.assertEqual(monitor.state()['enabled'],1)
        self.assertEqual(self.calls.count('join'),1)

    def test_restrictions_and_scope_do_not_submit(self):
        cases=[({'name':'瓦片建材群','description':''},None,'unmatched'),({'list_status':5},None,'full'),
               ({},{'question':'多少岁？'},'question'),({},{'entry_limit':[{'status':0}]},'restricted'),
               ({},{'name':'购物群','description':''},'unmatched')]
        for row,verification,expected in cases:
            with self.subTest(expected=expected):
                with app.db() as c:c.execute('DELETE FROM public_group_candidates');c.execute('DELETE FROM public_group_owners')
                self.rows=[dict(CANDIDATE,**row)];self.verification=dict(VERIFIED,**(verification or {}));self.calls=[]
                self.tick();self.assertNotIn('join',self.calls)
                self.assertEqual(discovery.state()['candidates'][0]['status'],expected)

    def test_existing_paused_group_is_preserved(self):
        self.members=[self.group()];monitor.discover(reader=self.catalog);monitor.control({'id':1,'enabled':False})
        self.tick();self.assertNotIn('join',self.calls);self.assertEqual(monitor.state()['enabled'],0)

    def test_model_answer_binds_question_and_can_then_apply_once(self):
        import group_answers
        import semantic
        group_answers.STOP.clear()
        semantic.save(dict(semantic.DEFAULTS,enabled=True,model='synthetic:1'))
        self.verification=dict(VERIFIED,question='多少岁？')
        self.tick();self.assertNotIn('join',self.calls)
        draft=lambda question,settings:dict(answer='年龄暂未提供',fact_keys=[],unknown=True)
        self.assertTrue(group_answers.tick(predictor=draft))
        self.assertFalse(group_answers.tick(predictor=draft))
        self.tick();self.assertEqual(self.calls.count('join'),1)
        self.assertEqual(discovery.state()['candidates'][0]['answer'],'年龄信息暂未提供。')
        self.tick();self.assertEqual(self.calls.count('join'),1)

    def test_question_change_and_unknown_facts_cannot_reuse_or_invent_age(self):
        import group_answers
        result=group_answers.validate(dict(answer='我十八岁',fact_keys=['game'],unknown=False),'多少岁？')
        self.assertEqual(result['answer'],'年龄信息暂未提供。')
        with app.db() as c:
            discovery.record(c,SENDER,[CANDIDATE])
            discovery.remember_question(c,SENDER,GID,'多少岁？')
        discovery.answer(dict(group_id=GID,question='多少岁？',answer='合成答案'))
        with app.db() as c:
            self.assertEqual(discovery.remember_question(c,SENDER,GID,'玩什么游戏？'),'')
        with self.assertRaises(ValueError):discovery.answer(dict(group_id=GID,question='多少岁？',answer='旧题答案'))

    def test_model_answer_after_pause_is_historical_only(self):
        import group_answers
        import semantic
        group_answers.STOP.clear()
        semantic.save(dict(semantic.DEFAULTS,enabled=True,model='synthetic:1'))
        self.verification=dict(VERIFIED,question='多少岁？');self.tick()
        def pause(question,settings):
            discovery.control({'enabled':False})
            return dict(answer='相关信息暂未提供。',fact_keys=[],unknown=True)
        self.assertTrue(group_answers.tick(predictor=pause))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT answer FROM public_group_questions').fetchone()[0],'')
            self.assertEqual(c.execute('SELECT status FROM public_group_answer_runs').fetchone()[0],'stale')

    def test_readonly_verification_proves_pending_without_rejoining(self):
        with app.db() as c:
            discovery.record(c,SENDER,[CANDIDATE])
            proof=dict(phase='platform_result',submission_started=True,http_status=200)
            c.execute("INSERT INTO public_group_attempts VALUES(?,?,?,?,?,?)",(SENDER,GID,app.now(),app.now(),'uncertain',json.dumps(proof)))
        self.verification=dict(VERIFIED,code=7601)
        discovery.reconcile_application_status(SENDER,client_factory=self.client)
        self.assertEqual(self.calls,['verify'])
        with app.db() as c:
            row=c.execute('SELECT status,proof FROM public_group_attempts').fetchone()
            self.assertEqual(row['status'],'pending')
            saved=json.loads(row['proof'])
            self.assertEqual({k:saved[k] for k in proof},proof)
            self.assertEqual(saved['status_verification']['code'],7601)
        discovery.reconcile_application_status(SENDER,client_factory=self.client)
        self.assertEqual(self.calls,['verify'])

    def test_pause_and_account_change_stop_public_discovery(self):
        discovery.control({'enabled':False});self.tick();self.assertEqual(self.calls,[])
        discovery.control({'enabled':True})
        with patch('uid_inbox_store._account',return_value=RECEIVER):self.tick()
        self.assertEqual(self.calls,[])
        with self.assertRaises(ValueError):discovery.control({'enabled':True},'demo')

    def test_daily_submission_budget_and_search_scope(self):
        with app.db() as c:
            self.assertEqual(discovery.search_terms(c),discovery.QUERIES)
            for n in range(4):c.execute('INSERT INTO public_group_attempts VALUES(?,?,?,?,?,?)',(SENDER,str(9000+n),app.now(),app.now(),'rejected','{}'))
        self.tick();self.assertNotIn('join',self.calls)


if __name__=='__main__':unittest.main()
