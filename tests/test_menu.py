import curses
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from herdr_shell import config, menu as ui, runtime


def menu(page="menu"):
    instance = ui.Menu.__new__(ui.Menu)
    instance.page = page
    instance.context = SimpleNamespace(pane="w1:p1", workspace="w1", tab="t1", cwd="/project")
    instance.notice = ""
    instance.query = ""
    instance.selected = 0
    instance.scroll = 0
    instance.section = "home"
    instance.settings_section = "layout"
    instance.history = []
    instance.items = []
    instance.search_origin = None
    instance.show_unassigned = False
    instance.accent = instance.selection = 0
    return instance


def plan(kind="pane", running=True):
    return {
        "action_id": kind + "-close", "kind": kind, "target_id": "target",
        "title": "Close " + kind + " target?", "count": 1,
        "pane_identities": [{"pane_id": "w1:p1", "terminal_id": "terminal1", "workspace_id": "w1", "tab_id": "t1"}],
        "activity": [{"pane_id": "w1:p1", "label": "Agent pane", "reason": "Agent is working"}] if running else [],
        "needs_confirmation": running,
    }


class CloseMenuTests(unittest.TestCase):
    def setUp(self):
        preferences = tempfile.TemporaryDirectory()
        self.addCleanup(preferences.cleanup)
        preference_env = patch.dict(os.environ, HERDR_SHELL_DESKTOP_CONFIG_HOME=preferences.name)
        preference_env.start()
        self.addCleanup(preference_env.stop)

    def test_idle_close_keeps_concrete_target_without_prompt(self):
        instance = menu()
        instance.confirm_close = Mock()
        captured = plan(running=False)
        with patch.object(ui, "close_plan", return_value=captured):
            job = instance.activate({"id": "pane-close", "kind": "action"})
        self.assertIs(job["plan"], captured)
        self.assertEqual(job["kind"], "close")
        instance.confirm_close.assert_not_called()

    def test_running_close_cancellation_never_dispatches(self):
        for kind in ("pane", "tab", "workspace"):
            with self.subTest(kind=kind):
                instance = menu()
                instance.confirm_close = Mock(return_value=False)
                captured = plan(kind)
                with patch.object(ui, "close_plan", return_value=captured):
                    self.assertIsNone(instance.activate({"id": kind + "-close", "kind": "action"}))
                instance.confirm_close.assert_called_once_with(captured)

    def test_confirmation_popup_uses_saved_scope_instead_of_recapturing(self):
        instance = menu("confirm-workspace-close")
        instance.confirm_close = Mock(return_value=True)
        captured = plan("workspace")
        with patch.dict("os.environ", {"HERDR_SHELL_CLOSE_PLAN": json.dumps(captured)}), patch.object(ui, "close_plan") as recapture:
            self.assertEqual(instance.run(), {"kind": "close", "plan": captured})
        recapture.assert_not_called()

    def test_mismatched_confirmation_scope_is_rejected(self):
        instance = menu("confirm-tab-close")
        instance.confirm_close = Mock()
        instance.show_error = Mock()
        with patch.dict("os.environ", {"HERDR_SHELL_CLOSE_PLAN": json.dumps(plan("workspace"))}):
            self.assertIsNone(instance.run())
        instance.confirm_close.assert_not_called()
        instance.show_error.assert_called_once()

    def dialog(self, keys, *, height=18, small=False):
        events = iter(keys)
        rendered = []

        def frame(*args, **kwargs):
            return SimpleNamespace(
                inner=80, height=height, small=small,
                text=lambda y, value, *attrs: rendered.append(value),
                footer=lambda value: None, key=lambda: next(events),
            )

        return patch.object(ui.dialogs, "Frame", side_effect=frame), rendered

    def test_enter_defaults_to_cancel_and_activity_is_visible(self):
        dialog, rendered = self.dialog(["\n"])
        with dialog:
            self.assertFalse(menu().confirm_close(plan("tab")))
        self.assertIn("Closing this tab stops 1 pane.", rendered)
        self.assertIn("  Agent is working", rendered)
        self.assertTrue(any(line.startswith("› Cancel — keep the tab") for line in rendered))

    def test_long_activity_list_can_be_scrolled_before_explicit_close(self):
        captured = plan("workspace")
        captured["count"] = 20
        captured["activity"] = [{"pane_id": str(i), "label": "Pane " + str(i), "reason": "Activity " + str(i)} for i in range(20)]
        dialog, rendered = self.dialog([curses.KEY_END, curses.KEY_DOWN, "\n"], height=16)
        with dialog:
            self.assertTrue(menu().confirm_close(captured))
        self.assertIn("  Activity 19", rendered)

    def test_small_confirmation_cannot_accept_close(self):
        dialog, _ = self.dialog([curses.KEY_DOWN, "\n", "\x1b"], small=True)
        with dialog:
            self.assertFalse(menu().confirm_close(plan()))


