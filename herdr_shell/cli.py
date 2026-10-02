"""Human CLI and manifest entrypoints."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib

from . import PLUGIN_ID, VERSION
from .actions import CATALOG, available, catalog_rows, execute, execute_close, open_ui
from .config import ConfigStore, bindings, conflicts, shortcut_changes
from .runtime import Client, Context, ShellError, binary, config_path, resolve_context, resolve_socket, run_herdr, state_path


def parser():
    p = argparse.ArgumentParser(description="Herdr menus, actions, keybindings and reversible configuration.")
    p.add_argument("--version", action="version", version=VERSION)
    target = p.add_mutually_exclusive_group()
    target.add_argument("--socket", help="Explicit Herdr socket")
    target.add_argument("--session", help="Named local session (default for the default session)")
    p.add_argument("--pane", help="Explicit originating pane")
    p.add_argument("--active", action="store_true", help="Explicitly use the selected session's focused pane")
    p.add_argument("--config", help="Herdr config file to edit/check")
    p.add_argument("--context", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    footer = sub.add_parser("footer", help="One row of currently active shortcut hints")
    footer.add_argument("--width", type=int, help="Available terminal columns (default: native footer width)")
    for name in ("menu", "settings"):
        sub.add_parser(name).add_argument("--inline", action="store_true", help="Run here instead of opening a popup")
    learn = sub.add_parser("learn", help="Read-only walkthrough, hands-on practice, or timed Speed Run")
    learn.add_argument("mode", nargs="?", choices=["welcome", "walkthrough", "hands-on", "game"], default="welcome")
    commands = sub.add_parser("commands")
    commands.add_argument("--json", action="store_true")
    action = sub.add_parser("action").add_subparsers(dest="action_command", required=True)
    run = action.add_parser("run")
    run.add_argument("id", choices=sorted(CATALOG))
    run.add_argument("--yes", action="store_true")
    key = sub.add_parser("keybindings")
    key.add_argument("--json", action="store_true")
    key.add_argument("--inline", action="store_true")
    ks = key.add_subparsers(dest="key_command")
    ks.add_parser("list")
    for name in ("set", "disable"):
        edit = ks.add_parser(name)
        edit.add_argument("id")
        if name == "set":
            edit.add_argument("keys", nargs="+")
        edit.add_argument("--apply", action="store_true", help="Validate, save and reload; otherwise show a diff")
    config = sub.add_parser("config").add_subparsers(dest="config_command", required=True)
    for name in ("check", "edit", "reload"):
        config.add_parser(name)
    edit = config.add_parser("set")
    edit.add_argument("path", help="Setting path, e.g. ui.pane_gaps")
    edit.add_argument("value", help="TOML value, e.g. false or '\"terminal\"'")
    edit.add_argument("--apply", action="store_true")
    apply = config.add_parser("apply", help="Add the menu/editor shortcuts, preserving current bindings")
    apply.add_argument("--menu-key", default="prefix+space")
    apply.add_argument("--bindings-key", default="prefix+alt+k")
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--yes", action="store_true")
    undo = config.add_parser("undo")
    undo.add_argument("--dry-run", action="store_true")
    undo.add_argument("--yes", action="store_true")
    theme = sub.add_parser("theme").add_subparsers(dest="theme_command", required=True)
    theme.add_parser("sync")
    launch = sub.add_parser("launch")
    launch.add_argument("tool", choices=["git", "editor", "files"])
    install = sub.add_parser("install", help="Link plugin and CLI, then add menu shortcuts")
    install.add_argument("--apply", action="store_true")
    for name in ("update", "remove"):
        maintenance = sub.add_parser(name, help="Preview " + name + "; add --apply to perform it")
        maintenance.add_argument("--apply", action="store_true")
        selection = maintenance.add_mutually_exclusive_group()
        selection.add_argument("--socket", default=argparse.SUPPRESS, help="Explicit Herdr socket")
        selection.add_argument("--session", default=argparse.SUPPRESS, help="Named local session")
        maintenance.add_argument("--config", default=argparse.SUPPRESS, help="Herdr config file")
        if name == "update":
            maintenance.add_argument("--local", action="store_true", help="Refresh this checkout without pulling Git")
    desktop = sub.add_parser("desktop", help="Omarchy controls for the focused Herdr terminal")
    ds = desktop.add_subparsers(dest="desktop_command", required=True)
    for name in ("install", "remove"):
        ds.add_parser(name).add_argument("--apply", action="store_true")
    for name in ("status", "enable", "disable"):
        ds.add_parser(name)
    for name in ("reconcile", "drain"):
        ds.add_parser(name, help=argparse.SUPPRESS).add_argument("--generation", required=name == "drain")
    route = ds.add_parser("route", help=argparse.SUPPRESS)
    route.add_argument("id")
    route.add_argument("--window-pid", type=int, required=True)
    route.add_argument("--client-pid", type=int, required=True)
    route.add_argument("--start", required=True)
    route.add_argument("--address", required=True)
    sub.add_parser("doctor")
    sub.add_parser("logs")
    manager = sub.add_parser("_omarchy", help=argparse.SUPPRESS)
    ms = manager.add_subparsers(dest="omarchy_command", required=True)
    ms.add_parser("start")
    watch = ms.add_parser("watch")
    watch.add_argument("--source", required=True)
    watch.add_argument("--revision", required=True)
    watch.add_argument("--wait", action="store_true")
    ms.add_parser("activate")
    ms.add_parser("deactivate").add_argument("--remove", action="store_true")
    for name in ("_menu", "_tool", "_learn"):
        sub.add_parser(name, help=argparse.SUPPRESS)
    confirm = sub.add_parser("_confirm", help=argparse.SUPPRESS)
    confirm.add_argument("id", choices=["pane-close", "tab-close", "workspace-close", "config-undo"])
    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--after-pid", type=int, required=True)
    worker.add_argument("--job", required=True)
    return p


def reload_for(args):
    client = Client(resolve_socket(args))
    return lambda: client.call("server.reload_config")


def apply_proposal(store, proposal, args, apply=False):
    print(proposal["diff"] or "Already configured.", end="" if proposal["diff"] else "\n")
    if apply:
        return store.apply(proposal, reload_for(args))
    return {"changed": False, "preview": True}


def run_job(context, job):
    context.validate()
    if job["kind"] == "close":
        return execute_close(job["plan"], context)
    if job["kind"] == "action":
        return execute(job["id"], context, yes=job.get("yes", False))
    if job["kind"] == "move-workspace":
        workspace = next((w for w in context.client.snapshot()["workspaces"] if w["workspace_id"] == job["id"]), None)
        if not workspace:
            raise ShellError("The destination workspace no longer exists. Reopen the menu.")
        if workspace["workspace_id"] == context.workspace:
            return {"changed": False}
        return context.client.call("pane.move", pane_id=context.pane, focus=True,
                                   destination={"type": "tab", "tab_id": workspace["active_tab_id"], "split": "right"})
    kind = job["target_kind"]
    if kind not in ("pane", "workspace", "tab"):
        raise ShellError("Invalid selection type.")
    if kind == "pane":
        pane = context.client.call("pane.get", pane_id=job["id"])["pane"]
        if pane["terminal_id"] != job["terminal"]:
            raise ShellError("The selected terminal changed. Reopen the menu.")
    return context.client.call(kind + ".focus", **{kind + "_id": job["id"]})


def defer_job(context, job):
    state_path().mkdir(parents=True, exist_ok=True, mode=0o700)
    executable = Path(__file__).resolve().parents[1] / "bin/herdr-shell"
    # Herdr restores popup focus when its process exits. Dispatch after that exit
    # so navigation is not overwritten and a tool can open the next popup.
    with (state_path() / "actions.log").open("a") as log:
        subprocess.Popen([sys.executable, str(executable), "--context", context.encode(), "_worker",
                          "--after-pid", str(os.getpid()), "--job", json.dumps(job)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                         close_fds=True)


def show_menu(context, page, *, popup=False):
    from .menu import show
    job = show(context, page)
    if job:
        if popup:
            defer_job(context, job)
            return None
        return run_job(context, job)


def install(args, store):
    root = Path(__file__).resolve().parents[1]
    destination = Path.home() / ".local/bin/herdr-shell"
    source = root / "bin/herdr-shell"
    before = store.read()
    reserved_shortcuts = []
    from . import footer_setup
    changes = shortcut_changes(existing=before, reserved=reserved_shortcuts)
    changes += footer_setup.changes(before, root)
    proposal = store.prepare(changes, before)
    print(f"Plugin: {root}\nCLI: {destination}\n" + proposal["diff"])
    if not args.apply:
        return {"preview": True, "reserved_shortcuts": reserved_shortcuts}
    if destination.exists() or destination.is_symlink():
        if not destination.is_symlink() or destination.resolve() != source:
            raise ShellError(f"{destination} already belongs to another installation.")
    store.validator(proposal["after"])
    client = Client(resolve_socket(args))
    plugins = client.call("plugin.list").get("plugins", [])
    previous = next((p for p in plugins if p["plugin_id"] == PLUGIN_ID), None)
    made_helper = False
    link_attempted = False
    try:
        # Select the same server as config reload, including named sessions.
        # The server may commit a link before its response is lost. Inspect its
        # current registration on every attempted link failure before rollback.
        link_attempted = True
        client.call("plugin.link", path=str(root), enabled=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if not destination.is_symlink() or destination.resolve() != source:
                raise ShellError(f"{destination} already belongs to another installation.")
        else:
            destination.symlink_to(source)
            made_helper = True
        result = store.apply(proposal, lambda: client.call("server.reload_config"))
    except Exception as exc:
        failures = []
        if made_helper and destination.is_symlink() and destination.resolve() == source:
            destination.unlink()
        if link_attempted:
            try:
                current = next((p for p in client.call("plugin.list").get("plugins", [])
                                if p["plugin_id"] == PLUGIN_ID), None)
                if current and Path(current["plugin_root"]).resolve() == root:
                    if previous:
                        client.call("plugin.link", path=previous["plugin_root"], enabled=previous.get("enabled", True))
                    else:
                        client.call("plugin.unlink", plugin_id=PLUGIN_ID)
            except (ShellError, OSError, KeyError) as rollback_error:
                failures.append(str(rollback_error))
        if failures:
            raise ShellError(str(exc) + "; plugin setup rollback failed: " + "; ".join(failures)) from exc
        raise
    return {"installed": PLUGIN_ID, "cli": str(destination), "reserved_shortcuts": reserved_shortcuts, **result}


def dispatch(args):
    if args.command == "_omarchy":
        from . import omarchy
        return omarchy.dispatch(args)
    if args.socket and args.session:
        raise ShellError("Choose either --socket or --session, including options before and after the command.")
    if args.config:
        os.environ["HERDR_CONFIG_PATH"] = str(Path(args.config).expanduser().resolve())
    store = ConfigStore()
    if args.command == "footer":
        from .footer import output
        print(output(args.width, store))
        return
    if args.command in ("update", "remove"):
        from . import lifecycle
        return lifecycle.update(args, store, install) if args.command == "update" else lifecycle.remove(args, store)
    if args.command == "desktop":
        from . import desktop
        if args.desktop_command in ("install", "remove"):
            remove = args.desktop_command == "remove"
            proposal = desktop.install_proposal(remove)
            print(proposal["diff"] or "Loader already configured; integration will be refreshed.")
            return desktop.install_desktop(proposal, remove) if args.apply else {"preview": True}
        if args.desktop_command == "status":
            return {"installed": desktop.installed(), "enabled": desktop.enabled(), "bindings": desktop.profile_rows()}
        if args.desktop_command == "reconcile":
            return desktop.reconcile(args.generation)
        if args.desktop_command == "drain":
            return desktop.drain(args.generation)
        if args.desktop_command in ("enable", "disable"):
            return desktop.set_enabled(args.desktop_command == "enable")
        try:
            return desktop.route(args.id, args.window_pid, args.client_pid, args.start, args.address)
        except (ShellError, OSError, ValueError, KeyError) as exc:
            desktop.record_error(args.id, exc)
            raise
    if args.command == "commands":
        rows = catalog_rows(bindings(store.read()))
        if args.json:
            return rows
        for row in rows:
            print(f"{row['id']:28} {row['label']}" + (" — " + row["unavailable"] if row["unavailable"] else ""))
        return
    if args.command == "keybindings":
        rows = bindings(store.read())
        if args.json:
            return {"bindings": rows, "conflicts": conflicts(store.read())}
        if args.key_command == "list":
            for row in rows:
                print(f"{row['id']:32} {' / '.join(row['keys']) or '(disabled)':36} {row['source']}")
            return
        if args.key_command in ("set", "disable"):
            row = next((r for r in rows if r["id"] == args.id), None)
            if not row or not row["path"]:
                raise ShellError("Unknown or legacy binding. Use `keybindings list` for editable IDs.")
            value = "" if args.key_command == "disable" else args.keys[0] if len(args.keys) == 1 else args.keys
            return apply_proposal(store, store.prepare([(row["path"], value)]), args, args.apply)
        context = resolve_context(args)
        return show_menu(context, "keybindings") if args.inline else open_ui(context, "keybindings")
    if args.command == "config":
        if args.config_command == "check":
            store.validate(store.read())
            found = conflicts(store.read())
            if found:
                raise ShellError("Shortcut conflicts: " + json.dumps(found))
            return {"config": str(store.path), "valid": True}
        if args.config_command == "edit":
            return open_ui(resolve_context(args), "settings")
        if args.config_command == "reload":
            return reload_for(args)()
        if args.config_command == "set":
            if not args.path.startswith(("theme.", "ui.", "terminal.")):
                raise ShellError("Use keybindings set for keys. Settings paths must start with theme., ui., or terminal.")
            value = tomllib.loads("value = " + args.value)["value"]
            return apply_proposal(store, store.prepare([(args.path.split("."), value)]), args, args.apply)
        if args.config_command == "apply":
            return apply_proposal(store, store.prepare(shortcut_changes(args.menu_key, args.bindings_key)),
                                  args, args.yes and not args.dry_run)
        if args.config_command == "undo":
            proposal, path = store.undo_proposal()
            print(proposal["diff"])
            return store.undo(proposal, path, reload_for(args)) if args.yes and not args.dry_run else {"preview": True}
    if args.command == "install":
        result = install(args, store)
        if args.apply:
            from . import onboarding
            if onboarding.needs_welcome():
                try:
                    result["welcome"] = open_ui(resolve_context(args), "welcome")
                except ShellError:
                    result["next"] = "Open Herdr Shell to see the welcome panel, or run herdr-shell learn."
        return result
    if args.command == "doctor":
        from . import desktop, omarchy
        report = {"version": VERSION, "herdr": run_herdr(["--version"]).strip(),
                  "config": str(store.path), "state": str(state_path()), "conflicts": conflicts(store.read()),
                  "omarchy_plugin": omarchy.status(),
                  "desktop": {"installed": desktop.installed(), "enabled": desktop.enabled(),
                              "bindings": desktop.effective_bindings(refresh=True)},
                  "tools": {name: shutil.which(name) for name in ("lazygit", "nvim", "yazi", "ranger")}}
        try:
            store.validate(store.read())
            report["config_valid"] = True
            client = Client(resolve_socket(args))
            snapshot = client.snapshot()
            report["server"] = {"version": snapshot["version"], "protocol": snapshot["protocol"]}
            report["plugins"] = client.call("plugin.list")
        except ShellError as exc:
            report["notice"] = str(exc)
        return report
    if args.command == "logs":
        path = state_path() / "actions.log"
        print(path.read_text()[-16000:] if path.exists() else "No background action log yet.")
        return
    if args.command == "_learn":
        # A split plugin invocation captures its launching shell; the trainer
        # acts from its own newly allocated terminal instead.
        args.pane = os.environ.get("HERDR_PANE_ID")
        if not args.pane:
            raise ShellError("The learning game must run in its own Herdr pane.")
    context = resolve_context(args)
    if args.command == "learn":
        return execute({"hands-on": "learn-hands-on", "game": "learn-game"}.get(args.mode, args.mode), context)
    if args.command == "_learn":
        from .trainer import run_game
        return run_game(context)
    if args.command == "_confirm":
        return open_ui(context, "confirm-" + args.id)
    if args.command == "_worker":
        for _ in range(200):
            try:
                os.kill(args.after_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.025)
        else:
            raise ShellError("Menu did not exit; action was not run.")
        # Let the server finish popup teardown before a new popup request.
        time.sleep(.06)
        try:
            return run_job(context, json.loads(args.job))
        except ShellError as exc:
            try:
                context.client.call("notification.show", title="Herdr Shell", body=str(exc))
            except ShellError:
                pass
            raise
    if args.command == "_tool":
        command = json.loads(os.environ.get("HERDR_SHELL_TOOL", "[]"))
        if not command or not all(isinstance(a, str) for a in command):
            raise ShellError("Missing tool command.")
        os.chdir(context.cwd)
        os.execvp(command[0], command)
    if args.command == "_menu":
        from .desktop import menu_instance
        with menu_instance(context):
            return show_menu(context, os.environ.get("HERDR_SHELL_PAGE", "menu"), popup=True)
    if args.command in ("menu", "settings"):
        return show_menu(context, args.command) if args.inline else open_ui(context, args.command)
    if args.command == "action":
        return execute(args.id, context, yes=args.yes)
    if args.command == "theme":
        return execute("theme-terminal", context)
    if args.command == "launch":
        return execute("launch-" + args.tool, context)


def main(argv=None):
    try:
        result = dispatch(parser().parse_args(argv))
        if result is not None:
            print(json.dumps(result, indent=2))
        return 0
    except (ShellError, OSError, ValueError, KeyError) as exc:
        print(f"herdr-shell: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
