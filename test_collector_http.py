import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import clubops as app
import collector
import collector_http as http
import collector_http_session as sessions
import collector_http_worker as worker
import collection_scheduler
import monitoring

VIDEO = '7664994032866659594'
PARENT = '7683708327758415300'


def session():
    return {'format': 'clubops-collection-session-1', 'account': 'test-account', 'sender_uid': '123456789',
        'captured_at': time.time(), 'user_agent': 'Mozilla/5.0 Chrome/145.0.0.0',
        'context': {'platform': 'Win32', 'width': 1536, 'height': 864, 'cores': 8, 'memory': 8},
        'cookies': {key: [{'name': 'sessionid', 'value': 'TEST_ONLY_SECRET', 'domain': '.douyin.com', 'path': '/', 'expires': -1}]
                    for key in sessions.PATHS}}


def record(cid=PARENT, **kw):
    return {'cid': cid, 'aweme_id': VIDEO, 'text': '国服找陪练预算100', 'create_time': 1788990000,
            'user': {'uid': 358898446378682, 'nickname': '夹具'}, **kw}


def body(items=None, **kw):
    return {'status_code': 0, 'comments': items if items is not None else [record()], 'has_more': 0, 'cursor': 0, **kw}


class FakeSigner:
    provider = 'test-only'
    def sign(self, params):
        from urllib.parse import urlencode
        return urlencode(params) + '&a_bogus=TEST_ONLY_SIGNATURE'


