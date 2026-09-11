"""Local Git transport and synthetic credentials only; never contact GitHub."""
import json
import base64
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import github_backup as backup


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class SourceBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='clubops-git-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.folder = self.root / 'backup'
        self.folder.mkdir()
        self.remote = self.root / 'remote.git'
        self.env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1',
                    'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}
        backup.command(['git', 'init', '--bare', str(self.remote)], env=self.env)
        (self.source / 'app.py').write_bytes(b'print("source")\r\n')
        self.names = ['app.py', 'source-files.json']
        self.save_names()

    def save_names(self):
        (self.source / 'source-files.json').write_text(json.dumps(self.names), encoding='utf-8')

    def run_backup(self, files=None):
        return backup.backup_git(files or backup.snapshot(self.source), self.folder,
                                 str(self.remote), 123, self.env)

    def remote_head(self):
        return backup.command(['git', '--git-dir=' + str(self.remote), 'rev-parse',
                               'refs/heads/' + backup.BRANCH], env=self.env).stdout.strip()

    def test_roundtrip_exact_bytes_unchanged_and_removed_source(self):
        (self.source / '.env').write_text('stay local')
        (self.source / 'data').mkdir()
        (self.source / 'data' / 'private.json').write_text('stay local')
        first = self.run_backup()
        self.assertEqual(first['status'], 'pushed')
        content = backup.command(['git', '--git-dir=' + str(self.remote), 'show',
                                  first['commit'] + ':app.py'], env=self.env).stdout
        self.assertEqual(content, b'print("source")\r\n')
        self.assertEqual(self.run_backup()['status'], 'unchanged')
        self.names.remove('app.py')
        self.save_names()
        second = self.run_backup()
        self.assertEqual(second['status'], 'pushed')
        self.assertNotEqual(first['commit'], second['commit'])
        paths = backup.command(['git', '--git-dir=' + str(self.remote), 'ls-tree', '-r',
                                '--name-only', second['commit']], env=self.env).stdout.decode().splitlines()
        self.assertEqual(set(paths), {'source-files.json', backup.MANIFEST})

    def test_failed_push_is_retained_then_retried_without_duplicate_commit(self):
        original = backup.command
        def interrupted(args, **kwargs):
            if 'push' in args:
                raise backup.BackupError('synthetic network failure')
            return original(args, **kwargs)
        with patch.object(backup, 'command', side_effect=interrupted):
            with self.assertRaisesRegex(backup.BackupError, 'synthetic'):
                self.run_backup()
        gitdir = str(self.folder / 'repository.git')
        pending = original(['git', '--git-dir=' + gitdir, 'rev-parse', backup.BRANCH], env=self.env).stdout.strip()
        retried = self.run_backup()
        self.assertEqual(retried['status'], 'pushed')
        self.assertEqual(retried['commit'].encode(), pending)
        self.assertEqual(self.remote_head(), pending)

    def test_public_fork_and_wrong_owner_rejected(self):
        good = {'id': 123, 'private': True, 'fork': False, 'full_name': 'owner/repo', 'owner': {'id': 1}}
        for bad in [dict(good, private=False), dict(good, fork=True), dict(good, owner={'id': 2})]:
            with patch.object(backup, 'api', side_effect=[{'login': 'owner', 'id': 1}, bad]):
                with self.assertRaises(backup.BackupError):
                    backup.private_repository('owner/repo', {})
        with patch.object(backup, 'api', return_value={'login': 'someone-else', 'id': 2}):
            with self.assertRaises(backup.BackupError):
                backup.private_repository('owner/repo', {})

    def test_secret_and_runtime_files_rejected_without_secret_in_error(self):
        synthetic = b'gh' + b'p_' + b'1234567890abcdefghijABCDEFGHIJ1234567890'
        with self.assertRaises(backup.BackupError) as error:
            backup.safe_content('app.py', synthetic)
        self.assertNotIn(synthetic.decode(), str(error.exception))
        for name in ['data/config.json', 'Data/config.json', 'a/NODE_MODULES/key.json', 'artifacts/session.json', '.env', '../outside', 'a/.wrangler/state']:
            with self.assertRaises(backup.BackupError):
                backup.safe_content(name, b'ordinary')
        with self.assertRaises(backup.BackupError):
            backup.safe_content('app.py', b'api_key="aaaaaaaaaaaaaaaaaaaaaaaa"\naccess_token="' + b'1234567890abcdefghijABCDEFGHIJ1234567890' + b'"')

    def test_unlisted_pending_content_never_reaches_remote(self):
        first = self.run_backup()
        contaminated = backup.snapshot(self.source)
        contaminated['forgotten.txt'] = b'not part of the manifest'
        with self.assertRaisesRegex(backup.BackupError, '清单之外'):
            self.run_backup(contaminated)
        self.assertEqual(self.remote_head().decode(), first['commit'])
        with self.assertRaisesRegex(backup.BackupError, '清单之外'):
            self.run_backup()
        self.assertEqual(self.remote_head().decode(), first['commit'])

    def test_lock_and_remote_identity_prevent_overlapping_or_wrong_push(self):
        with backup.backup_lock(self.folder):
            with self.assertRaises(backup.BackupError):
                with backup.backup_lock(self.folder):
                    self.fail('must not enter')
        self.assertFalse((self.folder / 'backup.lock').exists())
        self.run_backup()
        marker = self.folder / 'repository.json'
        value = json.loads(marker.read_text())
        value['repository_id'] = 456
        marker.write_text(json.dumps(value))
        with self.assertRaisesRegex(backup.BackupError, '身份不匹配'):
            self.run_backup()

    def test_remote_divergence_does_not_force_push(self):
        first = self.run_backup()
        foreign = self.root / 'foreign'
        foreign.mkdir()
        (self.source / 'app.py').write_text('print("remote change")')
        # Simulate a separately authored remote commit based on the saved history.
        gitdir = str(self.folder / 'repository.git')
        tree = backup.command(['git', '--git-dir=' + gitdir, 'rev-parse', first['commit'] + '^{tree}'], env=self.env).stdout.strip().decode()
        outsider = backup.command(['git', '--git-dir=' + gitdir, '-c', 'user.name=Fixture',
                                   '-c', 'user.email=fixture@example.invalid', 'commit-tree', tree,
                                   '-p', first['commit'], '-m', 'external change'], env=self.env).stdout.strip().decode()
        backup.command(['git', '--git-dir=' + gitdir, 'push', str(self.remote),
                        outsider + ':refs/heads/' + backup.BRANCH], env=self.env)
        with self.assertRaisesRegex(backup.BackupError, '分叉'):
            self.run_backup()
        self.assertEqual(self.remote_head().decode(), outsider)

    def api_fixture(self, *, bad_blob=False, changed_remote=False):
        first = self.run_backup()
        (self.source / 'app.py').write_text('print("API backup")\n', encoding='utf-8')
        original = backup.command
        def offline(args, **kwargs):
            if 'push' in args:
                raise backup.BackupError('synthetic offline')
            return original(args, **kwargs)
        with patch.object(backup, 'command', side_effect=offline):
            with self.assertRaises(backup.BackupError):
                self.run_backup()
        git = ['git', '--git-dir=' + str(self.folder / 'repository.git')]
        head = backup.command([*git, 'rev-parse', backup.BRANCH], env=self.env).stdout.decode().strip()
        tree = backup.command([*git, 'rev-parse', head + '^{tree}'], env=self.env).stdout.decode().strip()
        raw_commit = backup.command([*git, 'cat-file', 'commit', head], env=self.env).stdout
        calls, reads = [], []
        def fake_api(endpoint, env, payload=None, **kwargs):
            calls.append((endpoint, payload, kwargs))
            if 'matching-refs' in endpoint:
                reads.append(endpoint)
                sha = 'a' * 40 if changed_remote and len(reads)>1 else first['commit']
                return [{'ref': 'refs/heads/' + backup.BRANCH, 'object': {'sha': sha}}]
            if '?recursive=1' in endpoint:
                return {'tree': [], 'truncated': False}
            if endpoint.endswith('/blobs'):
                raw = base64.b64decode(payload['content'])
                sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
                return {'sha': 'b'*40 if bad_blob else sha}
            if endpoint.endswith('/trees'):
                self.assertEqual({x['path'] for x in payload['tree']}, set(backup.snapshot(self.source)))
                return {'sha': tree}
            if endpoint.endswith('/commits'):
                self.assertEqual(payload['parents'], [first['commit']])
                self.assertEqual(payload['tree'], tree)
                self.assertEqual(payload['message'], raw_commit.split(b'\n\n', 1)[1].decode())
                self.assertEqual(payload['author']['date'][-6:], '+00:00')
                self.assertEqual(payload['author']['name'], 'ClubOps Backup')
                return {'sha': head}
            if '/refs/heads/' in endpoint:
                self.assertEqual(kwargs['method'], 'PATCH')
                self.assertEqual(payload, {'sha': head, 'force': False})
                return {}
            self.fail(endpoint)
        return git, head, first['commit'], calls, fake_api

    def test_api_upload_preserves_git_objects_and_only_updates_ref_at_end(self):
        git, head, previous, calls, fake = self.api_fixture()
        with patch.object(backup, 'api', side_effect=fake):
            backup.publish_api(git, head, previous, 'owner/repo', backup.BRANCH, self.env)
        self.assertEqual(calls[-1][2]['method'], 'PATCH')
        self.assertEqual(sum('/refs/heads/' in c[0] for c in calls), 1)

    def test_api_hash_mismatch_never_updates_ref(self):
        git, head, previous, calls, fake = self.api_fixture(bad_blob=True)
        with patch.object(backup, 'api', side_effect=fake):
            with self.assertRaisesRegex(backup.BackupError, '哈希不符'):
                backup.publish_api(git, head, previous, 'owner/repo', backup.BRANCH, self.env)
        self.assertFalse(any('/refs/heads/' in c[0] for c in calls))

    def test_api_concurrent_remote_change_never_updates_ref(self):
        git, head, previous, calls, fake = self.api_fixture(changed_remote=True)
        with patch.object(backup, 'api', side_effect=fake):
            with self.assertRaisesRegex(backup.BackupError, '远端发生变化'):
                backup.publish_api(git, head, previous, 'owner/repo', backup.BRANCH, self.env)
        self.assertFalse(any('/refs/heads/' in c[0] for c in calls))


if __name__ == '__main__':
    unittest.main()
