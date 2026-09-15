"""Renew only the local reuse lease after one verified read of the original account.

No browser, SMS, IM call, endpoint fallback, credential capture or HTTP retry.
Page/status reads keep using the pure loader and cannot trigger this operation.
"""
import json
import os
import time
import uuid
from datetime import datetime

import collector_http_session as sessions
import runtime
import uid_bootstrap
import uid_session

SAFE_FIELDS=('status','http_attempts','http_status','verification_indicated','transport_error','transport_phase')


class RefreshError(Exception):
    def __init__(self,reason,evidence=None):
        self.reason=reason;self.evidence=evidence or {}
        super().__init__(reason)


def failure_status(reason,evidence):
    if reason=='cancelled':return 'cancelled'
    if evidence.get('verification_indicated'):return 'needs_verification'
    if evidence.get('http_status')==429:return 'rate_limited'
    if evidence.get('http_status')==403:return 'access_denied'
    if (reason in ('missing_session','account_mismatch') or evidence.get('http_status')==401
            or evidence.get('status') in ('needs_login','account_mismatch')):return 'needs_login'
    return 'failed'


def ensure(directory,binding,*,probe=None,cancelled=lambda:False,minimum_valid_seconds=0):
    if type(minimum_valid_seconds) is not int or not 0<=minimum_valid_seconds<=300:
        raise RefreshError('invalid_lease_margin')
    file=sessions.path(directory)
    try:
        with runtime.data_lock(file.parent):
            raw=file.read_bytes();value=sessions.load(directory,check_age=False)
            if value.get('account')!=binding['account_id'] or value.get('sender_uid')!=binding['sender_uid']:
                raise RefreshError('account_mismatch')
            verified=value.get('last_verified_at',value['captured_at'])
            if time.time()-verified<=sessions.MAX_AGE-minimum_valid_seconds:return value,None
            if cancelled():raise RefreshError('cancelled')
            proof=(probe or uid_bootstrap.probe)(dict(expected_account=binding['account_id'],
                cookie=sessions.cookie_header(value,'identity',check_age=False),user_agent=value['user_agent']))
            safe={k:proof[k] for k in SAFE_FIELDS if k in proof}
            if cancelled():raise RefreshError('cancelled',safe)
            if (proof.get('status')!='identity_verified' or proof.get('sender_uid')!=binding['sender_uid']
                    or proof.get('verification_indicated')):
                raise RefreshError('identity_unverified',safe)
            if file.is_symlink() or file.read_bytes()!=raw:raise RefreshError('session_changed')
            value={**value,'last_verified_at':max(time.time(),value['captured_at'])}
            sessions.validate(value)
            encrypted=sessions.MAGIC+uid_session.crypt(json.dumps(value,separators=(',',':')).encode())
            temporary=file.with_name('session-revalidation-'+uuid.uuid4().hex+'.tmp')
            try:
                with temporary.open('xb') as stream:
                    stream.write(encrypted);stream.flush();os.fsync(stream.fileno())
                if cancelled():raise RefreshError('cancelled',safe)
                sessions.replace_status(temporary,file)
            finally:
                if temporary.exists():temporary.unlink()
            return value,proof
    except RefreshError:raise
    except FileNotFoundError:raise RefreshError('missing_session') from None
    except Exception as exc:raise RefreshError('local_session_error',dict(error_type=type(exc).__name__)) from None


def legacy_expiry(c,session):
    """Recognize the old pre-diagnostic 12h guard, without rewriting its failure.

    It ran before HTTP/browser startup. Only a zero-data, immediate failure,
    with no other diagnostic and an intact matching aged snapshot is eligible.
    The next worker must independently verify identity before using it.
    """
    if not session or session['status']!='failed' or not session['finished_at'] or session['observed'] or session['frames']:return False
    events=c.execute('SELECT status FROM live_session_events WHERE session_id=?',(session['id'],)).fetchall()
    if len(events)!=1 or events[0][0]!='failed':return False
    try:
        started=datetime.fromisoformat(session['started_at']).timestamp()
        elapsed=datetime.fromisoformat(session['finished_at']).timestamp()-started
        if not 0<=elapsed<=1:return False
        binding=json.loads(session['config'])['collection_account']
        import collection_accounts
        value=sessions.load(collection_accounts.directory(binding),check_age=False)
        return bool(value.get('account')==binding['account_id'] and value.get('sender_uid')==binding['sender_uid']
            and value['captured_at']+sessions.MAX_AGE<=started)
    except (ValueError,KeyError,TypeError,OSError):return False
