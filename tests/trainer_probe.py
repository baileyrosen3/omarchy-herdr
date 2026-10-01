"""Exercise three learning modes in disposable Herdr/tmux servers.

Chord events enter the real private input broker; the game performs and observes
actual Herdr actions. Desktop-profile status and Quest.desktop_focused are
explicit headless fixtures. This does not verify compositor input or install
a desktop bridge on the host.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from herdr_shell import desktop, game_input, onboarding, scoring
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
from herdr_shell import trainer
from herdr_shell.trainer import Quest
Quest.desktop_focused = lambda self: True  # Headless fixture; no compositor claim.
make_private_directory = trainer.tempfile.mkdtemp
def qa_directory(**kwargs):
    kwargs.setdefault("dir", str(Path(__file__).resolve().parents[2]))
    return make_private_directory(**kwargs)
trainer.tempfile.mkdtemp = qa_directory
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
            opened = open_game(origin, mode="hands-on")
        game_pane = opened["plugin_pane"]["pane"]
        guide = Context(client.path, game_pane["pane_id"], game_pane["workspace_id"],
                        game_pane["tab_id"], opened["practice_directory"], game_pane["terminal_id"])
        eventually(lambda: "Try the keys. Watch their effects." in screen(), "Hands-on introduction")
        initial = next(p for p in client.snapshot()["panes"] if p["tab_id"] == guide.tab and p["pane_id"] != guide.pane)
        initial_root = Context(client.path, initial["pane_id"], initial["workspace_id"], initial["tab_id"],
                               opened["practice_directory"], initial["terminal_id"])
        before = client.snapshot()
        focus(initial_root)
        blocked = game_input.route("agent-new", focused_context())
        assert blocked and blocked["game"] == "blocked", blocked
        unchanged(before)
        focus(guide)
        keys("Space")
        deck = missions()
        assert len(deck) == 26 and all(mission.live for mission in deck)
        by_action = {mission.id: mission for mission in deck}
        mapping = json.dumps([(m.id, m.answer) for m in deck], sort_keys=True)
        profile = hashlib.sha256(mapping.encode()).hexdigest()
        broker_presses, demo_presses = 0, 0
        earlier = None

        def marker():
            return game_input._read_marker(game_input._marker_path(client.path))

        def ready(index, action=None, stage=0, required=None):
            count = f"{index + 1}/{len(deck)} missions"
            def armed():
                record = marker()
                capture = screen()
                if not record or record.get("action") not in by_action:
                    return None
                if action is not None and record["action"] != action:
                    return None
                if count not in capture or "Ready · press your chord." not in capture:
                    return None
                if required is not None and f"{stage}/{required} verified presses" not in capture:
                    return None
                return record
            record = eventually(armed, f"automatically armed mission {index + 1}", timeout=20)
            assert client.snapshot()["focused_pane_id"] == guide.pane
            assert not client.call("layout.export", tab_id=guide.tab)["layout"]["zoomed"]
            return record

        def required_presses(action):
            return 4 if action.startswith("agent-cycle-") else 2 if action in ("menu", "pane-zoom") else 1

        def submit(action):
            def routed():
                result = game_input.route(action, focused_context())
                if result and result.get("game") == "busy":
                    return None
                assert result and result.get("game") == "queued", (action, result)
                return result
            return eventually(routed, "route " + action)

        def perform_mission(index, action, *, demo=False):
            nonlocal broker_presses, demo_presses
            required = required_presses(action)
            for stage in range(required):
                if stage:
                    if action == "menu":
                        notice = "F4 Demo close" if demo else "Speed Run: repeat the shortcut"
                        eventually(lambda: desktop.menu_running(client.path) and notice in screen(),
                                   "actual practice menu opens")
                        # The popup renders before the guide finishes verifying
                        # its first press. F4 is a UI event and cannot retry a
                        # busy acknowledgement like submit(), so wait for the
                        # guide's committed stage before requesting its close.
                        if demo:
                            eventually(lambda: "1/2 verified presses" in client.call(
                                "pane.read", pane_id=guide.pane, source="visible")["read"]["text"],
                                       "first menu Demo is verified")
                    else:
                        ready(index, action, stage, required)
                if demo:
                    keys("F4")
                    demo_presses += 1
                else:
                    submit(action)
                    broker_presses += 1
            assert not Path(env["HERDR_SHELL_QA_UNEXPECTED_AGENT"]).exists(), "Practice invoked a configured real Omarchy agent"

        # Hands-on shows its answers and prepares every next target without
        # Enter. F4 demonstrates the first action and both real-menu presses.
        for index, mission in enumerate(deck):
            record = ready(index, mission.id)
            count = f"{index + 1}/{len(deck)} missions"
            assert "Hands-on walkthrough · no timer" in screen()
            assert "▶ Demo  Super+Alt+" + mission.answer in screen()
            if index == 0:
                before = client.snapshot()
                literal(mission.answer)
                eventually(lambda: "Typing its letter" in screen(), "ordinary letter rejected")
                unchanged(before)
                wrong = game_input.route("agent-new", focused_context())
                assert wrong["game"] == "wrong", wrong
                eventually(lambda: "Try the shortcut for this challenge" in screen(), "wrong chord rejected")
                assert count in screen()
                assert not client.snapshot()["agents"]
                before = client.snapshot()
                focus(initial_root)
                wrong = game_input.route("agent-new", focused_context())
                assert wrong and wrong["game"] in ("wrong", "blocked"), wrong
                unchanged(before)
                focus(origin)
                assert game_input.route("agent-new", focused_context()) is None
                unchanged(before)
                focus(guide)
                ready(index, mission.id)
                fixture_row = next(p for p in record["scope"] if p["tab_id"] != guide.tab)
                earlier = Context(client.path, fixture_row["pane_id"], fixture_row["workspace_id"],
                                  fixture_row["tab_id"], opened["practice_directory"], fixture_row["terminal_id"])
            elif index == 1:
                before = client.snapshot()
                focus(earlier)
                wrong = game_input.route("agent-new", focused_context())
                assert wrong and wrong["game"] in ("wrong", "blocked"), wrong
                unchanged(before)
                assert not client.snapshot()["agents"]
                focus(guide)
                ready(index, mission.id)
            perform_mission(index, mission.id, demo=index == 0 or mission.id == "menu")

        eventually(lambda: "Hands-on complete" in screen() and "Speed Run · timed + score" in screen(),
                   "Hands-on completion unlocks all learning choices", timeout=20)
        assert broker_presses == 31 and demo_presses == 3, (broker_presses, demo_presses)
        assert onboarding.has_learning_completed()
        assert not (Path(env["XDG_STATE_HOME"]) / "herdr-shell/learning-speed.json").exists(), "Untimed Hands-on saved a score"
        eventually(lambda: not client.snapshot()["agents"], "Hands-on simulators stop at activity choices")
        print("Hands-on: all 26 exercises completed automatically; F4 demos and real menu verified.", file=sys.stderr, flush=True)

        # The read-only guide returns to these same choices on completion.
        before_walkthrough = client.snapshot()
        keys("Enter")
        for title, _ in onboarding.WALKTHROUGH_PAGES:
            eventually(lambda title=title: title in screen(), "walkthrough page " + title)
            keys("Right")
        eventually(lambda: "Walkthrough complete" in screen() and "Speed Run · timed + score" in screen(),
                   "read-only walkthrough returns to unlocked choices")
        unchanged(before_walkthrough)
        previous_guide = guide
        keys("Down", "Down", "Enter")
        eventually(lambda: "Speed Run · recall, speed, accuracy" in screen(), "Speed Run introduction")
        new_record = eventually(lambda: (record if (record := marker()) and record.get("guide", {}).get("pane") != previous_guide.pane else None),
                                "fresh Speed Run input broker")
        guide = Context(**new_record["guide"])
        guide.validate()
        assert guide.workspace != previous_guide.workspace, (previous_guide, guide)
        assert Path(guide.cwd).is_relative_to(root), "Fresh practice escaped the private test directory"
        keys("Space")
        speed_started = time.monotonic()
        speed_order, assisted, pause_time = [], False, 0.0
        speed_presses = 0

        def score_header():
            match = re.search(r"(\d\d:\d\d) left · (\d+) pts · streak (\d+)", screen())
            assert match, screen()
            return match[1], int(match[2]), int(match[3])

        def frozen_clock(label, seconds=2.2):
            nonlocal pause_time
            frozen = score_header()
            started = time.monotonic()
            time.sleep(seconds)
            assert score_header() == frozen, (label, frozen, score_header())
            pause_time += time.monotonic() - started

        # The shuffled deck is intentionally not replaced by a deterministic
        # deck. Drive the action published by the actual live input broker.
        for index in range(len(deck)):
            record = ready(index)
            action = record["action"]
            assert action not in speed_order, (action, speed_order)
            speed_order.append(action)
            mission = by_action[action]
            capture = screen()
            assert "Recall the full Super+Alt shortcut." in capture
            assert "▶ Demo" not in capture and "Assisted hint ·" not in capture
            if index == 0:
                before = client.snapshot()
                assert score_header()[1:] == (0, 0)
                literal(mission.answer)
                eventually(lambda: "Typing its letter" in screen(), "Speed Run rejects typed answer")
                unchanged(before)
                assert score_header()[1:] == (0, 0)
                keys("Space")
                eventually(lambda: "Paused. Space resumes." in screen() and (marker() or {}).get("action") is None,
                           "Space pauses broker and response clock")
                paused = game_input.route(action, focused_context())
                assert paused and paused["game"] == "blocked", paused
                frozen_clock("explicit pause")
                keys("Space")
                ready(index, action)
            elif index == 1:
                _, points, streak = score_header()
                assert points >= 100 and streak >= 1
                wrong_action = "agent-new" if action != "agent-new" else "pane-split-right"
                before = client.snapshot()
                wrong = game_input.route(wrong_action, focused_context())
                assert wrong and wrong["game"] == "wrong", wrong
                eventually(lambda: "Wrong chord" in screen() and score_header()[1:] == (points - 25, 0),
                           "wrong chord deducts exactly 25 points and resets streak")
                unchanged(before)
            if index >= 2 and not assisted and required_presses(action) == 1:
                keys("F1")
                eventually(lambda: "Assisted hint · Super+Alt+" in screen() and "Clock paused for this assisted response." in screen(),
                           "F1 reveals assisted hint")
                frozen_clock("assisted hint")
                assisted = True
            before_presses = broker_presses
            perform_mission(index, action)
            speed_presses += broker_presses - before_presses

        complete = eventually(lambda: "SPEED RUN COMPLETE" in screen() and screen(), "Speed Run scoreboard", timeout=20)
        assert "26/26 missions verified" in complete, complete
        assert "correct 34 · wrong 1" in complete and "assisted presses 1" in complete, complete
        assert "Accuracy 97%" in complete, complete
        assert speed_presses == 34 and assisted, (speed_presses, assisted)
        assert set(speed_order) == set(by_action)
        saved = scoring.read_results(profile)
        assert saved["completed_runs"] == 1 and saved["best"] is None, saved
        last = saved["last"]
        assert last["correct"] == 34 and last["wrong"] == 1 and last["attempts"] == 35, last
        assert last["hinted_correct"] == 1 and 3375 <= last["points"] <= 9600, last
        assert f"{last['points']} points · 26/26 missions verified" in complete, complete
        assert abs(last["accuracy"] - 100 * 34 / 35) < 1e-9, last
        assert 0 <= last["best_time"] <= last["average_time"], last
        assert last["active_elapsed"] < time.monotonic() - speed_started - pause_time + .5, last
        assert broker_presses == 65 and demo_presses == 3, (broker_presses, demo_presses)
        assert client.call("pane.get", pane_id=origin.pane)["pane"]["terminal_id"] == origin.terminal
        assert len([t for t in client.snapshot()["tabs"] if t["workspace_id"] == origin.workspace]) == 1
        assert len([p for p in client.snapshot()["panes"] if p["workspace_id"] == origin.workspace]) == 1
        assert all(p["workspace_id"] != origin.workspace for p in client.snapshot()["agents"])
        before = {(p["pane_id"], p["terminal_id"]) for p in client.snapshot()["panes"]}
        keys("Escape")
        eventually(lambda: "SPEED RUN COMPLETE" not in screen(), "learning exits")
        eventually(lambda: not game_input._marker_path(client.path).exists(), "input broker expires on exit")
        assert game_input.route("pane-close", guide) is None
        eventually(lambda: not client.snapshot()["agents"], "owned simulator reports disappear after game exit")
        remaining = {(p["pane_id"], p["terminal_id"]) for p in client.snapshot()["panes"]}
        # The trainer itself may be closed by Herdr when its command exits.
        assert before - remaining <= {(guide.pane, guide.terminal)}, (before, remaining)
        assert remaining
        assert (Path(opened["practice_directory"]) / "README.txt").exists()
        assert (Path(guide.cwd) / "README.txt").exists()
        return {"passed": ["Hands-on automatically prepares 26 ordered exercises; F4 demonstrates a split and both real-menu presses",
                           "all eight read-only walkthrough pages preserve pane identities; Speed Run starts in a fresh guide/workspace",
                           "Speed Run drives all 26 shuffled live missions through 34 actual private-broker shortcut events",
                           "typed letters cannot act or score; one wrong chord deducts 25 and resets streak; unrelated workspace input stays outside the game",
                           "initial practice root and earlier live fixtures keep consuming wrong chords across missions; original user workspace remains outside scope",
                           "all four split/swap directions, rotation, zoom/restore, pane/tab/workspace creation/close/navigation",
                           "harmless simulated agent creation and four-priority cycles in both directions; temporary-repository Git pane",
                           "explicit pause and assisted hint freeze the displayed active clock; saved results contain 34 correct presses and one mistake",
                           "assisted completed run stays out of personal best; its actual points and accuracy match the scoreboard",
                           "original workspace untouched; Escape keeps practice shells/directory, expires broker, and clears simulated agents"],
                "verified_presses": 68, "broker_events": broker_presses, "demo_events": demo_presses,
                "speed_result": last,
                "fixtures": ["active-profile and Quest.desktop_focused checks supplied only in a temporary plugin copy",
                             "shortcut events submitted through real Unix IPC broker"],
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
