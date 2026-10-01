"""Focused Omarchy controls, explicit process identity, and reversible setup."""
from dataclasses import dataclass
from contextlib import contextmanager
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .actions import execute, open_ui
from .config import atomic_write
from .keymap import chord, collisions, key_name, source_positions
from .runtime import Client, Context, ShellError, state_path

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "-- BEGIN HERDR SHELL DESKTOP CONTROLS"
END = "-- END HERDR SHELL DESKTOP CONTROLS"
MAPPINGS = [
    ("SUPER + ALT + M", "menu", "Toggle Herdr Shell menu"),
    ("SUPER + ALT + T", "tab-new", "New tab"),
    ("SUPER + ALT + W", "tab-close", "Close tab"),
    ("SUPER + ALT + SHIFT + T", "workspace-new", "New workspace"),
    ("SUPER + ALT + SHIFT + W", "workspace-close", "Close workspace"),
    ("SUPER + ALT + A", "agent-new", "New agent pane"),
    ("SUPER + ALT + Q", "agent-cycle-next", "Next agent by priority"),
    ("SUPER + ALT + SHIFT + Q", "agent-cycle-previous", "Previous agent by priority"),
    ("SUPER + ALT + V", "launch-git", "Lazygit in pane directory"),
    ("SUPER + ALT + Z", "pane-zoom", "Zoom pane"),
    ("SUPER + ALT + X", "pane-close", "Close pane"),
    ("SUPER + ALT + J", "pane-rotate", "Rotate nearest two-pane split"),
    ("SUPER + ALT + P", "pane-cycle-next", "Next pane in this tab"),
    ("SUPER + ALT + SHIFT + P", "pane-cycle-previous", "Previous pane in this tab"),
    ("SUPER + ALT + Page_Up", "tab-previous", "Previous tab"),
    ("SUPER + ALT + Page_Down", "tab-next", "Next tab"),
    ("SUPER + ALT + SHIFT + Page_Up", "workspace-previous", "Previous workspace"),
    ("SUPER + ALT + SHIFT + Page_Down", "workspace-next", "Next workspace"),
]
for direction, key in (("up", "U"), ("down", "D"), ("left", "L"), ("right", "R")):
    MAPPINGS += [("SUPER + ALT + " + key, "pane-split-" + direction, "New pane " + direction),
                 ("SUPER + ALT + SHIFT + " + key, "pane-swap-" + direction, "Swap pane " + direction)]
_profile_cache = {}


def preferences_dir():
    return _desktop_config_home() / "herdr-shell"


