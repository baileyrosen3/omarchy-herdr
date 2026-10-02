"""Owned, offline-safe setup for the Omarchy service's private runtime copy.

Omarchy owns the source checkout. Herdr links the stable runtime copy, so
removal can still clean up after Omarchy has deleted its source directory.
"""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import tomllib
import uuid

from . import PLUGIN_ID, desktop, footer_setup
from .config import ConfigStore, MISSING, atomic_write, command_id, shortcut_changes
from .runtime import ShellError, config_path, run_herdr, state_path


ROOT = Path(__file__).resolve().parents[1]
_PREF_FILES = ("hyprland.lua", "plugin-root", "desktop-enabled", "binding-status.json")
_MAX_SERVERS = 16


def _state():
    return state_path() / "omarchy"


@contextmanager
def _locked():
    folder = _state()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / "managed.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _native(*arguments):
    # Use the native CLI's global registry fallback, regardless of any plugin
    # invocation or selected session inherited by the shell service.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERDR_PLUGIN_") and k not in
           ("HERDR_ENV", "HERDR_SESSION", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID")}
    # Unix socket paths have a short length limit. A private directory under
    # /tmp also works when HOME/XDG_STATE_HOME has a long path.
    with tempfile.TemporaryDirectory(prefix="herdr-shell-registry-", dir="/tmp") as folder:
        env["HERDR_SOCKET_PATH"] = str(Path(folder) / "offline.sock")
        output = run_herdr(["plugin", *arguments], env=env)
    if arguments[0] == "uninstall":
        return {"removed": True}
    try:
        reply = json.loads(output)
        if not isinstance(reply, dict) or "error" in reply or not isinstance(reply.get("result"), dict):
            raise ValueError("unexpected response")
        return reply["result"]
    except (ValueError, TypeError) as exc:
        raise ShellError("Herdr returned an invalid plugin registry response.") from exc


def _plugin():
    plugins = _native("list", "--json").get("plugins")
    if not isinstance(plugins, list) or any(not isinstance(p, dict) for p in plugins):
        raise ShellError("Herdr returned an invalid plugin list.")
    matches = [p for p in plugins if p.get("plugin_id") == PLUGIN_ID]
    if len(matches) > 1:
        raise ShellError("Herdr has duplicate registrations for Herdr Shell.")
    if matches and (not isinstance(matches[0].get("plugin_root"), str)
                    or not isinstance(matches[0].get("enabled"), bool)
                    or not isinstance(matches[0].get("source", {}), dict)):
        raise ShellError("Herdr returned an invalid plugin registration.")
    return matches[0] if matches else None


def _owned(plugin):
    return bool(plugin and plugin.get("source", {}).get("kind") == "local"
                and Path(plugin.get("plugin_root", "")).expanduser().resolve() == ROOT.resolve())


def _helper():
    return Path.home() / ".local/bin/herdr-shell", ROOT / "bin/herdr-shell"


def _owned_helper():
    destination, source = _helper()
    return destination.is_symlink() and destination.resolve() == source.resolve()


def _preflight():
    plugin = _plugin()
    if plugin is not None and not _owned(plugin):
        raise ShellError("Herdr Shell is registered from another installation; its registration was preserved.")
    destination, _ = _helper()
    if (destination.exists() or destination.is_symlink()) and not _owned_helper():
        raise ShellError("The herdr-shell CLI belongs to another installation; it was preserved.")
    owner = desktop.preferences_dir() / "plugin-root"
    if desktop.installed() and owner.exists():
        value = owner.read_text().strip()
        if not value or Path(value).expanduser().resolve() != ROOT.resolve():
            raise ShellError("The desktop bridge belongs to another installation; it was preserved.")
    elif desktop.installed():
        raise ShellError("Cannot verify the existing desktop bridge owner; it was preserved.")
    return plugin


def _saved(name):
    path = _state() / name
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or Path(value["plugin_root"]).resolve() != ROOT.resolve():
            raise ValueError("different owner")
        if name == "suspended.json" and any(not isinstance(value.get(k), bool)
                                            for k in ("native_enabled", "desktop_enabled")):
            raise ValueError("invalid suspended preferences")
        return value
    except (ValueError, KeyError, TypeError) as exc:
        raise ShellError("Cannot verify managed setup state; no changes were made.") from exc


def _revision():
    digest = hashlib.sha256()
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if path.is_file() and not any(p in (".git", "__pycache__") for p in relative.parts):
            digest.update(str(relative).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _socket_paths():
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "herdr"
    paths = [root / "herdr.sock"]
    sessions = root / "sessions"
    try:
        if sessions.is_dir():
            paths += [p / "herdr.sock" for p in sorted(sessions.iterdir()) if p.is_dir() and not p.is_symlink()]
    except FileNotFoundError:
        pass
    found = []
    for path in paths[:_MAX_SERVERS]:
        try:
            if stat.S_ISSOCK(path.stat().st_mode):
                found.append(path)
        except FileNotFoundError:
            # Server exit/handoff can unlink the socket during discovery.
            continue
    return found


def _peer_config(pid):
    entries = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    env = {}
    for entry in entries:
        key, _, value = entry.partition(b"=")
        if key in (b"HOME", b"XDG_CONFIG_HOME", b"HERDR_CONFIG_PATH"):
            env[key.decode()] = value.decode()
    root = Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME", str(Path.home()))) / ".config")
    return Path(env.get("HERDR_CONFIG_PATH") or root / "herdr/config.toml").expanduser().resolve()


def _reload_servers():
    reloaded, skipped = [], []
    wanted = config_path()
    for path in _socket_paths():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(1.5)
                conn.connect(str(path))
                pid, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid() or _peer_config(pid) != wanted:
                    skipped.append(str(path))
                    continue
                request_id = uuid.uuid4().hex
                request = {"id": request_id, "method": "server.reload_config", "params": {}}
                conn.sendall((json.dumps(request) + "\n").encode())
                with conn.makefile("rb") as stream:
                    raw = stream.readline(65537)
                if not raw:
                    skipped.append(str(path))
                    continue
                if len(raw) > 65536:
                    raise ShellError("Herdr config reload response exceeded its limit.")
                reply = json.loads(raw)
                if not isinstance(reply, dict):
                    raise ValueError("unexpected response")
                if reply.get("id") != request_id or "error" in reply or "result" not in reply:
                    raise ShellError("Herdr config reload failed: " + str(reply.get("error", "unexpected reply")))
                reloaded.append(str(path))
        except (TimeoutError, ConnectionResetError, BrokenPipeError):
            # A session can stop or hand off during discovery. Future servers
            # read the same registry/config and need no persistent polling.
            skipped.append(str(path))
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED, errno.EACCES, errno.EPERM, errno.ESRCH):
                raise ShellError("Cannot reload Herdr config: " + str(exc)) from exc
            skipped.append(str(path))
        except (ValueError, TypeError) as exc:
            raise ShellError("Herdr returned an invalid config reload response.") from exc
    return {"reloaded": reloaded, "skipped": skipped}


def _desktop_backup():
    path = desktop.hypr_path()
    return {"config": path.read_text() if path.exists() else None,
            "mode": path.stat().st_mode & 0o777 if path.exists() else 0o600,
            "prefs": {name: (desktop.preferences_dir() / name).read_text()
                      if (desktop.preferences_dir() / name).exists() else None for name in _PREF_FILES}}


def _restore_desktop(saved, expected):
    path = desktop.hypr_path()
    if (path.read_text() if path.exists() else None) != expected["config"]:
        raise ShellError("Desktop config changed during rollback; the later edit was preserved.")
    owner = desktop.preferences_dir() / "plugin-root"
    if owner.exists() and Path(owner.read_text().strip()).resolve() != ROOT.resolve():
        raise ShellError("Desktop ownership changed during rollback; the newer installation was preserved.")
    conflicts = []
    for name, value in saved["prefs"].items():
        target = desktop.preferences_dir() / name
        current = target.read_text() if target.exists() else None
        if current != expected["prefs"][name]:
            conflicts.append(name)
            continue
        if value is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, value)
    if saved["config"] is not None:
        atomic_write(path, saved["config"], saved["mode"])
        desktop.hypr("reload")
        if desktop.installed():
            desktop.reconcile()
    if conflicts:
        raise ShellError("Desktop preferences changed during rollback; later edits were preserved: " + ", ".join(conflicts))


