"""Public profile group discovery and self-only join, using the saved HTTP session.

Protocol source: the observed official pcim SDK, including applyJoinGroup.
This module cannot send a group message or invite another participant.
"""
import hashlib
import json
import re
import urllib.parse
import urllib.request
import urllib.error
from http.cookies import SimpleCookie

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


def follow_requirement(value):
    """Only an explicit free follow of this group's verified owner qualifies."""
    missing=[r for r in value.get('entry_limit',[]) if isinstance(r,dict) and r.get('status')!=1]
    if not missing or str(value.get('category'))!='2' or value.get('code') not in (0,7602):return None
    targets=set();days=[]
    for row in missing:
        ext=row.get('entry_limit_ext') or {}
        if ext.get('entry_type')!=1:return None
        parts=ext.get('entry_detail_template') or []
        if len(parts)!=1:return None
        extra=parts[0].get('extra') or {}
        uid=str(extra.get('group_owner_uid') or '')
        if uid!=value.get('inviter') or extra.get('follow_type')!='0':return None
        try:wire.numeric_uid(uid);day=int(extra.get('follow_days','0'))
        except (ValueError,TypeError):return None
        if not 0<=day<=30:return None
        targets.add(uid);days.append(day)
    if len(targets)!=1:return None
    return dict(uid=targets.pop(),sec_uid=value['owner_sec_uid'],days=max(days))


