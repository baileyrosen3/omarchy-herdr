from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from herdr_shell.desktop import MAPPINGS
from herdr_shell.runtime import Context, ShellError
from herdr_shell.trainer import Mission, Objective, Quest, missions, normalize_answer, open_game, run_game


def pane(pid, *, terminal=None, tab="t", workspace="w"):
    return {"pane_id": pid, "terminal_id": terminal or "term-" + pid,
            "tab_id": tab, "workspace_id": workspace}


def snapshot(*panes, focus="p1", workspace="w", tab="t", rects=None):
    return {"panes": list(panes) or [pane("p1")], "tabs": [{"tab_id": tab, "workspace_id": workspace}],
            "layouts": [{"tab_id": tab, "panes": [{"pane_id": pid, "rect": rect}
                                                    for pid, rect in (rects or {"p1": {"x": 0, "y": 0}}).items()]}],
            "focused_pane_id": focus, "focused_workspace_id": workspace, "focused_tab_id": tab}


def leaf(pid):
    return {"type": "pane", "pane_id": pid}


def split(first="p1", second="p2", direction="right", ratio=.5):
    return {"root": {"type": "split", "direction": direction, "ratio": ratio,
                     "first": leaf(first), "second": leaf(second)}, "zoomed": False}


class RecallTests(unittest.TestCase):
    def test_all_registered_shortcuts_have_exactly_one_recall_challenge(self):
        questions = [m for m in missions() if not m.live and not m.options]
        self.assertEqual({m.id for m in questions}, {action for _, action, _ in MAPPINGS})
        self.assertEqual(len(questions), len(MAPPINGS))
        self.assertEqual(len([m for m in missions() if m.live]), 8)

    def test_answers_accept_full_chords_case_and_page_aliases(self):
        for answer in ("Shift+PageDown", "super + alt + shift + page_down", "Alt+Super+SHIFT+PgDn"):
            self.assertEqual(normalize_answer(answer), "shift+pagedown")
        self.assertEqual(normalize_answer("SUPER+ALT+R"), "r")
        self.assertNotEqual(normalize_answer("PageDown"), normalize_answer("Shift+PageDown"))
        self.assertNotEqual(normalize_answer("Alt+R"), normalize_answer("R"))

    def test_risky_actions_are_recall_only(self):
        live = {m.id.removeprefix("live-") for m in missions() if m.live}
        self.assertFalse(live & {"pane-close", "tab-close", "workspace-close", "workspace-new", "agent-new",
                                 "agent-cycle-next", "agent-cycle-previous", "launch-git"})


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.context = Context("socket", "p1", "w", "t", "/tmp", "term-p1")

    def objective(self, action, before, layout=None):
        return Objective(Mission("live-" + action, "Round", "Prompt", "Key", "Hint", live=True),
                         self.context, before, layout)

    def test_directional_split_requires_correct_position_and_new_focus(self):
        for direction, rect in (("right", {"x": 10, "y": 0}), ("left", {"x": -10, "y": 0}),
                                ("up", {"x": 0, "y": -10}), ("down", {"x": 0, "y": 10})):
            objective = self.objective("pane-split-" + direction, snapshot())
            after = snapshot(pane("p1"), pane("p2"), focus="p2", rects={"p1": {"x": 0, "y": 0}, "p2": rect})
            layout = split("p2", "p1") if direction in ("left", "up") else split()
            layout["root"]["direction"] = "right" if direction in ("left", "right") else "down"
            self.assertTrue(objective.verified(after, layout), direction)
            wrong = deepcopy(after)
            wrong["focused_pane_id"] = "p1"
            self.assertFalse(objective.verified(wrong, layout))
            wrong = deepcopy(layout)
            wrong["root"]["first"], wrong["root"]["second"] = wrong["root"]["second"], wrong["root"]["first"]
            self.assertFalse(objective.verified(after, wrong))

    def test_split_on_another_pane_does_not_pass_even_on_the_expected_side(self):
        before = snapshot(pane("p1"), pane("p2"))
        objective = self.objective("pane-split-left", before)
        after = snapshot(pane("p1"), pane("p2"), pane("p3"), focus="p3",
                         rects={"p1": {"x": 10, "y": 0}, "p2": {"x": 0, "y": 0}, "p3": {"x": 5, "y": 0}})
        # Splitting p2 right happens to put p3 left of p1. It is not p1's split.
        layout = split("p2", "p3")
        layout = {"root": {"type": "split", "direction": "right", "ratio": .5,
                           "first": layout["root"], "second": leaf("p1")}}
        self.assertFalse(objective.verified(after, layout))

    def test_external_workspace_and_replaced_guide_never_pass(self):
        objective = self.objective("pane-split-right", snapshot())
        after = snapshot(pane("p1"), pane("p2"), focus="p2", rects={"p1": {"x": 0, "y": 0}, "p2": {"x": 10, "y": 0}})
        external = deepcopy(after)
        external["focused_workspace_id"] = "user-work"
        self.assertFalse(objective.verified(external, split()))
        replaced = deepcopy(after)
        replaced["panes"][0]["terminal_id"] = "different-terminal"
        self.assertFalse(objective.verified(replaced, split()))

    def test_rotation_preserves_pair_identity_ratio_and_existing_terminals(self):
        before = snapshot(pane("p1"), pane("p2"))
        objective = self.objective("pane-rotate", before, split())
        self.assertTrue(objective.verified(before, split(direction="down")))
        self.assertFalse(objective.verified(before, split(direction="down", ratio=.7)))
        self.assertFalse(objective.verified(before, split(second="other", direction="down")))
        self.assertFalse(objective.verified(before, split()))
        replaced = deepcopy(before)
        replaced["panes"][1]["terminal_id"] = "new-term"
        self.assertFalse(objective.verified(replaced, split(direction="down")))

    def test_zoom_requires_real_toggle_on_guide_and_same_panes(self):
        before = snapshot(pane("p1"), pane("p2"))
        objective = self.objective("pane-zoom", before, split())
        self.assertTrue(objective.verified(before, {**split(), "zoomed": True}))
        self.assertFalse(objective.verified(before, split()))
        wrong = deepcopy(before)
        wrong["focused_pane_id"] = "p2"
        self.assertFalse(objective.verified(wrong, {**split(), "zoomed": True}))

    def test_cycle_requires_next_pane_in_exact_same_tab(self):
        before = snapshot(pane("p1"), pane("p2"), pane("p3"))
        objective = self.objective("pane-cycle-next", before)
        correct = deepcopy(before)
        correct["focused_pane_id"] = "p2"
        self.assertTrue(objective.verified(correct))
        correct["focused_pane_id"] = "p3"
        self.assertFalse(objective.verified(correct))
        correct["focused_pane_id"] = "unrelated"
        self.assertFalse(objective.verified(correct))

    def test_new_tab_requires_local_addition_focus_and_no_deleted_tabs(self):
        before = snapshot()
        objective = self.objective("tab-new", before)
        after = deepcopy(before)
        after["tabs"].append({"tab_id": "new", "workspace_id": "w"})
        after["focused_tab_id"] = "new"
        self.assertTrue(objective.verified(after))
        after["tabs"] = after["tabs"][1:]
        self.assertFalse(objective.verified(after))
        after = deepcopy(before)
        after["tabs"].append({"tab_id": "other", "workspace_id": "user-work"})
        after["focused_tab_id"] = "other"
        self.assertFalse(objective.verified(after))


