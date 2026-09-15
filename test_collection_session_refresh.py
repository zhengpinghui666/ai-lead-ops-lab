"""Synthetic credential lease checks; no platform or production file access."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import collector_http_session as sessions
import collection_session_refresh as refresh
import uid_session
from test_collector_http import session


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.directory=Path(self.tmp.name)
        self.crypto=patch.object(uid_session,'crypt',side_effect=lambda raw,**kw:raw[::-1]);self.crypto.start()
        self.value=session();self.value['captured_at']-=sessions.MAX_AGE+30
        self.binding=dict(account_id=self.value['account'],sender_uid=self.value['sender_uid'],storage='isolated')
        self.file=sessions.path(self.directory);self.file.parent.mkdir(parents=True)
        self.save(self.value);self.before=self.file.read_bytes()
    def tearDown(self):self.crypto.stop();self.tmp.cleanup()
    def save(self,value):self.file.write_bytes(sessions.MAGIC+json.dumps(value).encode()[::-1])
    def verified(self,**extra):return dict(status='identity_verified',sender_uid=self.value['sender_uid'],expected_account=self.value['account'],http_attempts=1,http_status=200,**extra)

    def test_expired_requires_one_identity_check_and_preserves_capture_and_cookies(self):
        with self.assertRaises(ValueError):sessions.load(self.directory)
        probe=Mock(return_value=self.verified())
        value,proof=refresh.ensure(self.directory,self.binding,probe=probe)
        self.assertEqual(probe.call_count,1);self.assertEqual(proof['status'],'identity_verified')
        self.assertEqual({k:v for k,v in value.items() if k!='last_verified_at'},self.value)
        self.assertGreater(value['last_verified_at'],value['captured_at'])
        self.assertEqual(sessions.load(self.directory),value)
        self.assertEqual(sessions.status(self.directory)['expires_at'],value['last_verified_at']+sessions.MAX_AGE)
        self.assertNotIn(b'TEST_ONLY_SECRET',self.file.read_bytes());self.assertEqual(list(self.file.parent.glob('*.tmp')),[])

    def test_fresh_lease_does_not_do_io_to_the_platform(self):
        self.value['last_verified_at']=time.time();self.save(self.value)
        probe=Mock(side_effect=AssertionError('no request'))
        value,proof=refresh.ensure(self.directory,self.binding,probe=probe)
        self.assertIsNone(proof);probe.assert_not_called();self.assertEqual(value,self.value)

    def test_batch_margin_revalidates_before_expiry_without_changing_capture(self):
        self.value['last_verified_at']=time.time()-sessions.MAX_AGE+20;self.save(self.value)
        probe=Mock(return_value=self.verified())
        value,proof=refresh.ensure(self.directory,self.binding,probe=probe,minimum_valid_seconds=300)
        self.assertEqual(probe.call_count,1);self.assertEqual(proof['status'],'identity_verified')
        self.assertEqual(value['captured_at'],self.value['captured_at'])
        self.assertEqual(value['cookies'],self.value['cookies'])
        self.assertGreater(value['last_verified_at'],self.value['last_verified_at'])
        refresh.ensure(self.directory,self.binding,probe=probe,minimum_valid_seconds=300)
        self.assertEqual(probe.call_count,1)

    def test_invalid_margin_never_probes_or_writes(self):
        for margin in (-1,301,True,1.5):
            probe=Mock()
            with self.subTest(margin=margin),self.assertRaises(refresh.RefreshError):
                refresh.ensure(self.directory,self.binding,probe=probe,minimum_valid_seconds=margin)
            probe.assert_not_called();self.assertEqual(self.file.read_bytes(),self.before)

    def test_rejected_unrecognized_and_wrong_identity_do_not_extend(self):
        for result in [dict(status='http_rejected',http_status=429),dict(status='unrecognized_response'),
            dict(status='needs_login'),self.verified(verification_indicated=True),dict(status='identity_verified',sender_uid='987654321')]:
            with self.subTest(result=result):
                probe=Mock(return_value=result)
                with self.assertRaises(refresh.RefreshError):refresh.ensure(self.directory,self.binding,probe=probe)
                self.assertEqual(self.file.read_bytes(),self.before);self.assertEqual(probe.call_count,1)

    def test_binding_mismatch_stops_before_any_request(self):
        for binding in [dict(self.binding,account_id='other'),dict(self.binding,sender_uid='987654321')]:
            probe=Mock()
            with self.assertRaisesRegex(refresh.RefreshError,'account_mismatch'):refresh.ensure(self.directory,binding,probe=probe)
            probe.assert_not_called();self.assertEqual(self.file.read_bytes(),self.before)

    def test_cancel_before_and_during_identity_never_saves(self):
        probe=Mock()
        with self.assertRaisesRegex(refresh.RefreshError,'cancelled'):refresh.ensure(self.directory,self.binding,probe=probe,cancelled=lambda:True)
        probe.assert_not_called();flag=[False]
        def exchange(_):flag[0]=True;return self.verified()
        with self.assertRaisesRegex(refresh.RefreshError,'cancelled'):refresh.ensure(self.directory,self.binding,probe=exchange,cancelled=lambda:flag[0])
        self.assertEqual(self.file.read_bytes(),self.before)

    def test_replaced_session_is_not_overwritten(self):
        newer=dict(self.value,captured_at=time.time());saved=[]
        def exchange(_):self.save(newer);saved.append(self.file.read_bytes());return self.verified()
        with self.assertRaisesRegex(refresh.RefreshError,'session_changed'):refresh.ensure(self.directory,self.binding,probe=exchange)
        self.assertEqual(self.file.read_bytes(),saved[0])

    def test_malformed_metadata_and_private_exceptions_are_not_reported(self):
        self.value['context']={};self.save(self.value);probe=Mock()
        with self.assertRaises(refresh.RefreshError) as error:refresh.ensure(self.directory,self.binding,probe=probe)
        self.assertEqual(error.exception.reason,'local_session_error');probe.assert_not_called()
        self.file.write_bytes(self.before)
        with self.assertRaises(refresh.RefreshError) as error:refresh.ensure(self.directory,self.binding,probe=Mock(side_effect=ValueError('PRIVATE_SECRET_SENTINEL')))
        self.assertNotIn('PRIVATE_SECRET_SENTINEL',str(error.exception)+json.dumps(error.exception.evidence))
        self.assertEqual(self.file.read_bytes(),self.before)

    def test_invalid_lease_timestamps_are_rejected_even_when_ignoring_age(self):
        for verified in [float('inf'),True,time.time()+120,self.value['captured_at']-1]:
            with self.assertRaises(ValueError):sessions.validate(dict(self.value,last_verified_at=verified),check_age=False)

    def test_second_refresh_uses_renewed_lease_without_another_probe(self):
        probe=Mock(return_value=self.verified())
        refresh.ensure(self.directory,self.binding,probe=probe)
        value,proof=refresh.ensure(self.directory,self.binding,probe=probe)
        self.assertIsNone(proof);self.assertEqual(probe.call_count,1)

    def test_platform_gates_keep_their_specific_stop_reason(self):
        for proof,status in [(dict(http_status=429),'rate_limited'),(dict(http_status=403),'access_denied'),
            (dict(verification_indicated=True),'needs_verification'),(dict(status='account_mismatch'),'needs_login'),
            (dict(status='unrecognized_response'),'failed'),(dict(status='http_failed'),'failed')]:
            self.assertEqual(refresh.failure_status('identity_unverified',proof),status)


if __name__=='__main__':unittest.main()
