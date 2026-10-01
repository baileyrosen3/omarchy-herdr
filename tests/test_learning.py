"""Welcome integration must preserve normal menus and installation outcomes."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from herdr_shell import cli, menu, onboarding
from herdr_shell.runtime import ShellError


def instance(keys, page="menu"):
    events = iter(keys)
    result = menu.Menu.__new__(menu.Menu)
    result.page = page
    result.screen = SimpleNamespace(getmaxyx=lambda: (32, 100), erase=lambda: None,
                                    refresh=lambda: None, get_wch=lambda: next(events))
    result.accent = result.selection = result.error = 0
    result.notice = ""
    result.context = SimpleNamespace(pane="test-pane", cwd="/test")
    result.write = Mock()
    result.draw = Mock()
    result.load = Mock()
    result.filtered = Mock(return_value=[])
    return result


class LearningMenuTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        patcher = patch.object(onboarding, "state_path", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_menu_shows_welcome_once_then_opens_normal_menu(self):
        first = instance(["\x1b"])
        self.assertIsNone(first.run())
        self.assertFalse(onboarding.needs_welcome())
        first.draw.assert_not_called()
        returning = instance(["\x03"])
        with patch.object(onboarding, "welcome") as welcome:
            self.assertIsNone(returning.run())
        welcome.assert_not_called()
        returning.draw.assert_called_once()
        returning.load.assert_not_called()

    def test_open_menu_choice_reloads_menu_in_same_popup(self):
        first = instance(["\t", "\t", "\n", "\x03"])
        self.assertIsNone(first.run())
        first.load.assert_called_once_with(preserve=False)
        first.draw.assert_called_once()
        self.assertEqual(first.page, "menu")
        self.assertFalse(onboarding.needs_welcome())

    def test_game_choice_returns_job_after_successful_welcome(self):
        first = instance(["\t", "\n"])
        self.assertEqual(first.run(), {"kind": "action", "id": "learn-game", "yes": False})
        first.draw.assert_not_called()
        self.assertFalse(onboarding.needs_welcome())

    def test_explicit_welcome_can_be_reopened_after_marker(self):
        onboarding.mark_welcomed()
        with patch.object(onboarding, "welcome", return_value="game") as welcome:
            first = instance([], "welcome")
            self.assertEqual(first.run(), {"kind": "action", "id": "learn-game", "yes": False})
        welcome.assert_called_once_with(first)

    def test_explicit_walkthrough_does_not_replay_welcome_or_create_marker(self):
        first = instance([*["\n"] * len(onboarding.WALKTHROUGH_PAGES), "\t", "\n", "\x03"], "walkthrough")
        with patch.object(onboarding, "welcome") as welcome:
            self.assertIsNone(first.run())
        welcome.assert_not_called()
        first.load.assert_called_once_with(preserve=False)
        first.draw.assert_called_once()
        self.assertTrue(onboarding.needs_welcome())

    def test_confirmation_is_never_interrupted_by_first_use_welcome(self):
        first = instance([], "confirm-pane-close")
        first.confirm_close = Mock(return_value=False)
        first.show_error = Mock()
        plan = {"action_id": "pane-close", "needs_confirmation": True}
        with patch.dict("os.environ", HERDR_SHELL_CLOSE_PLAN=json.dumps(plan)), \
             patch.object(onboarding, "needs_welcome") as check, patch.object(onboarding, "welcome") as welcome:
            self.assertIsNone(first.run())
        check.assert_not_called()
        welcome.assert_not_called()
        first.confirm_close.assert_called_once_with(plan)

    def test_catalog_welcome_and_walkthrough_actions_use_learning_flow(self):
        first = instance([])
        first.learning = Mock(return_value={"kind": "action", "id": "learn-game", "yes": False})
        for action in ("welcome", "walkthrough"):
            with self.subTest(action=action):
                result = first.activate({"id": action, "kind": "action"})
                self.assertEqual(result["id"], "learn-game")
                first.learning.assert_called_with(action)

    def test_game_menu_skips_welcome_and_never_dispatches_a_selected_action(self):
        first = instance(["\x03"])
        first.practice = True
        first.help = Mock()
        with patch.object(onboarding, "welcome") as welcome:
            self.assertIsNone(first.run())
        welcome.assert_not_called()
        for row in ({"kind": "action", "id": "workspace-close"}, {"kind": "setting", "id": "theme.name"},
                    {"kind": "desktop-toggle", "id": "desktop-enabled"}):
            self.assertIsNone(first.activate(row))
        self.assertEqual(first.help.call_count, 3)

    def test_game_menu_blocks_undo_hotkeys(self):
        import curses
        first = instance([curses.KEY_F4, "\x1a", "\x03"])
        first.practice = True
        first.undo = Mock()
        with patch.object(menu, 'MenuView') as view:
            view.return_value.too_small = False
            self.assertIsNone(first.run())
        first.undo.assert_not_called()


class LearningInstallTests(unittest.TestCase):
    def dispatch(self, *, apply=True, welcome_needed=True, failure=None):
        args = cli.parser().parse_args(["install", *(["--apply"] if apply else [])])
        result = {"installed": "blr.herdr-shell"} if apply else {"preview": True}
        context = object()
        with patch.object(cli, "ConfigStore"), patch.object(cli, "install", return_value=result) as install, \
             patch.object(onboarding, "needs_welcome", return_value=welcome_needed) as check, \
             patch.object(cli, "resolve_context", return_value=context) as resolve, \
             patch.object(cli, "open_ui", return_value={"opened": True}) as open_ui:
            if failure:
                resolve.side_effect = failure
            returned = cli.dispatch(args)
        return returned, install, check, resolve, open_ui, context

    def test_preview_never_prompts_or_resolves_a_pane(self):
        result, _, check, resolve, open_ui, _ = self.dispatch(apply=False)
        self.assertEqual(result, {"preview": True})
        check.assert_not_called()
        resolve.assert_not_called()
        open_ui.assert_not_called()

    def test_successful_first_install_opens_welcome_for_resolved_pane(self):
        result, _, _, resolve, open_ui, context = self.dispatch()
        resolve.assert_called_once()
        open_ui.assert_called_once_with(context, "welcome")
        self.assertEqual(result["welcome"], {"opened": True})

    def test_returning_install_does_not_open_welcome(self):
        result, _, _, resolve, open_ui, _ = self.dispatch(welcome_needed=False)
        resolve.assert_not_called()
        open_ui.assert_not_called()
        self.assertEqual(result, {"installed": "blr.herdr-shell"})

    def test_install_without_a_pane_reports_next_open_instead_of_failing(self):
        result, _, _, _, open_ui, _ = self.dispatch(failure=ShellError("not inside Herdr"))
        self.assertEqual(result["installed"], "blr.herdr-shell")
        self.assertIn("next", result)
        open_ui.assert_not_called()

    def test_failed_install_does_not_prompt(self):
        args = cli.parser().parse_args(["install", "--apply"])
        with patch.object(cli, "ConfigStore"), patch.object(cli, "install", side_effect=ShellError("validation failed")), \
             patch.object(onboarding, "needs_welcome") as check, patch.object(cli, "open_ui") as open_ui:
            with self.assertRaisesRegex(ShellError, "validation failed"):
                cli.dispatch(args)
        check.assert_not_called()
        open_ui.assert_not_called()


if __name__ == "__main__":
    unittest.main()