def join_body(group_id,sender,ticket,inviter,answer=''):
    for value in (group_id,sender,inviter):wire.numeric_uid(value)
    # The observed official applyJoinGroup wrapper defaults ticket to "".
    # Public verification legitimately returns an empty string; keep that exact
    # server value, while rejecting absent/non-string/oversized values.
    if not isinstance(ticket,str) or len(ticket)>4096:raise ValueError('加群凭据无效')
    ext={'invitation':json.dumps({'invitee':{'source_app_id':6383},'invitor':{'im_user_id':int(inviter)},'source_type':50},separators=(',',':')),
         'source_type':'50','ticket':ticket}
    if not isinstance(answer,str) or len(answer)>500:raise ValueError('入群回答无效')
    if answer:ext['group_audit_answer']=answer
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
    required={'invitation','source_type','ticket'}
    if not required<=set(ext) or set(ext)-required-{'group_audit_answer'} or ext['source_type']!='50':raise ValueError('加群来源无效')
    invitation=json.loads(ext['invitation']);inviter=invitation.get('invitor',{}).get('im_user_id')
    if type(inviter) is not int:raise ValueError('邀请来源无效')
    if body!=join_body(group_id,sender,ext['ticket'],str(inviter),ext.get('group_audit_answer','')):raise ValueError('加群业务体无效')
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

    def follow_owner(self,verification,*,browser=True):
        """Meet a verified public-group free-follow requirement; no paid actions."""
        from group_profiles import profiles
        target=follow_requirement(verification)
        self.follow_evidence=dict(submission_started=False)
        if not target or target['uid']==self.sender:raise ValueError('没有可自动处理的免费关注条件')
        result=profiles(self.sender,[target],client=self)
        if len(result['profiles'])!=1:raise ValueError('群主身份未核对')
        if browser:
            from group_follow_browser import follow
            return follow(self.sender,target)
        session=self.provider.current()
        if session['sender_uid']!=self.sender:raise ValueError('账号已经改变')
        # Match the official secsdk 1.2.22 CSRF handshake for authenticated POSTs.
        # Tokens stay in memory, are never logged, and no downgrade is used.
        headers={'Cookie':session['identity_cookie'],'User-Agent':session['headers']['user-agent'],
                 'Referer':'https://www.douyin.com/user/'+target['sec_uid'],'Origin':'https://www.douyin.com'}
        handshake=urllib.request.Request('https://www.douyin.com/aweme/v1/web/commit/follow/user/',method='HEAD',
            headers=dict(headers,**{'x-secsdk-csrf-request':'1','x-secsdk-csrf-version':'1.2.22'}))
        with self.opener.open(handshake,timeout=10) as response:
            parts=response.headers.get('x-ware-csrf-token','').split(',')
            if response.status!=200 or len(parts)<2 or parts[0]!='0' or not parts[1] or len(parts[1])>4096 or any(ord(c)<32 for c in parts[1]):
                raise ValueError('关注请求验证未完成，尚未提交')
            headers['x-secsdk-csrf-token']=parts[1]
            for raw_cookie in response.headers.get_all('Set-Cookie',[]) if hasattr(response.headers,'get_all') else []:
                cookies=SimpleCookie();cookies.load(raw_cookie)
                if 'csrf_session_id' in cookies:
                    value=cookies['csrf_session_id'].value
                    if not value or len(value)>4096 or any(ord(c)<32 or c==';' for c in value):raise ValueError('请求验证会话无效')
                    kept=[p.strip() for p in headers['Cookie'].split(';') if p.strip() and p.strip().split('=',1)[0]!='csrf_session_id']
                    headers['Cookie']='; '.join(kept+['csrf_session_id='+value])
        headers.update({'x-secsdk-csrf-version':'1.2.22','Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'})
        request=urllib.request.Request('https://www.douyin.com/aweme/v1/web/commit/follow/user/?aid=6383',
            data=urllib.parse.urlencode(dict(user_id=target['uid'],type=1)).encode(),headers=headers)
        self.follow_evidence['submission_started']=True
        try:response=self.opener.open(request,timeout=15)
        except urllib.error.HTTPError as exc:
            self.follow_evidence['http_status']=exc.code
            exc.close()
            if 400<=exc.code<500:return dict(status='rejected',proof=dict(self.follow_evidence))
            raise ValueError('关注响应尚未确认') from None
        with response:
            raw=response.read(1048577)
            self.follow_evidence.update(http_status=response.status,response_bytes=len(raw),response_sha256=hashlib.sha256(raw).hexdigest())
            if response.status!=200 or len(raw)>1048576 or 'json' not in response.headers.get('Content-Type',''):
                raise ValueError('关注结果尚未确认')
        value=json.loads(raw)
        if not isinstance(value,dict) or type(value.get('status_code')) is not int:raise ValueError('关注响应无效')
        self.follow_evidence['platform_code']=value['status_code']
        return dict(status='accepted' if value['status_code']==0 else 'rejected',proof=dict(self.follow_evidence))

    def join(self,verification):
        self.join_evidence = dict(phase='conditions', submission_started=False)
        if (verification.get('code') not in (0,7602) or str(verification.get('category'))!='2' or
                verification.get('question') and not verification.get('group_audit_answer','').strip() or verification.get('join_allowance') not in ('-1','1') or
                any(not isinstance(v,dict) or v.get('status')!=1 for v in verification.get('entry_limit',[None]))):
            raise ValueError('公开群条件未通过，未提交申请')
        self.join_evidence['phase']='prepare'
        gid=verification['group_id'];body=join_body(gid,self.sender,verification['ticket'],verification['inviter'],verification.get('group_audit_answer',''))
        prepared=self.provider.prepare('group_join',body,dict(sender_uid=self.sender,command=650))
        sequence=uid_transport.validate_envelope(prepared,650,body)
        self.join_evidence.update(phase='transport',submission_started=True)
        status,mime,raw=self.exchange('group_join',prepared)
        proof=dict(http_status=status,response_bytes=len(raw),response_sha256=hashlib.sha256(raw).hexdigest())
        self.join_evidence.update(proof,phase='response')
        if status!=200 or 'protobuf' not in mime.lower():raise ValueError('加群响应未确认；禁止自动重发')
        root=wire.decode(raw)
        if (wire.one(root,1,0)!=650 or wire.one(root,2,0)!=sequence or wire.one(root,13,0)!=int(self.sender)
                or wire.one(root,5,0,0)!=0):raise ValueError('加群响应关联不一致；禁止自动重发')
        self.join_evidence['phase']='platform_result'
        proof['platform_code']=wire.one(root,3,0,0)
        self.join_evidence['platform_code']=proof['platform_code']
        if proof['platform_code']!=0:return dict(status='rejected',proof=proof)
        if wire.text(root,4)!='OK':raise ValueError('加群平台状态未确认；禁止自动重发')
        outer=wire.decode(wire.one(root,6,2,b''))
        if set(outer)!={650}:raise ValueError('加群响应结构未确认；禁止自动重发')
        result=wire.decode(wire.one(outer,650,2));proof['business_code']=wire.one(result,3,0,0)
        self.join_evidence.update(phase='business_result',business_code=proof['business_code'])
        check=wire.text(result,6)
        try:code=json.loads(check).get('status_code') if check else None
        except (ValueError,AttributeError):code=None
        if type(code) is int:proof['check_code']=code
        status='pending' if code in (7601,10014,7820) else 'accepted' if proof['business_code']==0 and not result.get(2) else 'rejected'
        return dict(status=status,proof=proof)
