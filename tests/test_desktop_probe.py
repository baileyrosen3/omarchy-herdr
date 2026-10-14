import importlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import desktop_probe as probe


class HyprctlFixture:
    """Only local fixture sockets and JSON; no process reaches a compositor."""

    def __init__(self, test):
        directory = tempfile.TemporaryDirectory(prefix="herdr-instance-unit-")
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = {"XDG_RUNTIME_DIR": str(self.root), "WAYLAND_DISPLAY": "wayland-old",
                    "HERDR_SHELL_DESKTOP_CONFIG_HOME": "/fixture/desktop-config",
                    "OMARCHY_PATH": "/fixture/omarchy", "PATH": "/fixture/bin",
                    "HERDR_SOCKET_PATH": "/fixture/inherited-herdr.sock"}
        self.records = []
        self.responses = {}
        self.calls = []
        self.instances_output = None
        self.instances_error = None
        self.test = test

    def instance(self, signature, display="wayland-1", *, responds=True, socket_path=True):
        self.records.append({"instance": signature, "time": 123, "pid": 456, "wl_socket": display})
        folder = self.root / "hypr" / signature
        folder.mkdir(parents=True)
        if socket_path:
            control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.test.addCleanup(control.close)
            control.bind(str(folder / ".socket.sock"))
            control.listen(1)
        self.responses[signature] = (0, json.dumps({"version": "0.56.2"}), "") if responds else (
            1, "", "couldn't connect to the control socket")

    def run(self, argv, *, env, capture_output, text, timeout, check):
        self.calls.append((list(argv), dict(env)))
        self.test.assertTrue(capture_output)
        self.test.assertTrue(text)
        self.test.assertEqual(timeout, 4)
        self.test.assertFalse(check)
        for name in ("OMARCHY_PATH", "PATH", "HERDR_SHELL_DESKTOP_CONFIG_HOME", "HERDR_SOCKET_PATH"):
            self.test.assertEqual(env[name], self.env[name], f"Inherited {name} was lost")
        if argv == ["hyprctl", "-j", "instances"]:
            if self.instances_error:
                if isinstance(self.instances_error, Exception):
                    raise self.instances_error
                return subprocess.CompletedProcess(argv, 1, "", self.instances_error)
            output = json.dumps(self.records) if self.instances_output is None else self.instances_output
            return subprocess.CompletedProcess(argv, 0, output, "")
        self.test.assertEqual([argv[0], *argv[3:]], ["hyprctl", "-j", "version"])
        self.test.assertEqual(argv[1], "-i")
        signature = argv[2]
        self.test.assertEqual(env["HYPRLAND_INSTANCE_SIGNATURE"], signature)
        response = self.responses[signature]
        if isinstance(response, Exception):
            raise response
        return subprocess.CompletedProcess(argv, *response)

    def select(self, *, uid=None):
        return probe.select_hyprland_environment(self.env, run_command=self.run, uid=uid)

    def verified(self):
        return [argv[2] for argv, _ in self.calls if "version" in argv]


class HyprlandDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HyprctlFixture(self)

    def test_stale_inherited_socket_selects_sole_live_instance(self):
        f = self.fixture
        f.instance("stale", responds=False)
        f.instance("current", "wayland-1")
        f.env["HYPRLAND_INSTANCE_SIGNATURE"] = "stale"
        inherited = dict(f.env)
        selected = f.select()
        self.assertEqual(selected["HYPRLAND_INSTANCE_SIGNATURE"], "current")
        self.assertEqual(selected["WAYLAND_DISPLAY"], "wayland-1")
        self.assertEqual(f.env, inherited, "Discovery must not mutate the inherited environment")
        self.assertEqual({k: v for k, v in selected.items() if k not in (
            "HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY")},
            {k: v for k, v in inherited.items() if k not in (
                "HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY")})
        self.assertEqual(f.verified(), ["stale", "current"])

    def test_missing_stale_socket_selects_live_json_instance(self):
        f = self.fixture
        f.env["HYPRLAND_INSTANCE_SIGNATURE"] = "missing-stale"
        f.instance("current", "wayland-2")
        selected = f.select()
        self.assertEqual(selected["HYPRLAND_INSTANCE_SIGNATURE"], "current")
        self.assertEqual(selected["WAYLAND_DISPLAY"], "wayland-2")
        self.assertEqual(f.verified(), ["current"])

    def test_multiple_live_instances_without_explicit_choice_are_ambiguous(self):
        f = self.fixture
        f.instance("first", "wayland-1")
        f.instance("second", "wayland-2")
        with self.assertRaisesRegex(RuntimeError, "Multiple live Hyprland instances") as error:
            f.select()
        self.assertIn("first (wayland-1)", str(error.exception))
        self.assertIn("second (wayland-2)", str(error.exception))
        self.assertIn("Set HYPRLAND_INSTANCE_SIGNATURE", str(error.exception))
        self.assertIn("Refusing to guess", str(error.exception))
        self.assertNotIn("HYPRLAND_INSTANCE_SIGNATURE", f.env)
        self.assertEqual(f.verified(), ["first", "second"])

    def test_valid_explicit_choice_is_preserved_with_multiple_instances(self):
        f = self.fixture
        f.instance("first", "wayland-1")
        f.instance("second", "wayland-2")
        f.env["HYPRLAND_INSTANCE_SIGNATURE"] = "second"
        selected = f.select()
        self.assertEqual(selected["HYPRLAND_INSTANCE_SIGNATURE"], "second")
        self.assertEqual(selected["WAYLAND_DISPLAY"], "wayland-2")
        self.assertEqual(f.verified(), ["second"])

    def test_stale_explicit_choice_does_not_resolve_multiple_live_instances(self):
        f = self.fixture
        f.instance("stale", responds=False)
        f.instance("first", "wayland-1")
        f.instance("second", "wayland-2")
        f.env["HYPRLAND_INSTANCE_SIGNATURE"] = "stale"
        with self.assertRaisesRegex(RuntimeError, "Multiple live Hyprland instances"):
            f.select()
        self.assertEqual(f.env["HYPRLAND_INSTANCE_SIGNATURE"], "stale")

    def test_directory_and_regular_file_are_not_live_control_sockets(self):
        f = self.fixture
        f.instance("directory-only", socket_path=False)
        f.instance("regular-file", socket_path=False)
        (f.root / "hypr/regular-file/.socket.sock").write_text("not a socket")
        with self.assertRaisesRegex(RuntimeError, "No live Hyprland control socket"):
            f.select()
        self.assertEqual(f.verified(), [])

    def test_socket_owned_by_a_different_uid_is_not_a_candidate(self):
        f = self.fixture
        f.instance("foreign")
        with self.assertRaisesRegex(RuntimeError, "not a control socket owned by UID"):
            f.select(uid=os.getuid() + 1)
        self.assertEqual(f.verified(), [])

    def test_nonresponding_socket_is_rejected_with_actionable_error(self):
        f = self.fixture
        f.instance("disconnected", responds=False)
        with self.assertRaisesRegex(RuntimeError, "No live Hyprland control socket") as error:
            f.select()
        self.assertIn("couldn't connect", str(error.exception))
        self.assertIn("hyprctl -j instances", str(error.exception))
        self.assertIn("no configuration was changed", str(error.exception))

    def test_duplicate_json_records_do_not_create_false_ambiguity(self):
        f = self.fixture
        f.instance("current")
        f.records.append(dict(f.records[0]))
        self.assertEqual(f.select()["HYPRLAND_INSTANCE_SIGNATURE"], "current")
        self.assertEqual(f.verified(), ["current"])

    def test_bad_instances_json_and_schema_fail_clearly(self):
        f = self.fixture
        malformed = ["not JSON", "{}", "[null]", '[{"instance": "current"}]',
                     '[{"instance": "../escape", "wl_socket": "wayland-1"}]',
                     '[{"instance": "current", "wl_socket": "../escape"}]',
                     '[{"instance": "current", "wl_socket": "wayland-1"}, '
                     '{"instance": "current", "wl_socket": "wayland-2"}]']
        for output in malformed:
            with self.subTest(output=output):
                f.instances_output = output
                with self.assertRaisesRegex(RuntimeError, "Cannot discover Hyprland instances"):
                    f.select()
        self.assertEqual(f.verified(), [])

    def test_instance_enumeration_failures_are_actionable(self):
        f = self.fixture
        for failure in ("instances rejected", FileNotFoundError("hyprctl missing"),
                        subprocess.TimeoutExpired(["hyprctl", "-j", "instances"], 4)):
            with self.subTest(failure=failure):
                f.instances_error = failure
                with self.assertRaisesRegex(RuntimeError, "Cannot discover Hyprland instances") as error:
                    f.select()
                self.assertIn("XDG_RUNTIME_DIR", str(error.exception))

    def test_invalid_or_timed_out_socket_response_is_not_live(self):
        f = self.fixture
        f.instance("current")
        for response in ((0, "not JSON", ""), (0, "{}", ""), (0, "[]", ""),
                         subprocess.TimeoutExpired(["hyprctl", "-i", "current", "-j", "version"], 4)):
            with self.subTest(response=response):
                f.responses["current"] = response
                with self.assertRaisesRegex(RuntimeError, "No live Hyprland control socket"):
                    f.select()

    def test_valid_explicit_socket_without_display_metadata_does_not_guess(self):
        f = self.fixture
        f.instance("explicit")
        f.env["HYPRLAND_INSTANCE_SIGNATURE"] = "explicit"
        f.records.clear()
        with self.assertRaisesRegex(RuntimeError, "does not report its Wayland display"):
            f.select()


