"""Scheduler regression: inspect every work before draining old pages/replies."""
import threading,unittest,time
from collections import Counter
import collector_http as http,collector_http_worker as worker
from test_collector_http import record,body

VIDEOS=['7664994032866659594','7664994032866659595','7664994032866659596']

class HeadPriorityTests(unittest.TestCase):
    def run_batch(self,*,concurrency=2,cancel_at=None,gate=None,budget=None,replies=False,lane=None):
        calls=[];events=[];cancel=threading.Event();lock=threading.Lock()
        class Client:
            def page(inner,operation,**kw):
                vid=kw['video'];offset=kw['cursor']
                with lock:
                    if budget is not None and len(calls)>=budget:
                        raise http.ReadError('resource_limited',{'reason':'request_budget'})
                    calls.append((vid,operation,offset))
                if gate and vid==VIDEOS[1]:raise http.ReadError(gate)
                time.sleep(.002)
                rows=[record(cid=str(int(vid)+offset*100+10000+i+(50000 if operation=='replies' else 0)),
                    aweme_id=vid,reply_id=kw.get('parent','0'),reply_comment_total=int(replies and offset==0 and operation=='comments')) for i in range(10)]
                return http.parse_page(body(rows,has_more=int(operation=='comments' and offset==0),cursor=offset+10),operation,vid,kw.get('parent',''))
        def emit(event):
            with lock:events.append(event)
            if cancel_at=='first_comment' and event['type']=='comment':cancel.set()
            if cancel_at=='after_heads' and event.get('stage')=='work_read_queue' and event['snapshot']['processing']['phase']=='history_and_replies':cancel.set()
        worker.collect(dict(kind='video',target='\n'.join(VIDEOS),page_concurrency=concurrency,
            video_limit=3,comment_limit=20,discovery_job={'read_lane':lane} if lane else None),emit,cancel,client=Client())
        return calls,events

    def test_all_front_pages_precede_history_at_one_and_two_workers(self):
        for parallel in (1,2):
            with self.subTest(parallel=parallel):
                calls,events=self.run_batch(concurrency=parallel)
                self.assertEqual({v for v,op,c in calls[:3]},set(VIDEOS))
                self.assertTrue(all(op=='comments' and c==0 for _,op,c in calls[:3]),calls)
                self.assertTrue(all(c>0 for _,_,c in calls[3:]),calls)
                self.assertEqual(Counter(v for v,_,_ in calls),Counter({v:2 for v in VIDEOS}))
                self.assertEqual(events[-1]['status'],'completed')
                counts=[e for e in events if e['type']=='parallel']
                self.assertTrue(all(0<=e['active_pages']<=parallel and e['peak_pages']<=parallel for e in counts))
                self.assertEqual(counts[-1]['active_pages'],0)
                self.assertEqual(sum(e['type']=='comment' for e in events),60)

    def test_reply_pages_also_wait_for_other_front_pages(self):
        calls,events=self.run_batch(replies=True)
        self.assertTrue(all(op=='comments' and c==0 for _,op,c in calls[:3]),calls)
        self.assertTrue(any(op=='replies' for _,op,_ in calls[3:]),calls)
        self.assertEqual(events[-1]['status'],'completed')

    def test_cancel_closes_suspended_work_without_history(self):
        for where in ['first_comment','after_heads']:
            calls,events=self.run_batch(cancel_at=where)
            self.assertTrue(all(op=='comments' and c==0 for _,op,c in calls),calls)
            self.assertEqual(events[-1]['status'],'cancelled')
            self.assertEqual([e['active_pages'] for e in events if e['type']=='parallel'][-1],0)
            self.assertFalse(any(e['type']=='checkpoint' and e.get('status')=='done' for e in events))

    def test_account_platform_and_unknown_failures_prevent_history(self):
        for gate in ['needs_verification','needs_login','rate_limited','access_denied','schema_changed','network_error']:
            with self.subTest(gate=gate):
                calls,events=self.run_batch(concurrency=1,gate=gate)
                self.assertEqual(events[-1]['status'],gate)
                self.assertTrue(all(op=='comments' and c==0 for _,op,c in calls))
                self.assertFalse(any(e.get('stage')=='work_read_queue' and e['snapshot']['processing']['phase']=='history_and_replies' for e in events))
                self.assertEqual([e['active_pages'] for e in events if e['type']=='parallel'][-1],0)

    def test_existing_request_budget_keeps_unread_work_distinct(self):
        for budget in (2,3,4):
            with self.subTest(budget=budget):
                calls,events=self.run_batch(concurrency=1,budget=budget)
                self.assertEqual(len(calls),budget)
                read={v for v,_,_ in calls}
                done={e['video_id'] for e in events if e['type']=='checkpoint' and e.get('status')=='done'}
                self.assertEqual(done,read)
                self.assertEqual([e['active_pages'] for e in events if e['type']=='parallel'][-1],0)