class LifecycleTests(unittest.TestCase):
    def test_live_completion_waits_for_stable_state_before_returning_focus(self):
        quest = Quest.__new__(Quest)
        quest.context = Mock(pane="guide", workspace="quest")
        quest.context.client.snapshot.return_value = {"focused_workspace_id": "quest"}
        quest.objective = Mock(action="pane-cycle-next")
        quest.objective.verified.return_value = True
        quest.stable, quest.award = 0, Mock()
        quest.poll_practice()
        quest.context.client.call.assert_not_called()
        quest.award.assert_not_called()
        quest.poll_practice()
        self.assertEqual(quest.context.client.call.call_args_list[0].args, ("pane.focus",))
        self.assertEqual(quest.context.client.call.call_args_list[0].kwargs, {"pane_id": "guide"})
        quest.award.assert_called_once()

    def test_unavailable_live_key_cannot_arm_or_change_zoom(self):
        quest = Quest.__new__(Quest)
        quest.context = Mock()
        mission = Mission("live-pane-split-right", "Round", "Prompt", "R", "Hint", live=True)
        with patch("herdr_shell.desktop.effective_bindings", return_value={"pane-split-right": {"active": False, "reason": "Reserved by desktop"}}):
            with self.assertRaisesRegex(ShellError, "Reserved by desktop"):
                quest.start_practice(mission)
        quest.context.client.call.assert_not_called()
        quest.context.validate.assert_not_called()

    def test_hidden_question_cannot_arm_skip_or_award(self):
        quest = Quest.__new__(Quest)
        quest.screen = Mock()
        quest.screen.getmaxyx.return_value = (10, 25)
        quest.deck = [Mission("live-pane-split-right", "Round", "Prompt", "R", "Hint", live=True)]
        quest.index, quest.active = 0, False
        quest.welcome, quest.draw = Mock(return_value=True), Mock()
        quest.key = Mock(side_effect=["\n", __import__("curses").KEY_F3, "\x1b"])
        quest.start_practice, quest.award, quest.advance = Mock(), Mock(), Mock()
        quest.run()
        quest.start_practice.assert_not_called()
        quest.award.assert_not_called()
        quest.advance.assert_not_called()

    def test_tiny_welcome_requires_resize_before_play(self):
        quest = Quest.__new__(Quest)
        quest.screen = Mock()
        quest.screen.getmaxyx.return_value = (10, 25)
        quest.page, quest.scroll_key = Mock(), Mock(return_value=False)
        quest.context = Mock()
        quest.key = Mock(side_effect=["\n", "\x1b"])
        self.assertFalse(quest.welcome())

    def test_launch_uses_only_new_workspace_and_regular_plugin_pane(self):
        context = Mock()
        context.workspace = "user-work"
        context.client.call.side_effect = [
            {"workspace": {"workspace_id": "quest"}, "root_pane": {"pane_id": "quest-root"}},
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

    def test_failed_launch_leaves_new_workspace_and_never_closes_work(self):
        context = Mock()
        context.workspace = "user-work"
        context.client.call.side_effect = [
            {"workspace": {"workspace_id": "quest"}, "root_pane": {"pane_id": "quest-root"}},
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


if __name__ == "__main__":
    unittest.main()
