"""Live cost controls with isolated data and synthetic adapters; no remote API."""
import json
import threading
import unittest
from unittest.mock import patch

import clubops as app
import analysis_store as store
import live_monitor as live
import semantic
import semantic_queue as queue
import test_semantic_queue as fixtures
from test_semantic import SyntheticAdapter, prediction


class LivePolicyTests(unittest.TestCase):
    setUp = fixtures.QueueTests.setUp
    add = fixtures.QueueTests.add
    jobs = fixtures.QueueTests.jobs

    def live_row(self, text='无畏契约找陪练'):
        config = dict(live.DEFAULTS, room_url='https://live.douyin.com/12345')
        with app.db() as c:
            import live_room_pool
            live_room_pool.ingest(c,[dict(room_url=config['room_url'],title='无畏契约陪练')],'saved_room',app.now())
            sid = c.execute('INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                ('fixture-live', config['room_url'], '10000000000000001', json.dumps(config), 'running', '', app.now(), app.now())).lastrowid
        live.receive(sid, dict(type='message', record=dict(room_id='10000000000000001', uid='10000000000000002',
            message_id='10000000000000003', nickname='测试', text=text, published_at=None)))
        with app.db() as c:
            row = c.execute('SELECT id FROM live_messages').fetchone()
            source, _ = store.inputs(c, 'live', row['id'])
        return dict(evidence_type='live', id=row['id'], input_hash=store.digest(source), request_id='manual-fixture')

    def off(self):
        semantic.save(dict(self.settings, live_model_enabled=False))

    def test_live_disabled_blocks_ingest_queue_and_manual_but_comments_continue(self):
        self.off()
        with patch.object(SyntheticAdapter, 'predict') as predict:
            body = self.live_row()
            self.assertEqual(self.jobs(), [])
            with self.assertRaisesRegex(ValueError, '直播弹幕'):
                semantic.analyze_one(body, adapter_factory=SyntheticAdapter)
            predict.assert_not_called()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT analysis_method FROM live_messages').fetchone()[0], 'rules')
            self.assertEqual(c.execute('SELECT count(*) FROM live_links').fetchone()[0], 1)
        self.add()
        self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
        self.assertEqual(self.jobs()[0]['status'], 'completed')

    def test_live_only_change_cancels_only_live_queue_and_keeps_old_comment_config(self):
        self.live_row()
        self.add()
        with app.db() as c:
            old = dict(self.settings)
            old.pop('live_model_enabled')
            c.execute("UPDATE semantic_jobs SET config_json=? WHERE evidence_type='comment'", (json.dumps(old),))
        self.off()
        self.assertEqual([j['status'] for j in self.jobs()], ['cancelled', 'queued'])
        queue.run_one(adapter_factory=SyntheticAdapter)
        self.assertEqual(self.jobs()[1]['status'], 'completed')

    def test_existing_live_result_history_is_unchanged_and_not_requeued(self):
        self.live_row()
        queue.run_one(adapter_factory=SyntheticAdapter)
        with app.db() as c:
            before = [tuple(r) for r in c.execute("SELECT * FROM intent_results WHERE method='model'")]
        engine = semantic.state()['engine']
        self.off()
        semantic.save(self.settings)
        with app.db() as c:
            self.assertEqual(before, [tuple(r) for r in c.execute("SELECT * FROM intent_results WHERE method='model'")])
        self.assertEqual(engine, semantic.state()['engine'])
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(queue.enqueue('live', [1])['existing'], 1)

    def test_dispatch_rechecks_policy_even_when_config_was_written_without_save(self):
        self.live_row()
        (app.DATA_DIR / semantic.CONFIG_FILE).write_text(json.dumps(dict(self.settings, live_model_enabled=False)))
        with patch.object(SyntheticAdapter, 'predict') as predict:
            queue.run_one(adapter_factory=SyntheticAdapter)
            predict.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'], 'cancelled')

    def run_during(self, body, change):
        entered, release = threading.Event(), threading.Event()
        results = []
        def predict(source):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return prediction(source), 'a'*64
        def work():
            results.append(semantic.analyze_one(body, adapter_factory=SyntheticAdapter))
        with patch.object(SyntheticAdapter, 'predict', side_effect=predict):
            worker = threading.Thread(target=work)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                change()
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertFalse(semantic.ACTIVE_CANCELLATIONS)
        return results[0]

    def test_inflight_manual_live_is_cancelled(self):
        body = self.live_row()
        self.assertEqual(self.run_during(body, self.off)['status'], 'cancelled')

    def test_inflight_comment_is_not_cancelled_or_staled_by_live_switch(self):
        self.add()
        job = self.jobs()[0]
        body = dict(evidence_type='comment', id=job['record_id'], input_hash=job['input_hash'], request_id='comment-fixture')
        self.assertEqual(self.run_during(body, self.off)['status'], 'completed')
        self.assertEqual(self.jobs()[0]['status'], 'queued')
        queue.run_one(adapter_factory=SyntheticAdapter)
        self.assertEqual(self.jobs()[0]['status'], 'skipped')

    def test_policy_requires_boolean(self):
        for value in (0, 1, 'false', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                semantic.save(dict(self.settings, live_model_enabled=value))

    def test_older_settings_form_cannot_silently_reenable_live(self):
        self.off()
        older_form = dict(self.settings)
        older_form.pop('live_model_enabled')
        semantic.save(older_form)
        self.assertIs(semantic.config()[0]['live_model_enabled'], False)


if __name__ == '__main__':
    unittest.main()
