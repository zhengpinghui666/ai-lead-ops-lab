"""Parallel inference against isolated data and synthetic adapters only."""
import threading
import unittest
from unittest.mock import patch

import clubops as app
import semantic
import semantic_queue as queue
import test_semantic_queue as fixtures
from test_semantic import SyntheticAdapter, prediction


class ConcurrencyTests(unittest.TestCase):
    setUp = fixtures.QueueTests.setUp
    add = fixtures.QueueTests.add
    jobs = fixtures.QueueTests.jobs

    def configure(self, limit=3, backend='openai_compatible'):
        credentials = patch('model_credentials.ready', return_value=True)
        credentials.start()
        self.addCleanup(credentials.stop)
        self.settings.update(backend=backend, api_base_url='https://api.example.test/v1', max_concurrency=limit)
        semantic.save(self.settings)

    def overlap(self, during=None, *, limit=3):
        self.configure(limit)
        for i in range(limit+1):
            self.add(str(i), text='无畏契约普通讨论 '+str(i))
        release, denied = threading.Event(), threading.Event()
        barrier = threading.Barrier(limit+1)
        calls, workers = [], []
        def predict(source):
            calls.append(source['text'])
            barrier.wait(5)
            if not release.wait(5):
                raise TimeoutError('fixture deadline')
            return prediction(source), 'a'*64
        def work():
            if not queue.run_one(adapter_factory=SyntheticAdapter):
                denied.set()
        with patch.object(SyntheticAdapter, 'predict', side_effect=predict):
            try:
                for _ in range(limit+1):
                    worker = threading.Thread(target=work)
                    workers.append(worker)
                    worker.start()
                barrier.wait(5)
                self.assertTrue(denied.wait(3), 'Extra worker must not enter inference')
                self.assertEqual(semantic.GUARD.count(), limit)
                self.assertEqual(len(queue.ACTIVE), limit)
                self.assertEqual(len(set(calls)), limit, 'Each worker claims a different source')
                if during:
                    during()
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
                self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertFalse(queue.ACTIVE)
        self.assertFalse(semantic.GUARD.locked())
        return calls

    def test_three_calls_overlap_but_fourth_waits_then_runs_once(self):
        calls = self.overlap()
        self.assertEqual(len(calls), 3)
        self.assertEqual([x['status'] for x in self.jobs()].count('completed'), 3)
        self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
        self.assertEqual([x['status'] for x in self.jobs()], ['completed']*4)
        with app.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM intent_results WHERE method='model'").fetchone()[0], 4)

    def test_cancel_all_discards_every_late_parallel_result(self):
        self.overlap(lambda: self.assertEqual(queue.cancel_all(), {'cancelled':1, 'cancelling':3}))
        self.assertEqual([x['status'] for x in self.jobs()], ['cancelled']*4)
        self.assertTrue(all(x['analysis_method']=='rules' for x in app.state()['comments']))

    def test_lowering_capacity_cancels_all_old_config_work(self):
        self.overlap(lambda: semantic.save(dict(self.settings, max_concurrency=1)))
        self.assertEqual([x['status'] for x in self.jobs()], ['cancelled']*4)
        self.assertEqual(queue.state()['concurrency_limit'], 1)
        self.add('new-config')
        self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
        self.assertEqual(self.jobs()[-1]['status'], 'completed')

    def test_human_review_during_overlap_remains_authoritative(self):
        def review():
            record = next(x for x in self.jobs() if x['status']=='running')['record_id']
            app.mutate('review', dict(id=record, category='social', reason='人工核对并发测试'))
        self.overlap(review)
        human = [x for x in app.state()['comments'] if x['analysis_method']=='human']
        self.assertEqual(len(human), 1)
        self.assertEqual(human[0]['category'], 'social')

    def test_manual_and_queue_never_reanalyze_the_same_input_version(self):
        self.configure()
        self.add('manual')
        job = self.jobs()[0]
        request = dict(evidence_type='comment', id=job['record_id'], input_hash=job['input_hash'], request_id='manual-concurrency')
        entered, release = threading.Event(), threading.Event()
        def slow(source):
            entered.set()
            release.wait(5)
            return prediction(source), 'a'*64
        with patch.object(SyntheticAdapter, 'predict', side_effect=slow) as predict:
            worker = threading.Thread(target=semantic.analyze_one, args=(request,), kwargs={'adapter_factory':SyntheticAdapter})
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(semantic.ModelBusy):
                    semantic.analyze_one(dict(request, request_id='another-manual-request'), adapter_factory=SyntheticAdapter)
                self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
                self.assertEqual(self.jobs()[0]['status'], 'skipped')
            finally:
                release.set()
                worker.join(5)
            self.assertEqual(predict.call_count, 1)
        self.assertFalse(semantic.GUARD.locked())

    def test_local_backend_stays_single_and_invalid_limits_rejected(self):
        self.configure(4, backend='ollama')
        self.assertEqual(queue.state()['concurrency_limit'], 1)
        self.add('local')
        with semantic.GUARD:
            self.assertFalse(queue.run_one(adapter_factory=SyntheticAdapter))
        for value in [0, 5, True, '3', 1.5]:
            with self.assertRaises(ValueError):
                semantic.validate_config(dict(self.settings, max_concurrency=value))

    def test_service_starts_once_and_stops_all_workers(self):
        self.configure()
        entered = threading.Barrier(queue.MAX_WORKERS+1)
        def bounded_work():
            entered.wait(3)
            queue.STOP.wait(3)
            return False
        with patch.object(queue, 'run_one', side_effect=bounded_work):
            try:
                queue.start_service()
                entered.wait(3)
                workers = list(queue.THREADS)
                queue.start_service()
                self.assertEqual(queue.THREADS, workers)
                self.assertEqual(len(workers), queue.MAX_WORKERS)
                self.assertTrue(queue.state()['worker_running'])
            finally:
                queue.shutdown()
            self.assertTrue(all(not t.is_alive() for t in queue.THREADS))
            self.assertFalse(queue.state()['worker_running'])


if __name__ == '__main__':
    unittest.main()
