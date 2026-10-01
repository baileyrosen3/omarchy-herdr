import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from herdr_shell.config import ConfigStore, bindings, command_id, conflicts, shortcut_changes
from herdr_shell.runtime import ShellError


DEFAULTS = {"prefix": "ctrl+b", "focus_pane_left": "prefix+h", "focus_pane_right": "prefix+l",
            "switch_tab": "prefix+1..9", "navigate_pane_left": "h", "zoom": "prefix+z"}
BASE = '# My hand-written comment\n[keys]\nprefix = "ctrl+space"\nfocus_pane_left = ["ctrl+alt+left", "prefix+h"] # keep this\n\n[ui]\npane_gaps = false\n'


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.path = root / "config.toml"
        self.path.write_text(BASE)
        self.store = ConfigStore(self.path, root / "state", validator=tomllib.loads)
        self.defaults = patch("herdr_shell.config.defaults", return_value=DEFAULTS)
        self.defaults.start()
        self.addCleanup(self.defaults.stop)

    def test_targeted_edit_keeps_comments_and_unrelated_content(self):
        p = self.store.prepare([(["keys", "zoom"], "prefix+f")])
        self.store.apply(p)
        text = self.path.read_text()
        self.assertIn('# My hand-written comment', text)
        self.assertIn('focus_pane_left = ["ctrl+alt+left", "prefix+h"] # keep this', text)
        self.assertIn('pane_gaps = false', text)

    def test_install_is_idempotent(self):
        self.store.apply(self.store.prepare(shortcut_changes()))
        self.assertFalse(self.store.prepare(shortcut_changes())["changes"])
        self.assertEqual(len(tomllib.loads(self.path.read_text())["keys"]["command"]), 2)

    def recovery(self, before, *, menu_key="prefix+space", bindings_key="prefix+alt+k", reserved=None):
        self.path.write_text(before)
        proposal = self.store.prepare(shortcut_changes(menu_key, bindings_key, existing=before, reserved=reserved), before)
        self.store.apply(proposal)
        return {command["command"]: command for command in tomllib.loads(self.path.read_text())["keys"]["command"]}, proposal

    def test_install_keeps_occupied_native_key_and_adds_unbound_menu(self):
        before = BASE.replace('prefix = "ctrl+space"', 'prefix = "ctrl+space"\nzoom = "prefix+space"')
        report = []
        commands, proposal = self.recovery(before, reserved=report)
        self.assertEqual(commands["blr.herdr-shell.menu"]["key"], "")
        self.assertEqual(commands["blr.herdr-shell.keybindings"]["key"], "prefix+alt+k")
        self.assertEqual(tomllib.loads(self.path.read_text())["keys"]["zoom"], "prefix+space")
        self.assertIn('# My hand-written comment', self.path.read_text())
        self.assertFalse(tomllib.loads(self.path.read_text())["ui"]["pane_gaps"])
        self.assertEqual(report[0]["action"], "menu")
        self.assertEqual(report[0]["action_id"], "blr.herdr-shell.menu")
        self.assertEqual(report[0]["key"], "prefix+space")
        self.assertEqual([owner["id"] for owner in report[0]["owners"]], ["zoom"])
        self.assertEqual(report[0]["owners"][0]["source"], "user")
        self.assertIn("Zoom", report[0]["reason"])
        self.assertNotIn("Shortcut collision", proposal["diff"])

    def test_install_respects_default_owner_and_normalized_modifier_aliases(self):
        report = {}
        commands, _ = self.recovery(BASE, menu_key="PREFIX + L", bindings_key="alt+control+left", reserved=report)
        self.assertEqual(commands["blr.herdr-shell.menu"]["key"], "")
        self.assertEqual(commands["blr.herdr-shell.keybindings"]["key"], "")
        self.assertEqual(report["menu"]["owners"][0]["id"], "focus_pane_right")
        self.assertEqual(report["menu"]["owners"][0]["source"], "default")
        self.assertEqual(report["keybindings"]["owners"][0]["id"], "focus_pane_left")

    def test_install_respects_custom_shell_and_plugin_action_owners(self):
        for command_type in ("shell", "plugin_action"):
            with self.subTest(command_type=command_type):
                command = {"key": ["prefix+space", "prefix+f"], "type": command_type,
                           "command": "other.plugin.menu", "description": "Personal menu"}
                before = BASE + '\n[[keys.command]]\nkey = ["prefix+space", "prefix+f"]\ntype = "' + command_type + '"\ncommand = "other.plugin.menu"\ndescription = "Personal menu"\n'
                report = []
                commands, _ = self.recovery(before, reserved=report)
                self.assertEqual(commands["other.plugin.menu"], command)
                self.assertEqual(commands["blr.herdr-shell.menu"]["key"], "")
                self.assertEqual(report[0]["owners"][0]["id"], "custom:" + command_id(command))
                self.assertEqual(report[0]["owners"][0]["label"], "Personal menu")

    def test_install_respects_expanded_range_and_legacy_indexed_bindings(self):
        for before, chord, owner in ((BASE, "prefix+5", "switch_tab"),
                                     (BASE + '\n[keys.indexed]\nworkspace = "ctrl+alt"\n', "option+control+8", "indexed:workspace")):
            with self.subTest(chord=chord):
                report = []
                commands, _ = self.recovery(before, menu_key=chord, reserved=report)
                self.assertEqual(commands["blr.herdr-shell.menu"]["key"], "")
                self.assertEqual(report[0]["owners"][0]["id"], owner)

    def test_install_does_not_reserve_same_key_in_navigation_mode(self):
        report = []
        commands, _ = self.recovery(BASE, menu_key="h", reserved=report)
        self.assertEqual(commands["blr.herdr-shell.menu"]["key"], "h")
        self.assertEqual(report, [])

    def test_install_preserves_custom_and_disabled_owned_fallbacks_verbatim(self):
        for key in ('"prefix+f"', '["prefix+f", "prefix+g"]', '""', '[]'):
            with self.subTest(key=key):
                before = BASE + '\n[[keys.command]]\nkey = ' + key + '\ntype = "plugin_action"\ncommand = "blr.herdr-shell.menu"\ndescription = "My menu"\n[[keys.command]]\nkey = ""\ntype = "plugin_action"\ncommand = "blr.herdr-shell.keybindings"\ndescription = "My keys"\n'
                report = []
                commands, proposal = self.recovery(before, reserved=report)
                self.assertEqual(proposal["changes"], [])
                self.assertEqual(self.path.read_text(), before)
                self.assertEqual(commands["blr.herdr-shell.menu"]["key"], tomllib.loads('key=' + key)["key"])
                self.assertEqual(report, [])

    def test_install_is_idempotent_after_reservation_and_tracks_both_new_fallbacks(self):
        report = []
        commands, _ = self.recovery(BASE, menu_key="prefix+a", bindings_key="prefix+a", reserved=report)
        self.assertEqual(commands["blr.herdr-shell.menu"]["key"], "prefix+a")
        self.assertEqual(commands["blr.herdr-shell.keybindings"]["key"], "")
        self.assertEqual(report[0]["action"], "keybindings")
        self.assertEqual(report[0]["owners"][0]["label"], "Herdr Shell menu")
        before = self.path.read_text()
        commands, proposal = self.recovery(before)
        self.assertEqual(proposal["changes"], [])
        self.assertEqual(self.path.read_text(), before)

    def test_explicit_config_apply_still_rejects_occupied_default_recovery_key(self):
        before = BASE.replace('prefix = "ctrl+space"', 'prefix = "ctrl+space"\nzoom = "prefix+space"')
        self.path.write_text(before)
        with self.assertRaisesRegex(ShellError, "Shortcut collision"):
            self.store.prepare(shortcut_changes())
        self.assertEqual(self.path.read_text(), before)

    def test_collision_with_default_and_expanded_range(self):
        for chord in ("prefix+l", "prefix+5"):
            with self.subTest(chord=chord), self.assertRaisesRegex(ShellError, "collision"):
                self.store.prepare([(["keys", "zoom"], chord)])

    def test_equivalent_modifier_order_collides(self):
        with self.assertRaisesRegex(ShellError, "collision"):
            self.store.prepare([(["keys", "zoom"], "alt+control+left")])

    def test_navigate_mode_is_separate(self):
        p = self.store.prepare([(["keys", "zoom"], "h")])
        self.assertTrue(p["changes"])

    def test_disabled_empty_array_is_readable(self):
        self.path.write_text(BASE + '\n[keys_extra]\nunused = true\n')
        p = self.store.prepare([(["keys", "zoom"], [])])
        row = next(r for r in bindings(p["after"]) if r["id"] == "zoom")
        self.assertEqual(row["keys"], [])

    def test_custom_commands_participate_in_collisions(self):
        self.path.write_text(BASE + '\n[[keys.command]]\nkey = "prefix+f"\ntype = "shell"\ncommand = "echo hi"\n')
        with self.assertRaisesRegex(ShellError, "collision"):
            self.store.prepare([(["keys", "zoom"], "prefix+f")])

    def test_existing_collision_does_not_block_unrelated_edit(self):
        self.path.write_text(BASE.replace('prefix = "ctrl+space"', 'prefix = "ctrl+space"\nzoom = "prefix+l"'))
        self.store.apply(self.store.prepare([(["ui", "pane_gaps"], True)]))

    def test_concurrent_edit_after_preview_is_not_overwritten(self):
        p = self.store.prepare([(["keys", "zoom"], "prefix+f")])
        self.path.write_text(BASE + '# Someone edited this\n')
        with self.assertRaisesRegex(ShellError, "changed since"):
            self.store.apply(p)
        self.assertIn('Someone edited', self.path.read_text())

    def test_concurrent_edit_during_validation_is_not_overwritten(self):
        self.store.validator = lambda _: self.path.write_text(BASE + '# concurrent\n')
        with self.assertRaisesRegex(ShellError, "during validation"):
            self.store.apply(self.store.prepare([(["ui", "pane_gaps"], True)]))

    def test_validation_failure_leaves_file_and_history_alone(self):
        def fail(_):
            raise ShellError("invalid key")
        self.store.validator = fail
        with self.assertRaisesRegex(ShellError, "invalid key"):
            self.store.apply(self.store.prepare([(["keys", "zoom"], "bad")]))
        self.assertEqual(self.path.read_text(), BASE)
        self.assertEqual(list(self.store.state.glob("*.json")), [])

    def test_reload_failure_rolls_back(self):
        def fail():
            raise ShellError("server unavailable")
        with self.assertRaisesRegex(ShellError, "rolled_back"):
            self.store.apply(self.store.prepare([(["ui", "pane_gaps"], True)]), fail)
        self.assertEqual(self.path.read_text(), BASE)

    def test_reload_failure_does_not_overwrite_a_concurrent_edit(self):
        def fail():
            self.path.write_text('# externally replaced\n')
            raise ShellError("server unavailable")
        with self.assertRaisesRegex(ShellError, "concurrent_edit"):
            self.store.apply(self.store.prepare([(["ui", "pane_gaps"], True)]), fail)
        self.assertEqual(self.path.read_text(), '# externally replaced\n')

    def test_undo_preserves_later_unrelated_edits(self):
        self.store.apply(self.store.prepare([(["keys", "zoom"], "prefix+f")]))
        self.path.write_text(self.path.read_text() + '# later edit\n')
        p, record = self.store.undo_proposal()
        self.store.undo(p, record)
        self.assertNotIn('zoom =', self.path.read_text())
        self.assertIn('# later edit', self.path.read_text())
        with self.assertRaisesRegex(ShellError, "No applied"):
            self.store.undo_proposal()

    def test_undo_refuses_to_replace_later_edit_to_same_key(self):
        self.store.apply(self.store.prepare([(["keys", "zoom"], "prefix+f")]))
        self.path.write_text(self.path.read_text().replace('zoom = "prefix+f"', 'zoom = "prefix+g"'))
        with self.assertRaisesRegex(ShellError, "later edit"):
            self.store.undo_proposal()

    def test_undo_install_preserves_later_custom_command(self):
        self.store.apply(self.store.prepare(shortcut_changes()))
        self.path.write_text(self.path.read_text() + '\n[[keys.command]]\nkey = "prefix+a"\ncommand = "user-command"\ntype = "shell"\n')
        p, record = self.store.undo_proposal()
        self.store.undo(p, record)
        cmds = tomllib.loads(self.path.read_text())["keys"]["command"]
        self.assertEqual([c["command"] for c in cmds], ['user-command'])

    def test_custom_command_edit_undo_preserves_other_fields(self):
        self.store.apply(self.store.prepare(shortcut_changes()))
        row = next(r for r in bindings(self.path.read_text()) if r["source"] == "custom")
        self.store.apply(self.store.prepare([(row["path"], "prefix+a")]))
        p, record = self.store.undo_proposal()
        self.store.undo(p, record)
        self.assertEqual(tomllib.loads(self.path.read_text())["keys"]["command"][0]["key"], 'prefix+space')


if __name__ == '__main__':
    unittest.main()
