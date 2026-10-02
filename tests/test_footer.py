"""Footer honesty, one-row geometry, recovery, and native invocation behavior."""
from contextlib import redirect_stdout
import io
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from herdr_shell import PLUGIN_ID, cli, config, desktop, footer

DEFAULTS = {"prefix": "ctrl+space", "focus_pane_left": "ctrl+alt+left"}
ALL_READY = {action: {"active": True, "status": "ready"} for _, action, _ in desktop.MAPPINGS}


class FooterTests(unittest.TestCase):
    def test_menu_stays_visible_and_one_row_never_exceeds_available_cells(self):
        for width in (0, 1, 10, 11, 15, 16, 17, 18, 19, 35, 80, 120, 240, 500):
            with self.subTest(width=width):
                line = footer.render(width, ALL_READY)
                self.assertLessEqual(footer.cell_width(line), width)
                self.assertNotIn("\n", line)
                self.assertNotIn("\r", line)
                if width >= 18:
                    self.assertTrue(line.startswith("Super+Alt | M Menu"))
                elif width >= 11:
                    self.assertTrue(line.startswith("Super+Alt+M"))
                else:
                    self.assertEqual(line, "")

    def test_wide_footer_teaches_each_of_the_26_chords(self):
        line = footer.render(500, ALL_READY)
        self.assertIn("U/D/L/R Split", line)
        self.assertIn("Shift+U/Shift+D/Shift+L/Shift+R Swap", line)
        self.assertIn("PageUp/PageDown Switch tab", line)
        self.assertIn("Shift+PageUp/Shift+PageDown Switch space", line)
        self.assertIn("Q Next agent", line)
        self.assertIn("Shift+Q Prev agent", line)
        self.assertIn("P Next pane", line)
        self.assertIn("Shift+P Prev pane", line)
        self.assertIn("T New tab", line)
        self.assertIn("W Close tab", line)
        self.assertIn("Shift+T New space", line)
        self.assertIn("Shift+W Close space", line)
        for key, action, _ in desktop.MAPPINGS:
            with self.subTest(action=action):
                available = {action: {"active": True, "status": "ready"}}
                one = footer.render(500, available, "Native Menu")
                suffix = desktop.pretty_key(key).removeprefix("Super+Alt+")
                self.assertIn(suffix + " ", one)

    def test_partial_direction_conflicts_remove_only_reserved_keys(self):
        available = {action: dict(row) for action, row in ALL_READY.items()}
        available["pane-split-left"] = {"active": False, "status": "conflict"}
        available["pane-swap-down"] = {"active": False, "status": "conflict"}
        available["agent-cycle-previous"] = {"active": False, "status": "disabled"}
        line = footer.render(500, available)
        self.assertIn("U/D/R Split", line)
        self.assertIn("Shift+U/Shift+L/Shift+R Swap", line)
        self.assertIn("Q Next agent", line)
        self.assertNotIn("U/D/L/R Split", line)
        self.assertNotIn("Shift+D", line)
        self.assertNotIn("Shift+Q", line)

    def test_active_flag_without_verified_ready_status_cannot_advertise_a_key(self):
        for row in ({"active": True, "status": "unavailable"}, {"active": False, "status": "ready"},
                    {"active": True}, {"status": "ready"}):
            with self.subTest(row=row):
                self.assertEqual(footer.render(80, {"menu": row}, "Native Menu"), "Native Menu")

    def test_reserved_menu_shows_recovery_and_only_available_direct_keys(self):
        available = {action: dict(row) for action, row in ALL_READY.items()}
        available["menu"] = {"active": False, "status": "conflict"}
        line = footer.render(80, available, "Ctrl+Space → Space Menu")
        self.assertTrue(line.startswith("Ctrl+Space → Space Menu | Super+Alt"))
        self.assertNotIn("M Menu", line)
        self.assertIn("U/D/L/R Split", line)

    def test_control_sequences_and_invisible_overrides_are_never_emitted(self):
        unsafe = "\x1b[31mCtrl\x1b[0m\n+\t界e\u0301\u202e\x00 Menu\x1b]0;bad title\x07"
        for width in (0, 1, 4, 7, 9, 11, 80):
            with self.subTest(width=width):
                line = footer.render(width, {}, unsafe)
                self.assertLessEqual(footer.cell_width(line), width)
                self.assertNotIn("\x1b", line)
                self.assertNotIn("\n", line)
                self.assertNotIn("\u202e", line)
                self.assertNotIn("\x00", line)
                self.assertNotIn("bad title", line)
        self.assertEqual(footer.cell_width("界e\u0301"), 3)
        self.assertEqual(footer.cell_width("界\ufe0f"), 2)
        self.assertEqual(footer.fit("a界b", 2), "a")
        self.assertEqual(footer.fit("a界b", 3), "a界")

    def test_native_fallback_uses_configured_prefix_and_customized_menu_key(self):
        text = f'''[keys]
prefix = "ctrl+b"
[[keys.command]]
key = "prefix+g"
type = "plugin_action"
command = "{PLUGIN_ID}.menu"
'''
        with patch.object(config, "defaults", return_value=DEFAULTS):
            self.assertEqual(footer.native_menu_hint(text), "Ctrl+B → G Menu")

    def test_occupied_or_unbound_native_fallback_is_not_advertised(self):
        for key in ("", "prefix+space"):
            text = f'''[keys]
zoom = "prefix+space"
[[keys.command]]
key = "{key}"
type = "plugin_action"
command = "{PLUGIN_ID}.menu"
'''
            with self.subTest(key=key), patch.object(config, "defaults", return_value=DEFAULTS):
                self.assertEqual(footer.native_menu_hint(text), "herdr-shell menu")

    def test_occupied_prefix_cannot_advertise_a_native_recovery_sequence(self):
        text = f'''[keys]
prefix = "ctrl+space"
zoom = "ctrl+space"
[[keys.command]]
key = "prefix+g"
type = "plugin_action"
command = "{PLUGIN_ID}.menu"
'''
        with patch.object(config, "defaults", return_value=DEFAULTS):
            self.assertEqual(footer.native_menu_hint(text), "herdr-shell menu")

    def test_native_width_environment_takes_precedence_and_invalid_input_falls_back(self):
        with patch.dict(os.environ, HERDR_FOOTER_WIDTH="35"), \
             patch.object(footer.shutil, "get_terminal_size", return_value=os.terminal_size((120, 24))):
            self.assertEqual(footer.default_width(), 35)
            os.environ["HERDR_FOOTER_WIDTH"] = "invalid"
            self.assertEqual(footer.default_width(), 120)
            os.environ["HERDR_FOOTER_WIDTH"] = "-1"
            self.assertEqual(footer.default_width(), 0)

    def test_provider_does_not_read_config_when_menu_is_verified_active(self):
        store = Mock()
        with patch.object(desktop, "effective_bindings", return_value=ALL_READY) as lookup, \
             patch.dict(os.environ, HERDR_FOOTER_LOCAL="1"):
            self.assertTrue(footer.output(80, store).startswith("Super+Alt | M Menu"))
        lookup.assert_called_once_with(refresh=True)
        store.read.assert_not_called()

    def test_failed_desktop_inspection_uses_native_recovery(self):
        store = Mock()
        store.read.return_value = "[keys]\n"
        with patch.object(desktop, "effective_bindings", side_effect=subprocess.TimeoutExpired("hyprctl", 4)), \
             patch.object(config, "defaults", return_value=DEFAULTS), \
             patch.dict(os.environ, HERDR_FOOTER_LOCAL="1"):
            self.assertEqual(footer.output(80, store), "herdr-shell menu")

    def test_remote_endpoint_never_uses_host_bindings_or_config(self):
        store = Mock()
        with patch.object(desktop, "effective_bindings") as lookup, \
             patch.dict(os.environ, HERDR_FOOTER_LOCAL="0"):
            self.assertEqual(footer.output(80, store), "Remote Herdr: native shortcuts")
        lookup.assert_not_called()
        store.read.assert_not_called()

    def test_footer_cli_emits_plain_line_and_needs_no_pane(self):
        stdout = io.StringIO()
        with patch.object(footer, "output", return_value="Super+Alt | M Menu") as provider, \
             patch.object(cli, "resolve_context") as resolve, redirect_stdout(stdout):
            self.assertEqual(cli.main(["footer", "--width", "80"]), 0)
        self.assertEqual(stdout.getvalue(), "Super+Alt | M Menu\n")
        provider.assert_called_once()
        self.assertEqual(provider.call_args.args[0], 80)
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
