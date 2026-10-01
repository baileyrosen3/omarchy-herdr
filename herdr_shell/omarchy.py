"""Omarchy service supervisor, kept outside the checkout for safe removal.

Omarchy has no pre-remove hook: it deletes the plugin directory immediately.
The service therefore starts a singleton from a small private runtime copy and
observes the manager's real directory/config, never QML object destruction.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib

from . import PLUGIN_ID
from .config import atomic_write
from .runtime import ShellError, state_path


ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 2


def source_path():
    # Omarchy's plugin CLI uses HOME/.config, independently of XDG_CONFIG_HOME.
    return Path.home() / ".config/omarchy/plugins" / PLUGIN_ID


def control_path():
    return state_path() / "omarchy"


def runtime_path():
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "herdr-shell/omarchy/runtime"


def status():
    try:
        value = json.loads((control_path() / "status.json").read_text())
        return value if isinstance(value, dict) else {"state": "unknown"}
    except (OSError, ValueError):
        return {"state": "unmanaged"}


def write_status(state, **details):
    atomic_write(control_path() / "status.json", json.dumps(
        {"state": state, "updated_at": int(time.time()), **details}, indent=2) + "\n")


@contextmanager
def locked(name, *, blocking=True):
    folder = control_path()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / name).open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def source_state(source):
    """Return None on an incomplete/invalid write, never treat it as disable."""
    if not source.exists():
        return "removed"
    try:
        manifest = json.loads((source / "manifest.json").read_text())
        if not isinstance(manifest, dict) or manifest.get("id") != PLUGIN_ID or type(manifest.get("schemaVersion")) is not int or manifest.get("schemaVersion") != 1:
            return None
        config = json.loads((Path.home() / ".config/omarchy/shell.json").read_text())
        if not isinstance(config, dict) or not isinstance(config.get("plugins", []), list) or not isinstance(config.get("disabledPlugins", []), list):
            return None
        if PLUGIN_ID in config.get("disabledPlugins", []):
            return "disabled"
        entries = config.get("plugins", [])
        return "enabled" if any(entry == PLUGIN_ID or isinstance(entry, dict) and
                                entry.get("id") == PLUGIN_ID for entry in entries) else "disabled"
    except (OSError, ValueError, TypeError):
        return None


def payload(source):
    """Read and validate a coherent executable snapshot; omit Git and QA data."""
    paths = [Path("bin/herdr-shell"), Path("herdr-plugin.toml"), Path("integrations/hyprland.lua"),
             Path("LICENSE"), Path("assets/branding/README.md")]
    paths += sorted(p.relative_to(source) for p in (source / "herdr_shell").glob("*.py"))
    required = {Path("herdr_shell/cli.py"), Path("herdr_shell/managed.py"), Path("herdr_shell/omarchy.py")}
    if not required.issubset(paths):
        raise ShellError("The Omarchy plugin runtime is incomplete; keep the previous version.")
    files = {}
    digest = hashlib.sha256()
    for relative in sorted(paths):
        path = source / relative
        if any(p.is_symlink() for p in (path, *path.parents) if p != source.parent):
            raise ShellError("Managed runtime files must not be symlinks.")
        data = path.read_bytes()
        if path.suffix == ".py" or relative == Path("bin/herdr-shell"):
            compile(data, str(path), "exec")
        if path.suffix == ".toml":
            if tomllib.loads(data.decode()).get("id") != PLUGIN_ID:
                raise ShellError("The Herdr manifest belongs to a different plugin.")
        files[str(relative)] = data
        digest.update(str(relative).encode() + b"\0" + data + b"\0")
    current_modules = sorted(p.relative_to(source) for p in (source / "herdr_shell").glob("*.py"))
    if current_modules != sorted(p for p in paths if p.suffix == ".py") or any(
            (source / relative).read_bytes() != data for relative, data in files.items()):
        raise ShellError("The source changed while reading an update; keep the previous runtime and retry.")
    return digest.hexdigest(), files


def snapshot(source, revision, files):
    """Replace files atomically; never copy or delete an unrelated directory."""
    destination = runtime_path()
    marker = destination / ".managed-runtime.json"
    if destination.is_symlink():
        raise ShellError("The managed runtime directory is a symlink; preserved it.")
    previous = {}
    if destination.exists():
        try:
            previous = json.loads(marker.read_text())
        except (OSError, ValueError) as exc:
            raise ShellError("The runtime directory is not owned by this integration; preserved it.") from exc
        if not isinstance(previous, dict) or previous.get("id") != PLUGIN_ID or previous.get("source") != str(source):
            raise ShellError("The managed runtime has a different owner; preserved it.")
    inventory = previous.get("files", [])
    if not isinstance(inventory, list) or any(not isinstance(p, str) or Path(p).is_absolute() or
                                            ".." in Path(p).parts for p in inventory):
        raise ShellError("Invalid managed runtime inventory; preserved old files.")
    for relative in set(files) | set(inventory):
        target = destination / relative
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ShellError("The managed runtime contains a symlink; preserved it.")
    bytecode = destination / "herdr_shell/__pycache__"
    if bytecode.is_symlink():
        raise ShellError("The managed bytecode directory is a symlink; preserved it.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative, data in files.items():
        target = destination / relative
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ShellError("The managed runtime contains a symlink; preserved it.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".update-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o755 if relative == "bin/herdr-shell" else 0o600)
                stream.write(data)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    # Only files named in our previous snapshot may be pruned.
    for relative in inventory:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ShellError("Invalid managed runtime inventory; preserved old files.")
        if relative not in files:
            (destination / path).unlink(missing_ok=True)
    # Timestamp-based .pyc files can match a same-size update within one second.
    # They are disposable; always import the freshly copied source after update.
    for cached in bytecode.glob("*.pyc"):
        cached.unlink()
    atomic_write(marker, json.dumps({"id": PLUGIN_ID, "source": str(source),
                                   "revision": revision, "files": sorted(files)}, indent=2) + "\n")


def start():
    """Called by QML, including after unrelated shell restarts/hot reloads."""
    source = source_path()
    if ROOT != source.resolve():
        raise ShellError("The service must be installed with omarchy plugin add; developer links are managed separately.")
    if source_state(source) != "enabled":
        return {"started": False, "reason": "Plugin is disabled or shell configuration is being written."}
    with locked("snapshot.lock"):
        revision, files = payload(source)
        marker = runtime_path() / ".managed-runtime.json"
        try:
            existing = json.loads(marker.read_text())
        except (OSError, ValueError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        if existing.get("id") != PLUGIN_ID or existing.get("source") != str(source) or existing.get("revision") != revision:
            snapshot(source, revision, files)
    folder = control_path()
    with (folder / "service.log").open("a") as log:
        process = subprocess.Popen([sys.executable, str(runtime_path() / "bin/herdr-shell"),
                                    "_omarchy", "watch", "--source", str(source), "--revision", revision],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, close_fds=True)
    return {"started": True, "pid": process.pid, "source": str(source)}


def run_runtime(operation, *, remove=False):
    command = [sys.executable, str(runtime_path() / "bin/herdr-shell"), "_omarchy", operation]
    if remove:
        command += ["--remove"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ShellError((result.stderr or result.stdout).strip() or "Managed integration setup failed.")
    return result.stdout.strip()


def watch(source, running_revision):
    """Reconcile only stable manager changes, and survive checkout deletion."""
    source = Path(source)
    if source != source_path() or ROOT != runtime_path().resolve():
        raise ShellError("Invalid managed service source or runtime.")
    with locked("service.lock", blocking=False) as acquired:
        if not acquired:
            return {"watching": False, "reason": "The service is already running."}
        os.environ["HERDR_SHELL_DESKTOP_CONFIG_HOME"] = str(Path.home() / ".config")
        last = status()
        applied = last.get("revision") if last.get("state") == "ready" else None
        suspended = last.get("state") == "disabled"
        observed, stable, retry_at = None, 0, 0
        while True:
            desired = source_state(source)
            stable = stable + 1 if desired == observed and desired is not None else 1
            observed = desired
            if desired is None or stable < 2 or time.monotonic() < retry_at:
                time.sleep(POLL_SECONDS)
                continue
            try:
                if desired in ("removed", "disabled"):
                    if desired == "removed" or not suspended:
                        output = run_runtime("deactivate", remove=desired == "removed")
                        write_status(desired, source=str(source), result=output)
                        suspended, applied = True, None
                    if desired == "removed":
                        return {"removed": True}
                else:
                    with locked("snapshot.lock"):
                        revision, files = payload(source)
                        marker = json.loads((runtime_path() / ".managed-runtime.json").read_text())
                        if not isinstance(marker, dict):
                            raise ShellError("Invalid managed runtime metadata; preserved the installed version.")
                        if revision != marker.get("revision"):
                            snapshot(source, revision, files)
                    if revision != running_revision:
                        # Start the new supervisor code, rather than keeping old
                        # imports alive after the manager has updated the repo.
                        # The detached replacement waits for this lock to exit.
                        with (control_path() / "service.log").open("a") as log:
                            subprocess.Popen([sys.executable, str(runtime_path() / "bin/herdr-shell"),
                                              "_omarchy", "watch", "--source", str(source),
                                              "--revision", revision, "--wait"],
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                             start_new_session=True, close_fds=True)
                        return {"restarting": True}
                    if revision != applied or suspended:
                        write_status("configuring", source=str(source), revision=revision)
                        output = run_runtime("activate")
                        write_status("ready", source=str(source), revision=revision, result=output)
                        applied, suspended = revision, False
                retry_at = 0
            except (ShellError, OSError, ValueError, SyntaxError, subprocess.TimeoutExpired) as exc:
                write_status("error", source=str(source), error=str(exc))
                print("Omarchy Herdr: " + str(exc), file=sys.stderr, flush=True)
                retry_at = time.monotonic() + 30
            time.sleep(POLL_SECONDS)


def dispatch(args):
    if args.omarchy_command == "start":
        return start()
    if args.omarchy_command == "watch":
        # A revision replacement can overlap its predecessor briefly.
        if args.wait:
            with locked("service.lock"):
                pass
        return watch(args.source, args.revision)
    from . import managed
    if args.omarchy_command == "activate":
        return managed.activate()
    return managed.deactivate(remove=args.remove)
