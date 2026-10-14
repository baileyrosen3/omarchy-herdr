"""Live tab names: one daemon per Herdr server follows each owned tab's agent task.

A tab belongs to the namer while its label is Herdr's default (its live
position) or the label the namer last set. Any other label (a manual rename,
herdr-board's card tabs, another tool's name) pins the tab until its label
returns to the default, which the "Name tab from agent task" action does.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from . import PLUGIN_ID, naming
from .config import atomic_write
from .runtime import Client, ShellError, state_path


ROOT = Path(__file__).resolve().parents[1]
SETTLE_SECONDS = 2.0       # a new title must hold this long before a rename
MIN_RENAME_SECONDS = 10.0  # per tab, so the tab bar never flickers
FULL_PASS_SECONDS = 30.0   # safety net for missed events and lagging tokens
LOG_LIMIT = 256 * 1024
MARKER_TTL_MS = 86_400_000  # Herdr's maximum
SUBSCRIPTIONS = ("tab.created", "tab.renamed", "tab.closed", "tab.moved", "pane.created",
                 "pane.closed", "pane.moved", "pane.updated", "pane.agent_detected")
STRUCTURAL = {"tab_created", "tab_renamed", "tab_closed", "tab_moved", "pane_created",
              "pane_closed", "pane_moved", "pane_agent_detected"}


def plan(snapshot, ledger, mode):
    """Decide ownership and desired labels for every tab, without side effects.

    Returns (entries, renames): the ledger entries for the current tabs and the
    {tab_id: label} renames that would bring owned tabs up to date.
    """
    positions = naming.tab_positions(snapshot)
    panes_by_tab = {}
    for pane in snapshot.get("panes", []):
        panes_by_tab.setdefault(pane.get("tab_id"), []).append(pane)
    entries, renames = {}, {}
    for tab in snapshot.get("tabs", []):
        tab_id = tab["tab_id"]
        label = tab.get("label") if isinstance(tab.get("label"), str) else ""
        default = positions[tab_id]
        entry = ledger.get(tab_id) or {}
        panes = panes_by_tab.get(tab_id, [])
        if label == default:
            topic_label = False
        elif entry.get("pinned") and label == entry.get("label"):
            entries[tab_id] = {"label": label, "pinned": True}
            continue
        elif label in (entry.get("label"), entry.get("previous")) and not entry.get("pinned"):
            topic_label = bool(entry.get("topic")) and label == entry.get("label")
        else:
            entries[tab_id] = {"label": label, "pinned": True}
            continue
        entries[tab_id] = {"label": label, "topic": topic_label}
        topic = next(filter(None, map(naming.agent_topic, panes)), None)
        if mode == "task" and topic:
            desired = naming.short_label(topic)
        elif mode != "task" and label != default:
            desired = default
        else:
            # No task yet, or the agent left: keep the current name.
            continue
        if desired != label:
            renames[tab_id] = desired
    return entries, renames


def marker_changes(snapshot, entries):
    """{pane_id: wanted} for agent panes whose topic marker must change."""
    changes = {}
    for pane in snapshot.get("panes", []):
        entry = entries.get(pane.get("tab_id")) or {}
        wanted = bool(pane.get("agent")) and bool(entry.get("topic")) and not entry.get("pinned")
        tokens = pane.get("tokens") if isinstance(pane.get("tokens"), dict) else {}
        if wanted or tokens.get(naming.MARKER_TOKEN) == naming.MARKER_VALUE:
            changes[pane["pane_id"]] = wanted
    return changes


class Ledger:
    """Labels the namer set, per tab; only the daemon holding the lock writes it."""

    def __init__(self, path):
        self.path = path
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            data = {}
        tabs = data.get("tabs") if isinstance(data, dict) else None
        self.tabs = {tab_id: entry for tab_id, entry in (tabs or {}).items()
                     if isinstance(entry, dict) and isinstance(entry.get("label"), str)}

    def save(self):
        atomic_write(self.path, json.dumps({"version": 1, "tabs": self.tabs}, indent=1, sort_keys=True) + "\n")


class Namer:
    def __init__(self, client, ledger, *, clock=time.monotonic, log=None):
        self.client = client
        self.ledger = ledger
        self.clock = clock
        self.log = log or (lambda message: None)
        self.lock = threading.Lock()
        self.fingerprints = {}   # pane_id -> (tab_id, topic)
        self.changed = {}        # tab_id -> when its task last changed (or retry time base)
        self.renamed = {}        # tab_id -> when the namer last renamed it
        self.full_due = float("-inf")
        self.preferences_stamp = None
        self.retry_due = float("-inf")

    def request_full(self, delay=0.0, *, retry=False):
        with self.lock:
            now = self.clock()
            self.full_due = min(self.full_due, now + delay) if self.full_due > now else now + delay
            if retry:
                self.retry_due = now + delay

    def observe(self, message):
        """Classify one event. Called for every spinner frame, so it stays cheap:
        only a changed task title or a structural change schedules work."""
        kind = message.get("event") if isinstance(message, dict) else None
        data = message.get("data") if isinstance(message, dict) and isinstance(message.get("data"), dict) else {}
        if kind == "pane_updated":
            pane = data.get("pane") if isinstance(data.get("pane"), dict) else {}
            pane_id = pane.get("pane_id")
            if not pane_id:
                return
            fingerprint = (pane.get("tab_id"), naming.agent_topic(pane))
            now = self.clock()
            with self.lock:
                previous = self.fingerprints.get(pane_id)
                if previous == fingerprint:
                    return
                self.fingerprints[pane_id] = fingerprint
                for tab_id in {fingerprint[0], previous[0] if previous else None} - {None}:
                    self.changed[tab_id] = now
        elif kind in STRUCTURAL:
            if kind == "pane_closed" and data.get("pane_id"):
                with self.lock:
                    self.fingerprints.pop(data["pane_id"], None)
            self.request_full(0.3)

    def step(self):
        """Apply settled task changes, or every tab on a full pass. Returns True
        when a pass ran."""
        now = self.clock()
        stamp = _preferences_stamp()
        with self.lock:
            if now < self.retry_due:
                return False
            settled = {tab for tab, at in self.changed.items() if now - at >= SETTLE_SECONDS}
            settling = set(self.changed) - settled
            full = now >= self.full_due or stamp != self.preferences_stamp
        if not settled and not full:
            return False
        snapshot = self.client.snapshot()
        mode = naming.preferences()["tab_mode"]
        entries, renames = plan(snapshot, self.ledger.tabs, mode)
        applied, retry = {}, {}
        for tab_id, label in renames.items():
            if tab_id in settling or (not full and tab_id not in settled):
                continue
            wait = MIN_RENAME_SECONDS - (now - self.renamed.get(tab_id, float("-inf")))
            if wait > 0:
                retry[tab_id] = now - SETTLE_SECONDS + wait
                continue
            applied[tab_id] = label
        before = {tab_id: entries[tab_id] for tab_id in applied}
        for tab_id, label in applied.items():
            entries[tab_id] = {"label": label, "previous": before[tab_id]["label"], "topic": mode == "task"}
        markers = marker_changes(snapshot, entries)
        # Mark agent panes before a task label appears and unmark only after it is
        # gone, so dashboards that hide prompt-derived names never see one unmarked.
        blocked = set()
        for pane_id in [pane for pane, wanted in markers.items() if wanted]:
            if not self._mark(pane_id, True):
                blocked.update(pane["tab_id"] for pane in snapshot.get("panes", [])
                               if pane["pane_id"] == pane_id)
        for tab_id in blocked:
            if tab_id in applied:
                applied.pop(tab_id)
                entries[tab_id] = before[tab_id]
        # Save first: the namer's own rename must already match the ledger, even if
        # this process stops between the two steps.
        self.ledger.tabs = entries
        self.ledger.save()
        failed = False
        for tab_id, label in applied.items():
            try:
                current = self.client.snapshot()
                tab = next((tab for tab in current.get("tabs", [])
                            if tab["tab_id"] == tab_id), None)
                if tab is None or tab.get("label") != before[tab_id]["label"]:
                    entries[tab_id] = {"label": tab.get("label", "") if tab else "",
                                       "pinned": True}
                    failed = True
                    continue
                self.client.call("tab.rename", tab_id=tab_id, label=label)
            except ShellError as exc:
                self.log(f"rename {tab_id} failed: {exc}")
                entries[tab_id] = before[tab_id]
                failed = True
                continue
            self.renamed[tab_id] = now
            self.log(f"tab {tab_id} → {label}")
        if failed:
            self.ledger.save()
        for pane_id in [pane for pane, wanted in markers.items() if not wanted]:
            self._mark(pane_id, False)
        with self.lock:
            for tab_id in settled:
                if self.changed.get(tab_id, now) <= now:
                    self.changed.pop(tab_id, None)
            self.changed.update(retry)
            if full:
                self.full_due = now + FULL_PASS_SECONDS
                self.preferences_stamp = stamp
        return True

    def _mark(self, pane_id, on):
        try:
            # Markers describe a label, so they outlive restarts; a full pass
            # re-asserts any that expire.
            self.client.call("pane.report_metadata", pane_id=pane_id, source=PLUGIN_ID, ttl_ms=MARKER_TTL_MS,
                             tokens={naming.MARKER_TOKEN: naming.MARKER_VALUE if on else None})
            return True
        except ShellError as exc:
            self.log(f"marker {pane_id} failed: {exc}")
            return False


def _preferences_stamp():
    try:
        stat = naming.preferences_path().stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class EventStream:
    """One subscription connection; a reader thread feeds events to observe()."""

    def __init__(self, path, observe):
        self.path = path
        self.observe = observe
        self.connection = None
        self.thread = None

    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(5)
            connection.connect(self.path)
            request = {"id": "herdr-shell-namer", "method": "events.subscribe",
                       "params": {"subscriptions": [{"type": kind} for kind in SUBSCRIPTIONS]}}
            connection.sendall((json.dumps(request) + "\n").encode())
            reader = connection.makefile("rb")
            reply = json.loads(reader.readline(65536) or b"{}")
            if "error" in reply or reply.get("id") != request["id"]:
                raise ShellError(f"Herdr refused the event subscription: {reply.get('error', reply)}")
            connection.settimeout(None)
        except (OSError, ValueError) as exc:
            connection.close()
            raise ShellError(f"Herdr events at {self.path}: {exc}") from exc
        self.connection = connection
        self.thread = threading.Thread(target=self._pump, args=(reader,), daemon=True)
        self.thread.start()

    def _pump(self, reader):
        try:
            for line in reader:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self.observe(message)
        except OSError:
            pass

    def close(self):
        if self.connection:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()


def paths(socket_path):
    key = hashlib.sha256(os.path.realpath(str(socket_path)).encode()).hexdigest()[:16]
    folder = state_path() / "namer"
    return folder / f"{key}.lock", folder / f"{key}.json", folder / f"{key}.log"


def _locked(lock_path):
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def running(socket_path):
    return _locked(paths(socket_path)[0])


def _command(socket_path):
    return [sys.executable, str(ROOT / "bin/herdr-shell"), "--socket", str(socket_path), "_namer", "run"]


def ensure(socket_path):
    """Start the server's namer unless one is already running. Cheap enough for
    plugin event hooks."""
    if running(socket_path):
        return {"namer": "running"}
    lock_path, _, log_path = paths(socket_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if log_path.stat().st_size > LOG_LIMIT:
            log_path.write_text("")
    except FileNotFoundError:
        pass
    env = {key: value for key, value in os.environ.items() if not key.startswith("HERDR_PLUGIN_")}
    with log_path.open("a") as log:
        process = subprocess.Popen(_command(socket_path), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=env, start_new_session=True, close_fds=True)
    return {"namer": "started", "pid": process.pid}


def stop(socket_path=None):
    """Stop one server's namer, or every namer of this user."""
    folder = state_path() / "namer"
    locks = [paths(socket_path)[0]] if socket_path else sorted(folder.glob("*.lock"))
    stopped = []
    for lock_path in locks:
        if not _locked(lock_path):
            continue
        try:
            pid = int(lock_path.read_text().split()[0])
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, ValueError, IndexError):
            continue
        if b"_namer" in command and b"herdr-shell" in command:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
    return {"stopped": stopped}


