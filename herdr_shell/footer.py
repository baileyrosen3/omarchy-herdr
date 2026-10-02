"""One terminal row of active Herdr shortcut hints for a native footer."""
import os
import re
import shutil
import subprocess
import unicodedata

from . import PLUGIN_ID
from . import config, desktop
from .runtime import ShellError

# Keys stay explicit when only part of a group is available. A conflicted key
# must never sneak back in through an abbreviation such as "all directions".
GROUPS = (
    ("Menu", (("menu", "M"),)),
    ("Split", tuple(("pane-split-" + direction, key) for direction, key in
                    (("up", "U"), ("down", "D"), ("left", "L"), ("right", "R")))),
    ("New agent", (("agent-new", "A"),)),
    ("Next agent", (("agent-cycle-next", "Q"),)),
    ("New tab", (("tab-new", "T"),)),
    ("Zoom", (("pane-zoom", "Z"),)),
    ("Close pane", (("pane-close", "X"),)),
    ("Next pane", (("pane-cycle-next", "P"),)),
    ("Rotate", (("pane-rotate", "J"),)),
    ("Git", (("launch-git", "V"),)),
    ("Close tab", (("tab-close", "W"),)),
    ("New space", (("workspace-new", "Shift+T"),)),
    ("Close space", (("workspace-close", "Shift+W"),)),
    ("Swap", tuple(("pane-swap-" + direction, "Shift+" + key) for direction, key in
                   (("up", "U"), ("down", "D"), ("left", "L"), ("right", "R")))),
    ("Prev pane", (("pane-cycle-previous", "Shift+P"),)),
    ("Prev agent", (("agent-cycle-previous", "Shift+Q"),)),
    ("Switch tab", (("tab-previous", "PageUp"), ("tab-next", "PageDown"))),
    ("Switch space", (("workspace-previous", "Shift+PageUp"), ("workspace-next", "Shift+PageDown"))),
)
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def clean(value):
    """Remove terminal escapes, controls, and invisible direction overrides."""
    text = ANSI.sub("", str(value))
    text = "".join(" " if char.isspace() else char for char in text
                   if not unicodedata.category(char).startswith("C") or char.isspace())
    return " ".join(unicodedata.normalize("NFC", text).split())


def _char_width(char):
    return 0 if unicodedata.category(char) in ("Mn", "Me") else \
           2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def cell_width(value):
    return sum(_char_width(char) for char in clean(value))


def fit(value, width):
    """Clip by terminal cells without splitting a wide character or adding rows."""
    output, used = [], 0
    for char in clean(value):
        size = _char_width(char)
        if used + size > max(0, width):
            break
        output.append(char)
        used += size
    return "".join(output).rstrip()


def default_width():
    supplied = os.environ.get("HERDR_FOOTER_WIDTH")
    if supplied is not None:
        try:
            return max(0, int(supplied))
        except ValueError:
            pass
    return max(0, shutil.get_terminal_size(fallback=(80, 24)).columns)


def native_menu_hint(text):
    """Use a native recovery shortcut only when its actual binding is unoccupied."""
    rows = config.bindings(text)
    occupied = config.conflicts(text)
    prefix = next((row["keys"][0] for row in rows if row["id"] == "prefix" and row["keys"]), "")
    for row in rows:
        if row.get("action_id") != PLUGIN_ID + ".menu":
            continue
        for key in row["keys"]:
            if not key or any("normal:" + chord in occupied for chord in config.normalized(key)):
                continue
            if key.lower().startswith("prefix+") and (not prefix or any(
                    "normal:" + chord in occupied for chord in config.normalized(prefix))):
                continue
            label = desktop.pretty_key(key, prefix)
            if label:
                return clean(label) + " Menu"
    return "herdr-shell menu"


def render(width, availability, fallback="herdr-shell menu"):
    """Prioritize the menu and add complete hint groups only while they fit."""
    width = max(0, width)
    ready = lambda action: availability.get(action, {}).get("active") is True and \
                           availability.get(action, {}).get("status") == "ready"
    hints = [("/".join(key for action, key in keys if ready(action)), label)
             for label, keys in GROUPS]
    hints = [(keys, label) for keys, label in hints if keys]
    if not hints:
        return fit(fallback, width)
    if ready("menu"):
        # Below eleven cells even the full menu chord cannot be shown safely.
        # Do not teach a misleading bare M when its modifiers will not fit.
        if width < 18:
            return "Super+Alt+M Menu" if width >= 16 else "Super+Alt+M" if width >= 11 else ""
        line = "Super+Alt | M Menu"
        hints = hints[1:]
    else:
        # A reserved M must not be advertised. Keep a recovery route visible.
        recovery = clean(fallback)
        line = recovery + " | Super+Alt"
        if cell_width(line) > width:
            return fit(recovery, width)
    for keys, label in hints:
        candidate = line + " | " + keys + " " + label
        if cell_width(candidate) <= width:
            line = candidate
    return line


def output(width=None, store=None):
    """Read-only footer provider; it does not require an originating pane."""
    width = default_width() if width is None else width
    # Desktop routing is verified only for local clients. Native renderers may
    # explicitly mark a remote endpoint so it cannot advertise host-only keys.
    if os.environ.get("HERDR_FOOTER_LOCAL") == "0":
        return fit("Remote Herdr: native shortcuts", width)
    try:
        availability = desktop.effective_bindings(refresh=True)
    except (ShellError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        availability = {}
    fallback = "herdr-shell menu"
    menu = availability.get("menu", {})
    if menu.get("active") is not True or menu.get("status") != "ready":
        try:
            fallback = native_menu_hint((store or config.ConfigStore()).read())
        except (ShellError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
    return render(width, availability, fallback)
