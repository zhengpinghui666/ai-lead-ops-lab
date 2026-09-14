import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import clubops as app
import asset_verticality as assets
import analysis_store as store
import semantic
import semantic_queue as queue
import live_monitor as live
import live_room_pool as pool
from test_semantic import SyntheticAdapter


class ClassificationTests(unittest.TestCase):
    def test_mobile_history_and_category_do_not_promote_a_service_asset(self):
        history=[dict(text=t) for t in ('手瓦找陪玩','无畏契约手游陪玩多少钱','手瓦有没有女陪')]
        self.assertFalse(assets.classify('无畏契约日常',history)['matched'])
        self.assertFalse(assets.classify('手游陪玩',[],game_category=True)['matched'])

    def test_title_requires_game_and_service_together(self):
        for title in ('无畏契约陪玩接单','VALORANT 陪练','无畏契约女陪'):
            self.assertTrue(assets.classify(title,[])['matched'],title)
        for title in ('无畏契约赛事预测','无畏契约练枪教学','陪玩接单','王者荣耀陪玩'):
            self.assertFalse(assets.classify(title,[])['matched'],title)

    def test_history_can_promote_game_asset_and_explains_threshold(self):
        history=[dict(id=str(i),text=t) for i,t in enumerate(('陪玩多少钱','我想找陪练','有没有女陪'))]
        result=assets.classify('无畏契约日常',history)
        self.assertTrue(result['matched']);self.assertEqual(result['history_hits'],3)
        self.assertEqual(len(result['evidence']),3)
        self.assertFalse(assets.classify('无畏契约日常',history[:2])['matched'])
        history.extend(dict(id=str(i+100),text='比赛比分 '+str(i)) for i in range(28))
        self.assertFalse(assets.classify('无畏契约日常',history)['matched'])

    def test_duplicates_and_game_without_service_do_not_promote(self):
        self.assertFalse(assets.classify('无畏契约',[dict(id=str(i),text='陪玩多少钱') for i in range(50)])['matched'])
        self.assertFalse(assets.classify('无畏契约',[dict(id=str(i),text='一起打游戏 '+str(i)) for i in range(50)])['matched'])

    def test_verified_live_category_supplies_game_context(self):
        self.assertTrue(assets.classify('技术陪玩接单',[],game_category=True)['matched'])
        self.assertFalse(assets.classify('技术陪玩接单',[])['matched'])


