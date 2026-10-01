"""Practice ownership, real-action delegation and exact outcome checks."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from herdr_shell import desktop, practice, runtime
from herdr_shell.rotation import _nearest
from herdr_shell.runtime import Context, ShellError


class Server:
    def __init__(self, folder):
        self.path = "/fixture/herdr.sock"
        self.folder = str(folder)
        self.sequence = 0
        self.calls = []
        self.data = {"panes": [], "tabs": [], "workspaces": [], "layouts": [], "agents": []}
        self.guide = self.space()["root_pane"]
        self.focus(self.guide["pane_id"])
        self.foreign = self.space()["root_pane"]
        self.data["agents"].append({**self.foreign, "agent_status": "blocked"})
        self.active_work = set()

    def name(self, prefix):
        self.sequence += 1
        return prefix + str(self.sequence)

    def space(self):
        workspace = {"workspace_id": self.name("workspace-")}
        self.data["workspaces"].append(workspace)
        tab = self.tab(workspace["workspace_id"])
        return {"workspace": workspace, **tab}

    def tab(self, workspace):
        tab = {"tab_id": self.name("tab-"), "workspace_id": workspace}
        pane = {"pane_id": self.name("pane-"), "terminal_id": self.name("terminal-"),
                "workspace_id": workspace, "tab_id": tab["tab_id"], "cwd": self.folder,
                "agent_status": "unknown", "focused": False}
        self.data["tabs"].append(tab)
        self.data["panes"].append(pane)
        self.data["layouts"].append({"tab_id": tab["tab_id"], "zoomed": False,
                                    "root": {"type": "pane", "pane_id": pane["pane_id"]}})
        return {"tab": deepcopy(tab), "root_pane": deepcopy(pane)}

    def focus(self, pane):
        info = self.pane(pane)
        self.data.update(focused_pane_id=pane, focused_tab_id=info["tab_id"], focused_workspace_id=info["workspace_id"])

    def pane(self, pane):
        return next(p for p in self.data["panes"] if p["pane_id"] == pane)

    def layout(self, tab):
        return next(l for l in self.data["layouts"] if l["tab_id"] == tab)

    def snapshot(self):
        return deepcopy(self.data)

    def call(self, method, **p):
        self.calls.append((method, deepcopy(p)))
        if method == "pane.get":
            return {"pane": deepcopy(self.pane(p["pane_id"]))}
        if method == "pane.process_info":
            pid = os.getpid()
            foreground = [{"pid": pid, "name": "shell"}]
            if p["pane_id"] in self.active_work:
                foreground = [{"pid": pid + 1, "name": "editor"}]
            return {"process_info": {"shell_pid": pid, "foreground_process_group_id": foreground[0]["pid"], "foreground_processes": foreground}}
        if method == "tab.create":
            result = self.tab(p["workspace_id"])
        elif method == "workspace.create":
            result = self.space()
        elif method == "pane.split":
            original = self.pane(p["target_pane_id"])
            pane = {**original, "pane_id": self.name("pane-"), "terminal_id": self.name("terminal-"), "cwd": p.get("cwd", self.folder)}
            self.data["panes"].append(pane)
            pair = {"type": "split", "direction": p["direction"], "ratio": .5,
                    "first": {"type": "pane", "pane_id": original["pane_id"]},
                    "second": {"type": "pane", "pane_id": pane["pane_id"]}}
            layout = self.layout(original["tab_id"])
            layout["root"] = practice._replace(layout["root"], {original["pane_id"]: pair})
            result = {"pane": deepcopy(pane)}
        elif method == "pane.swap":
            source = p.get("source_pane_id", p.get("pane_id"))
            layout = self.layout(self.pane(source)["tab_id"])
            _, pair = _nearest(layout["root"], source)
            target = p.get("target_pane_id") or next(pair[s]["pane_id"] for s in ("first", "second") if pair[s]["pane_id"] != source)
            layout["root"] = practice._replace(layout["root"], {source: {"type": "pane", "pane_id": target}, target: {"type": "pane", "pane_id": source}})
            return {"swap": {"changed": True}}
        elif method == "pane.zoom":
            layout = self.layout(self.pane(p["pane_id"])["tab_id"])
            layout["zoomed"] = not layout["zoomed"] if p["mode"] == "toggle" else p["mode"] == "on"
            return {}
        elif method in ("pane.focus", "agent.focus"):
            self.focus(p.get("pane_id", p.get("target")))
            for agent in self.data["agents"]:
                if agent["pane_id"] == self.data["focused_pane_id"] and agent["agent_status"] == "done":
                    agent["agent_status"] = "idle"
            return {}
        elif method in ("tab.focus", "workspace.focus"):
            field = "tab_id" if method == "tab.focus" else "workspace_id"
            self.focus(next(pane["pane_id"] for pane in self.data["panes"] if pane[field] == p[field]))
            return {}
        elif method == "layout.export":
            return {"layout": deepcopy(self.layout(p["tab_id"]))}
        elif method in ("pane.close", "tab.close", "workspace.close"):
            field = method.split(".")[0] + "_id"
            removed = {pane["pane_id"] for pane in self.data["panes"] if pane[field] == p[field]}
            self.data["panes"] = [pane for pane in self.data["panes"] if pane["pane_id"] not in removed]
            if field != "pane_id":
                self.data["tabs"] = [t for t in self.data["tabs"] if t[field] != p[field]]
                self.data["layouts"] = [l for l in self.data["layouts"] if any(t["tab_id"] == l["tab_id"] for t in self.data["tabs"])]
            if field == "workspace_id":
                self.data["workspaces"] = [w for w in self.data["workspaces"] if w[field] != p[field]]
            self.focus(self.guide["pane_id"])
            return {}
        elif method == "pane.report_agent":
            previous = next((a for a in self.data["agents"] if a["pane_id"] == p["pane_id"]), None)
            state = "done" if previous and previous["agent_status"] == "working" and p["state"] == "idle" else p["state"]
            self.data["agents"] = [a for a in self.data["agents"] if a["pane_id"] != p["pane_id"]]
            self.data["agents"].append({**self.pane(p["pane_id"]), "agent_status": state, "source": p["source"]})
            return {}
        elif method == "pane.clear_agent_authority":
            self.data["agents"] = [a for a in self.data["agents"] if a["pane_id"] != p["pane_id"] or a.get("source") != p["source"]]
            return {}
        elif method == "agent.get":
            return {"agent": deepcopy(next(a for a in self.data["agents"] if a["pane_id"] == p["target"]))}
        elif method in ("pane.rename", "pane.send_input", "popup.close"):
            return {}
        else:
            raise AssertionError((method, p))
        if p.get("focus"):
            self.focus(result.get("pane", result.get("root_pane"))["pane_id"])
        return result


class PracticeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="herdr-key-quest-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.server = Server(self.folder)
        pane = self.server.guide
        self.guide = Context(self.server.path, pane["pane_id"], pane["workspace_id"], pane["tab_id"], str(self.folder), pane["terminal_id"])
        for patcher in (patch.object(runtime, "Client", return_value=self.server),
                        patch.object(practice, "shell_jobs", return_value=[]),
                        patch("herdr_shell.closing.shell_jobs", return_value=[]),
                        patch.object(practice, "_start_simulator", side_effect=lambda c, d, name="agent": str(Path(d) / (name + ".py"))),
                        patch.object(practice, "_simulator_alive", return_value=True)):
            patcher.start(); self.addCleanup(patcher.stop)

    def plan(self, action):
        return practice.prepare(action, self.guide)

    def test_all26_prepare_keeps_guide_and_foreign_work_unchanged(self):
        before = deepcopy(self.server.foreign)
        for _, action, _ in desktop.MAPPINGS:
            with self.subTest(action=action):
                plan = self.plan(action)
                self.assertEqual(self.server.snapshot()["focused_pane_id"], self.guide.pane)
                self.assertNotEqual(plan.target.pane, self.guide.pane)
                self.assertNotIn(self.server.foreign["pane_id"], plan.owned_panes)
                self.assertEqual(self.server.pane(before["pane_id"]), before)
                self.assertEqual(Path(plan.directory).stat().st_mode & 0o077, 0)
                self.assertIn("shortcut from the guide", plan.instructions.casefold())
                if action in ("tab-close", "pane-close", "workspace-close"):
                    self.assertNotEqual(plan.origin.tab, self.guide.tab)
                    if action == "workspace-close":
                        self.assertNotEqual(plan.origin.workspace, self.guide.workspace)

    def test_plan_roundtrip_preserves_contexts_and_scope(self):
        plan = self.plan("pane-split-left")
        self.assertEqual(practice.Plan.from_dict(plan.to_dict()).to_dict(), plan.to_dict())

    def test_wrong_key_and_completed_duplicate_do_not_mutate(self):
        plan = self.plan("pane-split-right")
        self.server.calls.clear()
        with self.assertRaisesRegex(ShellError, "armed shortcut"):
            practice.perform(plan, "workspace-close")
        self.assertEqual(self.server.calls, [])
        practice.perform(plan, plan.action)
        self.assertTrue(practice.verified(plan))
        self.server.calls.clear()
        with self.assertRaisesRegex(ShellError, "already complete"):
            practice.perform(plan, plan.action)
        self.assertEqual(self.server.calls, [])

    def test_splits_and_swaps_all_directions_check_exact_real_topology(self):
        for operation in ("split", "swap"):
            for direction in ("up", "down", "left", "right"):
                with self.subTest(operation=operation, direction=direction):
                    self.server.focus(self.guide.pane)
                    plan = self.plan("pane-" + operation + "-" + direction)
                    practice.perform(plan, plan.action)
                    self.assertTrue(practice.verified(plan))
                    pane = self.server.pane(plan.origin.pane)
                    pane["terminal_id"] = "replaced-terminal"
                    self.assertFalse(practice.verified(plan))

    def test_wrong_sibling_split_cannot_be_scored_by_position_or_new_count(self):
        plan = self.plan("pane-split-right")
        self.server.call("pane.split", target_pane_id=plan.origin.pane, direction="down", cwd=plan.directory, focus=True)
        plan.presses = 1
        self.assertFalse(practice.verified(plan))

    def test_added_pane_or_replaced_identity_refused_before_action(self):
        for change in ("added", "replaced"):
            self.server.focus(self.guide.pane)
            plan = self.plan("pane-close")
            if change == "added":
                self.server.call("pane.split", target_pane_id=plan.target.pane, direction="down", focus=False)
            else:
                self.server.pane(plan.target.pane)["terminal_id"] = "replacement"
            self.server.calls.clear()
            with self.assertRaises(ShellError):
                practice.perform(plan, plan.action)
            self.assertFalse(any(method.endswith(".close") for method, _ in self.server.calls))

    def test_clean_closes_remove_only_the_disposable_scope(self):
        for action in ("pane-close", "tab-close", "workspace-close"):
            with self.subTest(action=action):
                self.server.focus(self.guide.pane)
                plan = self.plan(action)
                practice.perform(plan, action)
                self.assertTrue(practice.verified(plan))
                self.assertEqual(self.server.pane(self.guide.pane)["terminal_id"], self.guide.terminal)
                self.assertEqual(self.server.pane(self.server.foreign["pane_id"]), self.server.foreign)

    def test_new_foreground_or_background_work_refuses_close(self):
        for kind in ("foreground", "background"):
            self.server.focus(self.guide.pane)
            plan = self.plan("tab-close")
            if kind == "foreground":
                self.server.active_work.add(plan.target.pane)
                patches = []
            else:
                patches = [patch("herdr_shell.closing.shell_jobs", return_value=["Background job editor"])]
            for p in patches: p.start()
            try:
                with self.assertRaisesRegex(ShellError, "started work"):
                    practice.perform(plan, plan.action)
                self.assertIn(plan.target.pane, {p["pane_id"] for p in self.server.data["panes"]})
            finally:
                for p in patches: p.stop()

    def test_workspace_and_tab_navigation_is_bounded_to_plan_fixtures(self):
        for action in ("workspace-next", "workspace-previous", "tab-next", "tab-previous", "pane-cycle-next", "pane-cycle-previous"):
            with self.subTest(action=action):
                self.server.focus(self.guide.pane)
                plan = self.plan(action)
                practice.perform(plan, action)
                self.assertTrue(practice.verified(plan))
                self.assertIn(plan.target.pane, plan.owned_panes)
                self.assertNotEqual(plan.target.pane, self.server.foreign["pane_id"])

    def test_new_tab_and_workspace_adopt_actual_minted_identities(self):
        for action in ("tab-new", "workspace-new"):
            self.server.focus(self.guide.pane)
            plan = self.plan(action)
            practice.perform(plan, action)
            self.assertTrue(practice.verified(plan))
            self.assertIn(plan.target.pane, plan.owned_panes)
            self.assertIn(plan.target.tab, plan.owned_tabs)
            self.assertIn(plan.target.workspace, plan.owned_workspaces)

    def test_new_targets_are_published_before_focus_can_expose_a_rapid_repeat(self):
        for action in ("pane-split-right", "tab-new", "workspace-new", "agent-new", "launch-git"):
            with self.subTest(action=action):
                self.server.focus(self.guide.pane)
                plan = self.plan(action)
                published = set(plan.owned_panes)
                callbacks = []
                def publish():
                    added = set(plan.owned_panes) - published
                    self.assertEqual(len(added), 1)
                    self.assertNotIn(self.server.data["focused_pane_id"], added)
                    published.update(added)
                    callbacks.append(added)
                plan.on_adopt = publish
                real = self.server.call
                def call(method, **params):
                    if method in ("pane.split", "tab.create", "workspace.create"):
                        self.assertFalse(params["focus"])
                    if method == "pane.focus":
                        self.assertIn(params["pane_id"], published)
                    return real(method, **params)
                with patch.object(self.server, "call", side_effect=call), patch.object(practice, "_start_git"), \
                     patch.object(practice, "_git_alive", return_value=True):
                    practice.perform(plan, action)
                    self.assertTrue(practice.verified(plan))
                self.assertEqual(len(callbacks), 1)
                self.assertTrue(any(method in ("pane.split", "tab.create", "workspace.create") for method, _ in self.server.calls))

    def test_idle_close_activity_race_is_refused_without_a_confirmation_popup(self):
        plan = self.plan("tab-close")
        real = self.server.call
        def call(method, **params):
            result = real(method, **params)
            if method == "pane.focus" and params["pane_id"] == plan.target.pane:
                self.server.active_work.add(plan.target.pane)
            return result
        with patch.object(self.server, "call", side_effect=call), patch.object(practice.actions, "open_ui") as popup:
            with self.assertRaisesRegex(ShellError, "started after the close"):
                practice.perform(plan, plan.action)
        popup.assert_not_called()
        self.assertFalse(any(m.endswith(".close") for m, _ in self.server.calls))

    def test_idle_close_detects_new_unknown_panes_in_final_scope_snapshot(self):
        plan = self.plan("workspace-close")
        real = self.server.call
        def call(method, **params):
            result = real(method, **params)
            if method == "pane.focus" and params["pane_id"] == plan.target.pane:
                real("pane.split", target_pane_id=plan.target.pane, direction="right", focus=False)
            return result
        with patch.object(self.server, "call", side_effect=call), patch.object(practice.actions, "open_ui") as popup:
            with self.assertRaisesRegex(ShellError, "affected by closing changed"):
                practice.perform(plan, plan.action)
        popup.assert_not_called()
        self.assertFalse(any(m.endswith(".close") for m, _ in self.server.calls))

    def test_zoom_requires_two_actual_stages_and_exact_restoration(self):
        plan = self.plan("pane-zoom")
        self.assertFalse(practice.verified(plan))
        practice.perform(plan, plan.action)
        self.assertTrue(practice.verified(plan))
        self.assertEqual(plan.presses, 1)
        self.assertTrue(self.server.layout(plan.origin.tab)["zoomed"])
        self.server.focus(self.guide.pane)
        practice.perform(plan, plan.action)
        self.assertTrue(practice.verified(plan))
        self.assertEqual(plan.history, [True, False])
        self.server.layout(plan.origin.tab)["zoomed"] = True
        self.assertFalse(practice.verified(plan))

    def test_agent_ring_is_fixed_owned_and_never_writes_global_cycle_state(self):
        for action in ("agent-cycle-next", "agent-cycle-previous"):
            self.server.focus(self.guide.pane)
            plan = self.plan(action)
            self.server.calls.clear()
            with patch("herdr_shell.navigation.atomic_write") as write:
                for number in range(4):
                    practice.perform(plan, action)
                    self.assertEqual(plan.presses, number + 1)
                    self.assertTrue(practice.verified(plan))
                    self.server.focus(self.guide.pane)
                write.assert_not_called()
            focused = [p["target"] for method, p in self.server.calls if method == "agent.focus"]
            self.assertEqual(focused, plan.expected["agent_ring"])
            self.assertNotIn(self.server.foreign["pane_id"], focused)

    def test_stopped_simulator_refuses_visiting_a_replacement_real_agent(self):
        plan = self.plan("agent-cycle-next")
        self.server.calls.clear()
        with patch.object(practice, "_simulator_alive", return_value=False):
            with self.assertRaisesRegex(ShellError, "simulator stopped"):
                practice.perform(plan, plan.action)
        self.assertFalse(any(m == "agent.focus" for m, _ in self.server.calls))

    def test_agent_launch_never_runs_omarchy_agent_entrypoint(self):
        plan = self.plan("agent-new")
        practice.perform(plan, plan.action)
        self.assertTrue(practice.verified(plan))
        self.assertFalse(any(m == "plugin.pane.open" for m, _ in self.server.calls))
        self.assertTrue(any(m == "pane.report_agent" for m, _ in self.server.calls))

    def test_proxy_blocks_guide_close_and_unrelated_nested_move_destination(self):
        plan = self.plan("pane-close")
        proxy = practice.ScopedClient(plan)
        for method, params in (("pane.close", {"pane_id": self.guide.pane}),
                               ("tab.close", {"tab_id": self.guide.tab}),
                               ("workspace.close", {"workspace_id": self.guide.workspace}),
                               ("pane.move", {"pane_id": plan.target.pane, "destination": {"type": "tab", "tab_id": self.server.foreign["tab_id"]}})):
            with self.subTest(method=method), self.assertRaises(ShellError):
                proxy.call(method, **params)

    def test_real_plugin_pane_response_is_adopted_for_git_launch(self):
        plan = self.plan("pane-split-right")
        created = self.server.call("pane.split", target_pane_id=plan.target.pane, direction="right", focus=True)["pane"]
        practice._adopt(plan, {"plugin_pane": {"plugin_id": "blr.herdr-shell", "entrypoint": "git", "pane": created}})
        self.assertIn(created["pane_id"], plan.owned_panes)

    def test_refresh_never_repins_a_replaced_new_terminal(self):
        plan = self.plan("pane-split-right")
        result = self.server.call("pane.split", target_pane_id=plan.target.pane, direction="right", focus=True)
        practice._adopt(plan, result)
        new = result["pane"]["pane_id"]
        pinned = next(p["terminal_id"] for p in plan.pane_identities if p["pane_id"] == new)
        self.server.pane(new)["terminal_id"] = "user-replacement"
        with self.assertRaisesRegex(ShellError, "terminal was replaced"):
            practice._refresh(plan)
        self.assertEqual(next(p["terminal_id"] for p in plan.pane_identities if p["pane_id"] == new), pinned)

    def test_refresh_never_adopts_moved_pane_into_unrelated_tab_or_space(self):
        plan = self.plan("pane-split-right")
        original = deepcopy(plan.pane_identities)
        self.server.pane(plan.target.pane).update(workspace_id=self.server.foreign["workspace_id"], tab_id=self.server.foreign["tab_id"])
        with self.assertRaisesRegex(ShellError, "replaced or moved"):
            practice._refresh(plan)
        self.assertEqual(plan.pane_identities, original)
        self.assertEqual(plan.pins, original)

    def test_move_publishes_transient_owned_tab_without_repinning_final_identity(self):
        plan = self.plan("pane-rotate")
        moved = next(p for p in plan.pane_identities if p["tab_id"] == plan.target.tab and p["pane_id"] != plan.target.pane)
        pin = deepcopy(moved)
        publications = []
        plan.on_adopt = lambda: publications.append(deepcopy(plan.pane_identities))
        real = self.server.call
        def call(method, **params):
            if method != "pane.move":
                return real(method, **params)
            self.server.calls.append((method, deepcopy(params)))
            destination = params["destination"]
            pane = self.server.pane(params["pane_id"])
            result = {}
            if destination["type"] == "new_tab":
                created = self.server.tab(destination["workspace_id"])
                self.server.data["panes"].remove(self.server.pane(created["root_pane"]["pane_id"]))
                pane.update(tab_id=created["tab"]["tab_id"])
                result["created_tab"] = created["tab"]
            else:
                pane.update(tab_id=destination["tab_id"])
            result["pane"] = deepcopy(pane)
            return {"move_result": result}
        proxy = practice.ScopedClient(plan)
        with patch.object(self.server, "call", side_effect=call):
            proxy.call("pane.move", pane_id=pin["pane_id"], focus=False,
                       destination={"type": "new_tab", "workspace_id": pin["workspace_id"]})
            transient = next(p for p in publications[-1] if p["pane_id"] == pin["pane_id"])
            self.assertNotEqual(transient["tab_id"], pin["tab_id"])
            self.assertIn(transient["tab_id"], plan.owned_tabs)
            self.assertIn(pin, plan.pins)
            with self.assertRaisesRegex(ShellError, "replaced or moved"):
                practice._refresh(plan)
            proxy.call("pane.move", pane_id=pin["pane_id"], focus=False,
                       destination={"type": "tab", "tab_id": pin["tab_id"]})
        self.assertIn(pin, publications[-1])
        practice._refresh(plan)
        self.assertIn(pin, plan.pane_identities)
        self.assertEqual(len(publications), 2)

    def test_malformed_create_response_cannot_adopt_preexisting_foreign_scope(self):
        plan = self.plan("pane-split-right")
        result = self.server.call("pane.split", target_pane_id=self.server.foreign["pane_id"], direction="right", focus=False)
        prior = deepcopy(plan.pane_identities)
        with self.assertRaisesRegex(ShellError, "unrelated workspace"):
            practice._adopt(plan, result)
        self.assertEqual(plan.pane_identities, prior)
        with self.assertRaisesRegex(ShellError, "existing pane"):
            practice._adopt(plan, {"pane": self.server.foreign})

    def test_workspace_create_cannot_claim_an_existing_owned_workspace(self):
        plan = self.plan("workspace-new")
        result = self.server.tab(plan.target.workspace)
        result["workspace"] = {"workspace_id": plan.target.workspace}
        with self.assertRaisesRegex(ShellError, "new workspace"):
            practice._adopt(plan, result, method="workspace.create")

    def test_git_initializer_scrubs_inherited_git_routing_and_private_child_pins_repository(self):
        foreign = self.folder / "foreign-git"
        variables = {"GIT_DIR": str(foreign), "GIT_WORK_TREE": str(self.folder / "foreign-work"),
                     "GIT_COMMON_DIR": str(foreign), "GIT_INDEX_FILE": str(foreign / "index"),
                     "GIT_OBJECT_DIRECTORY": str(foreign / "objects"), "GIT_CONFIG_COUNT": "1",
                     "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/foreign/hooks"}
        with patch.dict(os.environ, variables):
            plan = self.plan("launch-git")
            self.assertTrue((Path(plan.directory) / ".git" / "HEAD").exists())
            self.assertFalse(foreign.exists())
            executable = self.folder / "fake-lazygit"
            capture = Path(plan.directory) / "child.json"
            executable.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                                  "open(" + repr(str(capture)) + ", 'w').write(json.dumps({'cwd':os.getcwd(), 'env':dict(os.environ), 'argv':sys.argv}))\n")
            executable.chmod(0o700)
            script = practice._git_script(plan.directory, str(executable))
            subprocess.run([sys.executable, str(script)], check=True, timeout=5)
        data = json.loads(capture.read_text())
        self.assertEqual(data["cwd"], plan.directory)
        self.assertEqual(data["argv"][1:], [])
        allowed = {k: v for k, v in practice._git_environment(plan.directory).items() if k.startswith("GIT_")}
        self.assertEqual({k: v for k, v in data["env"].items() if k.startswith("GIT_")}, allowed)
        self.assertFalse(foreign.exists())

    def test_close_tabs_and_workspaces_have_owned_native_predecessor(self):
        for action in ("tab-close", "workspace-close"):
            plan = self.plan(action)
            kind = action.split("-")[0]
            key = kind + "_id"
            rows = [r for r in self.server.data[kind + "s"] if kind != "tab" or r["workspace_id"] == plan.origin.workspace]
            position = [r[key] for r in rows].index(getattr(plan.origin, kind))
            predecessor = rows[position - 1][key]
            self.assertIn(predecessor, plan.owned_tabs if kind == "tab" else plan.owned_workspaces)
            self.assertNotEqual(predecessor, self.guide.tab if kind == "tab" else self.guide.workspace)

    def test_menu_opens_readonly_and_closes_only_its_exact_owned_marker(self):
        plan = self.plan("menu")
        marker = {"pid": 100, "start": "1", "pane": plan.target.pane, "terminal": plan.target.terminal}
        state = {"marker": None}
        def open_ui(context, page, env):
            self.assertEqual(page, "menu")
            self.assertEqual(env, {"HERDR_SHELL_GAME_MENU": "1"})
            state["marker"] = marker
            return {}
        original_call = self.server.call
        def call(method, **params):
            if method == "popup.close": state["marker"] = None
            return original_call(method, **params)
        with patch.object(practice.desktop, "menu_running", side_effect=lambda s: state["marker"]), \
             patch.object(practice.actions, "open_ui", side_effect=open_ui), patch.object(self.server, "call", side_effect=call):
            practice.perform(plan, plan.action)
            self.assertTrue(practice.verified(plan))
            practice.perform(plan, plan.action)
            self.assertTrue(practice.verified(plan))
            self.assertEqual(plan.history, ["opened", "closed"])

    def test_menu_replacement_preserved_instead_of_closed(self):
        plan = self.plan("menu")
        plan.presses = 1
        plan.expected["menu_marker"] = {"pid": 1, "pane": plan.origin.pane}
        self.server.calls.clear()
        with patch.object(practice.desktop, "menu_running", return_value={"pid": 2, "pane": self.server.foreign["pane_id"]}):
            with self.assertRaisesRegex(ShellError, "menu changed"):
                practice.perform(plan, plan.action)
        self.assertFalse(any(m == "popup.close" for m, _ in self.server.calls))

    def test_nonprivate_directory_refused_before_any_fixture_created(self):
        self.folder.chmod(0o755)
        self.server.calls.clear()
        with self.assertRaisesRegex(ShellError, "private directory"):
            self.plan("workspace-close")
        self.assertFalse(any(m.endswith(".create") for m, _ in self.server.calls))

    def demo_record(self):
        plan = self.plan("agent-new")
        script = Path(plan.directory) / "owned-demo.py"
        script.write_text("import signal\nsignal.pause()\n")
        process = subprocess.Popen([sys.executable, str(script)])
        def stop():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
        self.addCleanup(stop)
        pin = desktop.read_process(process.pid)
        record = {"context": asdict(plan.target), "pid": process.pid, "start": pin.start,
                  "path": str(script), "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
        practice.atomic_write(Path(plan.directory) / ".simulator-test.json", json.dumps(record))
        practice._report(plan.target, "idle")
        return plan, script, process

    def test_cleanup_stops_only_original_demo_and_clears_only_its_report_source(self):
        plan, script, process = self.demo_record()
        result = practice.cleanup(self.guide)
        process.wait(timeout=3)
        self.assertIn(plan.target.pane, result["stopped"])
        self.assertIn(plan.target.pane, result["reports_cleared"])
        self.assertTrue(script.exists())
        self.assertIn(plan.target.pane, {p["pane_id"] for p in self.server.data["panes"]})
        self.assertIn(self.server.foreign["pane_id"], {a["pane_id"] for a in self.server.data["agents"]})
        clears = [p for m, p in self.server.calls if m == "pane.clear_agent_authority"]
        self.assertEqual(clears[-1], {"pane_id": plan.target.pane, "source": "key-quest", "seq": 3})

    def test_cleanup_preserves_edited_script_process_but_releases_old_own_report(self):
        plan, script, process = self.demo_record()
        script.write_text("# personal edit\n" + script.read_text())
        result = practice.cleanup(self.guide)
        self.assertIsNone(process.poll())
        self.assertIn(plan.target.pane, result["preserved"])
        self.assertIn(plan.target.pane, result["reports_cleared"])

    def test_cleanup_replaced_terminal_does_not_signal_or_clear_reports(self):
        plan, _, process = self.demo_record()
        self.server.pane(plan.target.pane)["terminal_id"] = "replacement"
        with patch.object(practice.signal, "pidfd_send_signal") as send:
            result = practice.cleanup(self.guide)
        send.assert_not_called()
        self.assertIsNone(process.poll())
        self.assertEqual(result["reports_cleared"], [])

    def test_cleanup_material_report_failure_is_visible(self):
        self.demo_record()
        real = self.server.call
        def fail(method, **params):
            if method == "pane.clear_agent_authority":
                raise ShellError("source could not clear")
            return real(method, **params)
        with patch.object(self.server, "call", side_effect=fail):
            with self.assertRaisesRegex(ShellError, "own agent reports could not be released"):
                practice.cleanup(self.guide)

    def test_cleanup_exited_simulator_still_releases_its_old_source(self):
        plan, _, process = self.demo_record()
        process.terminate()
        process.wait(timeout=3)
        result = practice.cleanup(self.guide)
        self.assertIn(plan.target.pane, result["reports_cleared"])
        self.assertEqual(result["stopped"], [])

    def test_cleanup_pid_reuse_or_changed_argv_never_signals_replacement(self):
        for changed in ("start", "argv"):
            with self.subTest(changed=changed):
                plan, script, process = self.demo_record()
                original = desktop.read_process(process.pid)
                changed_process = type(original)(original.pid, original.parent, original.state, original.foreground,
                                                 "changed" if changed == "start" else original.start,
                                                 ("user-command",) if changed == "argv" else original.argv)
                with patch.object(practice.desktop, "read_process", return_value=changed_process), \
                     patch.object(practice.signal, "pidfd_send_signal") as send:
                    result = practice.cleanup(self.guide)
                send.assert_not_called()
                self.assertIsNone(process.poll())
                self.assertIn(plan.target.pane, result["preserved"])
                self.assertIn(plan.target.pane, result["reports_cleared"])


if __name__ == "__main__":
    unittest.main()
