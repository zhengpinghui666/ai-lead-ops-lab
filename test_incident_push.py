import json
import unittest
import tempfile
from pathlib import Path
from incident_push import push
from incident_push import resolve_server

THREAD='01a09666-68bb-7860-979d-b3415b851bed'


class Fake:
    def __init__(self,stage=None,receipt=None):self.stage=stage;self.receipt=receipt;self.calls=[];self.closed=False
    def write(self,data):self.calls.append(data)
    def request(self,method,params):
        self.calls.append((method,params))
        if self.stage==method:raise TimeoutError('PRIVATE_SENTINEL')
        if method=='tools/list':return dict(tools=[dict(name='send_message_to_thread')])
        if method=='tools/call':return self.receipt or dict(content=[dict(type='text',text=json.dumps(dict(threadId=THREAD)))])
        return {}
    def close(self):self.closed=True


class PushTests(unittest.TestCase):
    def test_removed_desktop_adapter_uses_installed_same_plugin(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);configured=root/'old'/'server.mjs'
            for version in ('0.1.9','0.1.10','untrusted-name','0.1.11'):
                folder=root/version;folder.mkdir()
                if version!='0.1.11':(folder/'server.mjs').write_text('fixture')
            self.assertEqual(resolve_server({'server':str(configured)},cache_root=root),root/'0.1.10/server.mjs')
            configured.parent.mkdir();configured.write_text('configured')
            self.assertEqual(resolve_server({'server':str(configured)},cache_root=root),configured)
            with self.assertRaises(OSError):resolve_server({'server':str(root/'missing')},cache_root=root/'missing-cache')

    def test_existing_task_only_and_no_execution_overrides(self):
        client=Fake();result=push({},THREAD,'Synthetic incident',client_factory=lambda c:client)
        self.assertEqual(result['status'],'pushed');self.assertTrue(client.closed)
        method,params=client.calls[-1]
        self.assertEqual(method,'tools/call')
        self.assertEqual(params['arguments'],{'threadId':THREAD,'prompt':'Synthetic incident'})
        self.assertEqual(params['name'],'send_message_to_thread')
    def test_pre_send_failure_allows_queue_fallback_but_post_send_is_unknown(self):
        for stage,state in [('initialize','not_sent'),('tools/list','not_sent'),('tools/call','unknown')]:
            client=Fake(stage);result=push({},THREAD,'Synthetic incident',client_factory=lambda c:client)
            self.assertEqual(result['status'],state);self.assertTrue(client.closed)
            self.assertNotIn('PRIVATE_SENTINEL',json.dumps(result))
    def test_wrong_thread_error_and_empty_response_are_not_delivered(self):
        for receipt in [dict(isError=True),dict(content=[dict(type='text',text='{}')]),dict(content=[dict(type='text',text='bad response')])]:
            client=Fake(receipt=receipt)
            self.assertEqual(push({},THREAD,'Synthetic incident',client_factory=lambda c:client)['status'],'unknown')
