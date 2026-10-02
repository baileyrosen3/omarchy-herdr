"""Previewable updates and removal of only this checkout's installation."""
from pathlib import Path
import subprocess
import sys
import tomllib

from . import PLUGIN_ID, desktop, footer_setup
from .config import MISSING, command_id
from .runtime import Client, ShellError, resolve_socket


ROOT = Path(__file__).resolve().parents[1]


def _git(*arguments):
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *arguments], capture_output=True,
                                text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShellError("Git update failed: " + str(exc)) from exc
    if result.returncode:
        raise ShellError((result.stderr or result.stdout).strip() or "Git update failed.")
    return result.stdout.strip()


def _git_preflight():
    if Path(_git("rev-parse", "--show-toplevel")).resolve() != ROOT.resolve():
        raise ShellError("Update must run from the plugin's own Git checkout.")
    if _git("status", "--porcelain"):
        raise ShellError("The checkout has local changes. Keep them, or use update --local to refresh this build without pulling.")
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    return {"branch": branch, "upstream": upstream, "head": _git("rev-parse", "HEAD")}


def _plugin(client):
    return next((p for p in client.call("plugin.list").get("plugins", []) if p["plugin_id"] == PLUGIN_ID), None)


def _same_root(plugin):
    return plugin and Path(plugin["plugin_root"]).expanduser().resolve() == ROOT.resolve()


def _helper():
    return Path.home() / ".local/bin/herdr-shell", ROOT / "bin/herdr-shell"


def _owned_helper():
    destination, source = _helper()
    return destination.is_symlink() and destination.resolve() == source.resolve()


def _desktop_owner():
    if not desktop.installed():
        return False
    try:
        owner = (desktop.preferences_dir() / "plugin-root").read_text().strip()
    except OSError as exc:
        raise ShellError("Cannot verify the desktop bridge owner; no lifecycle changes were made.") from exc
    if not owner or Path(owner).expanduser().resolve() != ROOT.resolve():
        raise ShellError("The desktop bridge belongs to another checkout. Run this command from its installation.")
    return True


def update(args, store, install_callback):
    """Refresh this checkout; install_callback(args, store) is cli.install."""
    had_desktop = _desktop_owner()
    was_enabled = desktop.enabled() if had_desktop else False
    destination, _ = _helper()
    if (destination.exists() or destination.is_symlink()) and not _owned_helper():
        raise ShellError("The CLI helper belongs to another installation; update that checkout instead.")
    git = None if getattr(args, "local", False) else _git_preflight()
    if not args.apply:
        return {"preview": True, "local": git is None, "git": git,
                "refresh": ["plugin registration", "native fallback shortcuts", "CLI helper"],
                "desktop": {"refresh": had_desktop, "enabled": was_enabled}}
    # Resolve the selected server before changing Git, rather than discovering
    # a missing or different installation only after a successful pull.
    client = Client(resolve_socket(args))
    previous = _plugin(client)
    if previous and not _same_root(previous):
        raise ShellError("The plugin is linked from another checkout; update that installation instead.")
    pulled = False
    if git:
        if _git_preflight() != git:
            raise ShellError("The checkout changed during update preflight. Review it again.")
        _git("pull", "--ff-only", "--no-rebase")
        pulled = _git("rev-parse", "HEAD") != git["head"]
        if pulled:
            # Imports in this process still describe the previous commit.
            # Re-enter the newly pulled CLI with an explicit session/config.
            command = [sys.executable, str(ROOT / "bin/herdr-shell"), "--socket", str(client.path),
                       "--config", str(store.path), "update", "--local", "--apply"]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ShellError("Git was updated, but refreshing the installation failed. Run update --local --apply: " + str(exc)) from exc
            if result.returncode:
                raise ShellError("Git was updated, but refreshing the installation failed. Run update --local --apply: " +
                                 (result.stderr or result.stdout).strip())
            return {"updated": True, "pulled": True, "refresh_output": result.stdout.strip()}
    result = install_callback(args, store)
    bridge = None
    if had_desktop:
        # Native installation must not silently change a profile preference.
        if not _desktop_owner() or desktop.enabled() != was_enabled:
            raise ShellError("The desktop bridge changed during update. Native installation was refreshed; review desktop setup separately.")
        bridge = desktop.install_desktop(desktop.install_proposal(), activate=was_enabled)
    return {"updated": True, "pulled": pulled, "installation": result, "desktop": bridge}


