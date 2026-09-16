"""Keyboard-first popup UI using the terminal's own colors."""
import curses
import json
import tomllib

from .actions import CATALOG, catalog_rows
from .config import ConfigStore, MISSING, bindings, get_value
from . import desktop
from . import dialogs
from .interface import DESCRIPTIONS, HOME, LABELS, SECTIONS, MenuView, fit, section_for
from .runtime import ShellError

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
    ("ui.confirm_close", "Confirm workspace close", True),
    ("ui.prompt_new_tab_name", "Ask for new tab name", True),
    ("ui.copy_on_select", "Copy selected text", True),
    ("ui.toast.delivery", "Notification delivery", "off"),
    ("ui.sound.enabled", "Notification sounds", True),
    ("terminal.new_cwd", "New pane directory policy", "follow"),
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
    "ui.pane_gaps": "Keep a gap between split panes instead of sharing borders.",
    "ui.pane_outer_borders": "Draw a border around the outside of the pane area.",
    "ui.pane_scrollbars": "Show interactive scrollbars beside terminal panes.",
    "ui.hide_tab_bar_when_single_tab": "Hide the tab bar in workspaces that have only one tab.",
    "ui.tab_bar_position": "Place the tab bar above or below the panes.",
    "ui.sidebar_width": "Default expanded sidebar width, in terminal columns. Enter a positive whole number.",
    "ui.sidebar_collapsed_mode": "Choose whether a collapsed sidebar keeps a narrow status rail or disappears.",
    "ui.agent_panel_sort": "Group agents by workspace, or bring agents needing attention to the top.",
    "ui.status_indicators": "Symbols make agent states distinguishable without relying on color.",
    "ui.confirm_close": "Ask before closing a workspace. Super+X still closes a pane immediately.",
    "ui.prompt_new_tab_name": "Ask for a name with Herdr's built-in New tab shortcut. This menu creates tabs immediately.",
    "ui.copy_on_select": "Copy selected terminal text to the clipboard automatically.",
    "ui.toast.delivery": "Choose where Herdr delivers background notifications.",
    "ui.sound.enabled": "Play sounds when agents change state in background workspaces.",
    "terminal.new_cwd": "Use follow, home, current, or a fixed directory. This menu's new panes always use the originating pane's directory.",
}


def setting_label(value):
    return "On" if value is True else "Off" if value is False else str(value)


