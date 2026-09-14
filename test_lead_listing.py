import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import lead_listing as listing

GAME='无畏契约'
NOW=listing.stamp('2026-09-14T04:00:00Z')
def options(**values):
    return listing.arguments({k:[str(v)] for k,v in dict(lead_page=1,**values).items()})
def lead(i,category='buyer',game=GAME,when='2026-09-14T03:00:00Z',**extra):
    return dict(id=i,category=category,game=game,nickname='合成用户'+str(i),external_id=str(i),
        latest=dict(id=i,evidence_type='comment',raw_text='合成原文'+str(i),published_at=when,
            discovered_at='2026-09-14T03:30:00Z',analysis_method='human',facts={'service_type':'娱乐陪'}),**extra)
def state(rows):
    return dict(leads=rows,comments=[dict(person_id=x['id'],game=x['game'],**x['latest']) for x in rows],
        jobs=[{'private_detail':'jobs'}],messages=[{'private_detail':'messages'}],members=[],events=[],
        stats={'comments':len(rows)},connections={},list_compact=True)
def project(rows,**values):
    return listing.page(state(rows),options(**values),GAME,reference=NOW)

class LeadListingTests(unittest.TestCase):
    def test_filter_before_paging_and_no_missing_or_duplicated_people(self):
        rows=[lead(i,category='buyer' if i%2 else 'noise') for i in range(1,124)]
        expected=list(range(123,0,-2));seen=[]
        for number in range(1,4):
            args=options(lead_tab='buyer',lead_page_size=25);args['page']=number
            result=listing.page(state(rows),args,GAME,reference=NOW)
            self.assertEqual(result['lead_list']['total'],62)
            seen.extend(x['id'] for x in result['leads'])
        self.assertEqual(seen,expected)
        result=project(rows,lead_query='合成原文1')
        self.assertIn(1,[x['id'] for x in result['leads']])
        self.assertLess(result['lead_list']['total'],30)

    def test_boundary_clamp_empty_and_category_grouping(self):
        rows=[lead(1),lead(2,'seller'),lead(3,'recruit'),lead(4,'club'),lead(5,'uncertain')]
        self.assertEqual([r['id'] for r in project(rows,lead_tab='supply')['leads']],[3,2])
        self.assertEqual(project(rows,lead_tab='club')['leads'][0]['id'],4)
        args=options(lead_query='missing');args['page']=999
        result=listing.page(state(rows),args,GAME,reference=NOW)['lead_list']
        self.assertEqual([result[k] for k in ('page','pages','total','start','end')],[1,1,0,0,0])

    def test_game_service_and_case_insensitive_search(self):
        rows=[lead(1),lead(2,game=''),lead(3,game='三角洲行动')]
        rows[0]['nickname']='MiMo';rows[0]['latest']['facts']['service_type']='技术陪'
        self.assertEqual([x['id'] for x in project(rows)['leads']],[2,1])
        self.assertEqual(project(rows,lead_game='other')['leads'][0]['id'],3)
        self.assertEqual(project(rows,lead_game=GAME,lead_service='技术陪',lead_query='mimo')['leads'][0]['id'],1)
        self.assertEqual(project(rows,lead_query='3')['leads'],[])

    def test_publication_window_unknown_future_and_custom_beijing_end(self):
        rows=[lead(1,when='2026-09-13T16:00:00Z'),lead(2,when='2026-09-14T15:59:59Z'),
              lead(3,when='2026-09-14T16:00:00Z'),lead(4,when=None),lead(5,when='broken'),
              lead(6,when='2026-09-13T15:59:59Z'),lead(7,when='2026-09-14T03:00:00Z')]
        custom=project(rows,lead_published='custom',lead_from='2026-09-14',lead_until='2026-09-14')
        self.assertEqual([x['id'] for x in custom['leads']],[2,7,1])
        self.assertEqual([x['id'] for x in project(rows,lead_published='unknown')['leads']],[5,4])
        self.assertEqual([x['id'] for x in project(rows,lead_published='future')['leads']],[3,2])
        self.assertEqual([x['id'] for x in project(rows,lead_published='hour')['leads']],[7])
        self.assertEqual(project(rows)['lead_list']['publication_summary'],dict(day=3,week=3,unknown=2,future=2))

    def test_discovery_sort_and_complete_unlinked_records(self):
        rows=[lead(1,when=None),lead(2)];rows[0]['latest']['discovered_at']='2026-09-14T03:59:00Z'
        self.assertEqual([x['id'] for x in project(rows,lead_sort='discovered')['leads']],[1,2])
        original=state(rows);original['comments'][0]['person_id']=None
        original['comments'][0]['history']=['kept']
        result=listing.page(copy.deepcopy(original),options(),GAME,reference=NOW)
        self.assertEqual(result['comments'],[original['comments'][0]])
        self.assertEqual(result['stats'],original['stats']);self.assertEqual(result['lead_list']['comment_count'],2)
        self.assertEqual(result['jobs'],[]);self.assertEqual(result['messages'],[])

    def test_validation_and_ignored_inactive_date_range(self):
        for args in [dict(lead_page=['-1']),dict(lead_page=['1','2']),dict(lead_page_size=['101']),
            dict(lead_tab=['other']),dict(lead_query=['x'*301]),dict(lead_junk=['1']),
            dict(lead_published=['custom']),
            dict(lead_page=['1'],lead_published=['custom'],lead_from=['2026-02-30']),
            dict(lead_page=['1'],lead_published=['custom'],lead_from=['2026-09-15'],lead_until=['2026-09-14'])]:
            with self.subTest(args=args),self.assertRaises(ValueError):listing.arguments(args)
        self.assertEqual(options(lead_from='bad',lead_published='all')['from'],'')
        self.assertIsNone(listing.arguments({'view':['leads'],'lead_id':['1']}))

    def test_workbench_only_compacts_explicit_list_never_detail(self):
        import server
        original=state([lead(i) for i in range(1,80)])
        with patch.object(server.clubops,'state',side_effect=lambda *a,**kw:copy.deepcopy(original)),\
             patch.object(server,'collection_state',return_value={'last_received':None}),\
             patch.object(server.messaging_http,'state',return_value={}),\
             patch.object(server.uid_messaging,'state',return_value={}):
            result=server.workbench_state('live','leads',lead_list=options())
            self.assertEqual(len(result['leads']),30);self.assertEqual(result['lead_list']['total'],79)
            detail=server.workbench_state('live','leads','detail/1',lead_id=1)
            self.assertNotIn('lead_list',detail);self.assertEqual(detail['comments'],original['comments'])
            with self.assertRaises(ValueError):server.workbench_state('live','inbox',lead_list=options())
            with self.assertRaises(ValueError):server.workbench_state('live','leads',lead_id=1,lead_list=options())

    def test_http_boundary_duplicates_scope_and_normal_query(self):
        import server
        class QuietHandler(server.Handler):
            def log_message(self,*args):pass
        httpd=server.LocalHTTPServer(('127.0.0.1',0),QuietHandler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with patch.object(server,'workbench_state',return_value={'leads':[]}) as build:
                with opener.open(f'http://127.0.0.1:{httpd.server_port}/api/state?view=leads&lead_page=2&lead_tab=supply',timeout=3) as r:
                    self.assertEqual(r.status,200)
                self.assertEqual(build.call_args.args[-1]['page'],2)
                self.assertEqual(build.call_args.args[-1]['tab'],'supply')
                build.reset_mock()
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    opener.open(f'http://127.0.0.1:{httpd.server_port}/api/state?view=leads&lead_page=2&lead_page=3',timeout=3)
                self.assertEqual(caught.exception.code,400);caught.exception.close();build.assert_not_called()
        finally:httpd.shutdown();thread.join(timeout=3);httpd.server_close()

if __name__=='__main__':unittest.main()
