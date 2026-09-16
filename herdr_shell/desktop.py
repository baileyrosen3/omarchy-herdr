"""Focused Omarchy controls, explicit process identity, and reversible setup."""
from dataclasses import dataclass
import difflib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .actions import execute
from .config import atomic_write
from .runtime import Client, Context, ShellError, state_path

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "-- BEGIN HERDR SHELL DESKTOP CONTROLS"
END = "-- END HERDR SHELL DESKTOP CONTROLS"
MAPPINGS = [
    ("SUPER + SPACE", "menu", "Herdr Shell menu"),
    ("SUPER + K", "keybindings", "Herdr keybindings"),
    ("SUPER + A", "agent-new", "New agent pane"),
    ("SUPER + G", "launch-git", "Lazygit in pane directory"),
    ("SUPER + U", "pane-split-up", "New pane above"),
    ("SUPER + D", "pane-split-down", "New pane below"),
    ("SUPER + L", "pane-split-left", "New pane left"),
    ("SUPER + R", "pane-split-right", "New pane right"),
    ("SUPER + M", "pane-zoom", "Zoom pane"),
    ("SUPER + X", "pane-close", "Close pane immediately"),
    ("SUPER + CTRL + LEFT", "tab-previous", "Previous tab"),
    ("SUPER + CTRL + RIGHT", "tab-next", "Next tab"),
    ("SUPER + code:20", "pane-resize-left", "Resize left"),
    ("SUPER + code:21", "pane-resize-right", "Resize right"),
    ("SUPER + SHIFT + code:20", "pane-resize-up", "Resize up"),
    ("SUPER + SHIFT + code:21", "pane-resize-down", "Resize down"),
]
for direction in ("left", "right", "up", "down"):
    target = "tab" if direction in ("left", "right") else "workspace"
    MAPPINGS += [("SUPER + " + direction.upper(), "navigate-" + direction, f"Navigate {direction} (pane / {target})"),
                 ("SUPER + SHIFT + " + direction.upper(), "pane-swap-" + direction, "Swap pane " + direction)]
for number in range(1, 11):
    MAPPINGS += [(f"SUPER + code:{number + 9}", f"workspace-number-{number}", f"Workspace {number}"),
                 (f"SUPER + SHIFT + code:{number + 9}", f"pane-workspace-{number}", f"Move pane to workspace {number}")]


def preferences_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "herdr-shell"


def enabled():
    try:
        return (preferences_dir() / "desktop-enabled").read_text().strip() == "1"
    except FileNotFoundError:
        return False


def set_enabled(value):
    if value and not installed():
        raise ShellError("Install the desktop bridge first: herdr-shell desktop install --apply")
    atomic_write(preferences_dir() / "desktop-enabled", "1\n" if value else "0\n")
    return {"desktop_controls": bool(value)}


def hypr_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hypr/hyprland.lua"


def installed():
    try:
        return BEGIN in hypr_path().read_text()
    except FileNotFoundError:
        return False


def pretty_key(key, prefix="ctrl+space"):
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super", "return": "Enter",
             "enter": "Enter", "space": "Space", "esc": "Esc", "minus": "−", "equal": "=", "plus": "+"}
    def chord(value):
        value = value.strip().lower()
        for code, label in [(20, "minus"), (21, "equal"), *[(n + 9, str(n % 10)) for n in range(1, 11)]]:
            value = value.replace("code:" + str(code), label)
        return "+".join(names.get(p.strip(), p.strip().capitalize()) for p in value.split("+"))
    return chord(prefix) + " → " + chord(key[7:]) if key.lower().startswith("prefix+") else chord(key)


def profile_rows():
    rows = [{"id": "desktop-enabled", "label": "Omarchy controls", "detail": "On" if enabled() else "Off",
             "scope": "Herdr focused", "kind": "desktop-toggle", "source": "desktop", "keys": []}]
    for key, action, label in MAPPINGS:
        rows.append({"id": "desktop:" + action, "label": label, "detail": pretty_key(key),
                     "keys": [key], "kind": "desktop-binding", "scope": "Herdr focused", "source": "desktop"})
    rows += [{"id": "desktop:alt-tab", "label": "Switch desktop windows", "detail": "Alt+Tab",
              "keys": ["alt+tab"], "kind": "desktop-binding", "scope": "Desktop", "source": "desktop"},
             {"id": "desktop:super-tab", "label": "Your desktop Super+Tab action", "detail": "Super+Tab",
              "keys": ["super+tab"], "kind": "desktop-binding", "scope": "Desktop", "source": "desktop"}]
    return rows


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    state: str
    foreground: bool
    start: str
    argv: tuple


