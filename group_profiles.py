"""Cached public identities for observed group speakers. No message sends."""
import hashlib
import json
import re
import urllib.parse
import urllib.request

import clubops as app
import group_inbox
import group_public
import uid_inbox_store
import uid_messaging

SCHEMA='''
CREATE TABLE IF NOT EXISTS group_profiles (
 account_uid TEXT NOT NULL,uid TEXT NOT NULL,sec_uid TEXT NOT NULL DEFAULT '',
 nickname TEXT NOT NULL DEFAULT '',gender INTEGER,checked_at TEXT NOT NULL DEFAULT '',
 next_check_at TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending',
 failures INTEGER NOT NULL DEFAULT 0,proof TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(account_uid,uid)
);
CREATE TABLE IF NOT EXISTS group_profile_scans (
 group_id INTEGER PRIMARY KEY,cursor INTEGER NOT NULL DEFAULT 0,
 next_check_at TEXT NOT NULL DEFAULT '',checked_at TEXT NOT NULL DEFAULT ''
);
'''


def remember(c,account,uid,sec=''):
    group_inbox.wire.numeric_uid(uid)
    if sec and not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',sec):sec=''
    # A conflicting sec UID is never allowed to replace a verified mapping.
    c.execute('''INSERT INTO group_profiles(account_uid,uid,sec_uid) VALUES(?,?,?)
        ON CONFLICT(account_uid,uid) DO UPDATE SET sec_uid=CASE
        WHEN group_profiles.sec_uid='' THEN excluded.sec_uid ELSE group_profiles.sec_uid END''',(account,uid,sec))


def profiles(account,targets,*,client=None):
    """Official PCIM POST is a read: sec_user_ids -> user data bound to both IDs."""
    if not isinstance(targets,list) or not 1<=len(targets)<=20:raise ValueError('资料读取范围无效')
    expected={group_public.owner(t['sec_uid']):group_inbox.wire.numeric_uid(t['uid']) for t in targets}
    if len(expected)!=len(targets) or len(set(expected.values()))!=len(targets):raise ValueError('资料身份重复')
    client=client or group_public.Client(account)
    session=client.provider.current()
    if session['sender_uid']!=account:raise ValueError('账号已经改变')
    data=urllib.parse.urlencode({'sec_user_ids':json.dumps(list(expected))}).encode()
    request=urllib.request.Request('https://www.douyin.com/aweme/v1/web/im/user/info/?aid=6383&version_code=170400',data=data,
        headers={'Cookie':session['identity_cookie'],'User-Agent':session['headers']['user-agent'],
                 'Referer':'https://www.douyin.com/','Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'})
    with client.opener.open(request,timeout=15) as response:
        raw=response.read(1048577)
        if response.status!=200 or len(raw)>1048576 or 'json' not in response.headers.get('Content-Type',''):
            raise ValueError('资料接口响应无效')
    value=json.loads(raw)
    if not isinstance(value,dict) or type(value.get('status_code')) is not int or value['status_code']!=0:
        raise ValueError('资料接口未通过')
    rows=value.get('data')
    if not isinstance(rows,list) or len(rows)>len(targets):raise ValueError('资料响应范围不符')
    output=[];seen=set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('资料记录无效')
        uid=row.get('uid');sec=row.get('sec_uid');name=row.get('nickname')
        if not isinstance(uid,str) or not isinstance(sec,str) or expected.get(sec)!=uid or uid in seen:
            raise ValueError('资料响应身份不一致')
        seen.add(uid)
        if not isinstance(name,str) or not name.strip() or len(name)>200:continue
        gender=row.get('gender')
        follow=row.get('follow_status')
        output.append(dict(uid=uid,sec_uid=sec,nickname=name,signature=row.get('signature')[:1000] if isinstance(row.get('signature'),str) else None,
                           gender=gender if type(gender) is int and gender in (0,1,2) else None,
                           follow_status=follow if type(follow) is int and follow in (0,1,2) else None))
    return dict(profiles=output,proof=dict(http_status=200,response_bytes=len(raw),response_sha256=hashlib.sha256(raw).hexdigest()))


def seed_observed(c,account):
    c.execute('''INSERT OR IGNORE INTO group_profiles(account_uid,uid)
        SELECT DISTINCT g.account_uid,m.uid FROM group_messages m JOIN monitored_groups g ON g.id=m.group_id
        WHERE g.account_uid=? AND g.member=1 AND g.matched=1''',(account,))


