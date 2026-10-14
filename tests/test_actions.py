from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from herdr_shell.actions import execute, open_ui
from herdr_shell.closing import close_plan, execute_close, set_pane_close_confirmation, shell_jobs
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
        if method == "tab.focus":
            tab = next(t for t in self.data["tabs"] if t["tab_id"] == params["tab_id"])
            self.data["focused_pane_id"] = tab.get("focused_pane_id") or next(
                p["pane_id"] for p in self.data["panes"] if p["tab_id"] == tab["tab_id"])
            return {"changed": True}
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

    def setUp(self):
        preferences = tempfile.TemporaryDirectory()
        self.addCleanup(preferences.cleanup)
        environment = patch.dict(os.environ, HERDR_SHELL_DESKTOP_CONFIG_HOME=preferences.name)
        environment.start()
        self.addCleanup(environment.stop)

    def test_left_and_up_splits_read_nested_swap_result_keep_focus_and_stay_unlabeled(self):
        for direction, native in (("left", "right"), ("up", "down")):
            client = FakeClient()
            context = FakeContext(client)
            def call(method, **params):
                client.calls.append((method, params))
                if method == "pane.split":
                    return {"pane": pane("created")}
                if method == "pane.swap":
                    return {"swap": {"changed": True}}
                return {}
            client.call = call
            result = execute("pane-split-" + direction, context)
            self.assertEqual(result["pane"]["pane_id"], "created")
            self.assertEqual(client.calls[0][1]["direction"], native)
            self.assertEqual(client.calls[1:], [("pane.swap", {"source_pane_id": "created", "target_pane_id": "p1"}),
                                                ("pane.focus", {"pane_id": "created"})])

    def test_new_split_panes_stay_unlabeled_so_clients_show_live_titles(self):
        client = FakeClient()
        client.data["panes"][0].update(agent="omp", terminal_title_stripped="π ⠹ Check edge cases")
        calls = []
        def call(method, **params):
            calls.append((method, params))
            if method == "pane.split":
                return {"pane": pane("created")}
            return {}
        client.call = call
        execute("pane-split-right", FakeContext(client))
        self.assertEqual([method for method, _ in calls], ["pane.split"])

    def test_failed_sibling_swap_reports_created_pane_before_naming(self):
        client = FakeClient()
        client.call = Mock(side_effect=[{"pane": pane("created")}, {"swap": {"changed": False}}])
        with self.assertRaisesRegex(ShellError, "Pane created was created down, but could not be moved up"):
            execute("pane-split-up", FakeContext(client))


class LearningActionTests(unittest.TestCase):
    def test_both_learning_modes_launch_owned_practice_without_popup(self):
        for action, mode in (("learn-hands-on", "hands-on"), ("learn-game", "speed")):
            with self.subTest(action=action):
                client = FakeClient()
                context = FakeContext(client)
                with patch("herdr_shell.trainer.open_game", return_value={"mode": mode}) as open_game:
                    self.assertEqual(execute(action, context), {"mode": mode})
                open_game.assert_called_once_with(context, mode=mode)
                self.assertEqual(client.calls, [])

    def test_walkthrough_remains_read_only_popup(self):
        client = FakeClient()
        context = FakeContext(client)
        with patch("herdr_shell.actions.open_ui", return_value={"walkthrough": True}) as open_ui:
            self.assertEqual(execute("walkthrough", context), {"walkthrough": True})
        open_ui.assert_called_once_with(context, "walkthrough")
        self.assertEqual(client.calls, [])



class AutomaticNamingActionTests(unittest.TestCase):
    def setUp(self):
        preferences = tempfile.TemporaryDirectory()
        self.addCleanup(preferences.cleanup)
        environment = patch.dict(os.environ, HERDR_SHELL_DESKTOP_CONFIG_HOME=preferences.name)
        environment.start()
        self.addCleanup(environment.stop)
        self.client = FakeClient()
        self.context = FakeContext(self.client)
        self.context.cwd = "/work/project"

    def test_new_tab_is_unlabeled_in_the_origin_directory_even_beside_an_agent(self):
        self.client.data["panes"][0].update(agent="omp", terminal_title_stripped="π ⠹ Review panel names")
        execute("tab-new", self.context)
        self.assertEqual(self.client.calls[-1], ("tab.create", {"workspace_id": "w", "cwd": "/work/project", "focus": True}))

    def test_name_tab_from_agent_task_hands_the_tab_back_with_its_position(self):
        from herdr_shell import namer
        self.client.data["tabs"].insert(0, {"tab_id": "t0", "workspace_id": "w", "label": "logs"})
        with patch.object(namer, "ensure") as ensure:
            execute("tab-auto-name", self.context)
        self.assertEqual(self.client.calls[-1], ("tab.rename", {"tab_id": "t", "label": "2"}))
        ensure.assert_called_once_with("test-socket")
        self.client.data["tabs"] = [tab for tab in self.client.data["tabs"] if tab["tab_id"] != "t"]
        with patch.object(namer, "ensure"), self.assertRaisesRegex(ShellError, "no longer exists"):
            execute("tab-auto-name", self.context)

    def test_new_workspace_uses_project_then_branch_and_never_the_agent_task(self):
        from herdr_shell import naming
        self.client.data["panes"][0].update(agent="omp", terminal_title_stripped="π ⠹ Investigate regression")
        self.client.data["workspaces"].append({"workspace_id": "w2", "label": "project"})
        def git(cwd, *args):
            return "/repos/project" if args[0] == "rev-parse" else "feature/search"
        with patch.object(naming, "_git", side_effect=git):
            execute("workspace-new", self.context)
        method, params = self.client.calls[-1]
        self.assertEqual(method, "workspace.create")
        self.assertEqual(params["label"], "project · feature/search")
        self.assertEqual(params["cwd"], "/work/project")

    def test_workspace_folder_mode_uses_origin_directory_without_git_lookup(self):
        from herdr_shell import naming
        naming.set_preference("workspace_mode", "folder")
        with patch.object(naming, "_git") as git:
            execute("workspace-new", self.context)
        self.assertEqual(self.client.calls[-1][1]["label"], "project")
        git.assert_not_called()