class ShortcutHintTests(unittest.TestCase):
    def test_nested_rotation_is_disabled_with_a_readable_reason(self):
        instance = menu()
        instance.store = SimpleNamespace(read=lambda: "")
        instance.context.client = SimpleNamespace(snapshot=lambda: {"workspaces": [], "tabs": [], "panes": []})
        profile = [{"id": "desktop-enabled", "kind": "desktop-toggle", "label": "Omarchy controls", "keys": []}]
        catalog = [{"id": "pane-rotate", "label": "Rotate", "keys": [], "description": "", "category": "Arrange"}]
        reason = "Rotation needs two sibling panes; this split contains a nested pane group."
        with patch.object(ui.desktop, "installed", return_value=True), patch.object(ui.desktop, "enabled", return_value=True), \
             patch.object(ui.desktop, "profile_rows", return_value=profile), patch.object(ui, "bindings", return_value=[]), \
             patch.object(ui, "catalog_rows", return_value=catalog), patch.object(ui, "rotation_unavailable", return_value=reason) as inspect:
            instance.load(preserve=False)
        inspect.assert_called_once_with(instance.context)
        row = next(row for row in instance.items if row["id"] == "pane-rotate")
        self.assertEqual(row["unavailable"], reason)
        with self.assertRaisesRegex(ui.ShellError, "nested pane group"):
            instance.activate(row)

    def test_only_live_active_chords_become_action_hints(self):
        instance = menu()
        instance.store = SimpleNamespace(read=lambda: "")
        instance.context.client = SimpleNamespace(snapshot=lambda: {"workspaces": [], "tabs": [], "panes": []})
        profile = [
            {"id": "desktop-enabled", "kind": "desktop-toggle", "label": "Omarchy controls", "keys": []},
            {"id": "desktop:pane-zoom", "action_id": "pane-zoom", "kind": "desktop-binding", "keys": ["SUPER + ALT + M"], "active": True, "status": "ready"},
            {"id": "desktop:launch-git", "action_id": "launch-git", "kind": "desktop-binding", "keys": ["SUPER + ALT + G"], "active": False, "status": "conflict", "reason": "Reserved by desktop"},
        ]
        catalog = [{"id": "pane-zoom", "label": "Zoom", "keys": [], "description": "", "category": "Arrange"},
                   {"id": "launch-git", "label": "Git", "keys": ["prefix+g"], "description": "", "category": "Launch"}]
        with patch.object(ui.desktop, "installed", return_value=True), patch.object(ui.desktop, "enabled", return_value=True), \
             patch.object(ui.desktop, "profile_rows", return_value=profile), patch.object(ui, "bindings", return_value=[]), \
             patch.object(ui, "catalog_rows", return_value=catalog):
            instance.load(preserve=False)
        rows = {row["id"]: row for row in instance.items}
        self.assertEqual(rows["pane-zoom"]["detail"], "Super+Alt+M")
        self.assertEqual(rows["launch-git"]["detail"], "Ctrl+Space → G")
        self.assertEqual(instance.controls_ready, 1)
        self.assertEqual(instance.controls_conflicts, 1)

    def test_profile_conflict_reason_is_available_in_keybindings(self):
        instance = menu("keybindings")
        instance.store = SimpleNamespace(read=lambda: "")
        instance.context.client = SimpleNamespace(snapshot=lambda: {"workspaces": [], "tabs": [], "panes": []})
        profile = [{"id": "desktop:launch-git", "action_id": "launch-git", "kind": "desktop-binding", "label": "Lazygit", "keys": ["SUPER + ALT + G"], "active": False, "status": "conflict", "reason": "Reserved by desktop"}]
        with patch.object(ui.desktop, "installed", return_value=True), patch.object(ui.desktop, "enabled", return_value=True), \
             patch.object(ui.desktop, "profile_rows", return_value=profile), patch.object(ui, "bindings", return_value=[]):
            instance.load(preserve=False)
        self.assertIn("conflict", instance.items[0]["label"])
        self.assertIn("Reserved by desktop", instance.items[0]["description"])

    def test_desktop_role_rows_are_not_marked_unavailable_profile_actions(self):
        instance = menu("keybindings")
        instance.store = SimpleNamespace(read=lambda: "")
        instance.context.client = SimpleNamespace(snapshot=lambda: {"workspaces": [], "tabs": [], "panes": []})
        profile = [{"id": "desktop:alt-tab", "kind": "desktop-binding", "label": "Switch desktop windows", "keys": ["alt+tab"], "scope": "Desktop"}]
        with patch.object(ui.desktop, "installed", return_value=True), patch.object(ui.desktop, "enabled", return_value=True), \
             patch.object(ui.desktop, "profile_rows", return_value=profile), patch.object(ui, "bindings", return_value=[]):
            instance.load(preserve=False)
        self.assertNotIn("unavailable", instance.items[0]["label"])
        self.assertIn("desktop role", instance.items[0]["description"])


