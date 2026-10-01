import curses
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from herdr_shell import trainer
from herdr_shell.desktop import MAPPINGS
from herdr_shell.runtime import Context, ShellError
from herdr_shell.trainer import Mission, Quest, missions, open_game, run_game


class MissionTests(unittest.TestCase):
    def test_every_registered_shortcut_is_a_live_mission(self):
        deck = missions()
        self.assertEqual(len(deck), 26)
        self.assertEqual({m.id for m in deck}, {action for _, action, _ in MAPPINGS})
        self.assertTrue(all(m.live and not m.options for m in deck))
        self.assertTrue({"pane-close", "tab-close", "workspace-close", "agent-new", "launch-git"} <= {m.id for m in deck})
        self.assertTrue(any(m.answer == "Shift+PageDown" for m in deck))


class GameplayTests(unittest.TestCase):
    def quest(self):
        quest = Quest.__new__(Quest)
        quest.context = Mock(pane="guide", terminal="guide-term", workspace="practice", tab="guide-tab")
        quest.context.client.snapshot.return_value = {"focused_pane_id": "guide", "panes": [
            {"pane_id": "guide", "terminal_id": "guide-term", "workspace_id": "practice", "tab_id": "guide-tab"}]}
        quest.input = Mock()
        quest.input.feedback.return_value = []
        quest.deck, quest.index = [missions()[0]], 0
        quest.active, quest.plan = True, SimpleNamespace(action="pane-split-right", presses=1, required_presses=1, pane_identities=[], instructions="Labeled practice target")
        quest.notice = ""
        quest.score, quest.mastered, quest.hinted, quest.help, quest.scroll = 0, set(), False, False, 0
        return quest

    def test_state_alone_never_awards_or_dispatches(self):
        quest = self.quest()
        quest.input.next_event.return_value = None
        with patch("herdr_shell.practice.perform") as perform, patch("herdr_shell.practice.verified") as verified:
            quest.poll_practice()
        perform.assert_not_called()
        verified.assert_not_called()
        self.assertEqual(quest.score, 0)

    def test_chord_requires_two_verified_observations_before_score(self):
        quest = self.quest()
        plan = quest.plan
        quest.input.next_event.return_value = ('pane-split-right', quest.context)
        with patch("herdr_shell.practice.perform") as perform, patch("herdr_shell.practice.verified", return_value=True) as verified, \
                patch.object(trainer.time, 'sleep'):
            quest.poll_practice()
        perform.assert_called_once_with(plan, 'pane-split-right')
        self.assertEqual(verified.call_count, 2)
        self.assertEqual(quest.score, 10)
        self.assertEqual(quest.index, 1)
        quest.input.disarm.assert_called_once()
        quest.input.complete.assert_called_once()

    def test_failed_observation_does_not_score_and_disarms(self):
        quest = self.quest()
        quest.input.next_event.return_value = ('pane-split-right', quest.context)
        with patch("herdr_shell.practice.perform"), patch("herdr_shell.practice.verified", side_effect=[True, False]), \
                patch.object(trainer.time, 'sleep'):
            quest.poll_practice()
        self.assertEqual(quest.score, 0)
        self.assertFalse(quest.active)
        quest.input.disarm.assert_called_once()
        quest.input.complete.assert_called_once()

    def test_multi_press_mission_waits_for_all_real_chords(self):
        quest = self.quest()
        quest.plan.required_presses = 2
        quest.input.next_event.return_value = ('pane-zoom', quest.context)
        with patch("herdr_shell.practice.perform"), patch("herdr_shell.practice.verified", return_value=True), \
                patch.object(trainer.time, 'sleep'):
            quest.poll_practice()
        self.assertEqual(quest.score, 0)
        self.assertEqual(quest.index, 0)
        self.assertTrue(quest.active)
        self.assertIn('1/2', quest.notice)

    def test_wrong_chord_feedback_keeps_mission_ready_without_action(self):
        quest = self.quest()
        quest.input.feedback.return_value = ['Try Super+Alt+R.']
        quest.input.next_event.return_value = None
        with patch("herdr_shell.practice.perform") as perform:
            quest.poll_practice()
        perform.assert_not_called()
        self.assertTrue(quest.active)
        self.assertEqual(quest.notice, 'Try Super+Alt+R.')

    def test_unavailable_chord_cannot_prepare_or_change_any_fixture(self):
        quest = self.quest()
        with patch("herdr_shell.desktop.effective_bindings", return_value={'pane-split-right': {'active': False, 'reason': 'Reserved by desktop'}}), \
                patch("herdr_shell.practice.prepare") as prepare:
            with self.assertRaisesRegex(ShellError, 'Reserved by desktop'):
                quest.start_practice(missions()[0])
        prepare.assert_not_called()
        quest.context.validate.assert_not_called()

    def test_preparing_arms_only_finished_owned_fixture_plan(self):
        quest = self.quest()
        plan = SimpleNamespace(pane_identities=[])
        with patch("herdr_shell.desktop.effective_bindings", return_value={'pane-split-right': {'active': True}}), \
                patch("herdr_shell.practice.prepare", return_value=plan) as prepare:
            quest.start_practice(missions()[0])
        prepare.assert_called_once_with('pane-split-right', quest.context)
        quest.input.arm.assert_called_once_with(plan)
        self.assertIs(quest.plan, plan)
        plan.on_adopt()
        quest.input.update.assert_called_once_with(plan)

    def test_typing_shortcut_letter_never_arms_or_awards(self):
        quest = self.quest()
        quest.active = False
        quest.welcome, quest.draw = Mock(return_value=True), Mock()
        quest.scroll_key = Mock(return_value=False)
        quest.key = Mock(side_effect=['r', 'R', 'Shift+R', '\x1b'])
        quest.screen = Mock()
        quest.screen.getmaxyx.return_value = (32, 100)
        quest.start_practice, quest.award = Mock(), Mock()
        with patch('herdr_shell.game_input.Broker') as broker:
            broker.return_value.__enter__.return_value = quest.input
            quest.run()
        quest.start_practice.assert_not_called()
        quest.award.assert_not_called()
        self.assertIn('Typing', quest.notice)

    def test_tiny_welcome_requires_resize_before_play(self):
        quest = self.quest()
        quest.screen = Mock()
        quest.screen.getmaxyx.return_value = (10, 25)
        quest.page, quest.scroll_key = Mock(), Mock(return_value=False)
        quest.key = Mock(side_effect=['\n', '\x1b'])
        self.assertFalse(quest.welcome())

    def test_return_never_steals_focus_from_a_nonpractice_terminal(self):
        quest = self.quest()
        quest.context.client.snapshot.return_value = {'focused_pane_id': 'real-work', 'panes': [
            {'pane_id': 'real-work', 'terminal_id': 'real-terminal'}]}
        quest.return_guide()
        quest.context.client.call.assert_not_called()

    def test_close_can_return_from_an_earlier_owned_fixture(self):
        quest = self.quest()
        quest.practice_identities = {('earlier', 'same-terminal', 'practice', 'earlier-tab')}
        quest.context.client.snapshot.return_value = {'focused_pane_id': 'earlier', 'panes': [
            {'pane_id': 'earlier', 'terminal_id': 'same-terminal', 'workspace_id': 'practice', 'tab_id': 'earlier-tab'}]}
        quest.return_guide()
        quest.context.client.call.assert_any_call('pane.focus', pane_id='guide')

    def test_reused_pane_id_cannot_receive_guide_focus_return(self):
        quest = self.quest()
        quest.practice_identities = {('earlier', 'old-terminal', 'practice', 'earlier-tab')}
        quest.context.client.snapshot.return_value = {'focused_pane_id': 'earlier', 'panes': [
            {'pane_id': 'earlier', 'terminal_id': 'new-terminal', 'workspace_id': 'practice', 'tab_id': 'earlier-tab'}]}
        quest.return_guide()
        quest.context.client.call.assert_not_called()

    def test_moved_practice_terminal_cannot_steal_focus_back_from_user_space(self):
        quest = self.quest()
        quest.practice_identities = {('earlier', 'same-terminal', 'practice', 'earlier-tab')}
        quest.context.client.snapshot.return_value = {'focused_pane_id': 'earlier', 'panes': [
            {'pane_id': 'earlier', 'terminal_id': 'same-terminal', 'workspace_id': 'user-work', 'tab_id': 'user-tab'}]}
        quest.return_guide()
        quest.context.client.call.assert_not_called()

    def test_mission_shows_practice_specific_instructions(self):
        quest = self.quest()
        quest.page = Mock()
        quest.draw(quest.deck[0])
        self.assertIn('Labeled practice target', quest.page.call_args.args[1])


