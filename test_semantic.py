"""Synthetic model responses and isolated DBs only; no real model calls."""
import copy
from contextlib import closing
import http.server
import http.client
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import clubops as app
import analysis_store as store
import semantic as model
import intent_eval
import server


def prediction(source, category='noise', certainty='clear'):
    return dict(category=category, certainty=certainty, reason='合成模型输出，用于测试结果分离', game='',
                facts={k: '' for k in model.FIELDS}, evidence=[dict(field='category', source='text', text=source['text'][:100])])


def ollama_prediction(source):
    return dict(category='noise', certainty='clear', reason='合成模型协议检查',
                classification_quote=source['text'][:100], game=dict(value='', quote='', source='text'),
                facts={k: dict(value='', quote='') for k in model.FIELDS})


class SyntheticAdapter:
    def __init__(self, settings):
        pass

    def predict(self, source):
        return prediction(source), 'a'*64


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(kind='comment', text='无畏契约找陪练，预算最多200元，亚服', parent='预算100元，国服', title='三角洲陪练')

    def test_low_certainty_is_not_a_buyer(self):
        result = model.validate_result(prediction(self.source, 'buyer', 'uncertain'), self.source)
        self.assertEqual((result['category'], result['proposed_category']), ('uncertain', 'buyer'))
        self.assertIsNone(result['confidence'])

    def test_local_game_grammar_uses_literal_source_spans(self):
        source=dict(kind='comment',text='瓦片掉了？',parent='',title='三角洲行动端游')
        props=model.ollama_schema(source)['properties']
        choices=[{k:v['const'] for k,v in item['properties'].items()} for item in props['game']['oneOf']]
        self.assertFalse(any(x['value']=='无畏契约' for x in choices))
        self.assertTrue(any(x['value']=='三角洲行动' and x['source']=='title' for x in choices))
        self.assertEqual(props['classification_quote']['enum'],[source['text']])
        for item in choices:
            if item['value']:self.assertIn(item['quote'],source[item['source']])

    def test_local_optional_fact_omission_preserves_supported_buyer(self):
        source=dict(kind='comment',text='能给我安排一位瓦女陪吗，怎么付款',parent='',title='国服无畏契约陪玩')
        raw=ollama_prediction(source);raw['category']='buyer'
        raw['game']=dict(value='无畏契约',quote='无畏契约',source='title')
        raw['facts']['region']=dict(value='国服',quote='国服')
        adapter=model.OllamaAdapter(dict(model.DEFAULTS,model='synthetic'))
        replies=[{'cloud':{'disabled':True}},{'model_info':{'synthetic':True}},
                 {'done':True,'done_reason':'stop','message':{'content':json.dumps(raw)}}]
        with patch.object(adapter,'request',side_effect=replies):
            value,_=adapter.predict(source)
        checked=model.validate_result(value,source)
        self.assertEqual(checked['category'],'buyer')
        self.assertEqual(checked['facts']['region'],'')
        self.assertEqual(adapter.omitted_fields,['region'])

    def test_local_fact_omission_does_not_accept_fabricated_category_evidence(self):
        source=dict(kind='comment',text='有没有一起玩的啊',parent='',title='无畏契约陪玩')
        raw=ollama_prediction(source);raw.update(category='buyer',classification_quote='想点女陪')
        adapter=model.OllamaAdapter(dict(model.DEFAULTS,model='synthetic'))
        replies=[{'cloud':{'disabled':True}},{'model_info':{'synthetic':True}},
                 {'done':True,'done_reason':'stop','message':{'content':json.dumps(raw)}}]
        with patch.object(adapter,'request',side_effect=replies):value,_=adapter.predict(source)
        with self.assertRaises(ValueError):model.validate_result(value,source)

    def test_field_attached_evidence_reaches_shared_literal_and_ownership_checks(self):
        value = ollama_prediction(self.source)
        value['game'] = dict(value='无畏契约', quote='无畏契约', source='text')
        value['facts']['budget'] = dict(value='预算最多200元', quote='预算最多200元')
        result = model.validate_result(model.normalize_ollama_result(value), self.source)
        self.assertEqual(result['facts']['budget'], '预算最多200元')
        self.assertEqual({e['kind'] for e in result['facts']['evidence']}, {'category', 'game', 'budget'})
        for changed in (dict(value='200', quote='预算最多200元'),
                        dict(value='预算100元', quote='预算100元')):
            value['facts']['budget'] = changed
            with self.assertRaises(ValueError):
                model.validate_result(model.normalize_ollama_result(value), self.source)

    def test_field_attached_output_cannot_omit_quote_or_add_a_parent_origin(self):
        cases = [dict(value='预算200元', quote=''),
                 dict(value='预算100元', quote='预算100元', source='parent'),
                 dict(value=200, quote='200'), dict(value='', quote=None)]
        for bad in cases:
            value = ollama_prediction(self.source)
            value['facts']['budget'] = bad
            with self.assertRaises(ValueError):
                model.normalize_ollama_result(value)
        value = ollama_prediction(self.source)
        value['facts']['budget'] = dict(value='', quote='预算最多200元')
        checked = model.validate_result(model.normalize_ollama_result(value), self.source)
        self.assertEqual(checked['facts']['budget'], '')
        self.assertNotIn('budget', [e['kind'] for e in checked['facts']['evidence']])
        value['classification_quote'] = ''
        with self.assertRaises(ValueError):
            model.validate_result(model.normalize_ollama_result(value), self.source)

    def test_fields_require_literal_owned_evidence(self):
        value = prediction(self.source)
        value['facts']['budget'] = '预算最多200元'
        value['evidence'].append(dict(field='budget', source='text', text='预算最多200元'))
        result = model.validate_result(value, self.source)
        self.assertEqual(result['facts']['budget'], '预算最多200元')
        for e in result['facts']['evidence']:
            self.assertEqual(self.source['text'][e['start']:e['end']], e['text'])
        for field, origin, quote in [('budget','parent','预算100元'), ('region','parent','国服'),
                                      ('budget','text','预算500元'), ('budget','text','预算最多200元')]:
            bad = prediction(self.source)
            bad['facts'][field] = quote if quote != '预算最多200元' else '200'
            bad['evidence'].append(dict(field=field, source=origin, text=quote))
            with self.assertRaises(ValueError):
                model.validate_result(bad, self.source)

    def test_schema_rejects_tools_unknown_fields_and_unproven_game(self):
        cases = []
        v = prediction(self.source);v['tools'] = ['send'];cases.append(v)
        v = prediction(self.source);v['category'] = 'won';cases.append(v)
        v = prediction(self.source);v['game'] = '无畏契约';cases.append(v)
        v = prediction(self.source);v['facts']['service_type'] = '新手陪练';cases.append(v)
        v = prediction(self.source);v['evidence'] = [];cases.append(v)
        for value in cases:
            with self.assertRaises(ValueError):
                model.validate_result(value, self.source)

    def test_config_rejects_remote_address_cloud_and_wrong_types(self):
        for changes in ({'host':'example.com'}, {'host':'127.0.0.1/extra'}, {'enabled':'true'},
                        {'port':True}, {'model':'local-cloud'}, {'timeout_seconds':61}, {'api_key':'x'}):
            with self.assertRaises(ValueError):
                model.validate_config(changes)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='semantic-test-')
        self.addCleanup(self.temp.cleanup)
        self.old_dir, app.DATA_DIR = app.DATA_DIR, Path(self.temp.name)
        self.addCleanup(setattr, app, 'DATA_DIR', self.old_dir)
        app.init()
        app.ingest({'records': [dict(comment_id='comment1', video_id='v1',video_title='无畏契约陪练服务', user_id='20001',
                                    text='无畏契约找陪练，预算200元')]})
        app.analyze()
        self.id = app.state()['comments'][0]['id']
        self.settings = dict(model.DEFAULTS, enabled=True, model='synthetic:1')
        model.save(self.settings)

    def body(self, key='request-0001'):
        row = app.state()['comments'][0]
        return dict(evidence_type='comment', id=self.id, input_hash=row['analysis_input_hash'], request_id=key)

    def test_save_and_state_are_inert(self):
        with patch('http.client.HTTPConnection') as connection:
            model.save(self.settings)
            self.assertEqual(model.state()['mode'], 'local_model_configured')
            self.assertFalse(model.state()['accuracy_verified'])
            app.state()
            connection.assert_not_called()
        with app.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM intent_results WHERE method='model'").fetchone()[0],0)

    def test_model_rule_and_human_are_independent_with_no_contact_permission(self):
        request = self.body()
        result = model.analyze_one(request, adapter_factory=SyntheticAdapter)
        self.assertEqual(result['status'], 'completed')
        row = app.state()['comments'][0]
        self.assertEqual((row['category'], row['analysis_method'], row['rule_category']), ('noise','model','buyer'))
        self.assertEqual(row['rule_facts']['budget'], '200')
        self.assertEqual(app.state()['videos'][0]['demand_count'], 0)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT category FROM comments').fetchone()[0], 'buyer')
        app.mutate('review', dict(id=self.id, category='social', reason='合成人工判断', review_token=row['review_token']))
        model.analyze_one(self.body('request-0002'), adapter_factory=SyntheticAdapter)
        row = app.state()['comments'][0]
        self.assertEqual((row['category'], row['analysis_method'], row['rule_category']), ('social','human','buyer'))
        self.assertEqual(row['model_result']['result']['category'], 'noise')
        state = app.state()
        self.assertEqual((state['messages'],state['jobs'],state['leads'][0]['contact_basis']), ([],[],''))
        self.assertEqual(state['stats']['category_counts'], {'social':1})

    def test_repeat_request_does_not_call_model_twice(self):
        request = self.body()
        first = model.analyze_one(request, adapter_factory=SyntheticAdapter)
        with patch.object(SyntheticAdapter, 'predict') as predict:
            second = model.analyze_one(request, adapter_factory=SyntheticAdapter)
            predict.assert_not_called()
        self.assertEqual(first['id'], second['id'])

    def test_prompt_upgrade_keeps_old_result_as_history_without_using_or_rerunning_it(self):
        model.analyze_one(self.body(), adapter_factory=SyntheticAdapter)
        prior = app.state()['comments'][0]
        history_before = store.history('comment', self.id)
        self.assertEqual(prior['analysis_method'], 'model')
        with patch.object(model, 'PROMPT_VERSION', 'synthetic-next-prompt'), patch.object(SyntheticAdapter, 'predict') as predict:
            current = app.state()['comments'][0]
            self.assertEqual((current['analysis_method'], current['category']), ('rules', 'buyer'))
            self.assertEqual(current['model_result'], prior['model_result'])
            self.assertEqual(store.history('comment', self.id), history_before)
            predict.assert_not_called()

    def test_alias_extension_keeps_identical_input_v6_result_without_rewriting(self):
        with patch.object(model,'PROMPT_VERSION','intent-prompt-v6'):
            model.analyze_one(self.body(),adapter_factory=SyntheticAdapter)
            previous=app.state()['comments'][0]
        history_before=store.history('comment',self.id)
        with patch.object(model,'PROMPT_VERSION','intent-prompt-v7'),patch.object(SyntheticAdapter,'predict') as predict:
            current=app.state()['comments'][0]
            self.assertEqual((current['analysis_method'],current['category']),('model',previous['category']))
            self.assertEqual(current['model_result'],previous['model_result'])
            self.assertEqual(store.history('comment',self.id),history_before)
            predict.assert_not_called()
        self.assertFalse(store.compatible_engine('provider:a:intent-prompt-v6:format','provider:b:intent-prompt-v7:format'))

    def test_failure_reverts_to_rule_instead_of_stale_success(self):
        model.analyze_one(self.body(), adapter_factory=SyntheticAdapter)
        with patch.object(SyntheticAdapter,'predict',side_effect=TimeoutError('private response')):
            result = model.analyze_one(self.body('request-0002'), adapter_factory=SyntheticAdapter)
        row = app.state()['comments'][0]
        self.assertEqual((row['category'],row['analysis_method']), ('buyer','rules'))
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('private response', json.dumps(store.history('comment',self.id)))

    def test_potential_demand_extension_preserves_prior_buyer_and_evidence(self):
        with patch.object(model, 'PROMPT_VERSION', 'intent-prompt-v7'):
            with patch.object(SyntheticAdapter, 'predict', side_effect=lambda s:(prediction(s, 'buyer'), 'a'*64)):
                model.analyze_one(self.body(), adapter_factory=SyntheticAdapter)
            previous = app.state()['comments'][0]
        history = store.history('comment', self.id)
        with patch.object(model, 'PROMPT_VERSION', 'intent-prompt-v8'), patch.object(SyntheticAdapter, 'predict') as predict:
            current = app.state()['comments'][0]
            self.assertEqual((current['category'], current['analysis_method']), ('buyer', 'model'))
            self.assertEqual(current['model_result'], previous['model_result'])
            self.assertEqual(store.history('comment', self.id), history)
            predict.assert_not_called()
        self.assertFalse(store.compatible_engine('x:intent-prompt-v7:f', 'y:intent-prompt-v8:f'))
        self.assertFalse(store.compatible_engine('x:intent-prompt-v8:f', 'x:intent-prompt-v7:f'))

    def test_source_change_while_running_makes_result_historical(self):
        def changing(source):
            app.ingest({'records':[dict(comment_id='comment1',video_id='v1',user_id='20001',text='无畏契约免费组队')]})
            return prediction(source), 'a'*64
        with patch.object(SyntheticAdapter, 'predict', side_effect=changing):
            result = model.analyze_one(self.body(), adapter_factory=SyntheticAdapter)
        self.assertEqual(result['status'],'stale')
        app.analyze()
        row = app.state()['comments'][0]
        self.assertEqual((row['category'],row['model_result']),('social',None))
        self.assertEqual(store.history('comment',self.id)['rows'][1]['status'],'stale')

    def test_human_correction_during_model_run_survives(self):
        def correcting(source):
            app.mutate('review',dict(id=self.id,category='uncertain',reason='同时发生的合成人工判断'))
            return prediction(source), 'a'*64
        with patch.object(SyntheticAdapter, 'predict', side_effect=correcting):
            model.analyze_one(self.body(), adapter_factory=SyntheticAdapter)
        row = app.state()['comments'][0]
        self.assertEqual((row['category'],row['analysis_method']),('uncertain','human'))

    def test_changed_config_and_disable_revert_to_rules(self):
        def changing(source):
            model.save(dict(self.settings, enabled=False))
            return prediction(source), 'a'*64
        with patch.object(SyntheticAdapter,'predict',side_effect=changing):
            self.assertEqual(model.analyze_one(self.body(),adapter_factory=SyntheticAdapter)['status'],'stale')
        self.assertEqual(app.state()['comments'][0]['analysis_method'],'rules')
        with self.assertRaises(ValueError):
            model.analyze_one(self.body(),adapter_factory=SyntheticAdapter)

    def test_concurrency_version_and_demo_checks_before_dispatch(self):
        with patch.object(SyntheticAdapter,'predict') as predict:
            with model.GUARD, self.assertRaises(ValueError):
                model.analyze_one(self.body(),adapter_factory=SyntheticAdapter)
            for body, mode in [({**self.body(),'input_hash':'old'},'live'),(self.body(),'demo'),({**self.body(),'id':True},'live')]:
                with self.assertRaises(ValueError):
                    model.analyze_one(body,mode,adapter_factory=SyntheticAdapter)
            predict.assert_not_called()

    def test_restart_marks_interrupted_without_dispatch(self):
        model.analyze_one(self.body(),adapter_factory=SyntheticAdapter)
        with app.db() as c:
            c.execute("UPDATE intent_results SET status='running' WHERE method='model'")
        with patch('http.client.HTTPConnection') as connection:
            store.recover()
            connection.assert_not_called()
        self.assertEqual(app.state()['comments'][0]['analysis_method'],'rules')

    def test_migration_backups_and_legacy_human_history_are_not_invented(self):
        app.mutate('review',dict(id=self.id,category='social',reason='已有人工判断'))
        with app.db() as c:
            c.execute('DROP TABLE intent_results')
        app.init()
        backup = next((app.DATA_DIR/'backups').glob('*before-intent-results-*'))
        with closing(sqlite3.connect(backup)) as c:
            self.assertEqual(c.execute('SELECT category FROM comments').fetchone()[0],'social')
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='intent_results'").fetchone())
        row=app.state()['comments'][0]
        self.assertEqual(row['analysis_method'],'human')
        self.assertIsNone(row['rule_category'])

    def test_live_model_and_manual_results_share_same_priority(self):
        import live_monitor as live
        import live_workflow as flow
        room='7683000000000000001'
        with app.db() as c:
            config=dict(live.DEFAULTS,room_url='https://live.douyin.com/12345')
            import live_room_pool
            live_room_pool.ingest(c,[dict(room_url=config['room_url'],title='无畏契约陪练')],'valorant_category',app.now())
            sid=c.execute('INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                ('synthetic-live',config['room_url'],room,json.dumps(config),'running','',app.now(),app.now())).lastrowid
        live.receive(sid,dict(type='message',record=dict(room_id=room,message_id='7683000000000000002',outer_message_id='7683000000000000003',uid='20002',nickname='合成用户',text='无畏契约找陪练',published_at=None)))
        row=live.state()['rows'][0]
        body=dict(evidence_type='live',id=row['id'],input_hash=row['analysis_input_hash'],request_id='live-request-0001')
        self.assertEqual(model.analyze_one(body,adapter_factory=SyntheticAdapter)['status'],'completed')
        row=flow.detail(row['id'])
        self.assertEqual((row['analysis_method'],row['category'],row['rule_category']),('model','noise','buyer'))
        flow.review(dict(id=row['id'],review_token=row['review_token'],category='uncertain',reason='合成人工核对'))
        row=flow.detail(row['id'])
        self.assertEqual((row['analysis_method'],row['category'],row['model_result']['result']['category']),('human','uncertain','noise'))

    def test_api_keeps_csrf_and_requires_saved_input_identity(self):
        httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        def post(path,body,token=server.CSRF):
            c=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
            try:
                c.request('POST',path,json.dumps(body),{'Content-Type':'application/json','X-ClubOps-Token':token})
                r=c.getresponse();return r.status,json.loads(r.read())
            finally:c.close()
        try:
            with patch.object(server,'PORT',httpd.server_port),patch('semantic.OllamaAdapter',side_effect=SyntheticAdapter) as adapter:
                self.assertEqual(post('/api/semantic-analyze',self.body(),token='bad')[0],403)
                self.assertEqual(post('/api/semantic-analyze',{**self.body(),'text':'replacement'})[0],400)
                adapter.assert_not_called()
                status,result=post('/api/semantic-analyze',self.body())
                self.assertEqual((status,result['result']['status']),(200,'completed'))
                adapter.assert_called_once()
                self.assertEqual(post('/api/semantic-save',dict(self.settings,enabled=False))[0],200)
                self.assertEqual(post('/api/semantic-queue-cancel',{},token='bad')[0],403)
                self.assertEqual(post('/api/semantic-queue-cancel',{'id':1})[0],400)
                self.assertEqual(post('/api/semantic-queue-cancel?mode=demo',{})[0],400)
                self.assertEqual(post('/api/semantic-queue-cancel',{})[0],200)
                self.assertEqual(app.state()['comments'][0]['analysis_method'],'rules')
        finally:
            httpd.shutdown();httpd.server_close();thread.join(2)


class HttpAdapterTests(unittest.TestCase):
    def run_http(self, *, cloud=False, remote=False):
        calls=[]
        source=dict(kind='comment',text='合成无需求文本',parent='',title='')
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self): self.respond()
            def do_POST(self): self.respond()
            def respond(self):
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
                calls.append((self.path,body))
                value={'cloud':{'disabled':not cloud}} if self.path=='/api/status' else {'model_info':{'general.architecture':'synthetic'},'remote_host':'https://example.com' if remote else ''} if self.path=='/api/show' else {'done':True,'done_reason':'stop','message':{'content':json.dumps(ollama_prediction(source))}}
                raw=json.dumps(value).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        try:
            adapter=model.OllamaAdapter(dict(model.DEFAULTS,model='synthetic',port=httpd.server_port))
            if cloud or remote:
                with self.assertRaises(model.ModelError): adapter.predict(source)
                self.assertNotIn('/api/chat',[p for p,_ in calls])
            else:
                raw,fingerprint=adapter.predict(source)
                self.assertEqual(model.validate_result(raw,source)['category'],'noise')
                self.assertEqual(len(fingerprint),64)
                self.assertEqual([p for p,_ in calls],['/api/status','/api/show','/api/chat'])
                body=calls[-1][1]
                self.assertFalse(body['stream']);self.assertNotIn('tools',body)
                self.assertEqual(json.loads(body['messages'][1]['content']),source)
        finally:
            httpd.shutdown();httpd.server_close();thread.join(2)

    def test_real_local_http_contract_with_synthetic_output(self): self.run_http()
    def test_cloud_runtime_does_not_receive_text(self): self.run_http(cloud=True)
    def test_remote_model_alias_does_not_receive_text(self): self.run_http(remote=True)


