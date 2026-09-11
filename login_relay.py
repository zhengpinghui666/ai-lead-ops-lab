"""Bounded HTTPS transport and endpoint-bound DPAPI pairing for SMS login."""
import http.client
import json
import math
import os
import re
import secrets
import ssl
import threading
import uuid
from urllib.parse import urlsplit
from email.utils import parsedate_to_datetime
import clubops as app
import runtime
from uid_session import crypt

MAGIC = b'CLUBOPS-LOGIN-PAIRING-1\n'
GUARD = threading.RLock()

def network():
    """Explicit relay-only connection setting; never inherit ambient proxies."""
    target=app.DATA_DIR/'login-relay-network.json'
    try:
        if target.is_symlink() or target.stat().st_size>4096:raise ValueError('中转网络配置无效')
        value=json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError:return {'mode':'direct','http_proxy':''}
    if not isinstance(value,dict) or set(value)!={'http_proxy'} or not isinstance(value['http_proxy'],str):
        raise ValueError('中转网络配置无效')
    proxy=value['http_proxy']
    if not proxy:return {'mode':'direct','http_proxy':''}
    try:
        u=urlsplit(proxy)
        if (u.scheme!='http' or u.hostname not in ('127.0.0.1','::1') or not u.port
                or u.username or u.password or u.path or u.query or u.fragment
                or any(c.isspace() for c in proxy)):
            raise ValueError()
    except ValueError:raise ValueError('中转代理须为本机 HTTP 代理，不含凭证、路径或其他主机') from None
    return {'mode':'local_proxy','http_proxy':proxy}

def connection(url,timeout=8):
    destination=urlsplit(origin(url)).hostname
    proxy=network()['http_proxy'];context=ssl.create_default_context()
    if proxy:
        u=urlsplit(proxy)
        result=http.client.HTTPSConnection(u.hostname,u.port,timeout=timeout,context=context)
        # CONNECT resolves the destination at the proxy; TLS still authenticates
        # the exact relay hostname end to end. No HTTP or direct fallback.
        result.set_tunnel(destination,443)
        return result
    return http.client.HTTPSConnection(destination,443,timeout=timeout,context=context)

def origin(value):
    if not isinstance(value,str) or len(value)>250 or any(c.isspace() for c in value):
        raise ValueError('短信中转地址无效')
    try:
        u=urlsplit(value.rstrip('/'))
        port=u.port
    except ValueError:
        raise ValueError('短信中转地址无效') from None
    if (u.scheme!='https' or not u.hostname or not u.hostname.isascii() or port not in (None,443)
            or u.username or u.password or u.path or u.query or u.fragment):
        raise ValueError('短信中转地址须为独立 HTTPS 主机，不含路径或密钥')
    if not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',label)
               for label in u.hostname.split('.')):
        raise ValueError('短信中转主机名无效')
    return 'https://'+u.hostname.lower()

def path():
    return app.DATA_DIR/'private/login-recovery/pairing.dpapi'

def write(value):
    target=path();target.parent.mkdir(parents=True,exist_ok=True)
    raw=MAGIC+crypt(json.dumps(value).encode())
    temp=target.with_name('.pairing-'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,target)
    finally:
        temp.unlink(missing_ok=True)

