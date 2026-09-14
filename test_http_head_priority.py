"""Scheduler regression: inspect every work before draining old pages/replies."""
import threading,unittest,time
from collections import Counter
import collector_http as http,collector_http_worker as worker
from test_collector_http import record,body

VIDEOS=['7664994032866659594','7664994032866659595','7664994032866659596']

class HeadPriorityTests(unittest.TestCase):
    def run_batch(self,*,concurrency=2,cancel_at=None,gate=None,budget=None,replies=False):
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
            video_limit=3,comment_limit=20),emit,cancel,client=Client())
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

if __name__=='__main__':unittest.main()
