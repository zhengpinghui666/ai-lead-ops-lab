"""A private author seed is not an account-wide access failure."""
import tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
import clubops as app
import collector
import collector_http as http
import collector_http_worker as worker
import discovery_tracking as tracking
import video_discovery as discovery
from test_collector_http import VIDEO,PARENT
from test_video_discovery import item,AUTHOR

class RestrictedAuthorSeedTests(unittest.TestCase):
    def test_mixed_seeds_skip_only_restricted_detail_and_continue_public_author(self):
        calls=[]
        class Client:
            def page(self,op,**kw):
                calls.append((op,kw))
                if op=='detail' and kw['video']==VIDEO:
                    return discovery.parse_discovery({'status_code':0,'aweme_detail':None,'filter_detail':{'aweme_id':VIDEO,'filter_reason':'status_audit_self_see'}},op,video=VIDEO)
                data={'status_code':0,'aweme_detail':item(PARENT)} if op=='detail' else {'status_code':0,'aweme_list':[item(PARENT)],'has_more':0,'max_cursor':0}
                return discovery.parse_discovery(data,op,video=kw.get('video',''),sec_uid=kw.get('sec_uid',''))
        result=discovery.discover(Client(),[VIDEO,PARENT],include_related=False)
        self.assertEqual([x[0] for x in calls],['detail','detail','author'])
        self.assertEqual(result['status'],'completed');self.assertEqual(result['failures'],[])
        self.assertEqual([r['video_id'] for r in result['candidates']],[PARENT])
        self.assertEqual(result['restricted_seeds'],[dict(video_id=VIDEO,reason='status_audit_self_see',restriction_scope='work')])

    def test_unknown_or_account_denial_stops_and_never_retries(self):
        for status,evidence in [('access_denied',{}),('access_denied',{'reason':'status_audit_self_see','restriction_scope':'work','video_id':PARENT}),('rate_limited',{}),('needs_verification',{}),('needs_login',{})]:
            calls=[]
            class Client:
                def page(self,op,**kw):calls.append(op);raise http.ReadError(status,evidence)
            with self.subTest(status=status,evidence=evidence):
                result=discovery.discover(Client(),[VIDEO,PARENT],include_related=False)
                self.assertEqual(calls,['detail']);self.assertEqual(result['failures'][0]['status'],status)
                self.assertEqual(result['restricted_seeds'],[])

    def test_worker_retirement_is_explicit_and_does_not_read_restricted_comments(self):
        for reason in discovery.WORK_RESTRICTION_DETAILS:
            calls=[];events=[]
            class Client:
                def page(self,op,**kw):
                    calls.append(op)
                    if op!='detail':raise AssertionError('Restricted content must not be read')
                    raise http.ReadError('access_denied',{'reason':reason,'restriction_scope':'work','video_id':VIDEO})
            worker.collect(dict(kind='author',target=VIDEO,video_limit=1,comment_limit=30,page_concurrency=1),events.append,threading.Event(),client=Client())
            self.assertEqual(calls,['detail']);self.assertEqual(events[-1]['status'],'completed')
            self.assertIn('本次未读取评论',events[-1]['detail'])
            self.assertEqual(len([e for e in events if e['type']=='discovery_restriction']),1)
            self.assertFalse(any(e['type']=='comment' for e in events))

    def test_parent_validates_exact_task_seed_and_keeps_other_work_and_history(self):
        with tempfile.TemporaryDirectory() as td,patch.object(app,'DATA_DIR',Path(td)),patch('collector.threading.Thread.start'):
            app.init();collector.ACTIVE.clear()
            task=collector.start(dict(kind='author',target=VIDEO,transport='http',request_id='synthetic-retire-seed'))['id']
            with app.db() as c:
                tracking.record(c,[dict(video_id=vid,video_title='无畏契约陪玩',author_sec_uid=AUTHOR,author_nickname='合成作者') for vid in (VIDEO,PARENT)],source='author',target=VIDEO)
                before={t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('videos','comments','leads','discovery_authors')}
            valid=dict(type='discovery_restriction',video_id=VIDEO,reason='status_audit_self_see',restriction_scope='work')
            for delta in (dict(video_id=PARENT),dict(reason='unknown'),dict(restriction_scope='account')):
                with self.assertRaises(ValueError):collector.restricted_discovery_seed(task,{**valid,**delta})
            collector.restricted_discovery_seed(task,valid)
            with app.db() as c:
                self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?',(VIDEO,)).fetchone()[0],0)
                self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?',(PARENT,)).fetchone()[0],1)
                self.assertEqual(before,{t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in before})
                authors=tracking.author_rows(c,tracking.config(c));self.assertEqual(authors[0]['seed_video_id'],PARENT);self.assertEqual(authors[0]['enabled'],1)
            collector.update(task,status='completed',finished_at=app.now())
            with self.assertRaises(ValueError):collector.restricted_discovery_seed(task,valid)
            collector.ACTIVE.clear()

if __name__=='__main__':unittest.main()