class EvaluationTests(unittest.TestCase):
    def dataset(self):
        return {'name':'synthetic test','rows':[dict(id='a',kind='comment',text='合成原文',parent='',title='',label='buyer',rationale='合成标注',origin='synthetic',labeler='assistant',review_status='confirmed',source_ref='synthetic://a')]}

    def test_false_positive_false_negative_and_missing_denominators(self):
        data=self.dataset(); row=data['rows'][0]
        p=[dict(id='a',input_hash=store.digest(intent_eval.source(row)),category='seller',method='model',engine='synthetic')]
        r=intent_eval.evaluate(data,p)
        self.assertEqual((r['per_class']['buyer']['fn'],r['per_class']['seller']['fp']),(1,1))
        self.assertEqual(r['per_class']['buyer']['false_negatives'],['a'])
        self.assertIsNone(r['per_class']['noise']['precision'])
        self.assertFalse(r['production_accuracy_verified'])

    def test_duplicate_and_conflicting_labels_do_not_inflate_samples(self):
        data=self.dataset(); row=data['rows'][0]
        data['rows'].append({**row,'id':'b','text':'  合成原文  '})
        self.assertEqual(intent_eval.evaluate(data)['evaluated'],1)
        data['rows'][1]['label']='noise'
        report=intent_eval.evaluate(data)
        self.assertEqual(report['evaluated'],0)
        self.assertEqual(report['skipped']['conflicting_labels'],2)
        self.assertIsNone(report['sample_accuracy'])

    def test_context_distinguishes_samples_and_version_mismatch_fails(self):
        data=self.dataset(); row=data['rows'][0]
        data['rows'].append({**row,'id':'b','parent':'另一位作者说的上下文'})
        self.assertEqual(intent_eval.evaluate(data)['eligible_unique'],2)
        with self.assertRaises(ValueError):
            intent_eval.evaluate(data,[dict(id='a',input_hash='wrong',category='buyer',method='rules',engine='synthetic')])

    def test_mixed_methods_and_unconfirmed_labels_are_not_model_accuracy(self):
        data=self.dataset();row=data['rows'][0]
        data['rows'].append({**row,'id':'b','text':'第二条'})
        p=[dict(id='a',category='buyer',method='model',engine='x'),dict(id='b',category='buyer',method='rules',engine='fallback')]
        with self.assertRaises(ValueError): intent_eval.evaluate(data,p)
        data['rows'][1]['review_status']='draft'
        self.assertEqual(intent_eval.evaluate(data)['skipped']['unconfirmed'],1)

    def test_live_uses_live_rules_and_records_actual_source_versions(self):
        data=self.dataset(); row=data['rows'][0]
        row.update(kind='live',text='@找陪玩 加油',label='noise')
        # A mention is not this speaker's intent. Never enter comment routing.
        with patch.object(app,'classify',side_effect=AssertionError('comment route')):
            report=intent_eval.evaluate(data)
        self.assertEqual(report['predictions'][0]['category'],'noise')
        self.assertEqual(report['engine'],'live-rules-v1')
        self.assertEqual(report['predictions'][0]['engine'],'live-rules-v1')
        self.assertEqual(report['input_kinds'],['live'])
        expected=hashlib.sha256((Path(__file__).parent/'live_rules.py').read_bytes()).hexdigest()
        self.assertEqual(report['classifier_source_sha256'],expected)
        self.assertEqual(set(report['classifier_sources_sha256']),{'live_rules.py','intent_rules.py','clubops.py'})
        row.update(text='带我上分怎么收费',label='buyer')
        self.assertEqual(intent_eval.evaluate(data)['predictions'][0]['category'],'buyer')

    def test_comment_context_routing_is_preserved(self):
        data=self.dataset(); row=data['rows'][0]
        row.update(text='多少钱',parent='陪玩服务',title='无畏契约')
        with patch.object(intent_eval.live_rules,'classify',side_effect=AssertionError('live route')), \
                patch.object(app,'classify',wraps=app.classify) as classify:
            report=intent_eval.evaluate(data)
        classify.assert_called_once_with('多少钱','无畏契约',parent_context='陪玩服务')
        self.assertEqual(report['engine'],intent_eval.RULESET_VERSION)
        self.assertEqual(report['input_kinds'],['comment'])
        self.assertNotIn('live_rules.py',report['classifier_sources_sha256'])

    def test_live_context_cannot_supply_intent_or_extra_duplicate_votes(self):
        data=self.dataset(); row=data['rows'][0]
        row.update(kind='live',text='多少钱')
        for key in ('parent','title'):
            row[key]='陪玩服务'
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,'须留空'):
                intent_eval.evaluate(data)
            row[key]=''
        data['rows'].append({**row,'id':'b','source_ref':'synthetic://different-room'})
        report=intent_eval.evaluate(data)
        self.assertEqual(report['evaluated'],1)
        self.assertEqual(report['skipped']['duplicates'],1)

    def test_mixed_dataset_records_each_actual_rule_engine(self):
        data=self.dataset(); row=data['rows'][0]
        data['rows'].append({**row,'id':'b','kind':'live'})
        report=intent_eval.evaluate(data)
        self.assertEqual(report['engine'],'mixed-rules')
        self.assertIsNone(report['classifier_source_sha256'])
        self.assertEqual({r['id']:r['engine'] for r in report['predictions']},
                         {'a':intent_eval.RULESET_VERSION,'b':'live-rules-v1'})
        self.assertEqual(report['per_kind']['comment']['evaluated'],1)
        self.assertEqual(report['per_kind']['live']['evaluated'],1)

    def test_pending_live_labels_produce_no_predictions_or_accuracy(self):
        data=self.dataset(); row=data['rows'][0]
        row.update(kind='live',review_status='pending',labeler='unassigned',label='',rationale='')
        with patch.object(intent_eval.live_rules,'classify',side_effect=AssertionError('unconfirmed input')):
            report=intent_eval.evaluate(data)
        self.assertEqual(report['engine'],'live-rules-v1')
        self.assertEqual(report['evaluated'],0)
        self.assertEqual(report['predictions'],[])
        self.assertEqual(report['skipped'],{'unconfirmed':1})
        self.assertIsNone(report['sample_accuracy'])
        self.assertIn('不能计算准确率',intent_eval.markdown(report))
        self.assertNotIn('未发现分类分歧',intent_eval.markdown(report))
        row.update(review_status='confirmed',label='buyer',rationale='没有标注者的假确认')
        with self.assertRaisesRegex(ValueError,'实际标注者'):
            intent_eval.evaluate(data)

    def test_external_live_predictions_keep_external_engine_and_input_guard(self):
        data=self.dataset(); row=data['rows'][0]
        row.update(kind='live')
        predictions=[dict(id='a',input_hash=store.digest(intent_eval.source(row)),
                          category='buyer',method='rules',engine='external-test')]
        with patch.object(intent_eval.live_rules,'classify',side_effect=AssertionError('external prediction')):
            report=intent_eval.evaluate(data,predictions)
        self.assertEqual(report['engine'],'external-test')
        self.assertIsNone(report['classifier_source_sha256'])
        self.assertEqual(report['classifier_sources_sha256'],{})
        row['text']='已经修改原文'
        with self.assertRaisesRegex(ValueError,'版本不一致'):
            intent_eval.evaluate(data,predictions)


if __name__=='__main__': unittest.main()
