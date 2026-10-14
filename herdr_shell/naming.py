"""Workspace names at creation and live tab names from agent task titles.

Workspaces carry project identity: other tools group by and look up their
labels. Tabs carry the job: the namer daemon (namer.py) follows the task title
of the agent running in each tab it owns. Panes stay unlabeled so clients keep
showing each terminal's live title.
"""
import json
from pathlib import Path
import subprocess

from .config import atomic_write


DEFAULTS = {"tab_mode": "task", "workspace_mode": "project"}
OPTIONS = {
    "tab_mode": (("task", "Agent task"), ("numbered", "Herdr default")),
    "workspace_mode": (("project", "Project"), ("folder", "Working directory")),
}
LABELS = {key: dict(values) for key, values in OPTIONS.items()}
TAB_LABEL_LIMIT = 28
# Reported on agent panes whose tab label the namer derived from a task title,
# so dashboards that never show prompt-derived names can skip that label.
MARKER_TOKEN = "herdr_shell_tab_label"
MARKER_VALUE = "topic"
# omp prefixes its session title with π; any other one-character non-alphanumeric
# word (braille spinner frames, ">", "✳") is a state marker, not part of the task.
AGENT_GLYPHS = frozenset({"π"})
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064\ufeff"))


def preferences_path():
    from .desktop import preferences_dir
    return preferences_dir() / "naming.json"


def preferences():
    try:
        value = json.loads(preferences_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {key: value[key] if isinstance(value.get(key), str) and value[key] in LABELS[key] else default
            for key, default in DEFAULTS.items()}


def set_preference(key, value):
    if not isinstance(key, str) or key not in OPTIONS or not isinstance(value, str) or value not in LABELS[key]:
        raise ValueError("Unknown naming preference")
    current = preferences()
    if current[key] == value:
        return False
    current[key] = value
    atomic_write(preferences_path(), json.dumps(current, indent=2) + "\n")
    return True


def directory_name(cwd):
    name = Path(cwd).expanduser().name
    return name or "root"


def _git(cwd, *args):
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                                text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def unique_label(base, existing, alternate=None):
    """Keep sibling names distinct without changing labels already on screen."""
    used = {label.casefold() for label in existing if isinstance(label, str)}
    if base.casefold() not in used:
        return base
    if alternate and alternate.casefold() not in used:
        return alternate
    index = 2
    while f"{base} · {index}".casefold() in used:
        index += 1
    return f"{base} · {index}"


def workspace_label(cwd, mode, existing):
    """The Git project (or folder), then its branch, then a suffix for duplicates."""
    folder = directory_name(cwd)
    root = _git(cwd, "rev-parse", "--show-toplevel") if mode == "project" else None
    base = (Path(root).name if root else folder) or folder
    if base.casefold() not in {name.casefold() for name in existing if isinstance(name, str)}:
        return base
    branch = _git(cwd, "branch", "--show-current") if root else None
    return unique_label(base, existing, f"{base} · {branch}" if branch else None)


def _clean(value):
    if not isinstance(value, str):
        return None
    return " ".join(value.translate(_INVISIBLE).split()) or None


def title_topic(title):
    """The task in an agent-style terminal title such as "π ⠹ Fix login".

    Titles without an agent glyph or state marker in front (a shell's
    "fish ~/src", an editor's file name) are not task titles.
    """
    words = (_clean(title) or "").split(" ")
    prefixed = False
    while words and words[0] and (words[0] in AGENT_GLYPHS or (len(words[0]) == 1 and not words[0].isalnum())):
        words.pop(0)
        prefixed = True
    if not prefixed:
        return None
    return " ".join(words) or None


def agent_topic(pane):
    """Task title of the agent in a pane: its own session title, then the chat
    title published by herdr-agent-usage. Never the agent's name, a directory,
    or any label (so names can't feed back into themselves)."""
    if not isinstance(pane, dict) or not pane.get("agent"):
        return None
    tokens = pane.get("tokens") if isinstance(pane.get("tokens"), dict) else {}
    cwd = pane.get("foreground_cwd") or pane.get("cwd")
    ignored = {value.casefold() for value in (pane.get("agent"), pane.get("display_agent"), "untitled",
                                               directory_name(cwd) if isinstance(cwd, str) else None)
               if isinstance(value, str)}
    title = pane.get("terminal_title_stripped") or pane.get("terminal_title")
    for candidate in (title_topic(title), _clean(tokens.get("quota_topic"))):
        if candidate and candidate.casefold() not in ignored and not candidate.startswith(("~", "/")):
            return candidate
    return None


def short_label(topic, limit=TAB_LABEL_LIMIT):
    """Fit a task title to the tab bar, preferring a word boundary."""
    if len(topic) <= limit:
        return topic
    cut = topic[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-·") + "…"


def tab_positions(snapshot):
    """Herdr's default tab label: the live 1-based position within its workspace."""
    counts, positions = {}, {}
    for tab in snapshot.get("tabs", []):
        workspace = tab.get("workspace_id")
        counts[workspace] = counts.get(workspace, 0) + 1
        positions[tab["tab_id"]] = str(counts[workspace])
    return positions


