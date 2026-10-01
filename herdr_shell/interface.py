"""Terminal presentation for the Omarchy + Herdr control menu."""
import curses
from pathlib import Path
import re
import unicodedata

from .branding import HERDR_LOGO, OMARCHY_LOGO

SECTIONS = [
    ("home", "Home", "All your Super+Alt shortcuts"),
    ("all", "All", "Every action, in one place"),
    ("launch", "Launch", "Agents and project tools"),
    ("navigate", "Navigate", "Move between panes, tabs, and workspaces"),
    ("panes", "Panes", "Split, swap, resize, and zoom"),
    ("workspaces", "Workspaces", "Organize your tabs and workspaces"),
    ("configure", "Configure", "Shortcuts, appearance, and integration"),
]
HOME_GROUPS = [
    ("Panes", ["pane-split-up", "pane-split-down", "pane-split-left", "pane-split-right",
               "pane-swap-up", "pane-swap-down", "pane-swap-left", "pane-swap-right",
               "pane-rotate", "pane-zoom", "pane-cycle-next", "pane-cycle-previous", "pane-close"]),
    ("Tabs", ["tab-new", "tab-close", "tab-previous", "tab-next"]),
    ("Workspaces", ["workspace-new", "workspace-close", "workspace-previous", "workspace-next"]),
    ("Agents & tools", ["agent-new", "agent-cycle-next", "agent-cycle-previous", "launch-git"]),
    ("Menu", ["menu"]),
]
HOME = [action for _, actions in HOME_GROUPS for action in actions]
HOME_LABELS = {
    "pane-split-up": "New pane above", "pane-split-down": "New pane below",
    "pane-split-left": "New pane left", "pane-split-right": "New pane right",
    "pane-swap-up": "Swap above", "pane-swap-down": "Swap below",
    "pane-swap-left": "Swap left", "pane-swap-right": "Swap right",
    "pane-rotate": "Rotate split", "pane-zoom": "Zoom / restore", "pane-close": "Close pane",
    "pane-cycle-next": "Next pane", "pane-cycle-previous": "Previous pane",
    "agent-new": "New agent", "agent-cycle-next": "Next agent by priority",
    "agent-cycle-previous": "Previous agent by priority", "launch-git": "Lazygit",
    "tab-new": "New tab", "tab-close": "Close tab", "tab-previous": "Previous tab", "tab-next": "Next tab",
    "workspace-new": "New workspace", "workspace-close": "Close workspace",
    "workspace-previous": "Previous workspace", "workspace-next": "Next workspace", "menu": "Close menu",
}
LABELS = {
    "menu": "Close menu",
    "agent-new": "New agent", "launch-git": "Lazygit", "launch-editor": "Editor",
    "launch-files": "File browser", "pane-split-up": "New pane above", "pane-split-down": "New pane below",
    "pane-split-left": "New pane left", "pane-split-right": "New pane right", "pane-zoom": "Zoom / restore pane",
    "keybindings": "Keybindings", "settings": "Appearance & Settings", "config-undo": "Undo config change",
    "config-reload": "Reload configuration", "theme-terminal": "Use terminal colors",
    "pane-workspace-picker": "Move pane to workspace", "desktop-setup": "Set up desktop integration",
    "pane-rotate": "Rotate split layout", "pane-cycle-next": "Next pane", "pane-cycle-previous": "Previous pane",
    "agent-cycle-next": "Next agent by priority", "agent-cycle-previous": "Previous agent by priority",
}
DESCRIPTIONS = {
    "menu": "Super+Alt+M opens or closes the control menu. Selecting this item closes the current menu.",
    "welcome": "Start with the walkthrough or hands-on walkthrough. Complete either to add Speed Run to this welcome; all activities are already available from the menu.",
    "walkthrough": "Read the shortcut family, agent priority sweep, safe-close policy, menu controls, and settings guide.",
    "learn-hands-on": "See all 26 Super+Alt chords and demonstrate real actions at your own pace in a safe practice workspace.",
    "learn-game": "Use real shortcuts against a 120-second active clock. Faster accurate presses earn more points and streak bonuses.",
    "agent-new": "Open your default Omarchy agent in a new pane to the right, in this tab and directory.",
    "launch-git": "Open Lazygit in a regular pane to the right, using this pane's directory.",
    "launch-editor": "Open your preferred editor in a popup in this directory.",
    "launch-files": "Browse this directory with Yazi or ranger in a popup.",
    "workspace-picker": "Choose a Herdr workspace. Its selected tab and pane are restored.",
    "tab-picker": "Choose another tab in this workspace.",
    "pane-picker": "Find and focus a pane in this session.",
    "pane-workspace-picker": "Choose a workspace and move this running pane into its active tab.",
    "workspace-new": "Create a Herdr workspace in this pane's directory.",
    "tab-new": "Create a tab in this workspace, using this pane's directory.",
    "pane-zoom": "Let this pane fill the Herdr tab. Select again to restore the split layout.",
    "pane-close": "Close an idle shell directly. Ask first before stopping an agent, running tool, or unknown activity.",
    "tab-close": "Close this tab and all its panes. Ask first if any pane has running or uncertain activity.",
    "workspace-close": "Close this workspace and all its tabs. Ask first if any pane has running or uncertain activity.",
    "pane-rotate": "Change the nearest two-pane split between side-by-side and stacked. Directional swap moves a pane instead.",
    "pane-cycle-next": "Focus the next pane in this tab. Wrap after the last pane.",
    "pane-cycle-previous": "Focus the previous pane in this tab. Wrap before the first pane.",
    "agent-cycle-next": "Visit agents needing input, then done, working, and idle agents. Cycle through each priority group in order.",
    "agent-cycle-previous": "Visit the same agent priority sequence in reverse.",
    "keybindings": "Browse the Omarchy profile and edit native Herdr shortcuts with a preview and undo.",
    "settings": "Adjust the theme, pane borders, gaps, sidebar, tabs, and notifications.",
    "config-reload": "Apply the current Herdr configuration to the running session.",
    "config-undo": "Review and undo the last managed configuration change.",
    "theme-terminal": "Use the colors of your outer terminal, including its Omarchy theme.",
    "desktop-setup": "Enable the focused Super+Alt profile with a reviewed Hyprland configuration change. Existing desktop shortcuts stay reserved.",
    "desktop-enabled": "Toggle the Super+Alt profile for focused Herdr terminals. Existing desktop shortcuts stay reserved, including in Herdr.",
}


