"""Audit regressions: provenance, duplicate reads, time boundaries and latency cohorts."""
import json
import sqlite3
import unittest

import freshness_audit as audit

SINCE='2026-09-13T00:00:00+08:00'
UNTIL='2026-09-13T01:00:00+08:00'
OBSERVED='2026-09-12T16:10:00+00:00'


class FreshnessAuditTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:')
        self.c.row_factory=sqlite3.Row
        self.c.executescript('''
        CREATE TABLE sources(id INTEGER,kind TEXT);
        CREATE TABLE collection_plans(id INTEGER,continuous INTEGER);
        CREATE TABLE collection_tasks(id INTEGER,request_id TEXT,status TEXT,created_at TEXT,finished_at TEXT,lookback_hours REAL);
        CREATE TABLE collection_observations(task_id INTEGER,kind TEXT,external_id TEXT,page_url TEXT,
            observed_at TEXT,filter_reason TEXT,ingest_disposition TEXT,published_at TEXT,comment_text TEXT);
        CREATE TABLE videos(id INTEGER,external_id TEXT,source_id INTEGER,enabled INTEGER);
        CREATE TABLE discovery_works(video_id TEXT,first_seen_at TEXT,last_checked_at TEXT,
            enabled INTEGER,relevant INTEGER,author_sec_uid TEXT);
        CREATE TABLE discovery_authors(sec_uid TEXT,enabled INTEGER);
        CREATE TABLE asset_verticality(kind TEXT,asset_key TEXT,result TEXT);
        CREATE TABLE comments(id INTEGER,source_id INTEGER,video_id INTEGER,external_id TEXT,discovered_at TEXT);
        CREATE TABLE semantic_jobs(id INTEGER,evidence_type TEXT,record_id INTEGER,status TEXT,
            created_at TEXT,started_at TEXT,finished_at TEXT);
        INSERT INTO sources VALUES(2,'browser'),(3,'import');
        INSERT INTO collection_plans VALUES(3,1),(4,0);
        INSERT INTO videos VALUES(1,'123456',2,1),(2,'789123',2,1),(3,'123456',3,1);
        INSERT INTO discovery_works VALUES('123456','2026-09-12T14:00:00Z',NULL,1,1,NULL);
        INSERT INTO asset_verticality VALUES('work','123456','{"matched":true}');
        ''')

    def tearDown(self):
        self.c.close()

    def task(self,tid,request=None):
        self.c.execute('INSERT INTO collection_tasks VALUES(?,?,?,?,?,?)',
            (tid,request or f'plan-3-run-{tid}','completed',OBSERVED,OBSERVED,1))

    def observe(self,tid,cid='a',video='123456',stamp=OBSERVED,published='2026-09-12T16:09:30Z',disposition='inserted',reason=''):
        self.c.execute('INSERT INTO collection_observations VALUES(?,?,?,?,?,?,?,?,?)',
            (tid,'comment',cid,'https://www.douyin.com/video/'+video,stamp,reason,disposition,published,'PRIVATE TEXT'))

    def comment(self,cid=1,external='a',video=1,source=2):
        self.c.execute('INSERT INTO comments VALUES(?,?,?,?,?)',(cid,source,video,external,OBSERVED))

    def run_audit(self):
        return audit.audit(self.c,SINCE,UNTIL)

    def test_first_insert_only_despite_repeated_reads_and_manual_imports(self):
        self.task(1);self.observe(1);self.comment()
        self.task(2);self.observe(2,stamp='2026-09-12T16:11:00Z',disposition='duplicate')
        self.task(3,'manual-recovery');self.observe(3,'manual');self.comment(2,'manual')
        self.task(4,'plan-4-run-1');self.observe(4,'finite');self.comment(3,'finite')
        self.task(5,'plan-3-run-5-spoof');self.observe(5,'spoof');self.comment(4,'spoof')
        r=self.run_audit()
        self.assertEqual(r['eligible_first_insertions'],1)
        self.assertEqual(r['observation_counts']['continuous'],2)
        self.assertEqual(r['observation_counts']['continuous_unique_comments'],1)
        self.assertEqual(r['excluded_insertions']['manual_or_other_plan_insertions'],3)
        self.assertNotIn('PRIVATE TEXT',json.dumps(r))

    def test_source_and_work_identity_cannot_cross_match(self):
        self.task(1);self.observe(1)
        self.comment(source=3,video=3)
        self.comment(2,video=2)
        r=self.run_audit()
        self.assertEqual(r['eligible_first_insertions'],0)
        self.assertEqual(r['excluded_insertions']['first_insertion_not_matched'],1)

    def test_cold_coverage_separate_from_prior_partial_or_filtered_reads(self):
        self.task(1);self.observe(1);self.comment()
        r=self.run_audit()
        self.assertEqual(r['samples'][0]['cohort'],'first_work_coverage')
        self.assertEqual(r['samples'][0]['work_wait_seconds'],7800)
        self.task(2)
        self.observe(2,'older',stamp='2026-09-12T15:59:00Z',disposition='',reason='filtered_old')
        r=self.run_audit()
        self.assertEqual(r['samples'][0]['cohort'],'previously_observed_work')
        self.assertIsNone(r['samples'][0]['work_wait_seconds'])
        self.assertEqual(r['samples_with_earlier_reads_after_publication'],0)
        self.task(3)
        self.observe(3,'another',stamp='2026-09-12T16:09:40Z',disposition='duplicate')
        r=self.run_audit()
        self.assertEqual(r['samples'][0]['earlier_read_batches_after_publication'],1)
        self.assertEqual(r['samples_with_earlier_reads_after_publication'],1)

    def test_invalid_future_old_and_legacy_do_not_improve_freshness(self):
        for i,(published,disposition) in enumerate([
            (None,'inserted'),('2026-09-12T16:11:00Z','inserted'),
            ('2026-09-12T14:59:00Z','inserted'),('2026-09-12T16:09:59Z','')],1):
            self.task(i);self.observe(i,str(i),published=published,disposition=disposition);self.comment(i,str(i))
        r=self.run_audit()
        self.assertEqual(r['eligible_first_insertions'],0)
        self.assertEqual(r['excluded_insertions']['missing_invalid_or_future_publication'],2)
        self.assertEqual(r['excluded_insertions']['outside_recorded_freshness_window'],1)
        self.assertIsNone(r['cohorts']['all']['collection_seconds']['within_60_seconds'])

    def test_timezones_queue_wait_and_first_attempt_failure(self):
        self.task(1);self.observe(1,published='2026-09-13T00:09:00+08:00');self.comment()
        self.c.execute("INSERT INTO semantic_jobs VALUES(1,'comment',1,'failed',?,?,?)",
            (OBSERVED,'2026-09-12T16:10:05Z','2026-09-12T16:10:25Z'))
        self.c.execute("INSERT INTO semantic_jobs VALUES(2,'comment',1,'completed',?,?,?)",
            ('2026-09-12T16:11:00Z','2026-09-12T16:11:01Z','2026-09-12T16:11:15Z'))
        r=self.run_audit();sample=r['samples'][0]
        self.assertEqual(sample['collection_seconds'],60)
        self.assertEqual(sample['queue_wait_seconds'],5)
        self.assertEqual(sample['first_model_job_id'],1)
        self.assertIsNone(sample['publish_to_model_seconds'])
        self.assertEqual(r['cohorts']['all']['collection_seconds']['within_60_seconds'],1)

    def test_first_complete_model_timing_and_after_cutoff_exclusion(self):
        self.task(1);self.observe(1);self.comment()
        self.c.execute("INSERT INTO semantic_jobs VALUES(1,'comment',1,'completed',?,?,?)",
            (OBSERVED,'2026-09-12T16:10:05Z','2026-09-12T16:10:25Z'))
        s=self.run_audit()['samples'][0]
        self.assertEqual((s['model_seconds'],s['collect_to_model_seconds'],s['publish_to_model_seconds']),(20,25,55))
        self.c.execute("UPDATE semantic_jobs SET finished_at='2026-09-12T17:00:01Z'")
        self.assertIsNone(self.run_audit()['samples'][0]['publish_to_model_seconds'])
        self.c.execute("UPDATE semantic_jobs SET started_at='2026-09-12T17:00:01Z',finished_at='2026-09-12T17:00:21Z'")
        self.assertIsNone(self.run_audit()['samples'][0]['queue_wait_seconds'])

    def test_ambiguous_source_fails_and_read_only_audit_has_no_writes(self):
        self.task(1);self.observe(1);self.comment()
        self.c.commit();before=self.c.total_changes
        self.c.execute('PRAGMA query_only=ON');self.run_audit()
        self.assertEqual(self.c.total_changes,before)
        self.c.execute('PRAGMA query_only=OFF')
        self.c.execute("INSERT INTO sources VALUES(4,'browser')")
        with self.assertRaisesRegex(ValueError,'来源不唯一'):self.run_audit()


if __name__=='__main__':
    unittest.main()
