import curses
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from herdr_shell import menu as ui


def menu(page="menu"):
    instance = ui.Menu.__new__(ui.Menu)
    instance.page = page
    instance.context = SimpleNamespace(pane="w1:p1", workspace="w1", tab="t1", cwd="/project")
    instance.notice = ""
    instance.query = ""
    instance.selected = 0
    instance.scroll = 0
    instance.section = "home"
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


if __name__ == "__main__":
    unittest.main()
