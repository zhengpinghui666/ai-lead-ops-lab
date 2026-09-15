import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import clubops as app
import collector as col
import collection_scheduler as scheduler
import discovery_tracking as discovery
import monitoring
import monitor_board

NOW='2026-09-12T02:00:00+00:00'
AUTHOR='MS4wLjABAAAA_SYNTHETIC_AUTHOR'
VID='7600000000000000100'


def work(n=0,related=True,author=AUTHOR):
    return dict(video_id=str(int(VID)+n),video_title='无畏契约陪玩合成作品' if related else '合成生活日常',
                author_sec_uid=author,author_nickname='合成作者',published_at='2026-09-12T01:59:00+00:00')


class DiscoveryTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.previous=app.DATA_DIR;app.DATA_DIR=Path(self.temp.name)
        app.init();self.clock=patch('clubops.now',return_value=NOW);self.clock.start()
        self.threads=patch('collector.threading.Thread.start');self.threads.start();col.ACTIVE.clear()
        self.base=col.start(dict(kind='video',target=VID,transport='http',request_id='discovery-baseline'))['id']
        self.source=col.state()['source_id']
        col.observe(self.base,self.source,dict(type='video',record=work()))
        col.observe(self.base,self.source,dict(type='comment',record=dict(video_id=VID,comment_id=str(int(VID)+500),text='合成普通评论')))
        col.update(self.base,status='completed',finished_at=NOW);col.ACTIVE.clear()

    def tearDown(self):
        col.ACTIVE.clear();self.threads.stop();self.clock.stop();app.DATA_DIR=self.previous;self.temp.cleanup()

    def save(self,**kw):return discovery.save({'enabled':True,**kw})
    def record(self,rows,source='author',task=None):
        with app.db() as c:discovery.record(c,rows,source=source,target=VID,task=task)
    def plan(self,**kw):return dict({'continuous':1,'kind':'search','transport':'local_browser','run_count':0,'video_limit':3},**kw)
    def choose(self,plan=None,instant=NOW):
        with app.db() as c:return discovery.choose(c,plan or self.plan(),instant)

    def activity(self,vid,published=None,*,observed=NOW,suffix='',text='合成近期评论',reason=''):
        with app.db() as c:
            c.execute('''INSERT INTO collection_observations
              (task_id,kind,external_id,page_url,observed_at,payload_hash,comment_text,published_at,filter_reason)
              VALUES(?,'comment',?,?,?,?,?,?,?)''',
              (self.base,'activity-'+vid+suffix,'https://www.douyin.com/video/'+vid,observed,'synthetic',text,published,reason))
            import work_cadence
            work_cadence.record(c,vid,'activity-'+vid+suffix,text,published,observed)

    def checkpoint_history(self,c,task_id,video_id,*,finished=NOW,status='done'):
        c.execute('''INSERT INTO collection_tasks
          (id,request_id,kind,target,video_limit,comment_limit,interactive,status,created_at,updated_at,finished_at)
          VALUES(?,?,'video',?,1,30,0,'completed',?,?,?)''',
          (task_id,'synthetic-archive-'+str(task_id),video_id,NOW,NOW,finished))
        c.execute('''INSERT INTO collection_checkpoints(task_id,video_id,video_title,video_url,status,updated_at)
          VALUES(?,?,'synthetic history',?,?,?)''',
          (task_id,video_id,'https://www.douyin.com/video/'+video_id,status,NOW))

    def test_new_work_inherits_latest_finished_done_task_by_id_only(self):
        vid=work(50)['video_id']
        older=discovery.future(NOW,-100)
        with app.db() as c:
            self.checkpoint_history(c,100,vid,finished=NOW)
            self.checkpoint_history(c,101,vid,finished=older)
            self.checkpoint_history(c,102,vid,finished=None)
            self.checkpoint_history(c,103,vid,status='pending')
            self.checkpoint_history(c,104,vid,status='unavailable')
            self.checkpoint_history(c,105,work(51)['video_id'])
            before=[tuple(r) for r in c.execute('SELECT * FROM collection_checkpoints ORDER BY task_id,video_id')]
            discovery.record(c,[work(50),work(52)],source='author',target=VID)
            self.assertEqual(c.execute('SELECT last_checked_at FROM discovery_works WHERE video_id=?',(vid,)).fetchone()[0],older)
            self.assertIsNone(c.execute('SELECT last_checked_at FROM discovery_works WHERE video_id=?',(work(52)['video_id'],)).fetchone()[0])
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM collection_checkpoints ORDER BY task_id,video_id')],before)
            # Rediscovery must not overwrite a newer check or a manual pause.
            c.execute('UPDATE discovery_works SET last_checked_at=?,enabled=0 WHERE video_id=?',(NOW,vid))
            discovery.record(c,[work(50)],source='author',target=VID)
            self.assertEqual(tuple(c.execute('SELECT last_checked_at,enabled FROM discovery_works WHERE video_id=?',(vid,)).fetchone()),(NOW,0))

    def test_brand_new_catalog_stays_bounded_with_large_archive_and_stale_statistics(self):
        with app.db() as c:
            # Production retained one-row planner estimates after thousands of
            # tasks accumulated. Replaying existing catalog rows misses this.
            c.execute('ANALYZE')
            for task_id in range(100,5100):
                self.checkpoint_history(c,task_id,work(10000+task_id)['video_id'])
            steps=0
            def budget():
                nonlocal steps
                steps+=1000
                return int(steps>250000)
            c.set_progress_handler(budget,1000)
            try:
                self.assertEqual(discovery.previous_completed_read(c,work(10100)['video_id']),NOW)
                discovery.record(c,[work(i) for i in range(50,80)],source='author',target=VID)
            finally:
                c.set_progress_handler(None,0)
            self.assertLessEqual(steps,250000)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM discovery_works WHERE last_checked_at IS NULL AND video_id BETWEEN ? AND ?',
                                      (work(50)['video_id'],work(79)['video_id'])).fetchone()[0],30)

    def test_checkpoint_index_upgrade_is_idempotent_and_preserves_history(self):
        with app.db() as c:
            self.checkpoint_history(c,100,work(50)['video_id'])
            c.execute('DROP INDEX idx_checkpoints_work_status_task')
            before=[tuple(r) for r in c.execute('SELECT * FROM collection_checkpoints ORDER BY task_id,video_id')]
        app.init();app.init()
        with app.db() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM collection_checkpoints ORDER BY task_id,video_id')],before)
            plan=' '.join(r[3] for r in c.execute("EXPLAIN QUERY PLAN SELECT task_id FROM collection_checkpoints WHERE video_id=? AND status='done' ORDER BY task_id DESC",(work(50)['video_id'],)))
            self.assertIn('SEARCH',plan)
            self.assertIn('idx_checkpoints_work_status_task',plan)

    def test_group_search_gets_a_turn_despite_unseen_general_keywords(self):
        self.save(keywords=['无畏契约合成新词'])
        with patch('uid_inbox_store._account',return_value='123456789'):
            with app.db() as c:
                c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('public_group_discovery',json.dumps(dict(enabled=True,account_uid='123456789'))))
                c.execute('INSERT OR REPLACE INTO discovery_queries VALUES(?,?,?)',('瓦搭子群','2026-09-11T00:00:00+00:00','2026-09-11T00:00:00+00:00'))
            _,result=self.choose(self.plan(run_count=3))
        self.assertEqual(result['kind'],'search')
        import group_discovery
        self.assertIn(result['key'],group_discovery.QUERIES)

    def test_private_checkpoint_retires_only_its_work_and_replaces_author_seed(self):
        self.save(); self.record([work(), work(1)])
        task = col.start(dict(kind='video',target=VID,transport='http',request_id='private-work'))['id']
        col.checkpoint(task, dict(type='targets', records=[work()]))
        col.checkpoint(task, dict(type='checkpoint',video_id=VID,status='unavailable',reason='author_secret',detail='作者隐私设置'))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?', (VID,)).fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?', (work(1)['video_id'],)).fetchone()[0], 1)
            self.assertEqual(discovery.author_rows(c, discovery.config(c))[0]['seed_video_id'], work(1)['video_id'])
        self.record([work()])
        with app.db() as c:
            self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?', (VID,)).fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_observations WHERE task_id=?', (self.base,)).fetchone()[0], 2)

    def test_alias_queries_are_scheduled_and_paused_work_stays_paused(self):
        aliases=['瓦','打瓦 陪玩','瓦搭子','瓦开黑','瓦陪练','瓦 点陪']
        result=self.save(keywords=aliases)
        self.assertEqual({r['keyword'] for r in result['queries']},set(aliases))
        row=work(2);row['video_title']='瓦搭子 陪玩接单'
        self.record([row],source='search')
        with app.db() as c:
            found=c.execute('SELECT relevant FROM discovery_works WHERE video_id=?',(row['video_id'],)).fetchone()
            self.assertEqual(found[0],1)
            c.execute('UPDATE discovery_works SET enabled=0 WHERE video_id=?',(row['video_id'],))
        self.record([row],source='search');self.save(keywords=aliases)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?',(row['video_id'],)).fetchone()[0],0)

    def test_unviewable_work_is_retired_without_losing_history_or_other_work(self):
        self.save();self.record([work(),work(1)])
        task=col.start(dict(kind='video',target=VID,transport='http',request_id='unviewable-work'))['id']
        col.checkpoint(task,dict(type='targets',records=[work()]))
        col.checkpoint(task,dict(type='checkpoint',video_id=VID,status='unavailable',reason='status_self_see',detail='平台提示权限或删除'))
        self.record([work()])
        with app.db() as c:
            self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?',(VID,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?',(work(1)['video_id'],)).fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_observations WHERE task_id=?',(self.base,)).fetchone()[0],2)

    def test_deleted_work_preserves_failure_and_is_not_reenabled_by_rediscovery(self):
        self.save();self.record([work(),work(1)])
        old=col.start(dict(kind='video',target=VID,transport='http',request_id='deleted-old'))['id']
        with app.db() as c:c.execute("UPDATE collection_tasks SET status='schema_changed',finished_at=? WHERE id=?",(app.now(),old))
        col.ACTIVE.clear()  # The synthetic worker thread is deliberately not started.
        task=col.start(dict(kind='video',target=VID,transport='http',request_id='deleted-recheck'))['id']
        col.checkpoint(task,dict(type='targets',records=[work()]))
        col.checkpoint(task,dict(type='checkpoint',video_id=VID,status='unavailable',reason='status_deleted',detail='平台明确返回作品已删除'))
        self.record([work()])
        with app.db() as c:
            self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?',(VID,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?',(work(1)['video_id'],)).fetchone()[0],1)
            self.assertEqual(c.execute('SELECT status FROM collection_tasks WHERE id=?',(old,)).fetchone()[0],'schema_changed')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_observations WHERE task_id=?',(self.base,)).fetchone()[0],2)

    def test_existing_author_samples_gain_scope_without_losing_pause_or_provenance(self):
        row=work(3,related=False);self.record([row])
        with app.db() as c:
            c.execute("UPDATE discovery_works SET title='瓦区 陪玩接单',enabled=0 WHERE video_id=?",(row['video_id'],))
            before=dict(c.execute('SELECT * FROM discovery_works WHERE video_id=?',(row['video_id'],)).fetchone())
            discovery.refresh_game_scope(c)
            after=dict(c.execute('SELECT * FROM discovery_works WHERE video_id=?',(row['video_id'],)).fetchone())
            self.assertEqual(after,{**before,'relevant':1})
            self.assertEqual(c.execute('SELECT enabled FROM videos WHERE external_id=?',(row['video_id'],)).fetchone()[0],0)

    def test_multiple_recent_comments_keep_priority_after_many_quiet_checks(self):
        self.save()
        for start in range(0,600,50):self.record([work(i) for i in range(start,start+50)])
        active=work(599)['video_id']
        with app.db() as c:c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=?,quiet_streak=5 WHERE video_id=?',
                                      (discovery.future(NOW,-300),discovery.future(NOW,-60),active))
        self.activity(active,discovery.future(NOW,-900))
        self.activity(active,discovery.future(NOW,-901),suffix='-second')
        self.activity(active,discovery.future(NOW,-902),suffix='-third')
        _,job=self.choose()
        self.assertIn(active,job['target'].splitlines())
        slot=next(r for r in job['work_selection']['slots'] if r['video_id']==active)
        self.assertEqual(slot['group'],'active')
        self.assertEqual(slot['latest_comment_at'],discovery.future(NOW,-900))
        self.assertEqual(len(job['work_selection']['slots']),3)
        self.assertTrue(any(r['group']!='active' for r in job['work_selection']['slots']))

    def test_single_recent_comment_cannot_reserve_priority_after_quiet_checks(self):
        self.save();self.record([work()])
        self.activity(VID,discovery.future(NOW,-900))
        with app.db() as c:
            c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=?,quiet_streak=2 WHERE video_id=?',
                      (discovery.future(NOW,-120),discovery.future(NOW,-60),VID))
            selected,_=discovery.select_work_targets(c,self.plan(),NOW,set(),set())
            self.assertFalse(selected,'A sparse quiet work respects its backed-off due time')
            c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=? WHERE video_id=?',
                      (discovery.future(NOW,-4000),discovery.future(NOW,-3000),VID))
            selected,audit=discovery.select_work_targets(c,self.plan(),NOW,set(),set())
            self.assertEqual(len(selected),1,'It stays monitored and returns when due')
            self.assertEqual(audit['slots'][0]['group'],'rotation')
            self.assertTrue(audit['slots'][0]['cadence']['quiet_priority_expired'])
            c.execute('UPDATE discovery_works SET quiet_streak=0 WHERE video_id=?',(VID,))
            selected,audit=discovery.select_work_targets(c,self.plan(),NOW,set(),set())
            self.assertEqual(audit['slots'][0]['group'],'active','A new unique recent comment resets the quiet streak in settle')

    def test_old_counts_and_invalid_times_do_not_prove_recent_activity(self):
        self.save();self.record([work(i) for i in range(6)])
        with app.db() as c:c.execute('UPDATE discovery_works SET last_checked_at=?,new_recent_comments=100,quiet_streak=0',
                                      (discovery.future(NOW,-120),))
        self.activity(VID,discovery.future(NOW,-3601))
        self.activity(work(1)['video_id'],None)
        self.activity(work(2)['video_id'],discovery.future(NOW,10))
        self.activity(work(3)['video_id'],discovery.future(NOW,-10),observed=discovery.future(NOW,10))
        self.activity(work(4)['video_id'],discovery.future(NOW,-10),observed=discovery.future(NOW,-20))
        self.activity(work(5)['video_id'],discovery.future(NOW,-10),text='  ')
        _,job=self.choose(self.plan(video_limit=5))
        self.assertTrue(all(r['group']=='rotation' for r in job['work_selection']['slots']))

    def test_quiet_history_waits_but_recent_relevance_and_reactivation_stay_fast(self):
        import work_cadence
        self.save();self.record([work(),work(1)])
        recent=work(1)['video_id']
        with app.db() as c:
            for vid,age in ((VID,700000),(recent,10000)):
                for i in range(60):
                    work_cadence.record(c,vid,'sample-'+str(i),'想点女陪' if i<20 else '普通内容',
                                        discovery.future(NOW,-age),NOW)
            c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=?,quiet_streak=5',
                      (discovery.future(NOW,-120),discovery.future(NOW,-90)))
            chosen,audit=discovery.select_work_targets(c,self.plan(),NOW,set(),set())
            self.assertEqual([r['video_id'] for r in chosen],[recent])
            later=discovery.future(NOW,3600)
            chosen,audit=discovery.select_work_targets(c,self.plan(),later,set(),set())
            self.assertIn(VID,[r['video_id'] for r in chosen],'Old work remains monitored when due')
            # A newly observed recent comment must promote even before settle resets quiet.
            work_cadence.record(c,VID,'new-sample','想点陪',discovery.future(NOW,-1),NOW)
            chosen,audit=discovery.select_work_targets(c,self.plan(),NOW,set(),set())
            self.assertIn(VID,[r['video_id'] for r in chosen])
            self.assertFalse(next(r for r in audit['slots'] if r['video_id']==VID)['cadence']['historical_quiet_backoff'])

    def test_repeated_read_does_not_extend_publication_activity_window(self):
        self.save();self.record([work()])
        published=discovery.future(NOW,-3500)
        self.activity(VID,published,observed=discovery.future(NOW,-3000))
        self.activity(VID,published,suffix='-read-again')
        self.assertEqual(self.choose()[1]['work_selection']['slots'][0]['group'],'active')
        later=discovery.future(NOW,101)
        self.activity(VID,published,observed=later,suffix='-expired-read')
        self.assertNotEqual(self.choose(instant=later)[1]['work_selection']['slots'][0]['group'],'active')

    def test_activity_window_respects_timezone_and_exact_boundary(self):
        self.save();self.record([work()])
        self.activity(VID,'2026-09-12T09:00:00+08:00')
        self.assertEqual(self.choose()[1]['work_selection']['slots'][0]['group'],'active')
        self.assertNotEqual(self.choose(instant=discovery.future(NOW,1))[1]['work_selection']['slots'][0]['group'],'active')

    def test_keyword_filtered_comment_is_activity_without_becoming_a_lead(self):
        self.save();self.record([work()])
        with app.db() as c:before=c.execute('SELECT COUNT(*) FROM comments').fetchone()[0]
        self.activity(VID,discovery.future(NOW,-30),reason='filtered_keyword')
        self.assertEqual(self.choose()[1]['work_selection']['slots'][0]['group'],'active')
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM comments').fetchone()[0],before)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM semantic_jobs').fetchone()[0],0)

    def test_quiet_check_backs_off_before_sparse_activity_expires(self):
        self.save();self.record([work()])
        with app.db() as c:c.execute('UPDATE discovery_works SET quiet_streak=4,new_recent_comments=7 WHERE video_id=?',(VID,))
        self.activity(VID,discovery.future(NOW,-3500))
        _,job=self.choose();self.finish_work_job(job,'still-active')
        with app.db() as c:row=c.execute('SELECT * FROM discovery_works WHERE video_id=?',(VID,)).fetchone()
        self.assertEqual(row['next_check_at'],discovery.future(NOW,60*32))
        self.assertEqual((row['quiet_streak'],row['new_recent_comments']),(5,7))
        later=discovery.future(NOW,60*32+1)
        _,job=self.choose(instant=later);self.finish_work_job(job,'expired',later)
        with app.db() as c:row=c.execute('SELECT * FROM discovery_works WHERE video_id=?',(VID,)).fetchone()
        self.assertEqual(row['next_check_at'],discovery.future(later,60*32))

    def test_activity_index_upgrade_backs_up_and_preserves_observations(self):
        self.save();self.record([work()]);self.activity(VID,discovery.future(NOW,-10))
        with app.db() as c:
            before=[tuple(r) for r in c.execute('SELECT * FROM collection_observations ORDER BY task_id,kind,external_id')]
            c.execute('DROP INDEX idx_observation_published_activity')
        app.init()
        backups=list((app.DATA_DIR/'backups').glob('*before-work-activity-*'))
        self.assertEqual(len(backups),1)
        app.init()
        self.assertEqual(len(list((app.DATA_DIR/'backups').glob('*before-work-activity-*'))),1)
        with app.db() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM collection_observations ORDER BY task_id,kind,external_id')],before)
            self.assertIsNotNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_observation_published_activity'").fetchone())

    def test_durable_unique_library_and_unbiased_author_ratio(self):
        self.save()
        rows=[work(i,related=i<8) for i in range(10)]
        self.record(rows[:8],'search');self.record(rows[:8],'search')
        a=discovery.state()['authors'][0]
        self.assertEqual(a['sampled'],0);self.assertIsNone(a['ratio']);self.assertFalse(a['focused'])
        self.record(rows);self.record(rows)
        a=discovery.state()['authors'][0]
        self.assertEqual((a['sampled'],a['related'],a['ratio'],a['focused']),(10,8,80,True))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM discovery_works').fetchone()[0],10)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],8)
        self.save(enabled=False)
        self.assertEqual(discovery.state()['counts']['related'],8,'Disabling discovery retains assets')

    def test_read_only_state_and_master_stop_prevent_dispatch(self):
        self.save(seed_videos=[VID]);self.record([work()])
        self.assertFalse(discovery.state()['enabled'])
        monitor=monitoring.save(dict(transport='http'))
        monitoring.command('start');self.assertTrue(discovery.state()['enabled'])
        task=scheduler.tick(NOW);self.assertIsNotNone(task)
        with app.db() as c:
            self.assertIsNotNone(discovery.worker_config(c,task))
            self.assertEqual(c.execute('SELECT last_task_id FROM collection_plans WHERE id=?',(monitor['id'],)).fetchone()[0],task)
        monitoring.command('stop')
        self.assertTrue(col.ACTIVE[task]['cancel']);self.assertFalse(discovery.state()['enabled'])
        self.assertIsNone(scheduler.tick(NOW));self.assertEqual(len(col.ACTIVE),1)

    def test_rotation_keywords_authors_and_comments(self):
        self.save();self.record([work(i) for i in range(10)])
        for turn,expected in [(0,'work'),(1,'author'),(2,'work'),(3,'search')]:
            self.assertEqual(self.choose(self.plan(run_count=turn))[1]['channel'],expected)
        with app.db() as c:cfg=discovery.config(c)
        selected=[]
        for i in range(len(cfg['keywords'])):
            _,job=self.choose(self.plan(run_count=3));selected.append(job['target'])
            with app.db() as c:c.execute('UPDATE discovery_queries SET last_checked_at=?,next_check_at=? WHERE keyword=?',
                (NOW,discovery.future(NOW,300),job['key']))
        self.assertEqual(len(set(selected)),len(cfg['keywords']))
        self.assertEqual(set(selected),set(cfg['keywords']))

    def test_initial_author_scan_and_focus_frequency(self):
        self.save();self.record([work(i) for i in range(10)])
        _,job=self.choose(self.plan(run_count=1));self.assertEqual(job['author_pages'],3)
        task=col.start(dict(kind='author',target=VID,transport='http',request_id='author-scan'),discovery_job=job)['id']
        col.update(task,status='completed',finished_at=NOW);col.ACTIVE.clear()
        with app.db() as c:
            discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
            discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
        author=discovery.state()['authors'][0]
        self.assertEqual(author['next_check_at'],discovery.future(NOW,90))
        _,next_job=self.choose(self.plan(run_count=1),discovery.future(NOW,91))
        self.assertEqual(next_job['author_pages'],1)
        with app.db() as c:self.assertEqual(discovery.worker_config(c,task)['author_pages'],3,'Existing batch budget is frozen')

    def test_focus_new_works_prioritized_and_author_pause_preserves_library(self):
        self.save();self.record([work(i,author='MS4wLjABAAAA_NORMAL_AUTHOR') for i in range(2)])
        self.record([work(i+10) for i in range(10)])
        _,job=self.choose()
        self.assertEqual(sum(int(v)>=int(VID)+10 for v in job['target'].splitlines()),2,'Focus retains most exploration slots; ordinary assets still receive a turn')
        discovery.author_command(dict(sec_uid=AUTHOR,enabled=False,priority='auto'))
        _,job=self.choose();self.assertTrue(all(int(v)<int(VID)+10 for v in job['target'].splitlines()))
        self.assertEqual(discovery.state()['counts']['related'],12)

    def finish_work_job(self,job,number,instant=NOW):
        ids=job['target'].splitlines()
        task=col.start(dict(kind='video',target=job['target'],transport='http',video_limit=len(ids),
                            request_id=f'fair-work-{number}'),discovery_job=job)['id']
        col.checkpoint(task,dict(type='targets',records=[dict(video_id=v,video_title='合成作品') for v in ids]))
        for vid in ids:col.checkpoint(task,dict(type='checkpoint',video_id=vid,status='done'))
        col.update(task,status='completed',finished_at=instant);col.ACTIVE.clear()
        with app.db() as c:discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
        return ids

    def test_active_work_is_not_starved_by_large_focused_backlog(self):
        self.save()
        for start in range(0,600,50):self.record([work(i) for i in range(start,start+50)])
        active=work(599)['video_id']
        self.activity(active,discovery.future(NOW,-120))
        with app.db() as c:c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=?,new_recent_comments=3 WHERE video_id=?',
                                      (discovery.future(NOW,-120),discovery.future(NOW,-60),active))
        _,job=self.choose()
        self.assertIn(active,job['target'].splitlines(),'Known fresh comments must receive a slot despite continuous new-work arrivals')
        self.assertEqual(len(set(job['target'].splitlines())),3)

    def test_quiet_work_gets_a_turn_during_continuous_focus_discovery(self):
        self.save();self.record([work(i) for i in range(20)])
        quiet=work(1000,author='MS4wLjABAAAA_NORMAL_AUTHOR')
        self.record([quiet])
        active=[work(i)['video_id'] for i in (18,19)]
        for vid in active:self.activity(vid,discovery.future(NOW,-120))
        with app.db() as c:c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=? WHERE video_id=?',
                                      (discovery.future(NOW,-3600),discovery.future(NOW,-1800),quiet['video_id']))
        selected=[]
        for turn in range(3):
            instant=discovery.future(NOW,turn*180)
            with app.db() as c:
                for vid in active:c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=?,new_recent_comments=3,quiet_streak=0 WHERE video_id=?',
                                              (discovery.future(instant,-120),discovery.future(instant,-60),vid))
            self.record([work(100+turn*3+i) for i in range(3)])
            _,job=self.choose(instant=instant)
            selected.extend(self.finish_work_job(job,turn,instant))
        self.assertIn(quiet['video_id'],selected,'Old work must get a bounded exploration turn even while hot and focused work remain due')
        self.assertTrue(set(active)<=set(selected))

    def test_single_work_budget_rotates_all_groups_and_survives_reload(self):
        self.save();self.record([work(i) for i in range(12)])
        quiet=work(1000,author='MS4wLjABAAAA_NORMAL_AUTHOR');self.record([quiet])
        active=work(11)['video_id'];groups=[]
        self.activity(active,discovery.future(NOW,-120))
        for turn in range(4):
            instant=discovery.future(NOW,turn*180)
            with app.db() as c:
                c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=?,new_recent_comments=2,quiet_streak=0 WHERE video_id=?',
                          (discovery.future(instant,-120),discovery.future(instant,-60),active))
                c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=?,quiet_streak=5 WHERE video_id=?',
                          (discovery.future(NOW,-3600),discovery.future(NOW,-1800),quiet['video_id']))
            _,job=self.choose(self.plan(video_limit=1),instant)
            self.assertEqual(len(job['target'].splitlines()),1)
            self.assertEqual(job['work_selection']['turn'],turn)
            groups.extend(r['group'] for r in job['work_selection']['slots'])
            self.finish_work_job(job,turn,instant)
        self.assertEqual(groups.count('active'),2)
        self.assertIn('focused_new',groups);self.assertIn('rotation',groups)

    def test_reserved_slots_fill_without_selecting_future_or_disabled_work(self):
        self.save();self.record([work(i) for i in range(12)])
        for i in range(12):self.activity(work(i)['video_id'],discovery.future(NOW,-120))
        with app.db() as c:
            c.execute('UPDATE discovery_works SET last_checked_at=?,new_recent_comments=2',(discovery.future(NOW,-120),))
            c.execute('UPDATE discovery_works SET next_check_at=? WHERE video_id=?',(discovery.future(NOW,60),VID))
            c.execute('UPDATE discovery_works SET enabled=0 WHERE video_id=?',(work(1)['video_id'],))
            c.execute('UPDATE videos SET enabled=0 WHERE external_id=?',(work(2)['video_id'],))
        _,job=self.choose(self.plan(video_limit=5))
        ids=job['target'].splitlines()
        self.assertEqual(len(set(ids)),5)
        self.assertFalse({VID,work(1)['video_id'],work(2)['video_id']}&set(ids))
        self.assertTrue(all(r['group']=='active' for r in job['work_selection']['slots']))

    def test_failed_work_retry_keeps_selection_and_turn_frozen(self):
        self.save();self.record([work(i) for i in range(10)])
        _,job=self.choose()
        task=col.start(dict(kind='video',target=job['target'],transport='http',request_id='retry-fair-work'),discovery_job=job)['id']
        col.update(task,status='network_error',finished_at=NOW);col.ACTIVE.clear()
        with app.db() as c:discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
        self.record([work(100+i) for i in range(10)])
        _,retry=self.choose(self.plan(run_count=3,last_task_id=task),discovery.future(NOW,300))
        self.assertEqual(retry,job)

    def test_paused_fixed_work_is_not_automatically_reenabled(self):
        scheduler.save(dict(kind='video',target=VID,transport='http'))
        self.save();self.record([work()])
        with app.db() as c:self.assertEqual(c.execute('SELECT enabled FROM discovery_works WHERE video_id=?',(VID,)).fetchone()[0],0)
        _,job=self.choose();self.assertNotEqual(job['channel'],'work')

    def test_cancelled_and_failed_batches_do_not_claim_completed_checks(self):
        self.save();self.record([work()])
        _,job=self.choose(self.plan(run_count=1))
        task=col.start(dict(kind='author',target=VID,transport='http',request_id='cancelled-scan'),discovery_job=job)['id']
        col.command(task,'cancel')
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            discovery.record(c,[work(8)],source='author',target=VID,task=row)
            self.assertIsNone(c.execute('SELECT 1 FROM discovery_works WHERE video_id=?',(work(8)['video_id'],)).fetchone())
        col.update(task,status='needs_verification',finished_at=NOW);col.ACTIVE.clear()
        with app.db() as c:discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
        self.assertIsNone(discovery.state()['authors'][0]['last_checked_at'])

    def test_asset_identity_conflict_and_invalid_config_rejected(self):
        self.save();self.record([work()])
        with self.assertRaises(ValueError):self.record([work(author='MS4wLjABAAAA_CONFLICT_AUTHOR')])
        for cfg in [dict(keywords=['陪玩']),dict(initial_author_pages=4),dict(focus_ratio=101),dict(enabled='true'),dict(seed_videos=['https://unrelated.test'])]:
            with self.assertRaises(ValueError):self.save(**cfg)

    def test_retry_preserves_failed_job_and_frozen_scope(self):
        self.save();self.record([work()])
        _,job=self.choose(self.plan(run_count=3))
        task=col.start(dict(kind=job['kind'],target=job['target'],transport=job['transport'],request_id='retry-job'),discovery_job=job)['id']
        col.update(task,status='needs_verification',finished_at=NOW);col.ACTIVE.clear()
        enabled,next_job=self.choose(self.plan(run_count=0,last_task_id=task),discovery.future(NOW,300))
        self.assertTrue(enabled);self.assertEqual(next_job,job,'Retry cannot rotate to another keyword or author')

    def empty_search(self, **snapshot_changes):
        from urllib.parse import quote
        self.save();self.record([work()])
        _,job=self.choose(self.plan(run_count=3))
        task=col.start(dict(kind=job['kind'],target=job['target'],transport=job['transport'],request_id='empty-search'),discovery_job=job)['id']
        col.update(task,status='no_data',finished_at=NOW);col.ACTIVE.clear()
        snapshot=dict(page_url='https://www.douyin.com/search/'+quote(job['target']),navigation_http_status=200,
            navigation_error='',responses=[],video_links=0,visible_text='搜索 综合 视频 用户')
        snapshot.update(snapshot_changes)
        with app.db() as c:
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                (task,'search-empty',json.dumps(snapshot),NOW))
        return task,job

    def test_empty_search_defers_only_discovery_and_preserves_failure(self):
        task,job=self.empty_search()
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertEqual(discovery.empty_search_wait(c,row),300)
            discovery.settle(c,row)
            query=c.execute('SELECT * FROM discovery_queries WHERE keyword=?',(job['target'],)).fetchone()
            self.assertIsNone(query['last_checked_at'])
            self.assertEqual(query['next_check_at'],discovery.future(NOW,300))
            self.assertEqual(c.execute('SELECT status FROM collection_tasks WHERE id=?',(task,)).fetchone()[0],'no_data')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_observations WHERE task_id=?',(task,)).fetchone()[0],0)
        _,next_job=self.choose(self.plan(run_count=3,last_task_id=task),discovery.future(NOW,30))
        self.assertEqual(next_job['channel'],'work')

    def test_empty_search_does_not_mask_verification_or_unrecognized_response(self):
        task,job=self.empty_search()
        with app.db() as c:
            original=c.execute('SELECT snapshot FROM collection_diagnostics WHERE task_id=?',(task,)).fetchone()[0]
            for changed in ({'visible_text':'请完成安全验证'},{'navigation_http_status':403},{'responses':[{'status':200}]},
                            {'page_url':'https://www.douyin.com/login/'},{'video_links':2}):
                with self.subTest(changed=changed):
                    c.execute('UPDATE collection_diagnostics SET snapshot=? WHERE task_id=?',(json.dumps({**json.loads(original),**changed}),task))
                    row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
                    self.assertEqual(discovery.empty_search_wait(c,row),0)
                    self.assertEqual(discovery.choose(c,self.plan(last_task_id=task),NOW)[1],job)
            c.execute('DELETE FROM collection_diagnostics WHERE task_id=?',(task,))
            self.assertEqual(discovery.empty_search_wait(c,row),0)

    def test_empty_search_monitor_settlement_keeps_comment_plan_running(self):
        monitor=monitoring.save(dict(transport='http'))
        monitoring.command('start')
        task,_=self.empty_search()
        with app.db() as c:
            c.execute('UPDATE collection_plans SET last_task_id=?,run_count=1 WHERE id=?',(task,monitor['id']))
        scheduler.tick(NOW)
        state=monitoring.state()
        self.assertTrue(state['enabled'])
        self.assertEqual(state['settled_task_id'],task)
        self.assertIn('已有作品评论继续轮询',state['detail'])

    def test_verified_empty_search_settles_without_stopping_comments_or_other_keywords(self):
        monitor=monitoring.save(dict(transport='http'))
        monitoring.command('start')
        shape=dict(version='search-response-shape-v1',data_type='array',data_count=0,aweme_list_type='null',
            item_list_type='undefined',status_code=0,has_more=0,cursor=16,nil_info_type='object',
            nil_type_kind='string',nil_reason_code='service_empty',body_gate='none')
        response=dict(kind='search',status=200,content_kind='json',body_bytes=5092,status_code=0,search_shape=shape)
        task,job=self.empty_search(responses=[response],visible_text='搜索结果为空')
        col.update(task,status='completed')
        with app.db() as c:
            c.execute("UPDATE collection_diagnostics SET stage='search-empty-valid' WHERE task_id=?",(task,))
            c.execute('UPDATE collection_plans SET last_task_id=?,run_count=1 WHERE id=?',(task,monitor['id']))
        scheduler.tick(NOW)
        self.assertTrue(monitoring.state()['enabled'])
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertEqual(discovery.empty_search_wait(c,row),300)
            query=c.execute('SELECT * FROM discovery_queries WHERE keyword=?',(job['target'],)).fetchone()
            self.assertEqual(query['last_checked_at'],NOW)
            self.assertEqual(query['next_check_at'],discovery.future(NOW,300))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_observations WHERE task_id=?',(task,)).fetchone()[0],0)
            other=next(k for k in discovery.config(c)['keywords'] if k!=job['target'])
            c.execute('UPDATE discovery_queries SET next_check_at=? WHERE keyword!=?',(discovery.future(NOW,600),other))
            next_job=discovery.choose(c,self.plan(run_count=3,last_task_id=task),discovery.future(NOW,30))[1]
            self.assertEqual(next_job['target'],other,'Only the empty keyword is delayed')
            original=c.execute('SELECT snapshot FROM collection_diagnostics WHERE task_id=?',(task,)).fetchone()[0]
            for change in ({'data_count':1},{'data_count':False},{'data_type':'null'},{'has_more':1},{'has_more':False},
                    {'cursor':-1},{'cursor':True},{'aweme_list_type':'object'},{'item_list_type':'array','item_list_count':1},
                    {'nil_reason_code':'verify_check'},{'nil_reason_code':'unknown_empty'},{'body_gate':'needs_verification'}):
                with self.subTest(change=change):
                    altered={**response,'search_shape':{**shape,**change}}
                    self.assertFalse(discovery.valid_empty_search_response(altered))
                    c.execute('UPDATE collection_diagnostics SET snapshot=? WHERE task_id=?',(json.dumps({**json.loads(original),'responses':[altered]}),task))
                    self.assertEqual(discovery.empty_search_wait(c,row),0)
            c.execute('UPDATE collection_diagnostics SET snapshot=? WHERE task_id=?',(original,task))
            c.execute("UPDATE collection_diagnostics SET stage='search-empty' WHERE task_id=?",(task,))
            self.assertEqual(discovery.empty_search_wait(c,row),0,'Completed task without the proven-empty stage is not empty-result evidence')

    def test_browser_outage_defers_search_and_continues_verified_http_work(self):
        task,job=self.empty_search(navigation_http_status=502)
        col.update(task,status='network_error')
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertEqual(discovery.empty_search_wait(c,row),300)
            discovery.settle(c,row)
            next_job=discovery.choose(c,self.plan(run_count=3,last_task_id=task),discovery.future(NOW,30))[1]
            self.assertEqual(next_job['channel'],'work')
            self.assertEqual(next_job['transport'],'http')
            self.assertEqual(c.execute('SELECT status FROM collection_tasks WHERE id=?',(task,)).fetchone()[0],'network_error')
            c.execute("UPDATE collection_tasks SET status='identity_failed' WHERE id=?",(self.base,))
            self.assertEqual(discovery.empty_search_wait(c,row),0)
            self.assertEqual(discovery.choose(c,self.plan(last_task_id=task),NOW)[1],job)

    def missing_browser_bodies(self):
        task,job=self.empty_search()
        col.update(task,status='partial',comments=2,finished_at=NOW)
        snapshots=[]
        with app.db() as c:
            c.execute('DELETE FROM collection_diagnostics WHERE task_id=?',(task,))
            for n in range(2):
                vid=work(n)['video_id'];url='https://www.douyin.com/video/'+vid
                c.execute('''INSERT INTO collection_checkpoints(task_id,video_id,video_title,video_url,status,updated_at)
                    VALUES(?,?,'synthetic',?,'partial',?)''',(task,vid,url,NOW))
                snapshot=dict(page_url=url,navigation_error='',navigation_http_status=200,
                    processing=dict(version='comment-quality-v1',recognized=True,invalid_records=0,skipped=0,non_text_skipped=0,parse_errors=1),
                    responses=[dict(kind='comment',status=200,content_kind='json',body_error='body_unavailable',body_failure_reason='resource_missing'),
                               dict(kind='comment',status=200,content_kind='json',status_code=0,invalid_records=0,comments_type='array',comments_count=1)])
                snapshots.append(snapshot)
                c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                          (task,'comment-read',json.dumps(snapshot),NOW))
        return task,job,snapshots

    def test_missing_browser_bodies_defer_only_search_and_preserve_partial_records(self):
        task,job,_=self.missing_browser_bodies()
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertIsNone(scheduler.transient_browser_body_wait(c,row),'Do not broaden the ordinary retry rule')
            self.assertEqual(discovery.empty_search_wait(c,row),300)
            discovery.settle(c,row)
            self.assertEqual(tuple(c.execute('SELECT status,comments FROM collection_tasks WHERE id=?',(task,)).fetchone()),('partial',2))
            self.assertEqual([r[0] for r in c.execute('SELECT status FROM collection_checkpoints WHERE task_id=?',(task,))],['partial','partial'])
            self.assertEqual(discovery.choose(c,self.plan(run_count=3,last_task_id=task),discovery.future(NOW,30))[1]['channel'],'work')
            self.assertEqual(c.execute('SELECT next_check_at FROM discovery_queries WHERE keyword=?',(job['target'],)).fetchone()[0],discovery.future(NOW,300))

    def test_browser_body_isolation_rejects_unknown_failures_or_unproven_http(self):
        task,job,snapshots=self.missing_browser_bodies()
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            for change in (dict(body_failure_reason='unknown'),dict(body_error='invalid_json'),dict(status=429),dict(status=403)):
                altered=json.loads(json.dumps(snapshots[0]));altered['responses'][0].update(change)
                c.execute('UPDATE collection_diagnostics SET snapshot=? WHERE task_id=? AND id=(SELECT MIN(id) FROM collection_diagnostics WHERE task_id=?)',(json.dumps(altered),task,task))
                self.assertEqual(discovery.empty_search_wait(c,row),0,change)
                self.assertEqual(discovery.choose(c,self.plan(last_task_id=task),NOW)[1],job)
            c.execute('UPDATE collection_diagnostics SET snapshot=? WHERE task_id=? AND id=(SELECT MIN(id) FROM collection_diagnostics WHERE task_id=?)',(json.dumps(snapshots[0]),task,task))
            for change in ("status='identity_failed'", "finished_at='2026-09-11T00:00:00+00:00'", 'comments=0,filtered_old=0,filtered_unknown=0,filtered_future=0,filtered_keyword=0,filtered_blocked=0'):
                c.execute('SAVEPOINT baseline_case')
                c.execute('UPDATE collection_tasks SET '+change+' WHERE id=?',(self.base,))
                self.assertEqual(discovery.empty_search_wait(c,row),0,change)
                c.execute('ROLLBACK TO baseline_case');c.execute('RELEASE baseline_case')

    def test_monitor_can_resume_after_verified_browser_body_isolation(self):
        monitor=monitoring.save(dict(transport='local_browser'))
        task,_,_=self.missing_browser_bodies()
        with app.db() as c:c.execute("UPDATE collection_plans SET status='attention',last_task_id=? WHERE id=?",(task,monitor['id']))
        self.assertTrue(monitoring.command('start')['enabled'])
        scheduler.tick(NOW)
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_plans WHERE id=?',(monitor['id'],)).fetchone()
            self.assertEqual(row['status'],'running')
            self.assertIn('正文丢失',row['detail'])
        monitoring.command('stop')
        scheduler.tick(discovery.future(NOW,301))
        with app.db() as c:self.assertEqual(c.execute('SELECT status FROM collection_plans WHERE id=?',(monitor['id'],)).fetchone()[0],'paused')

    def test_browser_outage_does_not_isolate_login_verification_or_unknown_errors(self):
        task,_=self.empty_search(navigation_http_status=502)
        col.update(task,status='network_error')
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            original=c.execute('SELECT snapshot FROM collection_diagnostics WHERE task_id=?',(task,)).fetchone()[0]
            for change in ({'navigation_http_status':403},{'navigation_http_status':429},{'navigation_http_status':None},
                           {'responses':[{'status':'needs_verification'}]}):
                c.execute('UPDATE collection_diagnostics SET snapshot=? WHERE task_id=?',(json.dumps({**json.loads(original),**change}),task))
                self.assertEqual(discovery.empty_search_wait(c,row),0)
            c.execute('UPDATE collection_diagnostics SET snapshot=? WHERE task_id=?',(original,task))
            c.execute("UPDATE collection_tasks SET finished_at='2026-09-12T00:00:00+00:00' WHERE id=?",(self.base,))
            self.assertEqual(discovery.empty_search_wait(c,row),0,'Stale HTTP health is not proof the other channel is usable')

    def test_empty_search_start_requires_independent_healthy_http_read(self):
        monitor=monitoring.save(dict(transport='local_browser'))
        task,_=self.empty_search()
        with app.db() as c:c.execute("UPDATE collection_plans SET status='attention',last_task_id=? WHERE id=?",(task,monitor['id']))
        self.assertTrue(monitoring.command('start')['enabled'])
        monitoring.command('stop')
        col.update(self.base,status='identity_failed')
        with self.assertRaises(ValueError):monitoring.command('start')

    def test_scoped_out_search_does_not_pause_existing_comment_monitor(self):
        monitor=monitoring.save(dict(transport='http'))
        monitoring.command('start')
        task,_=self.empty_search(video_links=3,responses=[dict(kind='search',status=200,content_kind='json',keys=['status_code','aweme_list','data'])])
        scope=dict(responses=[dict(policy='game-title-scope-v1',excluded_candidates=3,
            eligible_candidates=0,reason='no_game_evidence_in_title',all_douyin=False)])
        with app.db() as c:
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                (task,'search-scope',json.dumps(scope),NOW))
            c.execute('UPDATE collection_plans SET last_task_id=?,run_count=1 WHERE id=?',(task,monitor['id']))
        scheduler.tick(NOW)
        self.assertTrue(monitoring.state()['enabled'])
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone()
            self.assertEqual(row['status'],'no_data','Preserve the original observation outcome')
            self.assertEqual(discovery.empty_search_wait(c,row),300)
            self.assertEqual(discovery.choose(c,self.plan(last_task_id=task),discovery.future(NOW,30))[1]['channel'],'work')
            original=c.execute("SELECT snapshot FROM collection_diagnostics WHERE task_id=? AND stage='search-empty'",(task,)).fetchone()[0]
            for response in [dict(kind='search',status=403),dict(kind='search',status=200,content_kind='json',keys=['data'],status_code=8),
                    dict(kind='search',status=200,content_kind='json',keys=['data'],body_error='invalid_json')]:
                value={**json.loads(original),'responses':[response]}
                c.execute("UPDATE collection_diagnostics SET snapshot=? WHERE task_id=? AND stage='search-empty'",(json.dumps(value),task))
                self.assertEqual(discovery.empty_search_wait(c,row),0)
            c.execute("UPDATE collection_diagnostics SET snapshot=? WHERE task_id=? AND stage='search-empty'",(original,task))
            scope['responses'][0]['eligible_candidates']=1
            c.execute("UPDATE collection_diagnostics SET snapshot=? WHERE task_id=? AND stage='search-scope'",(json.dumps(scope),task))
            self.assertEqual(discovery.empty_search_wait(c,row),0)

    def test_board_query_count_does_not_grow_with_library(self):
        self.save()
        with app.db() as c:
            for start in range(0,1000,50):discovery.record(c,[work(i,author='') for i in range(start,start+50)],source='search',target='无畏契约')
        queries=[];original=app.db
        @contextmanager
        def traced(mode='live'):
            with original(mode) as connection:
                connection.set_trace_callback(queries.append);yield connection
        with patch('clubops.db',side_effect=traced):board=monitor_board.build([],[])
        self.assertEqual(len(board['rows']),1000)
        self.assertLess(len(queries),20,'Board SQL count must stay independent of library size')

    def test_vertical_work_gets_two_slots_despite_busy_ordinary_game_videos(self):
        self.save();self.record([work(i) for i in range(6)])
        ordinary=[]
        for i in range(100,120):
            row=work(i);row['video_title']='无畏契约合成游戏集锦'
            self.record([row]);ordinary.append(row['video_id'])
            self.activity(row['video_id'],discovery.future(NOW,-60))
        _,job=self.choose()
        slots=job['work_selection']['slots']
        self.assertEqual(len(slots),3)
        self.assertEqual(sum(s['vertical'] for s in slots),2)
        self.assertEqual(sum(s['video_id'] in ordinary for s in slots),1)

    def test_general_game_author_does_not_become_automatically_focused(self):
        self.save()
        for i in range(12):
            row=work(i);row['video_title']='无畏契约合成比赛集锦';self.record([row])
        author=discovery.state()['authors'][0]
        self.assertEqual((author['related'],author['sampled'],author['vertical']),(12,12,0))
        self.assertFalse(author['focused'])
        discovery.author_command(dict(sec_uid=AUTHOR,enabled=True,priority='focus'))
        self.assertTrue(discovery.state()['authors'][0]['focused'])

    def test_focused_author_has_reserved_turns_despite_older_normal_backlog(self):
        self.save();self.record([work(i) for i in range(10)])
        normal='MS4wLjABAAAA_NORMAL_AUTHOR'
        self.record([work(100,author=normal)])
        with app.db() as c:
            c.execute('UPDATE discovery_authors SET next_check_at=? WHERE sec_uid=?',(discovery.future(NOW,-7200),normal))
            c.execute('UPDATE discovery_authors SET next_check_at=? WHERE sec_uid=?',(discovery.future(NOW,-60),AUTHOR))
        chosen=[]
        for turn in range(3):
            instant=discovery.future(NOW,turn*180)
            _,job=self.choose(self.plan(run_count=1),instant)
            chosen.append(job['key'])
            task=col.start(dict(kind='author',target=job['target'],transport='http',request_id='focus-author-'+str(turn)),discovery_job=job)['id']
            col.update(task,status='completed',finished_at=instant);col.ACTIVE.clear()
            with app.db() as c:discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
        self.assertEqual(chosen,[AUTHOR,AUTHOR,normal])

    def test_newer_vertical_first_reads_precede_old_assets_but_old_get_rotation(self):
        self.save();self.record([work(i) for i in range(10)])
        newest=work(9)['video_id']
        with app.db() as c:
            c.execute("UPDATE discovery_works SET published_at='2026-01-01T00:00:00+00:00'")
            c.execute('UPDATE discovery_works SET published_at=? WHERE video_id=?',(NOW,newest))
        _,job=self.choose()
        self.assertIn(newest,job['target'].splitlines())
        self.finish_work_job(job,0,NOW)
        _,job=self.choose(instant=discovery.future(NOW,180))
        self.assertTrue(set(job['target'].splitlines())-{newest})

    def revisit_fixture(self):
        self.save()
        author='MS4wLjABAAAA_UNFOCUSED_BACKLOG'
        for start in range(0,300,50):self.record([work(i,author=author) for i in range(start,start+50)],source='search')
        checked=work(1000,author=author)['video_id'];active=work(1001,author=author)['video_id']
        ordinary={**work(1002,author=author),'video_title':'无畏契约普通游戏作品'}
        self.record([work(1000,author=author),work(1001,author=author),ordinary],source='search')
        with app.db() as c:
            c.execute('UPDATE discovery_works SET first_seen_at=?,next_check_at=?',
                      (discovery.future(NOW,-21600),discovery.future(NOW,-21600)))
            c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=? WHERE video_id IN (?,?)',
                      (discovery.future(NOW,-3600),discovery.future(NOW,-1800),checked,active))
        self.activity(active,discovery.future(NOW,-120))
        return checked,active,ordinary['video_id']

    def selection_at_round(self,c,turn,paused=()):
        class Round:
            def execute(inner,sql,params=()):
                if sql=="SELECT COUNT(*) FROM discovery_jobs WHERE kind='work' AND settled=1":
                    return c.execute('SELECT ?',(turn,))
                return c.execute(sql,params)
        return discovery.select_work_targets(Round(),self.plan(),NOW,set(),set(paused))

    def test_revisit_rounds_cannot_be_filled_only_by_older_unchecked_backlog(self):
        checked,active,ordinary=self.revisit_fixture()
        with app.db() as c:
            for turn in (2,5):
                rows,audit=self.selection_at_round(c,turn)
                self.assertEqual({r['video_id'] for r in rows},{checked,active,ordinary})
            # The third exploration round still gives the oldest unseen work a
            # turn; recent-first phases are also retained with the same budget.
            for turn in (0,1,8):
                rows,_=self.selection_at_round(c,turn)
                self.assertNotIn(checked,{r['video_id'] for r in rows})
                self.assertEqual(len(rows),3)
                self.assertIn(active,{r['video_id'] for r in rows})

    def test_revisit_priority_does_not_override_future_disabled_or_paused_work(self):
        checked,_,_=self.revisit_fixture()
        with app.db() as c:
            c.execute('UPDATE discovery_works SET next_check_at=? WHERE video_id=?',(discovery.future(NOW,60),checked))
            self.assertNotIn(checked,{r['video_id'] for r in self.selection_at_round(c,2)[0]})
            c.execute('UPDATE discovery_works SET next_check_at=?,enabled=0 WHERE video_id=?',(discovery.future(NOW,-60),checked))
            self.assertNotIn(checked,{r['video_id'] for r in self.selection_at_round(c,2)[0]})
            c.execute('UPDATE discovery_works SET enabled=1 WHERE video_id=?',(checked,))
            self.assertNotIn(checked,{r['video_id'] for r in self.selection_at_round(c,2,[checked])[0]})

    def test_single_slot_keeps_ordinary_monitoring_and_prefers_vertical(self):
        self.save();self.record([work(i) for i in range(12)])
        for i in range(100,112):
            row=work(i);row['video_title']='无畏契约合成普通集锦';self.record([row])
        vertical=[]
        for turn in range(6):
            instant=discovery.future(NOW,turn*180)
            _,job=self.choose(self.plan(video_limit=1),instant)
            self.assertEqual(len(job['work_selection']['slots']),1)
            vertical.append(job['work_selection']['slots'][0]['vertical'])
            self.finish_work_job(job,turn,instant)
        self.assertEqual(vertical,[True,True,False,True,True,False])

    def test_insufficient_vertical_targets_return_unused_slots(self):
        self.save();self.record([work()])
        for i in range(100,110):
            row=work(i);row['video_title']='无畏契约合成普通集锦';self.record([row])
        _,job=self.choose(self.plan(video_limit=5))
        ids=job['target'].splitlines()
        self.assertEqual(len(set(ids)),5)
        self.assertIn(VID,ids)

    def cadence_fixture(self):
        self.save();self.record([work(i) for i in range(10)])
        for i in range(2):
            for j in range(3):self.activity(work(i)['video_id'],discovery.future(NOW,-60-j),suffix='-'+str(j))
        ordinary=work(100);ordinary['video_title']='无畏契约合成普通集锦';self.record([ordinary])
        return {work(i)['video_id'] for i in range(2)}

    def test_due_vertical_activity_precedes_discovery_without_double_exploration(self):
        active=self.cadence_fixture()
        _,job=self.choose(self.plan(run_count=1))
        self.assertEqual(job['channel'],'work')
        self.assertTrue(active<=set(job['target'].splitlines()))
        self.assertEqual(len(job['target'].splitlines()),3)
        self.assertEqual(job['dispatch_selection']['reason'],'due_vertical_activity')
        self.assertEqual(sum(not s['vertical'] for s in job['work_selection']['slots']),1)

    def test_active_cadence_reserves_author_search_and_vertical_exploration(self):
        active=self.cadence_fixture();channels=[];active_counts=[]
        for turn in range(8):
            instant=discovery.future(NOW,turn*180)
            with app.db() as c:c.execute('UPDATE discovery_works SET next_check_at=NULL WHERE video_id=?',(work(100)['video_id'],))
            _,job=self.choose(self.plan(run_count=turn),instant)
            channels.append(job['channel'])
            if job['channel']=='work':
                active_counts.append(len(active&set(job['target'].splitlines())))
                self.finish_work_job(job,'cadence-'+str(turn),instant)
            else:
                task=col.start(dict(kind=job['kind'],target=job['target'],transport=job['transport'],
                    request_id='cadence-discovery-'+str(turn)),discovery_job=job)['id']
                col.update(task,status='completed',finished_at=instant);col.ACTIVE.clear()
                with app.db() as c:discovery.settle(c,c.execute('SELECT * FROM collection_tasks WHERE id=?',(task,)).fetchone())
        self.assertEqual(channels,['work','work','work','author','work','work','work','search'])
        self.assertEqual(active_counts,[2,2,1,2,2,1])

    def test_ordinary_or_not_due_activity_keeps_balanced_discovery(self):
        self.cadence_fixture()
        with app.db() as c:c.execute('UPDATE discovery_works SET next_check_at=? WHERE video_id IN (?,?)',
            (discovery.future(NOW,60),work(0)['video_id'],work(1)['video_id']))
        self.assertEqual(self.choose(self.plan(run_count=1))[1]['channel'],'author')
        self.activity(work(100)['video_id'],discovery.future(NOW,-30))
        self.assertEqual(self.choose(self.plan(run_count=1))[1]['channel'],'author')

    def test_active_cadence_does_not_replace_frozen_failed_discovery(self):
        self.save();self.record([work(i) for i in range(10)])
        _,job=self.choose(self.plan(run_count=3))
        task=col.start(dict(kind=job['kind'],target=job['target'],transport=job['transport'],
            request_id='cadence-failed-search'),discovery_job=job)['id']
        col.update(task,status='needs_verification',finished_at=NOW);col.ACTIVE.clear()
        self.activity(VID,discovery.future(NOW,-30))
        self.assertEqual(self.choose(self.plan(last_task_id=task))[1],job)

    def test_observed_ratio_sets_real_next_check_and_excludes_cold_work(self):
        self.save(work_interval=60)
        values=[work(i) for i in range(3)]
        for row in values[1:]:row['video_title']='无畏契约普通集锦'
        self.record(values)
        for i,hits in enumerate((30,2,1)):
            for n in range(100):
                self.activity(values[i]['video_id'],discovery.future(NOW,-7201),suffix=str(n),
                              text='点个陪玩' if n<hits else '合成普通聊天')
        _,job=self.choose()
        self.assertEqual(set(job['target'].splitlines()),{r['video_id'] for r in values})
        self.finish_work_job(job,'ratio-levels')
        with app.db() as c:
            next_checks={r['video_id']:r['next_check_at'] for r in c.execute('SELECT * FROM discovery_works')}
            for row,seconds in zip(values,(60,21600,2592000)):
                self.assertEqual(next_checks[row['video_id']],discovery.future(NOW,seconds))
            selected,_=discovery.select_work_targets(c,self.plan(),discovery.future(NOW,61),set(),set())
            self.assertEqual([r['video_id'] for r in selected],[VID])

    def test_high_weight_without_recent_activity_reserves_discovery(self):
        self.save(work_interval=60);self.record([work()])
        for n in range(40):
            self.activity(VID,discovery.future(NOW,-7201),suffix=str(n),
                          text='娱乐陪怎么点' if n<12 else '合成普通聊天')
        channels=[]
        for turn in range(4):
            instant=discovery.future(NOW,turn*61)
            _,job=self.choose(self.plan(run_count=1),instant)
            channels.append(job['channel'])
            if turn<3:
                self.assertEqual(job['dispatch_selection']['reason'],'due_high_weight_work')
                self.finish_work_job(job,'ratio-reservation-'+str(turn),instant)
        self.assertEqual(channels,['work','work','work','author'])

    def service_priority_fixture(self):
        self.save(work_interval=30)
        self.record([work(i) for i in range(20)],source='search')
        strong={work(18)['video_id'],work(19)['video_id']}
        with app.db() as c:
            c.execute('UPDATE discovery_works SET first_seen_at=?,next_check_at=?',
                      (discovery.future(NOW,-86400),discovery.future(NOW,-86400)))
            for vid in strong:
                c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=? WHERE video_id=?',
                          (discovery.future(NOW,-300),discovery.future(NOW,-210),vid))
        for vid in strong:
            for n in range(50):
                self.activity(vid,discovery.future(NOW,-7200),suffix=str(n),
                              text='点个陪玩' if n<6 else '合成普通评论')
        return strong

    def test_due_service_evidence_gets_capacity_despite_older_unchecked_backlog(self):
        strong=self.service_priority_fixture()
        with app.db() as c:
            for turn in (0,1,3,4):
                selected,_=self.selection_at_round(c,turn)
                self.assertTrue(strong<={r['video_id'] for r in selected})
            selected,_=self.selection_at_round(c,8)
            self.assertTrue({r['video_id'] for r in selected}-strong,'Periodic exploration remains available')

    def test_service_reservation_preserves_disabled_author_work_and_due_boundaries(self):
        strong=self.service_priority_fixture();one,two=sorted(strong)
        with app.db() as c:
            c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=? WHERE video_id=?',
                      (NOW,discovery.future(NOW,90),one))
            c.execute('UPDATE videos SET enabled=0 WHERE external_id=?',(two,))
            selected,_=self.selection_at_round(c,0)
            self.assertFalse(strong & {r['video_id'] for r in selected})
            c.execute('UPDATE videos SET enabled=1 WHERE external_id=?',(two,))
            selected,_=self.selection_at_round(c,0,[two])
            self.assertFalse(strong & {r['video_id'] for r in selected})
            c.execute('UPDATE discovery_authors SET enabled=0')
            self.assertEqual(self.selection_at_round(c,0)[0],[])

    def test_service_reservation_retains_ordinary_pool_and_observed_activity(self):
        strong=self.service_priority_fixture()
        ordinary={**work(40),'video_title':'无畏契约普通集锦'}
        self.record([ordinary],source='search')
        busy=work(17)['video_id']
        for n in range(3):self.activity(busy,discovery.future(NOW,-60-n),suffix=str(n))
        with app.db() as c:
            selected,_=self.selection_at_round(c,0)
            ids={r['video_id'] for r in selected}
            self.assertIn(ordinary['video_id'],ids);self.assertIn(busy,ids)
            self.assertTrue(ids & strong);self.assertEqual(len(ids),3)

    def test_signal_migration_has_backup_and_preserves_business_records(self):
        self.save();self.record([work()]);self.activity(VID,discovery.future(NOW,-60))
        with app.db() as c:
            before=[tuple(r) for r in c.execute('SELECT * FROM collection_observations')]
            c.execute('DROP TABLE work_comment_signals');c.execute('DROP TABLE work_cadence_migrations')
        app.init()
        backups=list((app.DATA_DIR/'backups').glob('*before-work-cadence-*'))
        self.assertEqual(len(backups),1)
        with app.db() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM collection_observations')],before)
            self.assertGreater(c.execute('SELECT COUNT(*) FROM work_comment_signals').fetchone()[0],0)
        app.init()
        self.assertEqual(len(list((app.DATA_DIR/'backups').glob('*before-work-cadence-*'))),1)


if __name__=='__main__':unittest.main()
