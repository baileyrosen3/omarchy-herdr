"""Native manager supervision with temporary sources, config and runtime files."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from herdr_shell import PLUGIN_ID, omarchy
from herdr_shell.runtime import ShellError


class EndLoop(Exception):
    pass


class OmarchyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.source = self.home / '.config/omarchy/plugins' / PLUGIN_ID
        self.runtime = self.home / 'data/runtime'
        self.control = self.home / 'state/omarchy'
        self.config = self.home / '.config/omarchy/shell.json'
        for path, text in {
            'manifest.json': json.dumps({'schemaVersion': 1, 'id': PLUGIN_ID}),
            'herdr-plugin.toml': 'id="' + PLUGIN_ID + '"\n',
            'bin/herdr-shell': 'print("helper")\n',
            'integrations/hyprland.lua': '-- bridge\n',
            'LICENSE': 'MIT License\n',
            'assets/branding/README.md': 'Upstream logo attribution\n',
            'herdr_shell/cli.py': 'pass\n',
            'herdr_shell/managed.py': 'pass\n',
            'herdr_shell/omarchy.py': 'pass\n',
            'herdr_shell/__init__.py': 'pass\n',
        }.items():
            target = self.source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.enabled(True)
        for patcher in (patch.object(omarchy.Path, 'home', return_value=self.home),
                        patch.object(omarchy, 'runtime_path', return_value=self.runtime),
                        patch.object(omarchy, 'control_path', return_value=self.control)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def enabled(self, value):
        self.config.write_text(json.dumps({'plugins': [{'id': PLUGIN_ID}] if value else []}))

    def copied(self):
        revision, files = omarchy.payload(self.source)
        omarchy.snapshot(self.source, revision, files)
        return revision

    def loop(self, revision, ticks=4, change=None):
        sleeps = []

        def tick(seconds):
            sleeps.append(seconds)
            if change:
                change(len(sleeps))
            if len(sleeps) >= ticks:
                raise EndLoop()

        with patch.object(omarchy, 'ROOT', self.runtime), patch.object(omarchy.time, 'sleep', side_effect=tick):
            with self.assertRaises(EndLoop):
                omarchy.watch(self.source, revision)

    def test_state_uses_native_manager_enable_and_disable(self):
        self.assertEqual(omarchy.source_state(self.source), 'enabled')
        self.config.write_text(json.dumps({'plugins': [PLUGIN_ID], 'disabledPlugins': [PLUGIN_ID]}))
        self.assertEqual(omarchy.source_state(self.source), 'disabled')
        self.enabled(False)
        self.assertEqual(omarchy.source_state(self.source), 'disabled')

    def test_incomplete_or_invalid_config_is_unknown_not_disable(self):
        for text in ('{', '[]', '{"plugins": {}}', '{"disabledPlugins": null}', '{"disabledPlugins": "bad"}'):
            with self.subTest(text=text):
                self.config.write_text(text)
                self.assertIsNone(omarchy.source_state(self.source))
        self.config.unlink()
        self.assertIsNone(omarchy.source_state(self.source))

    def test_invalid_manifest_and_status_types_do_not_crash_watch(self):
        for text in ('[]', 'null', '{', '{"schemaVersion":true}'):
            (self.source / 'manifest.json').write_text(text)
            self.assertIsNone(omarchy.source_state(self.source))
        self.control.mkdir(parents=True)
        (self.control / 'status.json').write_text('[]')
        self.assertEqual(omarchy.status()['state'], 'unknown')

    def test_payload_excludes_git_tests_and_detects_python_change(self):
        revision, files = omarchy.payload(self.source)
        (self.source / 'unrelated').write_text('outside runtime')
        self.assertEqual(omarchy.payload(self.source)[0], revision)
        self.assertNotIn('manifest.json', files)
        (self.source / 'herdr_shell/cli.py').write_text('changed = True\n')
        self.assertNotEqual(omarchy.payload(self.source)[0], revision)

    def test_invalid_new_code_keeps_previous_runtime(self):
        self.copied()
        before = (self.runtime / 'herdr_shell/cli.py').read_text()
        (self.source / 'herdr_shell/cli.py').write_text('if broken(\n')
        with self.assertRaises(SyntaxError):
            omarchy.payload(self.source)
        self.assertEqual((self.runtime / 'herdr_shell/cli.py').read_text(), before)

    def test_snapshot_permissions_and_stale_files(self):
        revision = self.copied()
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.runtime / 'bin/herdr-shell').stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.runtime / 'herdr-plugin.toml').stat().st_mode & 0o777, 0o600)
        (self.source / 'herdr_shell/obsolete.py').write_text('pass\n')
        self.copied()
        (self.runtime / 'personal.txt').write_text('keep me')
        (self.source / 'herdr_shell/obsolete.py').unlink()
        self.assertEqual(self.copied(), revision)
        self.assertFalse((self.runtime / 'herdr_shell/obsolete.py').exists())
        self.assertEqual((self.runtime / 'personal.txt').read_text(), 'keep me')

    def test_snapshot_clears_disposable_bytecode_after_code_update(self):
        self.copied()
        folder = self.runtime / 'herdr_shell/__pycache__'
        folder.mkdir()
        cached = folder / 'cli.cpython-313.pyc'
        cached.write_bytes(b'stale cache')
        self.copied()
        self.assertFalse(cached.exists())

    def test_unowned_runtime_and_unsafe_inventory_are_preserved(self):
        self.runtime.mkdir(parents=True)
        personal = self.runtime / 'personal.txt'
        personal.write_text('keep')
        revision, files = omarchy.payload(self.source)
        with self.assertRaisesRegex(ShellError, 'not owned'):
            omarchy.snapshot(self.source, revision, files)
        marker = self.runtime / '.managed-runtime.json'
        marker.write_text(json.dumps({'id': PLUGIN_ID, 'source': str(self.source), 'files': ['../../victim']}))
        with self.assertRaisesRegex(ShellError, 'Invalid.*inventory'):
            omarchy.snapshot(self.source, revision, files)
        self.assertEqual(personal.read_text(), 'keep')
        self.assertFalse((self.runtime / 'bin/herdr-shell').exists())

    def test_source_and_destination_symlinks_refused(self):
        target = self.source / 'herdr_shell/cli.py'
        target.unlink()
        target.symlink_to(self.source / 'herdr_shell/managed.py')
        with self.assertRaisesRegex(ShellError, 'symlinks'):
            omarchy.payload(self.source)
        target.unlink()
        target.write_text('pass\n')
        self.copied()
        target = self.runtime / 'herdr_shell/cli.py'
        target.unlink()
        target.symlink_to(self.source / 'herdr_shell/managed.py')
        revision, files = omarchy.payload(self.source)
        with self.assertRaisesRegex(ShellError, 'symlink'):
            omarchy.snapshot(self.source, revision, files)

    def test_start_refuses_developer_checkout_without_spawning(self):
        with patch.object(omarchy.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ShellError, 'developer links'):
                omarchy.start()
        spawn.assert_not_called()
        self.assertFalse(self.runtime.exists())

    def test_start_uses_cached_runtime_detached_and_revision_pinned(self):
        with patch.object(omarchy, 'ROOT', self.source), patch.object(omarchy.subprocess, 'Popen') as spawn:
            result = omarchy.start()
        self.assertTrue(result['started'])
        args, options = spawn.call_args
        self.assertEqual(args[0][1], str(self.runtime / 'bin/herdr-shell'))
        self.assertIn('--revision', args[0])
        self.assertTrue(options['start_new_session'])
        self.assertTrue(options['close_fds'])

    def test_stable_enable_runs_once_then_noops(self):
        revision = self.copied()
        with patch.object(omarchy, 'run_runtime', return_value='ready') as run:
            self.loop(revision)
        run.assert_called_once_with('activate')
        self.assertEqual(omarchy.status()['state'], 'ready')

    def test_unknown_config_never_triggers_cleanup(self):
        revision = self.copied()
        self.config.write_text('{')
        with patch.object(omarchy, 'run_runtime') as run:
            self.loop(revision)
        run.assert_not_called()

    def test_disable_resume_and_update_preference_flow(self):
        revision = self.copied()
        self.enabled(False)
        with patch.object(omarchy, 'run_runtime', return_value='ok') as run:
            self.loop(revision, ticks=6, change=lambda tick: self.enabled(True) if tick == 3 else None)
        self.assertEqual(run.call_args_list[0].args, ('deactivate',))
        self.assertEqual(run.call_args_list[0].kwargs, {'remove': False})
        self.assertEqual(run.call_args_list[1].args, ('activate',))
        self.assertEqual(run.call_count, 2)

    def test_remove_uses_cache_after_source_deleted(self):
        revision = self.copied()
        import shutil
        shutil.rmtree(self.source)
        with patch.object(omarchy, 'ROOT', self.runtime), patch.object(omarchy.time, 'sleep'), \
                patch.object(omarchy, 'run_runtime', return_value='removed') as run:
            self.assertEqual(omarchy.watch(self.source, revision), {'removed': True})
        run.assert_called_once_with('deactivate', remove=True)
        self.assertTrue((self.runtime / 'bin/herdr-shell').exists())

    def test_hot_update_restarts_supervisor_with_fresh_runtime(self):
        old = self.copied()
        (self.source / 'herdr_shell/omarchy.py').write_text('new_code = True\n')
        with patch.object(omarchy, 'ROOT', self.runtime), patch.object(omarchy.time, 'sleep'), \
                patch.object(omarchy.subprocess, 'Popen') as spawn, patch.object(omarchy, 'run_runtime') as run:
            result = omarchy.watch(self.source, old)
        self.assertEqual(result, {'restarting': True})
        self.assertIn('--wait', spawn.call_args.args[0])
        run.assert_not_called()
        self.assertIn('new_code', (self.runtime / 'herdr_shell/omarchy.py').read_text())

    def test_already_copied_update_still_restarts_old_supervisor(self):
        old = self.copied()
        (self.source / 'herdr_shell/omarchy.py').write_text('new_code = True\n')
        self.copied()  # QML start may have copied the new revision already.
        with patch.object(omarchy, 'ROOT', self.runtime), patch.object(omarchy.time, 'sleep'), \
                patch.object(omarchy.subprocess, 'Popen') as spawn:
            self.assertEqual(omarchy.watch(self.source, old), {'restarting': True})
        spawn.assert_called_once()

    def test_singleton_lock_skips_second_supervisor(self):
        revision = self.copied()
        with omarchy.locked('service.lock'), patch.object(omarchy, 'ROOT', self.runtime):
            result = omarchy.watch(self.source, revision)
        self.assertFalse(result['watching'])


if __name__ == '__main__':
    unittest.main()
