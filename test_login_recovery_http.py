"""Exercise local HTTP boundary, using a temporary database and real DPAPI only."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import clubops as app
import login_recovery
import login_relay
import server


class QuietHandler(server.Handler):
    def log_message(self, *args):
        pass


class LoginHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='login-http-test-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(app, 'DATA_DIR', Path(self.temp.name)).start()
        patch.object(login_recovery, 'ACTIVE', None).start()
        app.init()
        self.httpd = server.LocalHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def close(self):
        self.httpd.shutdown()
        self.thread.join(timeout=3)
        self.httpd.server_close()

    def request(self, path, body=None, **headers):
        request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
            headers=headers, method='GET' if body is None else 'POST')
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), dict(response.headers)

    def test_credentials_require_explicit_post_with_csrf_and_origin(self):
        private = login_relay.provision()
        login_relay.bind('https://fixture.example.test')
        status, raw, headers = self.request('/api/login-recovery')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        for key in ('phone_token', 'backend_token'):
            self.assertNotIn(private[key].encode(), raw)
        csrf = json.loads(raw)['csrf']
        self.assertEqual(self.request('/api/login-relay-phone')[0], 404)
        self.assertEqual(self.request('/api/login-relay-phone', {})[0], 403)
        self.assertEqual(self.request('/api/login-relay-phone', {}, **{'X-ClubOps-Token': csrf, 'Origin': 'https://unrelated.invalid'})[0], 403)
        good = {'X-ClubOps-Token': csrf, 'Origin': self.base}
        self.assertEqual(self.request('/api/login-relay-phone?mode=demo', {}, **good)[0], 400)
        status, raw, _ = self.request('/api/login-relay-phone', {}, **good)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['result']['authorization'], 'Bearer ' + private['phone_token'])
        self.assertNotIn(private['backend_token'].encode(), raw)
        self.assertEqual(json.loads(raw)['result']['account'], '1267597446')

    def test_pages_and_configuration_do_not_launch_work_or_expose_files(self):
        for path in ('/login', '/login.js', '/login.css', '/login-guide', '/iphone-script'):
            status, raw, headers = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
            self.assertGreater(len(raw), 100)
        for path in ('/data/private/login-recovery/pairing.dpapi', '/login-relay.py', '/login/../data'):
            self.assertEqual(self.request(path)[0], 404)
        with patch.object(login_recovery, 'start') as start:
            status, raw, _ = self.request('/api/login-recovery-save', login_recovery.DEFAULTS,
                **{'X-ClubOps-Token': server.CSRF, 'Origin': self.base})
        self.assertEqual(status, 200)
        start.assert_not_called()
        self.assertFalse(json.loads(raw)['result']['active'])
        self.assertFalse(login_recovery.config()['auto_recover'])


if __name__ == '__main__':
    unittest.main()