class FrontMetadataTests(unittest.TestCase):
    def test_sparse_front_is_only_retired_after_explicit_same_work_restriction(self):
        import json
        from urllib.parse import urlsplit,parse_qs
        from test_collector_http import session,FakeSigner
        from test_video_discovery import item
        for outcome,expected in [('private','completed'),('public','schema_changed'),('foreign','schema_changed'),
                                 ('login','needs_login'),('rate','rate_limited'),('verify','needs_verification')]:
            calls=[];events=[]
            def exchange(url,headers,cancelled):
                vid=parse_qs(urlsplit(url).query)['aweme_id'][0]
                operation='detail' if '/detail/' in urlsplit(url).path else 'comments'
                calls.append((operation,vid))
                if operation=='detail':
                    if outcome in ('login','rate'):return (401 if outcome=='login' else 429),'application/json',b'{}'
                    data=({'status_code':0,'aweme_detail':item(vid)} if outcome=='public' else
                          {'status_code':0,'verify_type':'captcha'} if outcome=='verify' else
                          {'status_code':0,'aweme_detail':None,'filter_detail':{'aweme_id':VIDEOS[1] if outcome=='foreign' else vid,'filter_reason':'status_self_see'}})
                else:data={'status_code':0,'comments':None,'has_more':0} if vid==VIDEOS[0] else body([record(aweme_id=vid)])
                return 200,'application/json',json.dumps(data).encode()
            client=http.Client(session(),signer=FakeSigner(),transport=exchange)
            worker.collect(dict(kind='video',target='\n'.join(VIDEOS[:2]),page_concurrency=1,video_limit=2,comment_limit=1,
                discovery_job={'read_lane':'front'},resolve_video_titles=True,known_video_titles={v:'国服瓦陪玩' for v in VIDEOS[:2]}),
                events.append,threading.Event(),client=client)
            self.assertEqual(events[-1]['status'],expected,outcome)
            self.assertEqual(calls[:2],[('comments',VIDEOS[0]),('detail',VIDEOS[0])])
            retired=[e['video_id'] for e in events if e.get('type')=='checkpoint' and e.get('status')=='unavailable']
            self.assertEqual(retired,[VIDEOS[0]] if outcome=='private' else [])
            if outcome=='private':self.assertIn(('comments',VIDEOS[1]),calls)

    def run_metadata(self,lane,known=True,detail_title='国服无畏契约陪玩',gate=None,cached_title=None):
        calls=[];events=[];vid=VIDEOS[0]
        old=dict(updated_at='2020-01-01T00:00:00+00:00',comments=20)
        fresh=dict(updated_at='2026-09-15T09:17:00+00:00',comments=30)
        class Client:
            def page(self,operation,**kw):
                calls.append(operation)
                if gate:raise http.ReadError(gate)
                if operation=='detail':return {'rows':[dict(video_id=vid,video_title=detail_title,metrics=fresh)]}
                return http.parse_page(body([record(aweme_id=vid)]),operation,vid,title=kw['title'])
        worker.collect(dict(kind='video',target=vid,page_concurrency=1,video_limit=1,comment_limit=1,
            discovery_job={'read_lane':lane},resolve_video_titles=True,refresh_video_metrics=True,
            known_video_titles={vid:cached_title or '国服无畏契约陪玩'} if known else {},known_video_metrics={vid:old}),
            events.append,threading.Event(),client=Client())
        return calls,events,old,fresh

    def test_known_front_does_not_wait_for_optional_detail_or_forge_freshness(self):
        calls,events,old,_=self.run_metadata('front')
        self.assertEqual(calls,['comments'])
        self.assertEqual(events[-1]['status'],'completed')
        self.assertEqual(next(e['record']['metrics'] for e in events if e['type']=='video'),old)

    def test_history_and_combined_keep_refreshing_stale_statistics(self):
        for lane in ('history','combined'):
            calls,events,_,fresh=self.run_metadata(lane)
            self.assertEqual(calls,['detail','comments'])
            self.assertEqual(events[-1]['status'],'completed')
            self.assertEqual(next(e['record']['metrics'] for e in events if e['type']=='video'),fresh)

    def test_unknown_front_still_resolves_title_and_checks_scope(self):
        calls,events,_,_=self.run_metadata('front',known=False)
        self.assertEqual(calls,['detail','comments'])
        self.assertEqual(events[-1]['status'],'completed')
        calls,events,_,_=self.run_metadata('front',known=False,detail_title='无畏契约手游陪玩')
        self.assertEqual(calls,['detail'])
        self.assertTrue(any(e.get('reason')=='outside_pc_scope' for e in events))

    def test_cached_excluded_title_never_reads_comments(self):
        calls,events,_,_=self.run_metadata('front',cached_title='无畏契约手游陪玩')
        self.assertEqual(calls,[])
        self.assertTrue(any(e.get('reason')=='outside_pc_scope' for e in events))

    def test_comment_gates_and_history_detail_gates_are_preserved(self):
        for lane in ('front','history'):
            for gate in ('needs_verification','needs_login','access_denied','rate_limited'):
                calls,events,_,_=self.run_metadata(lane,gate=gate)
                self.assertEqual(calls,['comments' if lane=='front' else 'detail'])
                self.assertEqual(events[-1]['status'],gate)


