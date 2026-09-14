"""Durable SMS verification outcomes; never stores codes or relay payloads."""
from datetime import datetime,timezone
import json
import math
import re
import sqlite3
import clubops as app

SCHEMA='''CREATE TABLE IF NOT EXISTS login_verification_metrics (
 job_id TEXT PRIMARY KEY, account TEXT NOT NULL, encountered_at TEXT NOT NULL,
 submitted_at TEXT, passed_at TEXT, time_basis TEXT NOT NULL
);'''


def stamp(value):
    if type(value) not in (int,float) or not math.isfinite(value):return None
    try:return datetime.fromtimestamp(value,timezone.utc).isoformat()
    except (ValueError,OSError,OverflowError):return None


def save(c,row,*,historical=False):
    if row.get('kind')!='login' or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',str(row.get('id',''))):return
    phase=row.get('status')
    used=row.get('sms_step_used') is True or row.get('code_filled') is True or phase in ('waiting_sms','code_received','code_filled')
    if not used:return
    created=stamp(row.get('created_at'));updated=stamp(row.get('updated_at'))
    if not created or not updated:return
    account=str(row.get('account',''))
    if not re.fullmatch(r'[0-9]{1,32}',account):return
    submitted=stamp(row.get('sms_submitted_at'))
    filled=row.get('code_filled') is True or phase=='code_filled'
    if not submitted and filled:submitted=updated
    passed=(filled and row.get('identity_status')=='identity_verified'
            and row.get('preparation_status')=='session_ready'
            and phase in ('completed','resuming','resume_pending'))
    c.execute('''INSERT INTO login_verification_metrics VALUES(?,?,?,?,?,?)
      ON CONFLICT(job_id) DO UPDATE SET submitted_at=COALESCE(login_verification_metrics.submitted_at,excluded.submitted_at),
      passed_at=COALESCE(login_verification_metrics.passed_at,excluded.passed_at)''',
      (row['id'],account,created,submitted,updated if passed else None,
       'historical_result_time' if historical and not row.get('sms_submitted_at') else 'observed_submission_time'))


def record(row):
    try:
        with app.db() as c:save(c,row)
    except sqlite3.Error:
        # The existing login history retains the metadata for migration/recovery.
        # Metrics must not turn a successful account recovery into a failed login.
        return False
    return True


def backfill(c,mode):
    if mode!='live':return
    path=app.DATA_DIR/'private/login-recovery/jobs.json'
    try:
        if path.is_symlink() or path.stat().st_size>32768:return
        rows=json.loads(path.read_text('utf-8'))
        if isinstance(rows,list):
            for row in rows:
                if isinstance(row,dict):save(c,row,historical=True)
    except (OSError,ValueError,TypeError):return