def section_for(row):
    action = row["id"]
    if action.startswith("launch-") or action in ("agent-new", "learn-hands-on", "learn-game"):
        return "launch"
    if action in ("workspace-picker", "tab-picker"):
        return "navigate"
    if action in ("workspace-new", "tab-new", "pane-workspace-picker") or action.startswith(("workspace-", "tab-")):
        return "workspaces"
    if action.startswith(("navigate-", "pane-focus-", "pane-cycle-", "agent-next", "agent-cycle-")) or action == "pane-picker":
        return "navigate"
    if action.startswith("pane-"):
        return "panes"
    return "configure"


def clean(value):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value))


def cell_width(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in clean(value))


def fit(value, width, pad=False):
    value = clean(value)
    width = max(0, width)
    clipped = cell_width(value) > width
    budget = max(0, width - 1) if clipped else width
    result, used = "", 0
    for char in value:
        size = cell_width(char)
        if used + size > budget:
            break
        result += char
        used += size
    if clipped and width:
        result += "…"
        used += 1
    return result + (" " * max(0, width - used) if pad else "")


def display_path(value):
    home = str(Path.home())
    return "~" + value[len(home):] if value == home or value.startswith(home + "/") else value


def wrap_cells(value, width):
    """Wrap by terminal cells, including wide filenames and config values."""
    value, width = clean(value), max(1, width)
    lines = []
    while value:
        used = end = 0
        for char in value:
            size = cell_width(char)
            if used + size > width:
                break
            used += size
            end += 1
        end = max(1, end)
        if end < len(value):
            space = value.rfind(" ", 0, end + 1)
            if space > 0:
                end = space
        lines.append(value[:end].rstrip())
        value = value[end:].lstrip()
    return lines or [""]