class IndependentLaneTests(HeadPriorityTests):
    def test_front_finishes_without_entering_history(self):
        calls,events=self.run_batch(concurrency=2,replies=True,lane='front')
        self.assertEqual(len(calls),3)
        self.assertTrue(all(op=='comments' and c==0 for _,op,c in calls))
        self.assertEqual(events[-1]['status'],'completed')
        self.assertEqual(sum(e.get('type')=='checkpoint' and e.get('status')=='done' for e in events),3)
        markers=[e['snapshot']['processing'] for e in events if e.get('stage')=='work_read_queue']
        self.assertEqual(len(markers),1);self.assertEqual(markers[0]['lane'],'front')

    def test_history_without_valid_own_cursor_reads_front_instead(self):
        calls,events=self.run_batch(lane='history')
        self.assertEqual(events[-1]['status'],'completed')
        self.assertTrue(all(calls.index((v,'comments',0))<calls.index((v,'comments',10)) for v in VIDEOS))

    def test_front_platform_gate_still_stops_without_history(self):
        calls,events=self.run_batch(lane='front',gate='needs_verification',concurrency=1)
        self.assertEqual(events[-1]['status'],'needs_verification')
        self.assertTrue(all(c==0 for _,_,c in calls))

for _n in dir(HeadPriorityTests):
    if _n.startswith('test_') and _n not in IndependentLaneTests.__dict__:setattr(IndependentLaneTests,_n,None)


if __name__=='__main__':unittest.main()