def _check_desktop(saved):
    if _desktop_backup() != saved:
        raise ShellError("Desktop setup changed during this operation; the later edit was preserved.")


def _restore_plugin(previous):
    current = _plugin()
    if current and not _owned(current):
        raise ShellError("Native plugin ownership changed; the newer installation was preserved.")
    if previous:
        if current is None or current.get("enabled") != previous.get("enabled"):
            _native("link", str(ROOT), "--enabled" if previous.get("enabled", True) else "--disabled")
    elif current:
        # Unlike unlink, uninstall has an offline fallback; local linked source
        # files are retained by Herdr. Never invoke it for a GitHub checkout.
        _native("uninstall", PLUGIN_ID)


def _rollback(exc, previous, native_attempted, saved_desktop, desktop_after, helper_created=False, helper_removed=False):
    failures = []
    destination, source = _helper()
    try:
        if helper_created and _owned_helper():
            destination.unlink()
        if helper_removed and not (destination.exists() or destination.is_symlink()):
            destination.symlink_to(source)
    except OSError as error:
        failures.append("CLI helper: " + str(error))
    if native_attempted:
        try:
            _restore_plugin(previous)
            # ConfigStore's recovery reload happens before this resource
            # rollback. Reload again after restoring the registry so live
            # sessions see the same native state as future sessions.
            _reload_servers()
        except (ShellError, OSError) as error:
            failures.append("native registration: " + str(error))
    if desktop_after is not None:
        try:
            _restore_desktop(saved_desktop, desktop_after)
        except (ShellError, OSError) as error:
            failures.append("desktop bridge: " + str(error))
    if failures:
        raise ShellError(str(exc) + "; setup rollback incomplete: " + "; ".join(failures)) from exc
    raise exc


