"""Own-account group catalog and text reads. No group sends, invitations or read markers."""
import json
import re
import uid_inbox as inbox
import uid_protocol as wire
import uid_session
import uid_transport


def group_row(row):
    cid=wire.text(row,1);short=inbox._number(wire.one(row,2,0),1)
    core=wire.decode(wire.one(row,50,2))
    if (not re.fullmatch(r'[A-Za-z0-9:_-]{5,100}',cid) or wire.one(row,3,0)!=2
            or wire.one(row,9,0,0)!=0 or wire.text(core,1)!=cid
            or wire.one(core,2,0)!=short or wire.one(core,3,0)!=2
            or wire.one(core,8,0,0)!=0):
        raise inbox.ReadError('conversation_identity_mismatch')
    return dict(conversation_id=cid,conversation_short_id=str(short),name=wire.text(core,5)[:200],
                description=wire.text(core,6)[:500],notice=wire.text(core,9)[:1000],
                member=inbox._flag(row,8),participants=inbox._number(wire.one(row,7,0,0)),inbox=0)


def catalog(account,*,cursor=0,limit=20,provider=None,exchange=None):
    inbox._number(cursor);inbox._number(limit,1,20)
    provider=provider or uid_session.Provider();exchange=exchange or uid_transport.request
    evidence=[];inbox._identity(account,provider,exchange,evidence)
    # Verified public encoder: 1 sort_type, 2 cursor, 3 con_type, 4 limit.
    body=wire.field(2006,wire.field(1,1)+wire.field(2,cursor)+wire.field(3,2)+wire.field(4,limit))
    info=inbox._read('conversations',body,account,provider,exchange,evidence)
    rows=[group_row(r) for r in inbox._items(info,1,limit)]
    if len({r['conversation_id'] for r in rows})!=len(rows):raise inbox.ReadError('duplicate_conversations_in_page')
    more=inbox._flag(info,2);next_cursor=inbox._number(wire.one(info,3,0,0))
    if more and next_cursor<=cursor:raise inbox.ReadError('nonadvancing_cursor')
    return dict(groups=rows,has_more=more,next_cursor=str(next_cursor),evidence=evidence)


def messages(account,group,*,cursor=0,limit=20,provider=None,exchange=None):
    inbox._number(cursor);inbox._number(limit,1,20)
    if not group.get('member') or group.get('inbox')!=0:raise ValueError('只能读取当前账号已加入的群')
    cid=group['conversation_id'];short=int(wire.numeric_uid(group['conversation_short_id']))
    if not re.fullmatch(r'[A-Za-z0-9:_-]{5,100}',cid):raise ValueError('群会话标识无效')
    provider=provider or uid_session.Provider();exchange=exchange or uid_transport.request
    evidence=[];inbox._identity(account,provider,exchange,evidence)
    body=wire.field(301,wire.field(1,cid)+wire.field(2,2)+wire.field(3,short)
                    +wire.field(4,1 if cursor else 3)+wire.field(5,cursor)+wire.field(6,limit))
    info=inbox._read('messages',body,account,provider,exchange,evidence)
    output=[];indices=[];seen=set();skipped=0
    for row in inbox._items(info,1,limit):
        if wire.text(row,1)!=cid or wire.one(row,2,0)!=2 or wire.one(row,5,0)!=short:
            raise inbox.ReadError('message_conversation_mismatch')
        mid=str(inbox._number(wire.one(row,3,0),1));index=inbox._number(wire.one(row,4,0,0))
        if mid in seen:raise inbox.ReadError('duplicate_message_id')
        seen.add(mid);indices.append(index)
        if wire.one(row,6,0)!=7 or wire.one(row,12,0,0)!=0:
            skipped+=1;continue
        uid=str(inbox._number(wire.one(row,7,0),1));wire.numeric_uid(uid)
        try:
            content=json.loads(wire.text(row,8));text=content.get('text') if isinstance(content,dict) else None
            if not isinstance(text,str) or not text.strip() or len(text)>5000:raise ValueError()
        except (ValueError,TypeError):
            skipped+=1;continue
        output.append(dict(message_id=mid,uid=uid,raw_text=text,index=str(index),
                           created_at_raw=str(inbox._number(wire.one(row,10,0,0)))))
    more=inbox._flag(info,3);next_cursor=inbox._number(wire.one(info,2,0,0))
    if more and (not next_cursor or cursor and next_cursor>=cursor):raise inbox.ReadError('nonadvancing_cursor')
    return dict(messages=output,skipped=skipped,has_more=more,next_cursor=str(next_cursor),
                minimum_index=str(min(indices)) if indices else None,maximum_index=str(max(indices)) if indices else None,
                evidence=evidence,group_sent=False,read_marker_requested=False)
