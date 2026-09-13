"""Public profile group discovery and self-only join, using the saved HTTP session.

Protocol source: the observed official pcim SDK, including applyJoinGroup.
This module cannot send a group message or invite another participant.
"""
import hashlib
import json
import re
import urllib.parse
import urllib.request

import uid_inbox
import uid_protocol as wire
import uid_session
import uid_transport

PATHS={'list':'/aweme/v1/web/im_group_api/list/other/',
       'verify':'/aweme/v1/web/im_group_api/share/verification/'}


def owner(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',value):
        raise ValueError('公开群作者标识无效')
    return value


def join_body(group_id,sender,ticket,inviter):
    for value in (group_id,sender,inviter):wire.numeric_uid(value)
    if not isinstance(ticket,str) or not 1<=len(ticket)<=4096:raise ValueError('加群凭据无效')
    ext={'invitation':json.dumps({'invitee':{'source_app_id':6383},'invitor':{'im_user_id':int(inviter)},'source_type':50},separators=(',',':')),
         'source_type':'50','ticket':ticket}
    payload=wire.field(1,group_id)+wire.field(2,int(group_id))+wire.field(3,2)+wire.field(4,int(sender))
    return wire.field(650,payload+b''.join(wire.field(5,wire.field(1,k)+wire.field(2,v)) for k,v in ext.items()))


def validate_join_body(body,sender):
    outer=wire.decode(body)
    if set(outer)!={650}:raise ValueError('仅接受本人加入公开群')
    row=wire.decode(wire.one(outer,650,2));group_id=wire.numeric_uid(wire.text(row,1))
    if (set(row)!={1,2,3,4,5} or wire.one(row,2,0)!=int(group_id) or wire.one(row,3,0)!=2 or
            row.get(4)!=[(0,int(wire.numeric_uid(sender))) ]):raise ValueError('只能将当前账号本人加入群聊')
    ext={}
    for kind,raw in row[5]:
        if kind!=2:raise ValueError('加群参数无效')
        entry=wire.decode(raw);key=wire.text(entry,1)
        if key in ext or set(entry)!={1,2}:raise ValueError('加群参数无效')
        ext[key]=wire.text(entry,2)
    if set(ext)!={'invitation','source_type','ticket'} or ext['source_type']!='50':raise ValueError('加群来源无效')
    invitation=json.loads(ext['invitation']);inviter=invitation.get('invitor',{}).get('im_user_id')
    if type(inviter) is not int:raise ValueError('邀请来源无效')
    if body!=join_body(group_id,sender,ext['ticket'],str(inviter)):raise ValueError('加群业务体无效')
    return group_id


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class Client:
    def __init__(self,sender,*,provider=None,exchange=None,opener=None):
        self.sender=sender;self.provider=provider or uid_session.Provider()
        self.exchange=exchange or uid_transport.request;self.evidence=[]
        uid_inbox._identity(sender,self.provider,self.exchange,self.evidence)
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def read(self,operation,data,sec):
        owner(sec);session=self.provider.current()
        if session['sender_uid']!=self.sender:raise ValueError('当前账号已经改变')
        headers={'Cookie':session['identity_cookie'],'User-Agent':session['headers']['user-agent'],
                 'Referer':'https://www.douyin.com/user/'+sec,'Accept':'application/json'}
        params={'aid':'6383'};body=None
        if operation=='list':params.update(data)
        elif operation=='verify':
            headers['Content-Type']='application/x-www-form-urlencoded;';body=urllib.parse.urlencode(data).encode()
        else:raise ValueError('公开群读取操作无效')
        request=urllib.request.Request('https://www.douyin.com'+PATHS[operation]+'?'+urllib.parse.urlencode(params),headers=headers,data=body)
        try:
            with self.opener.open(request,timeout=15) as response:
                raw=response.read(1048577)
                if response.status!=200 or len(raw)>1048576 or 'json' not in response.headers.get('Content-Type',''):
                    raise ValueError('invalid_response')
                result=json.loads(raw)
        except Exception:raise ValueError('公开群 HTTP 读取失败；未重试') from None
        if not isinstance(result,dict) or type(result.get('status_code')) is not int or result['status_code']!=0:
            raise ValueError('平台未通过公开群请求；未重试')
        self.evidence.append(dict(operation=operation,http_status=200,response_bytes=len(raw),response_sha256=hashlib.sha256(raw).hexdigest()))
        return result

    def catalog(self,sec):
        value=self.read('list',dict(sec_owner_uid=owner(sec),optional_group_types='["13","18"]',ext='{"im_group_list_other_sort":"2"}'),sec)
        if 'group_list' not in value:raise ValueError('公开群目录结构已变化')
        rows=[] if value['group_list'] is None else value['group_list']
        if not isinstance(rows,list) or len(rows)>200:raise ValueError('公开群目录超出范围')
        result=[]
        for row in rows:
            if not isinstance(row,dict):raise ValueError('公开群记录无效')
            gid=wire.numeric_uid(row.get('group_id'))
            if type(row.get('status')) is not int or type(row.get('group_member_count')) is not int or row['group_member_count']<0:raise ValueError('公开群状态无效')
            if not isinstance(row.get('group_name'),str) or not row['group_name']:raise ValueError('公开群名称缺失')
            if not isinstance(row.get('group_desc') or '',str):raise ValueError('公开群描述无效')
            result.append(dict(group_id=gid,owner_sec_uid=sec,name=row['group_name'],description=row.get('group_desc') or '',
                               participants=row['group_member_count'],list_status=row['status'],entry_limit=row.get('entry_limit') or '',
                               category=str(row.get('group_category') or '')))
        if len({r['group_id'] for r in result})!=len(result):raise ValueError('公开群目录重复')
        return result

    def verify(self,candidate):
        gid=wire.numeric_uid(candidate['group_id']);sec=owner(candidate['owner_sec_uid'])
        value=self.read('verify',dict(group_id=gid,enter_from='others_group_list',enter_method='default_enter_method',
                       secret_type='50',ext='{"group_reserve_v2_ab":"2","join_source":"50"}'),sec)
        check=value.get('data',{}).get('verification',{});data=check.get('data',{})
        if (not isinstance(data,dict) or data.get('conversation_id')!=gid or data.get('conversation_short_id')!=gid or
                data.get('group_owner_info',{}).get('sec_owner_id')!=sec):raise ValueError('公开群核验标识不一致')
        result=dict(group_id=gid,owner_sec_uid=sec,name=data.get('group_name') or '',description=data.get('group_desc') or '',
                    participants=data.get('group_member_count'),code=check.get('status_code'),category=data.get('group_category'),
                    question=data.get('group_audit_question') or '',entry_limit=data.get('entry_limit') or [],
                    join_allowance=data.get('ext',{}).get('join_allowance'),ticket=data.get('ticket'),inviter=str(data.get('inviter_id') or ''))
        if type(result['code']) is not int or type(result['participants']) is not int or not isinstance(result['entry_limit'],list):
            raise ValueError('公开群核验状态无效')
        return result

    def join(self,verification):
        if (verification.get('code') not in (0,7602) or str(verification.get('category'))!='2' or
                verification.get('question') or verification.get('join_allowance') not in ('-1','1') or
                any(not isinstance(v,dict) or v.get('status')!=1 for v in verification.get('entry_limit',[None]))):
            raise ValueError('公开群条件未通过，未提交申请')
        gid=verification['group_id'];body=join_body(gid,self.sender,verification['ticket'],verification['inviter'])
        prepared=self.provider.prepare('group_join',body,dict(sender_uid=self.sender,command=650))
        sequence=uid_transport.validate_envelope(prepared,650,body)
        status,mime,raw=self.exchange('group_join',prepared)
        proof=dict(http_status=status,response_bytes=len(raw),response_sha256=hashlib.sha256(raw).hexdigest())
        if status!=200 or 'protobuf' not in mime.lower():raise ValueError('加群响应未确认；禁止自动重发')
        root=wire.decode(raw)
        if (wire.one(root,1,0)!=650 or wire.one(root,2,0)!=sequence or wire.one(root,13,0)!=int(self.sender)
                or wire.one(root,5,0,0)!=0):raise ValueError('加群响应关联不一致；禁止自动重发')
        proof['platform_code']=wire.one(root,3,0,0)
        if proof['platform_code']!=0:return dict(status='rejected',proof=proof)
        if wire.text(root,4)!='OK':raise ValueError('加群平台状态未确认；禁止自动重发')
        outer=wire.decode(wire.one(root,6,2,b''))
        if set(outer)!={650}:raise ValueError('加群响应结构未确认；禁止自动重发')
        result=wire.decode(wire.one(outer,650,2));proof['business_code']=wire.one(result,3,0,0)
        check=wire.text(result,6)
        try:code=json.loads(check).get('status_code') if check else None
        except (ValueError,AttributeError):code=None
        if type(code) is int:proof['check_code']=code
        status='pending' if code in (7601,10014,7820) else 'accepted' if proof['business_code']==0 and not result.get(2) else 'rejected'
        return dict(status=status,proof=proof)