def _removal_proposal(store):
    before = store.read()
    doc = tomllib.loads(before)
    commands = doc.get("keys", {}).get("command", [])
    changes = [(["keys", "command", "@" + command_id(command)], MISSING)
               for command in commands if command.get("type") == "plugin_action"
               and isinstance(command.get("command"), str)
               and command["command"].startswith(PLUGIN_ID + ".")]
    changes += footer_setup.changes(before, ROOT, remove=True)
    return store.prepare(changes, before)


def remove(args, store):
    """Remove managed shortcuts/link/helper, retaining all user data and config."""
    had_desktop = _desktop_owner()
    was_enabled = desktop.enabled() if had_desktop else False
    proposal = _removal_proposal(store)
    print(proposal["diff"] or "No Herdr Shell fallback shortcuts to remove.")
    if not args.apply:
        return {"preview": True, "shortcuts": len(proposal["changes"]), "desktop": had_desktop,
                "helper": _owned_helper(), "plugin": "Unlink only if its registered root matches this checkout."}
    store.validator(proposal["after"])
    client = Client(resolve_socket(args))
    previous = _plugin(client)
    if previous and not _same_root(previous):
        raise ShellError("The plugin is linked from another checkout; remove that installation instead.")
    own_plugin = bool(_same_root(previous))
    destination, source = _helper()
    own_helper = _owned_helper()
    removal = desktop.install_proposal(remove=True) if had_desktop else None
    desktop_removed = helper_removed = unlink_attempted = failed_commit = False
    result = {"changed": False}

    def restore_resources():
        failures = []
        if helper_removed and not (destination.exists() or destination.is_symlink()):
            try:
                destination.symlink_to(source)
            except OSError as exc:
                failures.append("CLI helper: " + str(exc))
        if unlink_attempted:
            try:
                current = _plugin(client)
                # An absent registration may mean an unlink committed before
                # its response was lost. A newer checkout keeps its ownership.
                if current is None:
                    client.call("plugin.link", path=previous["plugin_root"], enabled=previous["enabled"])
            except (ShellError, OSError, KeyError) as exc:
                failures.append("plugin registration: " + str(exc))
        return failures

    def commit():
        nonlocal helper_removed, unlink_attempted, failed_commit
        # ConfigStore invokes this again after restoring a failed config write.
        if failed_commit:
            return client.call("server.reload_config")
        try:
            client.call("server.reload_config")
            if own_plugin:
                current = _plugin(client)
                if not _same_root(current) or current != previous:
                    raise ShellError("Plugin registration changed during removal; preserved the newer registration.")
                unlink_attempted = True
                client.call("plugin.unlink", plugin_id=PLUGIN_ID)
            if own_helper and _owned_helper():
                destination.unlink()
                helper_removed = True
        except Exception as exc:
            failed_commit = True
            failures = restore_resources()
            if failures:
                raise ShellError(str(exc) + "; removal rollback failed: " + "; ".join(failures)) from exc
            raise

    try:
        if had_desktop:
            desktop.install_desktop(removal, remove=True)
            desktop_removed = True
        if proposal["changes"]:
            result = store.apply(proposal, commit)
        elif own_plugin or own_helper:
            commit()
    except Exception as exc:
        failures = []
        if desktop_removed:
            # Do not overwrite a personal edit made while removal was running.
            try:
                current_config = desktop.hypr_path().read_text()
                if current_config == removal["after"]:
                    owner = (desktop.preferences_dir() / "plugin-root").read_text().strip()
                    if not owner or Path(owner).expanduser().resolve() != ROOT.resolve():
                        raise ShellError("desktop bridge ownership changed")
                    desktop.install_desktop(desktop.install_proposal(), activate=was_enabled)
                else:
                    failures.append("desktop config changed; preserved the later edit")
            except (ShellError, OSError, ValueError) as rollback_error:
                failures.append("desktop bridge: " + str(rollback_error))
        if failures:
            raise ShellError(str(exc) + "; removal rollback incomplete: " + "; ".join(failures)) from exc
        raise
    return {"removed": PLUGIN_ID, "desktop": desktop_removed, "plugin": unlink_attempted,
            "helper": helper_removed, **result}
