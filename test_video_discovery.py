import copy
import json
from pathlib import Path
import tempfile
import time
import threading
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import collector_http as http
import collector_http_session as sessions
import video_discovery as discovery
from test_collector_http import session, FakeSigner, VIDEO, PARENT, body, record
import collector_http_worker as worker

AUTHOR = 'MS4wLjABAAAA_TEST_AUTHOR'


def item(vid=VIDEO, **kwargs):
    return {'aweme_id': vid, 'desc': '无畏契约找队友', 'author': {'sec_uid': AUTHOR},
            'create_time': int(time.time()), 'statistics': {'comment_count': 42}, **kwargs}


class DiscoveryTests(unittest.TestCase):
    def test_matching_work_visibility_restriction_is_not_an_account_failure(self):
        value={'status_code':0,'aweme_detail':None,'filter_detail':{'aweme_id':VIDEO,'filter_reason':'status_self_see'}}
        with self.assertRaises(http.ReadError) as caught:discovery.parse_discovery(value,'detail',video=VIDEO)
        self.assertEqual(discovery.work_restriction(caught.exception,VIDEO),'status_self_see')
        self.assertIsNone(discovery.work_restriction(caught.exception,PARENT))
        for change,expected in [({'filter_detail':{'aweme_id':PARENT,'filter_reason':'status_self_see'}},'schema_changed'),
                ({'filter_detail':{'aweme_id':VIDEO,'filter_reason':'unknown'}},'schema_changed'),
                ({'verify_data':'challenge'},'needs_verification'),({'status_code':1},'upstream_rejected')]:
            with self.subTest(change=change),self.assertRaises(http.ReadError) as failed:
                discovery.parse_discovery({**value,**change},'detail',video=VIDEO)
            self.assertEqual(failed.exception.status,expected)
            self.assertIsNone(discovery.work_restriction(failed.exception,VIDEO))
        shape=discovery.discovery_shape(value)
        self.assertEqual(shape['filter_reason'],'status_self_see')
        self.assertTrue(shape['filter_video_id_valid'])

    def test_explicit_private_work_is_scoped_and_other_denials_stay_failures(self):
        denied = {'status_code': 0, 'aweme_detail': None, 'filter_detail': {'aweme_id': VIDEO, 'filter_reason': 'author_secret'}}
        with self.assertRaises(http.ReadError) as caught:
            discovery.parse_discovery(denied, 'detail', video=VIDEO)
        self.assertTrue(discovery.is_private_work(caught.exception, VIDEO))
        for changed, status in [({'filter_detail': {}}, 'schema_changed'),
                ({'filter_detail': {'aweme_id': PARENT, 'filter_reason': 'author_secret'}}, 'schema_changed'),
                ({'filter_detail': {'aweme_id': VIDEO, 'filter_reason': 'unknown'}}, 'schema_changed'),
                ({'verify_type': 'challenge'}, 'needs_verification'), ({'status_code': 4}, 'upstream_rejected')]:
            with self.subTest(changed=changed), self.assertRaises(http.ReadError) as caught:
                discovery.parse_discovery({**denied, **changed}, 'detail', video=VIDEO)
            self.assertEqual(caught.exception.status, status)
            self.assertFalse(discovery.is_private_work(caught.exception, VIDEO))

    def test_private_work_does_not_request_comments_or_stop_public_work(self):
        ids = [VIDEO, str(int(VIDEO)+1), str(int(VIDEO)+2)]
        for reason in ('author_secret','status_self_see','status_audit_self_see','status_deleted',None):
            scoped=reason is not None
            calls = []
            class Client:
                def page(self, operation, **kw):
                    vid = kw['video']; calls.append((operation, vid))
                    if operation == 'detail':
                        if vid == ids[1]:
                            if not scoped: raise http.ReadError('access_denied')
                            return discovery.parse_discovery({'status_code': 0, 'aweme_detail': None,
                                'filter_detail': {'aweme_id': vid, 'filter_reason': reason}}, operation, video=vid)
                        return discovery.parse_discovery({'status_code': 0, 'aweme_detail': item(vid)}, operation, video=vid)
                    return http.parse_page(body([]), operation, vid)
            events = []
            worker.collect({'kind': 'video', 'target': '\n'.join(ids), 'video_limit': 3, 'comment_limit': 1,
                'page_concurrency': 1, 'refresh_video_metrics': True}, events.append, threading.Event(), client=Client())
            self.assertEqual(events[-1]['status'], 'completed' if scoped else 'access_denied')
            self.assertEqual([vid for op, vid in calls if op == 'comments'], [ids[0], ids[2]] if scoped else [ids[0]])
            cp = next(e for e in events if e['type'] == 'checkpoint' and e['video_id'] == ids[1])
            self.assertEqual(cp['status'], 'unavailable' if scoped else 'partial')
            if scoped:self.assertEqual(cp['reason'],reason)

    def test_audit_visibility_requires_exact_work_and_no_challenge(self):
        value={'status_code':0,'aweme_detail':None,'filter_detail':{'aweme_id':VIDEO,'filter_reason':'status_audit_self_see'}}
        for change,expected in [({},'access_denied'),
                ({'filter_detail':{'aweme_id':PARENT,'filter_reason':'status_audit_self_see'}},'schema_changed'),
                ({'verify_type':'challenge'},'needs_verification'),({'status_code':4},'upstream_rejected')]:
            with self.subTest(change=change),self.assertRaises(http.ReadError) as caught:
                discovery.parse_discovery({**value,**change},'detail',video=VIDEO)
            self.assertEqual(caught.exception.status,expected)
            self.assertEqual(discovery.work_restriction(caught.exception,VIDEO),'status_audit_self_see' if not change else None)

    def test_deleted_work_requires_exact_id_and_success_without_challenge(self):
        value={'status_code':0,'aweme_detail':None,'filter_detail':{'aweme_id':VIDEO,'filter_reason':'status_deleted'}}
        for change,status in [({},'access_denied'),
                ({'filter_detail':{'aweme_id':PARENT,'filter_reason':'status_deleted'}},'schema_changed'),
                ({'filter_detail':{'filter_reason':'status_deleted'}},'schema_changed'),
                ({'filter_detail':{'aweme_id':VIDEO,'filter_reason':'unknown'}},'schema_changed'),
                ({'verify_data':'challenge'},'needs_verification'),
                ({'status_code':1},'upstream_rejected'),({'status_code':False},'schema_changed')]:
            with self.subTest(change=change),self.assertRaises(http.ReadError) as caught:
                discovery.parse_discovery({**value,**change},'detail',video=VIDEO)
            self.assertEqual(caught.exception.status,status)
            self.assertEqual(discovery.work_restriction(caught.exception,VIDEO),'status_deleted' if not change else None)
        public=discovery.parse_discovery({**value,'aweme_detail':item()},'detail',video=VIDEO)
        self.assertEqual(public['rows'][0]['video_id'],VIDEO)

    def test_browser_and_http_game_scope_agree(self):
        titles=['無畏契約','无畏契約','VALORANT比赛','瓦羅蘭特','打瓦找队友','瓦陪','陪瓦','#瓦 #端游','手瓦陪玩',
                '装修瓷砖瓦片','普通健康科普','陪玩聊天截图','','7600000000000000001']
        cases=[{'video_title':t} for t in titles]
        result=subprocess.run(['node','-e',"let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(s).map(r=>require('./collector_parser.cjs').inSearchScope(r,'无畏契约陪玩')))));"],
            input=json.dumps(cases),text=True,capture_output=True,check=True,timeout=10,cwd=Path(__file__).resolve().parent,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        expected=[discovery.in_search_scope(r,'无畏契约陪玩') for r in cases]
        self.assertEqual(expected,[True]*8+[False]*6)
        self.assertEqual(json.loads(result.stdout),expected)
        self.assertTrue(discovery.in_search_scope({'video_title':''},'SYNTHETIC FIXTURE'))

    def test_http_search_skips_unrelated_work_before_comment_budget(self):
        for relevant in (True,False):
            calls=[]
            class Client:
                def page(self,operation,**kw):
                    calls.append((operation,kw))
                    if operation=='search':
                        if kw['cursor']==0:
                            rows=[{'video_id':'7600000000000000009','video_title':'合成不相关装修作品','video_url':'https://www.douyin.com/video/7600000000000000009'}]
                        else:rows=[{'video_id':VIDEO,'video_title':'無畏契約找队友','video_url':'https://www.douyin.com/video/'+VIDEO}]
                        return {'rows':rows,'skipped':0,'has_more':relevant and kw['cursor']==0,'cursor':kw['cursor']+1,'search_id':'synthetic'}
                    return http.parse_page(body([record()]),operation,VIDEO)
            events=[]
            worker.collect({'kind':'search','target':'无畏契约陪玩','video_limit':1,'comment_limit':1,'page_concurrency':1},
                           events.append,threading.Event(),client=Client())
            self.assertEqual(events[-1]['status'],'completed' if relevant else 'no_data')
            self.assertEqual([kw['video'] for op,kw in calls if op=='comments'],[VIDEO] if relevant else [])
            diagnostic=next(e['snapshot']['responses'][0] for e in events if e['type']=='diagnostic' and e['snapshot']['responses'][0].get('operation')=='search_scope')
            self.assertEqual(diagnostic['excluded_candidates'],1)
            self.assertEqual(len([e for e in events if e['type']=='comment']),int(relevant))

    def test_mobile_fixed_work_never_requests_comments_after_title_resolution(self):
        for cached in (True,False):
            calls=[]
            class Client:
                def page(self,operation,**kw):
                    calls.append(operation)
                    if operation!='detail':raise AssertionError('Mobile comments must not be requested')
                    return discovery.parse_discovery({'status_code':0,'aweme_detail':item(desc='手瓦陪玩')},operation,video=VIDEO)
            events=[]
            worker.collect({'kind':'video','target':VIDEO,'video_limit':1,'comment_limit':1,'page_concurrency':1,
                'resolve_video_titles':True,'known_video_titles':{VIDEO:'手瓦陪玩'} if cached else {}},
                events.append,threading.Event(),client=Client())
            self.assertEqual(calls,[] if cached else ['detail'])
            self.assertFalse(any(e['type']=='comment' for e in events))
            self.assertTrue(any(e['type']=='checkpoint' and e.get('reason')=='outside_pc_scope' for e in events))

    def test_optional_metrics_failure_keeps_comments_but_challenge_stops(self):
        for status in ('empty_response','needs_verification'):
            calls=[]
            class Client:
                def page(self,operation,**kw):
                    calls.append(operation)
                    if operation=='detail':raise http.ReadError(status)
                    return http.parse_page(body([record()]),operation,VIDEO)
            events=[]
            worker.collect({'kind':'video','target':VIDEO,'video_limit':1,'comment_limit':1,'page_concurrency':1,
                'resolve_video_titles':True,'refresh_video_metrics':True},events.append,threading.Event(),client=Client())
            self.assertEqual(calls,['detail','comments'] if status=='empty_response' else ['detail'])
            self.assertEqual(events[-1]['status'],'completed' if status=='empty_response' else status)

    def test_formal_worker_refreshes_stale_stats_and_keeps_fresh_cache(self):
        import video_metadata
        for cached in (None,video_metadata.extract({'statistics':{'digg_count':9}})):
            calls=[]
            class Client:
                def page(self,operation,**kw):
                    calls.append(operation)
                    if operation=='detail':return discovery.parse_discovery({'status_code':0,'aweme_detail':item(statistics={'digg_count':42})},operation,video=VIDEO)
                    return http.parse_page(body([record()]),operation,VIDEO,title=kw.get('title',''))
            events=[]
            worker.collect({'kind':'video','target':VIDEO,'video_limit':1,'comment_limit':1,'page_concurrency':1,
                'resolve_video_titles':True,'known_video_titles':{VIDEO:'cached'},'refresh_video_metrics':True,
                'known_video_metrics':{VIDEO:cached}},events.append,threading.Event(),client=Client())
            metrics=next(e['record']['metrics'] for e in events if e['type']=='video')
            self.assertEqual(metrics['likes'],9 if cached else 42)
            self.assertEqual(calls,['comments'] if cached else ['detail','comments'])

    def test_missing_video_title_resolves_once_and_cached_title_skips_detail(self):
        for cached in ('', '已保存的作品文案'):
            calls=[]
            class Client:
                def page(self,operation,**kw):
                    calls.append(operation)
                    if operation=='detail':return discovery.parse_discovery({'status_code':0,'aweme_detail':item()},operation,video=VIDEO)
                    return http.parse_page(body([record()]),operation,VIDEO,title=kw.get('title',''))
            events=[]
            worker.collect({'kind':'video','target':VIDEO,'video_limit':1,'comment_limit':1,'page_concurrency':1,
                'resolve_video_titles':True,'known_video_titles':{VIDEO:cached}},events.append,threading.Event(),client=Client())
            title=next(e['record']['video_title'] for e in events if e['type']=='video')
            self.assertEqual(title,cached or '无畏契约找队友')
            self.assertEqual(calls,['comments'] if cached else ['detail','comments'])
            self.assertEqual(events[-1]['status'],'completed')

    def test_processing_diagnostics_exclude_exception_message(self):
        class Client:
            def page(self,*args,**kwargs):raise RuntimeError('PRIVATE_TOKEN_AND_RESPONSE')
        events=[]
        worker.collect({'kind':'video','target':VIDEO,'video_limit':1,'comment_limit':10,'page_concurrency':1},
            events.append,threading.Event(),client=Client())
        processing=[e['snapshot']['responses'][0] for e in events if e['type']=='diagnostic']
        self.assertEqual(processing[0]['error_type'],'RuntimeError')
        self.assertEqual(processing[0]['video_id'],VIDEO)
        self.assertNotIn('PRIVATE_TOKEN',json.dumps(events))
        with patch('collector_http_session.load',side_effect=ValueError('PRIVATE_COOKIE')):
            events=[]
            worker.collect({'kind':'video','target':VIDEO,'video_limit':1,'comment_limit':10,'page_concurrency':1},
                events.append,threading.Event())
        self.assertEqual(events[-1]['status'],'session_expired')
        self.assertNotIn('PRIVATE_COOKIE',json.dumps(events))

    def test_reply_pages_rotate_between_parents(self):
        parents=[PARENT,str(int(PARENT)+1)];calls=[]
        class Client:
            def page(self,operation,**kw):
                if operation=='comments':
                    return http.parse_page(body([record(cid=p,reply_comment_total=5) for p in parents]),operation,VIDEO)
                parent,cursor=kw['parent'],kw['cursor'];calls.append((parent,cursor))
                cid=str(int(parent)+100+cursor*10)
                return http.parse_page(body([record(cid=cid,reply_id=parent)],has_more=int(cursor==0),cursor=cursor+1),
                    operation,VIDEO,parent)
        events=[]
        worker.collect({'kind':'video','target':VIDEO,'video_limit':1,'comment_limit':6,'page_concurrency':1},
            events.append,threading.Event(),client=Client())
        self.assertEqual(calls,[(parents[0],0),(parents[1],0),(parents[0],1),(parents[1],1)])

    def test_large_old_reply_thread_does_not_starve_second_main_page(self):
        calls=[]
        class Client:
            def page(self,operation,**kw):
                cursor=kw['cursor'];calls.append((operation,cursor))
                if operation=='comments':
                    rows=[record(cid=str(int(PARENT)+cursor+i),reply_comment_total=100 if i==0 else 0) for i in range(10)]
                else:
                    rows=[record(cid=str(int(PARENT)+100+cursor+i),reply_id=PARENT) for i in range(10)]
                return http.parse_page(body(rows,has_more=1,cursor=cursor+10),operation,VIDEO,kw.get('parent',''))
        events=[]
        worker.collect({'kind':'video','target':VIDEO,'video_limit':1,'comment_limit':30,'page_concurrency':1},
            events.append,threading.Event(),client=Client())
        self.assertEqual(calls,[('comments',0),('replies',0),('comments',10)])
        comments=[e['record'] for e in events if e['type']=='comment']
        self.assertEqual(len(comments),30)
        self.assertEqual(len([r for r in comments if not r['parent_comment_id']]),20)

    def test_existing_snapshot_derives_only_matching_unexpired_cookies(self):
        value = session()
        value['cookies']['comments'].append({'name':'narrow', 'value':'DO_NOT_FORWARD',
            'domain':'.douyin.com', 'path':sessions.PATHS['comments'], 'expires':-1})
        value['cookies']['identity'].append({'name':'expired', 'value':'OLD',
            'domain':'.douyin.com', 'path':'/', 'expires':time.time()-10})
        original = copy.deepcopy(value)
        for operation in ('detail','author','related'):
            self.assertEqual(sessions.cookie_header(value,operation),'sessionid=TEST_ONLY_SECRET')
        self.assertEqual(value,original)
        value['cookies']['search'][0]['value']='CONFLICT'
        with self.assertRaises(ValueError):sessions.cookie_header(value,'author')

    def test_fixed_routes_and_params_and_redacted_diagnostics(self):
        calls, diagnostics = [], []
        def exchange(url, headers, cancelled):
            parsed=urlsplit(url);calls.append((parsed,headers))
            if parsed.path==sessions.READ_PATHS['detail']:
                payload={'status_code':0,'aweme_detail':item()}
            else:payload={'status_code':0,'aweme_list':[item()], 'has_more':0,'max_cursor':0}
            return 200,'application/json',json.dumps(payload).encode()
        client=http.Client(session(),signer=FakeSigner(),transport=exchange,diagnostic=diagnostics.append)
        for operation in ('detail','author','related'):
            result=client.page(operation,video=VIDEO,sec_uid=AUTHOR)
            self.assertEqual(result['rows'][0]['video_id'],VIDEO)
        self.assertEqual([p.path for p,h in calls],[sessions.READ_PATHS[k] for k in ('detail','author','related')])
        self.assertEqual(parse_qs(calls[1][0].query)['sec_user_id'],[AUTHOR])
        self.assertEqual(parse_qs(calls[2][0].query)['filterGids'],[VIDEO])
        self.assertEqual(calls[1][1]['Referer'],sessions.ORIGIN+'/user/'+AUTHOR)
        self.assertNotIn('SECRET',json.dumps(diagnostics))
        self.assertNotIn('SIGNATURE',json.dumps(diagnostics))

    def test_wrong_identity_and_unknown_payload_are_never_candidates(self):
        for operation,payload in [('detail',{'status_code':0,'aweme_detail':item('123456')}),
                ('author',{'status_code':0,'aweme_list':[item(author={'sec_uid':'ANOTHER_AUTHOR'})],'has_more':0}),
                ('related',{'status_code':0,'data':[]}),('author',{'status_code':0,'aweme_list':[],'has_more':1,'max_cursor':0})]:
            with self.subTest(operation=operation),self.assertRaises(http.ReadError):
                discovery.parse_discovery(payload,operation,video=VIDEO,sec_uid=AUTHOR)
        unknown=discovery.parse_discovery({'status_code':0,'aweme_detail':item(create_time=None,statistics={})},'detail',video=VIDEO)
        self.assertIsNone(unknown['rows'][0]['published_at'])
        self.assertIsNone(unknown['rows'][0]['comment_count'])

    def test_author_empty_middle_page_continues_and_retains_both_sides(self):
        calls, diagnostics = [], []
        def exchange(url, headers, cancelled):
            parsed=urlsplit(url);query=parse_qs(parsed.query)
            if parsed.path==sessions.READ_PATHS['detail']:
                calls.append('detail')
                payload={'status_code':0,'aweme_detail':item()}
            else:
                cursor=int(query['max_cursor'][0]);calls.append(cursor)
                payload={0:dict(aweme_list=[item()],has_more=1,max_cursor=200),
                    200:dict(aweme_list=[],has_more=1,max_cursor=100),
                    100:dict(aweme_list=[item(PARENT)],has_more=0,max_cursor=0)}[cursor]
                payload.update(status_code=0,private_token='SECRET_VALUE',status_msg='PRIVATE_MESSAGE')
            return 200,'application/json',json.dumps(payload).encode()
        client=http.Client(session(),signer=FakeSigner(),transport=exchange,diagnostic=diagnostics.append)
        result=discovery.discover(client,[VIDEO],author_pages=3,include_related=False)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(calls,['detail',0,200,100])
        self.assertEqual({r['video_id'] for r in result['candidates']},{VIDEO,PARENT})
        middle=diagnostics[2]
        self.assertTrue(middle['has_more'])
        self.assertEqual((middle['rows'],middle['next_cursor']),(0,100))
        self.assertEqual(middle['page_visibility']['state'],'empty_page_with_more')
        self.assertEqual(middle['response_shape']['item_count'],0)
        for secret in ('SECRET_VALUE','private_token','PRIVATE_MESSAGE',AUTHOR):
            self.assertNotIn(secret,json.dumps(diagnostics))

    def test_author_empty_pages_consume_budget(self):
        calls=[]
        def exchange(url, headers, cancelled):
            parsed=urlsplit(url);calls.append(parsed.path)
            payload={'status_code':0,'aweme_detail':item()} if parsed.path==sessions.READ_PATHS['detail'] else {
                'status_code':0,'aweme_list':[],'has_more':1,'max_cursor':1000-len(calls)*100}
            return 200,'application/json',json.dumps(payload).encode()
        client=http.Client(session(),signer=FakeSigner(),transport=exchange)
        result=discovery.discover(client,[VIDEO],author_pages=3,include_related=False)
        self.assertEqual(len(calls),4)  # One detail and the original three-page budget.
        self.assertEqual(result['candidates'],[])
        self.assertFalse(result['all_douyin'])

    def test_empty_author_page_does_not_relax_invalid_envelopes_or_cursor_progress(self):
        base={'status_code':0,'aweme_list':[],'has_more':1,'max_cursor':100}
        for change in ({'max_cursor':200},{'max_cursor':300},{'max_cursor':0},{'max_cursor':-1},
                       {'max_cursor':True},{'max_cursor':'100'},{'has_more':True},{'aweme_list':None},
                       {'aweme_list':{}},{'status_code':True},{'status_code':4},{'verify_type':1}):
            with self.subTest(change=change), self.assertRaises(http.ReadError):
                discovery.parse_discovery({**base,**change},'author',sec_uid=AUTHOR,requested_cursor=200)
        with self.assertRaises(http.ReadError):
            discovery.parse_discovery({k:v for k,v in base.items() if k!='aweme_list'},'author',requested_cursor=200)
        # Other endpoints retain their own empty-with-more rejection.
        with self.assertRaises(http.ReadError):discovery.parse_discovery(base,'related')
        with self.assertRaises(http.ReadError):http.parse_page({'status_code':0,'comments':[],'has_more':1,'cursor':100},'comments',VIDEO)

    def test_author_nonadvancing_cursor_stops_after_preserving_first_page(self):
        calls=[];diagnostics=[]
        def exchange(url, headers, cancelled):
            parsed=urlsplit(url);calls.append(parsed.path)
            payload={'status_code':0,'aweme_detail':item()} if len(calls)==1 else {
                'status_code':0,'aweme_list':[item()] if len(calls)==2 else [],'has_more':1,'max_cursor':200}
            return 200,'application/json',json.dumps(payload).encode()
        client=http.Client(session(),signer=FakeSigner(),transport=exchange,diagnostic=diagnostics.append)
        result=discovery.discover(client,[VIDEO],author_pages=3,include_related=False)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(len(calls),3)
        self.assertEqual(len(result['candidates']),1)
        self.assertEqual(diagnostics[-1]['reason'],'non_advancing_author_cursor')

    def test_dedup_retains_multiple_sources_and_bounds_pages(self):
        calls=[]
        class Client:
            def page(self,operation,**kw):
                calls.append(operation)
                return discovery.parse_discovery({'status_code':0,
                    'aweme_detail':item(), 'aweme_list':[item('123456'),item('123456')],
                    'has_more':1,'max_cursor':100},operation,video=VIDEO,sec_uid=AUTHOR)
        result=discovery.discover(Client(),[VIDEO,VIDEO])
        self.assertEqual(calls,['detail','author','related'])
        self.assertEqual(len(result['candidates']),1)
        self.assertEqual([p['kind'] for p in result['candidates'][0]['provenance']],['author','related'])
        self.assertFalse(result['all_douyin'])
        self.assertFalse(result['comment_freshness_verified'])

    def test_failed_read_keeps_candidates_but_stops_expansion(self):
        calls=[]
        class Client:
            def page(self,operation,**kw):
                calls.append(operation)
                if operation=='related':raise http.ReadError('needs_verification')
                return discovery.parse_discovery({'status_code':0,'aweme_detail':item(),
                    'aweme_list':[item('123456')],'has_more':0},operation,video=VIDEO,sec_uid=AUTHOR)
        result=discovery.discover(Client(),[VIDEO,'123456'])
        self.assertEqual(calls,['detail','author','related'])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(len(result['candidates']),1)
        self.assertEqual(result['failures'][0]['status'],'needs_verification')

    def test_account_challenge_blocks_new_endpoints_search_only_does_not(self):
        with tempfile.TemporaryDirectory() as folder,patch('collector_http_session.path',return_value=Path(folder)/'session.dpapi'):
            value=session()
            sessions.record_endpoint_status(value,'search','needs_verification',verification_scope='search')
            client=http.Client(value,signer=FakeSigner())
            client.check_gate('author')
            sessions.record_endpoint_status(value,'related','needs_verification')
            with patch('collector_http.exchange') as exchange,self.assertRaises(http.ReadError):
                client.page('detail',video=VIDEO)
            exchange.assert_not_called()

    def test_request_budget_cancel_and_invalid_parameters_make_no_extra_reads(self):
        calls=[]
        def exchange(*args):
            calls.append(1)
            return 200,'application/json',json.dumps({'status_code':0,'aweme_detail':item()}).encode()
        client=http.Client(session(),signer=FakeSigner(),transport=exchange,request_limit=1)
        client.page('detail',video=VIDEO)
        with self.assertRaises(http.ReadError):client.page('detail',video=VIDEO)
        for kwargs in ({'operation':'author','sec_uid':'https://other.test'},
                {'operation':'detail','video':VIDEO,'cursor':1}, {'operation':'related','video':VIDEO,'count':True}):
            with self.assertRaises(ValueError):client.page(**kwargs)
        self.assertEqual(len(calls),1)
        client.cancelled=lambda:True
        with self.assertRaises(http.ReadError) as result:client.page('detail',video=VIDEO)
        self.assertEqual(result.exception.status,'cancelled')


if __name__=='__main__':unittest.main()
