"""Explicit HTTPS Chat Completions adapter; no redirects, tools or retries."""
import http.client
import json
import ssl
from urllib.parse import urlsplit

import semantic
import analysis_store as store
import model_credentials
import clubops as app


class ChatAPIAdapter(semantic.OllamaAdapter):
    def prepare_request(self, path):
        if path != '/chat/completions':
            raise semantic.ModelError('unavailable')
        base = model_credentials.normalize_url(self.settings['api_base_url'])
        self.phase = 'api_inference'
        return urlsplit(base).path + path, {'Content-Type':'application/json', 'Accept':'application/json',
                                           'Authorization':'Bearer '+model_credentials.load(base)}

    def connection(self, timeout):
        host = urlsplit(model_credentials.normalize_url(self.settings['api_base_url'])).hostname
        return http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())

    def response_error(self, status):
        if status in (401, 403, 429):
            return {401:'api_auth',403:'api_denied',429:'api_rate'}[status]
        if 300 <= status < 400:
            return 'api_redirect'
        if 400 <= status < 500:
            return 'api_request'
        if 500 <= status < 600:
            return 'api_server'
        return 'unavailable'

    def predict(self, source):
        # Keep identity, cookies and unrelated record fields outside the request.
        source = {field: source[field] for field in ('kind', 'text', 'parent', 'title')}
        response = self.request('/chat/completions',dict(model=self.settings['model'],stream=False,
            temperature=0,max_tokens=2048,response_format={'type':'json_object'},
            messages=[{'role':'system','content':semantic.PROMPT+semantic.game_prompt(source)+
                       '\n未知游戏固定写为 {"value":"","quote":"","source":"text"}。返回 JSON 必须符合此结构：'+json.dumps(semantic.ollama_schema(),ensure_ascii=False)},
                      {'role':'user','content':json.dumps(source,ensure_ascii=False)}]))
        choices = response.get('choices')
        if not isinstance(choices,list) or len(choices)!=1 or not isinstance(choices[0],dict):
            raise semantic.ModelError('invalid_result')
        choice = choices[0]
        message = choice.get('message')
        if (choice.get('finish_reason')!='stop' or not isinstance(message,dict) or message.get('tool_calls') or
                message.get('function_call') or message.get('refusal') or not isinstance(message.get('content'),str)):
            raise semantic.ModelError('invalid_result')
        # Metadata hash identifies the provider/model response, not model weights.
        fingerprint=store.digest({'api_base_url':self.settings['api_base_url'],'requested_model':self.settings['model'],
                                  'reported_model':response.get('model'),'system_fingerprint':response.get('system_fingerprint')})
        result=json.loads(message['content'])
        # With no asserted game or quote, an empty origin carries no evidence.
        # Canonicalize only this empty tuple; asserted facts remain strictly checked.
        if isinstance(result,dict) and result.get('game') == {'value':'','quote':'','source':''}:
            result['game']['source']='text'
        return semantic.normalize_ollama_result(result),fingerprint