def _desktop_config_home():
    return Path(os.environ.get("HERDR_SHELL_DESKTOP_CONFIG_HOME") or
                os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def popup_environment():
    """Keep compositor preferences separate from the target Herdr config."""
    try:
        root = hypr("repl", 'return herdr_shell_bridge and (os.getenv("XDG_CONFIG_HOME") or os.getenv("HOME") .. "/.config")').strip()
        if not Path(root).is_absolute() or any(ord(char) < 32 or ord(char) == 127 for char in root):
            return {}
        return {"HERDR_SHELL_DESKTOP_CONFIG_HOME": root}
    except (ShellError, OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def enabled():
    try:
        return (preferences_dir() / "desktop-enabled").read_text().strip() == "1"
    except FileNotFoundError:
        return False


def set_enabled(value):
    if value and not installed():
        raise ShellError("Install the desktop bridge first: herdr-shell desktop install --apply")
    if value:
        registration = reconcile()
        if "skipped" in registration or not registration.get("generation"):
            raise ShellError("The desktop configuration changed; refresh integration before enabling shortcuts.")
        if not registration["registered"]:
            raise ShellError("All Herdr shortcuts are reserved by desktop bindings; the profile remains off.")
    atomic_write(preferences_dir() / "desktop-enabled", "1\n" if value else "0\n")
    _profile_cache.clear()
    return {"desktop_controls": bool(value)}


def hypr_path():
    return _desktop_config_home() / "hypr/hyprland.lua"


def installed():
    try:
        return BEGIN in hypr_path().read_text()
    except FileNotFoundError:
        return False


def pretty_key(key, prefix="ctrl+space"):
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super", "return": "Enter",
             "enter": "Enter", "space": "Space", "esc": "Esc", "minus": "−", "equal": "=", "plus": "+", "comma": ",",
             "page_up": "PageUp", "page_down": "PageDown"}
    def chord(value):
        value = value.strip().lower()
        for code, label in [(20, "minus"), (21, "equal"), *[(n + 9, str(n % 10)) for n in range(1, 11)]]:
            value = value.replace("code:" + str(code), label)
        return "+".join(names.get(p.strip(), p.strip().capitalize()) for p in value.split("+"))
    return chord(prefix) + " → " + chord(key[7:]) if key.lower().startswith("prefix+") else chord(key)


def profile_rows():
    effective = effective_bindings()
    rows = [{"id": "desktop-enabled", "label": "Super+Alt controls", "detail": "On" if enabled() else "Off",
             "scope": "Herdr focused", "kind": "desktop-toggle", "source": "desktop", "keys": []}]
    for key, action, label in MAPPINGS:
        rows.append({"id": "desktop:" + action, "label": label, "detail": pretty_key(key),
                     "action_id": action, "keys": [key], "kind": "desktop-binding", "scope": "Herdr focused",
                     "source": "desktop", **effective[action]})
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
            if not session:
                return None
        elif value.startswith("--session="):
            session = value.split("=", 1)[1]
            if not session:
                return None
        elif value == "--remote" and args:
            if not args.pop(0):
                return None
            remote = True
        elif value.startswith("--remote="):
            if not value.split("=", 1)[1]:
                return None
            remote = True
        elif value == "--remote-keybindings" and args:
            if not args.pop(0):
                return None
        elif value.startswith("--remote-keybindings="):
            if not value.split("=", 1)[1]:
                return None
        elif value == "--handoff":
            pass
        elif value == "session" and len(args) == 2 and args[0] == "attach":
            session = args[1]
            if not session:
                return None
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


def binding_snapshot():
    binds = json.loads(hypr("-j", "binds"))
    if not isinstance(binds, list):
        raise ShellError("Hyprland did not return its registered bindings.")
    default = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")) / "default/hypr"
    files = list(default.rglob("*.lua")) + list(hypr_path().parent.glob("*.lua"))
    positions = source_positions(files)
    def option(name):
        return json.loads(hypr("-j", "getoption", "input:" + name)).get("str", "")
    layout, variant = option("kb_layout") or "us", option("kb_variant")
    return binds, collisions(MAPPINGS, binds, positions=positions, layout=layout, variant=variant)


def effective_bindings(refresh=False):
    stamp = (str(preferences_dir()), installed(), enabled())
    if not refresh and _profile_cache.get("stamp") == stamp and time.monotonic() - _profile_cache.get("time", 0) < 1:
        return _profile_cache["rows"]
    rows = {}
    try:
        binds, occupied = binding_snapshot() if stamp[1] else ([], {})
        for keys, action, _ in MAPPINGS:
            mask, name = chord(keys)
            own = [b for b in binds if b.get("description") == "Herdr Shell: " + action and
                   b.get("modmask") == mask and key_name(b.get("key", "")) == name and not b.get("submap")]
            if action in occupied:
                status, reason = "conflict", "Reserved by " + occupied[action]
            elif len(own) > 1:
                status, reason = "conflict", "Duplicate Herdr shortcut registration"
            elif not stamp[1]:
                status, reason = "unavailable", "Install the Super+Alt bridge to activate this shortcut."
            elif not stamp[2]:
                status, reason = "disabled", "Super+Alt controls are off."
            elif not own:
                status, reason = "unavailable", "This shortcut is not registered; refresh desktop integration."
            else:
                status, reason = "ready", "Active in the focused local Herdr terminal."
            rows[action] = {"status": status, "reason": reason, "active": status == "ready"}
    except (ShellError, OSError, ValueError, KeyError) as exc:
        rows = {action: {"status": "unavailable", "reason": "Cannot inspect active shortcuts: " + str(exc),
                         "active": False} for _, action, _ in MAPPINGS}
    _profile_cache.update(stamp=stamp, time=time.monotonic(), rows=rows)
    return rows


def generation():
    value = hypr("repl", "return herdr_shell_bridge and herdr_shell_bridge.generation").strip()
    return value if re.fullmatch(r"[a-zA-Z0-9]{1,96}", value) and value != "nil" else None


def reconcile(expected=None):
    """Register only free chords after all Omarchy and personal config has loaded."""
    prefs = preferences_dir()
    prefs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (prefs / "registration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = generation()
        if not current or expected and expected != current:
            return {"skipped": "configuration reloaded"}
        _, occupied = binding_snapshot()
        allowed = [a for _, a, _ in MAPPINGS if a not in occupied]
        # The generation guard rejects a reconciliation raced by another reload.
        flags = "{" + ",".join("[" + json.dumps(a) + "]=true" for a in allowed) + "}"
        hypr("eval", "if herdr_shell_bridge and herdr_shell_bridge.generation == " + json.dumps(current) +
             " then herdr_shell_bridge.register(" + flags + ") end")
        atomic_write(prefs / "binding-status.json", json.dumps({"generation": current, "conflicts": occupied}))
        _profile_cache.clear()
        return {"registered": allowed, "conflicts": occupied, "generation": current}


def _menu_path(socket):
    # Popup processes inherit the target session's XDG environment, which may
    # differ from the compositor worker's. The socket gives both a shared scope.
    socket = Path(socket).resolve()
    return socket.parent / ".herdr-shell-menus" / (hashlib.sha256(str(socket).encode()).hexdigest()[:24] + ".json")


def menu_running(socket):
    try:
        marker = json.loads(_menu_path(socket).read_text())
        process = read_process(marker["pid"])
        if process.start == marker["start"] and "_menu" in process.argv and process.state not in ("T", "t", "Z", "X"):
            return marker
    except (OSError, ValueError, KeyError, IndexError):
        pass
    return None


@contextmanager
def menu_instance(context):
    path = _menu_path(context.socket)
    process = read_process(os.getpid())
    marker = {"pid": process.pid, "start": process.start, "pane": context.pane, "terminal": context.terminal}
    atomic_write(path, json.dumps(marker))
    try:
        yield
    finally:
        try:
            if json.loads(path.read_text()) == marker:
                path.unlink()
        except (FileNotFoundError, ValueError):
            pass


def toggle_menu(context):
    if menu_running(context.socket):
        return context.client.call("popup.close")
    result = open_ui(context, "menu")
    # Wait for popup ownership to be recorded before the next queued press.
    for _ in range(50):
        if menu_running(context.socket):
            return result
        time.sleep(.02)
    raise ShellError("The menu did not start. Check Herdr Shell logs before retrying.")


def record_error(action, exc):
    state_path().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_path() / "actions.log").open("a") as log:
        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} desktop {action}: {exc}\n")
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "Herdr Shell", str(exc)], timeout=3, check=False)


