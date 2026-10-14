"""Rendered client focus must acknowledge the requested terminal-input target."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from tests import probe_support, trainer_probe


class FocusAcknowledgementTests(unittest.TestCase):
    def test_trainer_waits_for_destination_after_guide_loses_focus(self):
        context = SimpleNamespace(pane="w2:p1", workspace="w2", client=Mock(),
                                  validate=Mock(return_value={}))
        guide = SimpleNamespace(pane="w2:p2")
        tmux = probe_support.TmuxSession("/fixture/tmux.sock", "quest:0.0", env={})
        # Herdr's border title is space-padded; only the focused title is bold.
        # Neither a stale workspace nor an unfocused destination acknowledges it.
        frames = [
            "┌\x1b[1m Probe w1:p1 \x1b[0m────┐\n",
            "┌ Probe w2:p1 ────┐┌ Herdr Learning ────┐\n"
            "┌\x1b[1m Probe w2:p3 \x1b[0m────┐\n",
            "┌\x1b[1m Probe w2:p1 extra \x1b[0m────┐┌ Herdr Learning ────┐\n",
            "┌\x1b[1m Probe w2:p1 \x1b[0m────┐┌ Herdr Learning ────┐\n",
        ]

        def acknowledge(predicate, *args, **kwargs):
            self.assertFalse(predicate(), "Guide lost focus, but the destination is not rendered")
            self.assertFalse(predicate(), "Destination is rendered, but another pane is focused")
            self.assertFalse(predicate(), "A longer pane title is not the exact destination")
            self.assertTrue(predicate(), "The exact destination title is now focused")
            return True

        with patch.object(tmux, "styled_screen", side_effect=frames), \
                patch.object(probe_support, "eventually", side_effect=acknowledge):
            trainer_probe.focus(context, tmux, guide)
        context.validate.assert_called_once_with()
        self.assertEqual(context.client.call.call_args_list, [
            call("pane.rename", pane_id="w2:p1", label="Probe w2:p1"),
            call("workspace.focus", workspace_id="w2"),
            call("pane.focus", pane_id="w2:p1"),
        ])

    def test_focused_title_still_waits_for_popup_absence(self):
        tmux = probe_support.TmuxSession("/fixture/tmux.sock", "quest:0.0", env={})

        def acknowledge(predicate, *args, **kwargs):
            self.assertFalse(predicate(), "The destination is focused, but its popup remains")
            self.assertTrue(predicate(), "The popup is gone and the destination is focused")
            return True

        with patch.object(tmux, "styled_screen", return_value="┌\x1b[1m Probe w2:p1 \x1b[0m────┐"), \
                patch.object(tmux, "screen", side_effect=["┌Herdr Shell───┐", "┌ Probe w2:p1 ───┐"]), \
                patch.object(probe_support, "eventually", side_effect=acknowledge):
            self.assertTrue(tmux.wait_focus("Probe w2:p1", popup_absent="┌Herdr Shell"))


if __name__ == "__main__":
    unittest.main()
