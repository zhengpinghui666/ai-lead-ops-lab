"""Isolated DPAPI pairing and bounded HTTPS transport; no live requests."""
import json
from email.utils import formatdate
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import clubops as app
import login_relay as relay

ORIGIN='https://relay.example.test'
ID='a'*32

class RelayTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(prefix='clubops-relay-test-')
        self.addCleanup(directory.cleanup)
        p=patch.object(app,'DATA_DIR',Path(directory.name));p.start();self.addCleanup(p.stop)

    def test_pairing_is_stable_encrypted_and_bound_to_one_origin(self):
        value=relay.provision();self.assertEqual(value,relay.provision())
        self.assertNotEqual(value['backend_token'],value['phone_token'])
        for token in (value['backend_token'],value['phone_token']):
            self.assertNotIn(token.encode(),relay.path().read_bytes())
            self.assertNotIn(token,json.dumps(relay.state()))
        self.assertFalse(relay.state()['ready'])
        relay.bind('https://RELAY.EXAMPLE.TEST:443/')
        self.assertTrue(relay.state()['ready']);self.assertEqual(relay.state()['origin'],ORIGIN)
        self.assertEqual(relay.phone_configuration()['authorization'],'Bearer '+value['phone_token'])
        with self.assertRaises(ValueError):relay.bind('https://other.example.test')
        self.assertEqual(relay.load()['origin'],ORIGIN)
        with self.assertRaises(ValueError):relay.Relay('https://other.example.test')

    def test_invalid_origins_and_corrupt_pairing_fail_closed(self):
        for value in ('http://relay.test','https://user:pass@relay.test','https://relay.test/path',
                      'https://relay.test?key=private','https://relay.test#fragment','https://relay.test:444',
                      'https://relay.test:bad','https://relay.test\\evil','https://-bad.test',123):
            with self.subTest(value=value),self.assertRaises(ValueError):relay.origin(value)
        relay.provision();relay.path().write_bytes(b'invalid')
        self.assertFalse(relay.state()['paired'])
        with self.assertRaises(ValueError):relay.provision()

    def ready_client(self):
        relay.provision();relay.bind(ORIGIN);return relay.Relay(ORIGIN)

    def test_explicit_local_proxy_tunnels_exact_host_with_verified_tls(self):
        client=self.ready_client()
        (app.DATA_DIR/'login-relay-network.json').write_text(json.dumps({'http_proxy':'http://127.0.0.1:7897'}))
        with patch('login_relay.http.client.HTTPSConnection') as factory:
            response=factory.return_value.getresponse.return_value
            response.status=200;response.read.return_value=b'{"service":"clubops-login-relay","version":1}'
            client.call('health')
            self.assertEqual(factory.call_args.args,('127.0.0.1',7897))
            self.assertTrue(factory.call_args.kwargs['context'].check_hostname)
            self.assertEqual(factory.call_args.kwargs['context'].verify_mode,ssl.CERT_REQUIRED)
            factory.return_value.set_tunnel.assert_called_once_with('relay.example.test',443)
            factory.return_value.close.assert_called_once()
        self.assertEqual(relay.state()['network']['mode'],'local_proxy')

    def test_bad_proxy_configuration_never_sends_or_inherits_environment(self):
        client=self.ready_client();path=app.DATA_DIR/'login-relay-network.json'
        with patch.dict('os.environ',{'HTTPS_PROXY':'http://outside.example:8080'}):
            self.assertEqual(relay.network()['mode'],'direct')
        values=[{'http_proxy':p} for p in ('http://remote.test:8080','http://localhost:8080','https://127.0.0.1:7897',
                'http://127.0.0.1','http://127.0.0.1:0','http://127.0.0.1:65536','http://user:password@127.0.0.1:7897',
                'http://127.0.0.1:7897/path','http://127.0.0.1:7897?key=private','http://127.0.0.1:7897#x')]
        values.extend([[],{'http_proxy':123},{'http_proxy':'','other':True}])
        for value in values:
            with self.subTest(value=value),patch('login_relay.http.client.HTTPSConnection') as factory:
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):client.call('health')
                factory.assert_not_called()

    def test_proxy_failure_never_falls_back_or_echoes_request(self):
        client=self.ready_client()
        (app.DATA_DIR/'login-relay-network.json').write_text(json.dumps({'http_proxy':'http://127.0.0.1:7897'}))
        with patch('login_relay.http.client.HTTPSConnection') as factory:
            factory.return_value.request.side_effect=OSError('private-request-context')
            with self.assertRaises(relay.RelayError) as error:client.call('take',{'id':ID})
            self.assertNotIn('private-request-context',str(error.exception))
            factory.assert_called_once();factory.return_value.request.assert_called_once()
            factory.return_value.close.assert_called_once()

    def test_https_host_certificates_exact_routes_and_body(self):
        client=self.ready_client()
        with patch('login_relay.http.client.HTTPSConnection') as factory:
            connection=factory.return_value
            response=connection.getresponse.return_value;response.status=201
            response.read.return_value=json.dumps({'id':ID,'expires_at':1800000180500}).encode()
            response.getheader.return_value=formatdate(1800000000,usegmt=True)
            result=client.call('login',{'id':ID,'account':'1267597446'})
            self.assertEqual(result['id'],ID)
            self.assertEqual(result['expires_in_seconds'],179.5)
            args,kwargs=factory.call_args;self.assertEqual(args,('relay.example.test',443))
            self.assertTrue(kwargs['context'].check_hostname)
            self.assertEqual(kwargs['context'].verify_mode,ssl.CERT_REQUIRED)
            self.assertEqual(kwargs['timeout'],8)
            args,kwargs=connection.request.call_args;self.assertEqual(args,('POST','/v1/login'))
            self.assertEqual(json.loads(kwargs['body']),{'id':ID,'account':'1267597446'})
            self.assertEqual(kwargs['headers']['Authorization'],'Bearer '+relay.load()['backend_token'])
            connection.close.assert_called_once()
            response.read.assert_called_once_with(4097)

    def test_remote_date_bounds_lifetime_without_using_local_clock(self):
        client=self.ready_client()
        for skew in (-3600,0,3600):
            with self.subTest(skew=skew), patch('time.time',return_value=1800000000+skew), \
                    patch('login_relay.http.client.HTTPSConnection') as factory:
                response=factory.return_value.getresponse.return_value;response.status=201
                response.getheader.return_value=formatdate(1800000000,usegmt=True)
                response.read.return_value=json.dumps({'id':ID,'expires_at':1800000180500}).encode()
                self.assertEqual(client.call('login',{'id':ID,'account':'fixture'})['expires_in_seconds'],179.5)

    def test_unverifiable_or_extended_remote_lifetime_fails_closed(self):
        client=self.ready_client()
        date=formatdate(1800000000,usegmt=True)
        for expires,header in [(1800000180000,None),(1800000180000,'private-invalid-date'),
                (1800000180000,'Wed, 15 Jan 2027 08:00:00'),(1800000182000,date),
                (1800000000000,date),(float('nan'),date),(float('inf'),date),(True,date)]:
            with self.subTest(expires=expires,header=header), \
                    patch('login_relay.http.client.HTTPSConnection') as factory:
                response=factory.return_value.getresponse.return_value;response.status=201
                response.getheader.return_value=header
                response.read.return_value=json.dumps({'id':ID,'expires_at':expires}).encode()
                with self.assertRaises(relay.RelayError) as error:
                    client.call('login',{'id':ID,'account':'fixture'})
                self.assertNotIn('private-invalid-date',str(error.exception))
                factory.return_value.request.assert_called_once()

    def test_failures_do_not_redirect_retry_or_echo_sensitive_response(self):
        client=self.ready_client()
        for status in (302,401,409,429,500):
            connection=MagicMock();response=connection.getresponse.return_value
            response.status=status;response.read.return_value=b'{"error":"private-secret-diagnostic"}'
            with patch('login_relay.http.client.HTTPSConnection',return_value=connection),self.assertRaises(relay.RelayError) as error:
                client.call('take',{'id':ID})
            self.assertNotIn('private-secret',str(error.exception))
            self.assertEqual(error.exception.code,'unavailable')
            connection.request.assert_called_once();connection.close.assert_called_once()

    def test_transport_rejects_extra_fields_bad_json_and_large_response(self):
        client=self.ready_client()
        with patch('login_relay.http.client.HTTPSConnection') as factory:
            for route,data in [('otp',{}),('login',{'id':ID,'account':7446}),('take',{'id':ID,'code':'123456'}),('health',{})]:
                with self.assertRaises(ValueError):client.call(route,data)
            factory.assert_not_called()
        for raw in (b'[]',b'not-json',b'x'*4097):
            connection=MagicMock();connection.getresponse.return_value.status=200
            connection.getresponse.return_value.read.return_value=raw
            with patch('login_relay.http.client.HTTPSConnection',return_value=connection),self.assertRaises(relay.RelayError):
                client.call('health')

    def test_diagnostics_separate_http_timeout_and_format_without_private_context(self):
        client=self.ready_client()
        for stage,error,expected in [('request',ssl.SSLError('private-key'),'tls'),('getresponse',TimeoutError('private-otp'),'timeout'),('read',OSError('private-body'),'network')]:
            with self.subTest(stage=stage),patch('login_relay.http.client.HTTPSConnection') as factory:
                connection=factory.return_value;response=connection.getresponse.return_value;response.status=200
                (response.read if stage=='read' else getattr(connection,stage)).side_effect=error
                with self.assertRaises(relay.RelayError) as caught:client.call('take',{'id':ID})
                diagnostic=caught.exception.diagnostic()
                self.assertEqual(diagnostic['reason'],expected);self.assertEqual(diagnostic['route'],'take')
                self.assertNotIn('private',json.dumps(diagnostic));connection.request.assert_called_once()
        with patch('login_relay.http.client.HTTPSConnection') as factory:
            response=factory.return_value.getresponse.return_value;response.status=403;response.read.return_value=b'x'*4097
            with self.assertRaises(relay.RelayError) as caught:client.call('take',{'id':ID})
            self.assertEqual(caught.exception.diagnostic(),{'reason':'response_too_large','route':'take','stage':'body','http_status':403})
        error=relay.RelayError('private-message','private-code',reason='private-reason',route='private-url',stage='private-stage',http_status='private-status')
        self.assertEqual(error.diagnostic(),{'reason':'unavailable'})

if __name__=='__main__':unittest.main()
