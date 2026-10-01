"""Opt-in shortcut QA in one disposable foot window and isolated Herdr server."""
import argparse
import json
import fcntl
import os
from dataclasses import asdict
from pathlib import Path
import re
import subprocess
import struct
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from herdr_shell import desktop
from herdr_shell.actions import close_plan
from herdr_shell.runtime import Client, Context, ShellError, run_herdr
from probe import eventually


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10, **kwargs).stdout


def focus_window(address):
    """Use the installed typed Lua API, selecting a verified compositor address."""
    if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", address):
        raise RuntimeError("Refusing an invalid desktop window address")
    selector = json.dumps("address:" + address)
    run("hyprctl", "eval", "local target = hl.get_window(" + selector + "); "
        "if not target then error('QA focus target no longer exists') end; "
        "hl.dispatch(hl.dsp.focus({ window = " + selector + " }))")


def move_cursor(x, y):
    """Use the documented absolute cursor dispatcher without changing config."""
    if not all(isinstance(value, (int, float)) and abs(value) < 1_000_000 for value in (x, y)):
        raise ValueError("Invalid QA cursor coordinates")
    run("hyprctl", "eval", "hl.dispatch(hl.dsp.cursor.move({ x = " + str(x) + ", y = " + str(y) + " }))")


class EphemeralKeyboard:
    """Own a temporary kernel keyboard; emit only the shortcuts used by this QA.

    Constants and native layouts come from linux/uinput.h, linux/input.h and
    linux/input-event-codes.h. This requires an already writable /dev/uinput;
    it never changes device permissions or installs an input service.
    """
    KEYS = {"Escape": 1, "q": 16, "w": 17, "r": 19, "t": 20, "u": 22, "p": 25,
            "a": 30, "d": 32, "f": 33, "j": 36, "l": 38, "z": 44, "x": 45, "v": 47,
            "m": 50, "Up": 103, "Page_Up": 104, "Left": 105,
            "Right": 106, "Down": 108, "Page_Down": 109}
    MODIFIERS = {"ctrl": 29, "shift": 42, "alt": 56, "logo": 125}
    EVENT = struct.Struct("@llHHi")

    @staticmethod
    def ioctl_code(direction, number, size=0):
        return (direction << 30) | (size << 16) | (ord("U") << 8) | number

    def __init__(self):
        self.name = "herdr-shell-qa-" + str(os.getpid())
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        self.held = []
        self.last_event = None
        self.created = False
        try:
            fcntl.ioctl(self.fd, self.ioctl_code(1, 100, struct.calcsize("@i")), 1)  # EV_KEY
            # A normal keyboard capability set lets udev/libinput recognize it.
            # Sending remains restricted to KEYS/MODIFIERS above.
            for code in range(1, 128):
                fcntl.ioctl(self.fd, self.ioctl_code(1, 101, struct.calcsize("@i")), code)
            setup = struct.pack("@HHHH80sI", 3, 0x1209, 1, 1, self.name.encode(), 0)
            fcntl.ioctl(self.fd, self.ioctl_code(1, 3, len(setup)), setup)
            fcntl.ioctl(self.fd, self.ioctl_code(0, 1))
            self.created = True
            eventually(lambda: any(k.get("name") == self.name
                                   for k in json.loads(run("hyprctl", "-j", "devices")).get("keyboards", [])),
                       "temporary QA keyboard recognized by compositor")
        except Exception:
            self.close()
            raise

    def _emit(self, code, value):
        self.last_event = {"code": code, "value": value}
        instant = time.time_ns()
        stamp = (instant // 1_000_000_000, instant % 1_000_000_000 // 1000)
        payload = (self.EVENT.pack(*stamp, 1, code, value) +
                   self.EVENT.pack(*stamp, 0, 0, 0))  # EV_SYN / SYN_REPORT
        limit = time.monotonic() + 1
        while payload:
            try:
                payload = payload[os.write(self.fd, payload):]
            except BlockingIOError:
                if time.monotonic() >= limit:
                    raise RuntimeError("Temporary QA keyboard event queue stalled")
                time.sleep(.005)

    def chord(self, name, mods, guard):
        if name not in self.KEYS or any(mod not in self.MODIFIERS for mod in mods) or len(set(mods)) != len(mods):
            raise ValueError("Unapproved QA shortcut")
        codes = [self.MODIFIERS[mod] for mod in mods] + [self.KEYS[name]]
        try:
            for code in codes:
                guard()  # Never press a key after the owned foot loses focus.
                self.held.append(code)
                self._emit(code, 1)
                time.sleep(.03)
        finally:
            # Always release, including after a focus change or failed press.
            for code in reversed(self.held[:]):
                self._emit(code, 0)
                self.held.remove(code)
                time.sleep(.03)

    def close(self):
        if self.fd is None:
            return
        try:
            for code in reversed(self.held[:]):
                try:
                    self._emit(code, 0)
                except OSError:
                    pass
            self.held.clear()
        finally:
            try:
                if self.created:
                    fcntl.ioctl(self.fd, self.ioctl_code(0, 2))
            finally:
                os.close(self.fd)
                self.fd = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launches-only", action="store_true", help="Only verify A, V, Q and Shift+Q after input sanity checks")
    args = parser.parse_args()
    original_enabled = desktop.enabled()
    if not desktop.installed() or not original_enabled:
        raise RuntimeError("Install and enable the Super+Alt bridge before running this opt-in test.")
    active = desktop.effective_bindings(refresh=True)
    missing = {action: row["reason"] for action, row in active.items() if not row["active"]}
    if missing or len(active) != 26:
        raise RuntimeError("All 26 shortcuts must be ready before desktop QA: " + json.dumps(missing))
    original = json.loads(run("hyprctl", "-j", "activewindow"))
    original_cursor = json.loads(run("hyprctl", "-j", "cursorpos"))
    original_errors = run("hyprctl", "configerrors").strip()
    checks = []
    bridge_log = desktop.state_path() / "actions.log"
    bridge_log_offset = bridge_log.stat().st_size if bridge_log.exists() else 0
    with tempfile.TemporaryDirectory(prefix="herdr-desktop-probe-") as directory:
        root = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "TMUX"}
        env.update(XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
                   HERDR_CONFIG_PATH=str(root / "config.toml"))
        fakebin = root / "fakebin"
        fakebin.mkdir()
        agent_command = root / "agent-command.json"
        # The real manifest/entrypoint dispatches its normal command, but this
        # server alone resolves omarchy to a harmless argument-recording stub.
        stub = fakebin / "omarchy"
        stub.write_text('#!/usr/bin/env python3\nimport json, os, sys, time\nfrom pathlib import Path\n'
                        'Path(os.environ["HERDR_QA_AGENT_COMMAND"]).write_text(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}))\n'
                        'time.sleep(60)\n')
        stub.chmod(0o700)
        env.update(PATH=str(fakebin) + os.pathsep + env.get("PATH", "/usr/bin"),
                   HERDR_QA_AGENT_COMMAND=str(agent_command), GIT_CONFIG_GLOBAL="/dev/null")
        repository = root / "repo"
        run("git", "-c", "init.defaultBranch=main", "init", "--quiet", str(repository), env=env)
        (root / "config.toml").write_text('onboarding=false\n[keys]\n'
            'focus_pane_left="ctrl+alt+left"\nfocus_pane_right="ctrl+alt+right"\n'
            'focus_pane_up="ctrl+alt+up"\nfocus_pane_down="ctrl+alt+down"\n'
            '[update]\nversion_check=false\nmanifest_check=false\n'
            '[ui]\nprompt_new_tab_name=false\nprompt_new_workspace_name=false\n'
            '[ui.sound]\nenabled=false\n')
        with (root / "log").open("w+") as log:
            server = subprocess.Popen(["herdr", "server"], env=env, stdout=log, stderr=log, start_new_session=True)
            client = Client(root / "config/herdr/herdr.sock")
            terminal = None
            keyboard = None
            last_key = None

            def diagnostics():
                def capture(operation):
                    try:
                        return operation()
                    except Exception as exc:
                        return {"error": str(exc)}
                focused = capture(lambda: json.loads(run("hyprctl", "-j", "activewindow")))
                own = capture(lambda: next((w for w in json.loads(run("hyprctl", "-j", "clients"))
                                           if terminal and w.get("pid") == terminal.pid), None))
                details = {"last_key": last_key, "qa_keyboard": keyboard.name if keyboard else None,
                           "last_keyboard_event": keyboard.last_event if keyboard else None,
                           "qa_root": str(root), "bridge_preferences": str(desktop.preferences_dir()),
                           "enabled": desktop.enabled(), "active_window": {k: focused.get(k) for k in ("pid", "address")},
                           "own_window": own, "python_clients": capture(lambda: [
                               {"process": asdict(process), "options": options}
                               for process, options in desktop.find_clients(terminal.pid)]) if terminal else [],
                           "registered": capture(lambda: [b for b in json.loads(run("hyprctl", "-j", "binds"))
                                                          if str(b.get("description", "")).startswith("Herdr Shell:")])}
                if terminal:
                    details["lua_candidates"] = capture(lambda: run("hyprctl", "repl", "return (function() "
                        "local b=herdr_shell_bridge; local out={}; "
                        "local w=hl.get_active_window(); out[#out+1]='active='..tostring(w and w.pid); "
                        "out[#out+1]='generation='..tostring(b and b.generation); "
                        "out[#out+1]='config='..tostring(os.getenv('XDG_CONFIG_HOME')); "
                        "local c=b and b.candidates(" + str(terminal.pid) + "); "
                        "out[#out+1]='candidates='..tostring(c and #c); "
                        "if c then for _,v in ipairs(c) do out[#out+1]='pid='..tostring(v.pid)..',start='..tostring(v.start) end end; "
                        "if b then for a,v in pairs(b.registered) do "
                        "out[#out+1]=a..':key='..tostring(v.key)..',mask='..tostring(v.modmask)..',enabled='..tostring(v.enabled)..',arg='..tostring(v.arg) end end; "
                        "return table.concat(out,'\\n') end)()").strip())
                    details["queues"] = capture(lambda: {p.name: {"bytes": p.stat().st_size,
                        "own_events": [line for line in p.read_text().splitlines() if "\t" + str(terminal.pid) + "\t" in line][-12:]}
                        for p in desktop.preferences_dir().glob("events-*.queue")})
                if bridge_log.exists():
                    with bridge_log.open() as actions:
                        actions.seek(bridge_log_offset)
                        details["new_action_log"] = actions.read()[-5000:]
                print("DESKTOP PROBE DIAGNOSTICS\n" + json.dumps(details, indent=2), flush=True)

            try:
                eventually(lambda: Path(client.path).exists(), "isolated QA socket")
                with patch.dict(os.environ, env, clear=True):
                    run_herdr(["plugin", "link", str(ROOT), "--enabled"])
                first = client.call("workspace.create", label="Desktop QA one", cwd=str(root), focus=True)
                second = client.call("workspace.create", label="Desktop QA two", cwd=str(root), focus=False)
                workspace = first["workspace"]["workspace_id"]
                client.call("pane.focus", pane_id=first["root_pane"]["pane_id"])
                terminal = subprocess.Popen(["foot", "-c", "/dev/null", "-a", "herdr-shell-qa", "-W", "140x42", "herdr"],
                                            env=env, stdout=log, stderr=log, start_new_session=True)

                def window():
                    return next((w for w in json.loads(run("hyprctl", "-j", "clients")) if w["pid"] == terminal.pid), None)

                win = eventually(window, "owned QA terminal window")
                monitor = next(m for m in json.loads(run("hyprctl", "-j", "monitors")) if m["id"] == win["monitor"])
                dimensions = [monitor["width"], monitor["height"]]
                if monitor["transform"] % 2:
                    dimensions.reverse()
                width, height = [int(size / monitor["scale"] * .8) for size in dimensions]
                x = monitor["x"] + (dimensions[0] / monitor["scale"] - width) / 2
                y = monitor["y"] + (dimensions[1] / monitor["scale"] - height) / 2
                selector = json.dumps("address:" + win["address"])
                # Keep only this disposable window independent of the user's
                # tiled layout and stable through stock fullscreen toggles.
                run("hyprctl", "eval", "hl.dispatch(hl.dsp.window.float({ window = " + selector + ", action = 'set' })); "
                    "hl.dispatch(hl.dsp.window.resize({ window = " + selector + ", x = " + str(width) + ", y = " + str(height) + ", relative = false })); "
                    "hl.dispatch(hl.dsp.window.move({ window = " + selector + ", x = " + str(x) + ", y = " + str(y) + ", relative = false }))")
                eventually(lambda: window()["floating"], "owned QA window floats independently")
                focus_window(win["address"])
                eventually(lambda: desktop.find_clients(terminal.pid), "foreground isolated QA Herdr client")
                keyboard = EphemeralKeyboard()
                # Input hotplug can refresh pointer-following focus. Select the
                # owned foot again after compositor recognition has completed.
                own = window()
                move_cursor(own["at"][0] + own["size"][0] / 2, own["at"][1] + own["size"][1] / 2)
                focus_window(win["address"])

                def guard_focus():
                    focused = json.loads(run("hyprctl", "-j", "activewindow"))
                    if focused.get("pid") != terminal.pid or focused.get("address") != win["address"]:
                        raise RuntimeError("QA window lost focus; refusing to send keys.")

                def key(name, *mods, delay=.06):
                    nonlocal last_key
                    last_key = {"name": name, "mods": list(mods), "device": keyboard.name}
                    keyboard.chord(name, mods, guard_focus)
                    if delay:
                        time.sleep(delay)

                def herdr_key(name, shift=False, delay=.06):
                    return key(name, "logo", "alt", *(["shift"] if shift else []), delay=delay)

                def snapshot():
                    return client.snapshot()

                def current_context():
                    current = snapshot()
                    p = next(p for p in current["panes"] if p["pane_id"] == current["focused_pane_id"])
                    return Context(client.path, p["pane_id"], p["workspace_id"], p["tab_id"], str(root), p["terminal_id"])

                def layout(tab=None):
                    current = snapshot()
                    return next(l for l in current["layouts"] if l["tab_id"] == (tab or current["focused_tab_id"]))

                def positions():
                    return {p["pane_id"]: p["rect"] for p in layout()["panes"]}

                def identities():
                    return {p["pane_id"]: (p["terminal_id"], p["workspace_id"], p["tab_id"]) for p in snapshot()["panes"]}

                def owned_menu_processes():
                    # Read only descendants of the server created by this test.
                    pending, seen, found = [server.pid], set(), {}
                    while pending:
                        pid = pending.pop()
                        if pid in seen:
                            continue
                        seen.add(pid)
                        if len(seen) > 512:
                            raise RuntimeError("Owned QA process tree is unexpectedly large")
                        folder = Path("/proc") / str(pid)
                        try:
                            pending.extend(int(p) for p in (folder / "task" / str(pid) / "children").read_text().split())
                            if b"_menu" in (folder / "cmdline").read_bytes().split(b"\0"):
                                variables = dict(entry.split(b"=", 1) for entry in (folder / "environ").read_bytes().split(b"\0") if b"=" in entry)
                                found[pid] = variables.get(b"HERDR_SHELL_PAGE", b"menu").decode()
                        except (FileNotFoundError, ProcessLookupError):
                            continue
                    return found

                def popup_page(page):
                    processes = owned_menu_processes()
                    return processes if len(processes) == 1 and next(iter(processes.values())) == page else None

                time.sleep(.3)
                injection_fullscreen = window()["fullscreen"]
                key("f", "logo")
                eventually(lambda: window()["fullscreen"] != injection_fullscreen, "stock Super+F verifies temporary keyboard injection")
                key("f", "logo")
                eventually(lambda: window()["fullscreen"] == injection_fullscreen, "stock fullscreen restores before Herdr tests")

                # Verify the four launch/agent chords through the real bridge.
                # All resulting processes and reported agents belong to this
                # disposable server, never the user's Herdr session.
                agent_tab = client.call("tab.create", workspace_id=workspace, cwd=str(repository), focus=True)
                count = len(snapshot()["panes"])
                herdr_key("a")
                eventually(lambda: len(snapshot()["panes"]) == count + 1 and agent_command.exists(), "A invokes isolated agent entrypoint")
                recorded = json.loads(agent_command.read_text())
                assert recorded == {"argv": ["agent", "--inline", "--pick"], "cwd": str(repository)}, recorded
                launched = next(p for p in snapshot()["panes"] if p["pane_id"] == snapshot()["focused_pane_id"])
                assert launched["tab_id"] == agent_tab["tab"]["tab_id"] and launched["workspace_id"] == workspace
                client.call("tab.close", tab_id=agent_tab["tab"]["tab_id"])

                git_tab = client.call("tab.create", workspace_id=workspace, cwd=str(repository), focus=True)
                count = len(snapshot()["panes"])
                herdr_key("v")
                eventually(lambda: len(snapshot()["panes"]) == count + 1, "V opens Git pane in isolated repository")
                git_pane = snapshot()["focused_pane_id"]
                eventually(lambda: any(p["name"] == "lazygit" for p in client.call("pane.process_info", pane_id=git_pane)["process_info"].get("foreground_processes", [])), "real Lazygit runs in owned pane")
                launched = next(p for p in snapshot()["panes"] if p["pane_id"] == git_pane)
                assert launched["tab_id"] == git_tab["tab"]["tab_id"] and launched["workspace_id"] == workspace
                assert client.call("pane.get", pane_id=git_pane)["pane"]["cwd"] == str(repository)
                client.call("tab.close", tab_id=git_tab["tab"]["tab_id"])
                checks.append("A dispatches the real agent entrypoint to a per-server safe Omarchy stub with expected arguments/cwd; V runs real Lazygit in an isolated repository and correct tab")

                agent_tabs, agent_panes = [], []
                for status in ("blocked", "done", "working", "idle"):
                    tab = client.call("tab.create", workspace_id=workspace, cwd=str(root), focus=False)
                    p = tab["root_pane"]
                    agent_tabs.append(tab["tab"]["tab_id"])
                    agent_panes.append(p["pane_id"])
                    if status == "done":
                        client.call("pane.report_agent", pane_id=p["pane_id"], source="herdr-shell-test", agent="codex", state="working", seq=1)
                    client.call("pane.report_agent", pane_id=p["pane_id"], source="herdr-shell-test", agent="codex",
                                state="idle" if status == "done" else status, seq=2)
                assert {p["pane_id"]: p["agent_status"] for p in snapshot()["agents"]} == dict(zip(agent_panes, ("blocked", "done", "working", "idle")))
                client.call("pane.focus", pane_id=first["root_pane"]["pane_id"])
                for expected in agent_panes:
                    herdr_key("q")
                    eventually(lambda: snapshot()["focused_pane_id"] == expected, "Q focuses next priority agent")
                herdr_key("q", shift=True)
                eventually(lambda: snapshot()["focused_pane_id"] == agent_panes[-2], "Shift+Q reverses the stable priority order")
                for tab_id in agent_tabs:
                    client.call("tab.close", tab_id=tab_id)
                checks.append("Q physically cycles needs-input, done, working and idle agents in stable order; Shift+Q reverses it after completion becomes seen")

                if args.launches_only:
                    assert desktop.enabled() == original_enabled
                    assert run("hyprctl", "configerrors").strip() == original_errors
                    result = {"herdr": snapshot()["version"], "bindings": len(active), "passed": checks}
                    (ROOT / "docs/desktop-launch-checks.json").write_text(json.dumps(result, indent=2) + "\n")
                    print(json.dumps(result, indent=2), flush=True)
                    return
                arrows = {"up": "Up", "down": "Down", "left": "Left", "right": "Right"}
                opposite = {"up": "down", "down": "up", "left": "right", "right": "left"}
                letters = {"up": "u", "down": "d", "left": "l", "right": "r"}
                for direction in ("up", "down", "left", "right"):
                    tab = client.call("tab.create", workspace_id=workspace, cwd=str(root), focus=True)
                    origin = tab["root_pane"]["pane_id"]
                    count = len(snapshot()["panes"])
                    herdr_key(letters[direction])
                    eventually(lambda: len(snapshot()["panes"]) == count + 1, "split " + direction)
                    created = snapshot()["focused_pane_id"]
                    assert created != origin
                    rects = positions()
                    axis = "x" if direction in ("left", "right") else "y"
                    assert (rects[created][axis] > rects[origin][axis]) == (direction in ("right", "down")), rects
                    key(arrows[opposite[direction]], "ctrl", "alt")
                    eventually(lambda: snapshot()["focused_pane_id"] == origin, "native arrow focuses original")
                    key(arrows[direction], "ctrl", "alt")
                    eventually(lambda: snapshot()["focused_pane_id"] == created, "native arrow focuses new pane")
                    before, before_ids = positions(), identities()
                    herdr_key(letters[opposite[direction]], shift=True)
                    eventually(lambda: positions() != before, "directional swap")
                    assert identities() == before_ids
                    assert positions()[created] == before[origin]
                checks.append("All four Super+Alt splits place panes correctly; native Ctrl+Alt arrows focus; all four Shift variants swap existing panes")

                fullscreen = window()["fullscreen"]
                herdr_key("z")
                eventually(lambda: layout()["zoomed"], "pane zoom")
                assert window()["fullscreen"] == fullscreen
                herdr_key("z")
                eventually(lambda: not layout()["zoomed"], "pane unzoom")
                key("f", "logo")
                eventually(lambda: window()["fullscreen"] != fullscreen, "Omarchy fullscreen retains desktop behavior")
                key("f", "logo")
                eventually(lambda: window()["fullscreen"] == fullscreen, "desktop fullscreen restores")
                checks.append("Pane zoom leaves desktop fullscreen untouched; Omarchy Super+F retains its desktop behavior")

                before_ids = identities()
                orientation = layout()["splits"][-1]["direction"]
                ratios = [s["ratio"] for s in layout()["splits"]]
                focused = snapshot()["focused_pane_id"]
                tabs = {t["tab_id"] for t in snapshot()["tabs"]}
                herdr_key("j")
                eventually(lambda: layout()["splits"][-1]["direction"] != orientation, "two-leaf rotation")
                assert identities() == before_ids
                assert [s["ratio"] for s in layout()["splits"]] == ratios
                assert snapshot()["focused_pane_id"] == focused
                assert {t["tab_id"] for t in snapshot()["tabs"]} == tabs
                current_tab = snapshot()["focused_tab_id"]
                ids = [p["pane_id"] for p in snapshot()["panes"] if p["tab_id"] == current_tab]
                expected = ids[(ids.index(focused) + 1) % len(ids)]
                herdr_key("p")
                eventually(lambda: snapshot()["focused_pane_id"] == expected, "next pane in tab")
                herdr_key("p", shift=True)
                eventually(lambda: snapshot()["focused_pane_id"] == focused, "previous pane in tab")
                checks.append("J rotates a sibling split preserving identities, ratio, focus and tabs; P/Shift+P stay in the current tab")

                old_tab, count = snapshot()["focused_tab_id"], len(snapshot()["tabs"])
                herdr_key("t")
                eventually(lambda: len(snapshot()["tabs"]) == count + 1, "new tab")
                new_tab = snapshot()["focused_tab_id"]
                herdr_key("Page_Up")
                eventually(lambda: snapshot()["focused_tab_id"] == old_tab, "previous tab")
                herdr_key("Page_Down")
                eventually(lambda: snapshot()["focused_tab_id"] == new_tab, "next tab")
                with patch.dict(os.environ, env, clear=True):
                    eventually(lambda: not close_plan("tab-close", current_context())["needs_confirmation"], "new tab shell settles")
                herdr_key("w")
                eventually(lambda: new_tab not in {t["tab_id"] for t in snapshot()["tabs"]}, "clean tab closes")
                count = len(snapshot()["workspaces"])
                herdr_key("t", shift=True)
                eventually(lambda: len(snapshot()["workspaces"]) == count + 1, "new workspace")
                new_workspace = snapshot()["focused_workspace_id"]
                ordered = [w["workspace_id"] for w in snapshot()["workspaces"]]
                previous_workspace = ordered[(ordered.index(new_workspace) - 1) % len(ordered)]
                herdr_key("Page_Up", shift=True)
                eventually(lambda: snapshot()["focused_workspace_id"] == previous_workspace, "previous workspace")
                herdr_key("Page_Down", shift=True)
                eventually(lambda: snapshot()["focused_workspace_id"] == new_workspace, "next workspace")
                with patch.dict(os.environ, env, clear=True):
                    eventually(lambda: not close_plan("workspace-close", current_context())["needs_confirmation"], "new workspace shell settles")
                herdr_key("w", shift=True)
                eventually(lambda: new_workspace not in {w["workspace_id"] for w in snapshot()["workspaces"]}, "clean workspace closes")
                assert window()["workspace"] == win["workspace"]
                checks.append("T/Shift+T create tabs/workspaces; PageUp/PageDown variants navigate; W variants close clean scopes without changing desktop workspaces")

                client.call("pane.focus", pane_id=first["root_pane"]["pane_id"])
                herdr_key("m")
                eventually(lambda: popup_page("menu"), "menu opens")
                herdr_key("m")
                eventually(lambda: not owned_menu_processes(), "menu toggles closed")
                herdr_key("m")
                first_menu = eventually(lambda: popup_page("menu"), "menu reopens")
                herdr_key("m", delay=0)
                herdr_key("m", delay=0)
                def reopened_menu():
                    processes = popup_page("menu")
                    return processes if processes and not set(processes) & set(first_menu) else None
                eventually(reopened_menu, "queued toggles close then reopen exactly one menu")
                assert len(owned_menu_processes()) == 1
                herdr_key("m")
                eventually(lambda: not owned_menu_processes(), "last menu toggle closes")
                checks.append("M toggles the owned menu; rapid close/open presses produce one popup without stacking")

                running = client.call("tab.create", workspace_id=workspace, cwd=str(root), focus=True)["root_pane"]
                client.call("pane.send_input", pane_id=running["pane_id"], text="sleep 60", keys=["enter"])
                eventually(lambda: any(p["name"] == "sleep" for p in client.call("pane.process_info", pane_id=running["pane_id"])["process_info"].get("foreground_processes", [])), "owned sleep starts")
                before_ids = identities()
                for name, shift, page in (("x", False, "confirm-pane-close"), ("w", False, "confirm-tab-close"), ("w", True, "confirm-workspace-close")):
                    herdr_key(name, shift=shift)
                    eventually(lambda: popup_page(page), "close confirmation " + page)
                    assert len(owned_menu_processes()) == 1
                    key("Escape")
                    eventually(lambda: not owned_menu_processes(), "Escape cancels " + page)
                    assert identities() == before_ids
                    assert any(p["name"] == "sleep" for p in client.call("pane.process_info", pane_id=running["pane_id"])["process_info"]["foreground_processes"])
                checks.append("X, W and Shift+W ask before closing running work; Escape preserves exact panes and running processes")

                assert desktop.enabled() == original_enabled
                assert run("hyprctl", "configerrors").strip() == original_errors
                result = {"herdr": snapshot()["version"], "bindings": len(active), "passed": checks}
                (ROOT / "docs/desktop-checks.json").write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result, indent=2), flush=True)
            except Exception:
                diagnostics()
                log.flush(); log.seek(0); print(log.read()[-7000:], flush=True)
                raise
            finally:
                try:
                    try:
                        if keyboard:
                            keyboard.close()
                    finally:
                        if terminal and terminal.poll() is None:
                            terminal.terminate(); terminal.wait(timeout=5)
                finally:
                    try:
                        if Path(client.path).exists():
                            try:
                                client.call("server.stop")
                            except (ShellError, OSError):
                                pass
                        try:
                            server.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            server.terminate(); server.wait(timeout=5)
                    finally:
                        if desktop.enabled() != original_enabled:
                            desktop.set_enabled(original_enabled)
                        move_cursor(original_cursor["x"], original_cursor["y"])
                        if original.get("address") and any(w.get("address") == original["address"] for w in json.loads(run("hyprctl", "-j", "clients"))):
                            focus_window(original["address"])


if __name__ == "__main__":
    main()