def drain(expected):
    if not re.fullmatch(r"[a-zA-Z0-9]{1,96}", expected):
        raise ShellError("Invalid shortcut queue identity.")
    prefs = preferences_dir()
    with (prefs / ("events-" + expected + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if generation() != expected:
            return {"skipped": "configuration reloaded"}
        path = prefs / ("events-" + expected + ".queue")
        offset_path = prefs / ("events-" + expected + ".offset")
        try:
            offset = int(offset_path.read_text())
        except (FileNotFoundError, ValueError):
            offset = 0
        count = 0
        with path.open("rb") as events:
            events.seek(offset)
            while line := events.readline():
                if not line.endswith(b"\n"):
                    break
                if generation() != expected:
                    break
                # Consume before dispatch: an interrupted close/toggle is never replayed.
                atomic_write(offset_path, str(events.tell()))
                action = "unknown"
                try:
                    sequence, action, window, client, start, address = line.decode().strip().split("\t")
                    if int(sequence) < 1:
                        raise ValueError("Invalid event order")
                    route(action, int(window), int(client), start, address)
                    count += 1
                except (ShellError, OSError, ValueError, KeyError) as exc:
                    record_error(action, exc)
        return {"processed": count}


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
    from .game_input import route as game_route
    practice = game_route(action, context, desktop_identity={
        "window_pid": window_pid, "client_pid": client_pid, "start": start, "address": address})
    if practice is not None:
        return practice
    if action == "menu":
        return toggle_menu(context)
    if menu_running(context.socket):
        return {"skipped": "close the menu or confirmation before using action shortcuts"}
    return execute(action, context)


def integration_text():
    lua = (ROOT / "integrations/hyprland.lua").read_text()
    mapping = "\n".join("  { keys=" + json.dumps(key) + ", action=" + json.dumps(action) + ", label=" + json.dumps(label) + " },"
                        for key, action, label in MAPPINGS)
    return lua.replace("-- GENERATED MAPPINGS", mapping)


def install_proposal(remove=False):
    path = hypr_path()
    if not path.exists():
        raise ShellError("This setup needs Omarchy's Lua Hyprland config at " + str(path))
    before = path.read_text()
    pattern = re.compile(r"\n" + re.escape(BEGIN) + r" \(added separator\)\n.*?" + re.escape(END) +
                         r"\n?|" + re.escape(BEGIN) + r"\n.*?" + re.escape(END) + r"\n?", re.S)
    after = pattern.sub("", before)
    if not remove:
        anchor = 'require("default.hypr.omarchy")'
        if after.count(anchor) != 1:
            raise ShellError("Cannot identify Omarchy's default binding loader. No configuration was changed.")
        # Declare the bridge last; asynchronous reconciliation sees all bindings.
        loader = preferences_dir() / "hyprland.lua"
        separator = bool(after and not after.endswith("\n"))
        block = BEGIN + (" (added separator)" if separator else "") + "\n" + 'dofile(' + json.dumps(str(loader)) + ')\n' + END + "\n"
        after += ("\n" if separator else "") + block
    return {"path": str(path), "before": before, "after": after,
            "diff": "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                               fromfile=str(path), tofile=str(path) + " (proposed)"))}


def install_desktop(proposal, remove=False, *, activate=True):
    state_path().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_path() / "desktop.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _install_desktop(proposal, remove, activate=activate)


def _install_desktop(proposal, remove=False, *, activate=True):
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
             for name in ("hyprland.lua", "plugin-root", "desktop-enabled", "binding-status.json")}
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
        registration = {}
        if not remove:
            registration = reconcile()
            if "skipped" in registration or not registration.get("generation"):
                raise ShellError("The desktop bridge was not registered after reload. No shortcut profile was enabled.")
            atomic_write(prefs / "desktop-enabled", "1\n" if activate and registration["registered"] else "0\n")
            actual = effective_bindings(refresh=True)
            if activate:
                missing = [a for a in registration["registered"] if not actual[a]["active"]]
            else:
                # An off preference does not prove that its chords registered.
                # Inspect ownership directly without briefly enabling the profile.
                binds, occupied = binding_snapshot()
                mapping = {a: keys for keys, a, _ in MAPPINGS}
                missing = []
                for action in registration["registered"]:
                    mask, name = chord(mapping[action])
                    own = [b for b in binds if b.get("description") == "Herdr Shell: " + action and
                           b.get("modmask") == mask and key_name(b.get("key", "")) == name and not b.get("submap")]
                    if action in occupied or len(own) != 1:
                        missing.append(action)
            if missing:
                raise ShellError("Cannot verify new shortcut registration: " + ", ".join(missing))
        _profile_cache.clear()
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
    return {"installed": not remove, "enabled": enabled(), "config": str(path),
            "conflicts": registration.get("conflicts", {})}
