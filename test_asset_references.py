import json
import unittest
import test_asset_verticality as fixtures
import clubops as app
import asset_references as refs
import asset_verticality as assets
import discovery_tracking as discovery
import video_discovery


class ReferenceTests(unittest.TestCase):
    setUp=fixtures.RoutingTests.setUp
    comment=fixtures.RoutingTests.comment
    def create(self):
        self.comment('普通讨论','无畏契约 #寻陪启事 #瓦陪',video='123456')
        state=refs.propose(dict(asset_key='123456'));return state['rows'][0]

    def decision(self,row,status='approved',**extra):
        return dict(id=row['id'],revision=row['revision'],status=status,reason='游戏与寻陪文案组合明确，作为相关作品参考。',
            game_terms=['无畏契约'],service_terms=['寻陪启事','瓦陪'],focus_author=False,**extra)

    def test_reviewed_pair_promotes_and_withdrawal_demotes_without_requeue(self):
        row=self.create()
        with app.db() as c:self.assertFalse(assets.refresh(c,'work','123456')['matched'])
        state=refs.review(self.decision(row));row=state['rows'][0]
        with app.db() as c:
            profile=assets.refresh(c,'work','123456');self.assertTrue(profile['matched'])
            self.assertEqual(profile['reference_hits'][0]['id'],row['id'])
            self.assertFalse(assets.classify('寻陪启事',[],reference_rules=refs.approved(c))['matched'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM semantic_jobs').fetchone()[0],0)
        refs.review(self.decision(row,'pending'))
        with app.db() as c:
            self.assertFalse(assets.refresh(c,'work','123456')['matched'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM asset_reference_reviews').fetchone()[0],2)
            self.assertEqual(refs.discovery_assets(c),(set(),[]))

    def test_stale_review_and_terms_outside_evidence_rejected(self):
        row=self.create();body=self.decision(row);body['service_terms']=['凭空虚构']
        with self.assertRaisesRegex(ValueError,'参考词须来自'):refs.review(body)
        refs.review(self.decision(row))
        with self.assertRaisesRegex(ValueError,'样本已更新'):refs.review(self.decision(row))

    def test_author_requires_verified_identity_and_respects_pause(self):
        row=self.create();body=self.decision(row);body['focus_author']=True
        with self.assertRaisesRegex(ValueError,'尚未核实'):refs.review(body)
        with app.db() as c:
            c.execute('INSERT INTO discovery_authors(sec_uid,nickname,seed_video_id,enabled,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)',('verified_author_123','作者','123456',0,app.now(),app.now()))
            content=json.loads(c.execute('SELECT content FROM asset_references').fetchone()[0]);content['author_sec_uid']='verified_author_123'
            c.execute('UPDATE asset_references SET content=?',(json.dumps(content),))
        refs.review(body)
        with app.db() as c:
            authors=discovery.author_rows(c,discovery.DEFAULT);self.assertTrue(authors[0]['focused']);self.assertFalse(authors[0]['enabled'])
            self.assertFalse(assets.classify('无畏契约赛事',[],reference_rules=refs.approved(c))['matched'])

    def test_caption_tail_and_structured_tags_preserved(self):
        row=video_discovery.video_row(dict(aweme_id='123456',desc='日常'*200+' #无畏契约',text_extra=[dict(hashtag_name='陪玩')]))
        self.comment('普通讨论','标题截断',video='123456')
        with app.db() as c:
            refs.save_content(c,row);content=refs.work(c,'123456');self.assertIn('陪玩',content['tags'])
            self.assertTrue(assets.refresh(c,'work','123456')['matched'])


if __name__=='__main__':unittest.main()
