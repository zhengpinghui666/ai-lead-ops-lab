"""Isolated candidate-pool/collector tests. No platform or model requests."""
import io
from contextlib import closing
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import clubops as app
import collector as col
import candidate_pool as pool
import collector_http_worker as worker
import video_discovery

NOW='2026-09-11T14:00:00+00:00'
IDS=[str(7600000000000000100+i) for i in range(10)]
ROWS=[{'video_id':v,'video_title':'无畏契约 '+v,'video_url':'https://www.douyin.com/video/'+v} for v in IDS]


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='candidate-pool-')
        self.old=app.DATA_DIR;app.DATA_DIR=Path(self.temp.name);app.init()
        self.threads=patch('collector.threading.Thread.start');self.threads.start()
        self.clock=patch('clubops.now',return_value=NOW);self.now=self.clock.start()
        col.ACTIVE.clear();self.counter=0

    def tearDown(self):
        col.ACTIVE.clear();self.threads.stop();self.clock.stop()
        app.DATA_DIR=self.old;self.temp.cleanup()

    def task(self,**kw):
        self.counter+=1
        return col.start({'kind':'search','target':'无畏契约陪玩','video_limit':3,'comment_limit':10,
                          'interactive':False,'request_id':f'fixture-{self.counter}',**kw})['id']

    def row(self,c,tid):
        return c.execute('SELECT * FROM collection_tasks WHERE id=?',(tid,)).fetchone()

    def discovery(self,tid,rows=ROWS):
        with app.db() as c:
            task=self.row(c,tid);policy=pool.configuration(c,task)
            chosen=pool.select(rows,task['video_limit'],policy);pool.record(c,task,rows)
        col.checkpoint(tid,{'type':'targets','records':chosen})
        return chosen

    def finish(self,tid,status='completed',done=True):
        with app.db() as c:
            c.execute('UPDATE collection_checkpoints SET status=? WHERE task_id=?',('done' if done else 'partial',tid))
        col.update(tid,status=status,finished_at=app.now())
        with app.db() as c:pool.settle(c,self.row(c,tid))
        col.ACTIVE.pop(tid,None)

    def observe(self,tid,vid,cid,published=NOW):
        with app.db() as c:
            c.execute('''INSERT INTO collection_observations(task_id,kind,external_id,page_url,observed_at,payload_hash,published_at)
                VALUES(?,'comment',?,?,?,?,?)''',(tid,cid,'https://www.douyin.com/video/'+vid,app.now(),'synthetic',published))

    def test_repeat_search_rotates_instead_of_repeating_top_three(self):
        first=self.task();chosen=self.discovery(first);self.assertEqual([r['video_id'] for r in chosen],IDS[:3])
        self.finish(first);self.now.return_value='2026-09-11T14:00:30+00:00'
        second=self.task();chosen=self.discovery(second);self.assertEqual([r['video_id'] for r in chosen],IDS[3:6])
        self.finish(second)
        with app.db() as c:
            summary=pool.state(c)
            self.assertEqual((summary['candidate_count'],summary['selected_videos'],summary['completed_reads']),(10,6,6))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM comments').fetchone()[0],0)

    def test_first_seen_scope_and_duplicate_selection_are_durable(self):
        tid=self.task();chosen=self.discovery(tid);col.checkpoint(tid,{'type':'targets','records':chosen})
        with app.db() as c:
            self.assertEqual(c.execute('SELECT SUM(selections) FROM collection_candidates').fetchone()[0],3)
        self.finish(tid);app.init()
        self.now.return_value='2026-09-11T15:00:00+00:00'
        tid=self.task(target='无畏契约复盘');chosen=self.discovery(tid)
        self.assertEqual([r['video_id'] for r in chosen],IDS[:3])
        with app.db() as c:
            self.assertEqual(pool.state(c)['scope_count'],2)
            self.assertEqual(c.execute('SELECT MIN(first_seen_at) FROM collection_candidates').fetchone()[0],NOW)

    def test_new_recent_activity_differs_from_repeated_or_old_comments(self):
        tid=self.task();self.discovery(tid)
        self.observe(tid,IDS[0],'1000001');self.observe(tid,IDS[1],'1000002','2026-09-01T00:00:00+00:00')
        self.observe(tid,IDS[2],'1000003','2026-09-12T00:00:00+00:00')
        self.finish(tid)
        with app.db() as c:
            rows=c.execute('SELECT video_id,new_recent_comments,quiet_streak FROM collection_candidates WHERE selections>0 ORDER BY video_id').fetchall()
            self.assertEqual([tuple(r) for r in rows],[(IDS[0],1,0),(IDS[1],0,1),(IDS[2],0,1)])
            pool.settle(c,self.row(c,tid))
            self.assertEqual(pool.state(c)['completed_reads'],3)
        self.now.return_value='2026-09-11T14:01:00+00:00'
        tid=self.task();self.discovery(tid,ROWS[:3]);self.observe(tid,IDS[0],'1000001')
        self.finish(tid)
        with app.db() as c:
            row=c.execute('SELECT new_recent_comments,quiet_streak FROM collection_candidates WHERE video_id=?',(IDS[0],)).fetchone()
            self.assertEqual(tuple(row),(1,1))

    def test_partial_or_cancelled_reads_do_not_claim_quiet_completion(self):
        tid=self.task();self.discovery(tid);self.finish(tid,'cancelled',done=False)
        with app.db() as c:
            self.assertEqual(pool.state(c)['completed_reads'],0)
            self.assertEqual(c.execute('SELECT SUM(quiet_streak) FROM collection_candidates').fetchone()[0],0)

    def test_late_cancelled_discovery_does_not_write_candidates(self):
        tid=self.task();col.command(tid,'cancel')
        with app.db() as c:
            pool.record(c,self.row(c,tid),ROWS)
            self.assertEqual(pool.state(c)['candidate_count'],0)

    def test_invalid_batch_is_atomic_and_fixed_video_never_expands(self):
        tid=self.task()
        with app.db() as c:
            for rows in (ROWS*6,[ROWS[0],ROWS[0]],[ROWS[0],{'video_id':123456}],[] ):
                with self.assertRaises(ValueError):pool.record(c,self.row(c,tid),rows)
            self.assertEqual(pool.state(c)['candidate_count'],0)
        self.finish(tid)
        tid=self.task(kind='video',target=IDS[0])
        with app.db() as c:
            self.assertIsNone(pool.configuration(c,self.row(c,tid)))
            with self.assertRaises(ValueError):pool.record(c,self.row(c,tid),ROWS)

    def test_fair_slot_and_activity_priority_never_select_absent_video(self):
        policy={'version':pool.VERSION,'as_of_ms':100000,'round':2,'history':[
            {'video_id':IDS[0],'last_selected_ms':90000,'priority_ms':200000},
            {'video_id':IDS[1],'last_selected_ms':90000,'priority_ms':95000},
            {'video_id':IDS[9],'last_selected_ms':0,'priority_ms':0}]}
        chosen=pool.select(ROWS[:4],3,policy)
        self.assertEqual([r['video_id'] for r in chosen],[IDS[2],IDS[1],IDS[3]])
        self.assertEqual(pool.select([],3,policy),[])
        self.assertEqual(pool.select(ROWS[:4],1,policy)[0]['video_id'],IDS[1])
        policy['round']=3
        self.assertEqual(pool.select(ROWS[:4],1,policy)[0]['video_id'],IDS[2])

    def test_python_and_browser_protocol_select_identically(self):
        self.threads.stop()  # Windows subprocess pipes use local reader threads.
        rng=random.Random(41);cases=[]
        for i in range(60):
            rows=rng.sample(ROWS,rng.randint(1,10))
            policy={'version':pool.VERSION,'as_of_ms':100000,'round':i,'history':[
                {'video_id':vid,'last_selected_ms':rng.randrange(90000),'priority_ms':rng.randrange(150000)} for vid in rng.sample(IDS,7)]}
            cases.append({'rows':rows,'limit':i%5+1,'policy':policy})
        script="let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(s).map(c=>require('./candidate_select.cjs').select(c.rows,c.limit,c.policy).map(r=>r.video_id)))));"
        result=subprocess.run(['node','-e',script],input=json.dumps(cases),text=True,capture_output=True,
                              cwd=col.BASE,check=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        expected=[[r['video_id'] for r in pool.select(c['rows'],c['limit'],c['policy'])] for c in cases]
        self.assertEqual(json.loads(result.stdout),expected)

    def test_migration_backup_preserves_previous_database(self):
        with app.db() as c:
            c.execute('DROP TABLE collection_candidate_reads');c.execute('DROP TABLE collection_candidates')
            c.execute("INSERT INTO settings VALUES('migration-sentinel','123')")
        app.init()
        backups=list((app.DATA_DIR/'backups').glob('*before-candidate-pool-*'))
        self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertEqual(c.execute("SELECT value FROM settings WHERE key='migration-sentinel'").fetchone()[0],'123')
            self.assertNotIn('collection_candidates',{r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        with app.db() as c:self.assertEqual(pool.state(c)['candidate_count'],0)

    def test_resume_keeps_selected_targets_and_updates_only_their_feedback(self):
        tid=self.task();chosen=self.discovery(tid);self.finish(tid,'interrupted',done=False)
        child=col.resume(tid,'resume-candidates')['id']
        col.checkpoint(child,{'type':'targets','records':chosen})
        self.finish(child)
        with app.db() as c:
            self.assertEqual(pool.state(c)['selected_videos'],3)
            self.assertEqual(pool.state(c)['completed_reads'],3)
            self.assertEqual(c.execute('SELECT SUM(selections) FROM collection_candidates').fetchone()[0],6)
        self.assertEqual({r['video_id'] for r in col.state()['tasks'][0]['checkpoints']},set(IDS[:3]))

    def test_collector_ipc_saves_candidates_then_feedback_without_new_leads(self):
        tid=self.task();messages=[{'type':'candidates','records':ROWS},
            {'type':'targets','records':ROWS[:3]},
            *({'type':'checkpoint','video_id':r['video_id'],'status':'done'} for r in ROWS[:3]),
            {'type':'status','status':'completed','detail':'Synthetic empty comment pages'}]
        process=Mock(stdin=io.StringIO(),stdout=io.StringIO(''.join(json.dumps(m)+'\n' for m in messages)))
        process.poll.return_value=0;process.wait.return_value=0
        with patch('collector.subprocess.Popen',return_value=process):col.run(tid,col.ACTIVE[tid])
        with app.db() as c:
            self.assertEqual(pool.state(c)['completed_reads'],3)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM leads').fetchone()[0],0)
        self.assertEqual(col.state()['tasks'][0]['status'],'completed')

    def test_http_search_uses_returned_candidates_without_extra_comment_budget(self):
        self.threads.stop()  # The HTTP worker needs its real local executor threads.
        calls=[]
        class Client:
            def page(self,operation,**kw):
                calls.append((operation,kw))
                if operation=='search':return {'rows':ROWS,'skipped':0,'has_more':False,'cursor':0,'search_id':''}
                return {'rows':[],'skipped':0,'has_more':False,'cursor':0,'reply_targets':[],'skipped_reasons':{}}
        policy={'version':pool.VERSION,'as_of_ms':100000,'round':1,'history':[
            {'video_id':vid,'last_selected_ms':90000,'priority_ms':200000} for vid in IDS[:3]]}
        events=[]
        worker.collect({'kind':'search','target':'synthetic','video_limit':3,'comment_limit':10,'page_concurrency':1,
                        'candidate_policy':policy},events.append,threading.Event(),client=Client())
        self.assertEqual(events[-1]['status'],'completed')
        self.assertEqual([r['video_id'] for r in next(e['records'] for e in events if e['type']=='targets')],IDS[3:6])
        self.assertEqual(len(next(e['records'] for e in events if e['type']=='candidates')),10)
        self.assertEqual(len([op for op,kw in calls if op=='search']),1)
        self.assertEqual(len([op for op,kw in calls if op=='comments']),3)

    def test_author_rotation_audit_matches_targets_and_failure_does_not_reuse_pool(self):
        self.threads.stop()
        policy={'version':pool.VERSION,'as_of_ms':100000,'round':1,'history':[
            {'video_id':vid,'last_selected_ms':90000,'priority_ms':200000} for vid in IDS[7:]]}
        for failures in ([],[{'status':'rate_limited','operation':'author'}]):
            calls=[]
            class Client:
                def page(self,operation,**kw):
                    calls.append(operation)
                    return {'rows':[],'skipped':0,'has_more':False,'cursor':0,'reply_targets':[],'skipped_reasons':{}}
            result={'candidates':ROWS,'scope':'bounded_author_candidates','failures':failures}
            events=[]
            with patch('video_discovery.discover',return_value=result):
                worker.collect({'kind':'author','target':IDS[0],'video_limit':3,'comment_limit':10,'page_concurrency':1,
                                'candidate_policy':policy},events.append,threading.Event(),client=Client())
            if failures:
                self.assertEqual(events[-1]['status'],'rate_limited');self.assertEqual(calls,[])
                self.assertFalse(any(e['type'] in ('candidates','targets') for e in events))
            else:
                self.assertEqual(events[-1]['status'],'completed')
                selected={r['video_id'] for r in next(e['records'] for e in events if e['type']=='targets')}
                self.assertEqual(selected,set(IDS[4:7]))
                audit=next(e['snapshot']['responses'][0] for e in events if e.get('stage')=='author_discovery')
                self.assertEqual({r['video_id'] for r in audit['candidates'] if r['selected']},selected)


if __name__=='__main__':unittest.main()