class LifecycleTests(unittest.TestCase):
    def test_launch_uses_only_new_workspace_and_regular_plugin_pane(self):
        context = Mock()
        context.workspace = "user-work"
        context.client.call.side_effect = [
            {"workspace": {"workspace_id": "quest"}, "root_pane": {
                "pane_id": "quest-root", "terminal_id": "quest-terminal", "workspace_id": "quest", "tab_id": "quest-tab"}},
            {"plugin_pane": {"pane": {"pane_id": "guide"}}},
        ]
        with tempfile.TemporaryDirectory() as root, patch("herdr_shell.trainer.tempfile.mkdtemp", return_value=root), \
                patch("herdr_shell.desktop.popup_environment", return_value={}):
            result = open_game(context)
            self.assertEqual(result["practice_workspace"], "quest")
            self.assertEqual(context.client.call.call_args_list[0].args, ("workspace.create",))
            self.assertTrue(context.client.call.call_args_list[0].kwargs["focus"])
            opening = context.client.call.call_args_list[1].kwargs
            self.assertEqual(opening["target_pane_id"], "quest-root")
            self.assertEqual(opening["placement"], "split")
            self.assertEqual(opening["entrypoint"], "trainer")
            self.assertNotIn("HERDR_SHELL_CONTEXT", opening["env"])
            self.assertTrue((Path(root) / "README.txt").exists())
            self.assertEqual(json.loads(opening['env']['HERDR_SHELL_GAME_ROOT'])['terminal_id'], 'quest-terminal')

    def test_failed_launch_leaves_new_workspace_and_never_closes_work(self):
        context = Mock()
        context.workspace = "user-work"
        context.client.call.side_effect = [
            {"workspace": {"workspace_id": "quest"}, "root_pane": {
                "pane_id": "quest-root", "terminal_id": "quest-terminal", "workspace_id": "quest", "tab_id": "quest-tab"}},
            ShellError("entrypoint missing"),
        ]
        with tempfile.TemporaryDirectory() as root, patch("herdr_shell.trainer.tempfile.mkdtemp", return_value=root), \
                patch("herdr_shell.desktop.popup_environment", return_value={}):
            with self.assertRaisesRegex(ShellError, "practice workspace quest remains"):
                open_game(context)
        self.assertEqual([c.args[0] for c in context.client.call.call_args_list], ["workspace.create", "plugin.pane.open"])

    def test_game_refuses_an_existing_user_workspace(self):
        context = Mock(workspace="user-work")
        with patch.dict("os.environ", {}, clear=True), patch("herdr_shell.trainer.curses.wrapper") as wrapper:
            with self.assertRaisesRegex(ShellError, "own practice workspace"):
                run_game(context)
        wrapper.assert_not_called()
        context.client.call.assert_not_called()

    def test_exit_and_ui_failure_clean_only_owned_practice_simulators(self):
        for failure in (None, ShellError('Guide interrupted')):
            with self.subTest(failure=failure):
                context = Mock(workspace='quest')
                with patch.dict('os.environ', {'HERDR_SHELL_GAME_WORKSPACE': 'quest'}, clear=True), \
                     patch('herdr_shell.trainer.curses.wrapper', side_effect=failure), \
                     patch('herdr_shell.practice.cleanup', create=True, return_value={}) as cleanup:
                    if failure:
                        with self.assertRaisesRegex(ShellError, 'Guide interrupted'):
                            run_game(context)
                    else:
                        run_game(context)
                cleanup.assert_called_once_with(context)

    def test_actual_cleanup_error_is_reported_after_game_exit(self):
        context = Mock(workspace='quest')
        with patch.dict('os.environ', {'HERDR_SHELL_GAME_WORKSPACE': 'quest'}, clear=True), \
             patch('herdr_shell.trainer.curses.wrapper'), \
             patch('herdr_shell.practice.cleanup', side_effect=ShellError('Practice report could not clear')):
            with self.assertRaisesRegex(ShellError, 'Practice report could not clear'):
                run_game(context)


if __name__ == '__main__':
    unittest.main()
