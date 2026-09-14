import json
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import bark_notify as bark
from incident_bridge import Bridge


class BarkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.key='synthetic_device_key_1234'
    def configure(self):return bark.save(self.root,{'url':'https://api.day.app/'+self.key+'/test','enabled':True})
    def verify(self):
        self.configure();value=bark.test(self.root,{},lambda *args:'accepted')
        return bark.confirm(self.root,{'id':value['last_test']['id']})
    def test_strict_host_and_key(self):
        for raw in ['http://api.day.app/'+self.key,'https://api.day.app.evil/'+self.key,'https://127.0.0.1/'+self.key,'https://api.day.app:444/'+self.key,'https://x@api.day.app/'+self.key,'https://api.day.app/%2Fbad','https://api.day.app/x',None]:
            with self.subTest(raw=raw),self.assertRaises(ValueError):bark.parse_key(raw)
        self.assertEqual(bark.parse_key('https://api.day.app/'+self.key+'/测试?group=a'),self.key)
    def test_save_is_encrypted_and_read_is_redacted(self):
        value=self.configure();self.assertTrue(value['configured']);self.assertFalse(value['verified'])
        self.assertNotIn(self.key,json.dumps(value));self.assertNotIn(self.key.encode(),(bark.directory(self.root)/'bark.dpapi').read_bytes())
        self.assertEqual(bark.load(self.root)['key'],self.key)
        revision=bark.load(self.root)['revision'];bark.save(self.root,{'url':'','enabled':False})
        self.assertEqual(bark.load(self.root)['revision'],revision);self.assertFalse(bark.state(self.root)['enabled'])
    def test_acceptance_requires_actual_user_confirmation(self):
        self.configure();value=bark.test(self.root,{},lambda *args:'accepted')
        self.assertFalse(value['verified']);self.assertIsNone(value['last_test']['confirmed_at'])
        self.assertTrue(bark.confirm(self.root,{'id':value['last_test']['id']})['verified'])
        bark.save(self.root,{'url':'https://api.day.app/different_device_key_123','enabled':True})
        self.assertFalse(bark.state(self.root)['verified'])
    def test_rejection_and_expiry_cannot_be_confirmed(self):
        self.configure();value=bark.test(self.root,{},lambda *args:'rejected')
        with self.assertRaises(ValueError):bark.confirm(self.root,{'id':value['last_test']['id']})
        with self.assertRaises(ValueError):bark.test(self.root,{})
    def test_no_send_before_user_confirms_and_dedup_after(self):
        self.configure();bridge=Bridge(self.root)
        report={'service':'running','comments':{'issue':'comments:attention','last_task_id':2876,'task_status':'needs_verification'}}
        bridge.observe(report)
        with closing(bridge.connect()) as c,c:c.execute("UPDATE incidents SET state='needs_user'")
        calls=[];transport=lambda *args:calls.append(args) or 'accepted'
        bark.notify_incidents(bridge,transport);self.assertEqual(calls,[])
        self.verify();bark.notify_incidents(bridge,transport);bark.notify_incidents(bridge,transport)
        self.assertEqual(len(calls),1);self.assertIn('评论监控',calls[0][2]);self.assertNotIn('2876',calls[0][2])
    def test_unknown_does_not_resend(self):
        self.configure();settings=bark.load(self.root);calls=[]
        transport=lambda *args:calls.append(args) or 'unknown'
        self.assertEqual(bark.deliver(self.root,settings,'once','incident','title','text',transport),'unknown')
        self.assertIsNone(bark.deliver(self.root,settings,'once','incident','title','text',transport));self.assertEqual(len(calls),1)
    def test_network_uses_post_body_not_key_path_and_sanitizes_response(self):
        class Reply:
            status=200
            def read(self,limit):return b'{"code":200}'
        class Connection:
            def request(self,*args):self.args=args
            def getresponse(self):return Reply()
            def close(self):pass
        conn=Connection()
        with patch.object(bark.http.client,'HTTPSConnection',return_value=conn):self.assertEqual(bark.send(self.key,'title','body'),'accepted')
        self.assertEqual(conn.args[:2],('POST','/push'));self.assertEqual(json.loads(conn.args[2])['device_key'],self.key)
    def test_current_pause_only_no_synthetic_or_old_incident(self):
        self.verify();bridge=Bridge(self.root);bridge.self_test()
        with closing(bridge.connect()) as c,c:c.execute("UPDATE incidents SET state='needs_user'")
        calls=[];bark.notify_incidents(bridge,lambda *args:calls.append(args) or 'accepted');self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
