import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import desktop_probe_support as support


class ConfirmationReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="herdr-popup-unit-")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.processes = {123: "confirm-tab-close"}

    def observe(self, **changes):
        document = {"pid": 123, "page": "confirm-tab-close", "phase": "awaiting-input",
                    "title": "Close tab 6?", "text": "Closing this tab stops 1 pane.\n"
                    "› Cancel — keep the tab\n↑↓ Select   Enter Confirm   Esc Cancel", "received": []}
        document.update(changes)
        (self.root / "123.json").write_text(json.dumps(document))
        return document

    def test_confirmation_waits_for_real_refreshed_dialog_not_process_page_metadata(self):
        expected = self.observe()

        def acknowledge(predicate, *args, **kwargs):
            self.observe(phase="starting", title="", text="")
            self.assertFalse(predicate(), "The process page exists, but no confirmation has rendered")
            self.observe(title="Could not complete the action", text="snapshot failed\nEnter / Esc Back")
            self.assertFalse(predicate(), "The process page still says confirmation, but an error dialog is visible")
            self.observe()
            self.assertEqual(predicate(), expected, "The real refreshed confirmation is now awaiting input")
            return expected

        with patch.object(support, "eventually", side_effect=acknowledge):
            result = support.wait_for_confirmation_ui(self.root, lambda: self.processes, "confirm-tab-close")
        self.assertEqual(result, expected)

    def test_confirmation_rejects_stale_surface_and_wrong_popup_identity(self):
        def acknowledge(predicate, *args, **kwargs):
            self.assertFalse(predicate(), "An absent surface is not ready")
            for changes in ({"pid": 456}, {"page": "confirm-workspace-close"},
                            {"phase": "received-input"}, {"title": "Close workspace one?"},
                            {"text": "Closing this tab stops 1 pane.\nEsc Cancel"},
                            {"text": "Closing this tab stops 1 pane.\nCancel — keep the tab"}):
                self.observe(**changes)
                self.assertFalse(predicate(), "Wrong/stale/unfinished content cannot authorize Escape")
            expected = self.observe()
            self.processes = {123: "confirm-tab-close", 456: "confirm-tab-close"}
            self.assertFalse(predicate(), "Multiple popup processes are not a safe input target")
            self.processes = {123: "menu"}
            self.assertFalse(predicate(), "A main-menu process cannot authorize confirmation input")
            self.processes = {123: "confirm-tab-close"}
            self.assertEqual(predicate(), expected)
            return expected

        with patch.object(support, "eventually", side_effect=acknowledge):
            support.wait_for_confirmation_ui(self.root, lambda: self.processes, "confirm-tab-close")

    def test_each_close_scope_requires_its_own_visible_confirmation(self):
        scopes = [
            ("confirm-pane-close", "Close pane root?", "Closing this pane stops 1 pane.",
             "Cancel — keep the pane"),
            ("confirm-tab-close", "Close tab 6?", "Closing this tab stops 1 pane.",
             "Cancel — keep the tab"),
            ("confirm-workspace-close", "Close workspace QA?", "Closing this workspace stops 1 pane.",
             "Cancel — keep the workspace"),
        ]
        for page, title, body, cancel in scopes:
            with self.subTest(page=page):
                expected = self.observe(page=page, title=title, text=body + "\n" + cancel + "\nEsc Cancel")
                with patch.object(support, "eventually", side_effect=lambda predicate, *a, **kw: predicate()):
                    self.assertEqual(support.wait_for_confirmation_ui(
                        self.root, lambda: {123: page}, page), expected)

    def test_observer_publishes_actual_refreshed_surface_before_reading_and_records_escape(self):
        from herdr_shell.dialogs import Frame

        lines = ["Closing this tab stops 1 pane.", "Cancel — keep the tab",
                 "   Close tab 6?", "Esc Cancel"]
        ordering = []

        def read_key():
            ordering.append("read")
            observed = support.read_popup_observation(self.root, os.getpid())
            self.assertEqual(observed["phase"], "awaiting-input")
            self.assertEqual(observed["title"], "Close tab 6?")
            self.assertIn("Cancel — keep the tab", observed["text"])
            self.assertEqual(observed["received"], [])
            return "\x1b"

        screen = SimpleNamespace(refresh=lambda: ordering.append("refresh"),
                                 getmaxyx=lambda: (len(lines), 100),
                                 instr=lambda row, column, width: lines[row][column:column + width].encode(),
                                 get_wch=read_key)
        frame = SimpleNamespace(menu=SimpleNamespace(screen=screen), small=False, y=0, x=0, inner=94)
        with patch.dict(os.environ, HERDR_SHELL_PAGE="confirm-tab-close"), patch.object(Frame, "key", Frame.key):
            support.install_popup_observer(self.root)
            self.assertEqual(Frame.key(frame), "\x1b")
        self.assertEqual(ordering, ["refresh", "read"])
        observed = support.read_popup_observation(self.root, os.getpid())
        self.assertEqual(observed["phase"], "received-input")
        self.assertEqual(observed["page"], "confirm-tab-close")
        self.assertEqual([event["key"] for event in observed["received"]], ["\x1b"])


if __name__ == "__main__":
    unittest.main()