def _log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    line = line.encode()[:LOG_LIMIT].decode(errors="ignore")
    try:
        fd = sys.stdout.fileno()
        if os.fstat(fd).st_size + len(line.encode()) > LOG_LIMIT:
            sys.stdout.flush()
            os.ftruncate(fd, 0)
            sys.stdout.seek(0)
    except (OSError, ValueError):
        pass
    sys.stdout.write(line)
    sys.stdout.flush()


def _code_signature():
    files = sorted((ROOT / "herdr_shell").glob("*.py")) + [ROOT / "bin/herdr-shell"]
    signature = []
    for path in files:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        signature.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def _retire_reason(client):
    plugins = client.call("plugin.list").get("plugins", [])
    mine = next((p for p in plugins if isinstance(p, dict) and p.get("plugin_id") == PLUGIN_ID), None)
    if not mine or not mine.get("enabled", True):
        return "Herdr Shell is disabled or removed in this server"
    root = mine.get("plugin_root")
    if isinstance(root, str) and Path(root).expanduser().resolve() != ROOT.resolve():
        return f"Herdr Shell now runs from {root}"
    return None


def run(socket_path):
    """Foreground daemon for one Herdr server; exits if another holds the lock."""
    socket_path = os.path.realpath(str(socket_path))
    lock_path, ledger_path, _ = paths(socket_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    holder = lock_path.open("a+")
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder.close()
        return {"namer": "running"}
    holder.seek(0)
    holder.truncate()
    holder.write(f"{os.getpid()}\n")
    holder.flush()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    client = Client(socket_path)
    namer = Namer(client, Ledger(ledger_path), log=_log)
    stream = EventStream(socket_path, namer.observe)
    signature = _code_signature()
    next_check, retry_at, missing_since, backoff = 0.0, 0.0, None, 1.0
    failing_since = None
    _log(f"namer started for {socket_path} (pid {os.getpid()})")
    reason = None
    while not stopping.is_set():
        now = time.monotonic()
        # Before any pass: a namer from another checkout, or for a server without
        # this plugin, must not rename anything.
        if now >= next_check:
            next_check = now + FULL_PASS_SECONDS
            if _code_signature() != signature:
                time.sleep(2)   # let a multi-file update finish before re-executing
                stream.close()
                holder.close()
                _log("code changed; restarting")
                os.execv(sys.executable, _command(socket_path))
            try:
                reason = _retire_reason(client)
            except ShellError:
                reason = None
            if reason:
                break
        if not stream.alive() and now >= retry_at:
            if not Path(socket_path).exists():
                missing_since = missing_since or now
                if now - missing_since > 15:
                    reason = "the Herdr server stopped"
                    break
            else:
                try:
                    stream.start()
                    missing_since, failing_since, backoff = None, None, 1.0
                    namer.request_full()
                except ShellError as exc:
                    failing_since = failing_since or now
                    if now - failing_since > 120:
                        reason = f"the Herdr server is unreachable: {exc}"
                        break
                    _log(str(exc))
                    backoff = min(backoff * 2, 30.0)
            retry_at = now + backoff
        if stream.alive():
            try:
                namer.step()
            except ShellError as exc:
                _log(f"pass failed: {exc}")
                namer.request_full(5.0, retry=True)
        stopping.wait(0.25)
    stream.close()
    _log(f"namer stopped: {reason or 'signal'}")
    holder.close()
    return {"namer": "stopped", "reason": reason or "signal"}


def dispatch(command, socket_path):
    if command == "run":
        run(socket_path)   # the daemon's stdout is its log, which already records stops
        return None
    if command == "ensure":
        return ensure(socket_path)
    return stop(socket_path)
