"""Managed service setup works offline and preserves ownership/preferences."""
from copy import deepcopy
import errno
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import socket
import stat
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from herdr_shell import PLUGIN_ID, config, managed
from herdr_shell.runtime import ShellError


BASE = '''# Keep my preferences
[keys]
prefix="ctrl+space"
[[keys.command]]
key="prefix+o"
type="plugin_action"
command="other.plugin.menu"
description="Personal command"
[ui]
pane_gaps=false
'''


class Native:
    def __init__(self):
        self.plugins = []
        self.calls = []
        self.failure = None
        self.after_failure = None

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "list":
            return {"plugins": deepcopy(self.plugins)}
        if self.failure:
            self.failure(args)
        if args[0] == "link":
            self.plugins = [{"plugin_id": PLUGIN_ID, "plugin_root": args[1],
                             "enabled": args[2] == "--enabled", "source": {"kind": "local"}}]
        elif args == ("uninstall", PLUGIN_ID):
            self.plugins = []
        else:
            raise AssertionError(args)
        if self.after_failure:
            self.after_failure(args)
        return {"plugin": deepcopy(self.plugins[0])} if self.plugins else {"removed": True}


class ManagedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "data/herdr-shell/omarchy/runtime"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "bin/herdr-shell").write_text("fixture executable")
        self.state = self.home / "state"
        self.helper = self.home / ".local/bin/herdr-shell"
        self.path = self.home / "config/herdr/config.toml"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(BASE)
        self.store = config.ConfigStore(self.path, self.state, Mock(side_effect=tomllib.loads))
        self.native = Native()
        self.prefs = self.home / "config/herdr-shell"
        self.hypr = self.home / "config/hypr/hyprland.lua"
        self.hypr.parent.mkdir(parents=True)
        self.hypr.write_text('require("default.hypr.omarchy")\n')
        self.desktop_calls = []
        self.reload = Mock(return_value={"reloaded": [], "skipped": []})
        self.desktop = SimpleNamespace(preferences_dir=lambda: self.prefs, hypr_path=lambda: self.hypr,
            installed=lambda: "managed bridge\n" in self.hypr.read_text(),
            enabled=lambda: (self.prefs / "desktop-enabled").exists() and (self.prefs / "desktop-enabled").read_text() == "1\n",
            integration_text=lambda: "desktop code", install_proposal=self.proposal,
            install_desktop=self.install_desktop, set_enabled=self.set_enabled, hypr=Mock(), reconcile=Mock())
        for patcher in (patch.object(managed, "ROOT", self.root),
                        patch.object(managed.Path, "home", return_value=self.home),
                        patch.object(managed, "state_path", return_value=self.state),
                        patch.object(managed, "config_path", return_value=self.path),
                        patch.object(managed, "ConfigStore", return_value=self.store),
                        patch.object(managed, "_native", self.native),
                        patch.object(managed, "desktop", self.desktop),
                        patch.object(managed, "_reload_servers", self.reload),
                        patch.object(config, "defaults", return_value={"prefix": "ctrl+space", "focus_pane_left": "ctrl+alt+left"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def proposal(self, remove=False):
        before = self.hypr.read_text()
        after = before.replace("managed bridge\n", "")
        if not remove:
            after += "managed bridge\n"
        return {"path": str(self.hypr), "before": before, "after": after}

    def install_desktop(self, proposal, remove=False, activate=True):
        self.desktop_calls.append(("remove" if remove else "install", activate))
        self.hypr.write_text(proposal["after"])
        self.prefs.mkdir(exist_ok=True)
        if not remove:
            (self.prefs / "plugin-root").write_text(str(self.root) + "\n")
            (self.prefs / "hyprland.lua").write_text("desktop code")
        (self.prefs / "desktop-enabled").write_text("1\n" if activate and not remove else "0\n")

    def set_enabled(self, value):
        self.desktop_calls.append(("enabled", value))
        (self.prefs / "desktop-enabled").write_text("1\n" if value else "0\n")

    def install(self):
        result = managed.activate()
        self.assertTrue(result["active"])
        return result

    def test_first_install_without_running_server_adds_only_owned_resources(self):
        result = self.install()
        self.assertTrue(result["changed"])
        self.assertTrue(self.helper.is_symlink())
        self.assertEqual(self.helper.resolve(), self.root / "bin/herdr-shell")
        doc = tomllib.loads(self.path.read_text())
        self.assertEqual(len(doc["keys"]["command"]), 3)
        self.assertEqual(doc["keys"]["command"][0]["command"], "other.plugin.menu")
        self.assertFalse(doc["ui"]["pane_gaps"])
        self.assertIn("# Keep my preferences", self.path.read_text())
        self.assertEqual(self.native.plugins[0]["plugin_root"], str(self.root))
        self.assertTrue(self.native.plugins[0]["enabled"])
        self.assertTrue(self.desktop.enabled())
        self.reload.assert_called_once()

    def test_first_install_with_reserved_native_recovery_still_installs_desktop(self):
        before = BASE.replace('prefix="ctrl+space"', 'prefix="ctrl+space"\nzoom="prefix+space"')
        self.path.write_text(before)
        result = self.install()
        self.assertTrue(self.desktop.installed())
        self.assertTrue(self.desktop.enabled())
        self.assertEqual(self.desktop_calls, [("install", True)])
        self.assertTrue(self.helper.is_symlink())
        self.assertEqual(self.native.plugins[0]["plugin_root"], str(self.root))
        doc = tomllib.loads(self.path.read_text())
        self.assertEqual(doc["keys"]["zoom"], "prefix+space")
        self.assertFalse(doc["ui"]["pane_gaps"])
        self.assertIn("# Keep my preferences", self.path.read_text())
        commands = {command["command"]: command for command in doc["keys"]["command"]}
        self.assertEqual(commands["other.plugin.menu"]["key"], "prefix+o")
        self.assertEqual(commands["blr.herdr-shell.menu"]["key"], "")
        self.assertEqual(commands["blr.herdr-shell.keybindings"]["key"], "prefix+alt+k")
        self.assertEqual(result["reserved_shortcuts"][0]["action"], "menu")
        self.assertEqual(result["reserved_shortcuts"][0]["owners"][0]["id"], "zoom")
        self.assertEqual(config.conflicts(self.path.read_text()), config.conflicts(before))
        self.reload.assert_called_once()

    def test_unchanged_activation_is_noop_including_receipt(self):
        self.install()
        receipt = managed._state() / "setup.json"
        before = receipt.stat().st_mtime_ns
        self.native.calls.clear(); self.desktop_calls.clear(); self.reload.reset_mock()
        self.assertFalse(managed.activate()["changed"])
        self.assertEqual(receipt.stat().st_mtime_ns, before)
        self.assertEqual(self.desktop_calls, [])
        self.assertTrue(all(c[0] == "list" for c in self.native.calls))
        self.reload.assert_not_called()

    def test_python_update_refreshes_active_desktop_with_unchanged_lua(self):
        runtime_python = self.root / "herdr_shell/config.py"
        runtime_python.parent.mkdir()
        runtime_python.write_text("# original conflict checker\n")
        self.install()
        before_config = self.path.read_text()
        before_loader = (self.prefs / "hyprland.lua").read_text()
        before_hypr = self.hypr.read_text()
        self.native.calls.clear()
        self.desktop_calls.clear()
        self.reload.reset_mock()

        runtime_python.write_text("# updated conflict checker\n")
        result = managed.activate()

        self.assertTrue(result["native_changed"])
        self.assertTrue(result["desktop_changed"])
        self.assertEqual(self.desktop_calls, [("install", True)])
        self.assertTrue(self.desktop.enabled())
        self.assertTrue(self.native.plugins[0]["enabled"])
        self.assertTrue(any(call[0] == "link" for call in self.native.calls))
        self.assertEqual((self.prefs / "hyprland.lua").read_text(), before_loader)
        self.assertEqual(self.hypr.read_text(), before_hypr)
        self.assertEqual(self.path.read_text(), before_config)
        self.reload.assert_called_once()

    def test_runtime_update_refreshes_disabled_desktop_without_enabling_it(self):
        self.install()
        self.set_enabled(False)
        before_config = self.path.read_text()
        before_loader = (self.prefs / "hyprland.lua").read_text()
        self.desktop_calls.clear()
        self.reload.reset_mock()

        (self.root / "bin/herdr-shell").write_text("updated executable")
        result = managed.activate()

        self.assertTrue(result["native_changed"])
        self.assertTrue(result["desktop_changed"])
        self.assertEqual(self.desktop_calls, [("install", False)])
        self.assertFalse(self.desktop.enabled())
        self.assertTrue(self.native.plugins[0]["enabled"])
        self.assertEqual((self.prefs / "hyprland.lua").read_text(), before_loader)
        self.assertEqual(self.path.read_text(), before_config)
        self.reload.assert_called_once()

    def test_refresh_preserves_manually_disabled_plugin_profile_and_custom_fallback(self):
        self.install()
        self.native.plugins[0]["enabled"] = False
        self.set_enabled(False)
        self.path.write_text(self.path.read_text().replace('key = "prefix+space"', 'key = "prefix+m"'))
        (self.root / "bin/herdr-shell").write_text("new build")
        before = self.path.read_text()
        result = managed.activate()
        self.assertTrue(result["native_changed"])
        self.assertFalse(self.native.plugins[0]["enabled"])
        self.assertFalse(self.desktop.enabled())
        self.assertEqual(self.path.read_text(), before)

    def test_disable_resume_restores_original_preferences_and_keeps_user_work(self):
        self.install()
        self.set_enabled(False)
        before = self.path.read_text()
        result = managed.deactivate()
        self.assertTrue(result["changed"])
        self.assertFalse(self.native.plugins[0]["enabled"])
        self.assertTrue(self.helper.is_symlink())
        self.assertEqual(self.path.read_text(), before)
        saved = json.loads((managed._state() / "suspended.json").read_text())
        self.assertTrue(saved["native_enabled"])
        self.assertFalse(saved["desktop_enabled"])
        managed.deactivate()
        self.assertEqual(json.loads((managed._state() / "suspended.json").read_text()), saved)
        self.install()
        self.assertTrue(self.native.plugins[0]["enabled"])
        self.assertFalse(self.desktop.enabled())
        self.assertFalse((managed._state() / "suspended.json").exists())

    def test_disabled_before_suspend_stays_disabled_on_resume(self):
        self.install()
        self.native.plugins[0]["enabled"] = False
        self.set_enabled(False)
        managed.deactivate()
        self.install()
        self.assertFalse(self.native.plugins[0]["enabled"])
        self.assertFalse(self.desktop.enabled())

    def test_disable_before_first_setup_restores_new_install_defaults(self):
        managed.deactivate()
        self.install()
        self.assertTrue(self.native.plugins[0]["enabled"])
        self.assertTrue(self.desktop.enabled())

    def test_remove_keeps_unrelated_commands_and_user_settings(self):
        self.install()
        self.path.write_text(self.path.read_text() + '''\n[[keys.command]]
key="prefix+a"
type="shell"
command="blr.herdr-shell.menu"
[[keys.command]]
key="prefix+b"
type="plugin_action"
command="blr.herdr-shell-extra.menu"
''')
        result = managed.deactivate(remove=True)
        self.assertTrue(result["removed"])
        self.assertFalse(self.helper.exists() or self.helper.is_symlink())
        self.assertEqual(self.native.plugins, [])
        self.assertFalse(self.desktop.installed())
        self.assertTrue((self.root / "bin/herdr-shell").exists())
        commands = tomllib.loads(self.path.read_text())["keys"]["command"]
        self.assertEqual([c["command"] for c in commands], ["other.plugin.menu", "blr.herdr-shell.menu", "blr.herdr-shell-extra.menu"])
        self.assertFalse((self.prefs / "plugin-root").exists())
        self.assertFalse(managed.deactivate(remove=True)["changed"])

    def test_remove_after_original_source_deleted_uses_stable_cache(self):
        source = self.home / "omarchy/plugins/blr.herdr-shell"
        source.mkdir(parents=True)
        self.install()
        source.rmdir()
        managed.deactivate(remove=True)
        self.assertTrue(self.root.exists())
        self.assertFalse(self.helper.is_symlink())

    def test_foreign_native_registration_refused_before_config_mutation(self):
        self.native.plugins = [{"plugin_id": PLUGIN_ID, "plugin_root": "/other/plugin", "enabled": True, "source": {"kind": "local"}}]
        with self.assertRaisesRegex(ShellError, "another installation"):
            self.install()
        self.assertEqual(self.path.read_text(), BASE)
        self.assertFalse(self.helper.is_symlink())
        self.assertEqual(self.desktop_calls, [])

    def test_missing_or_unknown_source_kind_is_never_uninstalled(self):
        for source in ({}, {"kind": "unknown"}, {"kind": "github"}):
            with self.subTest(source=source):
                self.native.calls.clear()
                self.native.plugins = [{"plugin_id": PLUGIN_ID, "plugin_root": str(self.root), "enabled": True, "source": source}]
                with self.assertRaisesRegex(ShellError, "another installation"):
                    managed.deactivate(remove=True)
                self.assertTrue(all(c[0] == "list" for c in self.native.calls))
                self.assertEqual(self.path.read_text(), BASE)

    def test_foreign_helper_and_desktop_owner_refused(self):
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text("personal helper")
        with self.assertRaisesRegex(ShellError, "CLI belongs"):
            managed.activate()
        self.helper.unlink()
        self.prefs.mkdir()
        (self.prefs / "plugin-root").write_text("/other/root")
        self.hypr.write_text(self.hypr.read_text() + "managed bridge\n")
        with self.assertRaisesRegex(ShellError, "desktop bridge belongs"):
            managed.deactivate(remove=True)
        self.assertEqual(self.path.read_text(), BASE)

    def test_legacy_removal_stale_desktop_metadata_does_not_block_migration(self):
        self.prefs.mkdir()
        (self.prefs / "plugin-root").write_text("/old/developer-checkout")
        (self.prefs / "desktop-enabled").write_text("0\n")
        self.install()
        self.assertEqual((self.prefs / "plugin-root").read_text().strip(), str(self.root))
        self.assertTrue(self.desktop.enabled())

    def test_config_validation_fails_before_native_or_desktop_changes(self):
        self.store.validator.side_effect = ShellError("invalid config")
        with self.assertRaisesRegex(ShellError, "invalid config"):
            managed.activate()
        self.assertTrue(all(c[0] == "list" for c in self.native.calls))
        self.assertEqual(self.desktop_calls, [])
        self.assertFalse(self.helper.is_symlink())

    def test_native_link_response_lost_after_commit_rolls_back(self):
        def lose(args):
            self.native.after_failure = None
            raise ShellError("link response lost")
        self.native.after_failure = lose
        with self.assertRaisesRegex(ShellError, "link response lost"):
            managed.activate()
        self.assertEqual(self.native.plugins, [])
        self.assertFalse(self.helper.is_symlink())
        self.assertEqual(self.path.read_text(), BASE)

    def test_config_reload_error_rolls_back_all_first_install_resources(self):
        self.reload.side_effect = [ShellError("server rejected reload"), {}, {}]
        with self.assertRaisesRegex(ShellError, "server rejected reload"):
            managed.activate()
        self.assertEqual(self.path.read_text(), BASE)
        self.assertEqual(self.native.plugins, [])
        self.assertFalse(self.helper.is_symlink())
        self.assertFalse(self.desktop.installed())
        self.assertFalse((managed._state() / "setup.json").exists())

    def test_removal_rollback_reloads_after_native_registration_restored(self):
        self.install()
        states = []
        def fail_first_reload():
            states.append(deepcopy(self.native.plugins))
            if len(states) == 1:
                raise ShellError("reload failed")
            return {}
        self.reload.side_effect = fail_first_reload
        with self.assertRaisesRegex(ShellError, "reload failed"):
            managed.deactivate(remove=True)
        self.assertEqual(states[:2], [[], []])
        self.assertTrue(states[2][0]["enabled"])
        self.assertTrue(self.native.plugins[0]["enabled"])

    def test_rollback_preserves_later_personal_profile_edit(self):
        self.install()
        # Force bridge refresh so the transaction writes its on preference.
        (self.prefs / "hyprland.lua").write_text("old bridge code")
        (self.root / "bin/herdr-shell").write_text("updated build")
        def fail_reload():
            self.reload.side_effect = None
            self.set_enabled(False)
            raise ShellError("reload failed")
        self.reload.side_effect = fail_reload
        with self.assertRaisesRegex(ShellError, "later edits were preserved: desktop-enabled"):
            managed.activate()
        self.assertFalse(self.desktop.enabled())
        self.assertEqual((self.prefs / "hyprland.lua").read_text(), "old bridge code")

    def test_desktop_owner_changed_after_preflight_is_preserved(self):
        def edit_after_link(args):
            if args[0] == "link":
                self.prefs.mkdir()
                (self.prefs / "plugin-root").write_text("/new/installation")
                self.hypr.write_text(self.hypr.read_text() + "managed bridge\n")
        self.native.after_failure = edit_after_link
        with self.assertRaisesRegex(ShellError, "Desktop setup changed"):
            managed.activate()
        self.assertEqual((self.prefs / "plugin-root").read_text(), "/new/installation")
        self.assertEqual(self.native.plugins, [])
        self.assertEqual(self.desktop_calls, [])

    def test_uninstall_response_lost_rolls_back_removal_resources(self):
        self.install()
        before, native, hypr = self.path.read_text(), deepcopy(self.native.plugins), self.hypr.read_text()
        def lose(args):
            self.native.after_failure = None
            if args[0] == "uninstall":
                raise ShellError("uninstall response lost")
        self.native.after_failure = lose
        with self.assertRaisesRegex(ShellError, "uninstall response lost"):
            managed.deactivate(remove=True)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.native.plugins, native)
        self.assertEqual(self.hypr.read_text(), hypr)
        self.assertTrue(self.helper.is_symlink())
        self.assertTrue(self.desktop.enabled())

    def test_disable_failure_does_not_leave_suspended_state(self):
        self.install()
        def fail(args):
            self.native.failure = None
            raise ShellError("registry write failed")
        self.native.failure = fail
        with self.assertRaisesRegex(ShellError, "registry write failed"):
            managed.deactivate()
        self.assertTrue(self.native.plugins[0]["enabled"])
        self.assertTrue(self.desktop.enabled())
        self.assertFalse((managed._state() / "suspended.json").exists())

    def test_invalid_owned_suspension_is_refused(self):
        managed._state().mkdir(parents=True)
        (managed._state() / "suspended.json").write_text(json.dumps({"plugin_root": str(self.root), "native_enabled": "false"}))
        with self.assertRaisesRegex(ShellError, "managed setup state"):
            managed.activate()
        self.assertEqual(self.path.read_text(), BASE)


class OfflineCliTests(unittest.TestCase):
    def test_native_cli_uses_json_envelope_and_unselected_missing_socket(self):
        payload = {"id": "cli:plugin", "result": {"type": "plugin_list", "plugins": []}}
        with tempfile.TemporaryDirectory() as folder, patch.object(managed, "_state", return_value=Path(folder)), \
             patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/real/session.sock", "HERDR_SESSION": "other", "HERDR_PLUGIN_ID": "other"}), \
             patch.object(managed, "run_herdr", return_value=json.dumps(payload)) as run:
            self.assertEqual(managed._native("list", "--json")["plugins"], [])
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["plugin", "list", "--json"])
            offline = kwargs["env"]["HERDR_SOCKET_PATH"]
            self.assertTrue(offline.startswith("/tmp/herdr-shell-registry-"))
            self.assertLess(len(offline.encode()), 100)
            self.assertFalse(Path(offline).parent.exists())
            self.assertNotIn("HERDR_ENV", kwargs["env"])
            self.assertNotIn("HERDR_SESSION", kwargs["env"])
            self.assertNotIn("HERDR_PLUGIN_ID", kwargs["env"])

    def test_cli_errors_and_invalid_json_are_not_empty_registry(self):
        for output in ('{"error":{"message":"broken"}}', '{"result":[]}', 'not json'):
            with self.subTest(output=output), patch.object(managed, "run_herdr", return_value=output):
                with self.assertRaises(ShellError):
                    managed._native("list", "--json")


class Connection:
    def __init__(self, uid=None, response=None, failure=None):
        self.uid = os.getuid() if uid is None else uid
        self.response = response
        self.failure = failure
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def settimeout(self, timeout):
        assert timeout <= 2

    def connect(self, path):
        if self.failure:
            raise self.failure

    def getsockopt(self, *unused):
        return struct.pack("3i", os.getpid(), self.uid, os.getgid())

    def sendall(self, raw):
        self.sent.append(json.loads(raw))

    def makefile(self, unused):
        reply = self.response
        if reply is None:
            reply = {"id": self.sent[-1]["id"], "result": {"type": "config_reloaded"}}
        elif isinstance(reply, dict):
            reply = {"id": self.sent[-1]["id"], **reply}
        return io.BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode() + b"\n")


