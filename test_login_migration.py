"""Isolated migration checks; no Cloudflare, SMS or production writes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('migrate_login_relay', Path(__file__).parent / 'scripts/migrate-login-relay.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
ACCOUNT = '1' * 32
PROJECT = 'fixture-login'
OLD = 'https://fixture-relay.fixture.workers.dev'
NEW = 'https://fixture-login.pages.dev'


class MigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='clubops-migration-test-')
        self.addCleanup(directory.cleanup)
        setting = patch.object(migration.app, 'DATA_DIR', Path(directory.name))
        setting.start(); self.addCleanup(setting.stop)
        migration.relay.provision(); migration.relay.bind(OLD)
        self.private = migration.relay.load()
        self.pair_bytes = migration.relay.path().read_bytes()
        migration.recovery._write(migration.app.DATA_DIR / migration.recovery.CONFIG,
                                 {'account': 'fixture-account', 'auto_recover': True})
        migration.recovery._write(migration.recovery._file('checks.json'),
                                 {'origin': OLD, 'health_at': 1, 'phone_test_at': 2})
        migration.recovery._write(migration.app.DATA_DIR / 'login-relay-network.json',
                                 {'http_proxy': 'http://127.0.0.1:7897'})

    def test_preview_is_read_only_and_never_returns_credentials(self):
        with patch.object(migration, 'verify_project') as verify:
            result = migration.plan(ACCOUNT, PROJECT, OLD)
        verify.assert_not_called()
        self.assertEqual(result['status'], 'ready_to_verify')
        self.assertEqual(migration.relay.path().read_bytes(), self.pair_bytes)
        self.assertTrue(migration.recovery.config()['auto_recover'])
        for key in ('phone_token', 'backend_token'):
            self.assertNotIn(self.private[key], json.dumps(result))

    def test_bad_or_stale_endpoint_never_contacts_cloud(self):
        with patch.object(migration, 'verify_project') as verify:
            for account, project, old in [('bad', PROJECT, OLD), (ACCOUNT, '../evil', OLD),
                    (ACCOUNT, 'evil.pages.dev', OLD), (ACCOUNT, PROJECT, 'https://other.example.test'),
                    (ACCOUNT, PROJECT, 'https://other.fixture.workers.dev')]:
                with self.subTest(account=account, project=project, old=old), self.assertRaises(ValueError):
                    migration.migrate(account, project, old)
        verify.assert_not_called()
        self.assertEqual(migration.relay.path().read_bytes(), self.pair_bytes)

    def test_running_service_lock_and_unfinished_job_block_apply(self):
        with patch.object(migration, 'verify_project') as verify:
            with migration.runtime.data_lock(migration.app.DATA_DIR), self.assertRaises(RuntimeError):
                migration.migrate(ACCOUNT, PROJECT, OLD)
            migration.recovery._write(migration.recovery._file('jobs.json'), [{'status': 'waiting_sms'}])
            with self.assertRaises(ValueError):
                migration.migrate(ACCOUNT, PROJECT, OLD)
        verify.assert_not_called()

    def test_remote_failure_keeps_all_existing_configuration(self):
        for failure in ('verify_project', 'verify_shared_state'):
            with self.subTest(failure=failure), patch.object(migration, 'verify_project'), \
                    patch.object(migration, 'verify_shared_state'), \
                    patch.object(migration, failure, side_effect=ValueError('fixture')):
                with self.assertRaises(ValueError): migration.migrate(ACCOUNT, PROJECT, OLD)
            self.assertEqual(migration.relay.path().read_bytes(), self.pair_bytes)
            self.assertTrue(migration.recovery.config()['auto_recover'])
            self.assertEqual(migration.recovery.checks()['phone_test_at'], 2)
            self.assertEqual(migration.relay.network()['mode'], 'local_proxy')

    def test_success_preserves_keys_account_history_and_resets_acceptance(self):
        history = [{'status': 'timeout', 'id': 'historical'}]
        migration.recovery._write(migration.recovery._file('jobs.json'), history)
        with patch.object(migration, 'verify_project') as verify, patch.object(migration, 'verify_shared_state') as shared:
            result = migration.migrate(ACCOUNT, PROJECT, OLD)
            again = migration.migrate(ACCOUNT, PROJECT, OLD)
        verify.assert_called_once(); shared.assert_called_once()
        self.assertEqual(result['status'], 'migrated'); self.assertFalse(result['phone_tested'])
        self.assertEqual(again['status'], 'already_migrated')
        self.assertEqual(migration.relay.load(), {**self.private, 'origin': NEW})
        self.assertEqual(migration.recovery.config(), {'account': 'fixture-account', 'auto_recover': False})
        self.assertEqual(migration.recovery.checks(), {})
        self.assertEqual(migration.recovery._history(), history)
        self.assertEqual(migration.relay.network()['mode'], 'direct')
        for key in ('phone_token', 'backend_token'):
            self.assertNotIn(self.private[key].encode(), migration.relay.path().read_bytes())
        with self.assertRaises(ValueError): migration.relay.bind(OLD)
        with self.assertRaises(ValueError): migration.relay.Relay(OLD)

    def test_interrupted_commit_disables_recovery_before_origin_changes(self):
        with patch.object(migration, 'verify_project'), patch.object(migration, 'verify_shared_state'), \
                patch.object(migration.relay, 'write', side_effect=OSError('fixture')):
            with self.assertRaises(OSError): migration.migrate(ACCOUNT, PROJECT, OLD)
        self.assertEqual(migration.relay.path().read_bytes(), self.pair_bytes)
        self.assertFalse(migration.recovery.config()['auto_recover'])
        self.assertEqual(migration.recovery.checks(), {})

    def test_shared_probe_is_synthetic_and_cleans_up_when_new_origin_fails(self):
        proposal = migration.plan(ACCOUNT, PROJECT, OLD)
        original = Mock()
        def response(route, body=None):
            if route == 'health': return {'service': 'clubops-login-relay', 'version': 1}
            if route == 'login': return {'id': body['id']}
            return {'status': 'cleared'}
        original.call.side_effect = response
        with patch.object(migration.relay, 'Relay', return_value=original), \
                patch.object(migration, 'candidate_call', side_effect=[response('health'), ValueError('fixture')]):
            with self.assertRaises(ValueError): migration.verify_shared_state(proposal, self.private)
        login = next(c for c in original.call.call_args_list if c.args[0] == 'login')
        self.assertTrue(login.args[1]['account'].startswith('migration-'))
        self.assertNotEqual(login.args[1]['account'], proposal['account'])
        self.assertEqual(original.call.call_args.args, ('cancel', {'id': login.args[1]['id']}))

    def test_direct_transport_rejects_redirects_limits_output_and_redacts_errors(self):
        proposal = migration.plan(ACCOUNT, PROJECT, OLD)
        for status, raw in [(302, b'private'), (200, b'x' * 4097), (200, b'not-json'), (200, b'[]')]:
            with self.subTest(status=status, raw_length=len(raw)), \
                    patch.object(migration.http.client, 'HTTPSConnection') as factory:
                response = factory.return_value.getresponse.return_value
                response.status = status; response.read.return_value = raw
                with self.assertRaises(ValueError) as error:
                    migration.candidate_call(proposal, self.private, 'health')
                self.assertNotIn('private', str(error.exception))
                self.assertEqual(factory.call_args.args, ('fixture-login.pages.dev', 443))
                self.assertTrue(factory.call_args.kwargs['context'].check_hostname)
                factory.return_value.set_tunnel.assert_not_called()
                factory.return_value.request.assert_called_once()
                factory.return_value.close.assert_called_once()
                response.read.assert_called_once_with(4097)

    def test_live_config_download_uses_selected_account_and_requires_production_binding(self):
        proposal = migration.plan(ACCOUNT, PROJECT, OLD)
        good = 'name = "fixture-login"\n[[env.production.services]]\nbinding = "RELAY"\nservice = "fixture-relay"\nenvironment = ""\n'
        def downloaded(text):
            def invoke(args, **kwargs):
                self.assertEqual(kwargs['env']['CLOUDFLARE_ACCOUNT_ID'], ACCOUNT)
                self.assertEqual(args[-4:], ['pages', 'download', 'config', PROJECT])
                (Path(kwargs['cwd']) / 'wrangler.toml').write_text(text, encoding='utf-8')
                return Mock(returncode=0)
            return invoke
        with patch.object(migration.runtime, 'dependencies', return_value=('node-fixture', None)), \
                patch.object(Path, 'is_file', return_value=True):
            for text in (good.replace('fixture-relay', 'other-relay'), good.replace('production', 'preview'),
                         good.replace('environment = ""', 'environment = "staging"')):
                with patch.object(migration.subprocess, 'run', side_effect=downloaded(text)), self.assertRaises(ValueError):
                    migration.verify_project(proposal)
            with patch.object(migration.subprocess, 'run', side_effect=downloaded(good)):
                migration.verify_project(proposal)


if __name__ == '__main__': unittest.main()
