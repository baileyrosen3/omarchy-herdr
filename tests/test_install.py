"""Installation transactions exercised with real temporary config/helper files."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from herdr_shell import cli, config
from herdr_shell import PLUGIN_ID
from herdr_shell.runtime import ShellError


BASE = '# Preserve my settings\n[keys]\nprefix = "ctrl+space"\n\n[ui]\npane_gaps = false\n'
DEFAULTS = {'prefix': 'ctrl+space', 'focus_pane_left': 'ctrl+alt+left'}


class Server:
    def __init__(self, plugins=()):
        self.plugins = [dict(p) for p in plugins]
        self.calls = []
        self.reload_error = None
        self.unlink_error = None
        self.link_error = None

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == 'plugin.list':
            return {'plugins': [dict(p) for p in self.plugins]}
        if method == 'plugin.link':
            if self.link_error:
                raise self.link_error
            self.plugins = [p for p in self.plugins if p['plugin_id'] != PLUGIN_ID]
            self.plugins.append({'plugin_id': PLUGIN_ID, 'plugin_root': params['path'], 'enabled': params['enabled']})
            return {'plugin_id': PLUGIN_ID}
        if method == 'plugin.unlink':
            if self.unlink_error:
                raise self.unlink_error
            self.plugins = [p for p in self.plugins if p['plugin_id'] != params['plugin_id']]
            return {'unlinked': params['plugin_id']}
        if method == 'server.reload_config':
            if self.reload_error:
                raise self.reload_error
            return {'reloaded': True}
        raise AssertionError('Unexpected installation API: ' + method)


class InstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.path = self.home / 'config/herdr/config.toml'
        self.path.parent.mkdir(parents=True)
        self.path.write_text(BASE)
        self.root = Path(cli.__file__).resolve().parents[1]
        self.source = self.root / 'bin/herdr-shell'
        self.helper = self.home / '.local/bin/herdr-shell'
        self.validator = Mock(side_effect=tomllib.loads)
        self.store = config.ConfigStore(path=self.path, state=self.home / 'state', validator=self.validator)
        self.server = Server()
        self.factory = Mock(return_value=self.server)
        for patcher in (
            patch.object(cli.Path, 'home', return_value=self.home),
            patch.dict(os.environ, XDG_CONFIG_HOME=str(self.home / 'config'),
                       XDG_STATE_HOME=str(self.home / 'state'), HERDR_SOCKET_PATH='/wrong/default.sock'),
            patch.object(config, 'defaults', return_value=DEFAULTS),
            patch.object(cli, 'Client', self.factory),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, *options):
        args = cli.parser().parse_args([*options, 'install', '--apply'])
        with redirect_stdout(io.StringIO()):
            return cli.install(args, self.store)

    def existing(self, *, enabled=False):
        previous = self.home / 'previous-checkout'
        previous.mkdir()
        item = {'plugin_id': PLUGIN_ID, 'plugin_root': str(previous), 'enabled': enabled}
        self.server.plugins = [dict(item), {'plugin_id': 'unrelated.plugin', 'plugin_root': '/unrelated', 'enabled': True}]
        return item

    def test_preview_changes_no_plugin_helper_config_or_history(self):
        args = cli.parser().parse_args(['install'])
        with redirect_stdout(io.StringIO()):
            result = cli.install(args, self.store)
        self.assertEqual(result, {'preview': True})
        self.assertEqual(self.path.read_text(), BASE)
        self.assertFalse(self.helper.exists())
        self.assertFalse(self.store.state.exists())
        self.validator.assert_not_called()
        self.factory.assert_not_called()

    def test_success_orders_link_before_config_apply_and_reload(self):
        result = self.install('--socket', '/explicit/work.sock')
        self.assertEqual(result['installed'], PLUGIN_ID)
        self.assertTrue(result['changed'])
        self.assertTrue(self.helper.is_symlink())
        self.assertEqual(self.helper.resolve(), self.source)
        self.assertIn('# Preserve my settings', self.path.read_text())
        self.assertFalse(tomllib.loads(self.path.read_text())['ui']['pane_gaps'])
        self.assertEqual(len(tomllib.loads(self.path.read_text())['keys']['command']), 2)
        self.assertEqual([method for method, _ in self.server.calls],
                         ['plugin.list', 'plugin.link', 'server.reload_config'])
        self.factory.assert_called_once_with('/explicit/work.sock')
        self.assertEqual(self.server.plugins[0]['plugin_root'], str(self.root))
        self.assertEqual(self.validator.call_count, 2)

    def test_refresh_keeps_custom_and_disabled_fallbacks_without_adding_duplicates(self):
        for key in ('"prefix+f"', '["prefix+f", "prefix+g"]', '""'):
            with self.subTest(key=key):
                text = BASE + '\n[[keys.command]]\nkey=' + key + '\ntype="plugin_action"\ncommand="blr.herdr-shell.menu"\ndescription="My menu"\n' + \
                    '\n[[keys.command]]\nkey=""\ntype="plugin_action"\ncommand="blr.herdr-shell.keybindings"\ndescription="My keys"\n'
                self.path.write_text(text)
                result = self.install('--socket', '/explicit/work.sock')
                self.assertFalse(result['changed'])
                self.assertEqual(self.path.read_text(), text)
                commands = tomllib.loads(self.path.read_text())['keys']['command']
                self.assertEqual(len(commands), 2)
                self.assertEqual(commands[0]['key'], tomllib.loads('key=' + key)['key'])
                self.assertEqual(commands[1]['key'], '')

    def test_named_session_and_explicit_socket_select_link_and_reload_server(self):
        cases = [(['--session', 'work'], str(self.home / 'config/herdr/sessions/work/herdr.sock')),
                 (['--session', 'default'], str(self.home / 'config/herdr/herdr.sock')),
                 (['--socket', '/different/session.sock'], '/different/session.sock')]
        for options, socket in cases:
            with self.subTest(options=options):
                self.factory.reset_mock()
                self.install(*options)
                self.factory.assert_called_once_with(socket)
        self.assertNotIn('/wrong/default.sock', [args[0] for args, _ in self.factory.call_args_list])

    def test_preflight_validation_failure_has_no_side_effects(self):
        self.validator.side_effect = ShellError('config validation rejected')
        with self.assertRaisesRegex(ShellError, 'validation rejected'):
            self.install('--session', 'work')
        self.factory.assert_not_called()
        self.assertFalse(self.helper.exists())
        self.assertEqual(self.path.read_text(), BASE)
        self.assertFalse(self.store.state.exists())

    def test_native_shortcut_collision_is_rejected_before_plugin_api(self):
        self.path.write_text(BASE.replace('prefix = "ctrl+space"', 'prefix = "ctrl+space"\nzoom = "prefix+space"'))
        before = self.path.read_text()
        with self.assertRaisesRegex(ShellError, 'Shortcut collision'):
            self.install('--session', 'work')
        self.factory.assert_not_called()
        self.assertFalse(self.helper.exists())
        self.assertEqual(self.path.read_text(), before)

    def test_second_validation_failure_rolls_back_new_link_and_helper(self):
        self.validator.side_effect = [None, ShellError('validation changed')]
        with self.assertRaisesRegex(ShellError, 'validation changed'):
            self.install('--session', 'work')
        self.assertFalse(self.helper.exists())
        self.assertFalse(self.helper.is_symlink())
        self.assertEqual(self.server.plugins, [])
        self.assertEqual(self.path.read_text(), BASE)
        self.assertEqual([method for method, _ in self.server.calls],
                         ['plugin.list', 'plugin.link', 'plugin.list', 'plugin.unlink'])

    def test_reload_failure_restores_config_and_removes_new_plugin_and_helper(self):
        self.server.reload_error = ShellError('reload failed')
        with self.assertRaisesRegex(ShellError, 'Config reload failed'):
            self.install('--session', 'work')
        self.assertEqual(self.path.read_text(), BASE)
        self.assertFalse(self.helper.is_symlink())
        self.assertEqual(self.server.plugins, [])
        self.assertEqual(sum(method == 'server.reload_config' for method, _ in self.server.calls), 2)
        records = [json.loads(path.read_text()) for path in self.store.state.glob('*.json')]
        self.assertEqual([record['status'] for record in records], ['rolled_back'])

    def test_failure_restores_preexisting_plugin_root_and_disabled_state(self):
        previous = self.existing(enabled=False)
        self.validator.side_effect = [None, ShellError('late validation rejected')]
        with self.assertRaisesRegex(ShellError, 'late validation rejected'):
            self.install('--session', 'work')
        restored = next(p for p in self.server.plugins if p['plugin_id'] == PLUGIN_ID)
        self.assertEqual(restored, previous)
        self.assertIn('unrelated.plugin', [p['plugin_id'] for p in self.server.plugins])
        self.assertNotIn('plugin.unlink', [method for method, _ in self.server.calls])
        self.assertEqual(self.server.calls[-1], ('plugin.link', {'path': previous['plugin_root'], 'enabled': False}))
        self.assertFalse(self.helper.is_symlink())

    def test_existing_helper_is_preserved_on_failure(self):
        self.helper.parent.mkdir(parents=True)
        self.helper.symlink_to(self.source)
        self.server.reload_error = ShellError('reload failed')
        with self.assertRaises(ShellError):
            self.install('--session', 'work')
        self.assertTrue(self.helper.is_symlink())
        self.assertEqual(self.helper.resolve(), self.source)

    def test_concurrent_plugin_replacement_is_never_unlinked_or_restored(self):
        previous = self.existing(enabled=False)
        replacement = self.home / 'concurrent-checkout'
        replacement.mkdir()

        def concurrent_failure(proposal, reload):
            self.server.plugins = [{'plugin_id': PLUGIN_ID, 'plugin_root': str(replacement), 'enabled': True}]
            raise ShellError('concurrent update')

        with patch.object(self.store, 'apply', side_effect=concurrent_failure):
            with self.assertRaisesRegex(ShellError, 'concurrent update'):
                self.install('--session', 'work')
        self.assertEqual(self.server.plugins[0]['plugin_root'], str(replacement))
        links = [params['path'] for method, params in self.server.calls if method == 'plugin.link']
        self.assertEqual(links, [str(self.root)])
        self.assertNotIn(previous['plugin_root'], links)
        self.assertNotIn('plugin.unlink', [method for method, _ in self.server.calls])
        self.assertFalse(self.helper.is_symlink())

    def test_concurrent_helper_replacement_survives_rollback(self):
        replacement = self.home / 'different-helper'
        replacement.write_text('preserve me')

        def concurrent_failure(proposal, reload):
            self.helper.unlink()
            self.helper.symlink_to(replacement)
            raise ShellError('concurrent helper replacement')

        with patch.object(self.store, 'apply', side_effect=concurrent_failure):
            with self.assertRaisesRegex(ShellError, 'concurrent helper replacement'):
                self.install('--session', 'work')
        self.assertEqual(self.helper.resolve(), replacement)
        self.assertEqual(replacement.read_text(), 'preserve me')
        self.assertEqual(self.server.plugins, [])

    def test_helper_created_during_link_is_verified_and_preserved(self):
        replacement = self.home / 'different-helper'
        replacement.write_text('preserve me')
        call = self.server.call

        def replace_helper(method, **params):
            result = call(method, **params)
            if method == 'plugin.link':
                self.helper.parent.mkdir(parents=True)
                self.helper.symlink_to(replacement)
            return result

        with patch.object(self.server, 'call', side_effect=replace_helper):
            with self.assertRaisesRegex(ShellError, 'another installation'):
                self.install('--session', 'work')
        self.assertEqual(self.helper.resolve(), replacement)
        self.assertEqual(replacement.read_text(), 'preserve me')
        self.assertEqual(self.server.plugins, [])
        self.assertEqual(self.path.read_text(), BASE)
        self.assertFalse(self.store.state.exists())

    def test_existing_helper_replaced_during_link_is_verified_and_preserved(self):
        self.helper.parent.mkdir(parents=True)
        self.helper.symlink_to(self.source)
        replacement = self.home / 'different-helper'
        replacement.write_text('preserve me')
        call = self.server.call

        def replace_helper(method, **params):
            result = call(method, **params)
            if method == 'plugin.link':
                self.helper.unlink()
                self.helper.symlink_to(replacement)
            return result

        with patch.object(self.server, 'call', side_effect=replace_helper):
            with self.assertRaisesRegex(ShellError, 'another installation'):
                self.install('--session', 'work')
        self.assertEqual(self.helper.resolve(), replacement)
        self.assertEqual(self.server.plugins, [])
        self.assertEqual(self.path.read_text(), BASE)

    def test_lost_link_response_after_commit_removes_new_plugin(self):
        call = self.server.call

        def lose_response(method, **params):
            result = call(method, **params)
            if method == 'plugin.link':
                raise ShellError('link response lost')
            return result

        with patch.object(self.server, 'call', side_effect=lose_response):
            with self.assertRaisesRegex(ShellError, 'link response lost'):
                self.install('--session', 'work')
        self.assertEqual(self.server.plugins, [])
        self.assertFalse(self.helper.exists())
        self.assertEqual(self.path.read_text(), BASE)
        self.assertEqual([method for method, _ in self.server.calls],
                         ['plugin.list', 'plugin.link', 'plugin.list', 'plugin.unlink'])

    def test_lost_link_response_restores_previous_root_and_disabled_state(self):
        previous = self.existing(enabled=False)
        call = self.server.call

        def lose_response(method, **params):
            result = call(method, **params)
            if method == 'plugin.link' and params['path'] == str(self.root):
                raise ShellError('link response lost')
            return result

        with patch.object(self.server, 'call', side_effect=lose_response):
            with self.assertRaisesRegex(ShellError, 'link response lost'):
                self.install('--session', 'work')
        self.assertEqual(next(p for p in self.server.plugins if p['plugin_id'] == PLUGIN_ID), previous)
        self.assertIn('unrelated.plugin', [p['plugin_id'] for p in self.server.plugins])
        self.assertFalse(self.helper.exists())
        self.assertEqual(self.path.read_text(), BASE)

    def test_lost_link_response_preserves_concurrent_plugin_replacement(self):
        replacement = self.home / 'concurrent-checkout'
        replacement.mkdir()
        call = self.server.call

        def replace_then_lose_response(method, **params):
            result = call(method, **params)
            if method == 'plugin.link':
                self.server.plugins = [{'plugin_id': PLUGIN_ID, 'plugin_root': str(replacement), 'enabled': True}]
                raise ShellError('link response lost')
            return result

        with patch.object(self.server, 'call', side_effect=replace_then_lose_response):
            with self.assertRaisesRegex(ShellError, 'link response lost'):
                self.install('--session', 'work')
        self.assertEqual(self.server.plugins[0]['plugin_root'], str(replacement))
        self.assertNotIn('plugin.unlink', [method for method, _ in self.server.calls])
        self.assertFalse(self.helper.exists())
        self.assertEqual(self.path.read_text(), BASE)

    def test_unrelated_helper_file_or_symlink_refuses_before_plugin_api(self):
        self.helper.parent.mkdir(parents=True)
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                if self.helper.exists() or self.helper.is_symlink():
                    self.helper.unlink()
                target = self.home / 'other-helper'
                target.write_text('existing helper')
                if symlink:
                    self.helper.symlink_to(target)
                else:
                    self.helper.write_text('existing helper')
                with self.assertRaisesRegex(ShellError, 'another installation'):
                    self.install('--session', 'work')
                self.assertEqual(self.helper.read_text(), 'existing helper')
                self.factory.assert_not_called()
                self.validator.assert_not_called()
                self.assertEqual(self.path.read_text(), BASE)

    def test_dangling_unrelated_helper_symlink_is_not_overwritten(self):
        self.helper.parent.mkdir(parents=True)
        target = self.home / 'missing-helper'
        self.helper.symlink_to(target)
        with self.assertRaisesRegex(ShellError, 'another installation'):
            self.install('--session', 'work')
        self.assertTrue(self.helper.is_symlink())
        self.assertEqual(self.helper.readlink(), target)
        self.factory.assert_not_called()

    def test_link_rejection_creates_no_helper_or_config_changes(self):
        self.server.link_error = ShellError('plugin manifest rejected')
        with self.assertRaisesRegex(ShellError, 'manifest rejected'):
            self.install('--session', 'work')
        self.assertFalse(self.helper.exists())
        self.assertEqual(self.path.read_text(), BASE)
        self.assertEqual(self.server.plugins, [])

    def test_rollback_failure_reports_original_error_and_remaining_link(self):
        self.validator.side_effect = [None, ShellError('config rejected')]
        self.server.unlink_error = ShellError('unlink denied')
        with self.assertRaisesRegex(ShellError, 'config rejected; plugin setup rollback failed: unlink denied'):
            self.install('--session', 'work')
        self.assertFalse(self.helper.is_symlink())
        self.assertEqual(self.path.read_text(), BASE)
        self.assertEqual(self.server.plugins[0]['plugin_root'], str(self.root))


if __name__ == '__main__':
    unittest.main()
