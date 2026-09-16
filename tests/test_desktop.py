import json
import os
from pathlib import Path
import subprocess
import tempfile
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
        with patch.object(d, "hypr", return_value=""):
            d.install_desktop(d.install_proposal())
            self.assertTrue(d.enabled())
            self.assertEqual(d.hypr_path().read_text().count(d.BEGIN), 1)
            d.install_desktop(d.install_proposal())
            self.assertEqual(d.hypr_path().read_text().count(d.BEGIN), 1)
            d.install_desktop(d.install_proposal(remove=True), remove=True)
        self.assertFalse(d.enabled())
        self.assertEqual(d.hypr_path().read_text(), before)

    def test_reload_failure_restores_all_integration_files(self):
        with patch.object(d, "hypr", return_value=""):
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

    def test_lua_preserves_dispatcher_and_routes_only_herdr(self):
        generated = self.root / "bridge.lua"
        generated.write_text(d.integration_text())
        result = subprocess.run(["lua", str(Path(__file__).with_name("desktop-lua.lua")), str(generated)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
