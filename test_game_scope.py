"""Cross-channel recall and false-positive regression for the user's game aliases."""
import json
from pathlib import Path
import subprocess
import unittest

import clubops as app
import asset_verticality as assets
import group_monitor as groups
import semantic
from game_scope import GAME_PATTERN, game_quote, exclusion_reason
from intent_rules import companion_relevance
from test_semantic import prediction
from video_discovery import in_search_scope


class GameScopeTests(unittest.TestCase):
    def test_group_notice_negation_does_not_change_strict_message_scope(self):
        for notice in ('本群仅面向PC端无畏契约，手游手瓦玩家请勿加入，群内不交流手游相关内容，混入手游玩家直接移出。\n再次重申：手瓦、陪玩、代练三类人员勿扰',
                       '端游交流群；不接受手游玩家'):
            self.assertTrue(groups.match(dict(name='瓦搭子群2️⃣',notice=notice)))
            self.assertTrue(exclusion_reason('瓦搭子群',notice))
        for notice in ('手游和端游都玩','手游玩家欢迎加入','手游玩家请勿加入，亚服玩家集合',
                       '不是不接受手游玩家', 'PC端玩家也欢迎，手游群'):
            self.assertFalse(groups.match(dict(name='瓦搭子群',notice=notice)),notice)
        self.assertFalse(groups.match(dict(name='手瓦群',notice='手游玩家请勿加入')))
        self.assertFalse(groups.match(dict(name='瓦搭子群',description='端游手游都玩',notice='不交流手游相关内容')))

    def test_domestic_pc_scope_is_shared_and_unknown_region_is_not_invented(self):
        foreign=['亚服','港服','台服','日服','韩服','美服','欧服','国际服','新加坡服','APAC server','NA服']
        titles=['无畏契约 '+term+' 陪玩' for term in foreign]+['端瓦国服亚服都玩']
        for title in titles:
            self.assertTrue(exclusion_reason(title))
            self.assertFalse(in_search_scope({'video_title':title},'瓦'))
            self.assertFalse(groups.match({'name':title}))
            self.assertFalse(assets.classify(title,[])['matched'])
            self.assertNotEqual(app.classify('找陪玩',title)['category'],'buyer')
        code="console.log(JSON.stringify(JSON.parse(process.argv[1]).map(video_title=>require('./collector_parser.cjs').inSearchScope({video_title},'瓦'))))"
        result=subprocess.run(['node','-e',code,json.dumps(titles)],text=True,capture_output=True,check=True,timeout=10,
            cwd=Path(__file__).resolve().parent,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(json.loads(result.stdout),[False]*len(titles))
        for title in ('无畏契约国服陪玩','国服瓦搭子群','端瓦陪玩 额外服务'):
            self.assertFalse(exclusion_reason(title))
        self.assertEqual(app.classify('找个搭子','瓦搭子群')['facts']['region'],'')

    def test_browser_http_groups_and_assets_share_scope(self):
        positive=['瓦','【瓦】陪玩','#瓦 #陪玩','打瓦找队友','瓦搭子群','瓦友开黑','瓦群','瓦开黑',
                  '瓦排位陪练','瓦双排','瓦组队','国服瓦','端瓦','女瓦陪玩','男瓦',
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

    def test_mobile_and_mixed_scope_is_excluded_before_any_channel_intent(self):
        titles=['瓦手游陪玩','手瓦陪玩','无畏契约手游','無畏契約手遊','VALORANT Mobile',
                'VALORANTMobile','无畏契约：源能行动','源能行动 陪玩','端瓦手瓦都接','端游无畏契约 手机版']
        for title in titles:
            with self.subTest(title=title):
                self.assertTrue(exclusion_reason(title))
                self.assertFalse(in_search_scope({'video_title':title},'瓦'))
                self.assertFalse(groups.match(dict(name=title)))
                self.assertFalse(assets.classify(title,[],game_category=True)['matched'])
                self.assertNotEqual(app.classify('找陪玩，预算100元',title)['category'],'buyer')
        code="console.log(JSON.stringify(JSON.parse(process.argv[1]).map(video_title=>require('./collector_parser.cjs').inSearchScope({video_title},'瓦'))))"
        result=subprocess.run(['node','-e',code,json.dumps(titles)],text=True,capture_output=True,check=True,timeout=10,
            cwd=Path(__file__).resolve().parent,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(json.loads(result.stdout),[False]*len(titles))
        self.assertTrue(exclusion_reason('找陪玩','无畏契约','手游有吗'))
        self.assertFalse(exclusion_reason('找陪玩','无畏契约端游'))

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
