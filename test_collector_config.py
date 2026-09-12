"""Actual child stdin framing tests; collection is replaced before main runs."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


BASE=Path(__file__).resolve().parent
LIMIT=1024*1024
SENTINEL='PRIVATE_CONFIG_SENTINEL'
STUB='''import collector_http_worker as w
def collect(config,emit,cancel):
    ids=config.get('candidate_policy',{}).get('vertical_ids',[])
    if config.get('wait_for_cancel'):
        emit({'type':'status','status':'cancelled' if cancel.wait(2) else 'failed'})
        return
    emit({'type':'status','status':'completed','ids_count':len(ids),'last_id':ids[-1] if ids else None})
w.collect=collect
w.main()
'''


class CollectorConfigTests(unittest.TestCase):
    def run_child(self,raw):
        result=subprocess.run([sys.executable,'-c',STUB],input=raw,capture_output=True,cwd=BASE,timeout=10,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertNotIn(SENTINEL.encode(),result.stdout+result.stderr)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace')[-200:])
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_full_supported_candidate_pool_reaches_worker_without_truncation(self):
        ids=[str(7600000000000000000+i) for i in range(10000)]
        policy={'version':'candidate-vertical-rotation-v2','vertical_ids':ids,'history':[
            dict(video_id=ids[i],last_selected_ms=1789230000000,priority_ms=1789230030000) for i in range(500)]}
        raw=(json.dumps({'kind':'search','target':'无畏契约陪玩','candidate_policy':policy},ensure_ascii=False)+'\n').encode()
        self.assertGreater(len(raw),30001);self.assertLess(len(raw),LIMIT)
        result=self.run_child(raw)
        self.assertEqual(result,[dict(type='status',status='completed',ids_count=10000,last_id=ids[-1])])

    def test_rejects_oversized_invalid_or_incomplete_line_without_collecting(self):
        for raw in [b'{"private":"'+SENTINEL.encode()+b'","padding":"'+b'x'*LIMIT+b'"}\n',
                    b'{invalid '+SENTINEL.encode()+b'\n',b'[]\n',b'{"key":"\xff"}\n',b'{"kind":"video"}']:
            with self.subTest(length=len(raw)):
                result=self.run_child(raw)
                self.assertEqual(len(result),1)
                self.assertEqual((result[0]['type'],result[0]['status']),('status','failed'))
                self.assertNotIn('ids_count',result[0])

    def test_config_frame_does_not_consume_following_control_frame(self):
        self.assertEqual(self.run_child(b'{"wait_for_cancel":true}\n{"command":"cancel"}\n')[0]['status'],'cancelled')


if __name__=='__main__':unittest.main()
