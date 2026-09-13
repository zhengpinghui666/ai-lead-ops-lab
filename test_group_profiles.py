import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
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
            profiles.tick(profile_reader=reader)
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

if __name__=='__main__':unittest.main()
