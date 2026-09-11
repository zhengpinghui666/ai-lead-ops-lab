"""Safe model failure evidence through the actual queue and local sockets."""
import http.client
import http.server
import json
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import clubops as app
import semantic
import semantic_api
import semantic_queue as queue
import test_semantic_api as fixtures


class DiagnosticTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp

    def connection(self, status=200, raw=b'{}', content_type='application/json'):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = status
        response.getheader.return_value = content_type
        response.read1.side_effect = [raw, b'']
        return connection

    def run_queue(self, connection):
        semantic.save(dict(self.settings, auto_analyze=True))
        app.ingest({'records':[dict(comment_id='diagnostic-1',video_id='v1',user_id='12345',text=fixtures.SOURCE['text'])]})
        app.analyze()
        with patch.object(semantic_api.ChatAPIAdapter, 'connection', return_value=connection):
            self.assertTrue(queue.run_one())
        with app.db() as c:
            row = dict(c.execute("SELECT status,detail,result_json FROM intent_results WHERE method='model'").fetchone())
        row['result'] = json.loads(row.pop('result_json'))
        return row

    def test_server_status_is_saved_without_reading_private_error_body(self):
        connection = self.connection(502, b'private upstream error with credentials')
        row = self.run_queue(connection)
        self.assertEqual(row['status'], 'failed')
        self.assertIn('HTTP 502', row['detail'])
        self.assertEqual(row['result']['diagnostic'], dict(code='api_server', stage='headers', http_status=502, received_bytes=0))
        connection.getresponse.return_value.read1.assert_not_called()
        connection.request.assert_called_once()
        self.assertNotIn('private upstream', json.dumps(row))
        self.assertEqual(app.state()['comments'][0]['analysis_method'], 'rules')
        self.assertEqual(app.state()['messages'], [])

    def test_connection_failure_records_stage_not_exception_text(self):
        connection = self.connection()
        connection.connect.side_effect = socket.gaierror('private endpoint and credential')
        row = self.run_queue(connection)
        self.assertEqual(row['result']['diagnostic'], dict(code='dns', stage='connect', received_bytes=0))
        self.assertNotIn('private endpoint', json.dumps(row))
        connection.request.assert_not_called()
        connection.close.assert_called_once()

    def test_non_json_oversize_invalid_json_and_error_envelope_are_distinct(self):
        for raw, content_type, code, stage in [
            (b'<html>private</html>', 'text/html', 'response_type', 'headers'),
            (b'x'*262145, 'application/json', 'response_size', 'body'),
            (b'not json private', 'application/json', 'response_json', 'decode'),
            (b'{"error":"private provider detail"}', 'application/json', 'response_error', 'decode'),
        ]:
            with self.subTest(code=code):
                connection = self.connection(raw=raw, content_type=content_type)
                adapter = semantic_api.ChatAPIAdapter(self.settings)
                with patch.object(adapter, 'connection', return_value=connection):
                    with self.assertRaises(semantic.ModelError) as error:
                        adapter.request('/chat/completions', {})
                record = semantic.diagnostic(adapter, semantic.failure_code(error.exception, adapter))
                self.assertEqual((record['code'],record['stage'],record['http_status']),(code,stage,200))
                self.assertNotIn('private',json.dumps(record))
                connection.request.assert_called_once()

    def test_success_records_complete_stage_and_response_size(self):
        connection = self.connection(raw=json.dumps(fixtures.response()).encode())
        row = self.run_queue(connection)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['result']['diagnostic']['code'], 'ok')
        self.assertEqual(row['result']['diagnostic']['stage'], 'complete')
        self.assertEqual(row['result']['diagnostic']['http_status'], 200)
        self.assertEqual(row['result']['diagnostic']['received_bytes'],len(json.dumps(fixtures.response()).encode()))

    def test_invalid_evidence_is_distinct_from_transport_failure(self):
        response = fixtures.response()
        content = json.loads(response['choices'][0]['message']['content'])
        content['facts']['budget'] = dict(value='999元',quote='999元')
        response['choices'][0]['message']['content'] = json.dumps(content)
        row = self.run_queue(self.connection(raw=json.dumps(response).encode()))
        self.assertEqual(row['status'],'failed')
        self.assertEqual(row['result']['diagnostic']['code'],'invalid_result')
        self.assertEqual(row['result']['diagnostic']['stage'],'validate')
        self.assertEqual(row['result']['diagnostic']['http_status'],200)

    def test_exception_categories_and_untrusted_diagnostic_fields(self):
        for error, code in [(ssl.SSLError('private'), 'tls'), (ConnectionResetError('private'),'connection'),
                            (http.client.RemoteDisconnected('private'),'connection'), (TimeoutError('private'),'timeout'),
                            (ValueError('private'),'invalid_result'), (semantic.ModelError('private'),'unavailable')]:
            self.assertEqual(semantic.failure_code(error),code)
        adapter = MagicMock(stage='private',http_status='private',received_bytes=-1)
        self.assertEqual(semantic.diagnostic(adapter,'private'),{'code':'unavailable'})

    def test_deadline_socket_interrupt_is_timeout_and_cancel_is_cancelled(self):
        for cancel_request in (False, True):
            entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    entered.set();release.wait(3)
                def log_message(self,*args): pass
            server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
            server.daemon_threads = True
            serving = threading.Thread(target=server.serve_forever,daemon=True)
            serving.start()
            adapter = semantic.OllamaAdapter(dict(semantic.DEFAULTS,port=server.server_port,timeout_seconds=5))
            adapter.cancel_event = cancelled
            # Shorten only this test's deadline; production config bounds stay intact.
            adapter.deadline = time.monotonic() + (2 if cancel_request else .25)
            errors = []
            def call():
                try: adapter.request('/api/status')
                except Exception as error: errors.append(semantic.failure_code(error,adapter))
            worker = threading.Thread(target=call)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                if cancel_request: cancelled.set()
                worker.join(1.5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors,['cancelled' if cancel_request else 'timeout'])
                self.assertEqual(adapter.stage,'headers')
            finally:
                release.set();worker.join(3);server.shutdown();server.server_close();serving.join(2)


if __name__ == '__main__':
    unittest.main()