def load():
    target=path()
    if target.is_symlink():raise ValueError('配对文件无效')
    with target.open('rb') as stream:raw=stream.read(16385)
    if len(raw)>16384 or not raw.startswith(MAGIC):raise ValueError('配对文件无效')
    value=json.loads(crypt(raw[len(MAGIC):],decrypt=True))
    if not isinstance(value,dict) or set(value)!={'origin','backend_token','phone_token'}:
        raise ValueError('配对文件无效')
    for key in ('backend_token','phone_token'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{40,128}',value.get(key,'')):raise ValueError('配对文件无效')
    if value['backend_token']==value['phone_token'] or not isinstance(value['origin'],str):
        raise ValueError('配对文件无效')
    if value['origin'] and origin(value['origin'])!=value['origin']:raise ValueError('配对文件无效')
    return value

def provision():
    with GUARD,runtime.data_lock(path().parent):
        if path().exists():return load()
        value={'origin':'','backend_token':secrets.token_urlsafe(32),'phone_token':secrets.token_urlsafe(32)}
        write(value);return value

def bind(url):
    url=origin(url)
    with GUARD,runtime.data_lock(path().parent):
        value=load()
        if value['origin'] and value['origin']!=url:
            raise ValueError('配对密钥已绑定其他中转地址；请先重新配对')
        write({**value,'origin':url})


def state():
    try:
        value=load()
        return {'paired':True,'origin':value['origin'],'ready':bool(value['origin']),'network':network()}
    except (OSError,ValueError,TypeError):
        return {'paired':False,'origin':'','ready':False}


def phone_configuration():
    """Only return from an explicit, CSRF-protected local pairing action."""
    value=load()
    if not value['origin']:raise ValueError('请先部署并绑定短信中转地址')
    return {'origin':value['origin'],'authorization':'Bearer '+value['phone_token']}

class RelayError(Exception):
    def __init__(self,message,code='unavailable',*,reason='unavailable',route=None,stage=None,http_status=None):
        super().__init__(message)
        self.code=code
        self.reason,self.route,self.stage,self.http_status=reason,route,stage,http_status

    def diagnostic(self):
        """Only fixed categories and a numeric status may enter job history."""
        value={'reason':self.reason if self.reason in {'http','timeout','tls','network','invalid_response','response_too_large'} else 'unavailable'}
        if self.route in {'health','login','take','cancel'}:value['route']=self.route
        if self.stage in {'request','response','body','decode','lifetime'}:value['stage']=self.stage
        if type(self.http_status) is int and 100<=self.http_status<=599:value['http_status']=self.http_status
        if self.code in {'unauthorized','no_matching_login','login_in_progress','relay_unavailable','not_configured'}:value['code']=self.code
        return value


def remaining_seconds(value, date_header):
    """Bound the lifetime against the HTTPS server clock, never the PC clock.

    HTTP Date has one-second precision; remove that whole second conservatively.
    The caller anchors this duration before the request so transit time cannot
    extend the remote task's 180-second lifetime.
    """
    try:
        expires=value.get('expires_at')
        if (type(expires) not in (int,float) or not math.isfinite(expires)
                or not isinstance(date_header,str) or len(date_header)>128):
            raise ValueError()
        server_time=parsedate_to_datetime(date_header)
        if server_time.tzinfo is None:raise ValueError()
        remaining=expires/1000-server_time.timestamp()
        if not 0<remaining<=181:raise ValueError()
        return max(0.0,remaining-1)
    except Exception:
        raise RelayError('无法核对云端登录任务有效期',reason='invalid_response') from None


class Relay:
    def __init__(self,url):
        self.origin=origin(url);value=load()
        if value['origin']!=self.origin:raise ValueError('中转地址与配对密钥不匹配')
        self.token=value['backend_token']
    def call(self,route,data=None):
        if route not in ('health','login','take','cancel'):raise ValueError('中转操作无效')
        if route=='health':
            if data is not None:raise ValueError('中转健康检查不接受内容')
        else:
            fields={'id','account'} if route=='login' else {'id'}
            if (not isinstance(data,dict) or set(data)!=fields or not isinstance(data.get('id'),str)
                    or not re.fullmatch(r'[a-f0-9]{32}',data['id'])):
                raise ValueError('登录任务参数无效')
            if route=='login' and (not isinstance(data['account'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{2,64}',data['account'])):
                raise ValueError('登录账号无效')
        channel=connection(self.origin)
        stage,status='request',None
        try:
            payload=None if data is None else json.dumps(data).encode()
            channel.request('GET' if payload is None else 'POST','/v1/'+route,body=payload,
                headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json','Accept':'application/json'})
            stage='response';response=channel.getresponse();status=response.status
            stage='body';raw=response.read(4097)
            if len(raw)>4096:raise RelayError('中转响应超过大小限制',reason='response_too_large')
            if response.status not in (200,201):
                allowed={'unauthorized','no_matching_login','login_in_progress','relay_unavailable','not_configured'}
                try:
                    candidate=json.loads(raw).get('error')
                    code=candidate if candidate in allowed else 'unavailable'
                except (ValueError,TypeError,AttributeError):code='unavailable'
                raise RelayError('中转请求未成功（HTTP '+str(response.status)+'）',code,reason='http')
            stage='decode'
            value=json.loads(raw)
            if not isinstance(value,dict):raise RelayError('中转响应格式无效',reason='invalid_response')
            if route=='login':
                stage='lifetime'
                value['expires_in_seconds']=remaining_seconds(value,response.getheader('Date'))
            return value
        except RelayError as exc:
            exc.route,exc.stage,exc.http_status=route,stage,status
            raise
        except Exception as exc:
            reason='timeout' if isinstance(exc,TimeoutError) else 'tls' if isinstance(exc,ssl.SSLError) else 'invalid_response' if stage in {'decode','lifetime'} else 'network'
            raise RelayError('短信中转连接失败；请检查地址、网络和配对',reason=reason,route=route,stage=stage,http_status=status) from None
        finally:channel.close()