def tick(*,member_reader=None,profile_reader=None):
    import group_monitor as monitor
    if monitor.STOP.is_set() or not monitor.GUARD.acquire(blocking=False):return
    acquired=False;targets=[];group=None
    try:
        account=uid_inbox_store._account()
        with app.db() as c:
            cfg=c.execute("SELECT value FROM settings WHERE key='group_profile_next_run'").fetchone()
            if cfg and json.loads(cfg[0])>app.now():return
            seed_observed(c,account)
            targets=[dict(r) for r in c.execute('''SELECT DISTINCT p.* FROM group_profiles p
                WHERE p.account_uid=? AND (EXISTS(SELECT 1 FROM group_messages m JOIN monitored_groups g ON g.id=m.group_id
                  WHERE m.uid=p.uid AND g.account_uid=p.account_uid AND g.enabled=1 AND g.member=1 AND g.matched=1)
                  OR EXISTS(SELECT 1 FROM people u JOIN comments x ON x.person_id=u.id WHERE u.external_id=p.uid
                    AND julianday(x.published_at)>=julianday('now','-1 day')))
                AND p.sec_uid!='' AND p.next_check_at<=?
                ORDER BY (p.nickname='') DESC, EXISTS(SELECT 1 FROM people u JOIN leads l ON l.person_id=u.id
                  JOIN message_jobs j ON j.lead_id=l.id WHERE u.external_id=p.uid) DESC,p.checked_at,p.uid LIMIT 20''',(account,app.now()))]
            if not targets:
                row=c.execute('''SELECT g.*,COALESCE(s.cursor,0) AS profile_cursor FROM monitored_groups g
                    LEFT JOIN group_profile_scans s ON s.group_id=g.id
                    WHERE g.account_uid=? AND g.enabled=1 AND g.member=1 AND g.matched=1
                    AND COALESCE(s.next_check_at,'')<=? AND EXISTS(SELECT 1 FROM group_messages m
                      JOIN group_profiles p ON p.uid=m.uid AND p.account_uid=g.account_uid
                      WHERE m.group_id=g.id AND p.sec_uid='') ORDER BY COALESCE(s.checked_at,''),g.id LIMIT 1''',(account,app.now())).fetchone()
                group=dict(row) if row else None
        if not targets and not group:return
        acquired=uid_messaging.GUARD.acquire(blocking=False)
        if not acquired:return
        monitor.ACTIVE=True
        with app.LOCKS['live'],app.db() as c:
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('group_profile_next_run',json.dumps(monitor.stamp_after(30))))
        if group:
            result=(member_reader or group_inbox.members)(account,dict(group,inbox=0),cursor=group['profile_cursor'])
            with app.LOCKS['live'],app.db() as c:
                if account!=uid_inbox_store._account() or monitor.STOP.is_set():return
                for row in result['members']:remember(c,account,row['uid'],row['sec_uid'])
                c.execute('INSERT OR REPLACE INTO group_profile_scans VALUES(?,?,?,?)',
                    (group['id'],result['next_cursor'] if result['has_more'] else 0,
                     monitor.stamp_after(30 if result['has_more'] else 21600),app.now()))
            return
        result=(profile_reader or profiles)(account,targets)
        with app.LOCKS['live'],app.db() as c:
            if account!=uid_inbox_store._account() or monitor.STOP.is_set():return
            found={r['uid']:r for r in result['profiles']}
            for target in targets:
                value=found.get(target['uid']);stamp=app.now()
                if not value:
                    c.execute("UPDATE group_profiles SET status='unavailable',next_check_at=? WHERE account_uid=? AND uid=?",
                              (monitor.stamp_after(21600),account,target['uid']))
                    continue
                if value['sec_uid']!=target['sec_uid']:raise ValueError('资料身份已经改变')
                import author_roles
                author_roles.remember(c,value['uid'],nickname=value['nickname'],signature=value.get('signature'),source='public_profile')
                if author_roles.related(c,value['uid']):
                    for m in c.execute("SELECT id,relevance FROM group_messages WHERE uid=? AND filter_reason='未通过陪玩需求初筛' AND julianday(published_at)>=julianday(?,'-1 day')",(value['uid'],stamp)).fetchall():
                        relevance=json.loads(m['relevance']);relevance.update(passed=True,reason='作者资料含陪玩／打手或俱乐部线索，交模型区分身份。')
                        c.execute("UPDATE group_messages SET filter_reason='',relevance=? WHERE id=?",(json.dumps(relevance,ensure_ascii=False),m['id']))
                c.execute('''UPDATE group_profiles SET nickname=?,gender=?,checked_at=?,next_check_at=?,status='ready',
                    failures=0,proof=? WHERE account_uid=? AND uid=? AND sec_uid=?''',
                    (value['nickname'],value['gender'],stamp,monitor.stamp_after(86400),json.dumps(result['proof']),account,value['uid'],value['sec_uid']))
                c.execute('''UPDATE people SET nickname=?,profile_gender=CASE WHEN ? IS NULL THEN profile_gender ELSE ? END,
                    profile_gender_observed_at=CASE WHEN ? IS NULL THEN profile_gender_observed_at ELSE ? END
                    WHERE external_id=? AND source_id IN(SELECT id FROM sources WHERE kind='browser')''',
                    (value['nickname'],value['gender'],value['gender'],value['gender'],stamp,value['uid']))
    except Exception:
        if acquired:
            with app.LOCKS['live'],app.db() as c:
                for target in targets:
                    c.execute("UPDATE group_profiles SET status='retrying',failures=failures+1,next_check_at=? WHERE account_uid=? AND uid=?",
                              (monitor.stamp_after(min(21600,60*2**min(8,target['failures']))),account,target['uid']))
                c.execute('INSERT INTO group_reads(group_id,account_uid,operation,status,detail,created_at) VALUES(?,?,?,?,?,?)',
                          (group['id'] if group else None,account,'profiles','failed','资料读取未完成；消息采集继续，稍后补齐',app.now()))
                c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('group_profile_next_run',json.dumps(monitor.stamp_after(120))))
    finally:
        if acquired:monitor.ACTIVE=False;uid_messaging.GUARD.release()
        monitor.GUARD.release()


def state(c,account):
    row=c.execute('''SELECT COUNT(DISTINCT m.uid) AS speakers,
      COUNT(DISTINCT CASE WHEN p.nickname!='' THEN m.uid END) AS named
      FROM group_messages m JOIN monitored_groups g ON g.id=m.group_id
      LEFT JOIN group_profiles p ON p.account_uid=g.account_uid AND p.uid=m.uid WHERE g.account_uid=?''',(account,)).fetchone()
    return dict(row)