SETTINGS_BASE = '# Personal spacing notes\n[ui]\npane_gaps = true\npane_outer_borders = false\n'
DESKTOP_PROFILE = [{"id": "desktop-enabled", "kind": "desktop-toggle", "label": "Omarchy controls", "keys": []}]
SPACING_PATHS = ("ui.pane_gap_horizontal", "ui.pane_gap_vertical",
                 "ui.tab_padding_horizontal", "ui.tab_padding_vertical")


class PaneSpacingCapabilityTests(unittest.TestCase):
    """Numeric spacing must be advertised by this exact native build, by itself."""

    def reply(self, value):
        with patch.object(runtime, "run_herdr", return_value=value):
            return ui.pane_spacing_supported()

    def test_pane_spacing_needs_its_own_explicit_integer_version(self):
        for value, expected in (
            ('{"pane_spacing":1}', True),
            ('{"pane_spacing":1,"footer":0}', True),
            ('{"pane_spacing":0}', False),
            ('{"pane_spacing":2}', False),
            ('{"pane_spacing":true}', False),
            ('{"pane_spacing":"1"}', False),
            ('{"footer":1}', False),
            ('{}', False),
            ('[]', False),
            ('not json', False),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.reply(value), expected)

    def test_rejected_capability_flag_never_enables_spacing(self):
        with patch.object(runtime, "run_herdr", side_effect=runtime.ShellError("unknown option")):
            self.assertFalse(ui.pane_spacing_supported())


