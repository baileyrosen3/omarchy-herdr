"""Keyboard-first popup UI using the terminal's own colors."""
import curses
import json
import os
import tomllib

from .actions import CATALOG, catalog_rows, close_plan, rotation_unavailable
from .closing import pane_close_confirmation_enabled, set_pane_close_confirmation
from .config import ConfigStore, MISSING, bindings, get_value
from . import desktop
from . import naming
from . import dialogs
from .interface import (DESCRIPTIONS, HOME, LABELS, SECTIONS, SETTINGS_SECTIONS, MenuView, action_order, fit,
                        group_for, section_for)
from .runtime import ShellError, capability

SETTINGS = [
    ("theme.name", "Theme", "catppuccin"),
    ("ui.pane_gaps", "Gaps between panes", True),
    ("ui.pane_outer_borders", "Outer pane borders", True),
    ("ui.pane_scrollbars", "Pane scrollbars", True),
    ("ui.hide_tab_bar_when_single_tab", "Hide single tab bar", False),
    ("ui.tab_bar_position", "Tab bar position", "top"),
    ("ui.sidebar_width", "Sidebar width", 26),
    ("ui.sidebar_collapsed_mode", "Collapsed sidebar", "compact"),
    ("ui.agent_panel_sort", "Agent ordering", "spaces"),
    ("ui.status_indicators", "Agent status indicators", "dots"),
    ("ui.confirm_close", "Confirm native workspace close", True),
    ("ui.prompt_new_tab_name", "Native tab-name prompt", True),
    ("ui.copy_on_select", "Copy selected text", True),
    ("ui.toast.delivery", "Notification delivery", "off"),
    ("ui.sound.enabled", "Notification sounds", True),
    ("terminal.new_cwd", "New pane directory policy", "follow"),
]

# Native-only numeric spacing. Stock Herdr silently ignores unknown fields, so
# these rows stay hidden unless the native build advertises pane_spacing.
SPACING_SETTINGS = [
    {"path": "ui.pane_gap_horizontal", "label": "Pane gap (columns)", "default": 2,
     "unit": "columns", "minimum": 0, "maximum": 65535},
    {"path": "ui.pane_gap_vertical", "label": "Pane gap (rows)", "default": 1,
     "unit": "rows", "minimum": 0, "maximum": 65535},
    {"path": "ui.tab_padding_horizontal", "label": "Tab edge padding (columns)", "default": 1,
     "unit": "columns", "minimum": 0, "maximum": 65535},
    {"path": "ui.tab_padding_vertical", "label": "Tab edge padding (rows)", "default": 1,
     "unit": "rows", "minimum": 0, "maximum": 65535},
]

SETTING_OPTIONS = {
    "ui.tab_bar_position": [("top", "Top"), ("bottom", "Bottom")],
    "ui.sidebar_collapsed_mode": [("compact", "Compact status rail"), ("hidden", "Hidden")],
    "ui.agent_panel_sort": [("spaces", "Group by workspace"), ("priority", "Attention first")],
    "ui.status_indicators": [("dots", "Colored dots"), ("symbols", "Distinct status symbols")],
    "ui.toast.delivery": [("off", "Off"), ("herdr", "Inside Herdr"), ("terminal", "Through the terminal"), ("system", "System notifications")],
}
SETTING_HELP = {
    "theme.name": "Herdr color theme. Use terminal to follow your terminal's palette, or a built-in theme such as catppuccin, nord, or gruvbox.",
    "ui.pane_gaps": "Keep a gap between split panes instead of shared borders. On a spacing-enabled native build, On uses the numeric gap and tab edge sizes; Off removes both while keeping their values for later.",
    "ui.pane_gap_horizontal": "Blank terminal columns between side-by-side panes when Gaps between panes is On. 0 leaves no blank gap and uses a shared divider if borders are shown. Whole numbers 0 to 65535; default 2.",
    "ui.pane_gap_vertical": "Blank terminal rows between stacked panes when Gaps between panes is On. 0 leaves no blank gap and uses a shared divider if borders are shown. Whole numbers 0 to 65535; default 1.",
    "ui.tab_padding_horizontal": "Blank terminal columns at each left/right edge of the tab's pane area when Gaps between panes is On. 0 adds no edge margin. Independent of pane gaps. Whole numbers 0 to 65535; default 1.",
    "ui.tab_padding_vertical": "Blank terminal rows at each top/bottom edge of the tab's pane area when Gaps between panes is On. 0 adds no edge margin. Independent of pane gaps. Whole numbers 0 to 65535; default 1.",
    "ui.pane_outer_borders": "Draw a border around the outside of the pane area.",
    "ui.pane_scrollbars": "Show interactive scrollbars beside terminal panes.",
    "ui.hide_tab_bar_when_single_tab": "Hide the tab bar in workspaces that have only one tab.",
    "ui.tab_bar_position": "Place the tab bar above or below the panes.",
    "ui.sidebar_width": "Default expanded sidebar width, in terminal columns. Enter a positive whole number.",
    "ui.sidebar_collapsed_mode": "Choose whether a collapsed sidebar keeps a narrow status rail or disappears.",
    "ui.agent_panel_sort": "Group agents by workspace, or bring agents needing attention to the top.",
    "ui.status_indicators": "Symbols make agent states distinguishable without relying on color.",
    "ui.confirm_close": "Ask before closing a workspace with Herdr's native command. Herdr Shell tab and workspace closes ask before stopping running agents, tools, or unknown activity.",
    "ui.prompt_new_tab_name": "Ask for a name with Herdr's built-in New tab shortcut. A name typed there is kept: automatic tab names never replace a manual name.",
    "ui.copy_on_select": "Copy selected terminal text to the clipboard automatically.",
    "ui.toast.delivery": "Choose where Herdr delivers background notifications.",
    "ui.sound.enabled": "Play sounds when agents change state in background workspaces.",
    "terminal.new_cwd": "Use follow, home, current, or a fixed directory. This menu's new panes always use the originating pane's directory.",
    "tab-name-mode": "Agent task (default): each tab follows the task title of the agent running in it, shortened for the tab bar, and keeps it after the agent exits. A tab you rename keeps your name; Name tab from agent task hands it back. Herdr default: tabs show their position.",
    "workspace-name-mode": "Name a new workspace from its Git project root (or the originating directory outside Git). Duplicate projects use the branch when available, then a suffix. Workspace names never change after creation: other tools group and find workspaces by name.",
}

