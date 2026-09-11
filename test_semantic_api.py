"""Isolated API credentials, transport and queue integration; no paid API calls."""
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import clubops as app
import semantic
import semantic_api
import semantic_queue as queue
import model_credentials as credentials
from test_semantic import ollama_prediction

BASE = 'https://api.example.test/v1'
KEY = 'synthetic-test-credential'
SOURCE = dict(kind='comment',text='无畏契约找陪练，预算200元',parent='',title='')


def response(source=SOURCE):
    return dict(model='reported-model',choices=[dict(finish_reason='stop',message=dict(
        role='assistant',content=json.dumps(ollama_prediction(source),ensure_ascii=False)))])


class APITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='model-api-test-')
        self.addCleanup(temporary.cleanup)
        old = app.DATA_DIR
        app.DATA_DIR = Path(temporary.name)
        self.addCleanup(setattr,app,'DATA_DIR',old)
        app.init()
        queue.STOP.clear()
        self.settings = dict(semantic.DEFAULTS,enabled=True,backend='openai_compatible',api_base_url=BASE,model='test-model')
        credentials.save(BASE,KEY)

    def test_key_is_encrypted_bound_and_absent_from_state_config_and_queue(self):
        semantic.save(dict(self.settings,api_key=KEY,auto_analyze=True))
        blob=credentials.key_path().read_bytes()
        self.assertNotIn(KEY.encode(),blob)
        self.assertTrue(blob.startswith(credentials.MAGIC))
        self.assertEqual(credentials.load(BASE),KEY)
        self.assertFalse(credentials.ready('https://elsewhere.example.test'))
        with self.assertRaises(ValueError):
            semantic.save(dict(self.settings,api_base_url='https://elsewhere.example.test'))
        app.ingest({'records':[dict(comment_id='1',video_id='1',user_id='12345',text=SOURCE['text'])]})
        self.assertEqual(app.analyze()['model_queue']['queued'],1)
        with app.db() as c:
            queued=c.execute('SELECT config_json FROM semantic_jobs').fetchone()[0]
        for raw in ((app.DATA_DIR/semantic.CONFIG_FILE).read_text(),json.dumps(app.state()),queued):
            self.assertNotIn(KEY,raw)
            self.assertNotIn('"api_key":',raw)
        self.assertTrue(semantic.state()['api_key_configured'])
        self.assertEqual(semantic.state()['mode'],'remote_api_configured')

    def test_invalid_endpoint_and_credentials_cannot_enable(self):
        for url in ('http://api.example.test','https://u:p@api.example.test','https://api.example.test/path',
                    'https://api.example.test?key=foo','https://api.example.test#key',
                    'https://api.example.test:444/v1','https://api.example.test/ v1'):
            with self.subTest(url=url),self.assertRaises(ValueError):
                semantic.validate_config(dict(self.settings,api_base_url=url))
        self.assertEqual(credentials.normalize_url('https://API.EXAMPLE.TEST:443/'),'https://api.example.test/v1')
        for key in ('', 'bad\r\nheader', 123):
            with self.assertRaises(ValueError):
                credentials.save(BASE,key)
        credentials.key_path().write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            semantic.save(self.settings)

    def test_transport_is_verified_https_and_exact_endpoint_bound(self):
        adapter=semantic_api.ChatAPIAdapter(self.settings)
        path,headers=adapter.prepare_request('/chat/completions')
        self.assertEqual(path,'/v1/chat/completions')
        self.assertEqual(headers['Authorization'],'Bearer '+KEY)
        with patch('semantic_api.http.client.HTTPSConnection') as connection:
            adapter.connection(5)
            args,kwargs=connection.call_args
            self.assertEqual(args,('api.example.test',))
            self.assertTrue(kwargs['context'].check_hostname)
            self.assertEqual(kwargs['context'].verify_mode,ssl.CERT_REQUIRED)
        other=semantic_api.ChatAPIAdapter(dict(self.settings,api_base_url='https://other.example.test'))
        with patch.object(other,'connection') as connect, self.assertRaises(ValueError):
            other.request('/chat/completions',{})
        connect.assert_not_called()

    def test_redirect_auth_and_rate_fail_once_without_echoing_body(self):
        for status,code in ((302,'api_redirect'),(400,'api_request'),(401,'api_auth'),(403,'api_denied'),(429,'api_rate'),(500,'api_server')):
            adapter=semantic_api.ChatAPIAdapter(self.settings)
            conn=MagicMock()
            conn.getresponse.return_value.status=status
            with patch.object(adapter,'connection',return_value=conn),self.assertRaisesRegex(semantic.ModelError,'^'+code+'$'):
                adapter.request('/chat/completions',{})
            conn.request.assert_called_once()
            conn.getresponse.return_value.read1.assert_not_called()
            conn.close.assert_called_once()

    def test_request_minimizes_data_and_checks_structured_evidence(self):
        adapter=semantic_api.ChatAPIAdapter(self.settings)
        with patch.object(adapter,'request',return_value=response()) as request:
            result,fingerprint=adapter.predict(dict(SOURCE,uid='12345',cookie='private',api_key='private'))
        body=request.call_args.args[1]
        self.assertEqual(json.loads(body['messages'][1]['content']),SOURCE)
        self.assertNotIn('tools',body)
        self.assertFalse(body['stream'])
        self.assertIn(json.dumps(app.GAMES,ensure_ascii=False),body['messages'][0]['content'])
        self.assertEqual(len(fingerprint),64)
        self.assertEqual(semantic.validate_result(result,SOURCE)['category'],'noise')
        invalid=response()
        value=json.loads(invalid['choices'][0]['message']['content'])
        value['facts']['budget']=dict(value='预算999元',quote='预算999元')
        invalid['choices'][0]['message']['content']=json.dumps(value)
        with patch.object(adapter,'request',return_value=invalid),self.assertRaises(ValueError):
            semantic.validate_result(adapter.predict(SOURCE)[0],SOURCE)

    def test_partial_refusal_tool_and_malformed_outputs_are_rejected(self):
        cases=[]
        for changes in (dict(finish_reason='length'),dict(message={'content':'{}','refusal':'refused'}),
                        dict(message={'content':'{}','tool_calls':[{}]}),dict(message={'content':'not-json'})):
            value=response();value['choices'][0].update(changes);cases.append(value)
        cases.extend(({'choices':[]},{'choices':[response()['choices'][0]]*2}))
        for value in cases:
            adapter=semantic_api.ChatAPIAdapter(self.settings)
            with patch.object(adapter,'request',return_value=value),self.assertRaises((semantic.ModelError,ValueError)):
                adapter.predict(SOURCE)

    def test_empty_game_origin_is_canonicalized_only_without_an_assertion(self):
        for game,accepted in ((dict(value='',quote='',source=''),True),
                              (dict(value='无畏契约',quote='无畏契约',source=''),False),
                              (dict(value='',quote='无畏契约',source=''),False)):
            result=response()
            content=json.loads(result['choices'][0]['message']['content'])
            content['game']=game
            result['choices'][0]['message']['content']=json.dumps(content)
            adapter=semantic_api.ChatAPIAdapter(self.settings)
            with patch.object(adapter,'request',return_value=result):
                if accepted:
                    self.assertEqual(semantic.validate_result(adapter.predict(SOURCE)[0],SOURCE)['game'],'')
                else:
                    with self.assertRaises(ValueError):
                        adapter.predict(SOURCE)

    def test_default_queue_selects_api_and_preserves_rules_without_contact(self):
        semantic.save(dict(self.settings,auto_analyze=True))
        app.ingest({'records':[dict(comment_id='1',video_id='1',user_id='12345',text=SOURCE['text'])]})
        self.assertEqual(app.analyze()['model_queue']['queued'],1)
        with patch.object(semantic_api.ChatAPIAdapter,'request',return_value=response()) as request:
            self.assertTrue(queue.run_one())
        request.assert_called_once()
        self.assertEqual(queue.state()['counts'],{'completed':1})
        row=app.state()['comments'][0]
        self.assertEqual((row['analysis_method'],row['rule_category'],row['category']),('model','buyer','noise'))
        self.assertEqual(app.state()['leads'][0]['contact_basis'],'')
        self.assertEqual(app.state()['messages'],[])
        self.assertEqual(app.state()['jobs'],[])


if __name__=='__main__':
    unittest.main()
