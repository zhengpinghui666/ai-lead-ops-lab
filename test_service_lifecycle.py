"""Normal service shutdown, local HTTP only, temporary DB, no platform calls."""
import json
from contextlib import closing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

BASE = Path(__file__).resolve().parent


class ServiceLifecycleTests(unittest.TestCase):
    def test_authenticated_idle_shutdown_and_lock_release(self):
        with tempfile.TemporaryDirectory(prefix='clubops-lifecycle-') as directory:
            root=Path(directory)
            log=root/'service.log'
            environment={**os.environ,'CLUBOPS_DATA_DIR':str(root/'data'),'LEADOPS_PORT':'0','PYTHONIOENCODING':'utf-8'}
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with log.open('wb') as stream:
                child=subprocess.Popen([sys.executable,'-u',str(BASE/'server.py')],cwd=BASE,env=environment,
                    stdout=stream,stderr=stream,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:
                    import re
                    address=None
                    deadline=time.monotonic()+12
                    while time.monotonic()<deadline:
                        text=log.read_text(encoding='utf-8')
                        match=re.search(r'http://127\.0\.0\.1:\d+/',text)
                        if match:address=match.group();break
                        if child.poll() is not None:self.fail(text)
                        time.sleep(.1)
                    self.assertIsNotNone(address)
                    with opener.open(address+'api/service',timeout=3) as response:instance=json.load(response)
                    self.assertEqual(instance['pid'],child.pid)
                    self.assertTrue(instance['graceful_shutdown'])
                    with opener.open(address+'api/state',timeout=3) as response:state=json.load(response)
                    self.assertEqual(state['collector']['verification']['mode'],'auto')
                    url=address+'api/service-stop?mode=live'
                    payload=json.dumps({'instance_id':instance['instance_id']}).encode()
                    def post(data=payload,headers=None,target=url):
                        return opener.open(urllib.request.Request(target,data=data,headers=headers or {},method='POST'),timeout=3)
                    with self.assertRaises(urllib.error.HTTPError) as no_token:post()
                    self.assertEqual(no_token.exception.code,403)
                    no_token.exception.close()
                    headers={'X-ClubOps-Token':state['csrf'],'Content-Type':'application/json','Origin':address.rstrip('/')}
                    with self.assertRaises(urllib.error.HTTPError) as wrong_origin:post(headers={**headers,'Origin':'https://external.invalid'})
                    self.assertEqual(wrong_origin.exception.code,403)
                    wrong_origin.exception.close()
                    with self.assertRaises(urllib.error.HTTPError) as stale:post(b'{"instance_id":"stale"}',headers)
                    self.assertEqual(stale.exception.code,400)
                    stale.exception.close()
                    with self.assertRaises(urllib.error.HTTPError) as demo:post(headers=headers,target=address+'api/service-stop?mode=demo')
                    self.assertEqual(demo.exception.code,400)
                    demo.exception.close()
                    import sqlite3
                    # Exercise active-write refusal directly without starting platform work.
                    import server
                    import clubops
                    from unittest.mock import patch
                    httpd=server.LocalHTTPServer(('127.0.0.1',0),server.Handler)
                    try:
                        with patch.object(clubops,'DATA_DIR',root/'data'):
                            httpd.active_writes=2
                            with self.assertRaises(ValueError):httpd.prepare_stop()
                            self.assertFalse(httpd.stopping)
                    finally:httpd.server_close()
                    with post(headers=headers) as response:
                        self.assertEqual(response.status,202)
                        self.assertEqual(json.load(response)['status'],'stopping')
                    self.assertEqual(child.wait(timeout=10),0)
                    import runtime
                    with runtime.data_lock(root/'data'):pass
                    with closing(sqlite3.connect(root/'data/clubops-live.db')) as db:
                        self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5)


if __name__=='__main__':unittest.main()
