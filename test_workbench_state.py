"""Page-scoped reads must not change historical classifications or counts."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clubops as app
import server


class WorkbenchStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.previous=app.DATA_DIR
        app.DATA_DIR=Path(self.tmp.name)
        app.init()

    def tearDown(self):
        app.DATA_DIR=self.previous
        self.tmp.cleanup()

    def test_monitor_does_not_build_unrelated_person_histories(self):
        with patch('clubops.state',side_effect=AssertionError('Full history queried')):
            value=server.workbench_state('live','monitor')
        self.assertEqual(value['view'],'monitor')
        self.assertIn('board',value['collector'])
        self.assertEqual(value['comments'],[])
        self.assertEqual(value['leads'],[])
        self.assertEqual(value['stats']['comments'],0)

    def test_other_light_pages_do_not_build_work_library(self):
        with patch('clubops.state',side_effect=AssertionError('Full history queried')), patch('monitor_board.build',side_effect=AssertionError('Unused work library queried')):
            for page in ('live','groups','settings'):
                value=server.workbench_state('live',page)
                self.assertNotIn('board',value['collector'])

    def test_legacy_full_state_and_lead_views_keep_source_evidence(self):
        app.seed_demo()
        full=server.workbench_state('demo')
        leads=server.workbench_state('demo','leads')
        # List reads already omit heavy histories. Their retained evidence and
        # classification must agree, and complete evidence remains in details.
        comment_keys=('id','person_id','raw_text','category','game','published_at','discovered_at')
        project=lambda rows,keys:[{k:r.get(k) for k in keys} for r in rows]
        self.assertEqual(project(full['comments'],comment_keys),project(leads['comments'],comment_keys))
        lead_keys=('id','person_id','category','game','stage')
        self.assertEqual(project(full['leads'],lead_keys),project(leads['leads'],lead_keys))
        self.assertEqual([r['latest']['raw_text'] for r in full['leads']],[r['latest']['raw_text'] for r in leads['leads']])
        for lead in full['leads']:
            detail=server.workbench_state('demo','leads','detail/'+str(lead['id']),lead_id=lead['id'])
            self.assertEqual(detail['leads'][0]['latest'],lead['latest'])
            original={r['id']:r for r in full['comments']}
            for comment in detail['comments']:
                self.assertEqual(comment,original[comment['id']])
        self.assertEqual(full['stats'],leads['stats'])
        overview=server.workbench_state('demo','overview')
        self.assertEqual(overview['stats'],full['stats'])
        self.assertLessEqual(len(overview['leads']),5)
        self.assertTrue(all(row['category']=='buyer' for row in overview['leads']))
        self.assertEqual(overview['comments'],[])

    def test_unknown_view_rejected(self):
        with self.assertRaises(ValueError):server.workbench_state('live','unrecognized')
