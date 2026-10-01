"""Key Quest: scored recall and observable practice in its own Herdr workspace.

The game never dispatches a shortcut on a player's behalf. Live objectives watch
Herdr state; answers about agents and closing work are recall challenges.
"""
from dataclasses import dataclass
import curses
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace

from . import PLUGIN_ID
from .interface import clean, fit, wrap_cells
from .runtime import ShellError, resolve_context


QUESTIONS = {
    "menu": ("Find any action and see the complete shortcut sheet.", "M opens the searchable Home menu. Press it again to close."),
    "pane-split-up": ("Put a new shell ABOVE this pane.", "U means up; split keys do not need Shift."),
    "pane-split-down": ("Put a new shell BELOW this pane.", "D means down; split keys do not need Shift."),
    "pane-split-left": ("Put a new shell LEFT of this pane.", "L means left; split keys do not need Shift."),
    "pane-split-right": ("Put a new shell RIGHT of this pane.", "R means right; split keys do not need Shift."),
    "pane-swap-up": ("Exchange this pane with its neighbor ABOVE.", "Shift changes a directional split into a swap."),
    "pane-swap-down": ("Exchange this pane with its neighbor BELOW.", "Shift changes a directional split into a swap."),
    "pane-swap-left": ("Exchange this pane with its neighbor to the LEFT.", "Shift changes a directional split into a swap."),
    "pane-swap-right": ("Exchange this pane with its neighbor to the RIGHT.", "Shift changes a directional split into a swap."),
    "pane-rotate": ("Rotate the nearest two-pane split between side by side and stacked.", "J rotates two sibling panes while preserving their processes."),
    "pane-zoom": ("Let this pane fill its tab, then restore its split.", "Z toggles zoom. Press the same key to restore."),
    "pane-close": ("Close only the current pane, with protection for running work.", "X closes a pane. Running or unverified work requires confirmation."),
    "pane-cycle-next": ("Visit the next pane without leaving this tab.", "P cycles panes in the current tab."),
    "pane-cycle-previous": ("Visit the previous pane without leaving this tab.", "Shift reverses P's pane cycle."),
    "tab-new": ("Create another tab in the current workspace.", "T creates a tab in this workspace and directory."),
    "tab-close": ("Close this tab and its panes, with protection for running work.", "W closes a tab. Running work requires confirmation."),
    "tab-previous": ("Visit the previous tab in this workspace.", "PageUp selects the previous tab."),
    "tab-next": ("Visit the next tab in this workspace.", "PageDown selects the next tab."),
    "workspace-new": ("Create an entirely new Herdr workspace.", "Shift promotes T from a new tab to a new workspace."),
    "workspace-close": ("Close this whole workspace, with protection for running work.", "Shift promotes W from closing a tab to closing a workspace."),
    "workspace-previous": ("Visit the previous Herdr workspace.", "Shift promotes PageUp from tabs to workspaces."),
    "workspace-next": ("Visit the next Herdr workspace.", "Shift promotes PageDown from tabs to workspaces."),
    "agent-new": ("Open your default Omarchy agent in a new pane.", "A starts an agent in the current directory and tab."),
    "agent-cycle-next": ("Visit agents by approval, done, working, then idle priority.", "Q follows a stable priority cycle; repeated presses continue through the groups."),
    "agent-cycle-previous": ("Go backward through that same stable agent cycle.", "Shift reverses Q's agent cycle."),
    "launch-git": ("Open Lazygit beside your work in this project's directory.", "V opens Lazygit in a regular pane."),
}

ROUNDS = (
    ("Build your board", ("pane-split-right", "pane-split-down", "pane-split-left", "pane-split-up")),
    ("Arrange and move", ("pane-swap-up", "pane-swap-down", "pane-swap-left", "pane-swap-right",
                           "pane-rotate", "pane-zoom", "pane-cycle-next", "pane-cycle-previous", "pane-close")),
    ("Tabs and spaces", ("tab-new", "tab-close", "tab-previous", "tab-next", "workspace-new",
                         "workspace-close", "workspace-previous", "workspace-next")),
    ("Agents and tools", ("agent-new", "agent-cycle-next", "agent-cycle-previous", "launch-git")),
    ("Use the whole plugin", ("menu",)),
)


