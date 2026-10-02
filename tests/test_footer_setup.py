"""Native capability gating and exact footer-provider ownership."""
import json
from pathlib import Path
import shlex
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from herdr_shell import config, footer_setup
from herdr_shell.runtime import ShellError


BASE = '# My layout\n[ui]\ntab_bar_position="top"\npane_gaps=false\nfooter_separator=" personal "\n'
PERSONAL = {"type": "text", "text": "My status"}


class FooterSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "plugin path with $(literal) `ticks`"
        self.root.mkdir()
        self.path = Path(temporary.name) / "config.toml"
        self.path.write_text(BASE)
        self.store = config.ConfigStore(self.path, Path(temporary.name) / "state", Mock(side_effect=tomllib.loads))
        patcher = patch.object(config, "defaults", return_value={"prefix": "ctrl+space", "focus_pane_left": "ctrl+alt+left"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def proposal(self, **options):
        return self.store.prepare(footer_setup.changes(self.store.read(), self.root, **options))

    def install(self):
        self.store.apply(self.proposal(capability=True))
        return tomllib.loads(self.store.read())["ui"]["footer"]

    def test_explicit_capability_flag_is_required_and_read_only(self):
        with patch.object(footer_setup, "run_herdr", return_value='{"footer":1,"other":0}') as probe:
            self.assertTrue(footer_setup.supported())
        probe.assert_called_once_with(["--client-capabilities"])
        for reply in ("not json", "null", "[]", '{}', '{"footer":0}', '{"footer":true}', '{"footer":"1"}', '{"footer":2}'):
            with self.subTest(reply=reply), patch.object(footer_setup, "run_herdr", return_value=reply):
                self.assertFalse(footer_setup.supported())
        with patch.object(footer_setup, "run_herdr", side_effect=ShellError("unknown option")):
            self.assertFalse(footer_setup.supported())
            self.assertEqual(self.proposal()["changes"], [])
        self.assertEqual(self.path.read_text(), BASE)

    def test_supported_install_has_absolute_safely_quoted_provider_and_one_entry(self):
        entries = self.install()
        self.assertEqual(len(entries), 1)
        command = shlex.split(entries[0]["command"])
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual(command[1:], [str(self.root / "bin/herdr-shell"), "footer"])
        self.assertEqual(entries[0]["interval_seconds"], 5)
        self.assertEqual(entries[0]["timeout_seconds"], 2)
        ui = tomllib.loads(self.store.read())["ui"]
        self.assertEqual(ui["tab_bar_position"], "top")
        self.assertEqual(ui["footer_separator"], " personal ")
        self.assertFalse(ui["pane_gaps"])
        self.assertIn("# My layout", self.store.read())

    def test_install_is_idempotent_and_preserves_later_provider_edits(self):
        entries = self.install()
        entries[0]["interval_seconds"] = 17
        self.store.apply(self.store.prepare([(["ui", "footer"], entries)]))
        before = self.store.read()
        self.assertEqual(self.proposal(capability=True)["changes"], [])
        self.assertEqual(self.store.read(), before)

    def test_personal_footer_and_other_installation_are_never_replaced(self):
        for entry in (PERSONAL, {"type": "command", "command": footer_setup.provider_command(self.root / "other")}):
            with self.subTest(entry=entry):
                self.path.write_text(BASE)
                self.store.apply(self.store.prepare([(["ui", "footer"], [entry])]))
                before = self.store.read()
                self.assertEqual(self.proposal(capability=True)["changes"], [])
                self.assertEqual(self.proposal(remove=True)["changes"], [])
                self.assertEqual(self.store.read(), before)

    def test_explicit_empty_footer_is_a_personal_disabled_setting(self):
        self.store.apply(self.store.prepare([(["ui", "footer"], [])]))
        before = self.store.read()
        self.assertEqual(self.proposal(capability=True)["changes"], [])
        self.assertEqual(self.proposal(remove=True)["changes"], [])
        self.assertEqual(self.proposal(capability=False)["changes"], [])
        self.assertEqual(self.store.read(), before)

    def test_remove_preserves_later_personal_entries_and_separator(self):
        entries = self.install()
        entries[0]["timeout_seconds"] = 8
        self.store.apply(self.store.prepare([(["ui", "footer"], [PERSONAL, *entries, {"type": "hostname"}])]))
        with patch.object(footer_setup, "supported", side_effect=AssertionError("removal must not probe")):
            self.store.apply(self.proposal(remove=True))
        ui = tomllib.loads(self.store.read())["ui"]
        self.assertEqual(ui["footer"], [PERSONAL, {"type": "hostname"}])
        self.assertEqual(ui["footer_separator"], " personal ")
        self.assertEqual(ui["tab_bar_position"], "top")

    def test_last_owned_entry_removes_field_and_downgrade_uses_same_cleanup(self):
        for options in ({"remove": True}, {"capability": False}, {"enabled": False}):
            with self.subTest(options=options):
                self.path.write_text(BASE)
                self.install()
                self.store.apply(self.proposal(**options))
                ui = tomllib.loads(self.store.read())["ui"]
                self.assertNotIn("footer", ui)
                self.assertEqual(ui["footer_separator"], " personal ")
                self.assertEqual(self.proposal(**options)["changes"], [])

    def test_similar_command_and_non_command_type_remain_personal(self):
        command = footer_setup.provider_command(self.root)
        for entry in ({"type": "command", "command": command + " --width 80"},
                      {"type": "text", "command": command, "text": "Status"}):
            with self.subTest(entry=entry):
                self.path.write_text(BASE)
                self.store.apply(self.store.prepare([(["ui", "footer"], [entry])]))
                self.assertEqual(self.proposal(remove=True)["changes"], [])

    def test_config_reload_failure_rolls_back_footer_in_same_transaction(self):
        before = self.store.read()
        with self.assertRaisesRegex(ShellError, "reload failed"):
            self.store.apply(self.proposal(capability=True), Mock(side_effect=ShellError("offline")))
        self.assertEqual(self.store.read(), before)


if __name__ == "__main__":
    unittest.main()
