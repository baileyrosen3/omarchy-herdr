"""Regression coverage for the direct-shortcut reference on Home."""
import curses
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from herdr_shell import actions, desktop, menu as ui
from herdr_shell.interface import HOME_GROUPS, MenuView


class Screen:
    def __init__(self, height=48, width=80):
        self.height, self.width = height, width
        self.lines = []

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.lines = []

    def addstr(self, y, x, value, *attributes):
        if not 0 <= y < self.height or not 0 <= x < self.width:
            raise AssertionError("Drawing outside the terminal")
        self.lines.append((y, x, value))

    def refresh(self):
        pass

    def text(self):
        return "\n".join(value for _, _, value in self.lines)


def home(*, enabled=True, conflicts=(), height=48, width=80):
    instance = ui.Menu.__new__(ui.Menu)
    instance.screen = Screen(height, width)
    instance.context = SimpleNamespace(pane="w1:p1", workspace="w1", tab="t1", cwd="/project",
                                       client=SimpleNamespace(snapshot=lambda: {"workspaces": [], "tabs": [], "panes": []}))
    instance.store = SimpleNamespace(read=lambda: "")
    instance.page, instance.section = "menu", "home"
    instance.query = instance.notice = ""
    instance.selected = instance.scroll = 0
    instance.search_origin = None
    instance.show_unassigned = False
    instance.history = []
    instance.items = []
    instance.accent = instance.selection = instance.good = instance.brand = instance.error = 0
    profile = [{"id": "desktop-enabled", "kind": "desktop-toggle", "label": "Super+Alt controls", "keys": []}]
    for key, action, label in desktop.MAPPINGS:
        conflict = action in conflicts
        profile.append({"id": "desktop:" + action, "action_id": action, "kind": "desktop-binding",
                        "keys": [key], "label": label, "detail": desktop.pretty_key(key),
                        "status": "conflict" if conflict else "ready" if enabled else "disabled",
                        "active": enabled and not conflict, "reason": "Reserved by desktop" if conflict else ""})
    with patch.object(ui.desktop, "installed", return_value=True), \
         patch.object(ui.desktop, "enabled", return_value=enabled), \
         patch.object(ui.desktop, "profile_rows", return_value=profile), \
         patch.object(ui, "bindings", return_value=[]), \
         patch.object(ui, "rotation_unavailable", return_value=""), \
         patch.object(actions, "available", return_value=""):
        instance.load(preserve=False)
    return instance


class HomeShortcutsTests(unittest.TestCase):
    def test_home_includes_each_direct_action_once_including_menu(self):
        instance = home()
        rows = instance.filtered()
        expected = {action for _, action, _ in desktop.MAPPINGS}
        self.assertEqual({row["id"] for row in rows}, expected)
        self.assertEqual(len(rows), len(desktop.MAPPINGS))
        self.assertEqual(sum(row["id"] == "menu" for row in rows), 1)

    def test_inactive_keys_remain_visible_without_disabling_menu_action(self):
        for enabled, conflicts in ((False, ()), (True, ("launch-git",))):
            with self.subTest(enabled=enabled, conflicts=conflicts):
                instance = home(enabled=enabled, conflicts=conflicts)
                rows = {row["id"]: row for row in instance.filtered()}
                for key, action, _ in desktop.MAPPINGS:
                    self.assertEqual(rows[action]["detail"], desktop.pretty_key(key))
                    self.assertEqual(rows[action]["keys"], [desktop.pretty_key(key)])
                git = rows["launch-git"]
                self.assertFalse(git["shortcut_active"])
                self.assertFalse(git.get("unavailable"))
                self.assertIn("Enter can still run", git["description"])
                self.assertEqual(instance.activate(git), {"kind": "action", "id": "launch-git", "yes": False})

    def test_tall_home_shows_all_actions_once_with_aligned_key_column(self):
        instance = home(width=54)
        rows = instance.filtered()
        MenuView(instance).render(rows)
        lines = instance.screen.lines
        columns = []
        for row in rows:
            matching = [(x, value) for _, x, value in lines if value[2:].startswith(row["label"]) and
                        value.rstrip().endswith(row["shortcut_suffix"])]
            self.assertEqual(len(matching), 1, row["id"])
            x, value = matching[0]
            columns.append(x + value.rfind(row["shortcut_suffix"]))
        self.assertEqual(len(set(columns)), 1)
        for heading, _ in HOME_GROUPS:
            self.assertEqual(sum(value == heading.upper() for _, _, value in lines), 1)
        self.assertIn("Hold Super+Alt", instance.screen.text())
        self.assertNotIn("Open your default Omarchy agent", instance.screen.text())
        self.assertEqual(instance.visible_rows, len(rows))
        self.assertEqual(instance.scroll, 0)

    def test_minimum_width_retains_full_shift_page_suffixes(self):
        instance = home(width=46)
        MenuView(instance).render(instance.filtered())
        text = instance.screen.text()
        self.assertIn("Shift+PageUp", text)
        self.assertIn("Shift+PageDown", text)
        suffix_lines = [value for _, _, value in instance.screen.lines if "Shift+Page" in value]
        self.assertEqual(len(suffix_lines), 2)
        self.assertFalse(any("…" in value[value.find("Shift+Page"):] for value in suffix_lines))

    def test_inactive_shortcut_marker_is_visible_beside_retained_key(self):
        for enabled, conflicts, expected in ((False, (), 26), (True, ("launch-git",), 1)):
            with self.subTest(enabled=enabled, conflicts=conflicts):
                instance = home(enabled=enabled, conflicts=conflicts, width=54)
                MenuView(instance).render(instance.filtered())
                marked = [value for _, _, value in instance.screen.lines if value[:2] in (" !", "›!")]
                self.assertEqual(len(marked), expected)
                self.assertTrue(any("Lazygit" in value and value.rstrip().endswith("V") for value in marked))

    def test_end_scrolls_to_last_action_and_repeats_its_group_heading(self):
        instance = home(height=16, width=46)
        keys = iter([curses.KEY_END, "\x1b"])
        instance.screen.get_wch = lambda: next(keys)
        with patch("herdr_shell.onboarding.needs_welcome", return_value=False):
            self.assertIsNone(instance.run())
        self.assertEqual(instance.selected, len(instance.filtered()) - 1)
        self.assertGreater(instance.scroll, 0)
        self.assertIn("MENU", instance.screen.text())
        self.assertIn("Close menu", instance.screen.text())
        self.assertIn("26/26", instance.screen.text())

    def test_f1_explains_reserved_full_chord_and_menu_action_still_works(self):
        instance = home(conflicts=("launch-git",))
        instance.selected = next(i for i, row in enumerate(instance.filtered()) if row["id"] == "launch-git")
        with patch.object(ui.dialogs, "document") as document:
            instance.help()
        lines = document.call_args.args[2]
        self.assertIn("Super+Alt+V", lines)
        self.assertTrue(any("Reserved by desktop" in line for line in lines))
        self.assertTrue(any("Enter can still run" in line for line in lines))

    def test_menu_entry_closes_instead_of_opening_another_popup(self):
        instance = home()
        instance.selected = next(i for i, row in enumerate(instance.filtered()) if row["id"] == "menu")
        keys = iter(["\n"])
        instance.screen.get_wch = lambda: next(keys)
        instance.context.client.call = Mock()
        with patch("herdr_shell.onboarding.needs_welcome", return_value=False):
            self.assertIsNone(instance.run())
        instance.context.client.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
