import io,json,tempfile,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch,Mock
from contextlib import closing,contextmanager
import clubops as app
import group_profiles as profiles
import group_inbox
import group_monitor as monitor
import uid_protocol as wire
from test_uid_session import data,SENDER,RECEIVER
import uid_session

SEC='synthetic_sec_user';UID=RECEIVER


class ProfilesTests(unittest.TestCase):
    def client(self,rows,status=0):
        response=io.BytesIO(json.dumps(dict(status_code=status,data=rows)).encode());response.status=200;response.headers={'Content-Type':'application/json'}
        return Mock(provider=uid_session.Provider(memory=data()),opener=Mock(open=Mock(return_value=response)))

    def test_public_profile_is_bound_to_both_ids_and_gender_must_be_explicit(self):
        target=[dict(uid=UID,sec_uid=SEC)]
        row=dict(uid=UID,sec_uid=SEC,nickname='真实昵称',gender=2)
        result=profiles.profiles(SENDER,target,client=self.client([row]))
        self.assertEqual(result['profiles'][0]['gender'],2)
        for value in [None,'2',True,3]:
            result=profiles.profiles(SENDER,target,client=self.client([dict(row,gender=value)]))
            self.assertIsNone(result['profiles'][0]['gender'])
        for rows in [[dict(row,uid=SENDER)],[dict(row,sec_uid='other_sec_uid')],[row,row]]:
            with self.assertRaises(ValueError):profiles.profiles(SENDER,target,client=self.client(rows))
        self.assertEqual(profiles.profiles(SENDER,target,client=self.client([]))['profiles'],[])

    def test_members_allow_observed_whole_page_and_terminal_cursor(self):
        rows=b''.join(wire.field(1,wire.field(1,100000+i)+wire.field(5,SEC+str(i))) for i in range(163))
        page=wire.field(1,rows+wire.field(2,0)+wire.field(3,2**64-1))
        group=dict(member=True,inbox=0,conversation_id='synthetic_group',conversation_short_id='7657809277079257637')
        with patch('uid_inbox._identity'),patch('uid_inbox._read',return_value=wire.decode(page)):
            result=group_inbox.members(SENDER,group,provider=Mock())
        self.assertEqual(len(result['members']),163);self.assertEqual(result['next_cursor'],0)
        with patch('uid_inbox._identity') as identity,self.assertRaises(ValueError):
            group_inbox.members(SENDER,dict(group,member=False))
        identity.assert_not_called()

    def test_cache_backfills_existing_customer_without_creating_jobs_or_rewriting_messages(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(app,'DATA_DIR',Path(folder)),patch('uid_inbox_store._account',return_value=SENDER):
            app.init();monitor.STOP.clear()
            with app.db() as c:
                c.execute("INSERT INTO monitored_groups(account_uid,conversation_id,conversation_short_id,name,description,notice,member,participants,matched,enabled,checked_at) VALUES(?,?,?,'瓦群','','',1,3,1,1,?)",(SENDER,'synthetic_group','7657809277079257637',app.now()))
                group=dict(c.execute('SELECT * FROM monitored_groups').fetchone())
                pid=monitor.attach(c,UID,app.now())
                c.execute("INSERT INTO group_messages(group_id,message_id,uid,person_id,raw_text,group_title,message_index,published_at,observed_at,filter_reason,relevance,category,game,reason,facts) VALUES(1,'100',?,?,'找人打瓦','瓦群','1',?,?,'','{}','buyer','无畏契约','','{}')",(UID,pid,app.now(),app.now()))
                profiles.remember(c,SENDER,UID,SEC)
            def reader(account,targets):
                self.assertEqual(account,SENDER);self.assertEqual(targets[0]['uid'],UID)
                return dict(profiles=[dict(uid=UID,sec_uid=SEC,nickname='已补齐昵称',gender=2)],proof={'http_status':200})
            base_db=app.db;selections=[];case=self
            class Connection:
                def __init__(self,c):self.c=c
                def __getattr__(self,key):return getattr(self.c,key)
                def execute(self,sql,*args):
                    if 'SELECT p.* FROM group_profiles p' in sql or 'SELECT DISTINCT p.* FROM group_profiles p' in sql:
                        case.assertFalse(self.c.in_transaction)
                        # A simultaneous collector write must not wait for the
                        # profile query, including on the old implementation.
                        with closing(sqlite3.connect(Path(folder)/'clubops-live.db',timeout=0)) as other,other:
                            other.execute("INSERT INTO events(kind,detail,created_at) VALUES('test','concurrent ingestion',?)",(app.now(),))
                        selections.append(True)
                    return self.c.execute(sql,*args)
            @contextmanager
            def instrumented_db(*args,**kw):
                with base_db(*args,**kw) as c:yield Connection(c)
            with patch.object(app,'db',side_effect=instrumented_db):
                profiles.tick(profile_reader=reader)
            self.assertEqual(selections,[True])
            with app.db() as c:
                self.assertEqual(c.execute('SELECT nickname,profile_gender FROM people').fetchone()[:],('已补齐昵称',2))
                self.assertEqual(c.execute('SELECT COUNT(*) FROM message_jobs').fetchone()[0],0)
                self.assertEqual(c.execute('SELECT raw_text FROM group_messages').fetchone()[0],'找人打瓦')
                self.assertEqual(profiles.state(c,SENDER),dict(speakers=1,named=1))
                profiles.remember(c,SENDER,UID,'conflicting_sec_uid')
                self.assertEqual(c.execute('SELECT sec_uid FROM group_profiles').fetchone()[0],SEC)
            mock=Mock();profiles.tick(profile_reader=mock);mock.assert_not_called()

    def test_identity_cache_does_not_create_leads_from_membership_alone(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(app,'DATA_DIR',Path(folder)),patch('uid_inbox_store._account',return_value=SENDER):
            app.init()
            with app.db() as c:
                profiles.remember(c,SENDER,UID)
                profiles.remember(c,SENDER,UID,SEC)
                self.assertEqual(c.execute('SELECT sec_uid FROM group_profiles').fetchone()[0],SEC)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM people').fetchone()[0],0)

    def test_selection_preserves_account_membership_freshness_and_priority(self):
        from datetime import datetime,timedelta
        with tempfile.TemporaryDirectory() as folder,patch.object(app,'DATA_DIR',Path(folder)):
            app.init();stamp=app.now()
            old=(datetime.fromisoformat(stamp)-timedelta(days=2)).isoformat()
            with app.db() as c:
                source=c.execute("INSERT INTO sources(name,kind) VALUES('selection fixture','browser')").lastrowid
                video=c.execute("INSERT INTO videos(source_id,external_id,title,created_at) VALUES(?,'fixture','fixture',?)",(source,stamp)).lastrowid
                group_ids=[]
                for owner,enabled in ((SENDER,1),('88888888',1),(SENDER,0)):
                    group_ids.append(c.execute("INSERT INTO monitored_groups(account_uid,conversation_id,conversation_short_id,name,description,notice,member,participants,matched,enabled,checked_at) VALUES(?,?,?,'瓦群','','',1,3,1,?,?)",(owner,'fixture'+str(enabled)+owner,'7657809277079257637',enabled,stamp)).lastrowid)
                def speaker(uid,gid,sec=SEC):
                    profiles.remember(c,SENDER,uid,sec)
                    c.execute("INSERT INTO group_messages(group_id,message_id,uid,raw_text,group_title,message_index,published_at,observed_at,filter_reason,relevance,category,game,reason,facts) VALUES(?,?,?,'fixture','瓦群','1',?,?,'','{}','social','无畏契约','','{}')",(gid,uid,uid,stamp,stamp))
                for number in range(100,125):speaker(str(number),group_ids[0])
                speaker('201',group_ids[1]);speaker('202',group_ids[2]);speaker('203',group_ids[0],'')
                for uid,published in (('204',stamp),('205',old)):
                    profiles.remember(c,SENDER,uid,SEC)
                    person=c.execute("INSERT INTO people(source_id,external_id,nickname) VALUES(?,?,'fixture')",(source,uid)).lastrowid
                    c.execute("INSERT INTO comments(source_id,external_id,video_id,person_id,raw_text,published_at,discovered_at) VALUES(?,?,?,?,'fixture',?,?)",(source,uid,video,person,published,stamp))
                # A known name yields to an unnamed speaker; a previously
                # contacted speaker precedes the remaining unnamed candidates.
                c.execute("UPDATE group_profiles SET nickname='known' WHERE uid='100'")
                person=c.execute("INSERT INTO people(source_id,external_id,nickname) VALUES(?,'124','fixture')",(source,)).lastrowid
                lead=c.execute("INSERT INTO leads(person_id,updated_at) VALUES(?,?)",(person,stamp)).lastrowid
                c.execute("INSERT INTO message_jobs(lead_id,request_id,content,status,detail,created_at,updated_at) VALUES(?,'fixture','fixture','draft','',?,?)",(lead,stamp,stamp))
            with app.db() as c:
                rows,group=profiles.select_candidates(c,SENDER,stamp)
                self.assertEqual(len(rows),20);self.assertIsNone(group)
                self.assertEqual(rows[0]['uid'],'124')
                self.assertTrue(all(r['account_uid']==SENDER for r in rows))
                self.assertTrue({'100','201','202','203','205'}.isdisjoint(r['uid'] for r in rows))
                self.assertFalse(c.in_transaction)
                c.execute("UPDATE group_profiles SET next_check_at='9999' WHERE uid BETWEEN '100' AND '124'")
            with app.db() as c:
                rows,group=profiles.select_candidates(c,SENDER,stamp)
                self.assertEqual([r['uid'] for r in rows],['204']);self.assertIsNone(group)
                c.execute("UPDATE group_profiles SET next_check_at='9999' WHERE uid='204'")
            with app.db() as c:
                rows,group=profiles.select_candidates(c,SENDER,stamp)
                self.assertEqual(rows,[]);self.assertEqual(group['id'],group_ids[0])
                self.assertFalse(c.in_transaction)

if __name__=='__main__':unittest.main()