class SettingsPage:
    """Real config file plus a patched desktop profile for menu/settings state tests."""

    page = "settings"
    spacing = False

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.path = root / "config.toml"
        self.path.write_text(SETTINGS_BASE)
        self.reload = Mock(return_value={})
        self.store = config.ConfigStore(self.path, root / "state", Mock(side_effect=tomllib.loads))
        self.instance = menu(self.page)
        self.instance.store = self.store
        self.instance.context.client = SimpleNamespace(
            snapshot=lambda: {"workspaces": [], "tabs": [], "panes": []}, call=self.reload)
        for patcher in (
            patch.object(ui.desktop, "preferences_dir", return_value=root / "preferences"),
            patch.object(config, "defaults", return_value={"prefix": "ctrl+space", "focus_pane_left": "ctrl+alt+left"}),
            patch.object(ui, "bindings", return_value=[]),
            patch.object(ui, "pane_close_confirmation_enabled", return_value=True),
            patch.object(ui, "pane_spacing_supported", return_value=self.spacing),
            patch.object(ui, "rotation_unavailable", return_value=""),
            patch.object(ui.desktop, "installed", return_value=True),
            patch.object(ui.desktop, "enabled", return_value=True),
            patch.object(ui.desktop, "profile_rows", return_value=DESKTOP_PROFILE),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.instance.load(preserve=False)

    def rows(self):
        return {row["id"]: row for row in self.instance.items}


class SpacingSettingsTests(SettingsPage, unittest.TestCase):
    """Native spacing rows share the preview, apply, and reload transaction."""

    spacing = True

    def edit(self, row_id, entries, *, review=True, document=None):
        row = self.rows()[row_id]
        prompts = iter(entries)
        shown = Mock(return_value=review) if document is None else Mock(side_effect=document)
        with patch.object(ui.dialogs, "prompt", side_effect=lambda *args, **kwargs: next(prompts)), \
             patch.object(ui.dialogs, "document", shown), patch.object(ui.Menu, "busy"):
            self.instance.edit(row)

    def test_inherited_spacing_defaults_are_compact_plain_numbers(self):
        for path, expected in zip(SPACING_PATHS, (2, 1, 1, 1)):
            with self.subTest(path=path):
                row = self.rows()[path]
                self.assertEqual(row["default"], expected)
                self.assertEqual(row["value"], expected)
                self.assertEqual(row["detail"], str(expected))
                self.assertEqual(row["source"], "default")

    def test_stock_native_never_exposes_ignored_spacing_fields(self):
        self.path.write_text(SETTINGS_BASE + "pane_gap_horizontal = 8\n")
        with patch.object(ui, "pane_spacing_supported", return_value=False):
            self.instance.load(preserve=False)
        ids = set(self.rows())
        for path in SPACING_PATHS:
            self.assertNotIn(path, ids)

    def test_zero_saves_through_the_preview_and_reload_transaction(self):
        self.edit("ui.pane_gap_horizontal", ["0"])
        ui_section = tomllib.loads(self.path.read_text())["ui"]
        self.assertEqual(ui_section["pane_gap_horizontal"], 0)
        self.assertTrue(ui_section["pane_gaps"])
        self.assertIn("# Personal spacing notes", self.path.read_text())

    def test_both_limits_are_inclusive(self):
        self.edit("ui.tab_padding_vertical", ["65535"])
        self.edit("ui.pane_gap_vertical", ["0"])
        ui_section = tomllib.loads(self.path.read_text())["ui"]
        self.assertEqual(ui_section["tab_padding_vertical"], 65535)
        self.assertEqual(ui_section["pane_gap_vertical"], 0)

    def test_negative_and_overflow_input_never_mutates_the_config(self):
        for value in ("-1", "65536", "99999999999999999999", "four"):
            with self.subTest(value=value):
                before = self.path.read_text()
                self.edit("ui.tab_padding_vertical", [value, ui.dialogs.CANCEL])
                self.assertEqual(self.path.read_text(), before)
        self.reload.assert_not_called()

    def test_sidebar_width_still_requires_positive_integers(self):
        before = self.path.read_text()
        self.edit("ui.sidebar_width", ["0", ui.dialogs.CANCEL])
        self.assertEqual(self.path.read_text(), before)

    def test_reset_restores_the_inherited_default(self):
        self.path.write_text(SETTINGS_BASE + "pane_gap_horizontal = 9\n")
        self.instance.load(preserve=False)
        self.assertEqual(self.rows()["ui.pane_gap_horizontal"]["source"], "user")
        self.edit("ui.pane_gap_horizontal", [ui.dialogs.RESET])
        text = self.path.read_text()
        self.assertNotIn("pane_gap_horizontal", text)
        self.assertTrue(tomllib.loads(text)["ui"]["pane_gaps"])
        self.assertEqual(self.rows()["ui.pane_gap_horizontal"]["source"], "default")
        self.assertEqual(self.rows()["ui.pane_gap_horizontal"]["value"], 2)
        self.assertEqual(self.rows()["ui.pane_gap_horizontal"]["detail"], "2")

    def test_concurrent_external_edit_is_kept_and_the_retry_builds_on_it(self):
        concurrent = SETTINGS_BASE + "copy_on_select = false\n"

        def write_concurrently(*args, **kwargs):
            self.path.write_text(concurrent)
            return True

        self.edit("ui.pane_gap_horizontal", ["7", ui.dialogs.CANCEL], document=write_concurrently)
        self.assertEqual(self.path.read_text(), concurrent)
        self.edit("ui.pane_gap_horizontal", ["7"])
        ui_section = tomllib.loads(self.path.read_text())["ui"]
        self.assertEqual(ui_section["pane_gap_horizontal"], 7)
        self.assertFalse(ui_section["copy_on_select"])


class SettingsCategoryTests(SettingsPage, unittest.TestCase):
    """Settings categories and value groups organize rows and preserve state."""

    def test_category_view_filters_and_search_spans_categories(self):
        instance = self.instance
        instance.settings_section = "behavior"
        behavior = [row["id"] for row in instance.filtered()]
        self.assertEqual(behavior, ["confirm-pane-close", "ui.confirm_close", "tab-name-mode",
                                    "workspace-name-mode", "ui.prompt_new_tab_name", "terminal.new_cwd",
                                    "ui.copy_on_select"])
        instance.set_query("sidebar")
        self.assertEqual({row["id"] for row in instance.filtered()},
                         {"ui.sidebar_width", "ui.sidebar_collapsed_mode"})
        self.assertEqual(instance.settings_section, "behavior")
        instance.set_query("")
        self.assertEqual([row["id"] for row in instance.filtered()], behavior)

    def test_naming_choices_persist_without_touching_native_config(self):
        instance = self.instance
        self.assertEqual(self.rows()["tab-name-mode"]["detail"], "Agent task")
        self.assertEqual(self.rows()["workspace-name-mode"]["detail"], "Project")
        with patch.object(ui.dialogs, "choose", return_value="numbered"):
            instance.activate(self.rows()["tab-name-mode"])
        self.assertEqual(ui.naming.preferences()["tab_mode"], "numbered")
        self.assertEqual(self.rows()["tab-name-mode"]["detail"], "Herdr default")
        with patch.object(ui.dialogs, "choose", return_value="folder"):
            instance.activate(self.rows()["workspace-name-mode"])
        self.assertEqual(ui.naming.preferences()["workspace_mode"], "folder")
        self.assertEqual(self.rows()["workspace-name-mode"]["detail"], "Working directory")
        self.assertEqual(self.path.read_text(), SETTINGS_BASE)
        self.reload.assert_not_called()

    def test_clearing_search_restores_the_original_category_selection(self):
        instance = self.instance
        instance.settings_section = "behavior"
        instance.load(preserve=False)
        rows = instance.filtered()
        instance.selected = next(i for i, row in enumerate(rows) if row["id"] == "ui.copy_on_select")
        instance.set_query("delivery")
        self.assertEqual(instance.selected, 0)
        instance.change_settings_section(1)
        self.assertEqual(instance.settings_section, "notifications")
        instance.set_query("")
        self.assertEqual(instance.settings_section, "behavior")
        self.assertEqual(instance.filtered()[instance.selected]["id"], "ui.copy_on_select")

    def test_refresh_preserves_category_and_selected_row(self):
        instance = self.instance
        instance.settings_section = "notifications"
        instance.load(preserve=False)
        rows = instance.filtered()
        instance.selected = next(i for i, row in enumerate(rows) if row["id"] == "ui.sound.enabled")
        instance.load()
        self.assertEqual(instance.settings_section, "notifications")
        self.assertEqual(instance.filtered()[instance.selected]["id"], "ui.sound.enabled")

    def test_back_from_keybindings_restores_category_and_selection(self):
        instance = self.instance
        instance.settings_section = "behavior"
        instance.load(preserve=False)
        rows = instance.filtered()
        instance.selected = next(i for i, row in enumerate(rows) if row["id"] == "ui.confirm_close")
        instance.open_page("keybindings")
        self.assertEqual(instance.page, "keybindings")
        instance.back()
        self.assertEqual(instance.page, "settings")
        self.assertEqual(instance.settings_section, "behavior")
        self.assertEqual(instance.filtered()[instance.selected]["id"], "ui.confirm_close")

    def test_cycling_wraps_categories_in_both_directions_in_place(self):
        instance = self.instance
        instance.settings_section = "layout"
        instance.change_settings_section(-1)
        self.assertEqual(instance.settings_section, "controls")
        instance.change_settings_section(1)
        self.assertEqual(instance.settings_section, "layout")
        instance.change_settings_section(1)
        self.assertEqual(instance.settings_section, "appearance")

    def test_cycling_during_search_keeps_query_and_results(self):
        instance = self.instance
        instance.settings_section = "layout"
        instance.set_query("sound")
        instance.change_settings_section(1)
        self.assertEqual(instance.settings_section, "appearance")
        self.assertEqual(instance.query, "sound")
        self.assertIn("ui.sound.enabled", [row["id"] for row in instance.filtered()])


class MenuOrganizationTests(SettingsPage, unittest.TestCase):
    """Main-menu sections, Home coverage, and settings navigation keep state."""

    page = "menu"

    def test_search_reaches_actions_across_sections_including_groups(self):
        instance = self.instance
        instance.section = "panes"
        instance.set_query("picker")
        sections = {row["section"] for row in instance.filtered()}
        self.assertLessEqual({"panes", "tabs", "workspaces"}, sections)
        instance.set_query("arrange")
        ids = [row["id"] for row in instance.filtered()]
        self.assertIn("pane-zoom", ids)
        self.assertIn("pane-workspace-picker", ids)

    def test_settings_cycling_keeps_parent_history_for_back(self):
        instance = self.instance
        instance.section = "settings"
        instance.open_page("settings")
        self.assertEqual(instance.page, "settings")
        parent = instance.history[-1]
        self.assertEqual(parent["page"], "menu")
        instance.change_settings_section(1)
        self.assertEqual(instance.settings_section, "appearance")
        self.assertEqual(instance.history[-1], parent)
        instance.back()
        self.assertEqual(instance.page, "menu")
        self.assertEqual(instance.section, "settings")


if __name__ == "__main__":
    unittest.main()
