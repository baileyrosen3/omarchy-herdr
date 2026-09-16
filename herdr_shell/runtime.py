"""Bounded socket requests and immutable invocation context."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import socket
import subprocess
import uuid


class ShellError(Exception):
    pass


def binary():
    return os.environ.get("HERDR_BIN_PATH", "herdr")


def config_path():
    return Path(os.environ.get("HERDR_CONFIG_PATH") or
                str(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) /
                    "herdr/config.toml")).expanduser().resolve()


def state_path():
    # One history for direct CLI and plugin invocations, including before link.
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "herdr-shell"


class Client:
    def __init__(self, path):
        if not path:
            raise ShellError("No Herdr session selected. Run inside Herdr or pass --socket or --session.")
        self.path = str(path)

    def call(self, method, **params):
        request_id = uuid.uuid4().hex
        request = {"id": request_id, "method": method, "params": params}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(5)
                conn.connect(self.path)
                conn.sendall((json.dumps(request) + "\n").encode())
                with conn.makefile("rb") as stream:
                    line = stream.readline(8 * 1024 * 1024 + 1)
                if len(line) > 8 * 1024 * 1024:
                    raise ShellError("Herdr response exceeded 8 MiB.")
                response = json.loads(line)
        except (OSError, ValueError) as exc:
            raise ShellError(f"Herdr at {self.path}: {exc}") from exc
        if response.get("id") != request_id:
            raise ShellError("Herdr returned an unexpected request ID.")
        if "error" in response:
            error = response["error"]
            raise ShellError(f"{error.get('code', 'error')}: {error.get('message', error)}")
        return response["result"]

    def snapshot(self):
        result = self.call("session.snapshot")
        return result.get("snapshot", result)


@dataclass(frozen=True)
class Context:
    socket: str
    pane: str
    workspace: str
    tab: str
    cwd: str
    terminal: str

    @property
    def client(self):
        return Client(self.socket)

    def encode(self):
        return json.dumps(asdict(self))

    def validate(self):
        result = self.client.call("pane.get", pane_id=self.pane)
        pane = result.get("pane", result)
        if pane.get("terminal_id") != self.terminal:
            raise ShellError("The originating terminal has changed. Reopen the menu.")
        if pane.get("workspace_id") != self.workspace or pane.get("tab_id") != self.tab:
            raise ShellError("The originating pane moved. Reopen the menu.")
        return pane


def resolve_socket(args=None):
    if args and getattr(args, "socket", None):
        return args.socket
    if args and getattr(args, "session", None):
        name = args.session
        if name in (".", "..") or "/" in name or "\\" in name:
            raise ShellError("Invalid session name.")
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "herdr"
        return str(root / "herdr.sock" if name == "default" else root / "sessions" / name / "herdr.sock")
    if os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    if os.environ.get("HERDR_ENV") == "1":
        name = os.environ.get("HERDR_SESSION", "default")
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "herdr"
        return str(root / "herdr.sock" if name == "default" else root / "sessions" / name / "herdr.sock")
    raise ShellError("Run inside Herdr or select a session with --session NAME.")


def resolve_context(args=None):
    explicit = bool(args and (getattr(args, "session", None) or getattr(args, "socket", None) or
                             getattr(args, "pane", None) or getattr(args, "active", False)))
    encoded = getattr(args, "context", None) if args else None
    encoded = encoded or (os.environ.get("HERDR_SHELL_CONTEXT") if not explicit else None)
    if encoded:
        try:
            context = Context(**json.loads(encoded))
        except (TypeError, ValueError) as exc:
            raise ShellError("Invalid saved Herdr context.") from exc
        context.validate()
        return context
    path = resolve_socket(args)
    explicit = bool(args and (getattr(args, "session", None) or getattr(args, "socket", None)))
    try:
        plugin = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}")) if not explicit else {}
    except ValueError as exc:
        raise ShellError("Invalid plugin invocation context.") from exc
    pane_id = (getattr(args, "pane", None) if args else None) or plugin.get("focused_pane_id")
    if not pane_id and not explicit:
        pane_id = os.environ.get("HERDR_PANE_ID")
    client = Client(path)
    if not pane_id:
        if not (args and getattr(args, "active", False)):
            raise ShellError("Choose --pane ID, or explicitly use --active for this session's focused pane.")
        pane_id = client.snapshot().get("focused_pane_id")
    if not pane_id:
        raise ShellError("This session has no focused pane.")
    result = client.call("pane.get", pane_id=pane_id)
    pane = result.get("pane", result)
    return Context(path, pane["pane_id"], pane["workspace_id"], pane["tab_id"],
                   pane.get("foreground_cwd") or pane.get("cwd") or plugin.get("focused_pane_cwd") or
                   plugin.get("workspace_cwd") or str(Path.home()), pane["terminal_id"])


def run_herdr(argv, *, env=None):
    try:
        result = subprocess.run([binary(), *argv], capture_output=True, text=True,
                                timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShellError(str(exc)) from exc
    if result.returncode:
        raise ShellError((result.stderr or result.stdout).strip() or f"Herdr exited {result.returncode}.")
    return result.stdout