class HTTPReadTests(unittest.TestCase):
    def test_explicit_video_pool_reads_two_concurrently_without_search(self):
        videos=[VIDEO, '7619229726170732785'];barrier=threading.Barrier(2)
        gate=threading.Lock();calls=[];active=peak=0
        def exchange(url, headers, cancelled):
            nonlocal active,peak
            query=parse_qs(urlsplit(url).query);vid=query['aweme_id'][0]
            with gate: calls.append(vid);active+=1;peak=max(peak,active)
            try:
                barrier.wait(timeout=3)
                data=body([record(cid=vid, aweme_id=vid)])
                return 200,'application/json',json.dumps(data).encode()
            finally:
                with gate:active-=1
        diagnostics=[]
        client=http.Client(session(), signer=FakeSigner(),transport=exchange,diagnostic=diagnostics.append)
        events=[]
        worker.collect({'kind':'video','target':'\n'.join(videos),'page_concurrency':2,'video_limit':2,'comment_limit':1},events.append,threading.Event(),client=client)
        self.assertEqual(peak,2)
        # The fixture can finish within one millisecond; the independent active
        # counter above proves overlap even when timestamp endpoints are equal.
        self.assertLessEqual(max(d['request_started_ms'] for d in diagnostics),min(d['response_received_ms'] for d in diagnostics))
        self.assertTrue(all(d['request_elapsed_ms']>=0 for d in diagnostics))
        self.assertCountEqual(calls,videos)
        self.assertEqual([e['status'] for e in events if e['type']=='status'][-1],'completed')
        self.assertCountEqual([e['record']['video_id'] for e in events if e['type']=='comment'],videos)

    def test_cancel_pool_preserves_unstarted_target_and_emits_no_late_rows(self):
        videos=[VIDEO,'7619229726170732785','7673063305880504538'];cancel=threading.Event()
        started=threading.Barrier(2);cancelled=threading.Event();calls=[];events=[]
        class Client:
            def page(self,operation,**kw):
                calls.append(kw['video']);started.wait(timeout=3)
                if kw['video']==videos[1]:
                    if not cancelled.wait(3):raise AssertionError('cancellation not delivered')
                return http.parse_page(body([record(cid=kw['video'],aweme_id=kw['video'])]),operation,kw['video'])
        def emit(event):
            events.append(event)
            if event['type']=='comment':cancel.set();cancelled.set()
        worker.collect({'kind':'video','target':'\n'.join(videos),'page_concurrency':2,'video_limit':3,'comment_limit':1},emit,cancel,client=Client())
        self.assertEqual([e['status'] for e in events if e['type']=='status'][-1],'cancelled')
        self.assertCountEqual(calls,videos[:2])
        self.assertEqual(len([e for e in events if e['type']=='comment']),1)
        self.assertEqual(len(next(e['records'] for e in events if e['type']=='targets')),3)
        self.assertFalse(any(e['type']=='checkpoint' and e['video_id']==videos[2] for e in events))

    def test_non_text_comments_are_skipped_but_bad_identity_is_not_accepted(self):
        result = http.parse_page(body([record(text='', sticker={})]), 'comments', VIDEO)
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['skipped_reasons'], {'non_text': 1, 'invalid_record': 0})
        with self.assertRaises(http.ReadError):
            http.parse_page(body([record(cid='invalid', text='')]), 'comments', VIDEO)
        mixed = http.parse_page(body([record(), record(cid='invalid', text='')]), 'comments', VIDEO)
        self.assertEqual(mixed['skipped_reasons'], {'non_text': 0, 'invalid_record': 1})

    def test_non_text_consumes_shared_comment_reply_budget(self):
        calls = []
        class Client:
            def page(self, operation, **kwargs):
                calls.append((operation, kwargs['count']))
                if operation == 'comments':
                    rows = [record(cid=str(int(PARENT)+i), reply_comment_total=int(i==0)) for i in range(9)]
                    rows.append(record(cid=str(int(PARENT)+9), text='', sticker={}))
                    return http.parse_page(body(rows, has_more=1, cursor=10), operation, VIDEO)
                rows = [record(cid=str(int(PARENT)+20+i), reply_id=PARENT) for i in range(2)]
                return http.parse_page(body(rows, has_more=1, cursor=2), operation, VIDEO, PARENT)
        events = []
        worker.collect({'page_concurrency':1, 'video_limit':1, 'comment_limit':12, 'kind':'video',
                        'target':'https://www.douyin.com/video/'+VIDEO}, events.append, threading.Event(), client=Client())
        self.assertEqual(calls, [('comments', 10), ('replies', 2)])
        self.assertEqual(sum(e['type']=='comment' for e in events), 11)
        self.assertEqual(sum(e['count'] for e in events if e['type']=='skipped'), 1)
        self.assertEqual(events[-1]['status'], 'completed')

    def test_invalid_search_records_still_make_batch_partial(self):
        class Client:
            def page(self, operation, **kwargs):
                if operation == 'search':
                    return http.parse_page({'status_code':0, 'data':[{'aweme_id':VIDEO}, {}], 'has_more':0, 'cursor':0}, operation)
                return http.parse_page(body(), operation, VIDEO)
        events = []
        worker.collect({'page_concurrency':1, 'video_limit':1, 'comment_limit':1, 'kind':'search',
                        'target':'合成测试'}, events.append, threading.Event(), client=Client())
        self.assertEqual(events[-1]['status'], 'partial')

    def test_failed_identity_is_persistent_and_blocks_all_requests(self):
        value = session()
        cfg = dict(kind='video', target=sessions.ORIGIN+'/video/'+VIDEO, video_limit=1,
                   page_concurrency=1, comment_limit=2)
        with tempfile.TemporaryDirectory() as td, patch.object(sessions.runtime, 'data_dir', return_value=Path(td)), \
                patch.object(sessions, 'load', return_value=value), patch.object(http, 'exchange') as exchange:
            events = []
            worker.collect(cfg, events.append, threading.Event(), session=value,
                           identity_probe=lambda _: {'status':'unrecognized_response','http_status':200})
            self.assertEqual(events[-1]['status'], 'identity_failed')
            self.assertFalse(sessions.status()['ready'])
            with patch('uid_bootstrap.probe') as probe:
                for kind in ('video', 'search'):
                    worker.collect({**cfg, 'kind':kind}, events.append, threading.Event(), session=value)
                probe.assert_not_called()
            exchange.assert_not_called()
            self.assertEqual(sessions.status()['status'], 'identity_failed')
            self.assertNotIn('TEST_ONLY_SECRET', Path(td,'private/collection-http/identity-status.json').read_text())
            replacement = {**value, 'captured_at':value['captured_at']+1}
            self.assertEqual(sessions.identity_state(replacement)['status'], 'prepared')
            self.assertEqual(sessions.identity_state(value)['status'], 'identity_failed')

    def test_large_ids_missing_uid_and_parent_attribution(self):
        value = record(cid=7683708327758415397, reply_id=PARENT, user={'sec_uid': 'not-a-numeric-uid'})
        result = http.parse_page(body([value]), 'replies', VIDEO, PARENT)
        self.assertEqual(result['rows'][0]['comment_id'], '7683708327758415397')
        self.assertEqual(result['rows'][0]['parent_comment_id'], PARENT)
        self.assertEqual(result['rows'][0]['user_id'], '')
        with self.assertRaises(http.ReadError):
            http.parse_page(body([record(aweme_id='7664994032866659999')]), 'comments', VIDEO)
        with self.assertRaises(http.ReadError):
            http.parse_page(body([record(reply_id='7683708327758415999')]), 'replies', VIDEO, PARENT)

    def test_explicit_empty_and_malformed_are_distinct(self):
        self.assertEqual(http.parse_page(body([], total=0), 'comments', VIDEO)['rows'], [])
        for value in ({}, body([], has_more=1), body([], cursor='3'), body(status_code=True), body(comments=None), body(status_code=8)):
            with self.assertRaises(http.ReadError):
                http.parse_page(value, 'comments', VIDEO)
        self.assertEqual(http.parse_page(body(comments=None, total=0), 'comments', VIDEO)['rows'], [])

    def test_verification_disguised_as_empty_search_is_not_success(self):
        value = {'status_code': 0, 'data': [], 'has_more': 0, 'cursor': 0, 'search_nil_info': {'search_nil_type': 'verify_check'}}
        with self.assertRaises(http.ReadError) as caught:
            http.parse_page(value, 'search')
        self.assertEqual(caught.exception.status, 'needs_verification')

    def test_reply_shape_diagnostics_preserve_strict_parser_without_private_fields(self):
        cases = [(body(comments=None,total='1'), 'invalid_page_container'),
                 (body([],has_more=1,cursor=10), 'empty_page_with_more'),
                 (body(has_more='0'), 'invalid_has_more'), (body(cursor='10'), 'invalid_cursor')]
        for payload, reason in cases:
            with self.subTest(reason=reason):
                log=[]
                payload.update(token='TEST_ONLY_BODY_SECRET',status_msg='TEST_ONLY_MESSAGE')
                client=http.Client(session(),signer=FakeSigner(),diagnostic=log.append,
                    transport=lambda *args:(200,'application/json',json.dumps(payload).encode()))
                with self.assertRaises(http.ReadError) as caught:
                    client.page('replies',video=VIDEO,parent=PARENT)
                self.assertEqual(caught.exception.status,'schema_changed')
                self.assertEqual(log[-1]['reason'],reason)
                self.assertEqual(log[-1]['parent_comment_id'],PARENT)
                self.assertEqual(log[-1]['response_shape']['status_code'],0)
                self.assertIs(log[-1]['response_shape']['verification_indicated'],False)
                encoded=json.dumps(log)
                for private in ('TEST_ONLY_BODY_SECRET','TEST_ONLY_MESSAGE','TEST_ONLY_SECRET','TEST_ONLY_SIGNATURE'):
                    self.assertNotIn(private,encoded)
        with self.assertRaises(http.ReadError) as caught:
            http.parse_page(body(comments=None,total=1,verify_type='captcha'),'replies',VIDEO,PARENT)
        self.assertEqual(caught.exception.status,'needs_verification')

    def test_terminal_null_replies_keep_statistical_total_and_main_comments_continue(self):
        empty=body(comments=None,total=1,cursor=10)
        result=http.parse_page(empty,'replies',VIDEO,PARENT)
        self.assertEqual(result['rows'],[]);self.assertFalse(result['has_more'])
        self.assertEqual(result['reply_visibility'],{'state':'terminal_without_visible_replies','declared_total':1,'returned_rows':0})
        # Only an explicit null reply container and valid terminal envelope qualify.
        rejected=[{k:v for k,v in empty.items() if k!='comments'}, {**empty,'total':-1},
                  {**empty,'total':True}, {**empty,'total':False}, {**empty,'total':None}, {**empty,'has_more':1},
                  {**empty,'cursor':'10'}, {**empty,'comments':{}}, {**empty,'status_code':8}]
        for payload in rejected:
            with self.subTest(payload=payload), self.assertRaises(http.ReadError):
                http.parse_page(payload,'replies',VIDEO,PARENT)
        with self.assertRaises(http.ReadError):http.parse_page(empty,'comments',VIDEO)
        calls=[]
        class Client:
            def page(self,operation,**kw):
                calls.append((operation,kw['cursor']))
                payload=(empty if operation=='replies' else
                         body([record(reply_comment_total=1)],has_more=1,cursor=10) if kw['cursor']==0 else
                         body([record(cid='7683708327758415397')],cursor=20))
                return http.parse_page(payload,operation,VIDEO,PARENT if operation=='replies' else '')
        events=[]
        worker.collect(dict(kind='video',target=VIDEO,page_concurrency=1,video_limit=1,comment_limit=3),
                       events.append,threading.Event(),client=Client())
        self.assertEqual(calls,[('comments',0),('replies',0),('comments',10)])
        self.assertEqual(events[-1]['status'],'completed')
        self.assertEqual(len([e for e in events if e['type']=='comment']),2)

    def test_no_retry_and_redacted_diagnostics(self):
        for status, raw, expected in ((429, b'limited', 'rate_limited'), (403, b'x', 'access_denied'),
                                     (200, b'', 'empty_response'), (200, b'bad', 'schema_changed')):
            calls, log = [], []
            def exchange(url, headers, cancelled):
                calls.append(url)
                self.assertEqual(urlsplit(url).hostname, 'www.douyin.com')
                return status, 'application/json', raw
            client = http.Client(session(), signer=FakeSigner(), transport=exchange, diagnostic=log.append)
            with self.assertRaises(http.ReadError) as caught:
                client.page('comments', video=VIDEO)
            self.assertEqual(caught.exception.status, expected)
            self.assertEqual(len(calls), 1)
            encoded = json.dumps(log)
            self.assertNotIn('TEST_ONLY_SECRET', encoded)
            self.assertNotIn('TEST_ONLY_SIGNATURE', encoded)
            self.assertNotIn('https://', encoded)

    def test_cancel_budget_and_argument_errors_make_no_request(self):
        calls = []
        client = http.Client(session(), signer=FakeSigner(), transport=lambda *a: calls.append(a), cancelled=lambda: True)
        with self.assertRaises(http.ReadError):
            client.page('comments', video=VIDEO)
        with self.assertRaises(ValueError):
            client.page('send', video=VIDEO)
        client.cancelled = lambda: False
        client.request_limit = 0
        with self.assertRaises(http.ReadError):
            client.page('comments', video=VIDEO)
        self.assertEqual(calls, [])

    def test_cancel_during_exchange_rejects_empty_and_valid_late_responses(self):
        for raw in (b'', json.dumps(body()).encode()):
            with self.subTest(empty=not raw):
                cancelled=threading.Event();diagnostics=[];calls=[]
                def transport(*args):
                    calls.append(True);cancelled.set()
                    return 200,'application/json',raw
                client=http.Client(session(),signer=FakeSigner(),transport=transport,
                    cancelled=cancelled.is_set,diagnostic=diagnostics.append)
                with self.assertRaises(http.ReadError) as caught:client.page('comments',video=VIDEO)
                self.assertEqual(caught.exception.status,'cancelled')
                self.assertEqual(diagnostics[-1]['status'],'cancelled')
                self.assertEqual(len(calls),1)

    def test_curl_callback_abort_is_cancel_or_limit_even_when_get_returns(self):
        for reason in ('cancelled','resource_limited'):
            with self.subTest(reason=reason):
                cancelled=threading.Event();calls=[]
                class FakeSession:
                    def __init__(self,**kwargs):assert kwargs=={'trust_env':False}
                    def __enter__(self):return self
                    def __exit__(self,*args):pass
                    def get(self,url,**kwargs):
                        calls.append(True)
                        if reason=='cancelled':cancelled.set()
                        block=b'x' if reason=='cancelled' else b'x'*(http.MAX_BODY+1)
                        assert kwargs['content_callback'](block)==0
                        return SimpleNamespace(status_code=200,headers={'content-type':'application/json'})
                module=SimpleNamespace(Session=FakeSession)
                with patch.dict('sys.modules',{'curl_cffi.requests':module}), self.assertRaises(http.ReadError) as caught:
                    http.exchange('https://www.douyin.com/',{},cancelled.is_set)
                self.assertEqual(caught.exception.status,reason)
                self.assertEqual(len(calls),1)

    def test_reply_contract_binds_video_parent_and_saved_cookie(self):
        calls = []
        def transport(url, headers, cancel):
            calls.append((url, headers))
            return 200, 'application/json', json.dumps(body([record(cid='7683708327758415397', reply_id=PARENT)])).encode()
        client = http.Client(session(), signer=FakeSigner(), transport=transport)
        client.page('replies', video=VIDEO, parent=PARENT, cursor=5)
        query = parse_qs(urlsplit(calls[0][0]).query)
        self.assertEqual(query['item_id'], [VIDEO])
        self.assertEqual(query['comment_id'], [PARENT])
        self.assertEqual(query['cursor'], ['5'])
        self.assertEqual(calls[0][1]['Cookie'], 'sessionid=TEST_ONLY_SECRET')

    def test_session_scope_expiry_and_no_browser_refresh(self):
        value = session()
        value['cookies']['comments'][0]['path'] = '/unrelated/'
        with self.assertRaises(ValueError):
            sessions.validate(value)
        value = session()
        value['cookies']['replies'][0]['domain'] = 'evil.example'
        with self.assertRaises(ValueError):
            sessions.validate(value)
        value = session()
        value['captured_at'] -= sessions.MAX_AGE + 60
        with self.assertRaises(ValueError):
            sessions.validate(value)

    def test_bootstrap_identity_mismatch_does_not_save(self):
        with tempfile.TemporaryDirectory() as folder, patch('collector_http_session.path', return_value=Path(folder)/'session.dpapi'):
            result = sessions.bootstrap(session(), probe=lambda x: {'status': 'account_mismatch'})
            self.assertFalse(result['credential_file_created'])
            self.assertFalse((Path(folder)/'session.dpapi').exists())

    def test_bootstrap_local_validation_reports_phase_without_claiming_http(self):
        value = session()
        value['context']['platform'] = 'SECRET_FIXTURE'
        with patch('uid_bootstrap.probe') as probe:
            result = sessions.bootstrap(value)
        probe.assert_not_called()
        self.assertEqual((result['status'],result['bootstrap_phase'],result['bootstrap_error']),
                         ('invalid_input','validation','invalid_client_context'))
        self.assertNotIn('identity_status', result)
        self.assertNotIn('SECRET_FIXTURE', json.dumps(result))

    def test_failed_preparation_preserves_previous_ciphertext_and_redacts_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder, patch('collector_http_session.path', return_value=Path(folder)/'session.dpapi'):
            target=Path(folder)/'session.dpapi'
            target.write_bytes(b'previous encrypted session')
            result=sessions.bootstrap(session(),probe=lambda x:dict(status='unrecognized_response',business_code=8,
                http_status=200,user_present=False,verification_indicated=False,cookie='TEST_ONLY_SECRET',raw_body='private'))
            self.assertEqual(result['identity_evidence']['business_code'],8)
            self.assertFalse(result['credential_file_created'])
            self.assertEqual(target.read_bytes(),b'previous encrypted session')
            self.assertNotIn('private',json.dumps(result))
            self.assertNotIn('TEST_ONLY_SECRET',json.dumps(result))

    def test_verification_gate_blocks_new_requests_until_session_changes(self):
        value = session()
        with tempfile.TemporaryDirectory() as folder, patch('collector_http_session.path', return_value=Path(folder)/'session.dpapi'):
            sessions.record_endpoint_status(value, 'search', 'needs_verification')
            self.assertEqual(sessions.endpoint_status(value, 'search'), 'needs_verification')
            log = []
            client = http.Client(value, signer=FakeSigner(), diagnostic=log.append)
            with patch('collector_http.exchange') as exchange, self.assertRaises(http.ReadError) as caught:
                client.page('search', keyword='无畏契约')
            self.assertEqual(caught.exception.status, 'needs_verification')
            exchange.assert_not_called()
            self.assertEqual(log[0]['request_number'], 0)
            self.assertEqual(log[0]['verification_scope'], 'account')
            value['captured_at'] += 1
            self.assertEqual(sessions.endpoint_status(value, 'search'), 'unknown')

    def test_only_explicit_search_verification_preserves_narrow_scope(self):
        with tempfile.TemporaryDirectory() as folder, patch('collector_http_session.path', return_value=Path(folder)/'session.dpapi'):
            for payload, expected in (({'verify_type': 1}, 'account'),
                                      ({'search_nil_info': {'search_nil_type': 'verify_check'}}, 'search'),
                                      ({'search_nil_info': {'search_nil_type': 'risk_check'}}, 'account')):
                with self.subTest(expected=expected, payload=payload):
                    value = session()
                    response = {'status_code': 0, 'data': [], 'cursor': 0, 'has_more': 0, **payload}
                    with patch('collector_http.exchange', return_value=(200, 'application/json', json.dumps(response).encode())) as exchange:
                        client = http.Client(value, signer=FakeSigner())
                        with self.assertRaises(http.ReadError):
                            client.page('search', keyword='合成测试')
                        self.assertEqual(exchange.call_count, 1)
                    gate = sessions.endpoint_state(value, 'search')
                    self.assertEqual(gate['verification_scope'], expected)
                    self.assertEqual(sessions.endpoint_status(value, 'comments'), 'unknown')
                    events = []
                    with patch('uid_bootstrap.probe') as identity, patch('collector_http.exchange') as exchange:
                        worker.collect({'kind':'search', 'target':'合成测试', 'video_limit':1,
                            'comment_limit':1, 'page_concurrency':1}, events.append, threading.Event(), session=value)
                        identity.assert_not_called()
                        exchange.assert_not_called()
                    self.assertEqual(events[-1]['status'], 'needs_verification')
                    evidence = next(e['snapshot']['responses'][0] for e in events if e['type']=='diagnostic')
                    self.assertEqual(evidence['request_number'], 0)
                    self.assertEqual(evidence['verification_scope'], expected)

    def test_worker_reads_replies_and_preserves_parent(self):
        calls = []
        class Client:
            def page(self, operation, **kwargs):
                calls.append((operation, kwargs))
                if operation == 'comments':
                    return http.parse_page(body([record(reply_comment_total=1)]), operation, VIDEO)
                return http.parse_page(body([record(cid='7683708327758415397', reply_id=PARENT)]), operation, VIDEO, PARENT)
        events = []
        worker.collect({'page_concurrency': 1, 'video_limit': 1, 'comment_limit': 3, 'kind': 'video',
                        'target': 'https://www.douyin.com/video/'+VIDEO}, events.append, threading.Event(), client=Client())
        self.assertEqual([r[0] for r in calls], ['comments', 'replies'])
        self.assertEqual(events[-1]['status'], 'completed')
        rows = [e['record'] for e in events if e['type']=='comment']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['parent_comment_id'], PARENT)

    def test_pagination_loop_stops_and_keeps_first_page(self):
        class Client:
            def page(self, operation, **kwargs):
                return http.parse_page(body(has_more=1, cursor=0), 'comments', VIDEO)
        events = []
        worker.collect({'page_concurrency':1, 'video_limit':1, 'comment_limit':20, 'kind':'video',
            'target':'https://www.douyin.com/video/'+VIDEO}, events.append, threading.Event(), client=Client())
        self.assertEqual(events[-1]['status'], 'schema_changed')
        self.assertEqual(len([e for e in events if e['type']=='comment']), 1)


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = app.DATA_DIR
        app.DATA_DIR = Path(self.temp.name)
        app.init()
        collector.ACTIVE.clear()
        self.threads = patch('collector.threading.Thread.start')
        self.threads.start()

    def tearDown(self):
        self.threads.stop()
        collector.ACTIVE.clear()
        app.DATA_DIR = self.old
        self.temp.cleanup()

    def test_video_pool_resume_keeps_only_pending_targets_and_filter_scope(self):
        videos=[VIDEO,'7619229726170732785']
        task=collector.start({'kind':'video','target':'\n'.join(videos),'transport':'http',
            'page_concurrency':2,'comment_limit':2,'request_id':'pool-initial'},lookback_hours=24,
            include_keywords='陪练',exclude_keywords='接单')['id']
        collector.checkpoint(task,{'type':'targets','records':collector.video_targets('\n'.join(videos))})
        collector.checkpoint(task,{'type':'checkpoint','video_id':videos[0],'status':'done'})
        collector.update(task,status='cancelled',finished_at=app.now());collector.ACTIVE.clear()
        resumed=collector.resume(task,'pool-resume')['id']
        with self.assertRaisesRegex(ValueError,'范围'):
            collector.checkpoint(resumed,{'type':'targets','records':[{'video_id':videos[0]}]})
        with app.db() as c:source_id=c.execute("SELECT id FROM sources WHERE kind='browser'").fetchone()[0]
        with self.assertRaisesRegex(ValueError,'不属于'):
            collector.observe(resumed,source_id,{'type':'video','record':{'video_id':videos[0]}})
        with app.db() as c:
            old=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            new=c.execute('SELECT * FROM collection_tasks WHERE id=?',(resumed,)).fetchone()
            self.assertEqual([r[0] for r in c.execute('SELECT video_id FROM collection_checkpoints WHERE task_id=?',(resumed,))],[videos[1]])
            for key in ('transport','kind','target','comment_limit','lookback_hours','comment_since','include_keywords','exclude_keywords'):
                self.assertEqual(new[key],old[key],key)
            self.assertEqual((new['video_limit'],new['page_concurrency']),(1,1))
        collector.ACTIVE.clear()

    def test_saved_pool_monitor_and_plan_remain_paused_without_dispatch(self):
        config={'kind':'video','target':VIDEO+'\n7619229726170732785','transport':'http','page_concurrency':4}
        monitor=monitoring.save(config)
        plan_id=collection_scheduler.save(config)['id']
        self.assertEqual((monitor['video_limit'],monitor['page_concurrency'],monitor['enabled']),(2,2,False))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_tasks').fetchone()[0],0)
            self.assertEqual(tuple(c.execute('SELECT video_limit,page_concurrency,status FROM collection_plans WHERE id=?',(plan_id,)).fetchone()),(2,2,'paused'))

    def test_transport_persists_idempotency_and_resume(self):
        config = {'kind':'video', 'target':VIDEO, 'request_id':'http-fixture', 'transport':'http'}
        result = collector.start(config)
        with self.assertRaises(ValueError):
            collector.start({**config, 'transport':'local_browser'})
        with app.db() as connection:
            self.assertEqual(connection.execute('SELECT transport FROM collection_tasks').fetchone()[0], 'http')
        collector.checkpoint(result['id'], {'type':'targets', 'records':[{'video_id':VIDEO}]})
        collector.update(result['id'], status='failed', finished_at=app.now())
        collector.ACTIVE.clear()
        resumed = collector.resume(result['id'], 'resume-http-fixture')
        with app.db() as connection:
            self.assertEqual(connection.execute('SELECT transport FROM collection_tasks WHERE id=?', (resumed['id'],)).fetchone()[0], 'http')

    def test_partial_real_rows_show_read_verified_without_healthy_monitor_baseline(self):
        task = collector.start({'kind':'video', 'target':VIDEO, 'transport':'http', 'request_id':'partial-fixture'})['id']
        collector.update(task, status='partial', comments=2, finished_at=app.now())
        collector.ACTIVE.clear()
        self.assertTrue(collector.state()['http']['live_verified'])
        monitoring.save({'transport':'http', 'kind':'video', 'target':VIDEO})
        with self.assertRaises(ValueError):
            monitoring.command('start')

    def test_monitor_does_not_use_browser_success_as_http_baseline(self):
        task = collector.start({'kind':'video', 'target':VIDEO, 'request_id':'browser-fixture'})['id']
        collector.update(task, status='completed', comments=1, finished_at=app.now())
        collector.ACTIVE.clear()
        monitor = monitoring.save({'transport':'http', 'kind':'video', 'target':VIDEO})
        self.assertEqual(monitor['transport'], 'http')
        with self.assertRaises(ValueError):
            monitoring.command('start')

    def test_state_does_not_read_another_workspace_session(self):
        with patch('collector_http_session.runtime.data_dir', side_effect=AssertionError('must use app data directory')):
            state = collector.state()['http']['session']
        self.assertEqual(state['status'], 'needs_login')
        self.assertFalse(state['ready'])

    def test_search_challenge_keeps_enabled_http_video_plan_independent(self):
        baseline = collector.start({'kind':'video', 'target':VIDEO, 'transport':'http', 'request_id':'baseline'})['id']
        collector.update(baseline, status='completed', comments=1, finished_at=app.now())
        collector.ACTIVE.clear()
        search = collection_scheduler.save({'kind':'search', 'target':'合成测试', 'transport':'http', 'priority':3})['id']
        video = collection_scheduler.save({'kind':'video', 'target':VIDEO, 'transport':'http', 'run_limit':1})['id']
        collection_scheduler.command(search, 'start')
        collection_scheduler.command(video, 'start')
        task = collection_scheduler.tick()
        diagnostic = {'responses':[{'operation':'search', 'transport':'http', 'status':'needs_verification',
                                   'reason':'search_verification_required', 'search_nil_type':'verify_check'}]}
        with app.db() as connection:
            connection.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                               (task, 'http_read', json.dumps(diagnostic), app.now()))
        collector.update(task, status='needs_verification', finished_at=app.now())
        collector.ACTIVE.clear()
        next_task = collection_scheduler.tick()
        plans = {p['id']:p for p in collection_scheduler.state()}
        self.assertEqual(plans[search]['status'], 'attention')
        self.assertEqual(plans[search]['run_count'], 1)
        self.assertEqual(plans[video]['status'], 'running')
        self.assertEqual(plans[video]['last_task_id'], next_task)
        self.assertIsNotNone(next_task)
        with app.db() as connection:
            row = connection.execute('SELECT kind,transport FROM collection_tasks WHERE id=?', (next_task,)).fetchone()
            self.assertEqual(tuple(row), ('video', 'http'))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0], 0)
        collector.update(next_task, status='completed', comments=1, finished_at=app.now())
        collector.ACTIVE.clear()
        self.assertIsNone(collection_scheduler.tick())
        with patch('collector_http_session.status', return_value={'endpoints':{'search':'needs_verification'}}) as status:
            with self.assertRaisesRegex(ValueError, '搜索会话仍待人工验证'):
                collection_scheduler.command(search, 'start')
            status.assert_called_once_with(app.DATA_DIR)

    def test_unknown_challenge_and_account_failures_pause_all_plans(self):
        baseline = collector.start({'kind':'video', 'target':VIDEO, 'transport':'http', 'request_id':'baseline'})['id']
        collector.update(baseline, status='completed', comments=1, finished_at=app.now())
        collector.ACTIVE.clear()
        search = collection_scheduler.save({'kind':'search', 'target':'合成测试', 'transport':'http'})['id']
        video = collection_scheduler.save({'kind':'video', 'target':VIDEO, 'transport':'http'})['id']
        for failure in ('needs_verification', 'needs_login', 'access_denied', 'rate_limited', 'identity_failed', 'session_expired'):
            with self.subTest(failure=failure):
                with app.db() as connection:
                    connection.execute("UPDATE collection_plans SET status='running',next_run_at=?", (app.now(),))
                task = collector.start({'kind':'search', 'target':'合成测试', 'transport':'http', 'request_id':failure})['id']
                diagnostic = {'responses':[{'operation':'search', 'transport':'http', 'status':failure, 'reason':'session_gate'}]}
                with app.db() as connection:
                    connection.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                                       (task, 'http_read', json.dumps(diagnostic), app.now()))
                collector.update(task, status=failure, finished_at=app.now())
                collector.ACTIVE.clear()
                self.assertIsNone(collection_scheduler.tick())
                self.assertTrue(all(p['status']=='attention' for p in collection_scheduler.state()))
                self.assertFalse(collector.ACTIVE)

    def test_persisted_search_scope_and_malformed_diagnostics(self):
        task = {'id':123, 'kind':'search', 'transport':'http', 'status':'needs_verification'}
        # Mock only the SQL read; these payloads must never turn broad failures into narrow ones.
        for responses, expected in (([{'operation':'search', 'transport':'http', 'status':'needs_verification',
                                      'reason':'session_gate', 'verification_scope':'search'}], True),
                                    ([{'operation':'identity', 'transport':'http', 'status':'identity_failed'}], False),
                                    ('malformed', False), ([], False)):
            from unittest.mock import Mock
            connection = Mock()
            connection.execute.return_value = [{'snapshot':json.dumps({'responses':responses})}]
            self.assertEqual(collection_scheduler.search_verification_only(connection, task), expected)

    def test_http_source_reuses_comment_identity_without_duplicate_leads(self):
        task = collector.start({'kind':'video', 'target':VIDEO, 'transport':'http', 'request_id':'dedupe-fixture'})['id']
        source = collector.ACTIVE[task]['source_id']
        collector.observe(task, source, {'type':'video', 'record':{'video_id':VIDEO}})
        row = http.parse_page(body(), 'comments', VIDEO)['rows'][0]
        collector.observe(task, source, {'type':'comment', 'record':row})
        collector.observe(task, source, {'type':'comment', 'record':row})
        with app.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM comments').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM collection_observations').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