def read_process(pid, proc_root=Path("/proc")):
    folder = proc_root / str(pid)
    raw = (folder / "stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    argv = tuple((folder / "cmdline").read_bytes().decode(errors="replace").rstrip("\0").split("\0"))
    return Process(pid, int(fields[1]), fields[0], fields[4] != "0" and fields[2] == fields[5], fields[19], argv)


def client_options(argv):
    if not argv or Path(argv[0]).name != "herdr":
        return None
    session, remote = "default", False
    args = list(argv[1:])
    while args:
        value = args.pop(0)
        if value == "--session" and args:
            session = args.pop(0)
        elif value.startswith("--session="):
            session = value.split("=", 1)[1]
        elif value == "--remote" and args:
            args.pop(0)
            remote = True
        elif value.startswith("--remote="):
            remote = True
        elif value == "--remote-keybindings" and args:
            args.pop(0)
        elif value.startswith("--remote-keybindings=") or value == "--handoff":
            pass
        elif value == "session" and len(args) == 2 and args[0] == "attach":
            session = args[1]
            args.clear()
        else:
            return None
    if session in (".", "..") or "/" in session or "\\" in session:
        raise ShellError("Invalid Herdr session name.")
    return {"session": session, "remote": remote}


def find_clients(window_pid, proc_root=Path("/proc")):
    pending, seen, clients = [window_pid], set(), []
    while pending:
        if len(seen) >= 256:
            raise ShellError("Terminal process tree is ambiguous; no Herdr action was run.")
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            process = read_process(pid, proc_root)
            options = client_options(process.argv)
            if options and process.foreground and process.state not in ("T", "t", "Z", "X"):
                clients.append((process, options))
                continue
            if process.state in ("T", "t", "Z", "X"):
                continue
            pending.extend(int(p) for p in (proc_root / str(pid) / "task" / str(pid) / "children").read_text().split())
        except FileNotFoundError:
            if pid == window_pid:
                raise ShellError("Focused terminal exited; no action was run.")
            continue
        except (OSError, ValueError, IndexError) as exc:
            raise ShellError("Cannot read the terminal process tree; no action was run.") from exc
    return clients


def socket_for(process, options, proc_root=Path("/proc")):
    if options["remote"]:
        raise ShellError("Direct SSH clients use their native Herdr shortcuts; desktop routing has no verified remote target.")
    env = {}
    allowed = {"XDG_CONFIG_HOME", "HERDR_SOCKET_PATH", "HERDR_SESSION"}
    for entry in (proc_root / str(process.pid) / "environ").read_bytes().split(b"\0"):
        key, _, value = entry.partition(b"=")
        if key.decode(errors="replace") in allowed:
            env[key.decode()] = value.decode()
    root = Path(env.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "herdr"
    session = options["session"]
    if session != "default":
        return str(root / "sessions" / session / "herdr.sock")
    return env.get("HERDR_SOCKET_PATH") or str(root / "herdr.sock")


def hypr(*argv):
    result = subprocess.run(["hyprctl", *argv], capture_output=True, text=True, timeout=4)
    if result.returncode:
        raise ShellError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def route(action, window_pid, client_pid, start, address):
    if action not in {a for _, a, _ in MAPPINGS} or not enabled():
        return {"skipped": "disabled or unknown action"}
    window = json.loads(hypr("-j", "activewindow"))
    if window.get("pid") != window_pid or window.get("address") != address:
        return {"skipped": "window focus changed"}
    clients = find_clients(window_pid)
    if len(clients) != 1:
        raise ShellError("Cannot identify a single foreground Herdr client; no action was run.")
    process, options = clients[0]
    if process.pid != client_pid or process.start != start:
        return {"skipped": "Herdr client changed"}
    client = Client(socket_for(process, options))
    snapshot = client.snapshot()
    pane = next((p for p in snapshot["panes"] if p["pane_id"] == snapshot.get("focused_pane_id")), None)
    if not pane:
        raise ShellError("Herdr has no active pane.")
    context = Context(client.path, pane["pane_id"], pane["workspace_id"], pane["tab_id"],
                      pane.get("foreground_cwd") or pane.get("cwd") or str(Path.home()), pane["terminal_id"])
    # Recheck desktop identity immediately before an operation. An API failure
    # never falls through to closing or moving the outer desktop window.
    current = json.loads(hypr("-j", "activewindow"))
    if current.get("pid") != window_pid or current.get("address") != address:
        return {"skipped": "window focus changed"}
    if action == "pane-close":
        return execute(action, context, yes=True)
    if action.startswith(("workspace-number-", "pane-workspace-")):
        number = int(action.rsplit("-", 1)[1])
        target = next((w for w in snapshot["workspaces"] if w.get("number") == number), None)
        if not target:
            raise ShellError(f"Herdr workspace {number} does not exist. Create it from the menu first.")
        if action.startswith("workspace-number-"):
            return client.call("workspace.focus", workspace_id=target["workspace_id"])
        if target["workspace_id"] == context.workspace:
            return {"changed": False}
        return client.call("pane.move", pane_id=context.pane, focus=True,
                           destination={"type": "tab", "tab_id": target["active_tab_id"], "split": "right"})
    return execute(action, context)


def integration_text():
    lua = (ROOT / "integrations/hyprland.lua").read_text()
    mapping = "\n".join("  [" + json.dumps(key) + "] = {" + json.dumps(action) + ", " + json.dumps(label) + "},"
                        for key, action, label in MAPPINGS)
    return lua.replace("-- GENERATED MAPPINGS", mapping)


def install_proposal(remove=False):
    path = hypr_path()
    if not path.exists():
        raise ShellError("This setup needs Omarchy's Lua Hyprland config at " + str(path))
    before = path.read_text()
    pattern = re.compile(re.escape(BEGIN) + r"\n.*?" + re.escape(END) + r"\n?", re.S)
    after = pattern.sub("", before)
    if not remove:
        anchor = 'require("default.hypr.omarchy")'
        if after.count(anchor) != 1:
            raise ShellError("Cannot identify Omarchy's default binding loader. No configuration was changed.")
        # Load before defaults and personal bindings, retaining each original
        # dispatcher as a fallback rather than copying package defaults.
        loader = preferences_dir() / "hyprland.lua"
        block = BEGIN + "\n" + 'dofile(' + json.dumps(str(loader)) + ')\n' + END + "\n"
        after = after.replace(anchor, block + anchor)
    return {"path": str(path), "before": before, "after": after,
            "diff": "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                               fromfile=str(path), tofile=str(path) + " (proposed)"))}


def install_desktop(proposal, remove=False):
    state_path().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_path() / "desktop.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _install_desktop(proposal, remove)


def _install_desktop(proposal, remove=False):
    path = Path(proposal["path"])
    if path.read_text() != proposal["before"]:
        raise ShellError("Hyprland config changed since the preview; review it again.")
    code = integration_text()
    check = subprocess.run(["luac", "-p", "-"], input=code, capture_output=True, text=True)
    if check.returncode:
        raise ShellError(check.stderr.strip())
    prefs = preferences_dir()
    history = state_path() / "desktop"
    history.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(history / (str(time.time_ns()) + ".lua"), proposal["before"])
    baseline = hypr("configerrors").strip()
    saved = {name: (prefs / name).read_text() if (prefs / name).exists() else None
             for name in ("hyprland.lua", "plugin-root", "desktop-enabled")}
    mode = path.stat().st_mode & 0o777
    try:
        atomic_write(prefs / "desktop-enabled", "0\n")
        if not remove:
            atomic_write(prefs / "hyprland.lua", code)
            atomic_write(prefs / "plugin-root", str(ROOT) + "\n")
        if path.read_text() != proposal["before"]:
            raise ShellError("Hyprland config changed during setup; review it again.")
        atomic_write(path, proposal["after"], mode)
        hypr("reload")
        errors = hypr("configerrors").strip()
        if errors and errors != baseline:
            raise ShellError(errors)
        if not remove:
            atomic_write(prefs / "desktop-enabled", "1\n")
    except Exception:
        for name, value in saved.items():
            if value is None:
                (prefs / name).unlink(missing_ok=True)
            else:
                atomic_write(prefs / name, value)
        if path.read_text() == proposal["after"]:
            atomic_write(path, proposal["before"], mode)
            hypr("reload")
        raise
    return {"installed": not remove, "enabled": not remove, "config": str(path)}
