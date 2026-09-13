"""Cross-channel recall and false-positive regression for the user's game aliases."""
import json
from pathlib import Path
import subprocess
import unittest

import clubops as app
import asset_verticality as assets
import group_monitor as groups
import semantic
from game_scope import GAME_PATTERN, game_quote
from intent_rules import companion_relevance
from test_semantic import prediction
from video_discovery import in_search_scope


class GameScopeTests(unittest.TestCase):
    def test_browser_http_groups_and_assets_share_scope(self):
        positive=['瓦','【瓦】陪玩','#瓦 #陪玩','打瓦找队友','瓦搭子群','瓦友开黑','瓦群','瓦开黑',
                  '瓦排位陪练','瓦双排','瓦组队','国服瓦','端瓦','瓦手游','手瓦陪玩','女瓦陪玩','男瓦',
                  '瓦区教学','抖瓦','無畏契約','VALORANT']
        negative=['瓦片批发','瓦工交流群','千瓦时电费','装修瓦房','日内瓦旅游','普通好友群','WAVA','']
        titles=positive+negative;expected=[True]*len(positive)+[False]*len(negative)
        for title,want in zip(titles,expected):
            with self.subTest(title=title):
                self.assertEqual(bool(GAME_PATTERN.search(title)),want)
                self.assertEqual(in_search_scope({'video_title':title},'瓦'),want)
                self.assertEqual(groups.match(dict(name=title,description='',notice='')),want)
                self.assertEqual(assets.classify(title,[])['game_confirmed'],want)
                self.assertEqual(app.classify('有没有一起玩的',title)['game']==app.TARGET_GAME,want)
        node="let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(s).map(video_title=>require('./collector_parser.cjs').inSearchScope({video_title},'瓦')))));"
        result=subprocess.run(['node','-e',node],input=json.dumps(titles),text=True,capture_output=True,
                              check=True,timeout=10,cwd=Path(__file__).resolve().parent,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(json.loads(result.stdout),expected)
        self.assertTrue(groups.match(dict(name='开黑交流群',description='一起打瓦',notice='')))

    def test_model_evidence_uses_actual_context_in_every_channel(self):
        for kind in ('comment','live','group'):
            for title,quote,valid in [('瓦搭子群','瓦搭子',True),('瓦搭子群','瓦',False),
                                      ('#瓦 #陪玩','瓦',True),('【瓦】陪练','瓦',True),
                                      ('瓦片批发','瓦',False),('瓦工交流群','瓦',False),
                                      ('瓦片与 #瓦','瓦',False),('瓦片与 #瓦','#瓦',True)]:
                with self.subTest(kind=kind,title=title,quote=quote):
                    source=dict(kind=kind,text='有没有一起玩的',title=title,parent='')
                    value=prediction(source,'uncertain','uncertain');value['game']=app.TARGET_GAME
                    value['evidence'].append(dict(field='game',source='title',text=quote))
                    self.assertEqual(game_quote(quote,title),valid)
                    if valid:self.assertEqual(semantic.validate_result(value,source)['category'],'uncertain')
                    else:
                        with self.assertRaises(ValueError):semantic.validate_result(value,source)

    def test_alias_is_neither_service_verticality_nor_buyer_evidence(self):
        for title in ('瓦搭子','打瓦日常','瓦开黑','瓦群','#瓦'):
            self.assertFalse(assets.classify(title,[])['matched'])
            self.assertFalse(companion_relevance('这局打得漂亮',title)['passed'])
            self.assertNotEqual(app.classify('这局打得漂亮',title)['category'],'buyer')
            self.assertNotEqual(app.classify('只找免费队友',title)['category'],'buyer')
            self.assertTrue(assets.classify(title+' 陪玩接单',[])['matched'])


if __name__=='__main__':unittest.main()