def activate():
    """Install or refresh the cached build, restoring only our suspended state."""
    with _locked():
        previous = _preflight()
        suspended = _saved("suspended.json")
        receipt = _saved("setup.json")
        native_enabled = suspended.get("native_enabled", True) if suspended else previous.get("enabled", True) if previous else True
        had_desktop = desktop.installed()
        desktop_enabled = suspended.get("desktop_enabled", False) if suspended else desktop.enabled() if had_desktop else True
        store = ConfigStore()
        reserved_shortcuts = []
        before = store.read()
        changes = shortcut_changes(existing=before, reserved=reserved_shortcuts)
        changes += footer_setup.changes(before, ROOT, enabled=native_enabled)
        proposal = store.prepare(changes, before)
        if proposal["changes"]:
            store.validator(proposal["after"])
        revision = _revision()
        native_changed = previous is None or previous.get("enabled") != native_enabled or not receipt or receipt.get("revision") != revision
        code = desktop.integration_text()
        loader = desktop.preferences_dir() / "hyprland.lua"
        bridge_changed = not had_desktop or not loader.exists() or loader.read_text() != code
        # Conflict handling lives in Python too, so package updates must refresh
        # registration even when the generated Lua stays identical.
        refresh_desktop = bridge_changed or native_changed
        profile_changed = had_desktop and desktop.enabled() != desktop_enabled
        destination, source = _helper()
        if not (native_changed or bridge_changed or profile_changed or proposal["changes"]
                or not _owned_helper() or suspended):
            return {"active": True, "native_enabled": bool(native_enabled), "desktop_enabled": desktop.enabled(),
                    "reserved_shortcuts": reserved_shortcuts,
                    "native_changed": False, "desktop_changed": False, "helper_changed": False, "changed": False}
        helper_created = native_attempted = False
        saved_desktop = _desktop_backup()
        desktop_after = None
        reload_result = {}
        receipt_before = (_state() / "setup.json").read_text() if receipt is not None else None
        try:
            if native_changed:
                if _plugin() != previous:
                    raise ShellError("Native registration changed during setup; refresh and try again.")
                native_attempted = True
                _native("link", str(ROOT), "--enabled" if native_enabled else "--disabled")
                linked = _plugin()
                if not _owned(linked) or linked.get("enabled") != native_enabled:
                    raise ShellError("Herdr did not confirm this build's native registration.")
            if not _owned_helper():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    raise ShellError("CLI ownership changed during setup; the later edit was preserved.")
                destination.symlink_to(source)
                helper_created = True
            if refresh_desktop:
                _check_desktop(saved_desktop)
                bridge = desktop.install_proposal()
                desktop.install_desktop(bridge, activate=desktop_enabled)
                desktop_after = _desktop_backup()
            elif profile_changed:
                _check_desktop(saved_desktop)
                desktop.set_enabled(desktop_enabled)
                desktop_after = _desktop_backup()
            atomic_write(_state() / "setup.json", json.dumps({"plugin_root": str(ROOT), "revision": revision}))

            def reload():
                reload_result.update(_reload_servers())
            result = store.apply(proposal, reload)
            if not proposal["changes"] and native_changed:
                reload()
        except Exception as exc:
            if receipt_before is None:
                (_state() / "setup.json").unlink(missing_ok=True)
            else:
                atomic_write(_state() / "setup.json", receipt_before)
            _rollback(exc, previous, native_attempted, saved_desktop, desktop_after, helper_created)
        (_state() / "suspended.json").unlink(missing_ok=True)
        return {"active": True, "native_enabled": bool(native_enabled), "desktop_enabled": desktop.enabled(),
                "reserved_shortcuts": reserved_shortcuts,
                "native_changed": native_changed, "desktop_changed": refresh_desktop or profile_changed,
                "helper_changed": helper_created, **result, **reload_result,
                "changed": bool(result["changed"] or native_changed or refresh_desktop or profile_changed or helper_created)}