@dataclass(frozen=True)
class Mission:
    id: str
    round: str
    prompt: str
    answer: str
    hint: str
    live: bool = False
    options: tuple = ()


def suffix(key):
    return key.replace("SUPER + ALT + ", "").replace("SHIFT + ", "Shift+").replace("Page_Up", "PageUp").replace("Page_Down", "PageDown")


def normalize_answer(value):
    """Accept suffixes or full chords; never accept missing Shift accidentally."""
    value = re.sub(r"[\s_-]+", "", value.casefold())
    parts = value.split("+")
    if parts[:2] in (["super", "alt"], ["alt", "super"]):
        parts = parts[2:]
    aliases = {"pgup": "pageup", "pgdn": "pagedown", "pgdown": "pagedown"}
    return "+".join(aliases.get(part, part) for part in parts)


def missions():
    # One source of truth for shortcut answers; key changes update the game.
    from .desktop import MAPPINGS
    keys = {action: suffix(key) for key, action, _ in MAPPINGS}
    result = []
    live = {"pane-split-right", "pane-split-down", "pane-split-left", "pane-split-up",
            "pane-rotate", "pane-zoom", "pane-cycle-next", "tab-new"}
    for title, actions in ROUNDS:
        for action in actions:
            prompt, hint = QUESTIONS[action]
            result.append(Mission(action, title, prompt, keys[action], hint))
            if action in live:
                result.append(Mission("live-" + action, title, prompt, keys[action], hint, live=True))
    result.extend([
        Mission("feature-close", "Use the whole plugin", "A close key finds a running command. What happens?", "B",
                "A clean shell can close directly; running or unverified work gets a confirmation with Cancel selected.",
                options=("A  It always kills the command", "B  It asks before closing running work", "C  It silently moves the command")),
        Mission("feature-agent", "Use the whole plugin", "Which agent group comes first in the Q cycle?", "C",
                "Approval or input comes first, followed by done, working, then idle.",
                options=("A  Idle", "B  Working", "C  Needs approval or input")),
        Mission("feature-desktop", "Use the whole plugin", "What happens to your ordinary Omarchy shortcuts inside Herdr?", "A",
                "This dedicated Super+Alt family preserves ordinary desktop shortcuts and adds no plain Alt shortcuts.",
                options=("A  They retain their desktop behavior", "B  They become Herdr commands", "C  Plain Alt becomes the prefix")),
        Mission("feature-settings", "Use the whole plugin", "Before applying a setting or native key edit, what can you inspect?", "B",
                "The editor previews the config change before apply/reload, and saved changes can be undone.",
                options=("A  Only the action name", "B  A preview of the configuration change", "C  Nothing; changes are immediate")),
        Mission("feature-theme", "Use the whole plugin", "How does the popup follow your Omarchy terminal theme?", "A",
                "The menu uses your terminal palette. Use terminal colors is available for Herdr's main interface.",
                options=("A  It uses the terminal palette", "B  It overwrites the Omarchy theme", "C  It installs a separate desktop theme")),
    ])
    return result


def open_game(context):
    """Create an isolated place to play; never clean up potentially used panes."""
    from .desktop import popup_environment
    context.validate()
    directory = Path(tempfile.mkdtemp(prefix="herdr-key-quest-"))
    (directory / "README.txt").write_text(
        "Herdr Key Quest practice directory\n\n"
        "These panes were created for the shortcut game. They are left in place\n"
        "when the game exits. Close the workspace yourself when finished; Herdr\n"
        "Shell checks for running work before closing. No real agents are started\n"
        "by the game. Your desktop shortcuts and configuration are unchanged.\n")
    created = context.client.call("workspace.create", label="Herdr Key Quest", cwd=str(directory),
                                  source_workspace_id=context.workspace, focus=True)
    workspace = created["workspace"]["workspace_id"]
    try:
        result = context.client.call("plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="trainer",
                                     placement="split", direction="right", target_pane_id=created["root_pane"]["pane_id"],
                                     cwd=str(directory), focus=True,
                                     env={**popup_environment(), "HERDR_SHELL_GAME_WORKSPACE": workspace})
    except Exception as exc:
        # Another client could have started work in the new shell already.
        # Preserve it, rather than rolling back by closing an entire workspace.
        raise ShellError(f"The practice workspace {workspace} remains available, but Key Quest could not open: {exc}") from exc
    return {**result, "practice_workspace": workspace, "practice_directory": str(directory)}


