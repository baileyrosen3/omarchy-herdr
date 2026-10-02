"""Lifecycle commands preserve user data and exercise transactional failures."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from herdr_shell import PLUGIN_ID, config, lifecycle, footer_setup
from herdr_shell.runtime import ShellError


BASE = '''# Personal config stays here
[keys]
prefix="ctrl+space"
[[keys.command]]
key="prefix+space"
type="plugin_action"
command="blr.herdr-shell.menu"
description="My menu key"
[[keys.command]]
key="prefix+k"
type="plugin_action"
command="blr.herdr-shell.keybindings"
description="My binding key"
[[keys.command]]
key="prefix+o"
type="plugin_action"
command="other.plugin.menu"
description="Another plugin"
[[keys.command]]
key="prefix+s"
type="shell"
command="blr.herdr-shell.menu"
description="A shell command"
[[keys.command]]
key="prefix+a"
type="plugin_action"
command="blr.herdr-shell-extra.menu"
description="A different plugin prefix"
[ui]
pane_gaps=false
'''


class Server:
    def __init__(self, root, events):
        self.path = "/selected/session.sock"
        self.plugins = [{"plugin_id": PLUGIN_ID, "plugin_root": str(root), "enabled": False}]
        self.events = events
        self.reload = None
        self.unlink = None

    def call(self, method, **params):
        self.events.append((method, params))
        if method == "plugin.list":
            return {"plugins": [dict(p) for p in self.plugins]}
        if method == "server.reload_config":
            if self.reload:
                self.reload()
            return {}
        if method == "plugin.unlink":
            if self.unlink:
                self.unlink()
            else:
                self.plugins = [p for p in self.plugins if p["plugin_id"] != params["plugin_id"]]
            return {}
        if method == "plugin.link":
            self.plugins = [p for p in self.plugins if p["plugin_id"] != PLUGIN_ID]
            self.plugins.append({"plugin_id": PLUGIN_ID, "plugin_root": params["path"], "enabled": params["enabled"]})
            return {}
        raise AssertionError(method)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "checkout"
        (self.root / "bin").mkdir(parents=True)
        self.source = self.root / "bin/herdr-shell"
        self.source.write_text("fixture helper")
        self.helper = self.home / ".local/bin/herdr-shell"
        self.helper.parent.mkdir(parents=True)
        self.helper.symlink_to(self.source)
        self.path = self.home / "config.toml"
        self.path.write_text(BASE)
        self.store = config.ConfigStore(self.path, self.home / "history", Mock(side_effect=tomllib.loads))
        self.events = []
        self.server = Server(self.root, self.events)
        self.factory = Mock(return_value=self.server)
        self.prefs = self.home / "preferences"
        self.prefs.mkdir()
        (self.prefs / "plugin-root").write_text(str(self.root) + "\n")
        (self.prefs / "desktop-enabled").write_text("0\n")
        self.hypr = self.home / "hyprland.lua"
        self.hypr.write_text("personal desktop settings\n")
        self.desktop = SimpleNamespace(installed=lambda: "managed bridge\n" in self.hypr.read_text(),
                                       enabled=lambda: (self.prefs / "desktop-enabled").read_text().strip() == "1",
                                       preferences_dir=lambda: self.prefs, hypr_path=lambda: self.hypr,
                                       install_proposal=self.desktop_proposal, install_desktop=self.desktop_apply)
        for patcher in (patch.object(lifecycle, "ROOT", self.root),
                        patch.object(lifecycle.Path, "home", return_value=self.home),
                        patch.object(lifecycle, "desktop", self.desktop),
                        patch.object(lifecycle, "Client", self.factory),
                        patch.object(config, "defaults", return_value={"prefix": "ctrl+space", "focus_pane_left": "ctrl+alt+left"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, *, apply=True, local=True, **values):
        return SimpleNamespace(apply=apply, local=local, socket="/selected/session.sock", session=None,
                               config=str(self.path), **values)

    def bridge(self, enabled=False):
        self.hypr.write_text("personal desktop settings\nmanaged bridge\n")
        (self.prefs / "desktop-enabled").write_text("1\n" if enabled else "0\n")

    def desktop_proposal(self, remove=False):
        before = self.hypr.read_text()
        after = before.replace("managed bridge\n", "")
        if not remove:
            after += "managed bridge\n"
        return {"path": str(self.hypr), "before": before, "after": after}

    def desktop_apply(self, proposal, remove=False, *, activate=True):
        self.events.append(("desktop.remove" if remove else "desktop.install", {"activate": activate}))
        if self.hypr.read_text() != proposal["before"]:
            raise ShellError("desktop changed")
        self.hypr.write_text(proposal["after"])
        (self.prefs / "desktop-enabled").write_text("0\n" if remove or not activate else "1\n")
        return {"installed": not remove, "enabled": self.desktop.enabled()}

    def remove(self, args=None):
        with redirect_stdout(io.StringIO()):
            return lifecycle.remove(args or self.args(), self.store)

    def commands(self):
        return tomllib.loads(self.path.read_text())["keys"]["command"]

    def test_remove_preview_has_no_api_config_helper_desktop_or_history_changes(self):
        self.bridge(True)
        before = self.hypr.read_text()
        result = self.remove(self.args(apply=False))
        self.assertEqual(result["shortcuts"], 2)
        self.assertTrue(result["desktop"])
        self.factory.assert_not_called()
        self.assertEqual(self.path.read_text(), BASE)
        self.assertEqual(self.hypr.read_text(), before)
        self.assertTrue(self.helper.is_symlink())
        self.assertFalse(self.store.state.exists())

    def test_remove_only_our_plugin_action_commands_and_resources(self):
        self.bridge(True)
        practice = self.home / "practice-workspace-record"
        practice.write_text("keep practice")
        result = self.remove()
        self.assertTrue(result["changed"])
        self.assertTrue(result["desktop"] and result["plugin"] and result["helper"])
        commands = self.commands()
        self.assertEqual([c["command"] for c in commands], ["other.plugin.menu", "blr.herdr-shell.menu", "blr.herdr-shell-extra.menu"])
        self.assertEqual(commands[1]["type"], "shell")
        self.assertFalse(tomllib.loads(self.path.read_text())["ui"]["pane_gaps"])
        self.assertIn("# Personal config", self.path.read_text())
        self.assertTrue(self.root.exists() and self.store.state.exists())
        self.assertEqual(practice.read_text(), "keep practice")
        self.assertEqual(self.hypr.read_text(), "personal desktop settings\n")
        methods = [method for method, _ in self.events]
        self.assertLess(methods.index("desktop.remove"), methods.index("plugin.unlink"))
        self.assertLess(methods.index("server.reload_config"), methods.index("plugin.unlink"))
        self.assertFalse(self.helper.is_symlink())
        self.factory.assert_called_once_with("/selected/session.sock")

    def test_remove_is_idempotent(self):
        self.remove()
        config_after = self.path.read_text()
        self.events.clear()
        result = self.remove()
        self.assertFalse(result["changed"] or result["plugin"] or result["helper"] or result["desktop"])
        self.assertEqual(self.path.read_text(), config_after)
        self.assertEqual(self.events, [("plugin.list", {})])

    def test_remove_validation_failure_happens_before_mutations(self):
        self.bridge(True)
        self.store.validator.side_effect = ShellError("invalid proposed config")
        with self.assertRaisesRegex(ShellError, "invalid proposed"):
            self.remove()
        self.factory.assert_not_called()
        self.assertTrue(self.desktop.installed() and self.desktop.enabled())
        self.assertTrue(self.helper.is_symlink())
        self.assertEqual(self.path.read_text(), BASE)

    def test_reload_failure_restores_native_config_and_disabled_desktop(self):
        self.bridge(False)
        self.server.reload = Mock(side_effect=ShellError("reload rejected"))
        with self.assertRaisesRegex(ShellError, "Config reload failed"):
            self.remove()
        self.assertEqual(self.path.read_text(), BASE)
        self.assertTrue(self.desktop.installed())
        self.assertFalse(self.desktop.enabled())
        self.assertTrue(self.helper.is_symlink())
        self.assertEqual(len(self.server.plugins), 1)
        self.assertNotIn("plugin.unlink", [m for m, _ in self.events])
        self.assertIn(("desktop.install", {"activate": False}), self.events)

    def test_concurrent_personal_native_edit_survives_reload_failure(self):
        def reject_after_edit():
            self.path.write_text(self.path.read_text().replace("pane_gaps=false", "pane_gaps=true"))
            raise ShellError("reload rejected after edit")

        self.server.reload = reject_after_edit
        with self.assertRaisesRegex(ShellError, "reload_failed_concurrent_edit"):
            self.remove()
        self.assertTrue(tomllib.loads(self.path.read_text())["ui"]["pane_gaps"])
        self.assertFalse(any(c["type"] == "plugin_action" and c["command"].startswith(PLUGIN_ID + ".") for c in self.commands()))
        self.assertTrue(self.helper.is_symlink())

    def test_lost_unlink_response_restores_plugin_before_config_reload_rollback(self):
        self.bridge(False)
        previous = dict(self.server.plugins[0])

        def unlink_then_fail():
            self.server.plugins = []
            raise ShellError("unlink response lost")

        self.server.unlink = unlink_then_fail
        with self.assertRaisesRegex(ShellError, "unlink response lost"):
            self.remove()
        self.assertEqual(self.server.plugins, [previous])
        self.assertEqual(self.path.read_text(), BASE)
        self.assertTrue(self.helper.is_symlink())
        self.assertFalse(self.desktop.enabled())
        methods = [m for m, _ in self.events]
        self.assertLess(methods.index("plugin.link"), len(methods) - 1)
        self.assertEqual(methods[-2:], ["server.reload_config", "desktop.install"])

    def test_helper_unlink_failure_restores_plugin_and_native_config(self):
        previous = dict(self.server.plugins[0])
        original_unlink = Path.unlink

        def reject_helper(path, *args, **kwargs):
            if path == self.helper:
                raise PermissionError("helper removal denied")
            return original_unlink(path, *args, **kwargs)

        with patch.object(lifecycle.Path, "unlink", autospec=True, side_effect=reject_helper):
            with self.assertRaisesRegex(ShellError, "helper removal denied"):
                self.remove()
        self.assertEqual(self.server.plugins, [previous])
        self.assertEqual(self.path.read_text(), BASE)
        self.assertTrue(self.helper.is_symlink())

    def test_changed_same_root_enabled_preference_is_preserved(self):
        def enable_plugin():
            self.server.plugins[0]["enabled"] = True

        self.server.reload = enable_plugin
        with self.assertRaisesRegex(ShellError, "registration changed during removal"):
            self.remove()
        self.assertTrue(self.server.plugins[0]["enabled"])
        self.assertEqual(self.path.read_text(), BASE)
        self.assertTrue(self.helper.is_symlink())

    def test_named_session_selects_the_reload_and_unlink_client(self):
        args = self.args()
        args.socket, args.session = None, "work"
        with patch.dict(os.environ, XDG_CONFIG_HOME=str(self.home / "config")):
            self.remove(args)
        self.factory.assert_called_once_with(str(self.home / "config/herdr/sessions/work/herdr.sock"))

    def test_concurrent_plugin_and_helper_replacements_are_preserved(self):
        replacement = self.home / "replacement"
        replacement.write_text("different helper")

        def replace_resources():
            self.server.plugins = [{"plugin_id": PLUGIN_ID, "plugin_root": "/another/checkout", "enabled": True}]
            self.helper.unlink()
            self.helper.symlink_to(replacement)

        self.server.reload = replace_resources
        with self.assertRaisesRegex(ShellError, "registration changed during removal"):
            self.remove()
        self.assertEqual(self.path.read_text(), BASE)
        self.assertEqual(self.server.plugins[0]["plugin_root"], "/another/checkout")
        self.assertEqual(self.helper.resolve(), replacement)
        self.assertNotIn("plugin.unlink", [m for m, _ in self.events])

    def test_other_plugin_root_refuses_before_any_config_or_desktop_removal(self):
        self.bridge(True)
        self.server.plugins[0]["plugin_root"] = "/another/checkout"
        self.helper.unlink()
        self.helper.write_text("preserve this file")
        with self.assertRaisesRegex(ShellError, "linked from another checkout"):
            self.remove()
        self.assertEqual(self.path.read_text(), BASE)
        self.assertTrue(self.desktop.installed() and self.desktop.enabled())
        self.assertEqual(self.helper.read_text(), "preserve this file")
        self.assertEqual(self.server.plugins[0]["plugin_root"], "/another/checkout")
        self.assertEqual(self.events, [("plugin.list", {})])

    def test_other_helper_survives_removal_of_our_plugin(self):
        self.helper.unlink()
        self.helper.write_text("preserve this file")
        result = self.remove()
        self.assertTrue(result["plugin"])
        self.assertFalse(result["helper"])
        self.assertEqual(self.helper.read_text(), "preserve this file")

    def test_later_personal_desktop_edit_is_preserved_on_removal_failure(self):
        self.bridge(False)

        def edit_and_reject():
            self.hypr.write_text("personal desktop settings\nlater personal bind\n")
            raise ShellError("reload rejected")

        self.server.reload = edit_and_reject
        with self.assertRaisesRegex(ShellError, "desktop config changed; preserved the later edit"):
            self.remove()
        self.assertEqual(self.hypr.read_text(), "personal desktop settings\nlater personal bind\n")
        self.assertEqual(self.path.read_text(), BASE)
        self.assertFalse(self.desktop.enabled())
        self.assertTrue(self.helper.is_symlink())

    def test_other_desktop_root_refuses_update_and_remove_before_mutations(self):
        self.bridge(True)
        (self.prefs / "plugin-root").write_text("/different/checkout\n")
        callback = Mock()
        for operation in (lambda: self.remove(), lambda: lifecycle.update(self.args(), self.store, callback)):
            with self.assertRaisesRegex(ShellError, "another checkout"):
                operation()
        self.assertEqual(self.path.read_text(), BASE)
        self.factory.assert_not_called()
        callback.assert_not_called()

    def test_local_update_never_runs_git_and_preserves_disabled_bridge(self):
        self.bridge(False)
        callback = Mock(return_value={"installed": PLUGIN_ID})
        args = self.args()
        with patch.object(lifecycle, "_git") as git:
            result = lifecycle.update(args, self.store, callback)
        git.assert_not_called()
        callback.assert_called_once_with(args, self.store)
        self.assertFalse(result["pulled"])
        self.assertTrue(self.desktop.installed())
        self.assertFalse(self.desktop.enabled())
        self.assertIn(("desktop.install", {"activate": False}), self.events)

    def test_local_update_preserves_enabled_bridge(self):
        self.bridge(True)
        lifecycle.update(self.args(), self.store, Mock(return_value={"installed": PLUGIN_ID}))
        self.assertTrue(self.desktop.enabled())
        self.assertIn(("desktop.install", {"activate": True}), self.events)

    def test_update_without_desktop_does_not_install_it(self):
        result = lifecycle.update(self.args(), self.store, Mock(return_value={"installed": PLUGIN_ID}))
        self.assertIsNone(result["desktop"])
        self.assertFalse(self.desktop.installed())

    def test_update_preview_never_calls_installer_or_selected_server(self):
        callback = Mock()
        with patch.object(lifecycle, "_git_preflight", return_value={"head": "old"}) as preflight:
            result = lifecycle.update(self.args(apply=False, local=False), self.store, callback)
        self.assertTrue(result["preview"])
        preflight.assert_called_once()
        callback.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(self.path.read_text(), BASE)

    def test_dirty_or_unconfigured_git_refuses_before_installer(self):
        callback = Mock()
        with patch.object(lifecycle, "_git_preflight", side_effect=ShellError("checkout has local changes")):
            with self.assertRaisesRegex(ShellError, "local changes"):
                lifecycle.update(self.args(local=False), self.store, callback)
        callback.assert_not_called()
        self.factory.assert_not_called()

    def test_git_preflight_requires_own_root_clean_status_branch_and_upstream(self):
        with patch.object(lifecycle, "_git", side_effect=[str(self.root), "", "main", "origin/main", "old"]) as git:
            result = lifecycle._git_preflight()
        self.assertEqual(result, {"branch": "main", "upstream": "origin/main", "head": "old"})
        self.assertEqual(git.call_args_list[1].args, ("status", "--porcelain"))
        with patch.object(lifecycle, "_git", side_effect=[str(self.root), " M file"]):
            with self.assertRaisesRegex(ShellError, "local changes"):
                lifecycle._git_preflight()
        with patch.object(lifecycle, "_git", return_value="/another/root"):
            with self.assertRaisesRegex(ShellError, "own Git checkout"):
                lifecycle._git_preflight()

    def test_fast_forward_that_changes_head_reenters_fresh_selected_cli(self):
        callback = Mock()
        git_state = {"head": "old", "branch": "main", "upstream": "origin/main"}
        with patch.object(lifecycle, "_git_preflight", return_value=git_state), \
             patch.object(lifecycle, "_git", side_effect=["pulled", "new"]) as git, \
             patch.object(lifecycle.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="refreshed", stderr="")) as process:
            result = lifecycle.update(self.args(local=False), self.store, callback)
        callback.assert_not_called()
        self.assertTrue(result["pulled"])
        self.assertEqual(git.call_args_list[0].args, ("pull", "--ff-only", "--no-rebase"))
        command = process.call_args.args[0]
        self.assertEqual(command[1:], [str(self.source), "--socket", "/selected/session.sock", "--config", str(self.path),
                                     "update", "--local", "--apply"])

    def test_no_new_commit_refreshes_in_process(self):
        callback = Mock(return_value={"installed": PLUGIN_ID})
        state = {"head": "old"}
        with patch.object(lifecycle, "_git_preflight", return_value=state), \
             patch.object(lifecycle, "_git", side_effect=["already current", "old"]):
            result = lifecycle.update(self.args(local=False), self.store, callback)
        self.assertFalse(result["pulled"])
        callback.assert_called_once()

    def test_changed_preflight_and_nonfastforward_failure_do_not_install(self):
        callback = Mock()
        with patch.object(lifecycle, "_git_preflight", side_effect=[{"head": "old"}, {"head": "different"}]), \
             patch.object(lifecycle, "_git") as git:
            with self.assertRaisesRegex(ShellError, "changed during update"):
                lifecycle.update(self.args(local=False), self.store, callback)
        git.assert_not_called()
        with patch.object(lifecycle, "_git_preflight", return_value={"head": "old"}), \
             patch.object(lifecycle, "_git", side_effect=ShellError("not possible to fast-forward")):
            with self.assertRaisesRegex(ShellError, "fast-forward"):
                lifecycle.update(self.args(local=False), self.store, callback)
        callback.assert_not_called()

    def test_update_different_plugin_root_or_helper_refuses_before_pull(self):
        callback = Mock()
        self.server.plugins[0]["plugin_root"] = "/another/checkout"
        with patch.object(lifecycle, "_git_preflight", return_value={"head": "old"}), patch.object(lifecycle, "_git") as git:
            with self.assertRaisesRegex(ShellError, "linked from another checkout"):
                lifecycle.update(self.args(local=False), self.store, callback)
        git.assert_not_called()
        self.helper.unlink()
        self.helper.write_text("another helper")
        with self.assertRaisesRegex(ShellError, "another installation"):
            lifecycle.update(self.args(), self.store, callback)
        callback.assert_not_called()

    def test_fresh_process_failure_reports_pulled_checkout_without_reset(self):
        with patch.object(lifecycle, "_git_preflight", return_value={"head": "old"}), \
             patch.object(lifecycle, "_git", side_effect=["pulled", "new"]), \
             patch.object(lifecycle.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="manifest invalid")):
            with self.assertRaisesRegex(ShellError, "Git was updated.*update --local --apply.*manifest invalid"):
                lifecycle.update(self.args(local=False), self.store, Mock())

    def test_remove_cleans_exact_provider_and_preserves_personal_footer_entries(self):
        entry = {"type": "command", "command": footer_setup.provider_command(self.root),
                 "interval_seconds": 19, "timeout_seconds": 7}
        personal = {"type": "text", "text": "My status"}
        self.store.apply(self.store.prepare([(["ui", "footer"], [entry, personal]),
                                            (["ui", "tab_bar_position"], "top")]))
        self.remove()
        ui = tomllib.loads(self.path.read_text())["ui"]
        self.assertEqual(ui["footer"], [personal])
        self.assertEqual(ui["tab_bar_position"], "top")


if __name__ == "__main__":
    unittest.main()
