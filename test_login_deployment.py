"""Deployment orchestration with mocked Cloudflare; never grants or deploys."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('deploy_login_relay', Path(__file__).parent / 'scripts/deploy-login-relay.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
ACCOUNT = '1' * 32
ORIGIN = 'https://clubops-login-relay.fixture.workers.dev'
PRIVATE = {'origin': '', 'backend_token': 'fixture-backend', 'phone_token': 'fixture-phone'}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        setting=patch.object(deploy.login_relay,'network',return_value={'mode':'direct','http_proxy':''})
        setting.start();self.addCleanup(setting.stop)

    def test_verify_only_keeps_deployed_code_and_secrets_unchanged(self):
        version='2'*8+'-'+'2'*4+'-'+'2'*4+'-'+'2'*4+'-'+'2'*12
        with patch.object(deploy, 'invoke', side_effect=[ACCOUNT, '(100%) '+version]) as invoke, \
             patch.object(deploy, 'require_idle'), \
             patch.object(deploy.login_relay, 'load', return_value={**PRIVATE,'origin':ORIGIN}), \
             patch.object(deploy.login_relay, 'bind') as bind, \
             patch.object(deploy, 'probe', return_value={'iphone_tested':False}), \
             patch.object(deploy.login_recovery, 'check_relay'), \
             patch.object(deploy.login_recovery, '_write'):
            result=deploy.deploy(ACCOUNT,verify_only=True)
        self.assertEqual([c.args[0] for c in invoke.call_args_list],[['whoami'],['deployments','list']])
        bind.assert_not_called();self.assertEqual(result['current_version'],version)
        self.assertEqual(result['status'],'deployed_and_https_verified')

    def test_failed_https_retains_deployment_phase_without_private_diagnostic(self):
        with patch.object(deploy, 'invoke', side_effect=[ACCOUNT,ORIGIN,'updated']), \
             patch.object(deploy, 'require_idle'), \
             patch.object(deploy.login_relay, 'load', return_value=PRIVATE), \
             patch.object(deploy.login_relay, 'bind'), \
             patch.object(deploy, 'probe', side_effect=Exception('private-request-context')), \
             patch.object(deploy.login_recovery, '_write') as write:
            with self.assertRaisesRegex(ValueError,'已部署') as caught:deploy.deploy(ACCOUNT)
        self.assertNotIn('private-request-context',str(caught.exception))
        result=write.call_args.args[1]
        self.assertEqual(result['status'],'deployed_https_check_failed')
        self.assertFalse(result['real_sms_login_tested'])
        self.assertNotIn('fixture-phone',json.dumps(result))

    def test_email_verification_failure_is_specific_and_redacted(self):
        for detail, expected in [('[code: 10034]', '验证注册邮箱'), ('unrecognized failure', '命令未完成')]:
            with self.subTest(detail=detail), \
                 patch.object(deploy.runtime, 'dependencies', return_value=('node', None)), \
                 patch.object(Path, 'is_file', return_value=True), \
                 patch.object(deploy.subprocess, 'run', return_value=Mock(returncode=1, stdout='', stderr='private-request-context ' + detail)):
                with self.assertRaisesRegex(ValueError, expected) as caught:
                    deploy.invoke(['deploy'], ACCOUNT)
                self.assertNotIn('private-request-context', str(caught.exception))

    def test_unauthenticated_success_exit_does_not_deploy(self):
        with patch.object(deploy, 'invoke', return_value='You are not authenticated.') as invoke:
            with self.assertRaisesRegex(ValueError, '尚未获准'):
                deploy.deploy(ACCOUNT)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(invoke.call_args.args[0], ['whoami'])

    def test_deploy_secrets_stay_in_stdin_and_only_https_proof_is_claimed(self):
        with patch.object(deploy, 'invoke', side_effect=[ACCOUNT, ORIGIN, 'updated']) as invoke, \
             patch.object(deploy, 'require_idle') as idle, \
             patch.object(deploy.login_relay, 'load', return_value=PRIVATE), \
             patch.object(deploy.login_relay, 'bind') as bind, \
             patch.object(deploy, 'probe', return_value={'iphone_tested': False}) as probe, \
             patch.object(deploy.login_recovery, 'check_relay'), \
             patch.object(deploy.login_recovery, '_write') as write:
            result = deploy.deploy(ACCOUNT)
        idle.assert_called_once()
        bind.assert_called_once_with(ORIGIN)
        probe.assert_called_once_with(ORIGIN, PRIVATE)
        payload = json.loads(invoke.call_args_list[2].args[2])
        self.assertEqual(payload, {'BACKEND_TOKEN': PRIVATE['backend_token'], 'PHONE_TOKEN': PRIVATE['phone_token']})
        for call in invoke.call_args_list:
            for token in (PRIVATE['backend_token'], PRIVATE['phone_token']):
                self.assertNotIn(token, json.dumps(call.args[:2]))
        self.assertFalse(result['real_sms_login_tested'])
        self.assertFalse(result['checks']['iphone_tested'])
        self.assertNotIn('fixture-backend', json.dumps(write.call_args.args[1]))

    def test_other_bound_origin_does_not_receive_credentials(self):
        with patch.object(deploy, 'invoke', side_effect=[ACCOUNT, ORIGIN]) as invoke, \
             patch.object(deploy, 'require_idle'), \
             patch.object(deploy.login_relay, 'load', return_value={**PRIVATE, 'origin': 'https://existing.example.test'}), \
             patch.object(deploy.login_relay, 'bind') as bind:
            with self.assertRaisesRegex(ValueError, '部署地址'):
                deploy.deploy(ACCOUNT)
        self.assertEqual(invoke.call_count, 2)
        bind.assert_not_called()

    def test_probe_submits_takes_once_and_cancels_without_phone_acceptance(self):
        relay = Mock()
        saved = {}
        def call(route, body=None):
            if route == 'health':return {'service': 'clubops-login-relay', 'version': 1}
            if route == 'login':saved['id'] = body['id'];return {'id': body['id']}
            if route == 'take':
                if saved.get('taken'):return {'status': 'taken'}
                saved['taken'] = True
                return {'id': saved['id'], 'status': 'received', 'code': saved['code']}
            return {'status': 'cleared'}
        relay.call.side_effect = call
        with patch.object(deploy.login_relay, 'Relay', return_value=relay), \
             patch.object(deploy.login_recovery, 'config', return_value={'account': '1267597446'}), \
             patch.object(deploy, 'phone_submit', side_effect=lambda origin, token, body: saved.update(code=body['code'])):
            result = deploy.probe(ORIGIN, PRIVATE)
        self.assertTrue(result['take_once'])
        self.assertFalse(result['iphone_tested'])
        self.assertEqual([call.args[0] for call in relay.call.call_args_list], ['health', 'login', 'take', 'take', 'cancel'])


if __name__ == '__main__':unittest.main()
