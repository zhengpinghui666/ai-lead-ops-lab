"""Isolated event/time/deduplication tests. No platform reads or messages."""
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import clubops as app
import daily_dashboard as dashboard
import server

DAY = '2026-09-13T00:00:00+08:00'
NOON = datetime.fromisoformat('2026-09-13T12:30:00+08:00')


class DailyDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(app, 'DATA_DIR', Path(self.temp.name)); self.patch.start()
        app.init()

    def tearDown(self):
        self.patch.stop(); self.temp.cleanup()

    def task(self, c):
        return c.execute("""INSERT INTO collection_tasks(request_id,kind,target,video_limit,comment_limit,
            interactive,status,created_at,updated_at) VALUES(lower(hex(randomblob(16))),'video','fixture',1,1,0,'completed',?,?)""",(DAY,DAY)).lastrowid

    def event(self,c,task,stamp,phase,submissions,**extra):
        c.execute("INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,'captcha_workflow',?,?)",
                  (task,json.dumps({'verification':dict(phase=phase,attempt_id='new-id-on-every-phase',submissions=submissions,**extra)}),stamp))

    def test_empty_is_no_attempt_and_calendar_days_end_today(self):
        value = dashboard.snapshot(reference=NOON)
        self.assertIsNone(value['captcha']['pass_rate'])
        self.assertEqual(value['labels'][-1], '2026-09-13')
        self.assertEqual(value['labels'][0], '2026-06-16')
        self.assertEqual(len(value['labels']),90)
        self.assertEqual(value['granularity'],'day')
        self.assertIsNone(value['conversions']['orders'])
        self.assertTrue(all(n == 0 for n in value['totals'].values()))

    def test_captcha_cumulative_counts_strict_evidence_and_midnight(self):
        with app.db() as c:
            passed=self.task(c);unknown=self.task(c);legacy=self.task(c);previous=self.task(c)
            self.event(c,passed,'2026-09-12T16:00:00+00:00','detected',0)
            self.event(c,passed,'2026-09-13T00:01:00+08:00','verifying',1)
            for _ in range(3):
                self.event(c,passed,'2026-09-13T00:02:00+08:00','accepted',1,platform_verdict='passed',verdict_source='visible_platform_result')
            self.event(c,unknown,'2026-09-13T02:00:00+08:00','needs_review',1)
            self.event(c,legacy,'2026-09-13T03:00:00+08:00','accepted',1)
            self.event(c,previous,'2026-09-12T23:59:00+08:00','needs_review',1)
        v=dashboard.snapshot(reference=NOON);r=v['captcha']
        self.assertEqual((r['encounters'],r['submitted'],r['passed'],r['unknown']), (3,3,1,2))
        self.assertEqual(r['pass_rate'],100.0)
        self.assertEqual(r['confirmed'],1)
        self.assertEqual(r['rate_status'],'partial')
        self.assertEqual(r['legacy_accepted'],1)
        self.assertEqual(r['all_time']['submitted'],4)
        self.assertEqual(v['series']['captcha_passed'][-1],1)
        self.assertEqual(v['series']['captcha_submitted'][-2:],[1,3])

    def test_unknown_only_is_not_zero_but_explicit_failure_is(self):
        with app.db() as c:
            unknown=self.task(c);failed=self.task(c)
            self.event(c,unknown,'2026-09-12T12:00:00+08:00','needs_review',1,adapter='douyin_same_shape_pair',platform_verdict='unknown')
            self.event(c,failed,'2026-09-13T01:00:00+08:00','needs_review',1,adapter='douyin_same_shape_pair',platform_verdict='failed')
        v=dashboard.snapshot(reference=NOON)
        self.assertEqual(v['series']['captcha_rate_same_shape'][-2:],[None,0.0])
        previous=v['captcha']['daily'][-2]['types'][1]
        self.assertEqual((previous['submitted'],previous['unknown'],previous['rate_status']),(1,1,'unconfirmed'))
        self.assertEqual(v['captcha']['rate_status'],'confirmed')
        self.assertEqual(v['captcha']['all_time']['pass_rate'],0.0)
        self.assertEqual(v['captcha']['all_time']['confirmed'],1)

    def test_after_midnight_no_attempt_keeps_historical_unknown(self):
        with app.db() as c:
            task=self.task(c)
            self.event(c,task,'2026-09-12T23:59:00+08:00','needs_review',1)
        v=dashboard.snapshot(reference=NOON)
        self.assertEqual(v['captcha']['rate_status'],'not_attempted')
        self.assertIsNone(v['captcha']['pass_rate'])
        self.assertEqual(v['captcha']['all_time']['unknown'],1)
        self.assertIsNone(v['captcha']['all_time']['pass_rate'])

    def test_first_observation_and_first_buyer_survive_repeated_reads_and_reanalysis(self):
        with app.db() as c:
            source=c.execute("INSERT INTO sources(name,kind) VALUES('fixture','browser')").lastrowid
            video=c.execute("INSERT INTO videos(source_id,external_id,title,url,created_at) VALUES(?,'1','fixture','https://example.test/1',?)",(source,DAY)).lastrowid
            person=c.execute("INSERT INTO people(source_id,external_id,nickname) VALUES(?,'12345','fixture')",(source,)).lastrowid
            for external,stamp in [('older','2026-09-12T23:59:00+08:00'),('today','2026-09-12T16:00:00+00:00')]:
                record=c.execute("""INSERT INTO comments(source_id,external_id,video_id,person_id,raw_text,discovered_at)
                    VALUES(?,?,?,?,'fixture',?)""",(source,external,video,person,stamp)).lastrowid
                for finish in [stamp,'2026-09-13T05:00:00+08:00']:
                    c.execute("""INSERT INTO intent_results(evidence_type,record_id,method,engine,request_id,input_hash,
                        input_json,status,result_json,started_at,finished_at) VALUES('comment',?,'model','fixture',?,'hash','{}','completed',?,?,?)""",
                        (record,finish,json.dumps(dict(category='buyer',game='无畏契约')),finish,finish))
                task=self.task(c)
                c.execute("""INSERT INTO collection_observations(task_id,kind,external_id,page_url,observed_at,payload_hash,comment_text)
                    VALUES(?,'comment',?,'https://example.test/1','2026-09-13T06:00:00+08:00','hash','fixture')""",(task,external))
        v=dashboard.snapshot(reference=NOON)
        self.assertEqual(v['totals']['comments'],1)
        self.assertEqual(v['totals']['modeled'],1)
        self.assertEqual(v['totals']['intent_users'],0,'Already a buyer yesterday, not newly identified today')
        self.assertEqual(v['series']['comments'][-2:], [1,1],'Separate days, not cumulative totals')
        self.assertEqual(v['series']['comments'][-1],1)
        self.assertEqual(v['series']['modeled'][-2:], [1,1])
        self.assertEqual(v['series']['intent_users'][-2:], [1,0])

    def test_range_boundary_quiet_days_and_today_do_not_include_future(self):
        start=NOON.replace(hour=0,minute=0)
        stamps=[start-timedelta(days=90),start-timedelta(days=89),start-timedelta(days=2),
                start-timedelta(days=2)+timedelta(hours=4),start,NOON+timedelta(minutes=1)]
        with app.db() as c:
            source=c.execute("INSERT INTO sources(name,kind) VALUES('fixture','browser')").lastrowid
            for i,stamp in enumerate(stamps):
                c.execute('INSERT INTO videos(source_id,external_id,title,url,created_at) VALUES(?,?,?,?,?)',
                          (source,str(i),'fixture','https://example.test/'+str(i),stamp.isoformat()))
        v=dashboard.snapshot(reference=NOON)
        self.assertEqual(v['series']['works'][0],1)
        self.assertEqual(v['series']['works'][-3:],[2,0,1])
        self.assertEqual(sum(v['series']['works']),4)
        self.assertEqual(v['totals']['works'],1,'Today KPI is not the 90-day total')
        self.assertTrue(all(len(values)==len(v['labels']) for values in v['series'].values()))

    def test_first_observation_dates_handle_repetition_urls_blank_and_future(self):
        with app.db() as c:
            for url,stamp,text in [
                ('https://example.test/a','2026-09-13T01:00:00+08:00','fixture'),
                ('https://example.test/a','2026-09-12T23:59:00+08:00','fixture'),
                ('https://example.test/b','2026-09-12T16:00:00+00:00','fixture'),
                ('https://example.test/b','2026-09-13T06:00:00+08:00','fixture'),
                ('https://example.test/blank','2026-09-13T06:00:00+08:00','  '),
                ('https://example.test/future','2026-09-14T00:00:00+08:00','fixture'),
                ('https://example.test/invalid','invalid','fixture')]:
                task=self.task(c)
                c.execute('''INSERT INTO collection_observations(task_id,kind,external_id,
                    page_url,observed_at,payload_hash,comment_text) VALUES(?,'comment','same-id',?,?,'hash',?)''',
                    (task,url,stamp,text))
            plan=' '.join(r['detail'] for r in c.execute('EXPLAIN QUERY PLAN '+dashboard.FIRST_COMMENTS))
            self.assertIn('idx_observation_first_daily',plan)
        v=dashboard.snapshot(reference=NOON)
        self.assertEqual(v['series']['comments'][-2:],[1,1])
        self.assertEqual(v['totals']['comments'],1)

    def test_daily_index_migration_backs_up_and_preserves_observations(self):
        with app.db() as c:
            task=self.task(c)
            c.execute('''INSERT INTO collection_observations(task_id,kind,external_id,page_url,
                observed_at,payload_hash,comment_text) VALUES(?,'comment','1','https://example.test/a',?,'hash','fixture')''',(task,DAY))
            before=[tuple(r) for r in c.execute('SELECT * FROM collection_observations')]
            c.execute('DROP INDEX idx_observation_first_daily')
        app.init()
        backups=list((app.DATA_DIR/'backups').glob('*before-daily-comment-index-*'))
        self.assertEqual(len(backups),1)
        with app.db() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM collection_observations')],before)
            self.assertIsNotNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_observation_first_daily'").fetchone())
        app.init()
        self.assertEqual(len(list((app.DATA_DIR/'backups').glob('*before-daily-comment-index-*'))),1)

    def test_home_does_not_build_full_state_or_collect_network_state(self):
        with patch('clubops.state',side_effect=AssertionError('full state')),patch('server.collection_state',side_effect=AssertionError('collector state')):
            v=server.workbench_state('live','overview')
        self.assertIn('dashboard',v)
        self.assertLess(len(json.dumps(v)),12000)
        self.assertEqual(v['comments'],[])


if __name__=='__main__':unittest.main()
