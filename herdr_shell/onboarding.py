"""First-use learning choices, completion state, and a read-only walkthrough."""
import curses
import json

from . import dialogs
from .config import atomic_write
from .interface import wrap_cells
from .runtime import state_path


# Bump only when the welcome itself needs to be shown again. Plugin releases
# and config reloads must not repeatedly interrupt a returning user.
WELCOME_VERSION = 1

WALKTHROUGH_PAGES = (
    ("One shortcut family", (
        "Hold Super+Alt together, then press a key. M opens the menu; M again closes it.",
        "Direct commands target the focused local Herdr terminal. The menu shows which shortcuts are actually ready or reserved.",
        "Omarchy's Super+Ctrl+Enter still opens Herdr. Super+Space keeps opening the Omarchy menu.",
        "A disabled or occupied direct shortcut remains unavailable. Use the native prefix menu shortcut or herdr-shell menu to recover.",
    )),
    ("Split and swap", (
        "Super+Alt+U / D / L / R split up / down / left / right, using your current directory.",
        "Super+Alt+Shift+U / D / L / R swap with the neighbor in that direction.",
        "Think U for up, D for down, L for left, and R for right. Shift rearranges an existing pane.",
    )),
    ("Focus and arrange panes", (
        "Ctrl+Alt+arrows focus a neighboring pane. At an edge, focus stays in this tab and workspace.",
        "Ctrl+Alt+Shift+arrows keep their native pane-resize action.",
        "Super+Alt+P visits the next pane in this tab; Shift+P goes back. Both wrap.",
        "Super+Alt+J rotates the nearest two-leaf split between stacked and side by side. A nested sibling group stays unchanged.",
        "Super+Alt+Z zooms a pane; press Z again to restore the layout.",
    )),
    ("Tabs and workspaces", (
        "Super+Alt+T makes a tab. Super+Alt+W closes the current tab.",
        "Super+Alt+Shift+T makes a Herdr workspace. Super+Alt+Shift+W closes the current workspace.",
        "Super+Alt+Page Up / Page Down switch to the previous / next tab.",
        "Super+Alt+Shift+Page Up / Page Down switch to the previous / next Herdr workspace.",
        "Pane focus, tab switching, and workspace switching each have their own commands.",
    )),
    ("Agents and Git", (
        "Super+Alt+A opens your default Omarchy agent in a pane to the right, in your current tab and directory. Choose an agent when no default is set.",
        "Super+Alt+V opens Lazygit in a regular pane to the right, in the same directory.",
        "Super+Alt+Q visits agents by priority: blocked, then done, working, and idle. Shift+Q follows that order in reverse.",
        "The order stays stable during a sweep, so status changes cannot keep sending you back to the same agent.",
    )),
    ("Close with confidence", (
        "Super+Alt+X closes the focused pane. W closes a tab; Shift+W closes a workspace.",
        "An idle shell closes directly. Running or unknown work asks first, with Cancel selected.",
        "Tab and workspace close check every pane in that scope. Esc keeps the panes and their work running.",
        "Menu close actions use the same policy. An action error never closes the outer desktop terminal as a fallback.",
    )),
    ("Find anything in the menu", (
        "Super+Alt+M opens the menu. Type to search across all sections; Enter uses the selected action.",
        "F1 explains the selected item and menu controls. F2 opens Keybindings; Ctrl+O or F3 opens Settings.",
        "Tab and Shift+Tab switch sections. Esc clears search, returns to the previous page or Home, then closes the menu.",
        "Ctrl+R refreshes while keeping your search and selection. Profile shortcut rows show live status and are read only.",
    )),
    ("Settings and colors", (
        "Native shortcuts and settings use a preview: Enter reviews a change, Enter again applies it, and Esc goes back.",
        "Ctrl+Z or F4 reviews undo for the last managed configuration change. Invalid entries stay available to correct.",
        "Use terminal colors lets Herdr follow the outer terminal's palette, including its Omarchy theme. Review the change first.",
        "Herdr Shell is a Herdr plugin. Its menu runs inside Herdr; a Hyprland Lua bridge supplies the focused Super+Alt shortcuts.",
        "Existing desktop bindings keep their owners. This profile adds no plain Alt shortcuts and leaves Omarchy's other commands intact.",
    )),
)


def _marker_path():
    return state_path() / "onboarding.json"


def needs_welcome():
    """A missing, corrupt, or older marker is eligible for a first-use welcome."""
    try:
        marker = json.loads(_marker_path().read_text())
        return not (isinstance(marker, dict) and type(marker.get("welcome_version")) is int
                    and marker["welcome_version"] == WELCOME_VERSION)
    except (OSError, ValueError):
        return True


