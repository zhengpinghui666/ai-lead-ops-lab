"""UID-bound, evidence-backed account role; no identity guessed from a video owner."""
import json
import re
import clubops as app

SCHEMA = '''CREATE TABLE IF NOT EXISTS service_author_roles (
 uid TEXT PRIMARY KEY,role TEXT NOT NULL,evidence TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observed_author_profiles (
 uid TEXT PRIMARY KEY,nickname TEXT NOT NULL DEFAULT '',signature TEXT NOT NULL DEFAULT '',
 self_description TEXT NOT NULL DEFAULT '',observed_at TEXT NOT NULL,source TEXT NOT NULL
);'''
VERSION = 'author-role-v1'
OWN_CLUB = re.compile(r'(?:我是|我们是|我们这边是|这里是).{0,20}(?:电竞俱乐部|陪玩俱乐部|陪玩店|陪玩工作室)'
    r'|(?:本店|本俱乐部|我们俱乐部|我们店).{0,24}(?:陪玩|男陪|女陪|打手|接单|下单|招|陪练)'
    r'|(?:我们|本店).{0,12}(?:提供|主营|承接).{0,10}(?:陪玩|陪练)')
CLUB_NAME = re.compile(r'(?:电竞俱乐部|陪玩俱乐部|陪玩店|陪玩工作室|电竞工作室|俱乐部官方|电竞客服|官方客服)[）)\s]*$')
PERSONAL = re.compile(r'受害者|避雷|黑粉|粉丝|离职|前员工|求职|应聘|找工作|求收留|在.{0,8}(?:俱乐部|陪玩店).{0,8}(?:做|当|打工)|我是.{0,8}(?:俱乐部|陪玩店)的(?:陪玩|打手|员工|店员|成员)')
REPORTED = re.compile(r'听说|据说|他说|她说|别人说|有人说|比如|例如|举例|原话|引用|假如|如果|不是我|不是.{0,8}(?:店|俱乐部)')


def evidence(text='', nickname='', signature=''):
    for source, value in (('text',text),('signature',signature)):
        if not isinstance(value,str) or PERSONAL.search(value) or REPORTED.search(value):continue
        hit=OWN_CLUB.search(value)
        if hit:return dict(version=VERSION,source=source,quote=hit.group(),basis='账号自述为俱乐部／店铺，未核验经营资质')
    if isinstance(nickname,str) and not PERSONAL.search(nickname) and len(nickname)<=200:
        hit=CLUB_NAME.search(nickname)
        if hit:return dict(version=VERSION,source='nickname',quote=nickname,basis='账号昵称明确自称俱乐部／店铺，未核验经营资质')
    return None


def remember(c,uid,*,text='',nickname='',signature=None,source='platform_comment'):
    if not isinstance(uid,str) or not re.fullmatch(r'[0-9]{5,30}',uid):return
    nickname=app.clean(nickname,200)
    signature=app.clean(signature,1000) if isinstance(signature,str) else None
    description=evidence(text)
    description=description['quote'] if description else ''
    previous=c.execute('SELECT * FROM observed_author_profiles WHERE uid=?',(uid,)).fetchone()
    values=(nickname or (previous['nickname'] if previous else ''),
            signature if signature is not None else (previous['signature'] if previous else ''),
            description or (previous['self_description'] if previous else ''))
    if previous and tuple(previous[k] for k in ('nickname','signature','self_description'))!=values:
        c.execute('DELETE FROM service_author_roles WHERE uid=?',(uid,))
    c.execute('''INSERT INTO observed_author_profiles VALUES(?,?,?,?,?,?)
      ON CONFLICT(uid) DO UPDATE SET nickname=excluded.nickname,signature=excluded.signature,
      self_description=excluded.self_description,observed_at=excluded.observed_at,source=excluded.source''',
      (uid,*values,app.now(),source))


def context(c,kind,rid):
    if kind=='comment':
        person=c.execute('SELECT p.external_id FROM comments x JOIN people p ON p.id=x.person_id WHERE x.id=?',(rid,)).fetchone()
    elif kind=='live':person=c.execute('SELECT uid FROM live_messages WHERE id=?',(rid,)).fetchone()
    else:person=c.execute('SELECT uid FROM group_messages WHERE id=?',(rid,)).fetchone()
    if not person or not person[0]:return None
    r=c.execute('SELECT uid,nickname,signature,self_description FROM observed_author_profiles WHERE uid=?',(person[0],)).fetchone()
    if not r:return None
    if not re.search(r'俱乐部|陪|打手|接单|客服|工作室|老板|招人',r['nickname']+r['signature']+r['self_description'],re.I):return None
    return dict(r)


def related(c,uid):
    r=c.execute('SELECT nickname,signature,self_description FROM observed_author_profiles WHERE uid=?',(uid,)).fetchone()
    return bool(r and re.search(r'俱乐部|陪玩|陪练|男陪|女陪|打手|接单|陪玩店|工作室|招人',' '.join(r)))


def confirm(c,source,result):
    author=source.get('author') or {}
    uid=author.get('uid')
    if not uid or result.get('category')!='club':return
    proof=evidence(source['text'],author.get('nickname',''),author.get('signature','')) or evidence(author.get('self_description',''))
    if not proof:return
    c.execute('''INSERT INTO service_author_roles VALUES(?,'club',?,?)
      ON CONFLICT(uid) DO UPDATE SET role=excluded.role,evidence=excluded.evidence,updated_at=excluded.updated_at''',
      (uid,json.dumps(proof,ensure_ascii=False),app.now()))


def identify(c,row,source):
    uid=str((source.get('author') or {}).get('uid') or row.get('uid') or row.get('user_identifier') or '')
    pid=row.get('person_id')
    if not uid and pid:
        person=c.execute('SELECT external_id FROM people WHERE id=?',(pid,)).fetchone()
        if person:uid=person[0]
    # Some summary projections intentionally carry only a source row id.
    if not uid and source.get('kind')=='comment' and row.get('id'):
        person=c.execute('SELECT p.external_id FROM comments x JOIN people p ON p.id=x.person_id WHERE x.id=?',(row['id'],)).fetchone()
        if person:uid=person[0]
    if uid:
        known=c.execute('SELECT role,evidence FROM service_author_roles WHERE uid=?',(uid,)).fetchone()
        if known and known['role']=='club':return json.loads(known['evidence'])
    return None


def project(c,row,source):
    proof=identify(c,row,source)
    if proof:
        row['author_role']=dict(role='club',evidence=proof)
        row['content_category']=row.get('category')
        row.update(category='club',category_group='club',
                   reason='模型已结合账号资料识别为俱乐部，统一归入俱乐部；'+proof['basis'])
    elif row.get('category')=='buyer' and row.get('analysis_method')!='human':
        author=source.get('author') or {}
        hint=evidence(source['text'],author.get('nickname',''),author.get('signature','')) or evidence(author.get('self_description',''))
        if hint:
            row.update(category='uncertain',category_group='uncertain',reason='账号资料有俱乐部身份线索，等待模型区分身份；暂不进入客户私信。')
            row['author_role']=dict(role='pending',evidence=hint)
    return row
