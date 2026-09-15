import json
import unittest
from datetime import datetime,timedelta
from unittest.mock import patch
import clubops as app
import analysis_store as store
import semantic
import semantic_queue as queue
import semantic_retry as retry
from test_semantic_queue import QueueTests
from test_semantic import SyntheticAdapter


class TimeoutAdapter(SyntheticAdapter):
    def predict(self,source):raise TimeoutError()


class RetryTests(unittest.TestCase):
    add=QueueTests.add
    jobs=QueueTests.jobs

    def setUp(self):
        QueueTests.setUp(self)
        self.old_scan=queue.RETRY_SCAN_AT;queue.RETRY_SCAN_AT=float('inf')
        self.addCleanup(setattr,queue,'RETRY_SCAN_AT',self.old_scan)
        self.at=datetime.fromisoformat(app.now())

    def failed(self,*,title=None):
        self.add(**({'title':title} if title else {}))
        with app.db() as c:c.execute('UPDATE comments SET published_at=?',(self.at.isoformat(),))
        queue.run_one(adapter_factory=TimeoutAdapter)
        self.assertEqual(self.jobs()[0]['status'],'failed')

    def promote(self,seconds=61):
        with app.db() as c:return retry.promote(c,self.settings,semantic.state(),self.at+timedelta(seconds=seconds))

    def test_two_retries_only_and_all_attempts_retained(self):
        self.failed();self.assertEqual(self.promote(1),0)
        self.assertEqual(queue.state()['counts']['retry_wait'],1)
        self.assertEqual(self.promote(),1);self.assertEqual(self.promote(),0)
        with patch.object(app,'now',return_value=(self.at+timedelta(seconds=62)).isoformat()):queue.run_one(adapter_factory=TimeoutAdapter)
        self.assertEqual(self.promote(100),0);self.assertEqual(self.promote(303),1)
        queue.run_one(adapter_factory=TimeoutAdapter)
        self.assertEqual(self.promote(999),0)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT retries FROM semantic_job_retries').fetchone()[0],2)
            ids=[r[0] for r in c.execute("SELECT request_id FROM intent_results WHERE method='model' ORDER BY id")]
            self.assertEqual(ids,['queue-job-1','queue-job-1-retry-1','queue-job-1-retry-2'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)

    def test_success_stops_retries_and_keeps_failure_history(self):
        self.failed();self.assertEqual(self.promote(),1);queue.run_one(adapter_factory=SyntheticAdapter)
        self.assertEqual(self.jobs()[0]['status'],'completed');self.assertEqual(self.promote(900),0)
        with app.db() as c:self.assertEqual([r[0] for r in c.execute("SELECT status FROM intent_results WHERE method='model' ORDER BY id")],['failed','completed'])

    def test_capacity_and_server_retry_keep_the_same_config(self):
        self.failed()
        with app.db() as c:
            rid=self.jobs()[0]['result_id']
            c.execute('UPDATE intent_results SET result_json=? WHERE id=?',(json.dumps(dict(diagnostic=dict(code='api_server',http_status=503))),rid))
            stamp=self.at+timedelta(seconds=61)
            self.assertEqual(retry.promote(c,self.settings,semantic.state(),stamp,capacity=0),0)
            self.assertEqual(retry.promote(c,{**self.settings,'model':'different:1'},semantic.state(),stamp),0)
            self.assertEqual(retry.promote(c,self.settings,semantic.state(),stamp),1)

    def test_long_saved_description_reaches_model_without_truncation(self):
        title='无畏契约陪玩服务。'+'完整上下文。'*190
        self.add(title=title)
        with app.db() as c:
            c.execute('UPDATE comments SET published_at=?',(self.at.isoformat(),))
            c.execute("UPDATE semantic_jobs SET status='failed',finished_at=?,detail='旧版输入长度限制'",(self.at.isoformat(),))
        self.assertEqual(self.promote(),1)
        with patch.object(SyntheticAdapter,'predict',autospec=True,side_effect=lambda adapter,source:self._predict_title(source,title)):
            queue.run_one(adapter_factory=SyntheticAdapter)
        self.assertEqual(self.jobs()[0]['status'],'completed')

    def _predict_title(self,source,title):
        from test_semantic import prediction
        self.assertEqual(source['title'],title)
        return prediction(source),'a'*64

    def test_auth_rate_schema_and_unknown_failures_do_not_retry(self):
        self.failed()
        with app.db() as c:
            rid=self.jobs()[0]['result_id']
            for code,status in [('api_auth',401),('api_rate',429),('tls',None),('invalid_result',200),('unavailable',None),('api_server',501)]:
                c.execute('UPDATE intent_results SET result_json=? WHERE id=?',(json.dumps(dict(diagnostic=dict(code=code,http_status=status))),rid))
                self.assertEqual(retry.promote(c,self.settings,semantic.state(),self.at+timedelta(seconds=61)),0,code)

    def test_manual_changed_expired_or_disabled_work_is_preserved(self):
        self.failed()
        self.assertEqual(self.promote(86401),0)
        with app.db() as c:
            c.execute("UPDATE comments SET analysis_method='human'")
        self.assertEqual(self.promote(),0)
        with app.db() as c:
            c.execute("UPDATE comments SET analysis_method='rules',raw_text='原文已改'")
        self.assertEqual(self.promote(),0)
        with app.db() as c:
            self.assertEqual(retry.promote(c,{**self.settings,'auto_analyze':False},semantic.state(),self.at+timedelta(seconds=61)),0)

    def test_manual_stop_cancels_waiting_retry_and_counts_survive_recovery(self):
        self.failed();self.assertEqual(self.promote(1),0)
        queue.recover();self.assertEqual(queue.state()['counts']['retry_wait'],1)
        queue.cancel_all();self.assertEqual(self.jobs()[0]['status'],'cancelled')
        self.assertEqual(self.promote(),0);self.assertEqual(queue.state()['counts']['retry_wait'],0)

    def test_new_manual_model_result_is_not_overwritten(self):
        self.failed();self.assertEqual(self.promote(),1)
        job=self.jobs()[0]
        semantic.analyze_one(dict(evidence_type='comment',id=job['record_id'],input_hash=job['input_hash'],request_id='manual-new-result'),adapter_factory=SyntheticAdapter)
        with patch.object(SyntheticAdapter,'predict') as call:queue.run_one(adapter_factory=SyntheticAdapter);call.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'],'skipped')

    def test_retry_that_expires_while_queued_does_not_call_model(self):
        self.failed();self.assertEqual(self.promote(),1)
        with app.db() as c:c.execute('UPDATE comments SET published_at=?',((self.at-timedelta(days=2)).isoformat(),))
        with patch.object(SyntheticAdapter,'predict') as call:queue.run_one(adapter_factory=SyntheticAdapter);call.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'],'stale')

    def test_description_limit_is_still_bounded(self):
        self.add()
        with app.db() as c:
            c.execute("UPDATE videos SET title=?",('无畏契约陪玩'+'字'*5000,))
            source,_=store.inputs(c,'comment',1)
        with patch.object(SyntheticAdapter,'predict') as call:
            with self.assertRaisesRegex(ValueError,'输入上限'):
                semantic.analyze_one(dict(evidence_type='comment',id=1,input_hash=store.digest(source),request_id='oversized-title'),adapter_factory=SyntheticAdapter)
            call.assert_not_called()


if __name__=='__main__':unittest.main()
