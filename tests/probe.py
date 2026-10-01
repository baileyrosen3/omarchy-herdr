"""Live integration checks in disposable Herdr and tmux servers under /tmp."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from herdr_shell import PLUGIN_ID
from herdr_shell.actions import execute, open_ui
from herdr_shell.config import ConfigStore, shortcut_changes
from herdr_shell.runtime import Client, Context, ShellError, run_herdr


def eventually(predicate, label, timeout=8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (ShellError, OSError) as exc:
            last = exc
        time.sleep(.08)
    raise AssertionError(f"Timed out: {label}; last result {last}")


def exercise(root, client):
    checks = []
    created = client.call("workspace.create", label="Shell QA", cwd=str(root), focus=True)
    pane = created["root_pane"]
    context = Context(client.path, pane["pane_id"], pane["workspace_id"], pane["tab_id"], str(root), pane["terminal_id"])
    os.environ.update(HERDR_SOCKET_PATH=client.path, HERDR_PANE_ID=context.pane, HERDR_ENV="1")
    linked = json.loads(run_herdr(["plugin", "link", str(ROOT)]))
    print("Linked", linked["result"]["plugin"]["plugin_id"], flush=True)
    store = ConfigStore()
    reload = lambda: client.call("server.reload_config")
    store.apply(store.prepare(shortcut_changes()), reload)
    store.validate(store.read())
    checks.append("manifest link, config diagnostics and shortcut install")
    try:
        store.validate(store.read().replace('prefix = "ctrl+space"', 'prefix = "flibble+invalid"'))
    except ShellError:
        checks.append("native invalid-key rejection")
    else:
        raise AssertionError("Herdr unexpectedly accepted an invalid key")

    execute("pane-split-right", context)
    execute("pane-zoom", context)
    assert client.snapshot()["layouts"][0]["zoomed"]
    execute("pane-zoom", context)
    checks.append("actions stay pinned to the original pane after focus changes")
    client.call("pane.focus", pane_id=context.pane)

    tmux_socket = root / "tmux.sock"
    def tmux(*args):
        terminal_env = {k: v for k, v in os.environ.items() if k not in
                        ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID")}
        result = subprocess.run(["tmux", "-S", str(tmux_socket), *args], capture_output=True,
                                text=True, timeout=8, env=terminal_env)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    def screen():
        return tmux("capture-pane", "-p", "-t", "qa:0.0")

    def keys(*args):
        tmux("send-keys", "-t", "qa:0.0", *args)

    def literal(value):
        tmux("send-keys", "-t", "qa:0.0", "-l", value)

    (root / "tmux.conf").write_text('set -g remain-on-exit on\n')
    tmux("-f", str(root / "tmux.conf"), "new-session", "-d", "-s", "qa", "-x", "132", "-y", "44", "herdr")
    try:
        eventually(lambda: "Shell QA" in screen(), "Herdr terminal attach")
        keys("C-Space", "Space")
        eventually(lambda: "Welcome to Herdr Shell" in screen(), "first-use welcome")
        keys("Enter")
        eventually(lambda: "One shortcut family" in screen(), "walkthrough opens from welcome")
        keys("Right")
        eventually(lambda: "Split and swap" in screen(), "walkthrough next page")
        keys("Left")
        eventually(lambda: "One shortcut family" in screen(), "walkthrough previous page")
        keys("Escape")
        eventually(lambda: "One shortcut family" not in screen(), "walkthrough dismissal")
        keys("C-Space", "Space")
        eventually(lambda: "HERDR  ×  OMARCHY" in screen(), "menu shortcut opens popup")
        assert "Welcome to Herdr Shell" not in screen()
        checks.append("first-use welcome, walkthrough next/back, dismissal and non-repeating welcome")
        (ROOT / "docs").mkdir(exist_ok=True)
        (ROOT / "docs/menu-capture.txt").write_text(screen())
        checks.append("real prefix shortcut opens native popup")

        keys("End")
        eventually(lambda: "26/26" in screen() and "Close menu" in screen(), "all shortcuts reachable on Home")
        keys("Enter")
        eventually(lambda: "HERDR  ×  OMARCHY" not in screen(), "menu reference entry dismisses popup")
        keys("C-Space", "Space")
        eventually(lambda: "Hold Super+Alt" in screen(), "compact Home reopens")
        checks.append("compact Home lists 26 shortcuts and menu toggle dismisses instead of reopening")

        keys("F2")
        eventually(lambda: "Keybindings" in screen(), "keybinding screen")
        literal("zoom default")
        keys("Enter")
        eventually(lambda: "TOML array" in screen(), "keybinding edit prompt")
        keys("C-u")
        literal('"prefix+f"')
        keys("Enter")
        eventually(lambda: "Review configuration" in screen(), "diff preview")
        (ROOT / "docs/keybinding-preview.txt").write_text(screen())
        keys("Enter")
        eventually(lambda: tomllib.loads(store.read())["keys"].get("zoom") == "prefix+f", "binding saved")
        eventually(lambda: "Saved and reloaded" in screen(), "successful save feedback")
        keys("Escape", "Escape", "Escape")
        eventually(lambda: "HERDR  ×  OMARCHY" not in screen(), "popup closes on Escape")
        keys("C-Space", "f")
        eventually(lambda: client.snapshot()["layouts"][0]["zoomed"], "reloaded shortcut works in client")
        keys("C-Space", "f")
        eventually(lambda: not client.snapshot()["layouts"][0]["zoomed"], "zoom toggles back")
        checks.append("binding edit, diff, save, and immediate client key reload")

        keys("C-Space", "Space")
        eventually(lambda: "HERDR  ×  OMARCHY" in screen(), "reopen menu")
        keys("F4")
        eventually(lambda: "Review configuration" in screen(), "undo preview")
        keys("Enter")
        eventually(lambda: "zoom" not in tomllib.loads(store.read())["keys"], "undo removes only added key")
        eventually(lambda: "Undone and reloaded" in screen(), "undo feedback")
        keys("Escape")
        eventually(lambda: "HERDR  ×  OMARCHY" not in screen(), "undo menu closes")
        checks.append("live configuration undo")

        keys("C-Space", "Space")
        eventually(lambda: "HERDR  ×  OMARCHY" in screen(), "reopen for action")
        literal("Toggle pane zoom")
        keys("Enter")
        eventually(lambda: client.snapshot()["layouts"][0]["zoomed"], "deferred action executes after popup exit")
        eventually(lambda: "HERDR  ×  OMARCHY" not in screen(), "action dismisses menu")
        checks.append("popup teardown preserves the selected action's result")
        execute("pane-zoom", context)

        open_ui(context, "settings")
        eventually(lambda: "Settings" in screen() and "HERDR" in screen(), "settings popup")
        (ROOT / "docs/settings-capture.txt").write_text(screen())
        tmux("resize-window", "-t", "qa:0", "-x", "55", "-y", "18")
        time.sleep(.25)
        assert "Traceback" not in screen()
        tmux("resize-window", "-t", "qa:0", "-x", "132", "-y", "44")
        eventually(lambda: "Settings" in screen(), "resize recovery")
        client.call("popup.close")
        eventually(lambda: "HERDR  ×  OMARCHY" not in screen(), "settings closes")
        checks.append("settings popup and terminal resize")

        client.call("plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="tool", placement="popup",
                    cwd=context.cwd,
                    env={"HERDR_SHELL_CONTEXT": context.encode(), "HERDR_SHELL_TOOL": json.dumps([
                        "python3", "-c", "import time; print('HERDR_TOOL_PROBE', flush=True); time.sleep(1)"])})
        eventually(lambda: "HERDR_TOOL_PROBE" in screen(), "tool entrypoint runs argv command")
        eventually(lambda: "HERDR_TOOL_PROBE" not in screen(), "tool exit restores terminal")
        checks.append("tool popup launch and automatic close")

        # The close policy skips confirmation for idle shells. Run disposable
        # work in this probe's captured pane to exercise the confirming path.
        literal("sleep 60")
        keys("Enter")

        def running_sleep():
            result = client.call("pane.process_info", pane_id=context.pane)
            info = result.get("process_info", result)
            return next((process for process in info.get("foreground_processes", [])
                         if process.get("name") == "sleep"), None)

        running = eventually(running_sleep, "disposable foreground work starts")
        before_panes = len(client.snapshot()["panes"])
        client.call("plugin.action.invoke", action_id=PLUGIN_ID + ".pane-close",
                    context={"focused_pane_id": context.pane, "workspace_id": context.workspace,
                             "tab_id": context.tab})
        eventually(lambda: "keep the pane" in screen(), "registered close action asks for confirmation")
        keys("Escape")
        eventually(lambda: "keep the pane" not in screen(), "cancel close confirmation")
        assert len(client.snapshot()["panes"]) == before_panes
        assert running_sleep()["pid"] == running["pid"]
        keys("C-c")
        eventually(lambda: not running_sleep(), "disposable work stops after cancellation check")
        checks.append("registered close action confirms running work and cancellation preserves its process")
    except Exception:
        print("SCREEN\n" + screen(), flush=True)
        log = root / "state/herdr-shell/actions.log"
        if log.exists():
            print("WORKER LOG\n" + log.read_text(), flush=True)
        print("PLUGIN LOGS", json.dumps(client.call("plugin.log.list", plugin_id=PLUGIN_ID)), flush=True)
        raise
    finally:
        tmux("kill-server")
    result = {"herdr": client.snapshot()["version"], "passed": checks}
    (ROOT / "docs/live-checks.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix="herdr-shell-probe-") as directory:
        root = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "TMUX"}
        env.update(XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
                   HERDR_CONFIG_PATH=str(root / "config.toml"))
        (root / "config.toml").write_text('onboarding = false\n[keys]\nprefix = "ctrl+space"\n[update]\nversion_check = false\nmanifest_check = false\n[ui.sound]\nenabled = false\n')
        with patch.dict(os.environ, env, clear=True), (root / "server.log").open("w+") as log:
            proc = subprocess.Popen(["herdr", "server"], env=env, stdout=log,
                                    stderr=log, start_new_session=True)
            socket_path = root / "config/herdr/herdr.sock"
            client = Client(socket_path)
            try:
                eventually(lambda: socket_path.exists(), "isolated server socket")
                exercise(root, client)
            finally:
                if socket_path.exists():
                    client.call("server.stop")
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
