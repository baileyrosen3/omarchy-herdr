"""Exercise Key Quest's real curses game in disposable Herdr/tmux servers."""
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
from herdr_shell.actions import execute
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
    client.call("plugin.link", path=str(ROOT), enabled=True)
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

    (root / "tmux.conf").write_text("set -g remain-on-exit on\n")
    tmux("-f", str(root / "tmux.conf"), "new-session", "-d", "-s", "quest", "-x", "140", "-y", "52", "herdr")
    try:
        eventually(lambda: "Untouched original" in screen(), "client attach")
        make_directory = tempfile.mkdtemp
        with patch("herdr_shell.trainer.tempfile.mkdtemp", side_effect=lambda **kwargs: make_directory(dir=root, **kwargs)):
            opened = open_game(origin)
        game_pane = opened["plugin_pane"]["pane"]
        guide = Context(client.path, game_pane["pane_id"], game_pane["workspace_id"],
                        game_pane["tab_id"], opened["practice_directory"], game_pane["terminal_id"])
        eventually(lambda: "Build muscle memory" in screen(), "real game welcome")
        keys("Enter")
        deck = missions()
        for index, mission in enumerate(deck):
            count = f"Challenge {index + 1}/{len(deck)}"
            eventually(lambda: count in screen(), "challenge " + mission.id)
            if not mission.live:
                literal(mission.answer)
                keys("Enter")
            else:
                keys("Enter")
                # Practice remains readable in the zoomed guide until the
                # player's action moves focus or changes zoom.
                if mission.id == "live-pane-rotate":
                    eventually(lambda: not client.call("layout.export", tab_id=guide.tab)["layout"]["zoomed"], "armed rotation")
                else:
                    eventually(lambda: ("Live!" in screen() or "LIVE:" in screen()), "armed " + mission.id)
                execute(mission.id.removeprefix("live-"), guide)
            if index < len(deck) - 1:
                eventually(lambda: f"Challenge {index + 2}/{len(deck)}" in screen(), "clear " + mission.id)
        complete = eventually(lambda: "QUEST COMPLETE" in screen() and screen(), "complete all challenges")
        assert "390/390 points" in complete, complete
        assert "39/39 challenges cleared" in complete, complete
        assert client.call("pane.get", pane_id=origin.pane)["pane"]["terminal_id"] == origin.terminal
        assert len([t for t in client.snapshot()["tabs"] if t["workspace_id"] == origin.workspace]) == 1
        assert len([p for p in client.snapshot()["panes"] if p["workspace_id"] == origin.workspace]) == 1
        assert not client.snapshot()["agents"]
        before = {(p["pane_id"], p["terminal_id"]) for p in client.snapshot()["panes"] if p["workspace_id"] == guide.workspace}
        keys("Escape")
        eventually(lambda: "QUEST COMPLETE" not in screen(), "game exits")
        remaining = {(p["pane_id"], p["terminal_id"]) for p in client.snapshot()["panes"] if p["workspace_id"] == guide.workspace}
        # The trainer itself may be closed by Herdr when its command exits.
        assert before - remaining <= {(guide.pane, guide.terminal)}, (before, remaining)
        assert remaining
        assert (Path(opened["practice_directory"]) / "README.txt").exists()
        return {"passed": ["real plugin game in new labeled workspace", "all 26 recall questions accept suffixes including Shift",
                           "all 8 live objectives verify real Herdr API effects", "5 feature scenarios and full 390-point completion",
                           "original workspace untouched, no agent launches", "Escape keeps practice shells and directory"]}
    except Exception:
        Path("/tmp/herdr-trainer-last-capture.txt").write_text(screen())
        details = {"snapshot": client.snapshot(), "plugins": client.call("plugin.list")}
        details["logs"] = client.call("plugin.log.list", plugin_id="blr.herdr-shell")
        details["game_read"] = client.call("pane.read", pane_id=guide.pane, source="visible")
        details["game_process"] = client.call("pane.process_info", pane_id=guide.pane)
        Path("/tmp/herdr-trainer-last-state.json").write_text(json.dumps(details, indent=2))
        raise
    finally:
        tmux("kill-server")


def main():
    with tempfile.TemporaryDirectory(prefix="herdr-trainer-probe-") as directory:
        root = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "TMUX"}
        env.update(XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
                   HERDR_CONFIG_PATH=str(root / "config.toml"))
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