class Menu:
    def __init__(self, screen, context, page="menu"):
        self.screen = screen
        self.context = context
        self.page = page
        self.query = ""
        self.selected = 0
        self.store = ConfigStore()
        self.notice = ""
        self.base = ""
        self.items = []
        self.section = "home"
        self.scroll = 0
        self.visible_rows = 1
        self.location = "Current workspace"
        self.controls_enabled = False
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
        return {"page": self.page, "section": self.section, "query": self.query, "selected_id": selected_id,
                "selected": self.selected, "scroll": self.scroll, "search_origin": self.search_origin}

    def restore_view(self, state):
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
        snapshot = self.context.client.snapshot()
        workspace = next((r for r in snapshot["workspaces"] if r["workspace_id"] == self.context.workspace), {})
        tab = next((r for r in snapshot["tabs"] if r["tab_id"] == self.context.tab), {})
        self.location = (workspace.get("label") or self.context.workspace) + "  ·  Tab " + (tab.get("label") or self.context.tab)
        key_rows = bindings(self.base)
        for row in key_rows:
            row["keys"] = [key for key in row["keys"] if key.strip()]
        prefix = next((r["keys"][0] for r in key_rows if r["id"] == "prefix" and r["keys"]), "ctrl+space")
        if self.page == "keybindings":
            self.items = desktop.profile_rows()
            for row in self.items:
                if row["kind"] == "desktop-binding":
                    row["label"] += " (profile)"
                    row["description"] = ("Read-only desktop shortcut. " +
                        ("This remains a desktop action." if row["scope"] == "Desktop" else
                         "Active only while Herdr is focused." if self.controls_enabled else
                         "Herdr routing is off. Enable Omarchy controls to use this shortcut in Herdr."))
            for r in key_rows:
                scopes = set("Herdr prefix" if k.startswith("prefix+") else
                             "Herdr navigation" if r.get("mode") == "navigate" else "Herdr direct" for k in r["keys"])
                self.items.append(dict(r, detail=" / ".join(desktop.pretty_key(k, prefix) for k in r["keys"]) or "Unassigned",
                                       description="Edit this shortcut, review the change, then save. Leave it blank to disable it." if r.get("path") else
                                       "Legacy indexed binding. Edit this entry in config.toml.",
                                       scope=" / ".join(sorted(scopes)) or "Herdr", kind="binding"))
        elif self.page == "settings":
            doc = tomllib.loads(self.base)
            self.items = desktop.profile_rows()[:1]
            for path, label, fallback in SETTINGS:
                value = get_value(doc, path.split("."))
                self.items.append({"id": path, "label": label, "path": path.split("."),
                                   "value": fallback if value == MISSING else value,
                                   "detail": setting_label(fallback if value == MISSING else value),
                                   "description": SETTING_HELP[path], "default": fallback,
                                   "source": "default" if value == MISSING else "user", "kind": "setting"})
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
            super_keys = {}
            for key, action, _ in desktop.MAPPINGS:
                super_keys.setdefault(action, []).append(key)
            for row in catalog_rows(key_rows):
                if row["id"] == "menu":
                    continue
                action = row["id"]
                keys = super_keys.get(action, []) if self.controls_enabled else []
                if not keys:
                    keys = row["keys"]
                description = DESCRIPTIONS.get(action) or row["description"]
                if not description and action.startswith("pane-split-"):
                    description = "Create a shell pane " + action.removeprefix("pane-split-") + " in this tab and directory."
                if not description and action.startswith("pane-resize-"):
                    description = "Adjust the focused pane's size toward the " + action.removeprefix("pane-resize-") + "."
                item = dict(row, label=LABELS.get(action, row["label"]), keywords=row["label"],
                            keys=keys, detail=" / ".join(desktop.pretty_key(k, prefix) for k in keys),
                            description=description, kind="action", section=section_for(row),
                            scope="Herdr focused" if action in super_keys and self.controls_enabled else "Herdr")
                self.items.append(item)
            toggle = desktop.profile_rows()[0]
            self.items.append(dict(toggle, section="configure", description=DESCRIPTIONS["desktop-enabled"]))
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
                ("label", "detail", "id", "category", "source", "scope", "description", "keywords", "section")).lower() for word in words)]
        if self.page == "menu" and not words:
            if self.section == "home":
                return sorted((r for r in rows if r["id"] in HOME), key=lambda r: HOME.index(r["id"]))
            if self.section == "all":
                return sorted(rows, key=lambda r: r["label"].casefold())
            return [r for r in rows if r.get("section") == self.section]
        return rows

    def draw(self, rows):
        MenuView(self).render(rows)

    def change_section(self, direction):
        index = next(i for i, section in enumerate(SECTIONS) if section[0] == self.section)
        self.section = SECTIONS[(index + direction) % len(SECTIONS)][0]
        self.page = "menu"
        self.history.clear()
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
            self.search_origin = (self.selected, self.scroll)
        self.query, self.selected, self.scroll = value, 0, 0
        if not value and self.search_origin:
            self.selected, self.scroll = self.search_origin
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
                            try:
                                updated = int(raw)
                            except ValueError as exc:
                                raise ValueError("Enter a positive whole number, such as 26.") from exc
                            if updated < 1:
                                raise ValueError("Enter a positive whole number.")
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

    def undo(self):
        proposal, path = self.store.undo_proposal()
        if self.review(proposal):
            self.busy("Restoring the previous values…")
            self.store.undo(proposal, path, self.reload_config)
            self.notice = "Undone and reloaded."
            self.load()

    def activate(self, row):
        self.notice = ""
        if row["kind"] == "desktop-toggle":
            if not desktop.installed():
                self.setup_desktop()
            else:
                desktop.set_enabled(not desktop.enabled())
                self.load()
                self.notice = "Super keys enabled in Herdr." if self.controls_enabled else "Super keys now use desktop actions."
            return
        if row["kind"] == "desktop-binding":
            dialogs.document(self, "Desktop shortcut · read only", [row["label"], row["detail"], "", row["description"], "",
                "Use Omarchy controls in Configure to enable or disable the Herdr profile."])
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
        else:
            if CATALOG[action].confirm and not self.confirm_close():
                return
            return {"kind": "action", "id": action, "yes": CATALOG[action].confirm}

    def setup_desktop(self):
        proposal = desktop.install_proposal()
        if desktop.installed() or self.review(proposal):
            self.busy("Setting up focused Super keys…")
            desktop.install_desktop(proposal)
            self.notice = "Omarchy controls enabled for the focused Herdr terminal."
            self.load()

    def confirm_close(self):
        result = dialogs.choose(self, "Close pane " + self.context.pane + "?",
                                [(False, "Cancel — keep the pane"), (True, "Close pane and its process")],
                                help_text="Directory: " + self.context.cwd, footer="↑↓ Select   Enter Confirm   Esc Cancel")
        return result is True

    def help(self):
        rows = self.filtered()
        row = rows[min(self.selected, len(rows) - 1)] if rows else {}
        details = ["SELECTED ITEM", row.get("label", "No item selected"), row.get("detail", ""),
                   row.get("unavailable") or row.get("description", ""), row.get("scope", ""),
                   "Source: " + row["source"] if row.get("source") else "",
                   "Directory: " + self.context.cwd, ""]
        dialogs.document(self, "Herdr × Omarchy · Menu help", details + [
            "MOVE AND SEARCH", "↑ / ↓ select an item. Enter uses the action named in the footer.",
            "Tab / Shift+Tab or ← / → switch sections on the main menu.",
            "Type to search. Search on the main menu includes every section.",
            "Esc clears search, then returns to the previous page or Home, then closes.",
            "PgUp / PgDn scroll lists and documents. Home / End jump to the ends.", "",
            "PAGES", "F2 opens Keybindings. Ctrl+O (or F3) opens Settings.",
            "Ctrl+R refreshes without losing your search or selection.",
            "Ctrl+Z (or F4) reviews undo for the last managed configuration change.", "",
            "KEYBINDINGS", "Super profile rows are read only. Native Herdr rows are editable.",
            "Ctrl+U shows or hides unassigned keys. Search always includes them.",
            "Enter a shortcut such as prefix+z; use a TOML array for alternatives.",
            "An empty value disables the shortcut. Ctrl+D restores native key defaults.",
            "Custom commands keep their own shortcut entry and have no inherited default.", "",
            "EDITING", "Use ← / →, Home / End, Backspace or Delete to edit text.",
            "Ctrl+U clears input. Ctrl+W deletes a word. Ctrl+K deletes to the end.",
            "Enter previews a change; Enter again saves and reloads. Esc goes back.",
            "Invalid entries remain in the editor so you can correct them.", "",
            "FOCUSED CONTROLS", "Super shortcuts affect Herdr only while its terminal is focused.",
            "Super+X closes the pane immediately. Close pane in this menu asks first.",
            "Actions use the pane and directory from which this menu was opened.",
            "Ctrl+C closes the menu; inside a dialog it cancels the dialog.",
        ])

    def run(self):
        if self.page == "desktop-setup":
            self.page = "settings"
            try:
                self.load(preserve=False)
                self.setup_desktop()
            except (ShellError, OSError) as exc:
                self.show_error(exc)
        if self.page == "confirm-pane-close":
            if self.confirm_close():
                return {"kind": "action", "id": "pane-close", "yes": True}
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
                elif key == curses.KEY_F1:
                    self.help()
                elif key in (curses.KEY_F2, curses.KEY_F3, "\x0f"):
                    self.notice = ""
                    self.open_page("keybindings" if key == curses.KEY_F2 else "settings")
                elif key in (curses.KEY_F4, "\x1a"):
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
                        return result
                elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    self.set_query(self.query[:-1])
                elif isinstance(key, str) and key.isprintable():
                    if key != "/" or self.query:
                        self.set_query(self.query + key)
            except (ShellError, OSError, ValueError) as exc:
                self.show_error(exc)


def show(context, page="menu"):
    return curses.wrapper(lambda screen: Menu(screen, context, page).run())
