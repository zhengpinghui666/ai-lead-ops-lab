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
                member=inbox._flag(row,8),participants=inbox._number(wire.one(row,7,0,0)),inbox=0,
                admin_only=wire.one(core,14,0,0)==1 and wire.one(core,15,0,0)==1)


DEFAULT_CATALOG_LIMIT = 5


def catalog(account,*,cursor=0,limit=DEFAULT_CATALOG_LIMIT,provider=None,exchange=None):
    inbox._number(cursor);inbox._number(limit,1,20)
    provider=provider or uid_session.Provider();exchange=exchange or uid_transport.request
    evidence=[];inbox._identity(account,provider,exchange,evidence)
    # Verified public encoder: 1 sort_type, 2 cursor, 3 con_type, 4 limit.
    # Group entries include large metadata. Keep the transport's body bound and
    # retry only an oversized HTTP 200 read, at the same cursor with a fresh seq.
    while True:
        body=wire.field(2006,wire.field(1,1)+wire.field(2,cursor)+wire.field(3,2)+wire.field(4,limit))
        try:
            info=inbox._read('conversations',body,account,provider,exchange,evidence)
            break
        except uid_transport.TransportError as exc:
            proof=exc.evidence
            if (limit<=1 or proof.get('transport_error')!='response_exceeds_bound'
                    or proof.get('http_status')!=200 or proof.get('transport_phase')!='response_body'):
                raise
            limit=max(1,limit//2)
    rows=[group_row(r) for r in inbox._items(info,1,limit)]
    if len({r['conversation_id'] for r in rows})!=len(rows):raise inbox.ReadError('duplicate_conversations_in_page')
    more=inbox._flag(info,2);next_cursor=inbox._number(wire.one(info,3,0,0))
    if more and next_cursor<=cursor:raise inbox.ReadError('nonadvancing_cursor')
    return dict(groups=rows,has_more=more,next_cursor=str(next_cursor),page_limit=limit,evidence=evidence)


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
    output=[];indices=[];seen={};skipped=0;duplicates=0
    message_types={};skip_reasons=dict(nontext_or_unsupported_type=0,deleted=0,invalid_text=0)
    for row in inbox._items(info,1,limit):
        if wire.text(row,1)!=cid or wire.one(row,2,0)!=2 or wire.one(row,5,0)!=short:
            raise inbox.ReadError('message_conversation_mismatch')
        mid=str(inbox._number(wire.one(row,3,0),1));index=inbox._number(wire.one(row,4,0,0))
        indices.append(index)
        if mid in seen:
            previous=seen[mid]
            # A verified group page can repeat an event with a new sequence
            # index and envelope metadata. Accept only identical business
            # fields; content, author, timestamp and deletion/type conflicts
            # still reject the entire page before any store write.
            metadata={4,9,13,17}
            if any(row.get(k)!=previous.get(k) for k in (set(row)|set(previous))-metadata):
                raise inbox.ReadError('duplicate_message_id')
            duplicates+=1
            for message in output:
                if message['message_id']==mid:
                    message['index']=str(max(int(message['index']),index))
                    break
            continue
        seen[mid]=row
        kind=wire.one(row,6,0);label=str(kind) if type(kind) is int else 'unknown'
        message_types[label]=message_types.get(label,0)+1
        if kind!=7 or wire.one(row,12,0,0)!=0:
            skip_reasons['nontext_or_unsupported_type' if kind!=7 else 'deleted']+=1
            skipped+=1;continue
        uid=str(inbox._number(wire.one(row,7,0),1));wire.numeric_uid(uid)
        try:
            content=json.loads(wire.text(row,8));text=content.get('text') if isinstance(content,dict) else None
            if not isinstance(text,str) or not text.strip() or len(text)>5000:raise ValueError()
        except (ValueError,TypeError):
            skip_reasons['invalid_text']+=1
            skipped+=1;continue
        sec=wire.text(row,14)
        output.append(dict(message_id=mid,uid=uid,raw_text=text,index=str(index),
                           sec_uid=sec if re.fullmatch(r'[A-Za-z0-9_-]{10,200}',sec) else '',
                           created_at_raw=str(inbox._number(wire.one(row,10,0,0)))))
    more=inbox._flag(info,3);next_cursor=inbox._number(wire.one(info,2,0,0))
    if more and (not next_cursor or cursor and next_cursor>=cursor):raise inbox.ReadError('nonadvancing_cursor')
    return dict(messages=output,skipped=skipped,duplicates=duplicates,message_types=message_types,skip_reasons=skip_reasons,has_more=more,next_cursor=str(next_cursor),
                minimum_index=str(min(indices)) if indices else None,maximum_index=str(max(indices)) if indices else None,
                evidence=evidence,group_sent=False,read_marker_requested=False)


def members(account,group,*,cursor=0,limit=100,provider=None,exchange=None):
    """One verified member page; official command 605, no invitations or markers."""
    inbox._number(cursor);inbox._number(limit,1,100)
    if not group.get('member') or group.get('inbox')!=0:raise ValueError('只能读取已加入群的成员资料')
    cid=group['conversation_id'];short=int(wire.numeric_uid(group['conversation_short_id']))
    if not re.fullmatch(r'[A-Za-z0-9:_-]{5,100}',cid):raise ValueError('群会话标识无效')
    provider=provider or uid_session.Provider();exchange=exchange or uid_transport.request
    evidence=[];inbox._identity(account,provider,exchange,evidence)
    body=wire.field(605,wire.field(1,cid)+wire.field(2,short)+wire.field(3,2)+wire.field(4,cursor)+wire.field(5,limit))
    info=inbox._read('group_members',body,account,provider,exchange,evidence)
    page=wire.decode(wire.one(info,1,2));rows=[]
    # Observed PCIM returns a whole member list despite limit=100 (163 rows).
    # Keep an independent response bound and preserve the real has_more flag.
    for row in inbox._items(page,1,1000):
        uid=str(inbox._number(wire.one(row,1,0),1));wire.numeric_uid(uid)
        sec=wire.text(row,5)
        if sec and not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',sec):raise ValueError('成员资料标识无效')
        rows.append(dict(uid=uid,sec_uid=sec))
    if len({r['uid'] for r in rows})!=len(rows):raise ValueError('成员页存在重复身份')
    more=inbox._flag(page,2);raw_cursor=wire.one(page,3,0,0)
    next_cursor=0 if not more and raw_cursor==2**64-1 else inbox._number(raw_cursor)
    if more and next_cursor<=cursor:raise inbox.ReadError('nonadvancing_cursor')
    return dict(members=rows,has_more=more,next_cursor=next_cursor,evidence=evidence)
