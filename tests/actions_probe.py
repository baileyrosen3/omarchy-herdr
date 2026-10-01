"""Action contract checks against a disposable Herdr server, never the user's session."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from herdr_shell.actions import close_plan, execute, execute_close
from herdr_shell.rotation import _tree, rotation_unavailable
from herdr_shell.runtime import Client, Context, ShellError


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except ShellError:
            pass
        time.sleep(.04)
    raise AssertionError("Isolated server did not reach expected state")


def context(client, pane):
    p = client.call("pane.get", pane_id=pane)["pane"]
    return Context(client.path, p["pane_id"], p["workspace_id"], p["tab_id"],
                   p.get("cwd") or "/tmp", p["terminal_id"])


def identity(client):
    return {p["pane_id"]: (p["terminal_id"], client.call("pane.process_info", pane_id=p["pane_id"])["process_info"]["shell_pid"])
            for p in client.snapshot()["panes"]}


def layout(client, tab):
    return client.call("layout.export", tab_id=tab)["layout"]


def exercise(root, client):
    first = client.call("workspace.create", label="Action QA", cwd=str(root), focus=True)["root_pane"]
    origin = context(client, first["pane_id"])
    # Exercise real split/swap request shapes and nested response fields in all
    # directions; mocked successes cannot establish this server contract.
    opposite = {"up": "down", "down": "up", "left": "right", "right": "left"}
    for direction in ("up", "down", "left", "right"):
        tab = client.call("tab.create", workspace_id=origin.workspace, cwd=str(root), focus=True)
        target = context(client, tab["root_pane"]["pane_id"])
        created = execute("pane-split-" + direction, target)["pane"]["pane_id"]
        def positions():
            current = next(l for l in client.snapshot()["layouts"] if l["tab_id"] == target.tab)
            return {p["pane_id"]: p["rect"] for p in current["panes"]}
        before = positions()
        axis = "x" if direction in ("left", "right") else "y"
        assert (before[created][axis] > before[target.pane][axis]) == (direction in ("right", "down")), before
        assert client.snapshot()["focused_pane_id"] == created
        ids = identity(client)
        assert execute("pane-swap-" + opposite[direction], context(client, created))["swap"]["changed"]
        assert positions()[created] == before[target.pane]
        assert positions()[target.pane] == before[created]
        assert identity(client) == ids
        execute("tab-close", target, yes=True)
    second = client.call("pane.split", target_pane_id=origin.pane, direction="right", ratio=.37, focus=True)["pane"]
    eventually(lambda: all(client.call("pane.process_info", pane_id=p["pane_id"])["process_info"].get("shell_pid")
                           for p in client.snapshot()["panes"]))
    # A running foreground command proves rotation retains actual work, not just IDs.
    client.call("pane.send_input", pane_id=second["pane_id"], text="sleep 60", keys=["enter"])
    eventually(lambda: any(p["name"] == "sleep" for p in client.call("pane.process_info", pane_id=second["pane_id"])["process_info"].get("foreground_processes", [])))
    before = identity(client)
    old_tree = _tree(layout(client, origin.tab)["root"])
    sleep_before = client.call("pane.process_info", pane_id=second["pane_id"])["process_info"]["foreground_processes"]
    result = execute("pane-rotate", context(client, second["pane_id"]))
    desired = deepcopy(old_tree); desired["direction"] = "down"
    assert _tree(layout(client, origin.tab)["root"]) == desired
    assert identity(client) == before
    assert client.call("pane.process_info", pane_id=second["pane_id"])["process_info"]["foreground_processes"] == sleep_before
    assert layout(client, origin.tab)["focused_pane_id"] == second["pane_id"]
    assert len(client.snapshot()["tabs"]) == 1
    execute("pane-rotate", context(client, second["pane_id"]))
    assert _tree(layout(client, origin.tab)["root"]) == old_tree
    assert identity(client) == before

    third = client.call("pane.split", target_pane_id=second["pane_id"], direction="down", ratio=.61, focus=True)["pane"]
    # A nested sibling rejects before moving; its two-leaf descendant can rotate.
    nested_before = _tree(layout(client, origin.tab)["root"])
    assert "nested" in rotation_unavailable(origin)
    try:
        execute("pane-rotate", origin)
    except ShellError as exc:
        assert "nested" in str(exc)
    else:
        raise AssertionError("Nested sibling rotation should be refused")
    assert _tree(layout(client, origin.tab)["root"]) == nested_before
    nested_ids = identity(client)
    client.call("pane.zoom", pane_id=third["pane_id"], mode="on")
    assert layout(client, origin.tab)["zoomed"]
    nested_tabs = {tab["tab_id"] for tab in client.snapshot()["tabs"]}
    nested_sleep = client.call("pane.process_info", pane_id=second["pane_id"])["process_info"]["foreground_processes"]
    execute("pane-rotate", context(client, third["pane_id"]))
    nested_desired = deepcopy(nested_before); nested_desired["second"]["direction"] = "right"
    assert _tree(layout(client, origin.tab)["root"]) == nested_desired
    assert identity(client) == nested_ids
    assert layout(client, origin.tab)["zoomed"]
    assert layout(client, origin.tab)["focused_pane_id"] == third["pane_id"]
    assert {tab["tab_id"] for tab in client.snapshot()["tabs"]} == nested_tabs
    assert client.call("pane.process_info", pane_id=second["pane_id"])["process_info"]["foreground_processes"] == nested_sleep
    execute("pane-rotate", context(client, third["pane_id"]))
    assert _tree(layout(client, origin.tab)["root"]) == nested_before
    assert identity(client) == nested_ids
    assert layout(client, origin.tab)["zoomed"]
    assert layout(client, origin.tab)["focused_pane_id"] == third["pane_id"]
    assert {tab["tab_id"] for tab in client.snapshot()["tabs"]} == nested_tabs
    assert client.call("pane.process_info", pane_id=second["pane_id"])["process_info"]["foreground_processes"] == nested_sleep

    # Ordinary process activity is classified conservatively, including clean shells.
    clean_plan = close_plan("pane-close", origin)
    assert not clean_plan["needs_confirmation"]
    assert close_plan("pane-close", context(client, second["pane_id"]))["needs_confirmation"]
    client.call("pane.send_input", pane_id=origin.pane, text="sleep 60 &", keys=["enter"])
    def background_ready():
        plan = close_plan("pane-close", origin)
        return plan if any("Background job sleep" in a["reason"] for a in plan["activity"]) else None
    background = eventually(background_ready)
    assert "Background job sleep" in background["activity"][0]["reason"], background
    try:
        execute_close(clean_plan, origin)
    except ShellError as exc:
        assert "command or agent started" in str(exc)
    else:
        raise AssertionError("New background work must invalidate an automatic close")
    clean = client.call("tab.create", workspace_id=origin.workspace, focus=False)["root_pane"]
    clean_context = context(client, clean["pane_id"])
    eventually(lambda: not close_plan("tab-close", clean_context)["needs_confirmation"])
    execute("tab-close", clean_context)
    assert clean["tab_id"] not in {t["tab_id"] for t in client.snapshot()["tabs"]}
    plan = close_plan("workspace-close", origin)
    client.call("tab.create", workspace_id=origin.workspace, focus=False)
    try:
        execute_close(plan, origin)
    except ShellError as exc:
        assert "affected by closing changed" in str(exc)
    else:
        raise AssertionError("A changed close scope should be rejected")

    # Reported lifecycle authority exercises the real agent API focus/seen behavior.
    agent_panes = []
    for status in ("blocked", "done", "working", "idle"):
        p = client.call("tab.create", workspace_id=origin.workspace, focus=False)["root_pane"]
        agent_panes.append((status, p["pane_id"]))
        if status == "done":
            client.call("pane.report_agent", pane_id=p["pane_id"], source="herdr-shell-test", agent="codex", state="working", seq=1)
        client.call("pane.report_agent", pane_id=p["pane_id"], source="herdr-shell-test", agent="codex",
                    state="idle" if status == "done" else status, seq=2)
    statuses = {a["pane_id"]: a["agent_status"] for a in client.snapshot()["agents"]}
    assert statuses[dict(agent_panes)["done"]] == "done", statuses
    current = origin.pane; focused = []
    for _ in range(4):
        execute("agent-cycle-next", context(client, current))
        current = client.snapshot()["focused_pane_id"]; focused.append(current)
    assert focused == [p for _, p in agent_panes], focused
    assert close_plan("pane-close", context(client, dict(agent_panes)["idle"]))["needs_confirmation"]
    return {"herdr": client.snapshot()["version"], "passed": [
        "all four split directions place/focus new terminals correctly; all four directional swaps preserve process identities",
        "rotation preserves pane IDs, terminal IDs, shell and running foreground process IDs, ratios and focus",
        "rotation staging tab closes automatically; inverse restores original layout",
        "nested group refused before mutation; zoomed two-pane descendant rotates and reverses while preserving ratios, terminals, running process, focus, zoom, and staging cleanup",
        "clean-shell tab closes directly; running command requires confirmation; changed scope refuses close",
        "background sleep requires confirmation and invalidates a prior clean-shell automatic close",
        "real agent focus cycles blocked, done, working, idle despite completion becoming seen",
    ]}


def main():
    with tempfile.TemporaryDirectory(prefix="herdr-actions-probe-") as directory:
        root = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "TMUX"}
        env.update(XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
                   HERDR_CONFIG_PATH=str(root / "config.toml"))
        (root / "config.toml").write_text('onboarding=false\n[update]\nversion_check=false\nmanifest_check=false\n[ui.sound]\nenabled=false\n')
        with patch.dict(os.environ, env, clear=True), (root / "server.log").open("w+") as log:
            proc = subprocess.Popen(["herdr", "server"], env=env, stdout=log, stderr=log, start_new_session=True)
            client = Client(root / "config/herdr/herdr.sock")
            try:
                eventually(lambda: Path(client.path).exists())
                print(json.dumps(exercise(root, client), indent=2))
            finally:
                if Path(client.path).exists():
                    client.call("server.stop")
                proc.wait(timeout=5)


if __name__ == "__main__":
    main()
