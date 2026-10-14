"""Consumer-visible shortcut and grouped-menu boundary regressions."""
import curses
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from herdr_shell import actions, desktop, menu as ui
from herdr_shell.interface import MenuView, SECTIONS, SETTINGS_SECTIONS, activation_label, cell_width


class Screen:
    def __init__(self, height=48, width=80):
        self.height, self.width = height, width
        self.lines = []
        self.selected_lines = []
        self.occupied = set()

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.lines = []
        self.selected_lines = []
        self.occupied = set()

    def addstr(self, y, x, value, *attributes):
        if not 0 <= y < self.height or not 0 <= x < self.width or x + cell_width(value) > self.width:
            raise AssertionError("Drawing outside the terminal")
        cells = {(y, column) for column in range(x, x + cell_width(value))}
        if cells & self.occupied:
            raise AssertionError("Drawing over existing content")
        self.occupied.update(cells)
        self.lines.append((y, x, value))
        if attributes and attributes[0] == curses.A_REVERSE:
            self.selected_lines.append((y, x, value))

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
    instance.settings_section = "layout"
    instance.query = instance.notice = ""
    instance.selected = instance.scroll = 0
    instance.search_origin = None
    instance.show_unassigned = False
    instance.history = []
    instance.items = []
    instance.accent = instance.selection = instance.good = instance.brand = instance.error = 0
    instance.selection = curses.A_REVERSE
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
                self.assertTrue(git["shortcut_inactive"])
                self.assertFalse(git.get("unavailable"))
                self.assertEqual(instance.activate(git), {"kind": "action", "id": "launch-git", "yes": False})

    def test_every_direct_chord_remains_discoverable_at_minimum_size(self):
        for enabled in (False, True):
            instance = home(enabled=enabled, height=16, width=46)
            rows = instance.filtered()
            for index, row in enumerate(rows):
                with self.subTest(enabled=enabled, action=row["id"]):
                    instance.selected = index
                    MenuView(instance).render(rows)
                    self.assertEqual(len(instance.screen.selected_lines), 1)
                    y, x, text = instance.screen.selected_lines[0]
                    self.assertIn(row["detail"], text)
                    self.assertEqual(text[1] == "!", not enabled)
                    self.assertLess(y, instance.screen.height - 5)
                    self.assertIn((y - 1, x, row["group"]), instance.screen.lines)
                    self.assertIn("Enter " + activation_label(row), instance.screen.text())

    def test_unavailable_action_keeps_its_chord_but_offers_details(self):
        instance = home(height=16, width=46)
        rows = instance.filtered()
        instance.selected = next(i for i, row in enumerate(rows) if row["id"] == "launch-git")
        rows[instance.selected] = dict(rows[instance.selected], unavailable="Install Lazygit to run this action.")
        MenuView(instance).render(rows)
        _, _, text = instance.screen.selected_lines[0]
        self.assertIn(rows[instance.selected]["detail"], text)
        self.assertEqual(text[:2], "›!")
        self.assertIn("Enter Details", instance.screen.text())

    def test_end_and_page_keys_keep_actual_actions_visible_after_resize(self):
        instance = home(height=26, width=86)
        steps = iter([
            (curses.KEY_END, None),
            (curses.KEY_RESIZE, (16, 46)),
            (curses.KEY_PPAGE, None),
            (curses.KEY_NPAGE, None),
            (curses.KEY_HOME, None),
            (curses.KEY_NPAGE, None),
            (curses.KEY_RESIZE, (48, 142)),
            (curses.KEY_END, None),
            ("\x03", None),
        ])
        visited = []

        def key():
            rows = instance.filtered()
            self.assertEqual(len(instance.screen.selected_lines), 1)
            y, _, text = instance.screen.selected_lines[0]
            self.assertIn(rows[instance.selected]["detail"], text)
            self.assertLess(y, instance.screen.height - 5)
            visited.append(instance.selected)
            value, size = next(steps)
            if size:
                instance.screen.height, instance.screen.width = size
            return value

        instance.screen.get_wch = key
        with patch("herdr_shell.onboarding.needs_welcome", return_value=False):
            self.assertIsNone(instance.run())
        last = len(instance.filtered()) - 1
        self.assertEqual(visited[:3], [0, last, last])
        self.assertLess(visited[3], last)
        self.assertEqual(visited[4], last)
        self.assertEqual(visited[5], 0)
        self.assertGreater(visited[6], 0)
        self.assertEqual(visited[7], visited[6])
        self.assertEqual(visited[-1], last)

    def test_f1_explains_reserved_full_chord_and_menu_action_still_works(self):
        instance = home(conflicts=("launch-git",))
        instance.selected = next(i for i, row in enumerate(instance.filtered()) if row["id"] == "launch-git")
        with patch.object(ui.dialogs, "document") as document:
            instance.help()
        lines = document.call_args.args[2]
        self.assertIn("Super+Alt+V", lines)
        self.assertTrue(any("Reserved by desktop" in line for line in lines))

    def test_menu_entry_closes_instead_of_opening_another_popup(self):
        instance = home()
        instance.selected = next(i for i, row in enumerate(instance.filtered()) if row["id"] == "menu")
        keys = iter(["\n"])
        instance.screen.get_wch = lambda: next(keys)
        instance.context.client.call = Mock()
        with patch("herdr_shell.onboarding.needs_welcome", return_value=False):
            self.assertIsNone(instance.run())
        instance.context.client.call.assert_not_called()


