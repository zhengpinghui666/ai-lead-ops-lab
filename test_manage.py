"""Maintenance regression tests: isolated files, no platform or model requests."""
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import manage
import runtime
import server


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clubops-portable-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / '原始 data'
        self.data.mkdir()
        self.database = self.data / 'clubops-live.db'

    def db(self):
        c = sqlite3.connect(self.database)
        c.execute('CREATE TABLE receipts (id TEXT PRIMARY KEY, status TEXT, raw_uid TEXT)')
        c.execute('INSERT INTO receipts VALUES (?,?,?)', ('do-not-resend', 'unknown', '90071992547409931234'))
        c.commit()
        return c

    def test_online_backup_committed_wal_and_restore_preserve_ledger(self):
        with closing(self.db()) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("INSERT INTO receipts VALUES ('committed', 'accepted', '12345')")
            writer.commit()
            writer.execute("INSERT INTO receipts VALUES ('uncommitted', 'pending', '12345')")
            (self.data / 'uid-http.json').write_text('credential-placeholder')
            (self.data / 'browser-profile').mkdir()
            result = manage.backup(self.data, self.root / 'backup')
            writer.rollback()
        output = self.root / '恢复 数据'
        restored = manage.restore(result['destination'], output)
        self.assertFalse(restored['services_started'])
        self.assertFalse((output / 'uid-http.json').exists())
        self.assertFalse((output / 'browser-profile').exists())
        with closing(sqlite3.connect(output / self.database.name)) as c:
            self.assertEqual(c.execute('SELECT id,status,raw_uid FROM receipts ORDER BY id').fetchall(),
                             [('committed', 'accepted', '12345'), ('do-not-resend', 'unknown', '90071992547409931234')])
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO receipts VALUES ('do-not-resend', 'new', '0')")
        self.assertEqual(manage.digest(output / self.database.name), result['files'][0]['sha256'])

    def test_backup_does_not_create_missing_database(self):
        with self.assertRaises(ValueError):
            manage.backup(self.data, self.root / 'backup')
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertFalse((self.root / 'backup').exists())

    def test_existing_backup_or_restore_directory_never_overwritten(self):
        self.db().close()
        output = self.root / 'backup'
        manage.backup(self.data, output)
        before = manage.digest(output / self.database.name)
        with self.assertRaises(ValueError):
            manage.backup(self.data, output)
        with self.assertRaises(ValueError):
            manage.restore(output, self.data)
        self.assertEqual(before, manage.digest(output / self.database.name))

    def test_tampered_backup_rejected_before_destination_created(self):
        self.db().close()
        output = self.root / 'backup'
        manage.backup(self.data, output)
        with (output / self.database.name).open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(ValueError):
            manage.restore(output, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists())

    def test_manifest_traversal_and_duplicate_rejected(self):
        self.db().close()
        output = self.root / 'backup'
        manage.backup(self.data, output)
        original = manage.read_json(output / 'manifest.json')
        for files in ([dict(original['files'][0], name='../clubops-live.db')], original['files'] * 2):
            (output / 'manifest.json').write_text(json.dumps(dict(original, files=files)))
            with self.assertRaises(ValueError):
                manage.restore(output, self.root / 'restored')
            self.assertFalse((self.root / 'restored').exists())

    def test_invalid_sqlite_rejected_even_with_matching_hash(self):
        output = self.root / 'backup'
        output.mkdir()
        path = output / self.database.name
        path.write_bytes(b'not a sqlite database')
        manage.write_json(output / 'manifest.json', {'format': 'clubops-db-backup-v1', 'files': [
            {'name': path.name, 'bytes': path.stat().st_size, 'sha256': manage.digest(path)}]})
        with self.assertRaises(sqlite3.DatabaseError):
            manage.restore(output, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists())

    def test_source_package_only_explicit_files_and_hashes(self):
        base = self.root / 'source'
        base.mkdir()
        (base / 'app.py').write_text('print("portable")')
        (base / 'secrets.txt').write_text('must stay local')
        (base / '.env').write_text('must stay local')
        manage.write_json(base / 'source-files.json', ['app.py', 'source-files.json'])
        result = manage.package(self.root / 'source.zip', base)
        with zipfile.ZipFile(result['destination']) as archive:
            self.assertEqual(set(archive.namelist()), {'app.py', 'source-files.json', 'SOURCE-MANIFEST.json'})
            manifest = json.loads(archive.read('SOURCE-MANIFEST.json'))
            for item in manifest['files']:
                self.assertEqual(item['sha256'], hashlib.sha256(archive.read(item['name'])).hexdigest())

    def test_source_package_rejects_sensitive_and_outside_paths(self):
        base = self.root / 'source'
        base.mkdir()
        for name in ['../elsewhere.py', '/abs.py', 'C:/abs.py', 'data/clubops-live.db', '.env', 'private/session.py']:
            (base / 'source-files.json').write_text(json.dumps([name]))
            with self.assertRaises(ValueError):
                manage.package(self.root / 'source.zip', base)
            self.assertFalse((self.root / 'source.zip').exists())

    def test_runtime_explicit_paths_do_not_silently_fallback(self):
        with patch.dict(os.environ, {'CLUBOPS_NODE': 'missing-node', 'CLUBOPS_PLAYWRIGHT': str(self.root / 'missing')}, clear=True):
            node, package = runtime.dependencies()
            self.assertEqual(node, 'missing-node')
            self.assertEqual(package, self.root / 'missing')
            report = runtime.doctor()
            self.assertTrue(report['core_ready'])
            self.assertFalse(report['browser_dependencies_ready'])
            self.assertFalse((self.root / 'missing').exists())

    def test_doctor_missing_optional_dependencies_does_not_init_data(self):
        unused = self.root / 'never-created'
        with patch.dict(os.environ, {'CLUBOPS_DATA_DIR': str(unused)}, clear=True), patch('shutil.which', return_value=None):
            report = runtime.doctor()
        self.assertTrue(report['core_ready'])
        self.assertFalse(report['browser_dependencies_ready'])
        self.assertFalse(unused.exists())

    def test_busy_port_cannot_initialize_database(self):
        with closing(socket.socket()) as listener:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            with patch.object(server, 'PORT', listener.getsockname()[1]), patch('clubops.init') as init:
                with self.assertRaises(OSError):
                    server.main()
                init.assert_not_called()

    def test_dynamic_port_checks_actual_host_and_keeps_origin_protection(self):
        httpd = server.LocalHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            for method, headers, expected in [('GET', {}, 200), ('GET', {'Host': 'evil.invalid'}, 403),
                    ('POST', {'Origin': 'https://evil.invalid', 'X-ClubOps-Token': server.CSRF}, 403)]:
                with closing(http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)) as c:
                    c.request(method, '/', headers=headers)
                    response = c.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

    def test_same_data_dir_second_process_rejected_then_lock_reusable(self):
        script = 'import runtime,sys; from pathlib import Path\nwith runtime.data_lock(Path(sys.argv[1])): print("acquired")'
        with runtime.data_lock(self.data):
            process = subprocess.run([sys.executable, '-c', script, str(self.data)], cwd=manage.BASE,
                                     capture_output=True, encoding='utf-8', timeout=10,
                                     env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('数据目录', process.stderr)
        self.assertNotIn('acquired', process.stdout)
        with runtime.data_lock(self.data):
            pass
        self.assertTrue((self.data / '.clubops.lock').exists())

    def test_powershell_check_from_another_directory_does_not_create_data(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell not installed')
        target = self.root / '检查 only'
        env = {**os.environ, 'CLUBOPS_PYTHON': '', 'PYTHONIOENCODING': 'utf-8'}
        result = subprocess.run([shell, '-NoProfile', '-File', str(manage.BASE / 'start.ps1'),
                                 '-Check', '-DataDir', str(target)], cwd=self.root,
                                capture_output=True, encoding='utf-8', errors='replace', env=env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['core_ready'])
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
