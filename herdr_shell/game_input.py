"""Private, short-lived shortcut input for an active learning guide.

The desktop router only submits a chord. The guide's main thread remains the
sole owner of practice actions and curses. A wrong or repeated chord cannot
fall through to an ordinary Herdr action while its practice target is active.
"""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import stat
import struct
import tempfile
import threading
import time

from .config import atomic_write
from .runtime import Context, ShellError


_MAX_BYTES = 32768
_MAX_PANES = 256
_TIMEOUT = 0.5
_IDENTITY = ("pane_id", "terminal_id", "workspace_id", "tab_id")


def _process(pid):
    # Imported lazily: desktop.route imports this module at its route hook.
    from .desktop import read_process
    return read_process(pid)


def _canonical(value):
    return str(Path(value).expanduser().resolve())


def _marker_path(value):
    path = Path(_canonical(value))
    key = hashlib.sha256(str(path).encode()).hexdigest()[:24]
    return path.parent / ".herdr-shell-games" / (key + ".json")


def _private_directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ShellError("Learning input's directory is not private; it was preserved.")


def _read_marker(path):
    """Never follow a replaced marker or load unbounded/untrusted JSON."""
    try:
        _private_directory(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, ShellError):
        return None


@contextmanager
def _record_lock(path):
    _private_directory(path.parent)
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "a") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ShellError("Practice input's lock is not private; it was preserved.")
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _context(value):
    if not isinstance(value, dict):
        raise ValueError("invalid context")
    result = Context(**value)
    if any(not isinstance(v, str) or not v or len(v) > 8192 for v in asdict(result).values()):
        raise ValueError("invalid context fields")
    return result


def _identity(context):
    return dict(zip(_IDENTITY, (context.pane, context.terminal, context.workspace, context.tab)))


def _identities(rows):
    if not isinstance(rows, list) or len(rows) > _MAX_PANES:
        raise ValueError("invalid practice identities")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k] or
                                           len(row[k]) > 256 for k in _IDENTITY):
            raise ValueError("invalid practice pane identity")
        value = {k: row[k] for k in _IDENTITY}
        key = tuple(value[k] for k in _IDENTITY)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _owner(marker):
    """PID start time and the guide's entrypoint prevent stale PID reuse."""
    try:
        pid, start = marker["pid"], marker["start"]
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(start, str):
            return False
        process = _process(pid)
        return (process.start == start and process.foreground and process.state not in ("T", "t", "Z", "X", "x")
                and "_learn" in process.argv)
    except (OSError, ValueError, KeyError, IndexError):
        return False


def _in_guide(marker, guide):
    info = guide.client.call("pane.process_info", pane_id=guide.pane)
    if not isinstance(info, dict):
        return False
    info = info.get("process_info", info)
    if not isinstance(info, dict):
        return False
    processes = info.get("foreground_processes", [])
    return isinstance(processes, list) and any(isinstance(p, dict) and p.get("pid") == marker["pid"] for p in processes)


def _valid(marker, context):
    try:
        if marker.get("version") != 1 or not re.fullmatch(r"[a-f0-9]{48}", marker["nonce"]):
            return False
        guide = _context(marker["guide"])
        if marker["socket"] != _canonical(context.socket) or _canonical(guide.socket) != marker["socket"]:
            return False
        scope = _identities(marker["scope"])
        if _identity(guide) not in scope or _identity(context) not in scope or not _owner(marker):
            return False
        # A terminal replacement or guide removal expires the entire record.
        guide.validate()
        context.validate()
        return _in_guide(marker, guide)
    except (ShellError, OSError, ValueError, TypeError, KeyError):
        return False


def _endpoint(marker):
    value = marker.get("endpoint")
    if not isinstance(value, str):
        raise ValueError("invalid endpoint")
    path = Path(value)
    if not path.is_absolute() or path.name != "input.sock" or not path.parent.name.startswith("herdr-key-input-"):
        raise ValueError("invalid endpoint")
    _private_directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path, None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("invalid endpoint owner or type")
    return path, info