def deactivate(remove=False):
    """Suspend owned controls, or remove their registration/config/helper."""
    with _locked():
        previous = _preflight()
        suspended = _saved("suspended.json")
        had_desktop = desktop.installed()
        saved_desktop = _desktop_backup()
        native_attempted = helper_removed = False
        desktop_after = None
        store = ConfigStore()
        before = store.read()
        changes = []
        if remove:
            commands = tomllib.loads(before).get("keys", {}).get("command", [])
            changes = [(["keys", "command", "@" + command_id(c)], MISSING) for c in commands
                       if c.get("type") == "plugin_action" and isinstance(c.get("command"), str)
                       and c["command"].startswith(PLUGIN_ID + ".")]
        changes += footer_setup.changes(before, ROOT, remove=True)
        proposal = store.prepare(changes, before)
        if proposal["changes"]:
            store.validator(proposal["after"])
        if not remove and suspended is None:
            atomic_write(_state() / "suspended.json", json.dumps({"plugin_root": str(ROOT),
                         "native_enabled": previous.get("enabled", True) if previous else True,
                         "desktop_enabled": desktop.enabled() if had_desktop else True}))
        reload_result = {}
        try:
            if had_desktop:
                _check_desktop(saved_desktop)
                if remove:
                    bridge = desktop.install_proposal(remove=True)
                    desktop.install_desktop(bridge, remove=True)
                    desktop_after = _desktop_backup()
                elif desktop.enabled():
                    desktop.set_enabled(False)
                    desktop_after = _desktop_backup()
            if previous and (remove or previous.get("enabled", True)):
                if _plugin() != previous:
                    raise ShellError("Native registration changed during removal; the later installation was preserved.")
                native_attempted = True
                if remove:
                    _native("uninstall", PLUGIN_ID)
                    if _plugin() is not None:
                        raise ShellError("Herdr did not confirm removal of this registration.")
                else:
                    _native("link", str(ROOT), "--disabled")
                    linked = _plugin()
                    if not _owned(linked) or linked.get("enabled"):
                        raise ShellError("Herdr did not confirm disabling this registration.")
            if remove and _owned_helper():
                _helper()[0].unlink()
                helper_removed = True

            def reload():
                reload_result.update(_reload_servers())
            result = store.apply(proposal, reload)
            if not proposal["changes"] and native_attempted:
                reload()
        except Exception as exc:
            if not remove and suspended is None:
                (_state() / "suspended.json").unlink(missing_ok=True)
            _rollback(exc, previous, native_attempted, saved_desktop, desktop_after,
                      helper_removed=helper_removed)
        if remove:
            owner = desktop.preferences_dir() / "plugin-root"
            if owner.exists() and Path(owner.read_text().strip()).resolve() == ROOT.resolve():
                for name in _PREF_FILES:
                    (desktop.preferences_dir() / name).unlink(missing_ok=True)
            for name in ("suspended.json", "setup.json"):
                (_state() / name).unlink(missing_ok=True)
        return {"removed": bool(remove), "active": False, "native_changed": native_attempted,
                "desktop_changed": desktop_after is not None, "helper_changed": helper_removed,
                **result, **reload_result,
                "changed": bool(result["changed"] or native_attempted or desktop_after is not None or helper_removed)}
