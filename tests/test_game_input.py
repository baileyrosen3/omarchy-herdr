"""Real local IPC with private temporary records, never a desktop or Herdr server."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from herdr_shell import game_input
from herdr_shell.runtime import Context, ShellError


class GameFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="herdr-game-input-test-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.socket = str(self.directory / "server.sock")
        self.guide = Context(self.socket, "guide", "keeper", "guide-tab", str(self.directory), "guide-term")
        self.target = Context(self.socket, "dummy", "practice", "dummy-tab", str(self.directory), "dummy-term")
        self.foreign = Context(self.socket, "ordinary", "practice", "dummy-tab", str(self.directory), "ordinary-term")
        self.contexts = {context.pane: context for context in (self.guide, self.target, self.foreign)}
        self.process = SimpleNamespace(pid=os.getpid(), start="12345", state="S", foreground=True,
                                       argv=("python3", "herdr-shell", "_learn"))
        process = patch.object(game_input, "_process", side_effect=lambda pid: self.process)
        process.start()
        self.addCleanup(process.stop)
        self.guide_bound = True
        self.in_guide_impl = game_input._in_guide
        binding = patch.object(game_input, "_in_guide", side_effect=lambda marker, guide: self.guide_bound)
        binding.start()
        self.addCleanup(binding.stop)
        owner = self

        def validate(context):
            if owner.contexts.get(context.pane) != context:
                raise ShellError("The originating terminal changed")
            return game_input._identity(context)

        context = patch.object(Context, "validate", new=validate)
        context.start()
        self.addCleanup(context.stop)
        self.focus = self.guide.pane
        client = SimpleNamespace(snapshot=lambda: {"focused_pane_id": self.focus,
                                  "panes": [game_input._identity(c) for c in self.contexts.values()]})
        peer = patch.object(Context, "client", new=property(lambda unused: client))
        peer.start()
        self.addCleanup(peer.stop)

    def plan(self, action="pane-close", rows=None):
        rows = [game_input._identity(self.guide), game_input._identity(self.target)] if rows is None else rows
        return SimpleNamespace(action=action, guide=self.guide, target=self.target,
                               pane_identities=rows, owned_panes=["guide", "dummy"],
                               owned_tabs=["guide-tab", "dummy-tab"], owned_workspaces=["keeper", "practice"])

    def broker(self):
        broker = game_input.Broker(self.guide)
        broker.__enter__()
        self.addCleanup(broker.close)
        return broker

    def request(self, broker, action="pane-close", context=None):
        return {"nonce": broker.nonce, "action": action, "context": asdict(context or self.guide)}


class GameInputTests(GameFixture, unittest.TestCase):
    def test_real_chord_roundtrip_queues_only_once_until_complete(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            self.assertEqual(game_input.route("pane-close", self.guide)["game"], "queued")
            self.assertEqual(game_input.route("pane-close", self.guide)["game"], "busy")
            self.assertEqual(broker.next_event(), ("pane-close", self.guide))
            self.assertIsNone(broker.next_event())
            broker.complete()
            self.focus = self.target.pane
            self.assertEqual(game_input.route("pane-close", self.target)["game"], "queued")
            self.assertEqual(broker.next_event(), ("pane-close", self.target))

    def test_wrong_chord_is_feedback_and_never_an_action(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            self.assertEqual(game_input.route("agent-new", self.guide)["game"], "wrong")
            self.assertIsNone(broker.next_event())
            self.assertTrue(broker.feedback())
            self.assertEqual(broker.feedback(), [])
            self.assertEqual(game_input.route("pane-close", self.guide)["game"], "queued")

    def test_unowned_pane_in_same_workspace_or_tab_is_outside_game(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            self.assertIsNone(game_input.route("pane-close", self.foreign))
            self.assertIsNone(broker.next_event())

    def test_guide_terminal_replacement_expires_the_record(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            self.contexts["guide"] = Context(self.socket, "guide", "keeper", "guide-tab", str(self.directory), "replacement")
            self.assertIsNone(game_input.route("pane-close", self.target))

    def test_target_terminal_replacement_is_not_intercepted(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            replaced = Context(self.socket, "dummy", "practice", "dummy-tab", str(self.directory), "replacement")
            self.contexts["dummy"] = replaced
            self.assertIsNone(game_input.route("pane-close", replaced))

    def test_queued_terminal_replacement_is_rejected_before_main_thread_action(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            self.assertEqual(game_input.route("pane-close", self.target)["game"], "queued")
            self.contexts.pop("dummy")
            self.assertIsNone(broker.next_event())
            self.assertIn("terminal changed", broker.feedback()[0])

    def test_focus_changed_to_ordinary_pane_drops_queued_action(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            self.assertEqual(game_input.route("pane-close", self.guide)["game"], "queued")
            self.focus = self.foreign.pane
            self.assertIsNone(broker.next_event())
            self.assertTrue(broker.feedback())

    def test_desktop_identity_is_rechecked_on_main_thread_before_action(self):
        identity = {"window_pid": 10, "client_pid": 11, "start": "22", "address": "0xabc"}
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            self.assertEqual(game_input.route("pane-close", self.guide, desktop_identity=identity)["game"], "queued")
            with patch.object(game_input, "_desktop_unchanged", return_value=False) as current:
                self.assertIsNone(broker.next_event())
            current.assert_called_once_with(identity, self.guide)
            self.assertTrue(broker.feedback())

    def test_valid_desktop_identity_keeps_public_event_tuple_unchanged(self):
        identity = {"window_pid": 10, "client_pid": 11, "start": "22", "address": "0xabc"}
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            game_input.route("pane-close", self.guide, desktop_identity=identity)
            with patch.object(game_input, "_desktop_unchanged", return_value=True):
                self.assertEqual(broker.next_event(), ("pane-close", self.guide))

    def test_pid_reuse_stopped_process_or_other_entrypoint_never_intercepts(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            for field, value in (("start", "different"), ("state", "Z"), ("state", "T"),
                                 ("foreground", False), ("argv", ("ordinary-command",))):
                with self.subTest(field=field, value=value):
                    before = getattr(self.process, field)
                    setattr(self.process, field, value)
                    self.assertIsNone(game_input.route("pane-close", self.guide))
                    setattr(self.process, field, before)

    def test_live_pid_in_another_terminal_does_not_intercept(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            self.guide_bound = False
            self.assertIsNone(game_input.route("pane-close", self.guide))

    def test_unarmed_and_disarmed_chords_are_consumed_without_dispatch(self):
        with game_input.Broker(self.guide) as broker:
            self.assertEqual(game_input.route("pane-close", self.guide)["game"], "blocked")
            broker.arm(self.plan())
            game_input.route("pane-close", self.guide)
            broker.disarm()
            self.assertIsNone(broker.next_event())
            self.assertEqual(game_input.route("pane-close", self.target)["game"], "blocked")

    def test_update_after_close_drops_target_and_preserves_guide(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            game_input.route("pane-close", self.guide)
            broker.next_event()
            self.contexts.pop(self.target.pane)
            broker.update(self.plan(rows=[game_input._identity(self.guide)]))
            broker.complete()
            self.assertIsNone(game_input.route("pane-close", self.target))
            self.assertEqual(game_input.route("pane-close", self.guide)["game"], "queued")

    def test_initial_root_is_scoped_before_first_mission(self):
        with game_input.Broker(self.guide, owned_identities=[game_input._identity(self.target)]) as broker:
            self.assertEqual(game_input.route("agent-new", self.target)["game"], "blocked")
            self.assertIsNone(game_input.route("agent-new", self.foreign))
            broker.arm(self.plan())
            self.assertEqual(game_input.route("agent-new", self.target)["game"], "wrong")

    def test_earlier_authenticated_fixture_stays_scoped_across_missions(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan("pane-close"))
            broker.disarm()
            self.assertEqual(game_input.route("agent-new", self.target)["game"], "blocked")
            broker.arm(self.plan("menu", rows=[game_input._identity(self.guide)]))
            self.assertEqual(game_input.route("agent-new", self.target)["game"], "wrong")
            self.assertIsNone(game_input.route("agent-new", self.foreign))

    def test_moved_or_replaced_retained_fixture_is_pruned_before_replay(self):
        for moved in (Context(self.socket, "dummy", "user-space", "user-tab", str(self.directory), "dummy-term"),
                      Context(self.socket, "dummy", "practice", "dummy-tab", str(self.directory), "replacement")):
            with self.subTest(moved=moved):
                self.contexts["dummy"] = self.target
                with game_input.Broker(self.guide) as broker:
                    broker.arm(self.plan())
                    self.contexts["dummy"] = moved
                    broker.arm(self.plan("menu", rows=[game_input._identity(self.guide)]))
                    self.assertIsNone(game_input.route("agent-new", moved))
                    self.assertEqual(broker.scope, [game_input._identity(self.guide)])

    def test_stale_baseline_is_refused_before_marker_or_socket_creation(self):
        self.contexts.pop(self.target.pane)
        with self.assertRaisesRegex(ShellError, "moved or was replaced"):
            with game_input.Broker(self.guide, owned_identities=[game_input._identity(self.target)]):
                self.fail("stale baseline accepted")
        self.assertFalse(game_input._marker_path(self.socket).exists())

    def test_closed_earlier_fixture_is_pruned_on_next_arm(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            self.contexts.pop(self.target.pane)
            broker.arm(self.plan("menu", rows=[game_input._identity(self.guide)]))
            self.assertEqual(broker.scope, [game_input._identity(self.guide)])

    def test_invalid_current_plan_does_not_grant_or_repin_scope(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            changed = game_input._identity(self.target)
            changed["terminal_id"] = "replacement"
            with self.assertRaisesRegex(ShellError, "moved or was replaced"):
                broker.update(self.plan(rows=[changed]))
            self.assertNotIn(changed, broker.scope)

    def test_two_menu_presses_can_use_updated_plan(self):
        with game_input.Broker(self.guide) as broker:
            plan = self.plan("menu")
            broker.arm(plan)
            for unused in range(2):
                self.assertEqual(game_input.route("menu", self.guide)["game"], "queued")
                self.assertEqual(broker.next_event(), ("menu", self.guide))
                broker.update(plan)
                broker.complete()

    def test_scope_rejects_a_pane_not_owned_by_plan(self):
        with game_input.Broker(self.guide) as broker:
            rows = [game_input._identity(self.foreign)]
            with self.assertRaisesRegex(ShellError, "outside"):
                broker.arm(self.plan(rows=rows))

    def test_worker_identity_gate_does_not_call_api_or_execute_an_action(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            with patch.object(Context, "validate", side_effect=AssertionError("worker must not block on API")):
                response = broker._accept(self.request(broker))
            self.assertEqual(response["state"], "queued")
            self.assertEqual(broker.next_event(), ("pane-close", self.guide))

    def test_private_modes_and_cleanup_disable_interception(self):
        broker = self.broker()
        broker.arm(self.plan())
        endpoint, folder, marker = broker.endpoint, broker.directory, broker.path
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(marker.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(endpoint.stat().st_mode & 0o777, 0o600)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        broker.close()
        self.assertFalse(marker.exists())
        self.assertFalse(endpoint.exists())
        self.assertFalse(folder.exists())
        self.assertIsNone(game_input.route("pane-close", self.guide))

    def test_live_owned_guide_with_unavailable_socket_blocks_normal_close(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            broker.endpoint.unlink()
            self.assertEqual(game_input.route("pane-close", self.guide)["game"], "blocked")
            self.process.start = "reused"
            self.assertIsNone(game_input.route("pane-close", self.guide))

    def test_foreign_replacement_marker_is_not_overwritten_or_cleaned(self):
        broker = self.broker()
        broker.arm(self.plan())
        replacement = json.loads(broker.path.read_text())
        replacement["nonce"] = "a" * 48
        broker.path.write_text(json.dumps(replacement))
        with self.assertRaisesRegex(ShellError, "ownership changed"):
            broker.update(self.plan())
        broker.close()
        self.assertEqual(json.loads(broker.path.read_text()), replacement)

    def test_malformed_later_record_is_preserved_and_does_not_intercept(self):
        broker = self.broker()
        broker.path.write_text("[]")
        self.assertIsNone(game_input.route("pane-close", self.guide))
        with self.assertRaisesRegex(ShellError, "later file"):
            broker.arm(self.plan())
        broker.close()
        self.assertEqual(broker.path.read_text(), "[]")

    def test_foreign_socket_replacement_is_preserved(self):
        broker = self.broker()
        broker.arm(self.plan())
        broker.endpoint.unlink()
        broker.endpoint.write_text("personal file")
        self.assertEqual(game_input.route("pane-close", self.guide)["game"], "blocked")
        broker.close()
        self.assertEqual(broker.endpoint.read_text(), "personal file")

    def test_existing_live_broker_refused_without_changing_owner(self):
        with game_input.Broker(self.guide) as first:
            before = first.path.read_text()
            with self.assertRaisesRegex(ShellError, "already open"):
                with game_input.Broker(self.guide):
                    self.fail("second guide started")
            self.assertEqual(first.path.read_text(), before)

    def test_expired_record_can_be_reclaimed_without_old_cleanup_removing_new(self):
        first = self.broker()
        first.arm(self.plan())
        self.process.start = "new-start"
        with game_input.Broker(self.guide) as second:
            second.arm(self.plan())
            first.close()
            self.assertEqual(json.loads(second.path.read_text())["nonce"], second.nonce)
            self.assertEqual(game_input.route("pane-close", self.guide)["game"], "queued")

    def test_public_marker_and_symlink_marker_are_ignored(self):
        broker = self.broker()
        broker.arm(self.plan())
        broker.path.chmod(0o644)
        self.assertIsNone(game_input.route("pane-close", self.guide))
        broker.path.chmod(0o600)
        replacement = self.directory / "replacement.json"
        replacement.write_text(broker.path.read_text())
        broker.path.unlink()
        broker.path.symlink_to(replacement)
        self.assertIsNone(game_input.route("pane-close", self.guide))
        broker.close()
        self.assertTrue(broker.path.is_symlink())

    def test_wrong_nonce_and_foreign_scope_do_not_queue(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            wrong = self.request(broker)
            wrong["nonce"] = "bad"
            self.assertFalse(broker._accept(wrong)["handled"])
            self.assertFalse(broker._accept(self.request(broker, context=self.foreign))["handled"])
            self.assertIsNone(broker.next_event())

    def test_feedback_and_action_queue_are_bounded(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            for unused in range(40):
                broker._accept(self.request(broker, action="agent-new"))
            self.assertEqual(len(broker.feedback()), 16)
            for unused in range(40):
                broker._accept(self.request(broker))
            self.assertEqual(broker.events.qsize(), 1)


class FakeConnection:
    def __init__(self, pid=None, uid=None, response=b"invalid\n"):
        self.pid = os.getpid() if pid is None else pid
        self.uid = os.getuid() if uid is None else uid
        self.response = response
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        pass

    def settimeout(self, timeout):
        assert timeout <= 1

    def connect(self, path):
        pass

    def getsockopt(self, *unused):
        return struct.pack("3i", self.pid, self.uid, os.getgid())

    def sendall(self, raw):
        self.sent.append(raw)

    def recv(self, count):
        response, self.response = self.response[:count], self.response[count:]
        return response


class PeerTests(GameFixture, unittest.TestCase):
    # Keep these cases on the same safe private fixture as the real IPC tests.
    def test_endpoint_peer_pid_or_uid_mismatch_is_not_intercepted(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            for peer in (FakeConnection(pid=os.getpid() + 1), FakeConnection(uid=os.getuid() + 1)):
                with self.subTest(pid=peer.pid, uid=peer.uid), patch.object(game_input.socket, "socket", return_value=peer):
                    self.assertEqual(game_input.route("pane-close", self.guide)["game"], "blocked")
                self.assertEqual(peer.sent, [])

    def test_bad_ack_from_verified_peer_blocks_ordinary_dispatch(self):
        with game_input.Broker(self.guide) as broker:
            broker.arm(self.plan())
            for raw in (b"invalid\n", b"[]\n", b'{"nonce":"wrong","handled":true}\n'):
                with self.subTest(raw=raw), patch.object(game_input.socket, "socket", return_value=FakeConnection(response=raw)):
                    self.assertEqual(game_input.route("pane-close", self.guide)["game"], "blocked")

    def test_incomplete_oversized_or_extra_input_is_rejected(self):
        for raw in (b'{}', b'{}\n{}\n', b'"' + b'x' * game_input._MAX_BYTES + b'"\n'):
            with self.subTest(size=len(raw)):
                with self.assertRaises(ValueError):
                    game_input._receive(FakeConnection(response=raw))

    def test_guide_binding_requires_pid_in_actual_terminal_foreground_processes(self):
        client = SimpleNamespace(call=lambda method, **params: {"process_info": {"foreground_processes": [{"pid": 99}]}})
        with patch.object(Context, "client", new=property(lambda unused: client)):
            self.assertTrue(self.in_guide_impl({"pid": 99}, self.guide))
            self.assertFalse(self.in_guide_impl({"pid": 100}, self.guide))

    def test_actual_desktop_identity_checks_window_client_start_and_socket(self):
        from herdr_shell import desktop
        identity = {"window_pid": 10, "client_pid": 11, "start": "22", "address": "0xabc"}
        process = SimpleNamespace(pid=11, start="22")
        current = {"pid": 10, "address": "0xabc"}
        with patch.object(desktop, "hypr", side_effect=lambda *unused: json.dumps(current)), \
                patch.object(desktop, "find_clients", return_value=[(process, {})]), \
                patch.object(desktop, "socket_for", return_value=self.socket) as target_socket:
            self.assertTrue(game_input._desktop_unchanged(identity, self.guide))
            current["address"] = "0xchanged"
            self.assertFalse(game_input._desktop_unchanged(identity, self.guide))
            current["address"] = "0xabc"
            process.start = "reused"
            self.assertFalse(game_input._desktop_unchanged(identity, self.guide))
            process.start = "22"
            target_socket.return_value = str(self.directory / "foreign.sock")
            self.assertFalse(game_input._desktop_unchanged(identity, self.guide))


if __name__ == "__main__":
    unittest.main()