class RoutingTests(unittest.TestCase):
    def test_foreign_region_in_text_or_parent_blocks_model_despite_domestic_source(self):
        rid,result=self.comment('亚服找陪玩','无畏契约国服陪玩')
        self.assertEqual(result['model_queue']['queued'],0)
        with app.db() as c:
            self.assertFalse(assets.routing(c,'comment',rid)['model_allowed'])
            c.execute("UPDATE comments SET raw_text='亚服有吗',external_id='parent'")
        child,_=self.comment('多少钱','无畏契约国服陪玩',key='child')
        with app.db() as c:
            c.execute("UPDATE comments SET parent_external_id='parent' WHERE id=?",(child,))
            self.assertFalse(assets.routing(c,'comment',child)['model_allowed'])

    def test_scope_migration_is_idempotent_and_preserves_raw_and_analysis_history(self):
        import game_scope
        self.comment('找陪玩','手瓦陪玩')
        self.room('无畏契约手游陪玩')
        with app.db() as c:
            original=[tuple(r) for r in c.execute('SELECT * FROM comments')]
            results=[tuple(r) for r in c.execute('SELECT * FROM intent_results')]
            game_scope.enforce_saved_scope(c);game_scope.enforce_saved_scope(c)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM comments')],original)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM intent_results')],results)
            self.assertEqual(c.execute('SELECT enabled FROM videos').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT enabled FROM live_rooms').fetchone()[0],0)

    def test_mobile_comment_in_pc_video_and_pc_comment_in_mobile_video_do_not_queue(self):
        for index,(text,title) in enumerate([('手瓦找陪玩','无畏契约陪玩'),('陪玩多少钱','无畏契约手游陪玩'),
                                           ('端瓦找陪玩','无畏契约手游陪玩')]):
            rid,result=self.comment(text,title,key='scope'+str(index),video='scope'+str(index))
            self.assertEqual(result['model_queue']['queued'],0)
            with app.db() as c:
                route=assets.routing(c,'comment',rid)
                self.assertFalse(route['model_allowed']);self.assertIn('手游',route['reason'])

    def test_mobile_live_message_never_queues_and_old_asset_cache_cannot_override_scope(self):
        sid=self.room();rid=self.message(sid,'手瓦陪玩多少钱')
        with app.db() as c:
            self.assertFalse(assets.routing(c,'live',rid)['model_allowed'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM semantic_jobs').fetchone()[0],0)
        # An already queued PC message is rechecked against the current room title.
        other=self.message(sid,'陪玩多少钱',2)
        with app.db() as c:
            c.execute("UPDATE live_rooms SET title='无畏契约手游陪玩'")
            self.assertFalse(assets.routing(c,'live',other)['model_allowed'])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        old,app.DATA_DIR=app.DATA_DIR,Path(self.temp.name);self.addCleanup(setattr,app,'DATA_DIR',old)
        app.init();queue.STOP.clear()
        self.settings=dict(semantic.DEFAULTS,enabled=True,auto_analyze=True,model='synthetic:1',live_model_enabled=True)
        semantic.save(self.settings)

    def comment(self,text,title='无畏契约陪玩',key='c1',video='v1'):
        app.ingest({'records':[dict(comment_id=key,video_id=video,video_title=title,text=text)]})
        result=app.analyze()
        with app.db() as c:record_id=c.execute('SELECT id FROM comments WHERE external_id=?',(key,)).fetchone()[0]
        return record_id,result

    def room(self,title='无畏契约陪玩'):
        url='https://live.douyin.com/12345'
        with app.db() as c:
            pool.ingest(c,[dict(room_url=url,title=title)],'valorant_category',app.now())
            cfg=dict(live.DEFAULTS,room_url=url)
            return c.execute('INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                ('routing-room',url,'10000000000000001',json.dumps(cfg),'running','',app.now(),app.now())).lastrowid

    def message(self,sid,text,n=1):
        live.receive(sid,dict(type='message',record=dict(room_id='10000000000000001',message_id=str(10000000000000100+n),text=text,nickname='合成用户')))
        with app.db() as c:return c.execute('SELECT MAX(id) FROM live_messages').fetchone()[0]

    def test_vertical_comment_without_keywords_is_queued_directly(self):
        record_id,result=self.comment('今天这局怎么样')
        self.assertEqual(result['model_queue']['queued'],1)
        with app.db() as c:
            route=assets.routing(c,'comment',record_id)
            self.assertTrue(route['model_allowed']);self.assertIsNone(route['keyword_match'])
        with patch.object(SyntheticAdapter,'predict',wraps=SyntheticAdapter(self.settings).predict) as predict:
            self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter))
            predict.assert_called_once()
        with app.db() as c:self.assertEqual(c.execute('SELECT status FROM semantic_jobs').fetchone()[0],'completed')

    def test_nonvertical_comment_with_demand_and_game_context_queues_model(self):
        record_id,result=self.comment('无畏契约找陪玩','无畏契约赛事')
        self.assertEqual(result['model_queue']['queued'],1)
        with app.db() as c:
            route=assets.routing(c,'comment',record_id)
            self.assertTrue(route['model_allowed']);self.assertTrue(route['keyword_match']['passed'])
            self.assertFalse(route['asset']['matched'])

    def test_ordinary_game_invitation_enters_model_without_rule_buyer(self):
        record_id,result=self.comment('现在有打的吗？','瓦搭子日常')
        self.assertEqual(result['model_queue']['queued'],1)
        with app.db() as c:
            self.assertNotEqual(c.execute('SELECT category FROM comments WHERE id=?',(record_id,)).fetchone()[0],'buyer')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0],0)

    def test_nonvertical_fallback_still_requires_message_and_game_evidence(self):
        for n,(text,title) in enumerate([('今天这局真好看','无畏契约赛事'),('找陪玩','日常生活'),
                                       ('皮肤多少钱','无畏契约赛事'),('找陪玩','手瓦日常'),
                                       ('港服求带','无畏契约赛事')]):
            record_id,result=self.comment(text,title,key='blocked'+str(n),video='blocked'+str(n))
            self.assertEqual(result['model_queue']['queued'],0,(text,title))
            with app.db() as c:self.assertFalse(assets.routing(c,'comment',record_id)['model_allowed'])

    def test_nonvertical_question_uses_its_own_parent_context(self):
        self.comment('陪玩服务','无畏契约赛事',key='parent')
        record_id,_=self.comment('多少钱','无畏契约赛事',key='child')
        with app.db() as c:
            self.assertFalse(assets.routing(c,'comment',record_id)['model_allowed'])
            c.execute("UPDATE comments SET parent_external_id='parent' WHERE id=?",(record_id,))
            self.assertTrue(assets.routing(c,'comment',record_id)['model_allowed'])
            c.execute("UPDATE comments SET raw_text='这款鼠标' WHERE external_id='parent'")
            self.assertFalse(assets.routing(c,'comment',record_id)['model_allowed'])

    def test_vertical_live_requires_keywords_even_when_model_enabled(self):
        sid=self.room();plain=self.message(sid,'这波操作漂亮',1);matched=self.message(sid,'陪玩多少钱',2)
        with app.db() as c:
            self.assertFalse(assets.routing(c,'live',plain)['model_allowed'])
            self.assertTrue(assets.routing(c,'live',matched)['model_allowed'])
            self.assertEqual([r[0] for r in c.execute('SELECT record_id FROM semantic_jobs')],[matched])
            source,_=store.inputs(c,'live',matched);self.assertEqual(source['title'],'无畏契约陪玩')

    def test_nonvertical_live_demand_in_confirmed_game_context_queues(self):
        sid=self.room('无畏契约赛事');record_id=self.message(sid,'无畏契约找陪玩')
        with app.db() as c:
            self.assertTrue(assets.routing(c,'live',record_id)['model_allowed'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM semantic_jobs').fetchone()[0],1)

    def test_demotion_before_dispatch_cancels_model_admission(self):
        record_id,_=self.comment('无畏契约找陪玩')
        with app.db() as c:c.execute("UPDATE videos SET title='无畏契约赛事'")
        with patch.object(SyntheticAdapter,'predict') as predict:
            self.assertTrue(queue.run_one(adapter_factory=SyntheticAdapter));predict.assert_not_called()
        with app.db() as c:self.assertIn(c.execute('SELECT status FROM semantic_jobs').fetchone()[0],('stale','skipped'))

    def test_historical_backfill_is_local_and_does_not_requeue(self):
        self.comment('普通聊天','无畏契约赛事')
        with app.db() as c:
            c.execute('DELETE FROM asset_verticality')
            c.execute("UPDATE videos SET title='无畏契约陪玩'")
        with patch.object(SyntheticAdapter,'predict') as predict:app.init();predict.assert_not_called()
        with app.db() as c:
            self.assertTrue(assets.profiles(c,'work')['v1']['matched'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM semantic_jobs').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM comments').fetchone()[0],1)

if __name__=='__main__':unittest.main()