def _saved_marker():
    try:
        value = json.loads(_marker_path().read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_marker(**changes):
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = {**_saved_marker(), **changes}
    atomic_write(path, json.dumps(marker) + "\n", mode=0o600)


def mark_welcomed():
    """Persist dismissal while preserving walkthrough completion and other state."""
    _save_marker(welcome_version=WELCOME_VERSION)


def has_learning_completed():
    return _saved_marker().get("learning_completed") is True


def mark_learning_completed():
    """Unlock the full welcome after either walkthrough actually finishes."""
    _save_marker(learning_completed=True)


def _finish_welcome(menu):
    try:
        mark_welcomed()
    except OSError:
        menu.notice = "Cannot save the welcome preference; it may appear again."


def welcome(menu, *, title="Welcome to Herdr Shell", completed=None):
    """Offer two walkthroughs first; completion adds Speed Run and explicit exit."""
    options = [("walkthrough", "Walkthrough · read the feature guide"),
               ("hands-on", "Hands-on walkthrough · try every key")]
    unlocked = has_learning_completed() if completed is None else completed
    if unlocked:
        options += [("game", "Speed Run · timed + score"), (None, "Exit welcome")]
    selected, displayed = 0, False
    while True:
        frame = dialogs.Frame(menu, title, height=20, width=78)
        if not frame.small:
            frame.text(5, "One family: Super+Alt + a key.", menu.accent)
            for index, (_, label) in enumerate(options):
                frame.text(7 + index, ("› " if selected == index else "  ") + label,
                           menu.selection if selected == index else curses.A_NORMAL)
            frame.footer("↑↓ Select  Enter Open  Esc Close")
        key = frame.key()
        if not frame.small:
            displayed = True
        if key in dialogs.ESCAPE:
            if displayed:
                _finish_welcome(menu)
            return None
        if frame.small or key == curses.KEY_RESIZE:
            continue
        if key in dialogs.ENTER:
            _finish_welcome(menu)
            return options[selected][0]
        if key in (curses.KEY_UP, curses.KEY_BTAB, curses.KEY_LEFT):
            selected = (selected - 1) % len(options)
        elif key in (curses.KEY_DOWN, "\t", curses.KEY_RIGHT):
            selected = (selected + 1) % len(options)
        elif key == curses.KEY_HOME:
            selected = 0
        elif key == curses.KEY_END:
            selected = len(options) - 1


def _wrapped(lines, width):
    result = []
    for index, line in enumerate(lines):
        if index:
            result.append("")
        result.extend(wrap_cells(line, width))
    return result


def walkthrough(menu):
    """Read-only pages; completion unlocks the full activity choice."""
    page = 0
    offsets = [0] * len(WALKTHROUGH_PAGES)
    total = len(WALKTHROUGH_PAGES)
    while True:
        if page == total:
            try:
                mark_learning_completed()
            except OSError:
                menu.notice = "Cannot save learning completion; Speed Run remains available from the menu."
            return welcome(menu, title="Walkthrough complete", completed=True)
        title, paragraphs = WALKTHROUGH_PAGES[page]
        frame = dialogs.Frame(menu, title, height=32, width=94)
        if not frame.small:
            lines = _wrapped(paragraphs, frame.inner)
            visible = max(1, frame.bottom - frame.top)
            offsets[page] = min(max(0, offsets[page]), max(0, len(lines) - visible))
            for y, line in enumerate(lines[offsets[page]:offsets[page] + visible], frame.top):
                frame.text(y, line)
            frame.footer("← Back →/Enter Next ↑↓ Scroll Esc")
            position = f"{page + 1}/{total}"
            if len(lines) > visible:
                position += f" · Lines {offsets[page] + 1}–{min(len(lines), offsets[page] + visible)}/{len(lines)}"
            frame.text(frame.height - 3, position, curses.A_DIM)
        key = frame.key()
        if key in dialogs.ESCAPE:
            return None
        if frame.small or key == curses.KEY_RESIZE:
            continue
        if key == curses.KEY_LEFT:
            page = max(0, page - 1)
        elif key in dialogs.ENTER or key == curses.KEY_RIGHT:
            page += 1
        elif key == curses.KEY_DOWN:
            offsets[page] += 1
        elif key == curses.KEY_UP:
            offsets[page] -= 1
        elif key == curses.KEY_NPAGE:
            offsets[page] += visible
        elif key == curses.KEY_PPAGE:
            offsets[page] -= visible
        elif key == curses.KEY_HOME:
            offsets[page] = 0
        elif key == curses.KEY_END:
            offsets[page] = len(lines)