# Settings page organization: each category from SETTINGS_SECTIONS holds display
# groups of related values in stable order. Controls also links to the real
# keybindings and desktop-setup actions instead of duplicating them.
SETTINGS_GROUPS = [
    ("layout", "Pane spacing", [
        "ui.pane_gaps", "ui.pane_gap_horizontal", "ui.pane_gap_vertical",
        "ui.tab_padding_horizontal", "ui.tab_padding_vertical",
        "ui.pane_outer_borders", "ui.pane_scrollbars"]),
    ("layout", "Tabs", ["ui.tab_bar_position", "ui.hide_tab_bar_when_single_tab"]),
    ("layout", "Sidebar", ["ui.sidebar_width", "ui.sidebar_collapsed_mode"]),
    ("appearance", "Theme", ["theme.name"]),
    ("appearance", "Agent presentation", ["ui.status_indicators", "ui.agent_panel_sort"]),
    ("behavior", "Confirmations", ["confirm-pane-close", "ui.confirm_close"]),
    ("behavior", "Names", ["tab-name-mode", "workspace-name-mode"]),
    ("behavior", "New panes", ["ui.prompt_new_tab_name", "terminal.new_cwd"]),
    ("behavior", "Clipboard", ["ui.copy_on_select"]),
    ("notifications", "Delivery & sound", ["ui.toast.delivery", "ui.sound.enabled"]),
    ("controls", "Desktop profile", ["desktop-enabled"]),
    ("controls", "Shortcuts", ["keybindings", "desktop-setup"]),
]


def setting_label(value):
    return "On" if value is True else "Off" if value is False else str(value)


def pane_spacing_supported():
    """Numeric pane spacing exists only with an explicit native capability."""
    return capability("pane_spacing") == 1


def settings_catalog(pane_spacing):
    """Every setting entry with its default, adding native numeric spacing when supported."""
    rows = [{"path": path, "label": label, "default": fallback} for path, label, fallback in SETTINGS]
    if pane_spacing:
        index = next(i for i, row in enumerate(rows) if row["path"] == "ui.pane_gaps") + 1
        rows[index:index] = [dict(entry) for entry in SPACING_SETTINGS]
    return rows


