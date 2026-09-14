"""Finite retries for pre-response connection failures on the same account."""
import time
import uid_bootstrap
import collection_session_refresh as refresh

def retryable(result):
    return (result.get('status')=='http_failed' and type(result.get('http_attempts')) is int and result['http_attempts']==1
        and result.get('transport_error') in ('connection_failed','timeout')
        and result.get('transport_phase') in ('connect','request','response_headers')
        and result.get('http_status') is None and not result.get('verification_indicated'))

def probe(value,*,cancelled=lambda:False,report=lambda *_:None,exchange=None,sleep=time.sleep):
    invoke=exchange or uid_bootstrap.probe
    for attempt in range(1,4):
        if cancelled():raise refresh.RefreshError('cancelled')
        result=invoke(dict(value))
        safe={k:result[k] for k in refresh.SAFE_FIELDS if k in result}
        report('identity_attempt',dict(attempt=attempt,**safe))
        if cancelled():raise refresh.RefreshError('cancelled',safe)
        if not retryable(result) or attempt==3:
            return dict(result,http_attempts=attempt if result.get('http_attempts') else attempt-1)
        # Short interruptible backoff; no new account, endpoint or credentials.
        for _ in range((1,3)[attempt-1]*10):
            if cancelled():raise refresh.RefreshError('cancelled',safe)
            sleep(.1)

def legacy_failure(c,session):
    """Admit only the old single-attempt failure to the existing repair API."""
    if not session or session['status']!='failed' or not session['finished_at'] or session['observed'] or session['frames']:
        return False
    import json
    rows=c.execute('SELECT status,detail FROM live_session_events WHERE session_id=? ORDER BY id',(session['id'],)).fetchall()
    if len(rows)!=2 or [r['status'] for r in rows]!=['preflight','failed']:return False
    try:
        proof=json.loads(rows[0]['detail'])
        return (proof.get('version')=='live-preflight-v1' and
            (proof.get('stage'),proof.get('reason')) in [('identity','identity_checked'),('local_session','identity_unverified')]
            and retryable(proof))
    except (ValueError,TypeError,AttributeError):return False
