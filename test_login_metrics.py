import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
import clubops as app
import daily_dashboard as dashboard
import login_metrics as metrics

T=lambda value:datetime.fromisoformat(value).timestamp()

class MetricsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        p=patch.object(app,'DATA_DIR',Path(temp.name));p.start();self.addCleanup(p.stop)
        app.init()
        self.row=dict(id='synthetic-login-1',kind='login',account='12345678',created_at=T('2026-09-13T23:58:00+08:00'),updated_at=T('2026-09-13T23:59:00+08:00'),status='waiting_sms')

    def test_sms_forwarded_not_passed_cross_midnight_single_submission(self):
        r=self.row
        metrics.record(r);metrics.record(dict(r,status='code_received'))
        view=dashboard.snapshot(reference=datetime.fromisoformat('2026-09-13T23:59:59+08:00'))
        sms=next(t for t in view['captcha']['types'] if t['key']=='sms')
        self.assertEqual(sms['encounters'],1);self.assertEqual(sms['submitted'],0);self.assertIsNone(sms['pass_rate'])
        r.update(updated_at=T('2026-09-14T00:01:00+08:00'),status='code_filled',sms_submitted_at=T('2026-09-14T00:01:00+08:00'))
        metrics.record(r)
        r.update(status='completed',code_filled=True,sms_step_used=True,identity_status='identity_verified',preparation_status='session_ready',updated_at=T('2026-09-14T00:02:00+08:00'))
        metrics.record(r);metrics.record(r)
        view=dashboard.snapshot(reference=datetime.fromisoformat('2026-09-14T12:00:00+08:00'))
        sms=next(t for t in view['captcha']['types'] if t['key']=='sms')
        self.assertEqual((sms['submitted'],sms['passed'],sms['pass_rate']),(1,1,100.0))
        self.assertEqual(view['series']['captcha_rate_sms'][-2:],[None,100.0])

    def test_phone_test_cookie_only_login_and_mismatched_identity_excluded(self):
        r=dict(self.row,status='completed',code_filled=True,sms_step_used=True,identity_status='identity_verified',preparation_status='session_ready')
        metrics.record(dict(r,kind='phone_test'))
        metrics.record(dict(r,id='synthetic-cookie-only',code_filled=False,sms_step_used=False))
        metrics.record(dict(r,identity_status='identity_failed'))
        view=dashboard.snapshot(reference=datetime.fromisoformat('2026-09-14T12:00:00+08:00'))
        self.assertEqual(view['captcha']['all_time']['submitted'],1)
        self.assertEqual(view['captcha']['all_time']['passed'],0)
        self.assertEqual(view['captcha']['all_time']['unknown'],1)
        with app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM login_verification_metrics').fetchone()[0],1)

    def test_only_allowlisted_metadata_retained_and_unconfirmed_rate_stays_null(self):
        metrics.record(dict(self.row,status='code_filled',code='123456',secret='should-never-be-stored'))
        with app.db() as c:
            raw=json.dumps(dict(c.execute('SELECT * FROM login_verification_metrics').fetchone()))
            self.assertNotIn('123456"',raw);self.assertNotIn('secret',raw)
        view=dashboard.snapshot(reference=datetime.fromisoformat('2026-09-13T23:59:59+08:00'))
        self.assertIsNone(view['series']['captcha_rate_sms'][-1])
        self.assertEqual(next(t for t in view['captcha']['types'] if t['key']=='sms')['rate_status'],'unconfirmed')
        self.assertTrue(all(v is None for v in view['series']['captcha_rate_slider']))

    def test_sms_backfill_idempotent_and_explicit_about_historical_time(self):
        p=app.DATA_DIR/'private/login-recovery/jobs.json';p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps([dict(self.row,status='completed',code_filled=True,identity_status='identity_verified',preparation_status='session_ready')]),'utf-8')
        with app.db() as c:
            metrics.backfill(c,'live');metrics.backfill(c,'live')
            row=c.execute('SELECT * FROM login_verification_metrics').fetchone()
            self.assertEqual(row['time_basis'],'historical_result_time')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM login_verification_metrics').fetchone()[0],1)
