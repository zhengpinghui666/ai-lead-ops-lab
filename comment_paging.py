"""Durable HTTP page rotation. Cursors are observations, not a coverage claim."""
from collections import deque
from datetime import datetime
import hashlib
import json
import re

import clubops as app

VERSION='comment-page-rotation-v1'
MAX_REPLIES=200
MAX_AGE=6*3600
SCHEMA='''CREATE TABLE IF NOT EXISTS collection_page_progress (
 source_id INTEGER NOT NULL REFERENCES sources(id),video_id TEXT NOT NULL,
 session_tag TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,
 task_id INTEGER NOT NULL REFERENCES collection_tasks(id),updated_at TEXT NOT NULL,
 PRIMARY KEY(source_id,video_id)
);'''


def session_tag(session):
    return hashlib.sha256((session['account']+':'+str(session['captured_at'])).encode()).hexdigest()


def cursor(value,nullable=False):
    if value is None and nullable:return None
    if type(value) is not int or not 0<=value<2**63:raise ValueError('评论分页游标无效')
    return value


def validate(state):
    if not isinstance(state,dict) or set(state)!={'version','main_cursor','replies'} or state['version']!=VERSION:
        raise ValueError('评论分页状态版本无效')
    cursor(state['main_cursor'],True)
    if not isinstance(state['replies'],list) or len(state['replies'])>MAX_REPLIES:raise ValueError('回复队列超出范围')
    seen=set()
    for row in state['replies']:
        if not isinstance(row,list) or len(row)!=2 or not isinstance(row[0],str) or not re.fullmatch(r'[0-9]{5,30}',row[0]) or row[0] in seen:
            raise ValueError('回复分页身份无效')
        cursor(row[1]);seen.add(row[0])
    return state


def empty():return dict(version=VERSION,main_cursor=0,replies=[])


def load(c,source_id,video_id,tag,instant):
    row=c.execute('SELECT * FROM collection_page_progress WHERE source_id=? AND video_id=?',(source_id,video_id)).fetchone()
    revision=row['revision'] if row else 0
    if not row:return revision,empty(),'new'
    if row['session_tag']!=tag:return revision,empty(),'session_changed'
    elapsed=(datetime.fromisoformat(instant)-datetime.fromisoformat(row['updated_at'])).total_seconds()
    if not 0<=elapsed<=MAX_AGE:return revision,empty(),'expired'
    return revision,validate(json.loads(row['state'])),'continued'


def save(c,task_id,source_id,message,expected_tag):
    """Parent receives this only after saving the page's preceding observations."""
    if set(message)!={'type','video_id','session_tag','revision','state','page'}:raise ValueError('评论分页记录字段无效')
    vid=message['video_id'];revision=message['revision']
    if not isinstance(vid,str) or not re.fullmatch(r'[0-9]{5,30}',vid) or type(revision) is not int or not 0<revision<2**63:
        raise ValueError('评论分页记录身份无效')
    state=validate(message['state']);page=message['page']
    if not isinstance(page,dict) or set(page)!={'operation','requested_cursor','next_cursor','has_more','parent','fully_consumed','deferred_replies'}:
        raise ValueError('评论分页来源无效')
    if page['operation'] not in ('comments','replies') or type(page['has_more']) is not bool or type(page['fully_consumed']) is not bool:
        raise ValueError('评论分页来源状态无效')
    cursor(page['requested_cursor']);cursor(page['next_cursor'])
    if page['has_more'] and page['next_cursor']<=page['requested_cursor']:raise ValueError('评论分页未前进')
    if (page['operation']=='comments' and page['parent']!='' or page['operation']=='replies' and
        (not isinstance(page['parent'],str) or not re.fullmatch(r'[0-9]{5,30}',page['parent']))):raise ValueError('回复来源无效')
    if type(page['deferred_replies']) is not int or not 0<=page['deferred_replies']<=20:raise ValueError('回复延期计数无效')
    task=c.execute('SELECT transport,status,finished_at FROM collection_tasks WHERE id=?',(task_id,)).fetchone()
    cp=c.execute('SELECT status FROM collection_checkpoints WHERE task_id=? AND video_id=?',(task_id,vid)).fetchone()
    if not task or task['transport']!='http' or task['finished_at'] or task['status']!='running' or not cp or cp['status']!='reading':return False
    if not expected_tag or message['session_tag']!=expected_tag:return False
    old=c.execute('SELECT revision FROM collection_page_progress WHERE source_id=? AND video_id=?',(source_id,vid)).fetchone()
    if revision!=(old[0] if old else 0)+1:return False
    c.execute('''INSERT INTO collection_page_progress VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(source_id,video_id) DO UPDATE SET session_tag=excluded.session_tag,
      revision=excluded.revision,state=excluded.state,task_id=excluded.task_id,updated_at=excluded.updated_at''',
      (source_id,vid,expected_tag,revision,json.dumps(state,separators=(',',':')),task_id,app.now()))
    # Cursor-only evidence is separate from the capped HTTP diagnostics stream.
    c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
      (task_id,'comment_paging',json.dumps(dict(processing=dict(version=VERSION,video_id=vid,revision=revision,
        main_cursor=state['main_cursor'],pending_replies=len(state['replies']),page=page)),separators=(',',':')),app.now()))
    return True


class Rotation:
    def __init__(self,state=None):
        state=validate(state or empty())
        self.main=state['main_cursor'];self.replies=deque(tuple(r) for r in state['replies'])
        self.head=True;self.prefer_reply=True
        self.known={r[0] for r in self.replies}

    def next(self):
        if self.head:return 'comments','',0
        if self.replies and (self.prefer_reply or self.main is None or len(self.replies)>=MAX_REPLIES-20):
            parent,offset=self.replies[0];return 'replies',parent,offset
        if self.main is not None:return 'comments','',self.main
        return None

    def accept(self,request,page,fully_consumed):
        op,parent,offset=request;more=page['has_more'];following=page['cursor']
        cursor(following)
        if type(more) is not bool or more and following<=offset:raise ValueError('评论分页未前进')
        deferred=0
        if op=='comments':
            for target in page.get('reply_targets',[]):
                if target not in self.known:
                    if len(self.replies)<MAX_REPLIES:
                        self.replies.append((target,0));self.known.add(target)
                    else:deferred+=1
            if self.head:
                # Always refresh page zero; a saved continuation follows it.
                if not more and fully_consumed:self.main=None
                elif self.main in (None,0):self.main=following if fully_consumed and not deferred else offset
                self.head=False
            elif fully_consumed and not deferred:self.main=following if more else None
            # Otherwise repeat the page next batch, including deferred parents.
            self.prefer_reply=True
        else:
            if not self.replies or self.replies[0]!=(parent,offset):raise ValueError('回复队列来源不一致')
            self.replies.popleft()
            if not fully_consumed:self.replies.appendleft((parent,offset))
            elif more:self.replies.append((parent,following))
            self.prefer_reply=False
        return dict(operation=op,parent=parent,requested_cursor=offset,next_cursor=following,has_more=more,
                    fully_consumed=fully_consumed,deferred_replies=deferred)

    def snapshot(self):
        return validate(dict(version=VERSION,main_cursor=self.main,replies=[list(r) for r in self.replies]))