class ReloadTests(unittest.TestCase):
    def test_matching_default_and_named_sessions_reload_foreign_config_skipped(self):
        a, b, foreign = Connection(), Connection(), Connection()
        paths = [Path("/config/herdr/herdr.sock"), Path("/config/herdr/sessions/dev/herdr.sock"), Path("/config/herdr/sessions/custom/herdr.sock")]
        wanted = Path("/config/herdr/config.toml")
        with patch.object(managed, "config_path", return_value=wanted), \
             patch.object(managed, "_socket_paths", return_value=paths), \
             patch.object(managed, "_peer_config", side_effect=[wanted, wanted, Path("/custom/config.toml")]), \
             patch.object(managed.socket, "socket", side_effect=[a, b, foreign]):
            self.assertEqual(managed._reload_servers(), {"reloaded": list(map(str, paths[:2])), "skipped": [str(paths[2])]})
        self.assertEqual(a.sent[0]["method"], "server.reload_config")
        self.assertEqual(b.sent[0]["method"], "server.reload_config")
        self.assertEqual(foreign.sent, [])

    def test_wrong_peer_uid_never_receives_api_mutation(self):
        peer = Connection(uid=os.getuid() + 1)
        with patch.object(managed, "_socket_paths", return_value=[Path("/config/herdr/herdr.sock")]), \
             patch.object(managed, "_peer_config") as config_peer, patch.object(managed.socket, "socket", return_value=peer):
            self.assertEqual(managed._reload_servers()["reloaded"], [])
        self.assertEqual(peer.sent, [])
        config_peer.assert_not_called()

    def test_stopped_or_handoff_sessions_are_transient_but_api_error_is_fatal(self):
        wanted = Path("/config/herdr/config.toml")
        for failure in (OSError(errno.ENOENT, "gone"), OSError(errno.ECONNREFUSED, "stopped"), TimeoutError("handoff")):
            with self.subTest(failure=failure), \
                 patch.object(managed, "_socket_paths", return_value=[Path("/config/herdr/herdr.sock")]), \
                 patch.object(managed.socket, "socket", return_value=Connection(failure=failure)):
                self.assertEqual(managed._reload_servers()["reloaded"], [])
        for response in ({"error": {"message": "bad config"}}, b"invalid\n", []):
            with self.subTest(response=response), \
                 patch.object(managed, "config_path", return_value=wanted), \
                 patch.object(managed, "_socket_paths", return_value=[Path("/config/herdr/herdr.sock")]), \
                 patch.object(managed, "_peer_config", return_value=wanted), \
                 patch.object(managed.socket, "socket", return_value=Connection(response=response)):
                with self.assertRaises(ShellError):
                    managed._reload_servers()

    def test_peer_environment_selects_effective_config(self):
        raw = b"HOME=/fixture\0XDG_CONFIG_HOME=/fixture/config\0"
        with patch.object(managed.Path, "read_bytes", return_value=raw):
            self.assertEqual(managed._peer_config(10), Path("/fixture/config/herdr/config.toml"))
        with patch.object(managed.Path, "read_bytes", return_value=raw + b"HERDR_CONFIG_PATH=/fixture/custom.toml\0"):
            self.assertEqual(managed._peer_config(10), Path("/fixture/custom.toml"))

    def test_session_discovery_is_bounded_and_ignores_symlink_directories(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"XDG_CONFIG_HOME": folder}):
            root = Path(folder) / "herdr"
            root.mkdir()
            (root / "herdr.sock").touch()
            for number in range(25):
                session = root / "sessions" / ("n%02d" % number)
                session.mkdir(parents=True)
                (session / "herdr.sock").touch()
            (root / "sessions/foreign").symlink_to(root / "sessions/n00", target_is_directory=True)
            real_stat = Path.stat
            def fixture_stat(path, **kwargs):
                result = real_stat(path, **kwargs)
                if path.name == "herdr.sock":
                    result = SimpleNamespace(st_mode=stat.S_IFSOCK)
                return result
            with patch.object(managed.Path, "stat", fixture_stat):
                paths = managed._socket_paths()
            self.assertEqual(len(paths), managed._MAX_SERVERS)
            self.assertEqual(paths[0], root / "herdr.sock")
            self.assertNotIn(root / "sessions/foreign/herdr.sock", paths)


if __name__ == "__main__":
    unittest.main()