class Menu:
    def __init__(self, screen, context, page="menu"):
        self.screen = screen
        self.context = context
        self.page = page
        self.query = ""
        self.selected = 0
        self.store = ConfigStore()
        self.notice = ""
        self.practice = os.environ.get("HERDR_SHELL_GAME_MENU") == "1"
        self.base = ""
        self.items = []
        self.section = "home"
        self.settings_section = "layout"
        self.scroll = 0
        self.visible_rows = 1
        self.location = "Current workspace"
        self.controls_enabled = False
        self.controls_ready = 0
        self.controls_conflicts = 0
        self.show_unassigned = False
        self.history = []
        self.search_origin = None
        self.screen.keypad(True)
        # Ctrl+Z is a menu shortcut here, not terminal job-control suspend.
        curses.raw()
        curses.curs_set(0)
        curses.set_escdelay(30)
        self.accent = curses.A_BOLD
        self.selection = curses.A_REVERSE
        self.good = curses.A_BOLD
        self.brand = curses.A_BOLD
        self.error = curses.A_BOLD
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            self.accent |= curses.color_pair(1)
            if curses.COLORS >= 8 and curses.COLOR_PAIRS > 3:
                curses.init_pair(2, curses.COLOR_GREEN, -1)
                curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)
                self.good |= curses.color_pair(2)
                self.selection = curses.color_pair(3) | curses.A_BOLD
            if curses.COLORS >= 8 and curses.COLOR_PAIRS > 4:
                curses.init_pair(4, curses.COLOR_YELLOW, -1)
                self.brand |= curses.color_pair(4)
            if curses.COLORS >= 8 and curses.COLOR_PAIRS > 5:
                curses.init_pair(5, curses.COLOR_RED, -1)
                self.error |= curses.color_pair(5)
        try:
            self.load(preserve=False)
        except (ShellError, OSError, ValueError) as exc:
            self.show_error(exc)

    def write(self, y, x, text, attr=0):
        height, width = self.screen.getmaxyx()
        if y >= height or x >= width or y < 0:
            return
        try:
            self.screen.addstr(y, x, fit(text, max(0, width - x - 1)), attr)
        except curses.error:
            pass

    def view_state(self):
        rows = self.filtered()
        selected_id = rows[min(self.selected, len(rows) - 1)]["id"] if rows else None
        return {"page": self.page, "section": self.section, "settings_section": self.settings_section,
                "query": self.query, "selected_id": selected_id,
                "selected": self.selected, "scroll": self.scroll, "search_origin": self.search_origin}

    def restore_view(self, state):
        self.settings_section = state["settings_section"]
        self.query = state["query"]
        self.search_origin = state["search_origin"]
        rows = self.filtered()
        self.selected = next((i for i, row in enumerate(rows) if row["id"] == state["selected_id"]),
                             min(state["selected"], max(0, len(rows) - 1)))
        self.scroll = state["scroll"]

    def load(self, preserve=True):
        state = self.view_state() if preserve else None
        self.base = self.store.read()
        if not preserve:
            self.query, self.selected, self.scroll, self.search_origin = "", 0, 0, None
        self.controls_enabled = desktop.installed() and desktop.enabled()
        profile = [dict(row) for row in desktop.profile_rows()]
        direct_rows = [row for row in profile if row["kind"] == "desktop-binding" and row.get("action_id")]
        self.controls_ready = sum(bool(row.get("active")) for row in direct_rows)
        self.controls_conflicts = sum(row.get("status") == "conflict" for row in direct_rows)
        snapshot = self.context.client.snapshot()
        workspace = next((r for r in snapshot["workspaces"] if r["workspace_id"] == self.context.workspace), {})
        tab = next((r for r in snapshot["tabs"] if r["tab_id"] == self.context.tab), {})
        self.location = (workspace.get("label") or self.context.workspace) + "  ·  Tab " + (tab.get("label") or self.context.tab)
        key_rows = bindings(self.base)
        for row in key_rows:
            row["keys"] = [key for key in row["keys"] if key.strip()]
        prefix = next((r["keys"][0] for r in key_rows if r["id"] == "prefix" and r["keys"]), "ctrl+space")
        if self.page == "keybindings":
            self.items = profile
            for row in self.items:
                if row["kind"] == "desktop-binding":
                    if row.get("scope") == "Desktop":
                        row["label"] += " (desktop)"
                        row["description"] = "Preserves its existing desktop role, including while Herdr is focused."
                        continue
                    status = row.get("status", "unavailable")
                    row["label"] += " (profile · " + status + ")"
                    row["description"] = "Read-only Super+Alt shortcut. " + (row.get("reason") or (
                        "Active only while Herdr is focused." if row.get("active") else
                        "This shortcut is currently unavailable."))
            for r in key_rows:
                scopes = set("Herdr prefix" if k.startswith("prefix+") else
                             "Herdr navigation" if r.get("mode") == "navigate" else "Herdr direct" for k in r["keys"])
                self.items.append(dict(r, detail=" / ".join(desktop.pretty_key(k, prefix) for k in r["keys"]) or "Unassigned",
                                       description="Edit this shortcut, review the change, then save. Leave it blank to disable it." if r.get("path") else
                                       "Legacy indexed binding. Edit this entry in config.toml.",
                                       scope=" / ".join(sorted(scopes)) or "Herdr", kind="binding"))
        elif self.page == "settings":
            doc = tomllib.loads(self.base)
            entries = {entry["path"]: entry for entry in settings_catalog(pane_spacing_supported())}
            self.items = []
            preferences = naming.preferences()
            for section, group, ids in SETTINGS_GROUPS:
                for row_id in ids:
                    entry = entries.pop(row_id, None)
                    if entry is not None:
                        path = entry["path"]
                        value = get_value(doc, path.split("."))
                        current = entry["default"] if value == MISSING else value
                        detail = setting_label(current)
                        row = {"id": path, "label": entry["label"], "path": path.split("."),
                               "value": current, "detail": detail,
                               "description": SETTING_HELP[path], "default": entry["default"],
                               "source": "default" if value == MISSING else "user", "kind": "setting",
                               "section": section, "group": group}
                        row.update({key: entry[key] for key in ("unit", "minimum", "maximum") if key in entry})
                        self.items.append(row)
                    elif row_id in ("tab-name-mode", "workspace-name-mode"):
                        key, label = {"tab-name-mode": ("tab_mode", "Tab names"),
                                      "workspace-name-mode": ("workspace_mode", "New workspace names")}[row_id]
                        value = preferences[key]
                        self.items.append({"id": row_id, "label": label,
                                           "detail": naming.LABELS[key][value],
                                           "value": value, "setting": key,
                                           "description": SETTING_HELP[row_id], "kind": "naming-setting",
                                           "section": section, "group": group})
                    elif row_id == "confirm-pane-close":
                        self.items.append({"id": row_id, "label": "Confirm pane close",
                                           "detail": setting_label(pane_close_confirmation_enabled()),
                                           "description": "On (default): ask before closing a pane with running or unknown activity. Off: close immediately, stopping any agent or command. Enter saves immediately; tab and workspace confirmations are unchanged.",
                                           "kind": "pane-close-toggle", "section": section, "group": group})
                    elif row_id == "desktop-enabled" and profile:
                        self.items.append(dict(profile[0], section=section, group=group))
                    elif row_id in ("keybindings", "desktop-setup"):
                        self.items.append({"id": row_id, "label": LABELS[row_id], "detail": "",
                                           "description": DESCRIPTIONS[row_id], "kind": "action",
                                           "section": section, "group": group})
        elif self.page.endswith("-picker"):
            moving = self.page == "pane-workspace-picker"
            kind = "workspace" if moving else self.page.removesuffix("-picker")
            rows = snapshot[kind + "s"]
            if kind == "tab":
                rows = [r for r in rows if r["workspace_id"] == self.context.workspace]
            if moving:
                rows = [r for r in rows if r["workspace_id"] != self.context.workspace]
            self.items = []
            for row in rows:
                if kind == "workspace":
                    detail = f"{row.get('tab_count', 0)} tabs · {row.get('pane_count', 0)} panes"
                elif kind == "tab":
                    count = sum(p["tab_id"] == row["tab_id"] for p in snapshot["panes"])
                    detail = f"{count} panes"
                else:
                    detail = row.get("agent_status") or ""
                current_id = {"workspace": self.context.workspace, "tab": self.context.tab, "pane": self.context.pane}[kind]
                if row[kind + "_id"] == current_id and not moving:
                    detail = "Current · " + detail
                label = row.get("label") or row.get("title") or row[kind + "_id"]
                if kind == "pane":
                    owner = next((w for w in snapshot["workspaces"] if w["workspace_id"] == row["workspace_id"]), {})
                    label += " · " + (owner.get("label") or row["workspace_id"]) + " / " + row["tab_id"]
                self.items.append({"id": row[kind + "_id"], "label": label,
                                   "detail": detail, "kind": "move-target" if moving else "target",
                                   "description": row.get("foreground_cwd") or row.get("cwd") or "",
                                   "target_kind": kind, "terminal": row.get("terminal_id")})
        else:
            self.items = []
            direct_keys = {}
            direct_unavailable = {}
            profile_actions = {row["action_id"]: row for row in direct_rows}
            proposed_keys = {action: key for key, action, _ in desktop.MAPPINGS}
            for row in direct_rows:
                if row.get("active"):
                    direct_keys.setdefault(row["action_id"], []).extend(row["keys"])
                elif row.get("status") in ("conflict", "unavailable"):
                    direct_unavailable[row["action_id"]] = row.get("reason") or "The direct shortcut is currently unavailable."
            for row in catalog_rows(key_rows):
                action = row["id"]
                if action == "pane-rotate":
                    row["unavailable"] = rotation_unavailable(self.context)
                keys = direct_keys.get(action, [])
                if not keys:
                    keys = row["keys"]
                description = DESCRIPTIONS.get(action) or row["description"]
                if not description and action.startswith("pane-split-"):
                    description = "Create a shell pane " + action.removeprefix("pane-split-") + " in this tab and directory."
                if not description and action.startswith("pane-resize-"):
                    description = "Adjust the focused pane's size toward the " + action.removeprefix("pane-resize-") + "."
                if action in direct_unavailable and action not in direct_keys:
                    description += " Direct shortcut: " + direct_unavailable[action]
                item = dict(row, label=LABELS.get(action, row["label"]), keywords=row["label"],
                            keys=keys, detail=" / ".join(desktop.pretty_key(k, prefix) for k in keys),
                            description=description, kind="action", section=section_for(row),
                            group=group_for(row), scope="Herdr focused" if action in direct_keys else "Herdr")
                if action in proposed_keys:
                    direct = profile_actions.get(action, {})
                    full_key = desktop.pretty_key(proposed_keys[action])
                    item.update(shortcut_key=full_key,
                                shortcut_active=bool(direct.get("active")),
                                shortcut_status=direct.get("status", "unavailable"),
                                shortcut_reason=direct.get("reason", ""))
                self.items.append(item)
            toggle = profile[0]
            self.items.append(dict(toggle, section=section_for(toggle), group=group_for(toggle),
                                   description=DESCRIPTIONS["desktop-enabled"]))
            self.items.sort(key=action_order)
        for row in self.items:
            if row["id"] == "desktop-enabled":
                row["description"] = DESCRIPTIONS["desktop-enabled"]
                row["detail"] = "On" if self.controls_enabled else "Off" if desktop.installed() else "Set up"
        if state:
            self.restore_view(state)

    def filtered(self):
        words = self.query.lower().split()
        rows = [r for r in self.items if (words or self.show_unassigned or r["kind"] != "binding" or any(r["keys"])) and
                all(word in " ".join(str(r.get(k, "")) for k in
                ("label", "detail", "id", "category", "source", "scope", "description", "keywords", "section",
                 "group")).lower() for word in words)]
        if self.page == "menu" and not words:
            if self.section == "home":
                home = []
                for row in (r for r in rows if r["id"] in HOME):
                    row = dict(row, detail=row["shortcut_key"], keys=[row["shortcut_key"]])
                    if not row["shortcut_active"]:
                        row["shortcut_inactive"] = True
                        row["description"] += " Shortcut " + row["shortcut_status"] + ": " + (row["shortcut_reason"] or "Enable the desktop profile in Settings → Controls.")
                        row["description"] += " Enter can still run an available menu action."
                    home.append(row)
                return home
            if self.section == "all":
                return rows
            return [r for r in rows if r.get("section") == self.section]
        if self.page == "settings" and not words:
            return [r for r in rows if r.get("section") == self.settings_section]
        return rows

    def draw(self, rows):
        MenuView(self).render(rows)

    def change_section(self, direction):
        index = next(i for i, section in enumerate(SECTIONS) if section[0] == self.section)
        self.section = SECTIONS[(index + direction) % len(SECTIONS)][0]
        self.page = "menu"
        self.history.clear()
        self.load(preserve=False)

    def change_settings_section(self, direction):
        """Cycle categories in place: the settings page and parent history stay intact."""
        index = next(i for i, section in enumerate(SETTINGS_SECTIONS) if section[0] == self.settings_section)
        self.settings_section = SETTINGS_SECTIONS[(index + direction) % len(SETTINGS_SECTIONS)][0]
        if not self.query:
            self.load(preserve=False)

    def open_page(self, page):
        if self.page == page:
            return
        self.history.append(self.view_state())
        self.page = page
        self.load(preserve=False)

    def back(self):
        if self.history:
            state = self.history.pop()
            self.page, self.section = state["page"], state["section"]
            self.load(preserve=False)
            self.restore_view(state)
        else:
            self.page, self.section = "menu", "home"
            self.load(preserve=False)

    def set_query(self, value):
        if value and not self.query:
            rows = self.filtered()
            origin = rows[min(self.selected, len(rows) - 1)]["id"] if rows else None
            self.search_origin = (origin, self.scroll, self.settings_section)
        self.query, self.selected, self.scroll = value, 0, 0
        if not value and self.search_origin:
            origin, scroll, settings_section = self.search_origin
            if self.page == "settings":
                self.settings_section = settings_section
            rows = self.filtered()
            self.selected = next((i for i, row in enumerate(rows) if row["id"] == origin), 0)
            self.scroll = min(scroll, self.selected)
            self.search_origin = None

    def show_error(self, exc):
        self.notice = "Could not complete the action. Ctrl+R refreshes."
        dialogs.document(self, "Could not complete the action", str(exc).splitlines())

    def busy(self, label):
        self.notice = label
        self.draw(self.filtered())

    def review(self, proposal):
        if not proposal.get("changes", proposal.get("diff")):
            self.notice = "Already configured."
            return False
        lines = ["Preview only. Enter saves and reloads; Esc returns without saving.", ""]
        for change in proposal.get("changes", []):
            before = "Inherited default" if change["before"] == MISSING else json.dumps(change["before"], ensure_ascii=False)
            after = "Inherited default" if change["after"] == MISSING else json.dumps(change["after"], ensure_ascii=False)
            lines += [".".join(change["path"]), "  " + before + " → " + after, ""]
        lines += proposal["diff"].splitlines()
        return dialogs.document(self, "Review configuration change", lines, apply=True)

    def reload_config(self):
        return self.context.client.call("server.reload_config")

    def edit(self, row):
        if not row.get("path"):
            dialogs.document(self, "Read-only binding", [row["label"], row["detail"], "", row["description"], str(self.store.path)])
            return
        binding = row["kind"] == "binding"
        value = (row["keys"][0] if len(row["keys"]) == 1 else row["keys"] or "") if binding else row["value"]
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        options = [] if binding else SETTING_OPTIONS.get(row["id"], [])
        if not binding and isinstance(value, bool):
            options = [(True, "On"), (False, "Off")]
        reset = not binding or row["source"] != "custom"
        if options:
            options = list(options)
            if value not in [v for v, _ in options]:
                options.insert(0, (value, setting_label(value) + " (current)"))
            options.append((dialogs.RESET, "Use inherited default (" + setting_label(row["default"]) + ")"))
        choice, error = value, ""
        while True:
            if options:
                selected = next((i for i, (v, _) in enumerate(options) if v == choice), 0)
                choice = dialogs.choose(self, row["label"], options, selected, help_text=row["description"])
                if choice is dialogs.CANCEL:
                    return
                updated = MISSING if choice is dialogs.RESET else choice
            else:
                hint = ('Shortcut, e.g. prefix+z, or TOML array ["prefix+z", "alt+z"]. Blank disables.'
                        if binding else row["description"])
                entered = dialogs.prompt(self, row["label"], raw, help_text=hint, error=error, reset=reset)
                if entered is dialogs.CANCEL:
                    return
                if entered is dialogs.RESET:
                    updated = MISSING
                else:
                    raw = entered
                    try:
                        if binding:
                            updated = tomllib.loads("value = " + raw)["value"] if raw.strip().startswith(('[', '"', "'")) else raw.strip()
                            if not (isinstance(updated, str) or isinstance(updated, list) and all(isinstance(v, str) for v in updated)):
                                raise ValueError("Use one shortcut or an array of quoted shortcuts.")
                        elif isinstance(value, int):
                            minimum, maximum = row.get("minimum", 1), row.get("maximum")
                            outside = (f"Enter a whole number of {row['unit']} from {minimum} to {maximum}."
                                       if row.get("unit") and maximum is not None else None)
                            try:
                                updated = int(raw)
                            except ValueError as exc:
                                raise ValueError(outside or "Enter a positive whole number, such as 26.") from exc
                            if updated < minimum or (maximum is not None and updated > maximum):
                                raise ValueError(outside or "Enter a positive whole number.")
                        else:
                            updated = tomllib.loads("value = " + raw)["value"] if raw.strip().startswith(('"', "'")) else raw.strip()
                            if not isinstance(updated, str) or not updated:
                                raise ValueError("Enter a non-empty value, or use Ctrl+D to restore the default.")
                    except (ValueError, KeyError) as exc:
                        error = str(exc)
                        self.show_error(exc)
                        continue
            try:
                proposal = self.store.prepare([(row["path"], updated)], self.base)
                if not proposal["changes"]:
                    self.notice = "Already configured."
                    return
                if not self.review(proposal):
                    continue
                self.busy("Validating and saving…")
                self.store.apply(proposal, self.reload_config)
            except (ShellError, OSError, ValueError) as exc:
                error = str(exc)
                self.show_error(exc)
                # Keep the user's entry while basing the next preview on the
                # latest file, including a concurrent external edit or rollback.
                self.base = self.store.read()
                continue
            self.load()
            self.notice = "Saved and reloaded. Ctrl+Z undoes this change."
            return

    def edit_naming(self, row):
        options = naming.OPTIONS[row["setting"]]
        selected = next(i for i, (value, _) in enumerate(options) if value == row["value"])
        value = dialogs.choose(self, row["label"], options, selected,
                               help_text=row["description"],
                               footer="↑↓ Select  Enter Save  Esc Cancel")
        if value is dialogs.CANCEL:
            return
        changed = naming.set_preference(row["setting"], value)
        self.load()
        self.notice = row["label"] + (" saved: " + naming.LABELS[row["setting"]][value]
                                      if changed else " already uses that value.")


    def undo(self):
        proposal, path = self.store.undo_proposal()
        if self.review(proposal):
            self.busy("Restoring the previous values…")
            self.store.undo(proposal, path, self.reload_config)
            self.notice = "Undone and reloaded."
            self.load()

    def activate(self, row):
        if getattr(self, "practice", False):
            self.help()
            self.notice = "Practice menu: browse shortcuts and help. Super+Alt+M closes it."
            return None
        self.notice = ""
        if row["kind"] == "desktop-toggle":
            if not desktop.installed():
                self.setup_desktop()
            else:
                desktop.set_enabled(not desktop.enabled())
                self.load()
                self.notice = "Super+Alt profile enabled. Conflicting shortcuts stay reserved." if self.controls_enabled else "Super+Alt profile disabled."
            return
        if row["kind"] == "pane-close-toggle":
            enabled = not pane_close_confirmation_enabled()
            set_pane_close_confirmation(enabled)
            self.load()
            self.notice = "Pane close confirmation enabled." if enabled else "Pane closes immediately, including running agents and commands."
            return
        if row["kind"] == "desktop-binding":
            dialogs.document(self, "Desktop shortcut · read only", [row["label"], row["detail"], "", row["description"], "",
                "Use Omarchy controls in Settings → Controls to enable or disable the Herdr profile."])
            return
        if row["kind"] == "naming-setting":
            self.edit_naming(row)
            return
        if row["kind"] in ("binding", "setting"):
            self.edit(row)
            return
        if row["kind"] == "target":
            return {"kind": "target", "target_kind": row["target_kind"], "id": row["id"],
                    "terminal": row.get("terminal")}
        if row["kind"] == "move-target":
            return {"kind": "move-workspace", "id": row["id"]}
        if row.get("unavailable"):
            raise ShellError(row["unavailable"])
        action = row["id"]
        if action == "menu":
            return {"kind": "dismiss"}
        if action in ("welcome", "walkthrough"):
            return self.learning(action)
        if action in ("keybindings", "settings", "workspace-picker", "tab-picker", "pane-picker", "pane-workspace-picker"):
            self.open_page(action)
        elif action == "desktop-setup":
            self.setup_desktop()
        elif action == "config-undo":
            self.undo()
        elif action == "config-reload":
            self.busy("Reloading configuration…")
            self.reload_config()
            self.load()
            self.notice = "Configuration reloaded."
        elif action == "theme-terminal":
            proposal = self.store.prepare([(["theme", "name"], "terminal")], self.base)
            if self.review(proposal):
                self.busy("Applying terminal colors…")
                self.store.apply(proposal, self.reload_config)
                self.notice = "Terminal palette selected."
                self.load()
        elif action in ("pane-close", "tab-close", "workspace-close"):
            return self.close(close_plan(action, self.context))
        else:
            return {"kind": "action", "id": action, "yes": CATALOG[action].confirm}

    def learning(self, page):
        from . import onboarding
        if page == "welcome":
            choice = onboarding.welcome(self)
        else:
            choice = "walkthrough"
        while choice == "walkthrough":
            choice = onboarding.walkthrough(self)
        if choice in ("hands-on", "game"):
            action = {"hands-on": "learn-hands-on", "game": "learn-game"}[choice]
            return {"kind": "action", "id": action, "yes": False}
        return {"kind": "dismiss"} if choice is None else None

    def setup_desktop(self):
        proposal = desktop.install_proposal()
        if desktop.installed() or self.review(proposal):
            self.busy("Setting up focused Super+Alt shortcuts…")
            desktop.install_desktop(proposal)
            self.notice = "Omarchy controls enabled for the focused Herdr terminal."
            self.load()

    def close(self, plan):
        if plan["action_id"] == "pane-close" and not pane_close_confirmation_enabled():
            plan["confirmed"] = True
        elif plan["needs_confirmation"] and not self.confirm_close(plan):
            return None
        return {"kind": "close", "plan": plan}

    def confirm_close(self, plan):
        """Keep the target and all affected activity readable before closing."""
        count = plan["count"]
        kind = plan["kind"]
        lines = [f"Closing this {kind} stops {count} pane" + ("." if count == 1 else "s."),
                 "Running or uncertain activity:", ""]
        for activity in plan["activity"]:
            lines.append(activity.get("label") or activity["pane_id"])
            lines.append("  " + activity["reason"])
        lines += ["", "Directory: " + self.context.cwd]
        selected, offset = 0, 0
        while True:
            frame = dialogs.Frame(self, plan["title"], height=32, width=110)
            if not frame.small:
                wrapped = [part for line in lines for part in dialogs.wrap_cells(line, frame.inner)]
                visible = max(1, frame.height - 12)
                offset = max(0, min(offset, max(0, len(wrapped) - visible)))
                for y, line in enumerate(wrapped[offset:offset + visible], 5):
                    frame.text(y, line)
                options = [f"Cancel — keep the {kind}", f"Close {kind} and its " + ("process" if kind == "pane" else "panes")]
                for index, label in enumerate(options):
                    frame.text(frame.height - 6 + index, fit(("› " if index == selected else "  ") + label, frame.inner, True),
                               self.selection if index == selected else curses.A_NORMAL)
                if len(wrapped) > visible:
                    frame.text(frame.height - 4, f"{offset + 1}–{min(len(wrapped), offset + visible)} of {len(wrapped)} lines · PgUp / PgDn scroll", curses.A_DIM)
                frame.footer("↑↓ Select   Enter Confirm   Esc Cancel")
            key = frame.key()
            if key in dialogs.ESCAPE:
                return False
            if frame.small or key == curses.KEY_RESIZE:
                continue
            if key in dialogs.ENTER:
                return selected == 1
            if key in (curses.KEY_UP, curses.KEY_DOWN, "\t", curses.KEY_BTAB):
                selected = 1 - selected
            elif key == curses.KEY_NPAGE:
                offset += visible
            elif key == curses.KEY_PPAGE:
                offset -= visible
            elif key == curses.KEY_HOME:
                offset = 0
            elif key == curses.KEY_END:
                offset = len(wrapped)

    def help(self):
        rows = self.filtered()
        row = rows[min(self.selected, len(rows) - 1)] if rows else {}
        details = ["SELECTED ITEM", row.get("label", "No item selected"), row.get("detail", ""),
                   row.get("unavailable") or row.get("description", ""), row.get("scope", ""),
                   "Source: " + row["source"] if row.get("source") else "",
                   "Directory: " + self.context.cwd, ""]
        dialogs.document(self, "Herdr × Omarchy · Menu help", details + [
            "MOVE AND SEARCH", "↑ / ↓ select an item. Enter uses the action named in the footer.",
            "Tab / Shift+Tab or ← / → switch sections on the main menu, and categories in Settings.",
            "Type to search. Search covers every main section, and every Settings category.",
            "Esc clears search, then returns to the previous page or Home, then closes.",
            "PgUp / PgDn scroll lists and documents. Home / End jump to the ends.", "",
            "PAGES", "F2 opens Keybindings. Ctrl+O (or F3) opens Settings.",
            "Ctrl+R refreshes without losing your search or selection.",
            "Ctrl+Z (or F4) reviews undo for the last managed configuration change.", "",
            "KEYBINDINGS", "Super+Alt profile rows are read only and show live availability. Native Herdr rows are editable.",
            "Ctrl+U shows or hides unassigned keys. Search always includes them.",
            "Enter a shortcut such as prefix+z; use a TOML array for alternatives.",
            "An empty value disables the shortcut. Ctrl+D restores native key defaults.",
            "Custom commands keep their own shortcut entry and have no inherited default.", "",
            "EDITING", "Use ← / →, Home / End, Backspace or Delete to edit text.",
            "Ctrl+U clears input. Ctrl+W deletes a word. Ctrl+K deletes to the end.",
            "Native settings and key edits preview changes; Enter again saves and reloads. Toggles save immediately. Esc goes back.",
            "Invalid entries remain in the editor so you can correct them.", "",
            "FOCUSED CONTROLS", "The Super+Alt profile affects Herdr only while its terminal is focused.",
            "Existing desktop shortcuts remain reserved, including while Herdr is focused.",
            "Idle shells close directly. Running or unknown activity asks first by default; Settings can turn off pane close confirmation.",
            "Directional focus stays local. Pane cycles continue through tabs and workspaces; tab cycles continue through workspaces.",
            "Actions use the pane and directory from which this menu was opened.",
            "Ctrl+C closes the menu; inside a dialog it cancels the dialog.",
        ])

    def run(self):
        from . import onboarding
        practice = getattr(self, "practice", False)
        if practice:
            self.notice = ("Speed Run: repeat the shortcut to close this demo."
                           if os.environ.get("HERDR_SHELL_GAME_MENU_MODE") == "speed" else
                           "Practice menu: F4 Demo close · Super+Alt+M closes it.")
        if not practice and (self.page in ("welcome", "walkthrough") or (self.page == "menu" and onboarding.needs_welcome())):
            job = self.learning("welcome" if self.page == "menu" else self.page)
            if job:
                return None if job["kind"] == "dismiss" else job
            self.page = "menu"
            self.load(preserve=False)
        if self.page == "desktop-setup":
            self.page = "settings"
            try:
                self.load(preserve=False)
                self.setup_desktop()
            except (ShellError, OSError) as exc:
                self.show_error(exc)
        if self.page in ("confirm-pane-close", "confirm-tab-close", "confirm-workspace-close"):
            try:
                saved = os.environ.get("HERDR_SHELL_CLOSE_PLAN")
                plan = json.loads(saved) if saved else close_plan(self.page.removeprefix("confirm-"), self.context)
                if plan["action_id"] != self.page.removeprefix("confirm-"):
                    raise ShellError("The close confirmation does not match this action. Reopen it from the originating pane.")
                return self.close(plan)
            except (ShellError, OSError, ValueError, KeyError, TypeError) as exc:
                self.show_error(exc)
                return None
        if self.page == "confirm-config-undo":
            try:
                self.undo()
            except ShellError as exc:
                self.show_error(exc)
            self.page = "menu"
            self.load(preserve=False)
        while True:
            rows = self.filtered()
            self.draw(rows)
            key = self.screen.get_wch()
            try:
                if key == "\x03":
                    return None
                if MenuView(self).too_small:
                    if key == "\x1b":
                        return None
                    continue
                if key == curses.KEY_RESIZE:
                    continue
                if key == "\x1b":
                    if self.query:
                        self.set_query("")
                    elif self.page != "menu" or self.history:
                        self.back()
                    elif self.section != "home":
                        self.section = "home"
                        self.load(preserve=False)
                    else:
                        return None
                elif key in ("\t", curses.KEY_RIGHT, curses.KEY_LEFT, curses.KEY_BTAB) and self.page == "menu":
                    self.notice = ""
                    self.change_section(-1 if key in (curses.KEY_LEFT, curses.KEY_BTAB) else 1)
                elif key in ("\t", curses.KEY_RIGHT, curses.KEY_LEFT, curses.KEY_BTAB) and self.page == "settings":
                    self.notice = ""
                    self.change_settings_section(-1 if key in (curses.KEY_LEFT, curses.KEY_BTAB) else 1)
                elif key == curses.KEY_F1:
                    self.help()
                elif key in (curses.KEY_F2, curses.KEY_F3, "\x0f"):
                    self.notice = ""
                    self.open_page("keybindings" if key == curses.KEY_F2 else "settings")
                elif key in (curses.KEY_F4, "\x1a"):
                    if practice and key == curses.KEY_F4 and os.environ.get("HERDR_SHELL_GAME_MENU_MODE") != "speed":
                        from .game_input import route
                        result = route("menu", self.context)
                        if result and result.get("game") == "queued":
                            self.notice = "Closing the practice menu…"
                        elif result and result.get("game") == "busy":
                            self.notice = "Still verifying the open. Press F4 again when ready."
                        else:
                            self.notice = "Demo unavailable. Esc returns to the guide; F2 retries."
                    elif practice:
                        self.notice = "Practice menu: configuration changes are disabled."
                    else:
                        self.undo()
                elif key == "\x15" and self.page == "keybindings":
                    state = self.view_state()
                    self.show_unassigned = not self.show_unassigned
                    self.restore_view(state)
                elif key == "\x15":
                    self.set_query("")
                elif key == "\x12":
                    self.busy("Refreshing…")
                    self.load()
                    self.notice = "Refreshed."
                elif key in (curses.KEY_DOWN, "\x0e"):
                    self.selected = max(0, min(len(rows) - 1, self.selected + 1))
                elif key in (curses.KEY_UP, "\x10"):
                    self.selected = max(0, self.selected - 1)
                elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                    step = self.visible_rows if key == curses.KEY_NPAGE else -self.visible_rows
                    self.selected = max(0, min(len(rows) - 1, self.selected + step))
                elif key in (curses.KEY_HOME, curses.KEY_END):
                    self.selected = 0 if key == curses.KEY_HOME else max(0, len(rows) - 1)
                elif key in ("\n", "\r", curses.KEY_ENTER) and rows:
                    result = self.activate(rows[self.selected])
                    if result:
                        return None if result["kind"] == "dismiss" else result
                elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    self.set_query(self.query[:-1])
                elif isinstance(key, str) and key.isprintable():
                    if key != "/" or self.query:
                        self.set_query(self.query + key)
            except (ShellError, OSError, ValueError) as exc:
                self.show_error(exc)


def show(context, page="menu"):
    return curses.wrapper(lambda screen: Menu(screen, context, page).run())