class DesktopProbeStartupTests(unittest.TestCase):
    def test_import_does_not_start_probe_or_query_live_desktop(self):
        with patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as spawn, \
             patch.object(os, "open") as open_device, patch.object(probe.desktop, "enabled") as enabled:
            importlib.reload(probe)
        run.assert_not_called()
        spawn.assert_not_called()
        open_device.assert_not_called()
        enabled.assert_not_called()

    def test_startup_uses_selected_environment_before_desktop_checks_and_restores_it(self):
        inherited = {"HYPRLAND_INSTANCE_SIGNATURE": "stale", "WAYLAND_DISPLAY": "old-display",
                     "HERDR_SHELL_DESKTOP_CONFIG_HOME": "/fixture/profile", "OMARCHY_PATH": "/fixture/omarchy"}
        selected = dict(inherited, HYPRLAND_INSTANCE_SIGNATURE="current", WAYLAND_DISPLAY="wayland-1")
        observed = []
        with patch.dict(os.environ, inherited, clear=True), patch.object(probe.sys, "argv", ["desktop_probe.py"]), \
             patch.object(probe, "select_hyprland_environment", return_value=selected) as select, \
             patch.object(probe, "_run_probe", side_effect=lambda args: observed.append(dict(os.environ))), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            probe.main()
            self.assertEqual(dict(os.environ), inherited)
        select.assert_called_once_with()
        self.assertEqual(observed, [selected])
        self.assertIn("replacing stale/unreachable inherited stale", output.getvalue())
        self.assertIn("WAYLAND_DISPLAY=wayland-1", output.getvalue())
        self.assertIn("discovery is read-only", output.getvalue())

    def test_isolated_server_keeps_selected_compositor_and_desktop_profile_namespace(self):
        inherited = {"HYPRLAND_INSTANCE_SIGNATURE": "current", "WAYLAND_DISPLAY": "wayland-1",
                     "XDG_RUNTIME_DIR": "/fixture/runtime", "XDG_CONFIG_HOME": "/fixture/config",
                     "HERDR_SOCKET_PATH": "/fixture/working-server.sock", "HERDR_PANE_ID": "working-pane",
                     "HERDR_SHELL_DESKTOP_CONFIG_HOME": "/fixture/desktop-profile",
                     "OMARCHY_PATH": "/fixture/omarchy", "PATH": "/fixture/bin", "TMUX": "working-tmux"}
        for explicit_profile in (True, False):
            with self.subTest(explicit_profile=explicit_profile):
                source = dict(inherited)
                if not explicit_profile:
                    source.pop("HERDR_SHELL_DESKTOP_CONFIG_HOME")
                with patch.dict(os.environ, source, clear=True):
                    isolated = probe.isolated_probe_environment(Path("/fixture/disposable"))
                    self.assertEqual(dict(os.environ), source)
                for name in ("HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                             "OMARCHY_PATH", "PATH"):
                    self.assertEqual(isolated[name], source[name])
                self.assertEqual(isolated["HERDR_SHELL_DESKTOP_CONFIG_HOME"],
                                 "/fixture/desktop-profile" if explicit_profile else "/fixture/config")
                self.assertEqual(isolated["XDG_CONFIG_HOME"], "/fixture/disposable/config")
                self.assertEqual(isolated["HERDR_CONFIG_PATH"], "/fixture/disposable/config.toml")
                self.assertNotIn("HERDR_SOCKET_PATH", isolated)
                self.assertNotIn("HERDR_PANE_ID", isolated)
                self.assertNotIn("TMUX", isolated)


if __name__ == "__main__":
    unittest.main()