def _receive(connection):
    raw = bytearray()
    while len(raw) <= _MAX_BYTES:
        chunk = connection.recv(min(4096, _MAX_BYTES + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
        if b"\n" in raw:
            line, remainder = bytes(raw).split(b"\n", 1)
            if len(line) > _MAX_BYTES or remainder:
                raise ValueError("unexpected extra input")
            return json.loads(line)
    raise ValueError("incomplete or oversized game input")


def _desktop_identity(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"window_pid", "client_pid", "start", "address"}:
        raise ValueError("invalid desktop identity")
    if any(isinstance(value[k], bool) or not isinstance(value[k], int) or value[k] <= 0
           for k in ("window_pid", "client_pid")):
        raise ValueError("invalid desktop process identity")
    if any(not isinstance(value[k], str) or not value[k] or len(value[k]) > 128 for k in ("start", "address")):
        raise ValueError("invalid desktop window identity")
    return dict(value)


def _desktop_unchanged(identity, context):
    if identity is None:
        return True
    from .desktop import find_clients, hypr, socket_for
    current = json.loads(hypr("-j", "activewindow"))
    if not isinstance(current, dict) or current.get("pid") != identity["window_pid"] or current.get("address") != identity["address"]:
        return False
    clients = find_clients(identity["window_pid"])
    if len(clients) != 1:
        return False
    process, options = clients[0]
    return (process.pid == identity["client_pid"] and process.start == identity["start"]
            and _canonical(socket_for(process, options)) == _canonical(context.socket))


def route(action, context, *, desktop_identity=None):
    """Return None outside this guide's exact scope, otherwise consume the chord."""
    marker = _read_marker(_marker_path(context.socket))
    if marker is None or not _valid(marker, context):
        return None
    try:
        desktop_identity = _desktop_identity(desktop_identity)
        endpoint, info = _endpoint(marker)
    except (OSError, ValueError, ShellError):
        return {"game": "blocked", "reason": "Practice input ownership changed; no action was run."}
    if info is None:
        return {"game": "blocked", "reason": "Practice input is unavailable; return to the guide or exit it."}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_TIMEOUT)
            connection.connect(str(endpoint))
            pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid() or pid != marker["pid"] or not _owner(marker):
                return {"game": "blocked", "reason": "Practice input ownership changed; no action was run."}
            request = {"nonce": marker["nonce"], "action": action, "context": asdict(context),
                       "desktop_identity": desktop_identity}
            connection.sendall((json.dumps(request) + "\n").encode())
            response = _receive(connection)
        if not isinstance(response, dict) or response.get("nonce") != marker["nonce"]:
            raise ValueError("invalid game acknowledgement")
        if not response.get("handled"):
            return {"game": "blocked", "reason": "Practice targets changed; no action was run."}
        return {"game": response.get("state", "blocked"), "action": action,
                "reason": response.get("reason", "")}
    except (OSError, ValueError, TypeError, KeyError):
        # Once a live, owned guide is known, an IPC failure must not launch a
        # real agent or close its pane through ordinary action dispatch.
        return {"game": "blocked", "reason": "Practice input is busy or unavailable; no action was run."}


class Broker:
    """Fast bounded IPC acknowledgements; practice execution stays on the UI thread."""
    def __init__(self, guide, *, owned_identities=()):
        self.guide = guide
        self.pid = os.getpid()
        self.start = _process(self.pid).start
        self.nonce = secrets.token_hex(24)
        self.path = _marker_path(guide.socket)
        self.directory = None
        self.endpoint = None
        self.server = None
        self.thread = None
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.events = queue.Queue(maxsize=1)
        self.notices = queue.Queue(maxsize=16)
        self.wrong_events = queue.Queue(maxsize=256)
        self.wrong_count = 0
        self.last_received_at = None
        self.action = None
        self.busy = False
        try:
            self.scope = _identities([_identity(guide), *owned_identities])
        except (ValueError, TypeError) as exc:
            raise ShellError("Invalid initial practice pane identities.") from exc
        self.retained = {}
        self.record = None
        self.endpoint_identity = None

    def __enter__(self):
        self.guide.validate()
        self.scope = self._retained_scope(self.scope)
        _private_directory(self.path.parent, create=True)
        previous = _read_marker(self.path)
        if previous and _owner(previous):
            raise ShellError("A learning guide is already open in this Herdr session; close it first.")
        # A malformed or foreign file is never overwritten just to start a game.
        if (self.path.exists() or self.path.is_symlink()) and previous is None:
            raise ShellError("Cannot verify the existing practice input record; it was preserved.")
        try:
            self.directory = Path(tempfile.mkdtemp(prefix="herdr-key-input-"))
            self.endpoint = self.directory / "input.sock"
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(str(self.endpoint))
            self.endpoint_identity = self.endpoint.lstat()
            self.endpoint.chmod(0o600)
            self.server.listen(4)
            self.server.settimeout(0.2)
            self._publish()
            self.thread = threading.Thread(target=self._serve, name="herdr-key-input", daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *unused):
        self.close()

    def _publish(self):
        if self.endpoint is None:
            return
        record = {"version": 1, "nonce": self.nonce, "pid": self.pid, "start": self.start,
                  "socket": _canonical(self.guide.socket), "guide": asdict(self.guide),
                  "endpoint": str(self.endpoint), "scope": self.scope, "action": self.action}
        raw = json.dumps(record)
        if len(raw.encode()) > _MAX_BYTES:
            raise ShellError("Too many practice targets for the shortcut input record.")
        # Refuse an owner that appeared after initial setup or during an update.
        with _record_lock(self.path):
            current = _read_marker(self.path)
            exists = self.path.exists() or self.path.is_symlink()
            if exists and current is None:
                raise ShellError("Cannot verify the practice input record; the later file was preserved.")
            if current is not None and current.get("nonce") != self.nonce and (self.record is not None or _owner(current)):
                raise ShellError("Practice input ownership changed; the newer guide was preserved.")
            atomic_write(self.path, raw)
            self.record = record

    def _plan_scope(self, plan):
        rows = _identities(list(plan.pane_identities))
        for attribute, field in (("owned_panes", "pane_id"), ("owned_tabs", "tab_id"),
                                 ("owned_workspaces", "workspace_id")):
            allowed = getattr(plan, attribute, None)
            if allowed is not None and any(row[field] not in allowed for row in rows):
                raise ShellError("A practice target is outside the game's owned scope.")
        # A close mission may have just removed plan.target. It is not adopted
        # again unless its exact identity is still in the current inventory.
        return _identities([_identity(self.guide), *rows])

    def _retained_scope(self, authenticated):
        """Keep exact known terminals, never discover ownership from their labels."""
        snapshot = self.guide.client.snapshot()
        panes = snapshot.get("panes") if isinstance(snapshot, dict) else None
        if not isinstance(panes, list):
            raise ShellError("Cannot verify live practice pane identities.")
        needed = set(self.retained) | {row["pane_id"] for row in authenticated}
        live = {pane.get("pane_id"): {k: pane.get(k) for k in _IDENTITY}
                for pane in panes if isinstance(pane, dict) and pane.get("pane_id") in needed}
        if any(live.get(row["pane_id"]) != row for row in authenticated):
            raise ShellError("A practice terminal moved or was replaced; its new identity was preserved.")
        retained = {key: row for key, row in self.retained.items() if live.get(key) == row}
        for row in authenticated:
            retained[row["pane_id"]] = row
        scope = _identities(list(retained.values()))
        if len(json.dumps(scope).encode()) > _MAX_BYTES - 4096:
            raise ShellError("Too many open practice targets. Leave the game or close unused practice spaces before replaying.")
        self.retained = retained
        return scope

    def arm(self, plan):
        with self.lock:
            self.scope = self._retained_scope(self._plan_scope(plan))
            self.action = plan.action
            if not isinstance(self.action, str) or not self.action:
                raise ShellError("Invalid practice shortcut action.")
            self.busy = False
            while not self.events.empty():
                self.events.get_nowait()
            self._publish()

    def update(self, plan):
        with self.lock:
            self.scope = self._retained_scope(self._plan_scope(plan))
            self.action = plan.action
            self._publish()

    def disarm(self):
        with self.lock:
            self.action = None
            self.busy = False
            while not self.events.empty():
                self.events.get_nowait()
            self._publish()

    def complete(self):
        with self.lock:
            self.busy = False

    def next_event(self):
        try:
            action, context, desktop_identity, received_at = self.events.get_nowait()
        except queue.Empty:
            return None
        try:
            self.guide.validate()
            context.validate()
            if not self.record or not _owner(self.record) or not _in_guide(self.record, self.guide):
                raise ShellError("The practice guide process changed.")
            snapshot = self.guide.client.snapshot()
            if snapshot.get("focused_pane_id") != context.pane:
                raise ShellError("Focus changed before the practice action.")
            if not _desktop_unchanged(desktop_identity, context):
                raise ShellError("Desktop focus changed before the practice action.")
        except (ShellError, OSError, ValueError, KeyError, IndexError):
            self._notice("Practice focus or its terminal changed; no action was run.")
            self.complete()
            return None
        self.last_received_at = received_at
        return action, context

    def metrics(self):
        """Drain timestamped mistakes without resetting their cumulative count."""
        with self.lock:
            events = []
            while True:
                try:
                    events.append(self.wrong_events.get_nowait())
                except queue.Empty:
                    return {"wrong_count": self.wrong_count, "wrong_events": events}

    def feedback(self):
        result = []
        while True:
            try:
                result.append(self.notices.get_nowait())
            except queue.Empty:
                return result

    def _notice(self, message):
        try:
            self.notices.put_nowait(message)
        except queue.Full:
            pass

    def _accept(self, request):
        received_at = time.monotonic()
        try:
            if not isinstance(request, dict) or request.get("nonce") != self.nonce:
                return {"handled": False, "nonce": self.nonce}
            context = _context(request["context"])
            desktop_identity = _desktop_identity(request.get("desktop_identity"))
            if _canonical(context.socket) != _canonical(self.guide.socket):
                return {"handled": False, "nonce": self.nonce}
            with self.lock:
                if _identity(context) not in self.scope:
                    return {"handled": False, "nonce": self.nonce}
                action = request.get("action")
                if not isinstance(action, str) or not action or len(action) > 256:
                    raise ValueError("invalid shortcut action")
                if self.action is None:
                    self._notice("Start the next practice challenge before using its shortcut.")
                    state, reason = "blocked", "No challenge is armed."
                elif self.busy:
                    self._notice("Wait for this practice action to finish.")
                    state, reason = "busy", "The previous press is still being verified."
                elif action != self.action:
                    self._notice("Try the shortcut for this challenge. F1 gives a hint.")
                    self.wrong_count += 1
                    try:
                        self.wrong_events.put_nowait({"action": action, "received_at": received_at})
                    except queue.Full:
                        pass
                    state, reason = "wrong", "That shortcut does not match this challenge."
                else:
                    self.busy = True
                    self.events.put_nowait((action, context, desktop_identity, received_at))
                    state, reason = "queued", ""
                return {"handled": True, "nonce": self.nonce, "state": state, "reason": reason}
        except (ShellError, OSError, ValueError, TypeError, KeyError, queue.Full):
            return {"handled": True, "nonce": self.nonce, "state": "blocked",
                    "reason": "Practice targets changed; no action was run."}

    def _serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(_TIMEOUT)
                    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        continue
                    response = self._accept(_receive(connection))
                    connection.sendall((json.dumps(response) + "\n").encode())
                except (OSError, ValueError, TypeError):
                    continue

    def close(self):
        self.stop.set()
        if self.server is not None:
            self.server.close()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        try:
            with _record_lock(self.path):
                current = _read_marker(self.path)
                if current and self.record and all(current.get(key) == self.record.get(key)
                                                  for key in ("nonce", "pid", "start", "endpoint", "guide", "socket")):
                    self.path.unlink(missing_ok=True)
        except (OSError, ShellError):
            pass
        if self.endpoint is not None and self.endpoint_identity is not None:
            try:
                info = self.endpoint.lstat()
                if (info.st_dev, info.st_ino) == (self.endpoint_identity.st_dev, self.endpoint_identity.st_ino):
                    self.endpoint.unlink()
            except FileNotFoundError:
                pass
        if self.directory is not None:
            try:
                self.directory.rmdir()
            except OSError:
                pass
