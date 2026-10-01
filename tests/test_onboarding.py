"""First-use persistence and guide navigation through real dialog frames."""
import curses
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from herdr_shell import onboarding
from herdr_shell.interface import cell_width


class Screen:
    def __init__(self, keys, sizes=((16, 46),)):
        self.keys = iter(keys)
        self.sizes = iter(sizes)
        self.size = sizes[-1]
        self.frames = []

    def getmaxyx(self):
        self.size = next(self.sizes, self.size)
        return self.size

    def erase(self):
        self.frames.append([])

    def refresh(self):
        pass

    def get_wch(self):
        key = next(self.keys)
        if isinstance(key, Exception):
            raise key
        return key


class Menu:
    accent = selection = error = 0

    def __init__(self, keys, sizes=((16, 46),)):
        self.screen = Screen(keys, sizes)

    def write(self, y, x, value, attr=0):
        height, width = self.screen.size
        if not (0 <= y < height and 0 <= x < width and x + cell_width(value) <= width):
            raise AssertionError(f"Out-of-bounds dialog output at {(y, x)} in {(height, width)}: {value!r}")
        self.screen.frames[-1].append((y, value))


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        patcher = patch.object(onboarding, "state_path", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_corrupt_or_old_marker_needs_welcome(self):
        self.assertTrue(onboarding.needs_welcome())
        self.state.mkdir()
        path = self.state / "onboarding.json"
        for value in ("not JSON", "[]", "null", "{}", '{"welcome_version": 0}',
                      '{"welcome_version": true}', '{"welcome_version": 1.0}'):
            with self.subTest(value=value):
                path.write_text(value)
                self.assertTrue(onboarding.needs_welcome())

    def test_marker_is_versioned_private_and_replaced_atomically(self):
        onboarding.mark_welcomed()
        path = self.state / "onboarding.json"
        self.assertEqual(json.loads(path.read_text()), {"welcome_version": onboarding.WELCOME_VERSION})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertFalse(onboarding.needs_welcome())
        path.chmod(0o666)
        path.write_text("old contents")
        onboarding.mark_welcomed()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["onboarding.json"])

    def test_welcome_choices_and_escape_persist_only_after_display(self):
        for keys, expected in ((["\n"], "walkthrough"), ([curses.KEY_DOWN, "\n"], "hands-on"),
                               ([curses.KEY_END, "\n"], "hands-on"), (["\x1b"], None)):
            with self.subTest(expected=expected):
                (self.state / "onboarding.json").unlink(missing_ok=True)
                instance = Menu(keys)
                self.assertEqual(onboarding.welcome(instance), expected)
                self.assertFalse(onboarding.needs_welcome())
                first = "\n".join(text for _, text in instance.screen.frames[0])
                self.assertIn("Walkthrough", first)
                self.assertIn("Hands-on walkthrough", first)
                self.assertNotIn("Speed Run", first)
                self.assertNotIn("Exit welcome", first)

    def test_completed_welcome_offers_all_three_and_explicit_exit(self):
        for keys, expected in ((["\n"], "walkthrough"), ([curses.KEY_DOWN, "\n"], "hands-on"),
                               ([curses.KEY_DOWN, curses.KEY_DOWN, "\n"], "game"),
                               ([curses.KEY_END, "\n"], None)):
            with self.subTest(expected=expected):
                onboarding.mark_learning_completed()
                instance = Menu(keys)
                self.assertEqual(onboarding.welcome(instance), expected)
                first = "\n".join(text for _, text in instance.screen.frames[0])
                self.assertIn("Speed Run", first)
                self.assertIn("Exit welcome", first)
                self.assertTrue(onboarding.has_learning_completed())

    def test_completion_and_welcome_preserve_each_others_marker_fields(self):
        onboarding.mark_welcomed()
        path = self.state / "onboarding.json"
        value = json.loads(path.read_text())
        value["other_preference"] = {"kept": True}
        path.write_text(json.dumps(value))
        onboarding.mark_learning_completed()
        onboarding.mark_welcomed()
        self.assertEqual(json.loads(path.read_text()), {"welcome_version": onboarding.WELCOME_VERSION,
                         "learning_completed": True, "other_preference": {"kept": True}})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(onboarding.has_learning_completed())

    def test_completion_accepts_only_boolean_true(self):
        self.state.mkdir()
        for value in ("not JSON", "null", "[]", "{}", '{"learning_completed": 1}',
                      '{"learning_completed": "true"}', '{"learning_completed": false}'):
            with self.subTest(value=value):
                (self.state / "onboarding.json").write_text(value)
                self.assertFalse(onboarding.has_learning_completed())

    def test_small_welcome_ignores_enter_and_does_not_mark_on_escape(self):
        instance = Menu(["\n", curses.KEY_DOWN, "\x1b"], ((15, 45),))
        self.assertIsNone(onboarding.welcome(instance))
        self.assertTrue(onboarding.needs_welcome())
        self.assertFalse(self.state.exists())

    def test_resize_preserves_selection_and_waits_for_a_usable_frame(self):
        instance = Menu([curses.KEY_DOWN, "\n", curses.KEY_RESIZE, "\n"],
                        ((16, 46), (15, 45), (16, 46), (16, 46)))
        self.assertEqual(onboarding.welcome(instance), "hands-on")
        self.assertFalse(onboarding.needs_welcome())

    def test_input_or_render_failure_leaves_welcome_unmarked(self):
        with self.assertRaisesRegex(OSError, "terminal gone"):
            onboarding.welcome(Menu([OSError("terminal gone")]))
        self.assertFalse(self.state.exists())
        with patch.object(Menu, "write", side_effect=OSError("cannot render")):
            with self.assertRaisesRegex(OSError, "cannot render"):
                onboarding.welcome(Menu(["\n"]))
        self.assertFalse(self.state.exists())

    def test_readable_welcome_then_small_escape_counts_as_dismissal(self):
        instance = Menu([curses.KEY_RESIZE, "\x1b"], ((16, 46), (15, 45)))
        self.assertIsNone(onboarding.welcome(instance))
        self.assertFalse(onboarding.needs_welcome())

    def test_marker_permission_failure_does_not_block_choice_or_dismissal(self):
        for key, expected in (("\n", "walkthrough"), ("\x1b", None)):
            with self.subTest(key=key):
                instance = Menu([key])
                with patch.object(onboarding, "mark_welcomed", side_effect=PermissionError("state is read-only")):
                    self.assertEqual(onboarding.welcome(instance), expected)
                self.assertIn("may appear again", instance.notice)
                self.assertTrue(onboarding.needs_welcome())

    def test_guide_completion_unlocks_all_activities_and_exit(self):
        next_pages = ["\n"] * len(onboarding.WALKTHROUGH_PAGES)
        for ending, expected in ((["\n"], "walkthrough"), ([curses.KEY_DOWN, "\n"], "hands-on"),
                                 ([curses.KEY_DOWN, curses.KEY_DOWN, "\n"], "game"),
                                 ([curses.KEY_END, "\n"], None), (["\x1b"], None)):
            with self.subTest(expected=expected):
                instance = Menu([*next_pages, *ending])
                self.assertEqual(onboarding.walkthrough(instance), expected)
                self.assertTrue(any("Walkthrough complete" in text for frame in instance.screen.frames for _, text in frame))
                self.assertTrue(onboarding.has_learning_completed())

    def test_leaving_before_last_page_does_not_unlock_speed_run_welcome(self):
        instance = Menu(["\n", "\x1b"])
        self.assertIsNone(onboarding.walkthrough(instance))
        self.assertFalse(onboarding.has_learning_completed())
        self.assertFalse(self.state.exists())

    def test_completed_choice_still_available_when_persistence_fails(self):
        instance = Menu([*["\n"] * len(onboarding.WALKTHROUGH_PAGES), curses.KEY_DOWN, curses.KEY_DOWN, "\n"])
        with patch.object(onboarding, "mark_learning_completed", side_effect=PermissionError("state read-only")):
            self.assertEqual(onboarding.walkthrough(instance), "game")
        self.assertIn("Cannot save learning completion", instance.notice)
        self.assertFalse(onboarding.has_learning_completed())

    def test_guide_back_and_scroll_preserve_position_at_minimum_size(self):
        instance = Menu([curses.KEY_NPAGE, curses.KEY_RIGHT, curses.KEY_LEFT,
                         curses.KEY_END, curses.KEY_HOME, "\x1b"])
        self.assertIsNone(onboarding.walkthrough(instance))
        frames = instance.screen.frames
        # Returning to a page keeps its scroll position; Home recovers its top.
        self.assertEqual(frames[1], frames[3])
        self.assertEqual(frames[0], frames[5])
        self.assertTrue(any("Lines" in text for _, text in frames[4]))

    def test_guide_keeps_every_page_scrollable_through_resize(self):
        keys = []
        for _ in onboarding.WALKTHROUGH_PAGES:
            keys.extend((curses.KEY_END, curses.KEY_RIGHT))
        keys.extend((curses.KEY_END, "\n"))
        instance = Menu(keys, ((16, 46), (32, 100), (16, 46)))
        self.assertIsNone(onboarding.walkthrough(instance))
        titles = {text for frame in instance.screen.frames for _, text in frame}
        for title, _ in onboarding.WALKTHROUGH_PAGES:
            self.assertIn(title, titles)

    def test_small_guide_cannot_advance_or_start_game(self):
        instance = Menu([curses.KEY_RIGHT, "\n", curses.KEY_END, "\x1b"], ((15, 45),))
        self.assertIsNone(onboarding.walkthrough(instance))
        self.assertFalse(self.state.exists())

    def test_guide_teaches_key_family_and_work_safety(self):
        text = "\n".join(line for _, lines in onboarding.WALKTHROUGH_PAGES for line in lines)
        for phrase in ("Super+Alt+U / D / L / R", "Super+Alt+Shift+U / D / L / R", "Super+Alt+P",
                       "Shift+P", "Super+Alt+J", "Super+Alt+Z", "Super+Alt+T", "Super+Alt+W",
                       "Super+Alt+Shift+T", "Super+Alt+Shift+W", "Super+Alt+Page Up / Page Down",
                       "Super+Alt+Shift+Page Up / Page Down", "Super+Alt+A", "Super+Alt+V",
                       "Super+Alt+Q", "Shift+Q", "Super+Alt+X", "M opens the menu",
                       "Ctrl+Alt+arrows", "Ctrl+Alt+Shift+arrows", "Super+Ctrl+Enter", "Super+Space",
                       "blocked, then done, working, and idle", "Running or unknown work asks first",
                       "Cancel selected", "F1", "F2", "Ctrl+O", "preview", "Ctrl+Z", "terminal colors",
                       "Herdr plugin", "Hyprland Lua bridge", "no plain Alt"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
