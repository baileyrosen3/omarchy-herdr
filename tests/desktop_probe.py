"""Opt-in desktop checks. Opens one disposable foot window and restores focus."""
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
from herdr_shell import desktop
from herdr_shell.runtime import Client, ShellError, run_herdr
from probe import eventually


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10, **kwargs).stdout


def main():
    if not desktop.enabled():
        raise RuntimeError("Enable the desktop integration before running this opt-in test.")
    original = json.loads(run("hyprctl", "-j", "activewindow"))
    checks = []
    with tempfile.TemporaryDirectory(prefix="herdr-desktop-probe-") as directory:
        root = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "TMUX"}
        env.update(XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
                   HERDR_CONFIG_PATH=str(root / "config.toml"))
        (root / "config.toml").write_text('onboarding = false\n[update]\nversion_check = false\nmanifest_check = false\n[ui.sound]\nenabled = false\n')
        with (root / "log").open("w+") as log:
            server = subprocess.Popen(["herdr", "server"], env=env, stdout=log, stderr=log, start_new_session=True)
            client = Client(root / "config/herdr/herdr.sock")
            terminal = None
            try:
                eventually(lambda: Path(client.path).exists(), "QA socket")
                with patch.dict(os.environ, env, clear=True):
                    run_herdr(["plugin", "link", str(ROOT)])
                first = client.call("workspace.create", label="Desktop QA one", cwd=str(root), focus=True)
                second = client.call("workspace.create", label="Desktop QA two", cwd=str(root), focus=False)
                pane = first["root_pane"]
                client.call("pane.focus", pane_id=pane["pane_id"])
                terminal = subprocess.Popen(["foot", "-c", "/dev/null", "-a", "herdr-shell-qa", "-W", "140x42", "herdr"],
                                            env=env, stdout=log, stderr=log, start_new_session=True)
                def window():
                    return next((w for w in json.loads(run("hyprctl", "-j", "clients")) if w["pid"] == terminal.pid), None)
                win = eventually(window, "QA terminal window")
                run("hyprctl", "dispatch", "focuswindow", "address:" + win["address"])
                eventually(lambda: desktop.find_clients(terminal.pid), "foreground QA Herdr")
                def key(name, *mods):
                    active = json.loads(run("hyprctl", "-j", "activewindow"))
                    if active.get("pid") != terminal.pid:
                        raise RuntimeError("QA window lost focus; refusing to send keys.")
                    args = ["wtype"]
                    for mod in mods:
                        args += ["-M", mod]
                    run(*args, "-k", name)
                    time.sleep(.15)
                def layout():
                    return next(l for l in client.snapshot()["layouts"] if l["workspace_id"] == pane["workspace_id"])
                time.sleep(.3)
                before = len(client.snapshot()["panes"])
                key("r", "logo")
                eventually(lambda: len(client.snapshot()["panes"]) == before + 1, "Super+R splits Herdr")
                right = client.snapshot()["focused_pane_id"]
                key("Left", "logo")
                eventually(lambda: client.snapshot()["focused_pane_id"] == pane["pane_id"], "Super+Left focuses pane")
                key("Right", "logo")
                eventually(lambda: client.snapshot()["focused_pane_id"] == right, "Super+Right focuses pane")
                checks.append("Super+R creates a pane; Super+arrows focus panes")

                key("m", "logo")
                eventually(lambda: layout()["zoomed"], "Super+M zooms Herdr")
                assert window()["fullscreen"] == 0
                key("m", "logo")
                eventually(lambda: not layout()["zoomed"], "Super+M restores Herdr")
                checks.append("Super+M zooms Herdr without fullscreening the desktop window")

                old_layout = layout()
                key("Left", "logo", "shift")
                eventually(lambda: layout()["panes"] != old_layout["panes"], "Super+Shift+Left swaps panes")
                old_layout = layout()
                key("equal", "logo")
                eventually(lambda: layout()["splits"] != old_layout["splits"], "Super+Equal resizes split")
                checks.append("Super+Shift+arrows swap panes; Super+Equal resizes")

                second_number = second["workspace"]["number"]
                key(str(second_number), "logo")
                eventually(lambda: client.snapshot()["focused_workspace_id"] == second["workspace"]["workspace_id"], "Super+number selects workspace")
                assert window()["workspace"] == win["workspace"]
                key(str(first["workspace"]["number"]), "logo")
                eventually(lambda: client.snapshot()["focused_workspace_id"] == pane["workspace_id"], "return to first workspace")
                moved_terminal = next(p["terminal_id"] for p in client.snapshot()["panes"] if p["pane_id"] == client.snapshot()["focused_pane_id"])
                key(str(second_number), "logo", "shift")
                eventually(lambda: any(p["terminal_id"] == moved_terminal and p["workspace_id"] == second["workspace"]["workspace_id"] for p in client.snapshot()["panes"]), "Super+Shift+number moves pane")
                checks.append("Super+number switches Herdr workspace; Shift moves a pane; desktop stays put")

                focused = client.snapshot()["focused_pane_id"]
                workspace = client.snapshot()["focused_workspace_id"]
                old_tab = client.snapshot()["focused_tab_id"]
                new_tab = client.call("tab.create", workspace_id=workspace, cwd=str(root), focus=True)["tab"]["tab_id"]
                key("Left", "logo", "ctrl")
                eventually(lambda: client.snapshot()["focused_tab_id"] == old_tab, "previous tab")
                key("Right", "logo", "ctrl")
                eventually(lambda: client.snapshot()["focused_tab_id"] == new_tab, "next tab")
                checks.append("Super+Ctrl+arrows switch Herdr tabs")

                # A menu popup adds a Python foreground process owned by this isolated server.
                def popup_count():
                    return len([p for p in Path('/proc').iterdir() if p.name.isdigit() and is_menu(p)])
                def is_menu(p):
                    try:
                        args = (p / 'cmdline').read_bytes()
                        return b'bin/herdr-shell\0_menu\0' in args and env['XDG_CONFIG_HOME'].encode() in (p / 'environ').read_bytes()
                    except OSError:
                        return False
                for name in ("space", "k"):
                    count = popup_count()
                    key(name, "logo")
                    eventually(lambda: popup_count() > count, "Super+" + name + " opens popup")
                    client.call("popup.close")
                    eventually(lambda: popup_count() == count, "popup closes")
                checks.append("Super+Space opens menu; Super+K opens keys")

                desktop.set_enabled(False)
                key("f", "logo")
                eventually(lambda: window()["fullscreen"] != 0, "disabled bridge restores desktop fullscreen")
                key("f", "logo")
                eventually(lambda: window()["fullscreen"] == 0, "desktop fullscreen restores")
                desktop.set_enabled(True)
                checks.append("desktop fullscreen remains available while the bridge is disabled")

                # Replace only our test terminal with an ordinary foreground process.
                terminal.terminate(); terminal.wait(timeout=5)
                terminal = subprocess.Popen(["foot", "-c", "/dev/null", "-a", "herdr-shell-qa", "sleep", "60"],
                                            env=env, stdout=log, stderr=log, start_new_session=True)
                win = eventually(window, "ordinary QA terminal")
                run("hyprctl", "dispatch", "focuswindow", "address:" + win["address"])
                key("f", "logo")
                eventually(lambda: window()["fullscreen"] != 0, "ordinary terminal retains fullscreen")
                checks.append("ordinary terminal keeps the desktop Super+F binding with bridge enabled")
                assert not run("hyprctl", "configerrors").strip()
                result = {"herdr": client.snapshot()["version"], "passed": checks}
                (ROOT / "docs/desktop-checks.json").write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result, indent=2), flush=True)
            except Exception:
                log.flush(); log.seek(0); print(log.read()[-5000:], flush=True)
                raise
            finally:
                desktop.set_enabled(True)
                if terminal and terminal.poll() is None:
                    terminal.terminate(); terminal.wait(timeout=5)
                if Path(client.path).exists():
                    client.call("server.stop")
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.terminate(); server.wait(timeout=5)
                if original.get("address"):
                    run("hyprctl", "dispatch", "focuswindow", "address:" + original["address"])


if __name__ == "__main__":
    main()