class GroupedMenuRenderingTests(unittest.TestCase):
    def test_every_catalog_action_is_selectable_without_heading_or_footer_overlap(self):
        instance = home(height=16, width=46)
        instance.section = "all"
        rows = instance.filtered()
        for index, row in enumerate(rows):
            with self.subTest(action=row["id"]):
                instance.selected = index
                MenuView(instance).render(rows)
                self.assertEqual(len(instance.screen.selected_lines), 1)
                y, x, text = instance.screen.selected_lines[0]
                self.assertTrue(text[2:].startswith(row["label"][:6]))
                self.assertLess(y, instance.screen.height - 5)
                self.assertIn((y - 1, x, row["group"]), instance.screen.lines)

    def test_main_and_settings_rails_fit_across_compact_and_branded_heights(self):
        instance = home()
        for page, categories in (("menu", SECTIONS), ("settings", SETTINGS_SECTIONS)):
            instance.page = page
            if page == "settings":
                instance.settings_section = categories[-1][0]
            else:
                instance.section = categories[-1][0]
            rows = [{"id": "test", "label": "Selected action", "group": "Example group"}]
            for height in range(26, 42):
                for width in (86, 124, 160):
                    with self.subTest(page=page, height=height, width=width):
                        instance.screen.height, instance.screen.width = height, width
                        view = MenuView(instance)
                        view.render(rows)
                        rail = [(y, value) for y, x, value in instance.screen.lines
                                if x == view.offset + 3 and value[2:].rstrip() in {s[1] for s in categories}]
                        self.assertEqual(len(rail), len(categories))
                        self.assertTrue(all(y < height - 5 for y, _ in rail))
                        self.assertEqual([text[2:].rstrip() for _, text in rail if text.startswith("›")],
                                         [categories[-1][1]])
                        self.assertEqual(len(instance.screen.selected_lines), 1)

    def test_home_and_other_main_sections_share_header_and_list_geometry(self):
        instance = home()
        for height, width in ((16, 46), (26, 86), (32, 124), (48, 160)):
            instance.screen.height, instance.screen.width = height, width
            layouts = []
            for section in ("home", "panes", "tabs", "agents", "all"):
                instance.section = section
                instance.selected = instance.scroll = 0
                MenuView(instance).render(instance.filtered())
                header = [(y, x, value) for y, x, value in instance.screen.lines
                          if value in ("HERDR", "OMARCHY") or value.startswith("/  ") or
                          (value and set(value) == {"─"})]
                layouts.append((header, instance.screen.selected_lines[0][:2]))
            with self.subTest(height=height, width=width):
                self.assertTrue(all(layout == layouts[0] for layout in layouts))

    def test_grouped_settings_and_ungrouped_rows_survive_minimum_size_resize(self):
        instance = home(height=48, width=142)
        instance.page = "settings"
        instance.settings_section = "controls"
        rows = [{"id": str(index), "label": "Setting " + str(index), "kind": "setting",
                 "group": "Group " + str(index // 2)} for index in range(15)]
        rows.append({"id": "unknown", "label": "Additional setting", "kind": "setting"})
        instance.selected = len(rows) - 1
        for height, width in ((48, 142), (16, 46), (26, 86), (16, 46), (48, 142)):
            with self.subTest(height=height, width=width):
                instance.screen.height, instance.screen.width = height, width
                MenuView(instance).render(rows)
                self.assertEqual(instance.selected, len(rows) - 1)
                self.assertEqual(len(instance.screen.selected_lines), 1)
                self.assertLess(instance.screen.selected_lines[0][0], height - 5)
                self.assertIn("Controls", instance.screen.text())
                self.assertIn("Tab", instance.screen.text())


if __name__ == "__main__":
    unittest.main()