def _leaf_ids(node):
    if node.get("type") == "pane":
        return {node["pane_id"]}
    return _leaf_ids(node.get("first", {})) | _leaf_ids(node.get("second", {})) if node.get("type") == "split" else set()


def _sibling(node, pane):
    if node.get("type") != "split":
        return None
    for side in ("first", "second"):
        child = node.get(side, {})
        if child.get("type") == "pane" and child.get("pane_id") == pane:
            return node
        found = _sibling(child, pane)
        if found:
            return found
    return None


class Objective:
    """Detect exact local changes without awarding points for unrelated work."""
    def __init__(self, mission, context, before, layout=None):
        self.action = mission.id.removeprefix("live-")
        self.context = context
        self.before = before
        self.tree = layout or {}
        self.panes = {p["pane_id"]: p for p in before["panes"] if p["tab_id"] == context.tab}
        self.tabs = {t["tab_id"] for t in before["tabs"] if t["workspace_id"] == context.workspace}

    def verified(self, after, layout=None):
        origin = next((p for p in after["panes"] if p["pane_id"] == self.context.pane), None)
        if not origin or any(origin.get(key) != value for key, value in
                             (("terminal_id", self.context.terminal), ("tab_id", self.context.tab),
                              ("workspace_id", self.context.workspace))):
            return False
        if after.get("focused_workspace_id") != self.context.workspace:
            return False
        panes = {p["pane_id"]: p for p in after["panes"] if p["tab_id"] == self.context.tab}
        if any(panes.get(pid, {}).get("terminal_id") != p.get("terminal_id") for pid, p in self.panes.items()):
            return False
        focus = after.get("focused_pane_id")
        if self.action.startswith("pane-split-"):
            added = set(panes) - set(self.panes)
            if len(added) != 1 or focus not in added:
                return False
            direction = self.action.removeprefix("pane-split-")
            pair = _sibling((layout or {}).get("root", {}), self.context.pane)
            expected = [focus, self.context.pane] if direction in ("left", "up") else [self.context.pane, focus]
            return bool(pair and pair.get("direction") == ("right" if direction in ("left", "right") else "down")
                        and [pair.get(side, {}).get("pane_id") for side in ("first", "second")] == expected)
        if self.action == "pane-rotate":
            previous = _sibling(self.tree.get("root", {}), self.context.pane)
            current = _sibling((layout or {}).get("root", {}), self.context.pane)
            return bool(previous and current and set(panes) == set(self.panes)
                        and len(_leaf_ids(previous)) == 2 and _leaf_ids(previous) == _leaf_ids(current)
                        and previous.get("direction") != current.get("direction")
                        and previous.get("ratio") == current.get("ratio"))
        if self.action == "pane-zoom":
            return bool(layout and layout.get("zoomed") != self.tree.get("zoomed")
                        and set(panes) == set(self.panes) and focus == self.context.pane)
        if self.action == "pane-cycle-next":
            ids = list(self.panes)
            expected = ids[(ids.index(self.context.pane) + 1) % len(ids)]
            return focus == expected and focus != self.context.pane and set(panes) == set(self.panes)
        if self.action == "tab-new":
            tabs = {t["tab_id"] for t in after["tabs"] if t["workspace_id"] == self.context.workspace}
            return len(tabs - self.tabs) == 1 and self.tabs <= tabs and after.get("focused_tab_id") in tabs - self.tabs
        return False


