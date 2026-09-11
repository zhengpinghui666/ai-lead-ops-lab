import base64
import gzip
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
import unittest
import team_access as access

class Handler(BaseHTTPRequestHandler):
    received=[]
    def log_message(self,*args):pass
    def do_GET(self):
        self.received.append((self.command,self.path,dict(self.headers)))
        raw=('原有内容'*10000 if self.path=='/api/state' else '原有内容').encode()
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(raw)
    def do_POST(self):
        self.received.append((self.command,self.path,dict(self.headers)))
        self.send_response(400);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"error":"fixture"}')

class AccessTests(unittest.TestCase):
    def test_local_forwarding_preserves_body_and_local_origin(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        origin=f'http://127.0.0.1:{server.server_port}'
        try:
            row=access.forward(dict(id='fixture',method='GET',path='/',headers={}),origin)
            self.assertEqual(base64.b64decode(row['body']).decode(),'原有内容')
            self.assertNotIn('body_encoding',row)
            row=access.forward(dict(id='large',method='GET',path='/api/state',headers={}),origin)
            compressed=base64.b64decode(row['body'])
            self.assertEqual(row['body_encoding'],'gzip')
            self.assertEqual(gzip.decompress(compressed).decode(),'原有内容'*10000)
            self.assertLess(len(compressed),12000)
            row=access.forward(dict(id='fixture',method='POST',path='/api/monitor-stop?mode=live',body='{}',headers={'x-clubops-token':'fixture'}),origin)
            self.assertEqual(row['status'],400)
            headers={k.lower():v for k,v in Handler.received[-1][2].items()}
            self.assertEqual(headers['origin'],origin);self.assertEqual(headers['x-clubops-token'],'fixture')
        finally:server.shutdown();server.server_close();thread.join()
    def test_fixed_routes_and_header_injection(self):
        for path in ['http://evil.test/','//evil.test/','/../private/token','/api/service','/x\\y','/api/state#fragment']:
            self.assertFalse(access.allowed('GET',path),path)
        self.assertFalse(access.allowed('POST','/api/service-stop'))
        with self.assertRaises(ValueError):access.forward(dict(id='test',method='GET',path='/',headers={'x-clubops-token':'x\r\nInjected: bad'}),'http://127.0.0.1:1')
    def test_public_gateway_and_computer_routes_agree(self):
        from pathlib import Path
        source=(Path(__file__).parent/'integrations/team-access/gateway.mjs').read_text(encoding='utf-8')
        for path in access.GETS:self.assertIn("'"+path+"'",source)
        self.assertNotIn('/_access/login',source)

if __name__=='__main__':unittest.main()
