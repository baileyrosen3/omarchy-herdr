"""The same action catalog powers manifests, keyboard hints, menus and the CLI."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shlex
import shutil

from . import PLUGIN_ID, VERSION
from .runtime import ShellError


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    category: str
    key: str = ""
    description: str = ""
    confirm: bool = False


NAVIGATION_FALLBACKS = {
    "left": "tab-previous",
    "right": "tab-next",
    "up": "workspace-previous",
    "down": "workspace-next",
}


ACTIONS = [
    Action("menu", "Open Herdr Shell", "Configure", description="Search all actions"),
    Action("keybindings", "Edit keybindings", "Configure", description="Search, change, disable and undo shortcuts"),
    Action("settings", "Edit settings", "Configure", description="Appearance, pane behavior and notifications"),
    Action("desktop-setup", "Set up Omarchy controls", "Configure", description="Preview focused Super-key controls and enable them"),
    Action("workspace-picker", "Switch workspace", "Navigate", "workspace_picker"),
    Action("tab-picker", "Switch tab", "Navigate"),
    Action("pane-picker", "Switch pane", "Navigate"),
    Action("pane-workspace-picker", "Move pane to workspace", "Arrange", description="Move this running pane into another workspace"),
    Action("agent-next-waiting", "Next waiting agent", "Navigate", description="Focus an agent that needs input"),
    Action("workspace-previous", "Previous workspace", "Navigate", "previous_workspace"),
    Action("workspace-next", "Next workspace", "Navigate", "next_workspace"),
    Action("tab-previous", "Previous tab", "Navigate", "previous_tab"),
    Action("tab-next", "Next tab", "Navigate", "next_tab"),
    Action("workspace-new", "New workspace", "Launch", "new_workspace"),
    Action("tab-new", "New tab", "Launch", "new_tab"),
    Action("agent-new", "New agent pane", "Launch", description="Default Omarchy agent in this pane's directory and tab"),
    Action("launch-git", "Open Git", "Launch", description="Lazygit in a regular pane in this project's directory"),
    Action("launch-editor", "Open editor", "Launch", description="Your editor in this project's directory"),
    Action("launch-files", "Open file browser", "Launch", description="Yazi or ranger in this project's directory"),
    Action("pane-split-right", "Split pane right", "Arrange", "split_vertical"),
    Action("pane-split-down", "Split pane below", "Arrange", "split_horizontal"),
    Action("pane-split-up", "Split pane above", "Arrange"),
    Action("pane-split-left", "Split pane left", "Arrange"),
    Action("pane-zoom", "Toggle pane zoom", "Arrange", "zoom"),
    Action("pane-close", "Close pane", "Arrange", "close_pane", "Close the selected pane and its process", True),
    Action("theme-terminal", "Use terminal colors", "Configure", description="Follow the outer terminal's palette"),
    Action("config-reload", "Reload configuration", "Configure", "reload_config"),
    Action("config-undo", "Undo last configuration change", "Configure"),
]
for direction in ("left", "down", "up", "right"):
    ACTIONS.append(Action("pane-focus-" + direction, "Focus pane " + direction, "Navigate", "focus_pane_" + direction))
    ACTIONS.append(Action("pane-resize-" + direction, "Resize pane " + direction, "Arrange", "resize_pane_" + direction))
    ACTIONS.append(Action("pane-swap-" + direction, "Swap pane " + direction, "Arrange",
                          description=f"Exchange this pane's position with its neighbor to the {direction}."))
    kind, step = NAVIGATION_FALLBACKS[direction].split("-")
    ACTIONS.append(Action("navigate-" + direction, "Navigate " + direction, "Navigate",
                          description=f"Focus a pane {direction}; at the edge, switch to the {step} {kind}"))
CATALOG = {a.id: a for a in ACTIONS}


def tool_command(action):
    if action == "launch-git":
        command = ["lazygit"]
    elif action == "launch-editor":
        command = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nvim")
    elif action == "launch-files":
        command = [next((p for p in ("yazi", "ranger") if shutil.which(p)), "yazi")]
    else:
        raise ShellError("Unknown tool.")
    if not command or not shutil.which(command[0]):
        raise ShellError(f"Install {command[0] if command else 'an editor'} to use this action.")
    return command


def available(action):
    if action.id == "agent-new" and not shutil.which("omarchy"):
        return "Install Omarchy to use its default agent launcher."
    if action.id.startswith("launch-"):
        try:
            tool_command(action.id)
        except ShellError as exc:
            return str(exc)
    return ""


def catalog_rows(key_rows=()):
    keys = {r["id"]: r["keys"] for r in key_rows}
    plugin_keys = {r.get("action_id"): r["keys"] for r in key_rows if r.get("action_id")}
    result = []
    for action in ACTIONS:
        row = asdict(action)
        row["keys"] = keys.get(action.key, []) or plugin_keys.get(PLUGIN_ID + "." + action.id, [])
        row["unavailable"] = available(action)
        result.append(row)
    return result


def open_ui(context, page="menu"):
    context.validate()
    require_active(context)
    return context.client.call("plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="menu",
                               placement="popup", width="86%", height="82%",
                               cwd=context.cwd, env={"HERDR_SHELL_CONTEXT": context.encode(),
                                                     "HERDR_SHELL_PAGE": page}, focus=True)


def require_active(context):
    # Herdr 0.9 popups always attach to the active pane and reject explicit
    # target/workspace parameters. Keep our action target in captured context.
    if context.client.snapshot().get("focused_pane_id") != context.pane:
        raise ShellError("Focus the originating pane before opening its popup.")


def execute(action_id, context, *, yes=False):
    if action_id not in CATALOG:
        raise ShellError(f"Unknown action: {action_id}")
    action = CATALOG[action_id]
    if action.confirm and not yes:
        raise ShellError(f"{action.label} requires confirmation. Pass --yes for pane {context.pane}.")
    context.validate()
    client = context.client
    if action_id in ("menu", "keybindings", "settings", "desktop-setup", "workspace-picker", "tab-picker", "pane-picker", "pane-workspace-picker"):
        return open_ui(context, action_id)
    if action_id == "agent-new":
        if not shutil.which("omarchy"):
            raise ShellError("Omarchy's agent launcher is unavailable.")
        # Split plugin panes derive their tab/workspace from the target pane;
        # Herdr rejects workspace_id even when it matches that target.
        return client.call("plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="agent",
                           placement="split", direction="right", target_pane_id=context.pane,
                           cwd=context.cwd, focus=True)
    if action_id.startswith("pane-focus-"):
        return client.call("pane.focus_direction", pane_id=context.pane, direction=action_id.removeprefix("pane-focus-"))
    if action_id.startswith("pane-swap-"):
        return client.call("pane.swap", source_pane_id=context.pane, direction=action_id.removeprefix("pane-swap-"))
    if action_id.startswith("navigate-"):
        direction = action_id.removeprefix("navigate-")
        result = client.call("pane.focus_direction", pane_id=context.pane, direction=direction)
        focus = result["focus"]
        # Fall through only when Herdr confirms there is no neighboring pane.
        # Errors and successful pane moves must never switch tabs/workspaces.
        if focus["changed"] or focus.get("reason") != "no_neighbor":
            return result
        return execute(NAVIGATION_FALLBACKS[direction], context)
    if action_id.startswith("pane-resize-"):
        return client.call("pane.resize", pane_id=context.pane, direction=action_id.removeprefix("pane-resize-"), amount=.05)
    if action_id.startswith("pane-split-"):
        direction = action_id.removeprefix("pane-split-")
        # Herdr's split API accepts right/down. Swap the new sibling with the
        # original pane for left/up, retaining focus on the new terminal.
        split_direction = {"left": "right", "up": "down"}.get(direction, direction)
        result = client.call("pane.split", target_pane_id=context.pane, workspace_id=context.workspace,
                             direction=split_direction, cwd=context.cwd, focus=True)
        if direction in ("left", "up"):
            created = result["pane"]["pane_id"]
            swapped = client.call("pane.swap", source_pane_id=created, target_pane_id=context.pane)
            if not swapped.get("changed"):
                raise ShellError(f"Pane {created} was created {split_direction}, but could not be moved {direction}.")
            client.call("pane.focus", pane_id=created)
        return result
    if action_id == "pane-zoom":
        return client.call("pane.zoom", pane_id=context.pane, mode="toggle")
    if action_id == "pane-close":
        return client.call("pane.close", pane_id=context.pane)
    if action_id == "tab-new":
        return client.call("tab.create", workspace_id=context.workspace, cwd=context.cwd, focus=True)
    if action_id == "workspace-new":
        return client.call("workspace.create", source_workspace_id=context.workspace, cwd=context.cwd, focus=True)
    if action_id in ("workspace-previous", "workspace-next", "tab-previous", "tab-next"):
        kind, direction = action_id.split("-")
        snapshot = client.snapshot()
        rows = snapshot[kind + "s"]
        if kind == "tab":
            rows = [r for r in rows if r["workspace_id"] == context.workspace]
        ids = [r[kind + "_id"] for r in rows]
        current = context.workspace if kind == "workspace" else context.tab
        if current not in ids:
            raise ShellError("The original target no longer exists.")
        selected = ids[(ids.index(current) + (1 if direction == "next" else -1)) % len(ids)]
        return client.call(kind + ".focus", **{kind + "_id": selected})
    if action_id == "agent-next-waiting":
        panes = [p for p in client.snapshot()["panes"] if p.get("agent_status") == "blocked"]
        if not panes:
            raise ShellError("No agents are waiting for input.")
        ids = [p["pane_id"] for p in panes]
        index = (ids.index(context.pane) + 1) % len(ids) if context.pane in ids else 0
        return client.call("pane.focus", pane_id=ids[index])
    if action_id == "launch-git":
        tool_command(action_id)
        return client.call("plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="git",
                           placement="split", direction="right", target_pane_id=context.pane,
                           cwd=context.cwd, focus=True)
    if action_id.startswith("launch-"):
        require_active(context)
        return client.call("plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="tool",
                           placement="popup", width="92%", height="90%", cwd=context.cwd,
                           focus=True,
                           env={"HERDR_SHELL_TOOL": json.dumps(tool_command(action_id)),
                                "HERDR_SHELL_CONTEXT": context.encode()})
    if action_id == "config-reload":
        return client.call("server.reload_config")
    if action_id in ("config-undo", "theme-terminal"):
        from .config import ConfigStore
        store = ConfigStore()
        reload = lambda: client.call("server.reload_config")
        if action_id == "config-undo":
            proposal, path = store.undo_proposal()
            if not yes:
                raise ShellError("Preview config undo with `herdr-shell config undo --dry-run`, then pass --yes.")
            return store.undo(proposal, path, reload)
        return store.apply(store.prepare([(["theme", "name"], "terminal")]), reload)
    raise ShellError("Action has no implementation: " + action_id)


def manifest():
    lines = ['# Generated by scripts/generate-manifest.py; edit the action catalog.',
             f'id = "{PLUGIN_ID}"', 'name = "Herdr Shell"', f'version = "{VERSION}"',
             'min_herdr_version = "0.9.0"', 'platforms = ["linux"]',
             'description = "Search actions, edit keybindings, and manage configuration with undo."']
    for action in ACTIONS:
        # Closing a pane must go through a confirming menu or explicit CLI flag.
        command = ["python3", "bin/herdr-shell", "_confirm", action.id] if action.confirm else [
            "python3", "bin/herdr-shell", "action", "run", action.id]
        if action.id == "config-undo":
            command = ["python3", "bin/herdr-shell", "_confirm", action.id]
        lines.extend(["", "[[actions]]", "id = " + json.dumps(action.id),
                      "title = " + json.dumps(action.label), 'contexts = ["pane"]',
                      "command = " + json.dumps(command)])
    for entrypoint in ("menu", "tool"):
        lines.extend(["", "[[panes]]", f'id = "{entrypoint}"',
                      'title = "Herdr Shell"', 'placement = "popup"',
                      'width = "86%"', 'height = "82%"',
                      'command = ' + json.dumps(["sh", "-c", 'exec python3 "$HERDR_PLUGIN_ROOT/bin/herdr-shell" _' + entrypoint])])
    lines.extend(["", "[[panes]]", 'id = "agent"', 'title = "Agent"', 'placement = "split"',
                  'command = ' + json.dumps(["omarchy", "agent", "--inline", "--pick"])])
    lines.extend(["", "[[panes]]", 'id = "git"', 'title = "Lazygit"', 'placement = "split"',
                  'command = ' + json.dumps(["lazygit"])])
    return "\n".join(lines) + "\n"