def activation_label(row):
    if not row:
        return ""
    if row.get("unavailable"):
        return "Details"
    if row.get("id") == "menu":
        return "Close"
    return {"setting": "Edit", "binding": "Edit" if row.get("path") else "Info",
            "desktop-binding": "Info", "desktop-toggle": "Set up" if row.get("detail") == "Set up" else "Toggle",
            "target": "Switch", "move-target": "Move"}.get(row.get("kind"),
            "Open" if row.get("id") in ("keybindings", "settings", "desktop-setup", "welcome", "walkthrough", "learn-hands-on", "learn-game") or row.get("id", "").endswith("-picker")
            else "Review" if row.get("confirm") or row.get("id") in ("config-undo", "theme-terminal") else "Run")


class MenuView:
    def __init__(self, menu):
        self.menu = menu
        self.height, self.width = menu.screen.getmaxyx()
        self.offset = max(0, (self.width - 142) // 2)
        self.width = min(self.width, 142)
        # Give the actual marks enough resolution while retaining usable lists
        # in shorter windows. Compact layouts keep just the names.
        self.home = menu.page == "menu" and menu.section == "home" and not menu.query
        self.show_logos = menu.page == "menu" and not self.home and self.width >= 88 and self.height >= 32
        self.header_extra = max(len(HERDR_LOGO), len(OMARCHY_LOGO)) - 3 if self.show_logos else 0
        self.too_small = self.height < 16 or self.width < 46

    def text(self, y, x, text, attr=0, width=None):
        self.menu.write(y, x + self.offset, fit(text, self.width - x - 2 if width is None else width), attr)

    def rule(self, y, x, width):
        self.text(y, x, "─" * max(0, width), curses.A_DIM, width)

    def paragraph(self, y, x, value, width, limit, attr=0):
        lines = wrap_cells(value, width)
        shown = lines[:max(0, limit)]
        if len(lines) > len(shown) and shown:
            shown[-1] = fit(shown[-1], max(0, width - 1)) + "…"
        for index, line in enumerate(shown):
            self.text(y + index, x, line, attr, width)
        return y + len(shown)

    def brand(self):
        m = self.menu
        x, y = 3, 1
        if self.show_logos:
            for index, line in enumerate(HERDR_LOGO, 1):
                self.text(index, 3, line, m.accent, 20)
            for index, line in enumerate(OMARCHY_LOGO, 1):
                self.text(index, 26, line, m.brand, 15)
            self.text(4, 23, "×", curses.A_DIM, 1)
            x, y = 45, 3
        self.text(y, x, "HERDR", m.accent, 5)
        self.text(y, x + 7, "×", curses.A_DIM, 1)
        self.text(y, x + 10, "OMARCHY", m.brand, 7)
        return x, y

    def render(self, rows):
        m = self.menu
        m.screen.erase()
        h, w = self.height, self.width
        if self.too_small:
            self.text(1, 2, "HERDR × OMARCHY", m.accent)
            self.text(3, 2, "Enlarge the terminal to 46 × 16.")
            self.text(5, 2, "Esc closes the menu.")
            m.screen.refresh()
            return
        if self.home:
            self.render_home(rows)
            return
        heading_x, heading_y = self.brand()
        extra = self.header_extra
        body_height = h - extra
        ready = getattr(m, "controls_ready", 0)
        conflicts = getattr(m, "controls_conflicts", 0)
        status = f"● Super+Alt {ready} ready" if m.controls_enabled and ready else "○ Super+Alt unavailable" if m.controls_enabled else "○ Super+Alt off"
        if conflicts:
            status += f" · {conflicts} reserved"
        if w >= 65:
            self.text(1, w - cell_width(status) - 4, status, m.good if m.controls_enabled else curses.A_DIM)
        self.text(heading_y + 1, heading_x, m.location, curses.A_DIM, w - heading_x - 4)
        path = display_path(m.context.cwd)
        self.text(heading_y + 2, heading_x, path, curses.A_DIM, w - heading_x - 4)
        self.rule(4 + extra, 3, w - 7)
        placeholder = "Type to search all actions…" if m.page == "menu" else "Type to search this page…"
        query = m.query
        while query and cell_width(query) > w - 12:
            query = query[1:]
        self.text(5 + extra, 3, "/  " + (query + "▏" if m.query else placeholder), m.accent if m.query else curses.A_DIM)
        self.rule(6 + extra, 3, w - 7)

        rail = w >= 86 and body_height >= 26
        details = w >= 124 and body_height >= 26
        left = 24 if rail else 3
        right = w - 37 if details else w - 3
        list_width = right - left - (2 if details else 0)
        bottom = h - 5
        if rail:
            for y in range(7 + extra, bottom):
                self.text(y, 21, "│", curses.A_DIM, 1)
            self.text(8 + extra, 3, "CONTROL MENU", curses.A_DIM)
            section_step = 2 if bottom - extra >= 24 else 1
            for index, (section, label, _) in enumerate(SECTIONS):
                active_section = m.section if m.page == "menu" else (
                    "configure" if m.page in ("settings", "keybindings") else "workspaces" if m.page == "pane-workspace-picker" else "navigate")
                active = active_section == section and not m.query
                self.text(10 + extra + index * section_step, 3, fit(("› " if active else "  ") + label, 17, True),
                          m.accent if active else curses.A_NORMAL, 17)
            hint_y = 13 + extra + (len(SECTIONS) - 1) * section_step
            if hint_y + 3 < bottom:
                self.rule(hint_y, 3, 16)
                self.text(hint_y + 2, 3, "Tab / ← →" if m.page == "menu" else "Esc", m.accent)
                self.text(hint_y + 3, 3, "Change section" if m.page == "menu" else "Previous page", curses.A_DIM)

        section = next(s for s in SECTIONS if s[0] == m.section)
        titles = {"keybindings": "Keybindings", "settings": "Appearance & Settings",
                  "workspace-picker": "Switch workspace", "tab-picker": "Switch tab", "pane-picker": "Switch pane",
                  "pane-workspace-picker": "Move pane to workspace"}
        title = ("Search results" if m.query else section[1]) if m.page == "menu" else titles.get(m.page, "Herdr Shell")
        subtitle = (f"{len(rows)} matching actions" if m.query else section[2]) if m.page == "menu" else {
            "keybindings": "Native keys editable · Super+Alt profile shows live status",
            "settings": "Preview changes before saving",
            "pane-workspace-picker": "Move this pane and keep its process running",
        }.get(m.page, "Choose a destination")
        if m.page != "menu":
            title = "‹ " + title
        elif not rail:
            title = "‹ " + title + " ›"
        self.text(8 + extra, left, title, m.accent, list_width)
        self.text(9 + extra, left, subtitle, curses.A_DIM, list_width)
        row_start = (10 if body_height < 22 else 11) + extra
        roomy = m.page == "menu" and body_height >= 34 and list_width >= 44 and len(rows) <= (bottom - row_start) // 2
        step = 2 if roomy else 1
        visible = max(1, (bottom - row_start) // step)
        m.visible_rows = visible
        m.selected = min(max(0, m.selected), max(0, len(rows) - 1))
        m.scroll = max(0, min(m.scroll, max(0, len(rows) - visible)))
        if m.selected < m.scroll:
            m.scroll = m.selected
        elif m.selected >= m.scroll + visible:
            m.scroll = m.selected - visible + 1
        for index, row in enumerate(rows[m.scroll:m.scroll + visible], m.scroll):
            y = row_start + (index - m.scroll) * step
            selected = index == m.selected
            attr = m.selection if selected else curses.A_DIM if row.get("unavailable") else curses.A_NORMAL
            detail = row.get("detail", "")
            if row.get("unavailable"):
                detail = "Unavailable"
            key_width = min(cell_width(detail), max(10, list_width // 2 - 2))
            label_width = max(6, list_width - key_width - 5)
            marker = "› " if selected else "! " if row.get("unavailable") else "  "
            line = marker + fit(row["label"], label_width, True)
            line += "  " + fit(detail, key_width)
            self.text(y, left, fit(line, list_width, True), attr, list_width)
            if roomy and not details:
                self.text(y + 1, left + 2, row.get("description", ""), curses.A_DIM, list_width - 2)
        if not rows:
            empty = m.page == "pane-workspace-picker" and not m.query
            self.text(row_start, left + 2, "No other workspaces yet." if empty else "No matching entries.", curses.A_NORMAL, list_width - 2)
            if row_start + 1 < bottom:
                hint = "Esc back · Workspaces → New workspace" if empty else "Esc clears search" if m.query else "Ctrl+R Refresh · F1 Help"
                self.text(row_start + 1, left + 2, hint, curses.A_DIM, list_width - 2)
        selected = rows[m.selected] if rows else {}
        if details:
            self.details(selected, right + 2, bottom)
        elif selected:
            help_text = selected.get("unavailable") or selected.get("description") or selected.get("scope", "")
            self.text(h - 5, 3, help_text, curses.A_DIM, w - 7)
        self.rule(h - 4, 3, w - 7)
        count = f"{m.selected + 1 if rows else 0}/{len(rows)}"
        if len(rows) > visible:
            count = ("↑ " if m.scroll else "") + count + (" ↓" if m.scroll + visible < len(rows) else "")
        notice = m.notice or ("PgUp / PgDn Scroll" if len(rows) > visible else selected.get("scope", ""))
        if m.page == "keybindings" and not m.notice:
            notice = "Ctrl+U " + ("Hide" if m.show_unassigned else "Show") + " unassigned · Ctrl+R Refresh"
        self.text(h - 3, 3, notice, m.accent if m.notice else curses.A_DIM, w - len(count) - 10)
        self.text(h - 3, w - len(count) - 4, count, curses.A_DIM)
        back = "Clear" if m.query else "Back" if m.page != "menu" or m.section != "home" else "Close"
        hints = ["↑↓ Select", "Enter " + activation_label(selected) if selected else "", "Esc " + back,
                 "F1 Help", "Tab Sections" if m.page == "menu" else "", "F2 Keys", "Ctrl+O Settings", "Ctrl+Z Undo"]
        footer = ""
        for hint in filter(None, hints):
            candidate = footer + ((" " if w < 65 else "  ") if footer else "") + hint
            if cell_width(candidate) <= w - 7:
                footer = candidate
        self.text(h - 2, 3, footer, m.accent, w - 7)
        m.screen.refresh()

    def render_home(self, rows):
        """A one-line shortcut sheet; group headings never consume selection slots."""
        m, h, w = self.menu, self.height, self.width
        self.brand()
        self.text(2, 3, m.location, curses.A_DIM, w - 7)
        self.text(3, 3, "/  Search actions…", curses.A_DIM, w - 7)
        ready, conflicts = m.controls_ready, m.controls_conflicts
        status = f"{ready} ready" if m.controls_enabled and ready else "unavailable" if m.controls_enabled else "off"
        if conflicts:
            status += f", {conflicts} reserved"
        self.text(4, 3, "Hold Super+Alt · " + status, m.accent, w - 7)
        self.rule(5, 3, w - 7)
        groups = {action: label for label, actions in HOME_GROUPS for action in actions}
        capacity = max(2, h - 10)
        m.selected = min(max(0, m.selected), max(0, len(rows) - 1))
        m.scroll = min(max(0, m.scroll), m.selected)

        def window(start):
            visible, used, previous = [], 0, None
            for index in range(start, len(rows)):
                group = groups[rows[index]["id"]]
                cost = 1 + (group != previous)
                if used + cost > capacity:
                    break
                visible.append((index, group if group != previous else None))
                used += cost
                previous = group
            return visible

        visible = window(m.scroll)
        while rows and (not visible or visible[-1][0] < m.selected):
            m.scroll += 1
            visible = window(m.scroll)
        # Fill space when the window grows again, keeping the selected item visible.
        while m.scroll and window(m.scroll - 1)[-1][0] >= m.selected:
            m.scroll -= 1
            visible = window(m.scroll)
        m.visible_rows = max(1, len(visible))
        key_width = max((cell_width(row.get("shortcut_suffix", "")) for row in rows), default=1)
        list_width = w - 6
        label_width = list_width - key_width - 4
        y = 6
        for index, heading in visible:
            row = rows[index]
            if heading:
                self.text(y, 3, heading.upper(), m.accent, list_width)
                y += 1
            inactive = not row.get("shortcut_active")
            selected = index == m.selected
            marker = "!" if inactive or row.get("unavailable") else " "
            line = ("›" if selected else " ") + marker
            line += fit(row["label"], label_width, True) + "  "
            line += fit(row.get("shortcut_suffix", ""), key_width, True)
            attr = m.selection if selected else curses.A_DIM if inactive or row.get("unavailable") else curses.A_NORMAL
            self.text(y, 3, line, attr, list_width)
            y += 1
        self.rule(h - 4, 3, w - 7)
        count = f"{m.selected + 1 if rows else 0}/{len(rows)}"
        if m.scroll:
            count = "↑ " + count
        if visible and visible[-1][0] + 1 < len(rows):
            count += " ↓"
        inactive = any(not row.get("shortcut_active") or row.get("unavailable") for row in rows)
        notice = m.notice or ("! Inactive / limited · F1 details" if inactive else "Type learn for walkthrough / Speed Run")
        if len(visible) < len(rows) and not m.notice and not inactive:
            notice = "PgUp/PgDn scroll · F1 details"
        self.text(h - 3, 3, notice, curses.A_DIM, w - cell_width(count) - 9)
        self.text(h - 3, w - cell_width(count) - 4, count, curses.A_DIM, cell_width(count))
        selected = rows[m.selected] if rows else {}
        self.text(h - 2, 3, "↑↓ Select · Enter " + activation_label(selected) + " · Esc · F1 Help", m.accent, w - 7)
        self.text(h - 1, 3, "Tab Sections  F2 Keys  Ctrl+O Settings", curses.A_DIM, w - 7)
        m.screen.refresh()

    def details(self, row, x, bottom):
        m = self.menu
        width = self.width - x - 4
        extra = self.header_extra
        content_bottom = bottom - 1
        for y in range(7 + extra, bottom):
            self.text(y, x - 2, "│", curses.A_DIM, 1)
        self.text(8 + extra, x, "DETAILS", curses.A_DIM, width)
        y = self.paragraph(10 + extra, x, row.get("label", "Search for an action"), width, 2, m.accent)
        description = row.get("unavailable") or row.get("description") or {
            "binding": "Edit this native Herdr shortcut, review the diff, then save and reload.",
            "setting": "Edit this setting and review its change before applying it.",
            "desktop-binding": "Part of the Super+Alt profile. Live status shows whether the shortcut can run.",
            "target": "Press Enter to switch to this destination.",
            "move-target": "Move the originating pane to this workspace's active tab.",
        }.get(row.get("kind"), "")
        y = self.paragraph(y + 1, x, description, width, min(6, content_bottom - y - 1))
        shortcut = row.get("detail", "")
        if shortcut and y + 4 < content_bottom:
            label = "VALUE" if row.get("kind") == "setting" else "SHORTCUT" if row.get("keys") else "STATUS"
            self.text(y + 1, x, label, curses.A_DIM, width)
            y = self.paragraph(y + 2, x, shortcut, width, min(4, content_bottom - y - 2), m.accent)
        source = {"default": "Inherited default", "user": "User override", "custom": "Custom command",
                  "desktop": "Desktop profile · read only", "legacy": "Legacy binding · read only"}.get(row.get("source"), "")
        if row.get("kind") == "desktop-toggle":
            source = "Focused Herdr terminals only"
        if source and y + 2 < content_bottom:
            y = self.paragraph(y + 1, x, source, width, 2, curses.A_DIM)
        if row.get("scope") and y + 2 < content_bottom:
            y = self.paragraph(y + 1, x, row["scope"], width, 2, curses.A_DIM)
        if row.get("path") and y + 2 < content_bottom:
            self.paragraph(y + 1, x, ".".join(row["path"]), width, 2, curses.A_DIM)
