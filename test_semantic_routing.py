import json,sqlite3,unittest
from unittest.mock import patch
import clubops as app
import analysis_store as store
import semantic,semantic_queue,semantic_routing as routing,semantic_copy
from test_semantic_queue import QueueTests
from test_semantic import SyntheticAdapter,prediction

class RoutingTests(unittest.TestCase):
    setUp=QueueTests.setUp
    add=QueueTests.add
    jobs=QueueTests.jobs
    def configure(self):
        ready=patch('model_credentials.ready',return_value=True);ready.start();self.addCleanup(ready.stop)
        self.settings.update(backend='openai_compatible',api_base_url='https://api.example.test/v1',model='remote:test',split_enabled=True,local_model='local:test',max_concurrency=3)
        semantic.save(self.settings)

    def test_alternates_and_same_task_keeps_route_across_retry(self):
        self.configure()
        with app.db() as c:
            lanes=[]
            for i in range(6):
                lease=routing.reserve(c,self.settings,'task-'+str(i));lanes.append(lease.row['lane']);lease.release()
            self.assertEqual(lanes,['local','api']*3)
            lease=routing.reserve(c,self.settings,'task-0');self.assertEqual(lease.row['lane'],'local');lease.release()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM semantic_routes').fetchone()[0],6)
            lease=routing.reserve(c,self.settings,'task-6');self.assertEqual(lease.row['lane'],'local');lease.release()

    def test_busy_local_does_not_reassign_or_advance(self):
        self.configure()
        with app.db() as c:
            first=routing.reserve(c,self.settings,'first')
            try:
                second=routing.reserve(c,self.settings,'second');self.assertEqual(second.row['lane'],'api');second.release()
                with self.assertRaises(semantic.ModelBusy):routing.reserve(c,self.settings,'third')
                self.assertEqual(c.execute('SELECT COUNT(*) FROM semantic_routes').fetchone()[0],2)
            finally:first.release()
            third=routing.reserve(c,self.settings,'third');self.assertEqual(third.row['lane'],'local');third.release()

    def test_actual_queue_uses_both_engines_once_no_messages(self):
        self.configure();seen=[]
        class Adapter(SyntheticAdapter):
            def __init__(self,settings):seen.append((settings['backend'],settings['model']))
        for i in range(4):self.add('split-'+str(i),text=f'无畏契约找陪练，预算{100+i}元')
        for i in range(4):self.assertTrue(semantic_queue.run_one(adapter_factory=Adapter))
        self.assertEqual(seen,[('ollama','local:test'),('openai_compatible','remote:test')]*2)
        with app.db() as c:
            results=c.execute("SELECT engine,result_json,status FROM intent_results WHERE method='model' ORDER BY id").fetchall()
            self.assertEqual(len(results),4)
            for i,row in enumerate(results):
                self.assertEqual(row['status'],'completed');data=json.loads(row['result_json']);self.assertEqual(data['inference']['lane'],'api' if i%2 else 'local')
                self.assertEqual(row['engine'],data['inference']['engine']);self.assertTrue(store.compatible_engine(row['engine'],semantic.engine_for(self.settings)))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0],0)
        self.assertFalse(semantic_queue.run_one(adapter_factory=Adapter));self.assertEqual(len(seen),4)

    def test_previous_primary_results_remain_compatible(self):
        self.configure();primary=semantic.engine_for(dict(self.settings,split_enabled=False));combined=semantic.engine_for(self.settings)
        self.assertTrue(store.compatible_engine(primary,combined))
        self.assertFalse(store.compatible_engine(primary.replace('remote:test','remote:other'),combined))
        self.assertFalse(store.compatible_engine(combined,primary))

    def test_failed_local_releases_slot_and_retains_model(self):
        self.configure();self.add()
        class Failing(SyntheticAdapter):
            def predict(self,source):raise TimeoutError()
        self.assertTrue(semantic_queue.run_one(adapter_factory=Failing));self.assertFalse(routing.LOCAL_SLOT.locked())
        with app.db() as c:
            row=c.execute("SELECT result_json,status FROM intent_results WHERE method='model'").fetchone()
            self.assertEqual(row['status'],'failed');self.assertEqual(json.loads(row['result_json'])['inference']['lane'],'local')

    def test_copy_draft_is_idempotent_and_never_creates_message(self):
        self.configure()
        body=dict(lead_id=1,request_id='copy-request-001');source=dict(game='',text='想点个女陪',brand='Mimo电竞')
        with patch.object(semantic_copy,'lead_source',return_value=source),patch.object(semantic_copy,'predict',return_value='你好呀，想点个瓦陪陪吗？感兴趣可以看看我主页～') as predict:
            first=semantic_copy.generate(body);second=semantic_copy.generate(body)
            self.assertEqual(first['content'],second['content']);self.assertFalse(first['sent']);self.assertEqual(predict.call_count,1)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM semantic_copy_drafts').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0],0)

    def test_bad_copy_or_unknown_game_missing_name_is_rejected(self):
        for text in ['你好呀，想点个陪陪吗？看看我主页～','你好，免费包赢，来主页点瓦陪陪','你好，瓦陪陪99元，来主页看看','你好呀，加微信点瓦陪陪，看主页']:
            with self.assertRaises(ValueError):semantic_copy.validate({'content':text},'')
        self.assertIn('瓦',semantic_copy.validate({'content':'你好呀，想点个瓦陪陪吗？感兴趣看看我主页～'},''))

    def test_invalid_split_and_old_form_preservation(self):
        self.configure()
        old={k:v for k,v in self.settings.items() if k not in ('split_enabled','local_model','local_context_tokens','local_keep_alive_seconds')}
        semantic.save(old);saved,_=semantic.config();self.assertTrue(saved['split_enabled']);self.assertEqual(saved['local_model'],'local:test')
        for change in [dict(backend='ollama'),dict(local_model='x-cloud'),dict(local_model=''),dict(local_context_tokens=0)]:
            with self.assertRaises(ValueError):semantic.validate_config(dict(self.settings,**change))

if __name__=='__main__':unittest.main()
