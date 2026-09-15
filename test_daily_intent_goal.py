import json,tempfile,unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
import clubops as app
import analysis_store
import daily_intent_goal as goal

START=datetime.fromisoformat('2026-09-15T00:00:00+08:00')
END=datetime.fromisoformat('2026-09-15T12:00:00+08:00')
AT='2026-09-15T01:00:00+08:00'

class DailyIntentGoalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.p=patch.object(app,'DATA_DIR',Path(self.temp.name));self.p.start();app.init()
        with app.db() as c:
            self.source=c.execute("INSERT INTO sources(name,kind) VALUES('fixture','browser')").lastrowid
            self.video=c.execute("INSERT INTO videos(source_id,external_id,title,created_at) VALUES(?,'fixture','无畏契约陪玩',?)",(self.source,AT)).lastrowid
            self.group=c.execute("INSERT INTO monitored_groups(account_uid,conversation_id,conversation_short_id,name,description,notice,member,participants,matched,checked_at) VALUES('11111','fixture','12345','瓦群','','',1,20,1,?)",(AT,)).lastrowid
            self.live=c.execute("INSERT INTO live_sessions(request_id,room_url,room_id,config,status,detail,started_at,updated_at) VALUES('fixture','https://live.douyin.com/12345','12345','{}','completed','',?,?)",(AT,AT)).lastrowid
    def tearDown(self):self.p.stop();self.temp.cleanup()
    def record(self,c,uid,kind='comment',text='无畏契约想点女陪，预算100元',published=AT,finish=AT,game='无畏契约'):
        external=str(c.execute('SELECT COUNT(*) FROM intent_results').fetchone()[0]+1)
        person=c.execute('INSERT OR IGNORE INTO people(source_id,external_id,nickname) VALUES(?,?,?)',(self.source,uid,'fixture')).lastrowid
        person=c.execute('SELECT id FROM people WHERE source_id=? AND external_id=?',(self.source,uid)).fetchone()[0]
        if kind=='comment':
            rid=c.execute("INSERT INTO comments(source_id,external_id,video_id,person_id,raw_text,published_at,discovered_at,analysis_method,category,game) VALUES(?,?,?,?,?,?,?,'model','buyer',?)",(self.source,external,self.video,person,text,published,AT,game)).lastrowid
        elif kind=='group':
            rid=c.execute("INSERT INTO group_messages(group_id,message_id,uid,person_id,raw_text,group_title,message_index,published_at,observed_at,filter_reason,relevance,category,game,reason,analysis_method,facts) VALUES(?,?,?,?,?,'瓦群',?,?,?,'','{}','buyer',?,'','model','{}')",(self.group,external,uid,person,text,external,published,AT,game)).lastrowid
        else:
            rid=c.execute("INSERT INTO live_messages(session_id,room_id,message_id,uid,nickname,raw_text,published_at,observed_at,filter_reason,category,analysis_method,reason,facts,include_matches,exclude_matches,payload_hash,game) VALUES(?,'12345',?,?,'fixture',?,?,?,'','buyer','model','','{}','[]','[]','fixture',?)",(self.live,external,uid,text,published,AT,game)).lastrowid
        self.result(c,kind,rid,finish=finish,game=game)
        return rid
    def result(self,c,kind,rid,category='buyer',finish=AT,game='无畏契约'):
        source,_=analysis_store.inputs(c,kind,rid)
        return c.execute("INSERT INTO intent_results(evidence_type,record_id,method,engine,request_id,input_hash,input_json,status,result_json,started_at,finished_at) VALUES(?,?,'model','fixture',lower(hex(randomblob(8))),?,?,'completed',?,?,?)",(kind,rid,analysis_store.digest(source),json.dumps(source),json.dumps(dict(category=category,game=game,facts={},reason='fixture')),finish,finish)).lastrowid
    def value(self,c,**kwargs):return goal.summary(c,START,END,model_engine='fixture',**kwargs)
    def test_goal_and_midday_pace_are_not_actual_predictions(self):
        with app.db() as c:v=self.value(c)
        self.assertEqual((v['target'],v['achieved'],v['remaining'],v['expected_by_now'],v['status']),(100,0,100,50,'behind'))
    def test_cross_channel_duplicates_and_reanalysis_count_one_user(self):
        with app.db() as c:
            for kind in ('comment','live','group'):
                rid=self.record(c,'12345',kind);self.result(c,kind,rid,finish='2026-09-15T02:00:00+08:00')
            self.record(c,'23456','group')
            v=self.value(c)
        self.assertEqual(v['achieved'],2);self.assertEqual(sum(v['source_counts'].values()),2)
    def test_old_false_buyers_teammates_suppliers_and_unknown_games_do_not_fill_goal(self):
        with app.db() as c:
            for uid,text in [('1','有没有一起玩的啊'),('2','黄金白银有人打吗'),('3','求老板点我'),('4','招女陪')]:self.record(c,uid,text=text)
            self.record(c,'5',game='');self.record(c,'6',game='三角洲行动')
            self.record(c,'7',text='无畏契约手游想点个陪陪')
            self.assertEqual(self.value(c)['achieved'],0)
    def test_retracted_human_and_latest_model_judgment_are_respected(self):
        with app.db() as c:
            a=self.record(c,'1');b=self.record(c,'2','live');d=self.record(c,'3','group')
            c.execute("UPDATE comments SET category='noise',analysis_method='human' WHERE id=?",(a,))
            c.execute("INSERT INTO live_judgments VALUES(?,'social','{}','fixture',1,?)",(b,AT))
            self.result(c,'group',d,category='social',finish='2026-09-15T02:00:00+08:00')
            self.assertEqual(self.value(c)['achieved'],0)
    def test_day_boundary_future_and_first_qualified_user_are_not_recounted(self):
        with app.db() as c:
            self.record(c,'1',published='2026-09-14T23:00:00+08:00',finish='2026-09-14T23:59:00+08:00')
            self.record(c,'1','group');self.record(c,'2',finish='2026-09-15T12:01:00+08:00')
            v=self.value(c)
            self.assertEqual(v['achieved'],0);self.assertEqual(v['daily']['2026-09-14'],1)
    def test_freshness_at_identification_and_live_receive_time(self):
        with app.db() as c:
            self.record(c,'1',published='2026-09-14T01:00:00+08:00')
            self.record(c,'2',published='2026-09-14T00:59:59+08:00')
            self.record(c,'3',published=None);self.record(c,'4','live',published=None)
            self.record(c,'5','group',published=None);self.record(c,'6',published='2026-09-15T01:00:01+08:00')
            self.assertEqual(self.value(c)['achieved'],2)
    def test_modified_source_without_new_model_and_known_club_do_not_count(self):
        with app.db() as c:
            rid=self.record(c,'1');self.record(c,'2','group')
            c.execute("UPDATE comments SET raw_text='修改后的原文' WHERE id=?",(rid,))
            c.execute("INSERT INTO service_author_roles VALUES('2','club',?,?)",(json.dumps(dict(basis='fixture')),AT))
            self.assertEqual(self.value(c)['achieved'],0)
    def test_goal_can_be_exceeded_without_creating_messages(self):
        with app.db() as c:
            for n in range(101):self.record(c,str(n+1000))
            v=self.value(c);self.assertEqual((v['achieved'],v['remaining'],v['progress_percent'],v['status']),(101,0,101.0,'met'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)

if __name__=='__main__':unittest.main()
