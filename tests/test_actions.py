from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from herdr_shell.actions import execute, open_ui
from herdr_shell.closing import close_plan, execute_close, shell_jobs
from herdr_shell.navigation import cycle_agent, cycle_pane
from herdr_shell.rotation import rotate_pane, rotation_unavailable
from herdr_shell.runtime import ShellError


def pane(pid, tab="t", workspace="w", **extra):
    return dict(pane_id=pid, terminal_id="term-" + pid, workspace_id=workspace, tab_id=tab, **extra)


class FakeClient:
    def __init__(self):
        self.data = {"panes": [pane("p1"), pane("p2")], "agents": [],
                     "workspaces": [{"workspace_id": "w", "label": "Work"}],
                     "tabs": [{"tab_id": "t", "workspace_id": "w", "label": "Main"}],
                     "focused_pane_id": "p1"}
        self.calls = []
        self.processes = {}

    def snapshot(self):
        return deepcopy(self.data)

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "pane.process_info":
            return {"process_info": self.processes.get(params["pane_id"],
                    {"shell_pid": 10, "foreground_process_group_id": 10,
                     "foreground_processes": [{"pid": 10, "name": "bash"}]})}
        if method == "agent.get":
            return {"agent": deepcopy(next(a for a in self.data["agents"] if a["pane_id"] == params["target"]))}
        if method in ("pane.focus", "agent.focus"):
            target = params.get("pane_id") or params["target"]
            self.data["focused_pane_id"] = target
            for agent in self.data["agents"]:
                if agent["pane_id"] == target and agent["agent_status"] == "done":
                    agent["agent_status"] = "idle"
            return {"changed": True}
        if method.endswith(".close"):
            return {"closed": True}
        return {}


class FakeContext:
    def __init__(self, client, pid="p1"):
        p = next(p for p in client.data["panes"] if p["pane_id"] == pid)
        self.socket, self.pane, self.workspace, self.tab = "test-socket", pid, p["workspace_id"], p["tab_id"]
        self.terminal, self.cwd, self.client = p["terminal_id"], "/tmp", client

    def validate(self):
        if not any(p["pane_id"] == self.pane and p["terminal_id"] == self.terminal and
                   p["workspace_id"] == self.workspace and p["tab_id"] == self.tab for p in self.client.data["panes"]):
            raise ShellError("origin changed")

    def encode(self):
        return "saved-context"


class SplitSwapTests(unittest.TestCase):
    def test_directional_swap_uses_directional_api_target(self):
        for direction in ("up", "down", "left", "right"):
            client = FakeClient()
            execute("pane-swap-" + direction, FakeContext(client))
            self.assertEqual(client.calls[-1], ("pane.swap", {"pane_id": "p1", "direction": direction}))

    def test_left_and_up_splits_read_nested_swap_result_and_keep_new_focus(self):
        for direction, native in (("left", "right"), ("up", "down")):
            client = FakeClient()
            def call(method, **params):
                client.calls.append((method, params))
                if method == "pane.split":
                    return {"pane": pane("created")}
                if method == "pane.swap":
                    return {"swap": {"changed": True}}
                return {}
            client.call = call
            result = execute("pane-split-" + direction, FakeContext(client))
            self.assertEqual(result["pane"]["pane_id"], "created")
            self.assertEqual(client.calls[0][1]["direction"], native)
            self.assertEqual(client.calls[1], ("pane.swap", {"source_pane_id": "created", "target_pane_id": "p1"}))
            self.assertEqual(client.calls[2], ("pane.focus", {"pane_id": "created"}))

    def test_failed_sibling_swap_reports_created_pane(self):
        client = FakeClient()
        client.call = Mock(side_effect=[{"pane": pane("created")}, {"swap": {"changed": False}}])
        with self.assertRaisesRegex(ShellError, "Pane created was created down, but could not be moved up"):
            execute("pane-split-up", FakeContext(client))


class CloseTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.context = FakeContext(self.client)
        self.jobs = patch("herdr_shell.closing.shell_jobs", return_value=[])
        self.jobs_mock = self.jobs.start()
        self.addCleanup(self.jobs.stop)
        self.desktop_env = patch("herdr_shell.desktop.popup_environment", return_value={})
        self.desktop_env.start()
        self.addCleanup(self.desktop_env.stop)

    def test_clean_shell_scope_closes_without_popup(self):
        for action, method, target in (("pane-close", "pane.close", "pane_id"),
                                      ("tab-close", "tab.close", "tab_id"),
                                      ("workspace-close", "workspace.close", "workspace_id")):
            plan = close_plan(action, self.context)
            self.assertFalse(plan["needs_confirmation"])
            execute(action, self.context)
            self.assertEqual(self.client.calls[-1][0], method)
            self.assertEqual(set(self.client.calls[-1][1]), {target})

    def test_agents_even_idle_and_done_require_confirmation(self):
        for status in ("idle", "done", "working", "blocked", "unknown"):
            self.client.data["agents"] = [pane("p1", agent="codex", agent_status=status)]
            plan = close_plan("pane-close", self.context)
            self.assertTrue(plan["needs_confirmation"])
            self.assertIn(status, plan["activity"][0]["reason"])
            self.client.calls.clear()
            execute("pane-close", self.context)
            method, params = self.client.calls[-1]
            self.assertEqual(method, "plugin.pane.open")
            saved = json.loads(params["env"]["HERDR_SHELL_CLOSE_PLAN"])
            self.assertEqual(saved["pane_identities"], plan["pane_identities"])
            self.assertFalse(any(m == "pane.close" for m, _ in self.client.calls))

    def test_running_and_unknown_process_activity_require_confirmation(self):
        for info in ({"shell_pid": 10, "foreground_process_group_id": 12,
                      "foreground_processes": [{"pid": 12, "name": "nvim"}]}, {}):
            self.client.processes["p1"] = info
            self.assertTrue(close_plan("pane-close", self.context)["needs_confirmation"])

    def test_process_api_failure_requires_confirmation(self):
        original = self.client.call
        def unavailable(method, **params):
            if method == "pane.process_info":
                raise ShellError("Process inspection unavailable")
            return original(method, **params)
        self.client.call = unavailable
        self.assertTrue(close_plan("pane-close", self.context)["needs_confirmation"])

    def test_confirmed_plan_still_rejects_another_target(self):
        plan = close_plan("pane-close", self.context)
        plan.update(target_id="p2", needs_confirmation=True, confirmed=True)
        with self.assertRaisesRegex(ShellError, "affected by closing changed"):
            execute_close(plan, self.context)
        self.assertFalse(any(m.endswith(".close") for m, _ in self.client.calls))

    def test_explicit_yes_bypasses_prompt_but_keeps_identity_validation(self):
        self.client.data["agents"] = [pane("p1", agent="codex", agent_status="done")]
        execute("pane-close", self.context, yes=True)
        self.assertEqual(self.client.calls[-1], ("pane.close", {"pane_id": "p1"}))

    def test_confirmed_scope_rejects_new_moved_and_replaced_panes(self):
        for mutate in (lambda c: c.data["panes"].append(pane("p3")),
                       lambda c: c.data["panes"][1].update(terminal_id="replacement"),
                       lambda c: c.data["panes"][1].update(tab_id="another")):
            client = FakeClient(); context = FakeContext(client)
            plan = close_plan("tab-close", context)
            plan["needs_confirmation"] = True
            mutate(client)
            with self.assertRaisesRegex(ShellError, "affected by closing changed"):
                execute_close(plan, context)
            self.assertFalse(any(m.endswith(".close") for m, _ in client.calls))

    def test_workspace_close_rejects_new_even_empty_tab(self):
        plan = close_plan("workspace-close", self.context)
        self.client.data["tabs"].append({"tab_id": "new", "workspace_id": "w"})
        with self.assertRaisesRegex(ShellError, "affected by closing changed"):
            execute_close(plan, self.context)

    def test_unconfirmed_plan_rejects_newly_started_command(self):
        plan = close_plan("pane-close", self.context)
        self.client.processes["p1"] = {}
        with self.assertRaisesRegex(ShellError, "command or agent started"):
            execute_close(plan, self.context)

    def test_shell_background_and_suspended_work_requires_confirmation(self):
        self.jobs_mock.return_value = ["Background job sleep", "Suspended job nvim"]
        plan = close_plan("pane-close", self.context)
        self.assertTrue(plan["needs_confirmation"])
        self.assertIn("Background job sleep", plan["activity"][0]["reason"])
        self.assertIn("Suspended job nvim", plan["activity"][0]["reason"])

    def test_background_tree_access_failure_requires_confirmation(self):
        self.jobs_mock.side_effect = PermissionError("restricted proc")
        plan = close_plan("pane-close", self.context)
        self.assertTrue(plan["needs_confirmation"])
        self.assertIn("could not be verified", plan["activity"][0]["reason"])

    def test_background_work_started_after_auto_plan_refuses_close(self):
        plan = close_plan("pane-close", self.context)
        self.jobs_mock.return_value = ["Background job sleep"]
        with self.assertRaisesRegex(ShellError, "command or agent started"):
            execute_close(plan, self.context)

    def test_popup_environment_cannot_overwrite_captured_origin(self):
        open_ui(self.context, "settings", env={"HERDR_SHELL_CONTEXT": "wrong", "extra": "ok"})
        env = self.client.calls[-1][1]["env"]
        self.assertEqual(env["HERDR_SHELL_CONTEXT"], "saved-context")
        self.assertEqual(env["extra"], "ok")

    def test_popup_environment_keeps_native_target_and_fixed_desktop_scope_separate(self):
        from herdr_shell.config import ConfigStore
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            compositor = Path(directory) / "compositor"
            with patch("herdr_shell.desktop.popup_environment", return_value={"HERDR_SHELL_DESKTOP_CONFIG_HOME": str(compositor)}):
                open_ui(self.context, "settings", env={"XDG_CONFIG_HOME": str(target),
                        "HERDR_SHELL_DESKTOP_CONFIG_HOME": "wrong", "HERDR_SHELL_CONTEXT": "wrong", "HERDR_SHELL_PAGE": "wrong"})
            environment = self.client.calls[-1][1]["env"]
            self.assertEqual(environment["XDG_CONFIG_HOME"], str(target))
            self.assertEqual(environment["HERDR_SHELL_DESKTOP_CONFIG_HOME"], str(compositor))
            self.assertEqual(environment["HERDR_SHELL_CONTEXT"], self.context.encode())
            self.assertEqual(environment["HERDR_SHELL_PAGE"], "settings")
            with patch.dict(os.environ, {**environment, "HERDR_CONFIG_PATH": ""}):
                self.assertEqual(ConfigStore().path, target / "herdr/config.toml")


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, XDG_STATE_HOME=self.temp.name)
        self.env.start(); self.addCleanup(self.env.stop)
        self.client = FakeClient()

    def test_pane_cycle_stays_in_originating_tab_and_wraps(self):
        self.client.data["panes"].append(pane("other", tab="t2"))
        cycle_pane(FakeContext(self.client), -1)
        self.assertEqual(self.client.data["focused_pane_id"], "p2")
        cycle_pane(FakeContext(self.client, "p2"), 1)
        self.assertEqual(self.client.data["focused_pane_id"], "p1")

    def agents(self):
        self.client.data["panes"] = [pane(p) for p in ("origin", "idle", "working", "done", "blocked", "unknown")]
        self.client.data["agents"] = [pane(status, agent="codex", agent_status=status)
                                       for status in ("idle", "working", "done", "blocked", "unknown")]

    def test_agent_priority_sweep_survives_done_becoming_idle(self):
        self.agents(); current = "origin"; selected = []
        for _ in range(5):
            cycle_agent(FakeContext(self.client, current), 1)
            current = self.client.data["focused_pane_id"]; selected.append(current)
        self.assertEqual(selected, ["blocked", "done", "working", "idle", "blocked"])
        self.assertEqual(next(a for a in self.client.data["agents"] if a["pane_id"] == "done")["agent_status"], "idle")

    def test_reverse_uses_same_frozen_sweep(self):
        self.agents()
        cycle_agent(FakeContext(self.client, "origin"), 1)
        cycle_agent(FakeContext(self.client, "blocked"), 1)
        cycle_agent(FakeContext(self.client, "done"), -1)
        self.assertEqual(self.client.data["focused_pane_id"], "blocked")

    def test_unknown_only_agents_are_not_reported_as_idle(self):
        self.agents()
        self.client.data["agents"] = [a for a in self.client.data["agents"] if a["agent_status"] == "unknown"]
        with self.assertRaisesRegex(ShellError, "Unknown agents"):
            cycle_agent(FakeContext(self.client, "origin"), 1)
        self.assertFalse(any(m == "agent.focus" for m, _ in self.client.calls))

    def test_replaced_agent_is_not_focused_from_persisted_order(self):
        self.agents()
        cycle_agent(FakeContext(self.client, "origin"), 1)
        next(a for a in self.client.data["agents"] if a["pane_id"] == "done")["terminal_id"] = "new-agent"
        cycle_agent(FakeContext(self.client, "blocked"), 1)
        self.assertEqual(self.client.data["focused_pane_id"], "working")


class ShellJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        task = self.proc / "100" / "task" / "100"
        task.mkdir(parents=True)
        (task / "children").write_text("101 102 103 104 105")

    def child(self, pid, state="S", parent=100, name="sleep"):
        folder = self.proc / str(pid); folder.mkdir(exist_ok=True)
        (folder / "stat").write_text(f"{pid} ({name}) {state} {parent} 0 0 0")

    def test_live_stopped_jobs_detected_but_dead_and_reparented_children_ignored(self):
        self.child(101, name="sleep")
        self.child(102, state="T", name="name with ) parentheses")
        self.child(103, state="Z")
        self.child(104, parent=1)
        self.assertEqual(shell_jobs(100, self.proc), ["Background job sleep", "Suspended job name with ) parentheses"])

    def test_missing_children_list_is_unverified(self):
        with self.assertRaises(FileNotFoundError):
            shell_jobs(999, self.proc)

    def test_invalid_child_entry_is_not_treated_as_clean(self):
        (self.proc / "100/task/100/children").write_text("invalid")
        with self.assertRaises(ShellError):
            shell_jobs(100, self.proc)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, XDG_STATE_HOME=self.temp.name)
        self.env.start(); self.addCleanup(self.env.stop)
        self.client = FakeClient(); self.context = FakeContext(self.client)
        self.root = {"type": "split", "direction": "right", "ratio": .37,
                     "first": {"type": "pane", "pane_id": "p1"},
                     "second": {"type": "pane", "pane_id": "p2"}}

    def test_nested_sibling_rejected_before_mutation_and_hint_explains(self):
        root = deepcopy(self.root)
        root["second"] = {"type": "split", "direction": "down", "ratio": .5,
                          "first": {"type": "pane", "pane_id": "p2"},
                          "second": {"type": "pane", "pane_id": "p3"}}
        self.client.call = Mock(return_value={"layout": {"root": root}})
        self.assertIn("nested", rotation_unavailable(self.context))
        with self.assertRaisesRegex(ShellError, "nested pane group"):
            rotate_pane(self.context)
        self.assertTrue(all(c.args[0] == "layout.export" for c in self.client.call.call_args_list))

    def test_rotation_uses_identity_preserving_move_with_original_ratio(self):
        final = deepcopy(self.root); final["direction"] = "down"
        exports = iter([self.root, self.root, final])
        calls = []
        def call(method, **params):
            calls.append((method, params))
            if method == "layout.export":
                return {"layout": {"root": deepcopy(next(exports)), "focused_pane_id": "p1", "zoomed": False}}
            if method == "pane.move":
                return {"move_result": {"pane": pane("p2")}}
            return {}
        self.client.call = call
        result = rotate_pane(self.context)
        moves = [p for m, p in calls if m == "pane.move"]
        self.assertEqual(len(moves), 2)
        self.assertEqual(moves[0]["destination"]["type"], "new_tab")
        self.assertEqual(moves[1]["destination"], {"type": "tab", "tab_id": "t", "target_pane_id": "p1", "split": "down", "ratio": .37})
        self.assertFalse(any(m == "layout.apply" for m, _ in calls))
        self.assertTrue(result["changed"])

    def test_return_failure_restores_original_orientation_without_closing(self):
        calls = []; moves = 0
        def call(method, **params):
            nonlocal moves
            calls.append((method, params))
            if method == "layout.export":
                return {"layout": {"root": deepcopy(self.root), "focused_pane_id": "p1"}}
            if method == "pane.move":
                moves += 1
                if moves == 2:
                    raise ShellError("temporary failure")
                return {"move_result": {"pane": pane("p2")}}
            return {}
        self.client.call = call
        with self.assertRaisesRegex(ShellError, "original split was restored"):
            rotate_pane(self.context)
        self.assertEqual([p for m, p in calls if m == "pane.move"][-1]["destination"]["split"], "right")
        self.assertFalse(any(m.endswith(".close") for m, _ in calls))

    def test_zoomed_rotation_exposes_layout_then_restores_zoom_without_replacing_terminals(self):
        current, zoomed, calls = deepcopy(self.root), True, []
        def call(method, **params):
            nonlocal zoomed
            calls.append((method, params))
            if method == "layout.export":
                return {"layout": {"root": deepcopy(current), "focused_pane_id": "p1", "zoomed": zoomed}}
            if method == "pane.zoom":
                zoomed = params["mode"] == "on"
            if method == "pane.move":
                if params["destination"]["type"] == "tab" and not zoomed:
                    current["direction"] = params["destination"]["split"]
                return {"move_result": {"pane": pane("p2")}}
            return {}
        self.client.call = call
        before = self.client.snapshot()["panes"]
        self.assertTrue(rotate_pane(self.context)["changed"])
        self.assertEqual(current["direction"], "down")
        self.assertEqual(current["ratio"], .37)
        self.assertTrue(zoomed)
        self.assertEqual(self.client.snapshot()["panes"], before)
        self.assertEqual([p["mode"] for m, p in calls if m == "pane.zoom"], ["off", "on"])
        self.assertFalse(any(m.endswith(".close") or m == "layout.apply" for m, _ in calls))

    def test_failed_staging_restores_captured_zoom(self):
        zoomed = True
        def call(method, **params):
            nonlocal zoomed
            if method == "layout.export":
                return {"layout": {"root": deepcopy(self.root), "focused_pane_id": "p1", "zoomed": zoomed}}
            if method == "pane.zoom":
                zoomed = params["mode"] == "on"
            if method == "pane.move":
                raise ShellError("staging unavailable")
            return {}
        self.client.call = call
        with self.assertRaisesRegex(ShellError, "staging unavailable"):
            rotate_pane(self.context)
        self.assertTrue(zoomed)


if __name__ == "__main__":
    unittest.main()
