import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
import clubops as app
import video_metadata as metadata
import monitor_board


class VideoMetadataTests(unittest.TestCase):
    def test_unknown_zero_and_invalid_counts_are_distinct(self):
        result=metadata.extract({'statistics':{'digg_count':0,'comment_count':False,'share_count':-1,'collect_count':'99'}})
        self.assertEqual({k:result[k] for k in metadata.COUNTERS},dict(likes=0,comments=None,shares=None,favorites=None))
        self.assertFalse(metadata.stale(result))
        self.assertTrue(metadata.stale(None))
        self.assertTrue(metadata.stale({'updated_at':'yesterday'}))
        self.assertTrue(metadata.stale({'updated_at':(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()}))

    def test_snapshot_persists_without_changing_local_comment_totals(self):
        with tempfile.TemporaryDirectory() as folder:
            old=app.DATA_DIR
            app.DATA_DIR=Path(folder)
            try:
                app.init()
                app.ingest({'records':[dict(comment_id='fixture',video_id='760000000000000001',text='test')]})
                with app.db() as c:
                    vid=c.execute('SELECT id FROM videos').fetchone()[0]
                    current=metadata.extract({'statistics':{'digg_count':20,'comment_count':1000,'share_count':4,'collect_count':0}})
                    metadata.save(c,vid,current)
                    metadata.save(c,vid,{**current,'likes':1,'updated_at':'2020-01-01T00:00:00Z'})
                    self.assertEqual(metadata.project(c,vid)['likes'],20)
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM comments').fetchone()[0],1)
                board=monitor_board.build([],[])
                self.assertEqual(board['rows'][0]['archived_comments'],1)
                self.assertEqual(board['rows'][0]['metrics']['comments'],1000)
                app.init()
                with app.db() as c:self.assertEqual(metadata.project(c,vid)['favorites'],0)
            finally:app.DATA_DIR=old

    def test_new_table_migration_backs_up_without_rewriting_existing_videos(self):
        with tempfile.TemporaryDirectory() as folder:
            old=app.DATA_DIR
            app.DATA_DIR=Path(folder)
            try:
                app.init()
                app.ingest({'records':[dict(comment_id='fixture',video_id='fixture',text='test')]})
                with app.db() as c:
                    before=[tuple(r) for r in c.execute('SELECT * FROM videos')]
                    c.execute('DROP TABLE video_metadata')
                app.init()
                self.assertEqual(len(list((app.DATA_DIR/'backups').glob('*before-video-metadata-*'))),1)
                with app.db() as c:self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM videos')],before)
                app.init()
                self.assertEqual(len(list((app.DATA_DIR/'backups').glob('*before-video-metadata-*'))),1)
            finally:app.DATA_DIR=old