class Quest:
    def __init__(self, screen, context, deck=None):
        self.screen, self.context = screen, context
        self.deck = missions() if deck is None else deck
        self.index, self.score, self.mastered = 0, 0, set()
        self.missed, self.attempts, self.hinted = [], 0, False
        self.value, self.notice = "", ""
        self.active, self.objective = False, None
        self.stable = 0
        self.help = False
        self.scroll = 0
        self.page_title = ""
        self.scroll_max = 0
        self.accent = curses.A_BOLD
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            self.accent |= curses.color_pair(1)
        screen.keypad(True)
        screen.timeout(250)

    def write(self, y, text, attr=0):
        h, w = self.screen.getmaxyx()
        if 0 <= y < h - 1 and w > 4:
            try:
                self.screen.addstr(y, 2, fit(clean(text), w - 4), attr)
            except curses.error:
                pass

    def page(self, title, lines, footer):
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        if (h < 16 or w < 38) and not self.active:
            self.write(1, "KEY QUEST", self.accent)
            self.write(3, "Resize to 38 × 16 to play.")
            self.write(5, "Your answer is kept. Esc quits.")
            self.screen.refresh()
            return
        self.write(1, "HERDR × OMARCHY   KEY QUEST", self.accent)
        self.write(3, title, self.accent)
        if self.page_title != title:
            self.scroll, self.page_title = 0, title
        wrapped = [part for line in lines for part in wrap_cells(line, max(1, w - 4))]
        visible = max(1, h - 9)
        self.scroll_max = max(0, len(wrapped) - visible)
        self.scroll = max(0, min(self.scroll, self.scroll_max))
        for row, part in enumerate(wrapped[self.scroll:self.scroll + visible], 5):
            self.write(row, part)
        if self.scroll_max:
            self.write(h - 4, f"↑↓ / PageUp PageDown  {self.scroll + 1}–{min(self.scroll + visible, len(wrapped))}/{len(wrapped)}", curses.A_DIM)
        self.write(h - 3, footer, self.accent)
        self.screen.refresh()

    def welcome(self):
        while True:
            self.page("Build muscle memory. Earn your shortcut stars.", [
                "Five rounds teach every Super+Alt shortcut, then the plugin's everyday features.",
                "Recall: type the final key, such as R or Shift+PageDown. Do not press the desktop shortcut during a recall question.",
                "Live practice: press the real shortcut here. The guide verifies the resulting Herdr state and brings you back to this pane.",
                "The guide stays zoomed so it remains readable. Rotation and zoom expose your practice board during their challenges.",
                "Only the practice workspace is scored. Agents and close commands are quiz questions; the game never runs them for you.",
                "If focus moves during practice, return with Ctrl+Alt+arrows or Super+Alt+P. F1 gives a hint; F3 skips a challenge.",
                "Esc quits the game and keeps these practice panes. Close this workspace yourself when finished.",
            ], "Enter Play   Esc Quit")
            key = self.key()
            if self.scroll_key(key):
                continue
            if key in ("\x1b", "\x03"):
                return False
            height, width = self.screen.getmaxyx()
            if height < 16 or width < 38:
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                return True

    def key(self):
        try:
            return self.screen.get_wch()
        except curses.error:
            return None

    def scroll_key(self, key):
        if key == curses.KEY_DOWN:
            self.scroll = min(self.scroll_max, self.scroll + 1)
        elif key == curses.KEY_UP:
            self.scroll = max(0, self.scroll - 1)
        elif key == curses.KEY_NPAGE:
            self.scroll = min(self.scroll_max, self.scroll + 8)
        elif key == curses.KEY_PPAGE:
            self.scroll = max(0, self.scroll - 8)
        else:
            return False
        return True

    def award(self):
        self.mastered.add(self.deck[self.index].id)
        points = max(2, 10 - self.attempts * 2 - int(self.hinted) * 4)
        self.score += points
        self.advance(f"Cleared! +{points} points. Next challenge.")

    def advance(self, notice=""):
        self.index += 1
        self.attempts, self.hinted, self.value = 0, False, ""
        self.active, self.objective, self.help = False, None, False
        self.stable = 0
        self.scroll = 0
        self.notice = notice

    def start_practice(self, mission):
        from .desktop import effective_bindings
        action = mission.id.removeprefix("live-")
        binding = effective_bindings().get(action, {})
        if not binding.get("active"):
            raise ShellError("Shortcut not active: " + binding.get("reason", "The bridge could not verify this shortcut.") +
                             " Enable it in Configure or run herdr-shell desktop install --apply. F3 skips this drill; recall questions still work.")
        # The guide is a regular pane, so its current captured identity is valid.
        self.context.validate()
        before = self.context.client.snapshot()
        if before.get("focused_pane_id") != self.context.pane:
            raise ShellError("Focus the Key Quest pane before starting a challenge.")
        if action == "pane-rotate":
            # Reveal the actual pair for this drill. Native minimum-size ratios
            # can change when rotating an extremely narrow split while zoomed.
            self.context.client.call("pane.zoom", pane_id=self.context.pane, mode="off")
            before = self.context.client.snapshot()
        layout = self.context.client.call("layout.export", tab_id=self.context.tab).get("layout", {}) if action.startswith("pane-split-") or action in ("pane-rotate", "pane-zoom") else None
        if action == "pane-rotate":
            pair = _sibling((layout or {}).get("root", {}), self.context.pane)
            if not pair or len(_leaf_ids(pair)) != 2:
                raise ShellError("This challenge needs two sibling panes. Split this guide pane, then press Enter again, or F3 to skip.")
        if action == "pane-cycle-next" and len([p for p in before["panes"] if p["tab_id"] == self.context.tab]) < 2:
            raise ShellError("Create another pane before this challenge, or F3 to skip.")
        self.objective = Objective(mission, self.context, before, layout)
        self.stable = 0
        self.active = True
        self.notice = "Live! Hold Super+Alt and press " + mission.answer + "."

    def poll_practice(self):
        after = self.context.client.snapshot()
        action = self.objective.action
        layout = self.context.client.call("layout.export", tab_id=self.context.tab).get("layout", {}) if action.startswith("pane-split-") or action in ("pane-rotate", "pane-zoom") else None
        if not self.objective.verified(after, layout):
            self.stable = 0
            return
        # Rotation uses several requests. Do not restore tutorial focus in the
        # middle of that operation after seeing a transient correct layout.
        self.stable += 1
        if self.stable < 2:
            return
        # Return only from a verified local challenge, never from another workspace.
        self.context.validate()
        if self.context.client.snapshot().get("focused_workspace_id") != self.context.workspace:
            return
        self.context.client.call("pane.focus", pane_id=self.context.pane)
        self.context.client.call("pane.zoom", pane_id=self.context.pane, mode="on")
        self.award()

    def draw(self, mission):
        height, width = self.screen.getmaxyx()
        if self.active and (height < 18 or width < 38):
            self.page("LIVE: " + mission.answer, ["Press Super+Alt+" + mission.answer + ".", "The guide returns after verification."], "F3 Skip / Esc Quit")
            return
        progress = f"Challenge {self.index + 1}/{len(self.deck)}  ·  {self.score} points  ·  {len(self.mastered)} cleared"
        lines = [progress, "", mission.prompt, ""]
        if mission.live:
            lines.extend(["LIVE PRACTICE — use the real desktop shortcut.",
                          "Hold Super+Alt and press " + mission.answer + ".",
                          "Challenge armed. Use the real shortcut now." if self.active else "Enter arms the challenge. Then use that shortcut here.",
                          "Correct local changes return focus to this guide."])
            if mission.id == "live-pane-rotate" and not self.active:
                lines.append("Arming reveals the split. Remember J before pressing Enter.")
        elif mission.options:
            lines.extend(mission.options)
            lines.append("Answer A, B, or C: " + self.value)
        else:
            lines.extend(["RECALL — type a key suffix; do not run the shortcut.", "Hold Super+Alt + [ " + self.value + " ]"])
        if self.help:
            lines.extend(["", "Hint: " + mission.hint, "Answer: " + mission.answer])
        if self.notice:
            lines.extend(["", self.notice])
        self.page(mission.round, lines, "Enter Check / Arm   F1 Hint   F3 Skip   Esc Quit")

    def finish(self):
        while True:
            total = len(self.deck) * 10
            stars = 3 if self.score >= total * .85 else 2 if self.score >= total * .55 else 1
            self.page("QUEST COMPLETE   " + "★" * stars, [
                f"{self.score}/{total} points. {len(self.mastered)}/{len(self.deck)} challenges cleared.",
                "Your practice panes are still here. The game does not close them or change your desktop configuration.",
                "Keep using Super+Alt+M as your shortcut sheet. F1 explains an action; F2 shows keybindings. Settings offers previews and undo.",
                f"{len(self.missed)} skipped challenges can be replayed.",
            ], "R Replay skipped   Enter / Esc Finish")
            key = self.key()
            if self.scroll_key(key):
                continue
            if key in ("\x1b", "\x03", "\n", "\r", curses.KEY_ENTER):
                return False
            if key in ("r", "R") and self.missed:
                self.deck, self.missed = self.missed, []
                self.index, self.score, self.mastered = 0, 0, set()
                self.notice = "A fresh round for the challenges you skipped."
                return True

    def run(self):
        if not self.welcome():
            return
        while True:
            if self.index >= len(self.deck):
                if self.finish():
                    continue
                return
            mission = self.deck[self.index]
            if self.active:
                try:
                    self.poll_practice()
                except (ShellError, OSError, KeyError, ValueError) as exc:
                    self.active = False
                    self.notice = str(exc)
                if self.index >= len(self.deck) or self.deck[self.index] is not mission:
                    continue
            self.draw(mission)
            key = self.key()
            if key in ("\x1b", "\x03"):
                return
            height, width = self.screen.getmaxyx()
            if (height < 16 or width < 38) and not self.active:
                continue
            if key == curses.KEY_RESIZE or key is None:
                continue
            if self.scroll_key(key):
                continue
            if key == curses.KEY_F1:
                self.hinted, self.help = True, True
            elif key == curses.KEY_F3:
                if mission.live:
                    try:
                        self.context.validate()
                        if self.context.client.snapshot().get("focused_pane_id") == self.context.pane:
                            self.context.client.call("pane.zoom", pane_id=self.context.pane, mode="on")
                    except (ShellError, OSError, KeyError, ValueError):
                        pass
                self.missed.append(mission)
                self.advance("Skipped. You can replay it after the last round.")
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if mission.live:
                    if not self.active:
                        try:
                            self.start_practice(mission)
                        except (ShellError, OSError, KeyError, ValueError) as exc:
                            self.notice = str(exc)
                elif normalize_answer(self.value) == normalize_answer(mission.answer):
                    self.award()
                else:
                    self.attempts += 1
                    self.notice = "Try again, or F1 for a hint. Shift matters."
                    self.value = ""
            elif not mission.live and key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.value = self.value[:-1]
            elif not mission.live and key == "\x15":
                self.value = ""
            elif not mission.live and isinstance(key, str) and key.isprintable() and len(self.value) < 48:
                self.value += key


def run_game(context=None):
    # A regular plugin pane has its own identity. Do not inherit a popup's saved
    # originating context or the focused pane captured before it was spawned.
    own = os.environ.get("HERDR_PANE_ID")
    if own:
        context = resolve_context(SimpleNamespace(pane=own, active=False))
    if context is None:
        raise ShellError("Key Quest must run in its practice plugin pane.")
    if os.environ.get("HERDR_SHELL_GAME_WORKSPACE") != context.workspace:
        raise ShellError("Key Quest was not opened in its own practice workspace. Run herdr-shell learn.")
    context.validate()
    context.client.call("pane.zoom", pane_id=context.pane, mode="on")
    curses.wrapper(lambda screen: Quest(screen, context).run())
