from argparse import Namespace
import json
import os
import unittest
from unittest.mock import patch

from herdr_shell.runtime import Context, ShellError, resolve_context, resolve_socket

PANE = {"pane_id": "p-source", "workspace_id": "w-source", "tab_id": "t-source",
        "terminal_id": "term-source", "cwd": "/tmp/a project; literal"}


class ContextTests(unittest.TestCase):
    def test_popup_uses_captured_original_pane(self):
        env = {"HERDR_SOCKET_PATH": "/tmp/socket", "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(
            {"focused_pane_id": "p-source"}), "HERDR_PANE_ID": "popup"}
        with patch.dict(os.environ, env, clear=True), patch("herdr_shell.runtime.Client.call", return_value={"pane": PANE}) as call:
            context = resolve_context()
        call.assert_called_once_with("pane.get", pane_id="p-source")
        self.assertEqual(context.cwd, "/tmp/a project; literal")

    def test_explicit_session_never_reuses_inherited_pane(self):
        env = {"HERDR_SOCKET_PATH": "/tmp/old", "HERDR_PANE_ID": "old-pane"}
        with patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(ShellError, "Choose --pane"):
            resolve_context(Namespace(session="other", pane=None, socket=None, active=False))

    def test_explicit_target_overrides_saved_popup_context(self):
        old = Context('/tmp/old', 'old-pane', 'old-workspace', 'old-tab', '/tmp', 'old-term')
        with patch.dict(os.environ, {'HERDR_SHELL_CONTEXT': old.encode()}, clear=True), patch(
                "herdr_shell.runtime.Client.call", return_value={"pane": PANE}) as call:
            context = resolve_context(Namespace(socket='/tmp/new', session=None, pane='p-source', active=False))
        self.assertEqual(context.socket, '/tmp/new')
        call.assert_called_once_with("pane.get", pane_id="p-source")

    def test_config_override_does_not_change_socket_location(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': '/tmp/xdg', 'HERDR_CONFIG_PATH': '/tmp/custom.toml'}, clear=True):
            path = resolve_socket(Namespace(session='test', socket=None))
        self.assertEqual(path, '/tmp/xdg/herdr/sessions/test/herdr.sock')

    def test_replaced_or_moved_origin_is_rejected(self):
        context = Context('/tmp/socket', 'p-source', 'w-source', 't-source', '/tmp', 'term-source')
        for changed in ({**PANE, 'terminal_id': 'replacement'}, {**PANE, 'tab_id': 'moved'}):
            with patch("herdr_shell.runtime.Client.call", return_value={"pane": changed}), self.assertRaises(ShellError):
                context.validate()


if __name__ == '__main__':
    unittest.main()
