import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
import subprocess
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from herdr_shell import desktop as d
from herdr_shell.runtime import ShellError


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root / "config"),
                              XDG_STATE_HOME=str(self.root / "state"))
        self.env.start()
        self.addCleanup(self.env.stop)
        d.hypr_path().parent.mkdir(parents=True)
        d.hypr_path().write_text('-- personal header\nrequire("default.hypr.omarchy")\nrequire("hypr.bindings")\n')
        self.proc = self.root / "proc"
        d._profile_cache.clear()

    def registration(self):
        return patch.object(d, "reconcile", return_value={"generation": "testgen", "registered": ["pane-zoom"], "conflicts": {}})

    def active_bindings(self):
        return patch.object(d, "effective_bindings", return_value={"pane-zoom": {"active": True}})

    def process(self, pid, argv, children=(), foreground=True, state="S", env=b""):
        base = self.proc / str(pid)
        (base / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
        fields = ["0"] * 50
        fields[0], fields[1], fields[2], fields[4], fields[5], fields[19] = state, "1", str(pid), "9", str(pid if foreground else 1), "999"
        (base / "stat").write_text(f"{pid} (name with ) parens) " + " ".join(fields))
        (base / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (base / "environ").write_bytes(env)
        (base / "task" / str(pid) / "children").write_text(" ".join(map(str, children)))

    def test_foreground_client_and_named_session(self):
        self.process(10, ["foot"], [20], False)
        self.process(20, ["bash"], [30], False)
        self.process(30, ["/usr/bin/herdr", "--session", "work"], [40], env=b"XDG_CONFIG_HOME=/tmp/other\0")
        self.process(40, ["herdr"], [])
        clients = d.find_clients(10, self.proc)
        self.assertEqual(len(clients), 1)  # stop at client; do not use a nested command
        process, options = clients[0]
        self.assertEqual(process.pid, 30)
        self.assertEqual(d.socket_for(process, options, self.proc), "/tmp/other/herdr/sessions/work/herdr.sock")

    def test_plain_terminal_and_suspended_client(self):
        self.process(10, ["foot"], [20], False)
        self.process(20, ["herdr"], state="T")
        self.assertEqual(d.find_clients(10, self.proc), [])
        self.process(20, ["bash"])
        self.assertEqual(d.find_clients(10, self.proc), [])

    def test_shared_terminal_process_is_ambiguous(self):
        self.process(10, ["ghostty"], [20, 30], False)
        self.process(20, ["herdr"])
        self.process(30, ["herdr"])
        self.assertEqual(len(d.find_clients(10, self.proc)), 2)

    def test_remote_and_cli_commands_are_not_local_targets(self):
        self.process(10, ["herdr", "--remote", "host"])
        process, options = d.find_clients(10, self.proc)[0]
        with self.assertRaisesRegex(ShellError, "remote"):
            d.socket_for(process, options, self.proc)
        for argv in (["herdr", "api", "snapshot"], ["herdr", "--version"], ["herdr", "server"]):
            self.assertIsNone(d.client_options(argv))

    def test_focus_and_client_identity_races_skip_actions(self):
        window = {"pid": 10, "address": "0x123"}
        process = d.Process(20, 10, "S", True, "999", ("herdr",))
        snapshot = {"focused_pane_id": "p", "panes": [{"pane_id": "p", "workspace_id": "w", "tab_id": "t", "terminal_id": "term"}]}
        with patch.object(d, "enabled", return_value=True), patch.object(d, "find_clients", return_value=[(process, {})]), \
             patch.object(d, "socket_for", return_value="socket"), patch.object(d, "Client") as client, \
             patch.object(d, "execute") as execute, patch.object(d, "hypr", return_value=json.dumps(window)) as hypr:
            self.assertIn("skipped", d.route("pane-zoom", 11, 20, "999", "0x123"))
            self.assertIn("skipped", d.route("pane-zoom", 10, 20, "old", "0x123"))
            client.return_value.snapshot.return_value = snapshot
            hypr.side_effect = [json.dumps(window), json.dumps({"pid": 11})]
            self.assertIn("skipped", d.route("pane-zoom", 10, 20, "999", "0x123"))
            execute.assert_not_called()

    def test_install_remove_preserve_user_config(self):
        before = d.hypr_path().read_text()
        with patch.object(d, "hypr", return_value=""), self.registration(), self.active_bindings():
            d.install_desktop(d.install_proposal())
            self.assertTrue(d.enabled())
            self.assertEqual(d.hypr_path().read_text().count(d.BEGIN), 1)
            d.install_desktop(d.install_proposal())
            self.assertEqual(d.hypr_path().read_text().count(d.BEGIN), 1)
            d.install_desktop(d.install_proposal(remove=True), remove=True)
        self.assertFalse(d.enabled())
        self.assertEqual(d.hypr_path().read_text(), before)

    def test_refresh_disabled_profile_never_enables_it_and_checks_registration(self):
        mask, _ = d.chord("SUPER + ALT + Z")
        binds = [{"description": "Herdr Shell: pane-zoom", "modmask": mask, "key": "Z", "submap": ""}]
        with patch.object(d, "hypr", return_value=""), self.registration(), \
             patch.object(d, "effective_bindings", return_value={"pane-zoom": {"active": False, "status": "disabled"}}), \
             patch.object(d, "binding_snapshot", return_value=(binds, {})), \
             patch.object(d, "atomic_write", wraps=d.atomic_write) as write:
            result = d.install_desktop(d.install_proposal(), activate=False)
        self.assertTrue(result["installed"])
        self.assertFalse(result["enabled"])
        flags = [call.args[1] for call in write.call_args_list if Path(call.args[0]).name == "desktop-enabled"]
        self.assertTrue(flags)
        self.assertTrue(all(flag == "0\n" for flag in flags))

    def test_disabled_status_does_not_hide_missing_registration_during_refresh(self):
        before = d.hypr_path().read_text()
        with patch.object(d, "hypr", return_value=""), self.registration(), \
             patch.object(d, "effective_bindings", return_value={"pane-zoom": {"active": False, "status": "disabled"}}), \
             patch.object(d, "binding_snapshot", return_value=([], {})):
            with self.assertRaisesRegex(ShellError, "verify.*pane-zoom"):
                d.install_desktop(d.install_proposal(), activate=False)
        self.assertEqual(d.hypr_path().read_text(), before)
        self.assertFalse(d.enabled())

    def test_install_remove_preserve_config_without_final_newline(self):
        before = d.hypr_path().read_text().removesuffix("\n")
        d.hypr_path().write_text(before)
        with patch.object(d, "hypr", return_value=""), self.registration(), self.active_bindings():
            d.install_desktop(d.install_proposal())
            d.install_desktop(d.install_proposal())
            self.assertEqual(d.hypr_path().read_text().count(d.BEGIN), 1)
            d.install_desktop(d.install_proposal(remove=True), remove=True)
        self.assertEqual(d.hypr_path().read_text(), before)

    def test_reload_failure_restores_all_integration_files(self):
        with patch.object(d, "hypr", return_value=""), self.registration(), self.active_bindings():
            d.install_desktop(d.install_proposal())
        old = {p.name: p.read_text() for p in d.preferences_dir().iterdir()}
        before = d.hypr_path().read_text()
        with patch.object(d, "hypr", side_effect=["", "ok", "new error", "ok"]), \
             patch.object(d, "integration_text", return_value="-- changed integration\n"):
            with self.assertRaisesRegex(ShellError, "new error"):
                d.install_desktop(d.install_proposal())
        self.assertEqual(d.hypr_path().read_text(), before)
        self.assertEqual({p.name: p.read_text() for p in d.preferences_dir().iterdir()}, old)

    def test_stale_preview_and_unknown_config_refused(self):
        proposal = d.install_proposal()
        d.hypr_path().write_text("-- edited later\n")
        with self.assertRaisesRegex(ShellError, "changed"):
            d.install_desktop(proposal)
        with self.assertRaisesRegex(ShellError, "loader"):
            d.install_proposal()

    def test_readable_keys(self):
        self.assertEqual(d.pretty_key("prefix+f"), "Ctrl+Space → F")
        self.assertEqual(d.pretty_key("SUPER + SHIFT + code:19"), "Super+Shift+0")
        self.assertEqual(d.pretty_key("SUPER + ALT + Page_Up"), "Super+Alt+PageUp")

    def test_popup_desktop_scope_uses_compositor_root_without_changing_native_config(self):
        from herdr_shell.config import ConfigStore
        compositor = self.root / "compositor-config"
        target = self.root / "target-config"
        with patch.object(d, "hypr", return_value=str(compositor) + "\n"):
            environment = d.popup_environment()
        self.assertEqual(environment, {"HERDR_SHELL_DESKTOP_CONFIG_HOME": str(compositor)})
        with patch.dict(os.environ, {**environment, "XDG_CONFIG_HOME": str(target), "HERDR_CONFIG_PATH": ""}):
            self.assertEqual(d.preferences_dir(), compositor / "herdr-shell")
            self.assertEqual(d.hypr_path(), compositor / "hypr/hyprland.lua")
            self.assertEqual(ConfigStore().path, target / "herdr/config.toml")
        for value in ("nil", "", "relative/path", "/tmp/path\nwith-control"):
            with self.subTest(value=value), patch.object(d, "hypr", return_value=value):
                self.assertEqual(d.popup_environment(), {})
        with patch.object(d, "hypr", side_effect=ShellError("no compositor")):
            self.assertEqual(d.popup_environment(), {})

    def test_loader_is_last_after_personal_bindings(self):
        proposal = d.install_proposal()
        self.assertLess(proposal["after"].index('require("hypr.bindings")'), proposal["after"].index(d.BEGIN))
        self.assertTrue(proposal["after"].endswith(d.END + "\n"))

    def test_failed_registration_restores_config_and_managed_files(self):
        with patch.object(d, "hypr", return_value=""), self.registration(), self.active_bindings():
            d.install_desktop(d.install_proposal())
        d.atomic_write(d.preferences_dir() / "binding-status.json", '{"previous": true}')
        before = d.hypr_path().read_text()
        managed = ("hyprland.lua", "plugin-root", "desktop-enabled", "binding-status.json")
        saved = {name: (d.preferences_dir() / name).read_text() for name in managed}

        def registration_failure():
            d.atomic_write(d.preferences_dir() / "binding-status.json", '{"partial": true}')
            raise ShellError("registration failed")

        with patch.object(d, "hypr", return_value="") as hypr, \
             patch.object(d, "reconcile", side_effect=registration_failure), \
             patch.object(d, "integration_text", return_value="-- changed bridge\n"):
            with self.assertRaisesRegex(ShellError, "registration failed"):
                d.install_desktop(d.install_proposal())
        self.assertEqual(d.hypr_path().read_text(), before)
        self.assertEqual({name: (d.preferences_dir() / name).read_text() for name in managed}, saved)
        self.assertEqual(hypr.call_args_list.count(unittest.mock.call("reload")), 2)

    def test_skipped_registration_rolls_back_instead_of_enabling(self):
        before = d.hypr_path().read_text()
        with patch.object(d, "hypr", return_value=""), \
             patch.object(d, "reconcile", return_value={"skipped": "configuration reloaded"}):
            with self.assertRaisesRegex(ShellError, "not registered"):
                d.install_desktop(d.install_proposal())
        self.assertEqual(d.hypr_path().read_text(), before)
        self.assertFalse(d.enabled())
        self.assertFalse((d.preferences_dir() / "hyprland.lua").exists())

    def test_registration_missing_from_live_bindings_rolls_back(self):
        before = d.hypr_path().read_text()
        with patch.object(d, "hypr", return_value=""), self.registration(), \
             patch.object(d, "effective_bindings", return_value={"pane-zoom": {"active": False}}):
            with self.assertRaisesRegex(ShellError, "verify.*pane-zoom"):
                d.install_desktop(d.install_proposal())
        self.assertEqual(d.hypr_path().read_text(), before)
        self.assertFalse(d.enabled())

    def test_fully_conflicted_profile_installs_disabled(self):
        conflicts = {action: "Personal shortcut" for _, action, _ in d.MAPPINGS}
        with patch.object(d, "hypr", return_value=""), \
             patch.object(d, "reconcile", return_value={"generation": "testgen", "registered": [], "conflicts": conflicts}), \
             patch.object(d, "effective_bindings", return_value={}):
            result = d.install_desktop(d.install_proposal())
        self.assertTrue(result["installed"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["conflicts"], conflicts)

    def test_reconcile_passes_only_unoccupied_actions_with_generation_guard(self):
        occupied = {"tab-new": "Personal tab shortcut", "pane-zoom": "Personal zoom shortcut"}
        with patch.object(d, "generation", return_value="testgen"), \
             patch.object(d, "binding_snapshot", return_value=([], occupied)), patch.object(d, "hypr") as hypr:
            result = d.reconcile("testgen")
        self.assertNotIn("tab-new", result["registered"])
        self.assertNotIn("pane-zoom", result["registered"])
        self.assertIn("menu", result["registered"])
        command = hypr.call_args.args
        self.assertEqual(command[0], "eval")
        self.assertIn('generation == "testgen"', command[1])
        self.assertNotIn('["tab-new"]=true', command[1])
        self.assertNotIn('["pane-zoom"]=true', command[1])
        self.assertEqual(json.loads((d.preferences_dir() / "binding-status.json").read_text())["conflicts"], occupied)

    def test_stale_reconcile_does_not_inspect_or_register(self):
        with patch.object(d, "generation", return_value="current"), \
             patch.object(d, "binding_snapshot") as inspect, patch.object(d, "hypr") as hypr:
            self.assertIn("skipped", d.reconcile("old"))
        inspect.assert_not_called()
        hypr.assert_not_called()

    def test_enable_reconciles_before_activation_and_disable_stays_direct(self):
        d.atomic_write(d.preferences_dir() / "desktop-enabled", "0\n")

        def reconcile():
            self.assertFalse(d.enabled(), "late conflicts must be reconciled before activating")
            return {"generation": "testgen", "registered": ["pane-zoom"], "conflicts": {}}

        with patch.object(d, "installed", return_value=True), patch.object(d, "reconcile", side_effect=reconcile) as inspect:
            d.set_enabled(True)
        inspect.assert_called_once()
        self.assertTrue(d.enabled())
        with patch.object(d, "reconcile") as inspect, patch.object(d, "hypr") as hypr:
            d.set_enabled(False)
        self.assertFalse(d.enabled())
        inspect.assert_not_called()
        hypr.assert_not_called()

    def test_refused_or_skipped_enable_leaves_profile_disabled(self):
        d.atomic_write(d.preferences_dir() / "desktop-enabled", "0\n")
        for result in ({"skipped": "configuration reloaded"},
                       {"generation": "testgen", "registered": [], "conflicts": {"menu": "Personal chord"}}):
            with self.subTest(result=result), patch.object(d, "installed", return_value=True), \
                 patch.object(d, "reconcile", return_value=result):
                with self.assertRaises(ShellError):
                    d.set_enabled(True)
                self.assertFalse(d.enabled())

    def test_live_profile_status_requires_exact_registered_owner(self):
        binds = [{"modmask": 72, "key": "Z", "description": "Herdr Shell: pane-zoom", "dispatcher": "__lua"},
                 {"modmask": 72, "key": "Prior", "description": "Herdr Shell: tab-previous", "dispatcher": "__lua"}]
        with patch.object(d, "installed", return_value=True), patch.object(d, "enabled", return_value=True), \
             patch.object(d, "binding_snapshot", return_value=(binds, {"tab-close": "Personal close shortcut"})):
            status = d.effective_bindings(refresh=True)
        self.assertTrue(status["pane-zoom"]["active"])
        self.assertTrue(status["tab-previous"]["active"])
        self.assertEqual(status["tab-close"]["status"], "conflict")
        self.assertFalse(status["tab-close"]["active"])
        self.assertIn("Personal close shortcut", status["tab-close"]["reason"])
        self.assertEqual(status["tab-new"]["status"], "unavailable")

    def test_live_profile_is_not_ready_when_disabled_or_inspection_fails(self):
        own = [{"modmask": 72, "key": "Z", "description": "Herdr Shell: pane-zoom"}]
        with patch.object(d, "installed", return_value=True), patch.object(d, "enabled", return_value=False), \
             patch.object(d, "binding_snapshot", return_value=(own, {})):
            self.assertEqual(d.effective_bindings(refresh=True)["pane-zoom"]["status"], "disabled")
        with patch.object(d, "installed", return_value=True), patch.object(d, "enabled", return_value=True), \
             patch.object(d, "binding_snapshot", side_effect=ShellError("unreachable")):
            result = d.effective_bindings(refresh=True)
        self.assertTrue(all(not row["active"] for row in result.values()))
        self.assertIn("unreachable", result["menu"]["reason"])

    def test_page_key_alias_collisions_use_actual_registry(self):
        bind = {"modmask": 72, "key": "Prior", "description": "Personal previous tab"}

        def hypr(*args):
            if args == ("-j", "binds"):
                return json.dumps([bind])
            return json.dumps({"str": "us" if args[-1] == "input:kb_layout" else ""})

        with patch.object(d, "hypr", side_effect=hypr), patch.object(d, "source_positions", return_value={}):
            registered, occupied = d.binding_snapshot()
        self.assertEqual(registered, [bind])
        self.assertIn("Personal previous tab", occupied["tab-previous"])

    def queue(self, generation="testgen", contents=""):
        d.preferences_dir().mkdir(parents=True, exist_ok=True)
        queue = d.preferences_dir() / ("events-" + generation + ".queue")
        queue.write_text(contents)
        return queue

    def test_queue_preserves_order_and_consumes_before_dispatch(self):
        first = "1\tpane-zoom\t10\t20\t999\t0x123\n"
        second = "2\ttab-new\t10\t20\t999\t0x123\n"
        last = "3\tagent-new\t10\t20\t999\t0x123"
        queue = self.queue(contents=first + second + last)
        offset = queue.with_suffix(".offset")
        received = []

        def dispatch(action, *args):
            received.append(action)
            self.assertEqual(int(offset.read_text()), len(first) if len(received) == 1 else len(first + second))
            if action == "pane-zoom":
                raise ShellError("interrupted action")

        with patch.object(d, "generation", return_value="testgen"), patch.object(d, "route", side_effect=dispatch), \
             patch.object(d, "record_error") as error:
            self.assertEqual(d.drain("testgen"), {"processed": 1})
        self.assertEqual(received, ["pane-zoom", "tab-new"])
        error.assert_called_once()
        with patch.object(d, "generation", return_value="testgen"), patch.object(d, "route") as route:
            self.assertEqual(d.drain("testgen"), {"processed": 0})
            route.assert_not_called()
            with queue.open("a") as events:
                events.write("\n")
            self.assertEqual(d.drain("testgen"), {"processed": 1})
            self.assertEqual(route.call_args.args[0], "agent-new")

    def test_reloaded_generation_does_not_consume_old_queue(self):
        queue = self.queue(contents="1\tpane-zoom\t10\t20\t999\t0x123\n")
        with patch.object(d, "generation", return_value="newgen"), patch.object(d, "route") as route:
            self.assertIn("skipped", d.drain("testgen"))
        route.assert_not_called()
        self.assertFalse(queue.with_suffix(".offset").exists())

    def test_reload_during_dispatch_leaves_remaining_old_events_unconsumed(self):
        first = "1\tpane-zoom\t10\t20\t999\t0x123\n"
        queue = self.queue(contents=first + "2\ttab-new\t10\t20\t999\t0x123\n")
        current = ["testgen"]
        received = []

        def dispatch(action, *args):
            received.append(action)
            current[0] = "reloaded"

        with patch.object(d, "generation", side_effect=lambda: current[0]), patch.object(d, "route", side_effect=dispatch):
            d.drain("testgen")
        self.assertEqual(received, ["pane-zoom"])
        self.assertEqual(int(queue.with_suffix(".offset").read_text()), len(first))

    def test_concurrent_drainers_serialize_action_execution(self):
        self.queue(contents="1\tpane-zoom\t10\t20\t999\t0x123\n")
        entered, release = Event(), Event()

        def dispatch(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release action")

        with patch.object(d, "generation", return_value="testgen"), patch.object(d, "route", side_effect=dispatch) as route, \
             ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(d.drain, "testgen")
            self.assertTrue(entered.wait(2))
            second = workers.submit(d.drain, "testgen")
            try:
                with self.assertRaises(TimeoutError):
                    second.result(timeout=.05)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2), {"processed": 1})
            self.assertEqual(second.result(timeout=2), {"processed": 0})
            route.assert_called_once()

    def test_menu_marker_rejects_reused_or_wrong_process(self):
        socket = str(self.root / "herdr.sock")
        path = d._menu_path(socket)
        d.atomic_write(path, json.dumps({"pid": 20, "start": "999"}))
        for process in (d.Process(20, 10, "S", True, "new", ("python3", "_menu")),
                        d.Process(20, 10, "S", True, "999", ("bash",)),
                        d.Process(20, 10, "T", True, "999", ("python3", "_menu"))):
            with self.subTest(process=process), patch.object(d, "read_process", return_value=process):
                self.assertIsNone(d.menu_running(socket))

    def test_menu_owner_cleanup_does_not_delete_replacement_marker(self):
        context = Mock(socket=str(self.root / "herdr.sock"), pane="p", terminal="term")
        process = d.Process(20, 10, "S", True, "999", ("python3", "_menu"))
        with patch.object(d, "read_process", return_value=process):
            with d.menu_instance(context):
                marker = d.menu_running(context.socket)
                self.assertEqual(marker["pane"], "p")
                replacement = {**marker, "pid": 30}
                d.atomic_write(d._menu_path(context.socket), json.dumps(replacement))
            self.assertEqual(json.loads(d._menu_path(context.socket).read_text()), replacement)

    def test_toggle_closes_owned_menu_or_waits_for_new_owner(self):
        context = Mock(socket=str(self.root / "herdr.sock"))
        with patch.object(d, "menu_running", return_value={"pid": 20}), patch.object(d, "open_ui") as open_ui:
            d.toggle_menu(context)
        context.client.call.assert_called_once_with("popup.close")
        open_ui.assert_not_called()
        with patch.object(d, "menu_running", side_effect=[None, None, {"pid": 20}]), \
             patch.object(d, "open_ui", return_value="popup") as open_ui, patch.object(d.time, "sleep"):
            self.assertEqual(d.toggle_menu(context), "popup")
        open_ui.assert_called_once_with(context, "menu")

    def test_menu_owner_location_is_shared_across_state_directories(self):
        socket = self.root / "server/herdr.sock"
        first = d._menu_path(str(socket))
        with patch.dict(os.environ, XDG_STATE_HOME=str(self.root / "other-state")):
            second = d._menu_path(str(socket))
        self.assertEqual(first, second)
        self.assertEqual(first.parent.parent, socket.parent)

    def test_route_does_not_run_actions_inside_owned_menu(self):
        window = {"pid": 10, "address": "0x123"}
        process = d.Process(20, 10, "S", True, "999", ("herdr",))
        pane = {"pane_id": "p", "workspace_id": "w", "tab_id": "t", "terminal_id": "term"}
        with patch.object(d, "enabled", return_value=True), patch.object(d, "find_clients", return_value=[(process, {})]), \
             patch.object(d, "socket_for", return_value="socket"), patch.object(d, "Client") as client, \
             patch.object(d, "execute") as execute, patch.object(d, "hypr", return_value=json.dumps(window)), \
             patch.object(d, "menu_running", return_value={"pid": 30}):
            client.return_value.snapshot.return_value = {"focused_pane_id": "p", "panes": [pane]}
            self.assertIn("skipped", d.route("pane-zoom", 10, 20, "999", "0x123"))
        execute.assert_not_called()

    def test_live_game_consumes_chord_before_normal_action_or_menu_dispatch(self):
        window = {"pid": 10, "address": "0x123"}
        process = d.Process(20, 10, "S", True, "999", ("herdr",))
        pane = {"pane_id": "p", "workspace_id": "w", "tab_id": "t", "terminal_id": "term"}
        with patch.object(d, "enabled", return_value=True), patch.object(d, "find_clients", return_value=[(process, {})]), \
             patch.object(d, "socket_for", return_value="socket"), patch.object(d, "Client") as client, \
             patch.object(d, "execute") as execute, patch.object(d, "hypr", return_value=json.dumps(window)), \
             patch.object(d, "toggle_menu") as menu, patch.object(d, "menu_running") as running, \
             patch("herdr_shell.game_input.route", return_value={"game": "queued"}) as game:
            client.return_value.snapshot.return_value = {"focused_pane_id": "p", "panes": [pane]}
            for action in ('pane-close', 'agent-new', 'menu'):
                self.assertEqual(d.route(action, 10, 20, '999', '0x123'), {"game": "queued"})
                self.assertEqual(game.call_args.kwargs['desktop_identity'], {
                    "window_pid": 10, "client_pid": 20, "start": '999', "address": '0x123'})
        execute.assert_not_called()
        menu.assert_not_called()
        running.assert_not_called()

    def test_lua_registers_only_free_chords_and_routes_only_local_herdr(self):
        generated = self.root / "bridge.lua"
        generated.write_text(d.integration_text())
        vectors = [
            ["herdr"], ["/usr/bin/herdr", "--session", "work"], ["herdr", "--session=work"],
            ["herdr", "--handoff"], ["herdr", "session", "attach", "work"],
            ["herdr", "--remote", "host"], ["herdr", "--remote=host"],
            ["herdr", "--remote-keybindings", "local"], ["herdr", "--remote-keybindings=local"],
            ["herdr", "--session", ""], ["herdr", "--session="], ["herdr", "--session", "../bad"],
            ["herdr", "--session", "."], ["herdr", "--session", "a\\b"],
            ["herdr", "--remote", ""], ["herdr", "--remote="], ["herdr", "--remote-keybindings="],
            ["herdr", "--session"], ["herdr", "--remote"], ["herdr", "--remote-keybindings"],
            ["herdr", "session", "attach"], ["herdr", "session", "attach", "work", "extra"],
            ["herdr", "api", "snapshot"], ["herdr", "--version"], ["herdr", "server"], ["bash"],
        ]
        fixtures = self.root / "cli-vectors.lua"
        lines = []
        for argv in vectors:
            try:
                options = d.client_options(argv)
            except ShellError:
                options = None
            literal = "{" + ",".join(json.dumps(value) for value in argv) + "}"
            lines.append("{argv=" + literal + ", accepted=" + str(options is not None).lower() +
                         ", remote=" + str(bool(options and options["remote"])).lower() +
                         ", session=" + json.dumps(options["session"] if options else "") + "}")
        fixtures.write_text("return {\n" + ",\n".join(lines) + "\n}\n")
        result = subprocess.run(["lua", str(Path(__file__).with_name("desktop-lua.lua")), str(generated), str(fixtures)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
