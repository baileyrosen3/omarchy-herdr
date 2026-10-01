"""Exercise all live Key Quest missions in disposable Herdr/tmux servers.

Chord events enter the real private input broker; the game performs and observes
actual Herdr actions. Only the active desktop-profile check is a fixture. This
does not verify compositor input or install a desktop bridge on the host.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from herdr_shell import desktop, game_input
from herdr_shell.runtime import Client, Context, ShellError
from herdr_shell.trainer import missions, open_game


def eventually(predicate, label, timeout=10):
    until = time.monotonic() + timeout
    last = None
    while time.monotonic() < until:
        try:
            last = predicate()
            if last:
                return last
        except (ShellError, OSError) as exc:
            last = exc
        time.sleep(.08)
    raise AssertionError(f"Timed out: {label}; last={last}")


def exercise(root, client, env):
    original = client.call("workspace.create", label="Untouched original", cwd=str(root), focus=True)
    p = original["root_pane"]
    origin = Context(client.path, p["pane_id"], p["workspace_id"], p["tab_id"], str(root), p["terminal_id"])
    fixture = root / "plugin"
    for name in ("bin", "herdr_shell", "integrations", "assets"):
        shutil.copytree(ROOT / name, fixture / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("herdr-plugin.toml", "LICENSE"):
        shutil.copy2(ROOT / name, fixture / name)
    (fixture / "bin/herdr-shell").write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herdr_shell import desktop
desktop.effective_bindings = lambda **kwargs: {action: {"active": True, "status": "ready", "reason": "Isolated QA profile fixture"}
                                             for _, action, _ in desktop.MAPPINGS}
desktop.popup_environment = lambda: {}
from herdr_shell.cli import main
raise SystemExit(main())
''')
    client.call("plugin.link", path=str(fixture), enabled=True)
    tmux_socket = root / "tmux.sock"

    def tmux(*args):
        result = subprocess.run(["tmux", "-S", str(tmux_socket), *args], env=env,
                                text=True, capture_output=True, timeout=8)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    def screen():
        return tmux("capture-pane", "-p", "-t", "quest:0.0")

    def keys(*values):
        tmux("send-keys", "-t", "quest:0.0", *values)

    def literal(value):
        tmux("send-keys", "-t", "quest:0.0", "-l", value)

    def focused_context():
        current = client.snapshot()
        pane = next(p for p in current["panes"] if p["pane_id"] == current["focused_pane_id"])
        return Context(client.path, pane["pane_id"], pane["workspace_id"], pane["tab_id"],
                       pane.get("foreground_cwd") or pane.get("cwd") or str(root), pane["terminal_id"])

    def focus(context):
        context.validate()
        client.call("workspace.focus", workspace_id=context.workspace)
        client.call("pane.focus", pane_id=context.pane)

    def unchanged(before):
        after = client.snapshot()
        assert {(p["pane_id"], p["terminal_id"], p["workspace_id"], p["tab_id"]) for p in before["panes"]} == \
               {(p["pane_id"], p["terminal_id"], p["workspace_id"], p["tab_id"]) for p in after["panes"]}

    (root / "tmux.conf").write_text("set -g remain-on-exit on\n")
    tmux("-f", str(root / "tmux.conf"), "new-session", "-d", "-s", "quest", "-x", "140", "-y", "52", "herdr")
    guide = None
    try:
        eventually(lambda: "Untouched original" in screen(), "client attach")
        make_directory = tempfile.mkdtemp
        with patch("herdr_shell.trainer.tempfile.mkdtemp", side_effect=lambda **kwargs: make_directory(dir=root, **kwargs)), \
                patch("herdr_shell.desktop.popup_environment", return_value={}):
            opened = open_game(origin)
        game_pane = opened["plugin_pane"]["pane"]
        guide = Context(client.path, game_pane["pane_id"], game_pane["workspace_id"],
                        game_pane["tab_id"], opened["practice_directory"], game_pane["terminal_id"])
        eventually(lambda: "Watch the action happen" in screen(), "real game welcome")
        initial = next(p for p in client.snapshot()["panes"] if p["tab_id"] == guide.tab and p["pane_id"] != guide.pane)
        initial_root = Context(client.path, initial["pane_id"], initial["workspace_id"], initial["tab_id"],
                               opened["practice_directory"], initial["terminal_id"])
        before = client.snapshot()
        focus(initial_root)
        blocked = game_input.route("agent-new", focused_context())
        assert blocked and blocked["game"] == "blocked", blocked
        unchanged(before)
        focus(guide)
        keys("Enter")
        deck = missions()
        assert len(deck) == 26 and all(mission.live for mission in deck)
        presses = 0
        earlier = None
        for index, mission in enumerate(deck):
            count = f"{index + 1}/{len(deck)} missions"
            eventually(lambda: count in screen(), "challenge " + mission.id)
            keys("Enter")
            marker = eventually(lambda: (value if (value := game_input._read_marker(game_input._marker_path(client.path)))
                                         and value.get("action") == mission.id else None), "armed " + mission.id)
            eventually(lambda: "Waiting for chord" in screen(), "ready board " + mission.id)
            assert client.snapshot()["focused_pane_id"] == guide.pane
            assert not client.call("layout.export", tab_id=guide.tab)["layout"]["zoomed"]
            if index == 0:
                before = client.snapshot()
                literal(mission.answer)
                eventually(lambda: "Typing its letter" in screen(), "ordinary letter rejected")
                keys("Enter")
                time.sleep(.2)
                assert count in screen()
                after = client.snapshot()
                assert {(p["pane_id"], p["terminal_id"]) for p in before["panes"]} == \
                       {(p["pane_id"], p["terminal_id"]) for p in after["panes"]}
                wrong = game_input.route("agent-new", guide)
                assert wrong["game"] == "wrong", wrong
                eventually(lambda: "Try the shortcut for this challenge" in screen(), "wrong chord rejected")
                assert count in screen()
                assert not client.snapshot()["agents"]
                before = client.snapshot()
                focus(initial_root)
                wrong = game_input.route("agent-new", focused_context())
                assert wrong and wrong["game"] == "wrong", wrong
                unchanged(before)
                focus(origin)
                assert game_input.route("agent-new", focused_context()) is None
                unchanged(before)
                focus(guide)
                fixture_row = next(p for p in marker["scope"] if p["tab_id"] != guide.tab)
                earlier = Context(client.path, fixture_row["pane_id"], fixture_row["workspace_id"],
                                  fixture_row["tab_id"], opened["practice_directory"], fixture_row["terminal_id"])
            elif index == 1:
                before = client.snapshot()
                focus(earlier)
                wrong = game_input.route("agent-new", focused_context())
                assert wrong and wrong["game"] == "wrong", wrong
                unchanged(before)
                assert not client.snapshot()["agents"]
                focus(guide)
                eventually(lambda: count in screen(), "return from earlier practice fixture")
            required = 4 if mission.id.startswith("agent-cycle-") else 2 if mission.id in ("menu", "pane-zoom") else 1
            for stage in range(required):
                def submit():
                    result = game_input.route(mission.id, focused_context())
                    if result and result.get("game") == "busy":
                        return None
                    assert result and result.get("game") == "queued", (mission.id, stage, result)
                    return result
                eventually(submit, "route " + mission.id)
                presses += 1
                if index == 0:
                    duplicate = game_input.route(mission.id, guide)
                    assert duplicate["game"] == "busy", duplicate
                if stage + 1 < required:
                    if mission.id == "menu":
                        eventually(lambda: "Practice menu: browse shortcuts" in screen(), "actual practice menu opens")
                        assert desktop.menu_running(client.path)
                    else:
                        eventually(lambda: f"{stage + 1}/{required} verified presses" in screen(),
                                   f"verified stage {stage + 1} of " + mission.id)
            if index < len(deck) - 1:
                eventually(lambda: f"{index + 2}/{len(deck)} missions" in screen(), "clear " + mission.id)
            assert not Path(env["HERDR_SHELL_QA_UNEXPECTED_AGENT"]).exists(), "Game invoked a configured real Omarchy agent"
        complete = eventually(lambda: "QUEST COMPLETE" in screen() and screen(), "complete all challenges")
        assert "260/260 points" in complete, complete
        assert "26/26 missions verified" in complete, complete
        assert presses == 34, presses
        assert client.call("pane.get", pane_id=origin.pane)["pane"]["terminal_id"] == origin.terminal
        assert len([t for t in client.snapshot()["tabs"] if t["workspace_id"] == origin.workspace]) == 1
        assert len([p for p in client.snapshot()["panes"] if p["workspace_id"] == origin.workspace]) == 1
        assert all(p["workspace_id"] != origin.workspace for p in client.snapshot()["agents"])
        before = {(p["pane_id"], p["terminal_id"]) for p in client.snapshot()["panes"] if p["workspace_id"] == guide.workspace}
        keys("Escape")
        eventually(lambda: "QUEST COMPLETE" not in screen(), "game exits")
        eventually(lambda: not game_input._marker_path(client.path).exists(), "input broker expires on exit")
        assert game_input.route("pane-close", guide) is None
        eventually(lambda: not client.snapshot()["agents"], "owned simulator reports disappear after game exit")
        remaining = {(p["pane_id"], p["terminal_id"]) for p in client.snapshot()["panes"] if p["workspace_id"] == guide.workspace}
        # The trainer itself may be closed by Herdr when its command exits.
        assert before - remaining <= {(guide.pane, guide.terminal)}, (before, remaining)
        assert remaining
        assert (Path(opened["practice_directory"]) / "README.txt").exists()
        return {"passed": ["real curses game and visible unzoomed guide in its own workspace",
                           "all 26 live missions verify actual Herdr effects through 34 private-broker shortcut events",
                           "typed letters/wrong chords cannot act or score; repeated presses wait; unrelated workspace input stays outside the game",
                           "initial practice root and earlier live fixtures keep consuming wrong chords across missions; original user workspace remains outside scope",
                           "all four split/swap directions, rotation, zoom/restore, pane/tab/workspace creation/close/navigation",
                           "harmless simulated agent creation and four-priority cycles in both directions; temporary-repository Git pane",
                           "actual browsing-only menu opens/closes through two M events; full 260-point completion",
                           "original workspace untouched; Escape keeps practice shells/directory, expires broker, and clears simulated agents"],
                "fixtures": ["active-profile check supplied only in a temporary plugin copy", "shortcut events submitted through real Unix IPC broker"],
                "git_pane": "real Lazygit" if shutil.which("lazygit") else "labeled harmless simulator (Lazygit unavailable)",
                "limits": ["compositor key routing for the redesigned game remains unverified by this probe"]}
    except Exception:
        Path("/tmp/herdr-trainer-last-capture.txt").write_text(screen())
        details = {"snapshot": client.snapshot(), "plugins": client.call("plugin.list")}
        details["logs"] = client.call("plugin.log.list", plugin_id="blr.herdr-shell")
        # Keep the last disposable targets' output/process metadata when a
        # launch fails. Only this private server's processes are inspected.
        details["recent_targets"] = []
        recent = [p for p in details["snapshot"]["panes"] if guide is not None and p["workspace_id"] == guide.workspace]
        for pane in recent[-4:]:
            diagnostic = {"pane_id": pane["pane_id"]}
            try:
                diagnostic["read"] = client.call("pane.read", pane_id=pane["pane_id"], source="visible")
                info = client.call("pane.process_info", pane_id=pane["pane_id"])["process_info"]
                diagnostic["process_info"] = info
                diagnostic["git_environments"] = {}
                for process in info.get("foreground_processes", []):
                    proc_root = Path("/proc") / str(process["pid"])
                    if proc_root.joinpath("cwd").resolve().is_relative_to(root):
                        git_env = dict(item.decode().split("=", 1)
                                       for item in proc_root.joinpath("environ").read_bytes().split(b"\0")
                                       if item.startswith(b"GIT_") and b"=" in item)
                        safe_keys = {"GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
                        diagnostic["git_environments"][str(process["pid"])] = {
                            "pins": {k: v for k, v in git_env.items() if k in safe_keys},
                            "other_keys": sorted(set(git_env) - safe_keys),
                        }
            except (ShellError, OSError, UnicodeError, KeyError) as exc:
                diagnostic["error"] = str(exc)
            details["recent_targets"].append(diagnostic)
        if guide is not None:
            for name, method, params in (("game_read", "pane.read", {"source": "visible"}),
                                         ("game_process", "pane.process_info", {})):
                try:
                    details[name] = client.call(method, pane_id=guide.pane, **params)
                except ShellError as exc:
                    details[name] = {"error": str(exc)}
        Path("/tmp/herdr-trainer-last-state.json").write_text(json.dumps(details, indent=2))
        raise
    finally:
        tmux("kill-server")


def main():
    with tempfile.TemporaryDirectory(prefix="herdr-trainer-probe-") as directory:
        root = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "XDG_")) and k not in ("TMUX", "HOME")}
        (root / "home").mkdir()
        (root / "bin").mkdir()
        agent_error = root / "unexpected-real-agent.txt"
        (root / "bin/omarchy").write_text('#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\n'
            'Path(os.environ["HERDR_SHELL_QA_UNEXPECTED_AGENT"]).write_text(repr(sys.argv))\nraise SystemExit(86)\n')
        (root / "bin/omarchy").chmod(0o755)
        env.update(XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
                   HERDR_CONFIG_PATH=str(root / "config.toml"), HOME=str(root / "home"),
                   HERDR_SHELL_QA_UNEXPECTED_AGENT=str(agent_error), PATH=str(root / "bin") + os.pathsep + env["PATH"])
        (root / "config.toml").write_text("onboarding=false\n[update]\nversion_check=false\nmanifest_check=false\n[ui.sound]\nenabled=false\n")
        with patch.dict(os.environ, env, clear=True), (root / "server.log").open("w+") as log:
            proc = subprocess.Popen(["herdr", "server"], env=env, stdout=log, stderr=log, start_new_session=True)
            client = Client(root / "config/herdr/herdr.sock")
            try:
                eventually(lambda: Path(client.path).exists(), "server socket")
                print(json.dumps(exercise(root, client, env), indent=2))
            finally:
                if Path(client.path).exists():
                    client.call("server.stop")
                proc.wait(timeout=5)


if __name__ == "__main__":
    main()
