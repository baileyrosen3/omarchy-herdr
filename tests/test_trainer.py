# Verified learning flow: keyboard receipts, automatic progression, and fair clocks.
import curses
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from herdr_shell import trainer
from herdr_shell.desktop import MAPPINGS
from herdr_shell.runtime import ShellError
from herdr_shell.trainer import Quest, missions, open_game, run_game


def pane(name, terminal=None, workspace="practice", tab="guide-tab"):
    return dict(pane_id=name, terminal_id=terminal or name + "-term", workspace_id=workspace, tab_id=tab)


class Screen:
    def __init__(self, keys=(), size=(32, 100)):
        self.events = iter(keys)
        self.size = size
        self.frames = []
        self.timeouts = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.frames.append([])

    def refresh(self):
        pass

    def addstr(self, y, x, value, attr=0):
        if not self.frames:
            self.frames.append([])
        self.frames[-1].append(value)

    def keypad(self, enabled):
        pass

    def timeout(self, value):
        self.timeouts.append(value)

    def get_wch(self):
        try:
            return next(self.events)
        except StopIteration:
            raise curses.error("No input")


class TrainerCase(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        for target, value in (("herdr_shell.trainer.time.monotonic", lambda: self.now),
                              ("herdr_shell.trainer.time.sleep", lambda delay: None),
                              ("herdr_shell.trainer.curses.curs_set", lambda visible: None),
                              ("herdr_shell.trainer.curses.mousemask", lambda mask: None),
                              ("herdr_shell.trainer.curses.has_colors", lambda: False),
                              ("herdr_shell.scoring.read_results", lambda profile: {"best": None})):
            patcher = patch(target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict("os.environ", {"HERDR_SHELL_GAME_ROOT": "null"})
        environment.start()
        self.addCleanup(environment.stop)

    def quest(self, mode="speed", action="pane-split-right", presses=1):
        context = Mock(pane="guide", terminal="guide-term", workspace="practice", tab="guide-tab",
                       socket="/tmp/herdr-qa.sock", cwd="/tmp/herdr-qa-private")
        context.client.snapshot.return_value = {"focused_pane_id": "guide", "panes": [pane("guide")]}
        deck = [next(m for m in missions() if m.id == action)]
        quest = Quest(Screen(), context, deck=deck, mode=mode)
        quest.input = Mock()
        quest.input.metrics.return_value = {"wrong_count": 0, "wrong_events": []}
        quest.input.feedback.return_value = []
        quest.input.next_event.return_value = None
        quest.input.last_received_at = None
        quest.desktop_focused = Mock(return_value=True)
        quest.plan = self.plan(quest, action, presses)
        quest.phase, quest.active = "ready", True
        quest.response_started_at = self.now
        if mode == "speed":
            quest.timer.arm(self.now)
        return quest

    def plan(self, quest, action="pane-split-right", presses=1):
        target = Mock(pane="target", terminal="target-term", workspace="practice", tab="target-tab")
        return SimpleNamespace(action=action, presses=0, required_presses=presses,
                               pane_identities=[pane("target", tab="target-tab")],
                               instructions="Labeled practice target", expected={}, target=target)

    def receipt(self, quest, elapsed=1.0):
        self.now += elapsed
        quest.input.next_event.return_value = (quest.plan.action, quest.context)
        quest.input.last_received_at = self.now

    def successful_press(self, quest):
        plan = quest.plan
        def perform(current, action):
            current.presses += 1
        with patch("herdr_shell.practice.perform", side_effect=perform) as perform_mock, \
             patch("herdr_shell.practice.verified", return_value=True) as verified:
            quest.poll_practice()
        return plan, perform_mock, verified


class MissionTests(TrainerCase):
    def test_every_registered_shortcut_is_a_live_mission(self):
        deck = missions()
        self.assertEqual(len(deck), 26)
        self.assertEqual({m.id for m in deck}, {action for _, action, _ in MAPPINGS})
        self.assertTrue(all(m.live and not m.options for m in deck))
        self.assertTrue({"pane-close", "tab-close", "workspace-close", "agent-new", "launch-git"} <= {m.id for m in deck})
        self.assertTrue(any(m.answer == "Shift+PageDown" for m in deck))

    def test_speed_shuffles_full_deck_but_hands_on_preserves_teaching_order(self):
        quest = self.quest()
        with patch("herdr_shell.trainer.random.SystemRandom") as random:
            random.return_value.shuffle.side_effect = lambda rows: rows.reverse()
            quest.reset("speed")
            self.assertEqual(quest.deck, list(reversed(missions())))
            random.return_value.shuffle.assert_called_once()
            random.return_value.shuffle.reset_mock()
            quest.reset("hands-on")
            self.assertEqual(quest.deck, missions())
            random.return_value.shuffle.assert_not_called()

    def test_score_profile_is_stable_small_digest_of_actual_shortcuts(self):
        quest = self.quest()
        expected = hashlib.sha256(json.dumps([(m.id, m.answer) for m in missions()], sort_keys=True).encode()).hexdigest()
        self.assertEqual(quest.profile, expected)
        self.assertEqual(len(quest.profile), 64)

    def test_hands_on_shows_chord_and_specific_target_explanations(self):
        quest = self.quest("hands-on")
        quest.page = Mock()
        quest.draw(quest.deck[0])
        text = "\n".join(quest.page.call_args.args[1])
        self.assertIn("Super+Alt+R", text)
        self.assertIn("Labeled practice target", text)
        self.assertIn(quest.deck[0].hint, text)
        self.assertIn("Demo", text)

    def test_speed_hides_every_answer_until_assisted_hint(self):
        quest = self.quest()
        quest.page = Mock()
        for mission in missions():
            with self.subTest(action=mission.id):
                quest.draw(mission)
                text = "\n".join(quest.page.call_args.args[1])
                self.assertNotIn("Super+Alt+" + mission.answer, text)
                self.assertNotIn("Labeled practice target", text)
                self.assertNotIn("Demo", text)
        quest.hinted = True
        quest.draw(quest.deck[0])
        self.assertIn("Super+Alt+R", "\n".join(quest.page.call_args.args[1]))

    def test_tiny_welcome_requires_resize_before_start(self):
        quest = self.quest()
        quest.screen = Screen([" ", "\n", "\x1b"], size=(10, 25))
        self.assertFalse(quest.welcome())

    def test_minimum_width_keeps_pause_help_exit_and_secondary_controls_readable(self):
        for mode in ("hands-on", "speed"):
            with self.subTest(mode=mode):
                quest = self.quest(mode)
                quest.screen.size = (16, 38)
                quest.draw(quest.deck[0])
                text = "\n".join(quest.screen.frames[-1])
                for key in ("Space", "F1", "F2", "F3", "Esc"):
                    self.assertIn(key, text)
                if mode == "hands-on":
                    self.assertIn("F4", text)


class ReceiptTests(TrainerCase):
    def test_state_alone_never_awards_or_dispatches(self):
        quest = self.quest()
        quest.context.client.snapshot.return_value["panes"].append(pane("new-pane"))
        with patch("herdr_shell.practice.perform") as perform, patch("herdr_shell.practice.verified") as verified:
            quest.poll_practice()
        perform.assert_not_called()
        verified.assert_not_called()
        self.assertEqual(quest.race.correct, 0)
        self.assertEqual(quest.race.points, 0)

    def test_ordinary_letters_and_enter_never_dispatch_actions(self):
        for mode in ("hands-on", "speed"):
            quest = self.quest(mode)
            with patch("herdr_shell.practice.perform") as perform, patch("herdr_shell.game_input.route") as route:
                for key in ("r", "R", "Shift+R", "\n"):
                    self.assertTrue(quest.handle_key(key))
            perform.assert_not_called()
            route.assert_not_called()
            self.assertEqual(quest.race.correct, 0)

    def test_chord_requires_two_verified_observations_before_score(self):
        quest = self.quest()
        self.receipt(quest)
        plan, perform, verified = self.successful_press(quest)
        perform.assert_called_once_with(plan, "pane-split-right")
        self.assertEqual(verified.call_count, 2)
        self.assertEqual(quest.race.correct, 1)
        self.assertEqual(quest.race.points, 200)
        self.assertEqual(quest.index, 0)
        self.assertEqual(quest.phase, "effect")
        self.assertFalse(quest.timer.running)
        quest.input.complete.assert_called_once()

    def test_receipt_timestamp_excludes_slow_execution_and_ui_poll(self):
        quest = self.quest()
        quest.input.next_event.return_value = (quest.plan.action, quest.context)
        quest.input.last_received_at = 101.0
        self.now = 110.0
        self.successful_press(quest)
        self.assertEqual(quest.race.average_time, 1.0)
        self.assertEqual(quest.timer.elapsed(), 1.0)
        self.now = 200
        self.assertEqual(quest.timer.elapsed(), 1.0)

    def test_after_effect_next_target_prepares_without_enter(self):
        quest = self.quest()
        second = next(m for m in missions() if m.id == "pane-split-down")
        quest.deck.append(second)
        self.receipt(quest)
        self.successful_press(quest)
        self.now += .4
        quest.finish_effect()
        self.assertEqual(quest.index, 1)
        self.assertEqual(quest.phase, "next")
        next_plan = self.plan(quest, second.id)
        with patch("herdr_shell.desktop.effective_bindings", return_value={second.id: {"active": True}}), \
             patch("herdr_shell.practice.prepare", return_value=next_plan) as prepare:
            quest.prepare_next()
        prepare.assert_called_once_with(second.id, quest.context, display_chord=False)
        self.assertEqual(quest.phase, "ready")
        self.assertEqual(quest.plan, next_plan)
        self.assertEqual(quest.timer.elapsed(), 1.0)

    def test_run_automatically_arms_first_target_before_any_mission_key(self):
        quest = self.quest()
        quest.phase, quest.active, quest.plan = "next", False, None
        quest.welcome = Mock(return_value=True)
        quest.save_score = Mock()
        order = []
        keys = iter([None, "\x1b"])
        def key():
            order.append("key")
            return next(keys)
        quest.key = key
        def prepare(action, context, **kwargs):
            order.append("prepare")
            return self.plan(quest)
        with patch("herdr_shell.game_input.Broker") as broker, \
             patch("herdr_shell.desktop.effective_bindings", return_value={quest.deck[0].id: {"active": True}}), \
             patch("herdr_shell.practice.prepare", side_effect=prepare):
            broker.return_value.__enter__.return_value = quest.input
            quest.run()
        self.assertEqual(order[0], "prepare")
        self.assertEqual(order.count("prepare"), 1)
        quest.input.arm.assert_called_once()

    def test_multi_press_mission_verifies_each_press_without_advancing_early(self):
        quest = self.quest(action="pane-zoom", presses=2)
        self.receipt(quest)
        self.successful_press(quest)
        self.assertEqual(quest.race.correct, 1)
        self.assertEqual(quest.mastered, set())
        self.assertEqual(quest.index, 0)
        self.now += .4
        quest.finish_effect()
        self.assertEqual(quest.phase, "ready")
        self.receipt(quest)
        self.successful_press(quest)
        self.assertEqual(quest.race.correct, 2)
        self.assertEqual(quest.mastered, {"pane-zoom"})
        self.assertEqual(quest.timer.elapsed(), 2.0)

    def test_unverified_result_never_scores_and_refunds_technical_time(self):
        for checks in ([False], [True, False]):
            with self.subTest(checks=checks):
                quest = self.quest()
                self.receipt(quest, 3)
                with patch("herdr_shell.practice.perform"), patch("herdr_shell.practice.verified", side_effect=checks):
                    quest.poll_practice()
                self.assertEqual(quest.race.correct, 0)
                self.assertEqual(quest.race.points, 0)
                self.assertEqual(quest.timer.elapsed(), 0.0)
                self.assertEqual(quest.phase, "blocked")
                self.assertEqual(quest.failures, 1)
                quest.input.complete.assert_called_once()

    def test_execution_exception_refunds_only_failed_current_response(self):
        quest = self.quest(action="pane-zoom", presses=2)
        self.receipt(quest, 2)
        self.successful_press(quest)
        self.now += .4
        quest.finish_effect()
        self.receipt(quest, 5)
        with patch("herdr_shell.practice.perform", side_effect=ShellError("Target changed")):
            quest.poll_practice()
        self.assertEqual(quest.race.correct, 1)
        self.assertEqual(quest.timer.elapsed(), 2.0)
        self.assertEqual(quest.phase, "blocked")

    def test_return_focus_failure_blocks_recovery_without_refunding_verified_score(self):
        quest = self.quest()
        self.receipt(quest)
        self.successful_press(quest)
        self.now += .4
        with patch.object(quest, "return_guide", side_effect=ShellError("Origin moved")):
            quest.finish_effect()
        self.assertEqual(quest.phase, "blocked")
        self.assertEqual(quest.failures, 1)
        self.assertEqual(quest.race.correct, 1)
        self.assertEqual(quest.timer.elapsed(), 1)
        self.assertIn("Could not return", quest.notice)

    def test_correct_receipt_after_deadline_cannot_run_or_score(self):
        quest = self.quest()
        self.receipt(quest, 120)
        with patch("herdr_shell.practice.perform") as perform:
            quest.poll_practice()
        perform.assert_not_called()
        self.assertEqual(quest.race.correct, 0)
        self.assertEqual(quest.phase, "finished")
        self.assertEqual(quest.finished_reason, "TIME UP")

    def test_queued_receipt_is_consumed_before_a_later_pause_or_hint_key(self):
        for key in (" ", curses.KEY_F1):
            with self.subTest(key=key):
                quest = self.quest()
                quest.welcome, quest.save_score = Mock(return_value=True), Mock()
                reads = iter([key, "\x1b"])
                def read_key():
                    value = next(reads)
                    if value == key:
                        quest.input.next_event.return_value = (quest.plan.action, quest.context)
                        quest.input.last_received_at = self.now + 1
                        self.now += 10
                    return value
                quest.key = read_key
                def perform(plan, action):
                    plan.presses += 1
                with patch("herdr_shell.game_input.Broker") as broker, \
                     patch("herdr_shell.practice.perform", side_effect=perform), \
                     patch("herdr_shell.practice.verified", return_value=True):
                    broker.return_value.__enter__.return_value = quest.input
                    quest.run()
                self.assertEqual(quest.race.correct, 1)
                self.assertEqual(quest.race.hinted_correct, 0)
                self.assertEqual(quest.race.average_time, 1)
                self.assertEqual(quest.timer.elapsed(), 1)

    def test_deadline_last_poll_preserves_earlier_queued_receipt(self):
        quest = self.quest()
        quest.welcome, quest.save_score = Mock(return_value=True), Mock()
        quest.key = Mock(return_value="\x1b")
        quest.input.next_event.side_effect = [None, (quest.plan.action, quest.context)]
        quest.input.last_received_at = self.now + 119
        self.now += 121
        def perform(plan, action):
            plan.presses += 1
        with patch("herdr_shell.game_input.Broker") as broker, \
             patch("herdr_shell.practice.perform", side_effect=perform), \
             patch("herdr_shell.practice.verified", return_value=True):
            broker.return_value.__enter__.return_value = quest.input
            quest.run()
        self.assertEqual(quest.race.correct, 1)
        self.assertEqual(quest.timer.elapsed(), 119)
        self.assertNotEqual(quest.finished_reason, "TIME UP")


class PreparationTests(TrainerCase):
    def test_speed_inactive_shortcut_cannot_prepare_any_fixture(self):
        quest = self.quest()
        with patch("herdr_shell.desktop.effective_bindings", return_value={quest.deck[0].id: {"active": False, "reason": "Reserved by desktop"}}), \
             patch("herdr_shell.practice.prepare") as prepare:
            with self.assertRaisesRegex(ShellError, "Reserved by desktop"):
                quest.start_practice(quest.deck[0])
        prepare.assert_not_called()
        quest.context.validate.assert_not_called()

    def test_hands_on_inactive_shortcut_prepares_demo_with_visible_chord(self):
        quest = self.quest("hands-on")
        plan = self.plan(quest)
        with patch("herdr_shell.desktop.effective_bindings", return_value={quest.deck[0].id: {"active": False}}), \
             patch("herdr_shell.practice.prepare", return_value=plan) as prepare:
            quest.start_practice(quest.deck[0])
        prepare.assert_called_once_with(quest.deck[0].id, quest.context, display_chord=True)
        quest.input.arm.assert_called_once_with(plan)
        self.assertIn("Demo", quest.notice)
        self.assertFalse(quest.timer.running)
        plan.on_adopt()
        quest.input.update.assert_called_once_with(plan)

    def test_preparation_and_retries_are_untimed_and_do_not_busy_loop(self):
        quest = self.quest()
        quest.timer.cancel_response()
        quest.phase, quest.plan, quest.active = "next", None, False
        with patch("herdr_shell.practice.prepare", side_effect=ShellError("Dependency unavailable")) as prepare, \
             patch("herdr_shell.desktop.effective_bindings", return_value={quest.deck[0].id: {"active": True}}):
            quest.prepare_next()
        self.assertEqual(quest.phase, "blocked")
        self.assertFalse(quest.timer.running)
        self.assertEqual(quest.timer.elapsed(), 0.0)
        self.assertEqual(prepare.call_count, 1)
        quest.handle_key(curses.KEY_F2)
        self.assertEqual(quest.phase, "next")
        self.assertEqual(quest.index, 0)
        self.assertFalse(quest.timer.running)

    def test_wrong_feedback_baseline_is_reset_after_new_target_preparation(self):
        quest = self.quest()
        quest.input.metrics.return_value = {"wrong_count": 7, "wrong_events": []}
        with patch("herdr_shell.desktop.effective_bindings", return_value={quest.deck[0].id: {"active": True}}), \
             patch("herdr_shell.practice.prepare", return_value=self.plan(quest)):
            quest.start_practice(quest.deck[0])
        self.assertEqual(quest.wrong_seen, 7)
        quest.feedback_metrics()
        self.assertEqual(quest.race.wrong, 0)

    def test_enter_does_not_retry_blocked_target(self):
        quest = self.quest()
        quest.phase = "blocked"
        quest.handle_key("\n")
        self.assertEqual(quest.phase, "blocked")
        quest.input.arm.assert_not_called()


class DemoTests(TrainerCase):
    def test_demo_and_f4_queue_the_same_scoped_input_then_verify_actual_action(self):
        quest = self.quest("hands-on")
        with patch("herdr_shell.game_input.route", return_value={"game": "queued"}) as route, \
             patch("herdr_shell.practice.perform") as perform:
            quest.handle_key(curses.KEY_F4)
        route.assert_called_once_with(quest.plan.action, quest.context)
        perform.assert_not_called()
        self.receipt(quest)
        self.successful_press(quest)
        self.assertEqual(quest.mastered, {quest.deck[0].id})
        self.assertEqual(quest.race.correct, 0)
        self.assertEqual(quest.race.points, 0)

    def test_mouse_demo_requires_its_visible_click_row(self):
        quest = self.quest("hands-on")
        quest.demo_rows = {8}
        with patch("herdr_shell.trainer.curses.getmouse", return_value=(0, 4, 8, 0, curses.BUTTON1_CLICKED)), \
             patch("herdr_shell.game_input.route", return_value={"game": "queued"}) as route:
            quest.handle_key(curses.KEY_MOUSE)
        route.assert_called_once_with(quest.plan.action, quest.context)
        with patch("herdr_shell.trainer.curses.getmouse", return_value=(0, 4, 9, 0, curses.BUTTON1_CLICKED)), \
             patch("herdr_shell.game_input.route") as route:
            quest.handle_key(curses.KEY_MOUSE)
        route.assert_not_called()

    def test_speed_demo_mouse_and_f4_can_never_dispatch_or_score(self):
        quest = self.quest()
        with patch("herdr_shell.game_input.route") as route, patch("herdr_shell.trainer.curses.getmouse") as mouse:
            quest.demo()
            quest.handle_key(curses.KEY_F4)
            quest.handle_key(curses.KEY_MOUSE)
        route.assert_not_called()
        mouse.assert_not_called()
        self.assertEqual(quest.race.correct, 0)

    def test_paused_demo_cannot_act(self):
        quest = self.quest("hands-on")
        quest.suspension = "Paused"
        with patch("herdr_shell.game_input.route") as route:
            quest.demo()
        route.assert_not_called()


class ClockAndFeedbackTests(TrainerCase):
    def test_explicit_pause_avoids_desktop_and_herdr_focus_queries(self):
        quest = self.quest()
        quest.paused = True
        quest.sync_visibility()
        quest.desktop_focused.assert_not_called()
        quest.context.client.snapshot.assert_not_called()
        self.assertFalse(quest.timer.running)

    def test_space_pause_resume_preserves_reaction_and_run_budget(self):
        quest = self.quest()
        self.now += 2
        quest.handle_key(" ")
        self.assertFalse(quest.timer.running)
        self.now += 60
        self.assertEqual(quest.timer.elapsed(), 2)
        quest.handle_key(" ")
        self.assertTrue(quest.timer.running)
        self.now += 1
        self.assertEqual(quest.timer.elapsed(), 3)

    def test_resize_and_lost_desktop_focus_pause_until_visible_again(self):
        for reason in ("resize", "desktop", "pane"):
            with self.subTest(reason=reason):
                quest = self.quest()
                self.now += 2
                if reason == "resize":
                    quest.screen.size = (10, 25)
                elif reason == "desktop":
                    quest.desktop_focused.return_value = False
                else:
                    quest.context.client.snapshot.return_value = {"focused_pane_id": "real", "panes": [pane("real", workspace="user-work")]}
                quest.sync_visibility()
                self.assertFalse(quest.timer.running)
                self.now += 60
                self.assertEqual(quest.timer.elapsed(), 2)
                quest.screen.size = (32, 100)
                quest.desktop_focused.return_value = True
                quest.context.client.snapshot.return_value = {"focused_pane_id": "guide", "panes": [pane("guide")]}
                quest.sync_visibility()
                self.assertTrue(quest.timer.running)
                self.now += 1
                self.assertEqual(quest.timer.elapsed(), 3)

    def test_owned_menu_visibility_requires_exact_origin_terminal_and_scope(self):
        quest = self.quest(action="menu", presses=2)
        quest.plan.presses = 1
        marker = {"pane": "target", "terminal": "target-term"}
        quest.plan.expected["menu_marker"] = marker
        with patch("herdr_shell.desktop.menu_running", return_value=marker):
            quest.context.client.snapshot.return_value = {"focused_pane_id": "target", "panes": [pane("target", tab="target-tab")]}
            self.assertTrue(quest.visible_context()[0])
            for current in (pane("real", workspace="user-work"), pane("target", terminal="replacement", tab="target-tab"),
                            pane("target", workspace="user-work", tab="target-tab")):
                with self.subTest(current=current):
                    quest.context.client.snapshot.return_value = {"focused_pane_id": current["pane_id"], "panes": [current]}
                    self.assertFalse(quest.visible_context()[0])

    def test_early_menu_escape_recovers_only_owned_target_and_refunds_pending_close(self):
        quest = self.quest(action="menu", presses=2)
        quest.plan.presses = 1
        quest.plan.expected["menu_marker"] = {"pane": "target", "terminal": "target-term"}
        quest.race.award(1)
        self.now += 1
        quest.timer.cancel_response()
        quest.timer.arm()
        self.now += 4
        quest.context.client.snapshot.return_value = {"focused_pane_id": "target", "panes": [pane("target", tab="target-tab")]}
        with patch("herdr_shell.desktop.menu_running", return_value=None):
            quest.sync_visibility()
        self.assertEqual(quest.phase, "blocked")
        self.assertEqual(quest.failures, 1)
        self.assertEqual(quest.race.correct, 1)
        self.assertEqual(quest.timer.elapsed(), 1)
        self.assertIn("closed early", quest.notice)
        quest.context.client.call.assert_any_call("pane.focus", pane_id="guide")

    def test_missing_menu_marker_cannot_steal_focus_from_unrelated_or_replaced_target(self):
        for current in (pane("real", workspace="user-work"), pane("target", terminal="replacement", tab="target-tab")):
            with self.subTest(current=current):
                quest = self.quest(action="menu", presses=2)
                quest.plan.presses = 1
                quest.plan.expected["menu_marker"] = {"pane": "target", "terminal": "target-term"}
                quest.context.client.snapshot.return_value = {"focused_pane_id": current["pane_id"], "panes": [current]}
                with patch("herdr_shell.desktop.menu_running", return_value=None):
                    quest.sync_visibility()
                self.assertEqual(quest.phase, "ready")
                self.assertTrue(quest.suspension)
                self.assertFalse(quest.timer.running)
                quest.context.client.call.assert_not_called()

    def test_hint_stays_assisted_and_clock_frozen_across_zoom_presses(self):
        quest = self.quest(action="pane-zoom", presses=2)
        self.now += 2
        quest.handle_key(curses.KEY_F1)
        self.assertTrue(quest.hinted)
        self.assertFalse(quest.timer.running)
        self.receipt(quest, 60)
        self.successful_press(quest)
        self.now += .4
        quest.finish_effect()
        self.assertFalse(quest.timer.running)
        self.receipt(quest, 60)
        self.successful_press(quest)
        self.assertEqual(quest.race.hinted_correct, 2)
        self.assertEqual(quest.race.points, 200)
        self.assertEqual(quest.timer.elapsed(), 2)

    def test_menu_second_press_remains_frozen_after_hint(self):
        quest = self.quest(action="menu", presses=2)
        self.now += 2
        quest.handle_key(curses.KEY_F1)
        self.receipt(quest, 30)
        self.successful_press(quest)
        self.assertEqual(quest.phase, "ready")
        self.assertFalse(quest.timer.running)
        self.receipt(quest, 30)
        self.successful_press(quest)
        self.assertEqual(quest.race.hinted_correct, 2)
        self.assertEqual(quest.timer.elapsed(), 2)

    def test_wrong_metrics_penalize_once_and_never_dispatch(self):
        quest = self.quest()
        quest.race.award(1)
        self.now += 2
        quest.input.metrics.return_value = {"wrong_count": 2, "wrong_events": [{"received_at": 101}, {"received_at": 102}]}
        with patch("herdr_shell.practice.perform") as perform:
            quest.poll_practice()
        perform.assert_not_called()
        self.assertEqual(quest.race.wrong, 2)
        self.assertEqual(quest.race.points, 150)
        self.assertEqual(quest.race.streak, 0)
        quest.input.metrics.return_value = {"wrong_count": 2, "wrong_events": []}
        quest.feedback_metrics()
        self.assertEqual(quest.race.wrong, 2)

    def test_busy_or_blocked_feedback_is_not_accuracy_error(self):
        quest = self.quest()
        quest.input.feedback.return_value = ["Wait for this practice action to finish.", "No challenge is armed."]
        quest.feedback_metrics()
        self.assertEqual(quest.race.wrong, 0)

    def test_wrong_received_after_deadline_is_not_accuracy_error(self):
        quest = self.quest()
        self.now += 121
        quest.input.metrics.return_value = {"wrong_count": 1, "wrong_events": [{"received_at": self.now}]}
        quest.feedback_metrics()
        self.assertEqual(quest.race.wrong, 0)


class FocusAndChoiceTests(TrainerCase):
    def test_return_never_steals_focus_from_real_replaced_or_moved_terminals(self):
        for current in (pane("real", workspace="user-work"), pane("earlier", terminal="replacement", tab="earlier-tab"),
                        pane("earlier", terminal="same-terminal", workspace="user-work", tab="earlier-tab")):
            with self.subTest(current=current):
                quest = self.quest()
                quest.practice_identities = {("earlier", "same-terminal", "practice", "earlier-tab")}
                quest.context.client.snapshot.return_value = {"focused_pane_id": current["pane_id"], "panes": [current]}
                quest.return_guide()
                quest.context.client.call.assert_not_called()

    def test_close_can_return_from_exact_earlier_owned_fixture(self):
        quest = self.quest()
        quest.practice_identities = {("earlier", "same-terminal", "practice", "earlier-tab")}
        quest.context.client.snapshot.return_value = {"focused_pane_id": "earlier", "panes": [pane("earlier", terminal="same-terminal", tab="earlier-tab")]}
        quest.return_guide()
        quest.context.client.call.assert_any_call("workspace.focus", workspace_id="practice")
        quest.context.client.call.assert_any_call("pane.focus", pane_id="guide")

    def test_only_completed_hands_on_unlocks_welcome(self):
        for mode, complete in (("speed", False), ("speed", True), ("hands-on", False), ("hands-on", True)):
            with self.subTest(mode=mode, complete=complete):
                quest = self.quest(mode)
                quest.index = len(quest.deck) if complete else 0
                with patch("herdr_shell.practice.cleanup"), patch("herdr_shell.onboarding.mark_learning_completed") as mark, \
                     patch("herdr_shell.onboarding.welcome", return_value=None) as welcome:
                    self.assertIsNone(quest.activity_choices())
                self.assertEqual(mark.call_count, int(mode == "hands-on" and complete))
                self.assertEqual(welcome.call_args.kwargs["completed"], True if mode == "hands-on" and complete else None)
                self.assertEqual(quest.screen.timeouts[-2:], [-1, 100])
                self.assertEqual(quest.context.client.call.call_args_list[-1].kwargs, {"pane_id": "guide", "mode": "off"})

    def test_completion_save_failure_does_not_block_local_unlocked_choice(self):
        quest = self.quest("hands-on")
        quest.index = len(quest.deck)
        with patch("herdr_shell.practice.cleanup"), \
             patch("herdr_shell.onboarding.mark_learning_completed", side_effect=PermissionError("Read only")), \
             patch("herdr_shell.onboarding.welcome", return_value="game") as welcome:
            self.assertEqual(quest.activity_choices(), "speed")
        self.assertTrue(welcome.call_args.kwargs["completed"])
        self.assertIn("could not save", quest.notice)

    def test_read_only_replay_in_learning_choice_loops_back_to_available_modes(self):
        quest = self.quest("hands-on")
        quest.index = len(quest.deck)
        with patch("herdr_shell.practice.cleanup"), patch("herdr_shell.onboarding.mark_learning_completed"), \
             patch("herdr_shell.onboarding.welcome", return_value="walkthrough"), \
             patch("herdr_shell.onboarding.walkthrough", side_effect=["walkthrough", "hands-on"]) as guide:
            self.assertEqual(quest.activity_choices(), "hands-on")
        self.assertEqual(guide.call_count, 2)

    def test_restart_closes_old_broker_before_launching_fresh_practice_space(self):
        quest = self.quest()
        quest.index = len(quest.deck)
        quest.welcome = Mock(return_value=True)
        quest.finish = Mock(return_value="speed")
        order = []
        quest.input.close.side_effect = lambda: order.append("close")
        with patch("herdr_shell.game_input.Broker") as broker, \
             patch("herdr_shell.trainer.open_game", side_effect=lambda context, mode: order.append("launch")) as launch:
            broker.return_value.__enter__.return_value = quest.input
            quest.run()
        self.assertEqual(order, ["close", "launch"])
        launch.assert_called_once_with(quest.context, mode="speed")
        quest.input.arm.assert_not_called()
        quest.welcome.assert_called_once()

    def test_restart_launch_failure_cannot_resume_closed_broker(self):
        quest = self.quest()
        quest.index = len(quest.deck)
        quest.welcome = Mock(return_value=True)
        quest.finish = Mock(return_value="hands-on")
        with patch("herdr_shell.game_input.Broker") as broker, \
             patch("herdr_shell.trainer.open_game", side_effect=ShellError("New guide could not open")):
            broker.return_value.__enter__.return_value = quest.input
            with self.assertRaisesRegex(ShellError, "New guide could not open"):
                quest.run()
        quest.input.close.assert_called_once()
        quest.input.arm.assert_not_called()
        broker.return_value.__exit__.assert_called_once()

    def test_incomplete_assisted_and_failed_runs_cannot_set_personal_best(self):
        for condition in ("complete", "incomplete", "hinted", "skipped", "failure"):
            with self.subTest(condition=condition):
                quest = self.quest()
                quest.mastered = {m.id for m in missions()}
                if condition == "incomplete":
                    quest.mastered.pop()
                elif condition == "hinted":
                    quest.race.hinted_correct = 1
                elif condition == "skipped":
                    quest.missed = [quest.deck[0]]
                elif condition == "failure":
                    quest.failures = 1
                with patch("herdr_shell.scoring.save_result", return_value={"best": None}) as save:
                    quest.save_score()
                    quest.save_score()
                save.assert_called_once()
                self.assertEqual(save.call_args.kwargs["eligible"], condition == "complete")


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
            self.assertEqual(opening["env"]["HERDR_SHELL_LEARNING_MODE"], "speed")
            self.assertNotIn("HERDR_SHELL_CONTEXT", opening["env"])
            self.assertTrue((Path(root) / "README.txt").exists())
            self.assertEqual(json.loads(opening['env']['HERDR_SHELL_GAME_ROOT'])['terminal_id'], 'quest-terminal')

    def test_invalid_learning_mode_cannot_create_a_workspace(self):
        context = Mock()
        with self.assertRaisesRegex(ShellError, "Unknown learning mode"):
            open_game(context, mode="unknown")
        context.validate.assert_not_called()
        context.client.call.assert_not_called()

    def test_internal_trainer_uses_own_allocated_pane_instead_of_launch_origin(self):
        origin = Mock(workspace="user-work")
        guide = Mock(workspace="quest")
        with patch.dict("os.environ", {"HERDR_PANE_ID": "own-guide", "HERDR_SHELL_GAME_WORKSPACE": "quest"}, clear=True), \
             patch("herdr_shell.trainer.resolve_context", return_value=guide) as resolve, \
             patch("herdr_shell.trainer.curses.wrapper"), patch("herdr_shell.practice.cleanup") as cleanup:
            run_game(origin)
        self.assertEqual(resolve.call_args.args[0].pane, "own-guide")
        self.assertFalse(resolve.call_args.args[0].active)
        origin.client.call.assert_not_called()
        guide.client.call.assert_called_once_with("pane.zoom", pane_id=guide.pane, mode="off")
        cleanup.assert_called_once_with(guide)

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