class CloseTests(unittest.TestCase):
    def setUp(self):
        preferences = tempfile.TemporaryDirectory()
        self.addCleanup(preferences.cleanup)
        preference_env = patch.dict(os.environ, HERDR_SHELL_DESKTOP_CONFIG_HOME=preferences.name)
        preference_env.start()
        self.addCleanup(preference_env.stop)
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

    def test_pane_close_preference_bypasses_only_pane_prompts_and_can_be_reenabled(self):
        self.client.data["agents"] = [pane("p1", agent="codex", agent_status="working")]
        set_pane_close_confirmation(False)
        execute("pane-close", self.context)
        self.assertEqual(self.client.calls[-1], ("pane.close", {"pane_id": "p1"}))
        self.assertFalse(any(method == "plugin.pane.open" for method, _ in self.client.calls))

        for action in ("tab-close", "workspace-close"):
            with self.subTest(action=action):
                self.client.calls.clear()
                execute(action, self.context)
                self.assertEqual(self.client.calls[-1][0], "plugin.pane.open")
                self.assertEqual(self.client.calls[-1][1]["env"]["HERDR_SHELL_PAGE"], "confirm-" + action)
                self.assertFalse(any(method.endswith(".close") for method, _ in self.client.calls))

        set_pane_close_confirmation(True)
        self.client.calls.clear()
        execute("pane-close", self.context)
        self.assertEqual(self.client.calls[-1][0], "plugin.pane.open")
        self.assertFalse(any(method.endswith(".close") for method, _ in self.client.calls))

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

    def hierarchy(self):
        self.client.data["workspaces"] = [
            {"workspace_id": "wa"}, {"workspace_id": "empty"}, {"workspace_id": "wb"}]
        self.client.data["tabs"] = [
            {"tab_id": "tc", "workspace_id": "wb", "focused_pane_id": "d"},
            {"tab_id": "ta", "workspace_id": "wa", "focused_pane_id": "e"},
            {"tab_id": "td", "workspace_id": "wb", "focused_pane_id": "f"},
            {"tab_id": "tb", "workspace_id": "wa", "focused_pane_id": "b"}]
        self.client.data["panes"] = [
            pane("c", "tc", "wb"), pane("a", "ta", "wa"), pane("f", "td", "wb"),
            pane("b", "tb", "wa"), pane("d", "tc", "wb"), pane("e", "ta", "wa")]
        self.client.data["focused_pane_id"] = "a"

    def test_pane_cycle_crosses_tabs_and_workspaces_in_both_directions(self):
        self.hierarchy()
        for action, expected in (
                ("pane-cycle-next", ["e", "b", "c", "d", "f", "a"]),
                ("pane-cycle-previous", ["f", "d", "c", "b", "e", "a"])):
            with self.subTest(action=action):
                self.client.data["focused_pane_id"] = "a"
                for target in expected:
                    current = self.client.data["focused_pane_id"]
                    execute(action, FakeContext(self.client, current))
                    self.assertEqual(self.client.data["focused_pane_id"], target)

    def test_tab_cycle_crosses_workspaces_and_restores_selected_panes(self):
        self.hierarchy()
        for action, expected in (
                ("tab-next", ["b", "d", "f", "e"]),
                ("tab-previous", ["f", "d", "b", "e"])):
            with self.subTest(action=action):
                self.client.data["focused_pane_id"] = "e"
                for target in expected:
                    current = self.client.data["focused_pane_id"]
                    execute(action, FakeContext(self.client, current))
                    self.assertEqual(self.client.data["focused_pane_id"], target)

    def test_single_pane_and_tab_keep_focus(self):
        self.client.data["panes"] = [pane("p1")]
        for action in ("pane-cycle-next", "pane-cycle-previous", "tab-next", "tab-previous"):
            execute(action, FakeContext(self.client))
            self.assertEqual(self.client.data["focused_pane_id"], "p1")

    def test_missing_origin_is_rejected_without_changing_focus(self):
        origin = FakeContext(self.client)
        self.client.data["panes"] = [pane("p2")]
        before = self.client.snapshot()
        with self.assertRaises(ShellError):
            cycle_pane(origin, 1)
        self.assertEqual(self.client.snapshot(), before)

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
