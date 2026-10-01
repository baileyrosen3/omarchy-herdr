"""Key Quest: real shortcut missions in disposable Herdr practice fixtures.

Only compositor-routed chords trigger actions. The guide verifies their result;
ordinary letters never act as shortcut answers.
"""
from dataclasses import dataclass
import curses
import json
import os
from pathlib import Path
import time
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
    "agent-new": ("Practice opening an agent in a new pane.", "A normally starts your default agent. This lesson uses a harmless simulator."),
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


def missions():
    from .desktop import MAPPINGS
    keys = {action: suffix(key) for key, action, _ in MAPPINGS}
    return [Mission(action, title, QUESTIONS[action][0], keys[action], QUESTIONS[action][1], live=True)
            for title, actions in ROUNDS for action in actions]


def open_game(context):
    """Create an isolated place to play; never clean up potentially used panes."""
    from .desktop import popup_environment
    context.validate()
    directory = Path(tempfile.mkdtemp(prefix="herdr-key-quest-"))
    (directory / "README.txt").write_text(
        "Herdr Key Quest practice directory\n\n"
        "These panes were created for the shortcut game. They are left in place\n"
        "when the game exits. Close the workspace yourself when finished; Herdr\n"
        "Shell checks for running work before closing. Practice agents are harmless\n"
        "simulations. Your desktop shortcuts and configuration are unchanged.\n")
    created = context.client.call("workspace.create", label="Herdr Key Quest", cwd=str(directory),
                                  source_workspace_id=context.workspace, focus=True)
    workspace = created["workspace"]["workspace_id"]
    try:
        root = {k: created["root_pane"][k] for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")}
        result = context.client.call("plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="trainer",
                                     placement="split", direction="right", target_pane_id=created["root_pane"]["pane_id"],
                                     cwd=str(directory), focus=True,
                                     env={**popup_environment(), "HERDR_SHELL_GAME_WORKSPACE": workspace,
                                          "HERDR_SHELL_GAME_ROOT": json.dumps(root)})
    except Exception as exc:
        # Another client could have started work in the new shell already.
        # Preserve it, rather than rolling back by closing an entire workspace.
        raise ShellError(f"The practice workspace {workspace} remains available, but Key Quest could not open: {exc}") from exc
    return {**result, "practice_workspace": workspace, "practice_directory": str(directory)}


class Quest:
    def __init__(self, screen, context, deck=None):
        self.screen, self.context = screen, context
        self.deck = missions() if deck is None else deck
        self.index, self.score, self.mastered = 0, 0, set()
        self.missed, self.hinted = [], False
        self.notice, self.help = "", False
        self.active, self.plan = False, None
        self.practice_identities = {(context.pane, context.terminal, context.workspace, context.tab)}
        try:
            root = json.loads(os.environ.get("HERDR_SHELL_GAME_ROOT", "null"))
            self.initial_identities = [] if root is None else [root]
        except ValueError as exc:
            raise ShellError("The game's initial terminal identity is invalid.") from exc
        self.input = None
        self.scroll, self.scroll_max, self.page_title = 0, 0, ""
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
        screen.timeout(100)

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
        if h < 16 or w < 38:
            self.write(1, "KEY QUEST", self.accent)
            self.write(3, "Resize to 38 × 16 to play.")
            self.write(5, "F3 skips. Esc leaves the game.")
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

    def welcome(self):
        while True:
            self.page("Press the shortcut. Watch the action happen.", [
                "26 missions use the real Super+Alt chords. No letter answers.",
                "Enter prepares a labeled practice target. Your chord acts on that target; the guide stays safe.",
                "Watch the split, swap, zoom, close, or navigation happen. The guide returns after verifying the result.",
                "Agents are harmless practice simulations. Lazygit uses a temporary Git repository. Workspace jumps stay in the practice area.",
                "M opens the real menu in a browsing-only practice view; press M again to close it.",
                "F1 explains, F3 skips, and Esc leaves. Practice spaces are kept. Shortcuts must be active in the Omarchy profile.",
            ], "Enter Play   Esc Leave")
            key = self.key()
            if self.scroll_key(key):
                continue
            if key in ("\x1b", "\x03"):
                return False
            h, w = self.screen.getmaxyx()
            if h >= 16 and w >= 38 and key in ("\n", "\r", curses.KEY_ENTER):
                return True

    def advance(self, notice=""):
        self.input.disarm()
        self.index += 1
        self.hinted, self.help = False, False
        self.active, self.plan = False, None
        self.scroll, self.notice = 0, notice

    def award(self):
        self.mastered.add(self.deck[self.index].id)
        points = 6 if self.hinted else 10
        self.score += points
        self.advance(f"Verified! +{points} points. Enter prepares the next mission.")

    def start_practice(self, mission):
        from . import practice
        from .desktop import effective_bindings
        binding = effective_bindings().get(mission.id, {})
        if not binding.get("active"):
            raise ShellError("Shortcut unavailable: " + binding.get("reason", "Cannot verify the desktop bridge.") +
                             " Enable the profile or use F3 to skip. The walkthrough remains available without the bridge.")
        self.context.validate()
        if self.context.client.snapshot().get("focused_pane_id") != self.context.pane:
            raise ShellError("Return to the Key Quest guide before preparing a mission.")
        self.plan = practice.prepare(mission.id, self.context)
        plan = self.plan
        plan.on_adopt = lambda: self.input.update(plan)
        self.remember_practice()
        self.input.arm(self.plan)
        self.active = True
        self.notice = "Ready. Press Super+Alt+" + mission.answer + " — ordinary letters do not count."

    def remember_practice(self):
        if not hasattr(self, "practice_identities"):
            self.practice_identities = {(self.context.pane, self.context.terminal, self.context.workspace, self.context.tab)}
        if self.plan:
            self.practice_identities.update((p["pane_id"], p["terminal_id"], p["workspace_id"], p["tab_id"])
                                            for p in self.plan.pane_identities)

    def return_guide(self):
        self.context.validate()
        snapshot = self.context.client.snapshot()
        current = next((p for p in snapshot["panes"] if p["pane_id"] == snapshot.get("focused_pane_id")), None)
        self.remember_practice()
        if current and tuple(current.get(k) for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")) in self.practice_identities:
            self.context.client.call("workspace.focus", workspace_id=self.context.workspace)
            self.context.client.call("pane.focus", pane_id=self.context.pane)

    def poll_practice(self):
        from . import practice
        notices = self.input.feedback()
        if notices:
            self.notice = notices[-1]
        event = self.input.next_event()
        if event is None:
            return
        action, incoming = event
        try:
            practice.perform(self.plan, action)
            self.remember_practice()
            self.input.update(self.plan)
            # A routed chord and the matching actual state change are required.
            first = practice.verified(self.plan)
            if first:
                time.sleep(.15)
            second = first and practice.verified(self.plan)
            if not second:
                raise ShellError("The shortcut ran, but its expected practice result could not be verified. Enter retries this mission.")
            # Give the action time to remain visible before returning to the guide.
            if self.plan.action != "menu" or self.plan.presses >= self.plan.required_presses:
                time.sleep(.8)
                self.return_guide()
            if self.plan.presses >= self.plan.required_presses:
                self.award()
            else:
                self.notice = f"Verified {self.plan.presses}/{self.plan.required_presses}. Press the same chord again."
        except (ShellError, OSError, KeyError, ValueError) as exc:
            self.notice = str(exc)
            try:
                self.return_guide()
            except (ShellError, OSError, KeyError, ValueError) as focus_error:
                self.notice += f" Guide could not regain focus: {focus_error}"
            self.active = False
            self.input.disarm()
        finally:
            self.input.complete()

    def draw(self, mission):
        lines = [f"{self.index + 1}/{len(self.deck)} missions · {len(self.mastered)} cleared · {self.score} points", "",
                 mission.prompt, "", "Press Super+Alt+" + mission.answer,
                 "Shortcut acts on the labeled practice target."]
        if self.active:
            lines.append(f"Waiting for chord · {self.plan.presses}/{self.plan.required_presses} verified presses")
            if self.plan.instructions:
                lines += ["", self.plan.instructions]
        else:
            lines.append("Enter prepares this mission.")
        if self.help:
            lines += ["", mission.hint]
        if self.notice:
            lines += ["", self.notice]
        self.page(mission.round, lines, "Enter Prepare   F1 Explain   F3 Skip   Esc Leave")

    def finish(self):
        while True:
            total = len(self.deck) * 10
            stars = 3 if self.score >= total * .85 else 2 if self.score >= total * .55 else 1
            self.page("QUEST COMPLETE   " + "★" * stars, [
                f"{self.score}/{total} points. {len(self.mastered)}/{len(self.deck)} missions verified.",
                "You performed the shortcuts and changed real Herdr practice panes, tabs, and workspaces.",
                "Practice spaces remain available. Leave the game to restore normal shortcut actions.",
                f"{len(self.missed)} skipped missions can be replayed.",
            ], "F3 Replay skipped   Enter / Esc Finish")
            key = self.key()
            if self.scroll_key(key):
                continue
            if key in ("\x1b", "\x03", "\n", "\r", curses.KEY_ENTER):
                return False
            if key == curses.KEY_F3 and self.missed:
                self.deck, self.missed = self.missed, []
                self.index, self.score, self.mastered = 0, 0, set()
                self.notice = "A fresh round for the missions you skipped."
                return True

    def run(self):
        from .game_input import Broker
        with Broker(self.context, owned_identities=getattr(self, "initial_identities", ())) as self.input:
            for row in getattr(self, "initial_identities", ()):
                self.practice_identities.add(tuple(row[k] for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")))
            if not self.welcome():
                return
            while True:
                if self.index >= len(self.deck):
                    if self.finish():
                        continue
                    return
                mission = self.deck[self.index]
                if self.active:
                    self.poll_practice()
                    if self.index >= len(self.deck) or self.deck[self.index] is not mission:
                        continue
                self.draw(mission)
                key = self.key()
                if key in ("\x1b", "\x03"):
                    return
                if key == curses.KEY_RESIZE or key is None:
                    continue
                if self.scroll_key(key):
                    continue
                if key == curses.KEY_F1:
                    self.hinted, self.help = True, True
                elif key == curses.KEY_F3:
                    self.missed.append(mission)
                    self.advance("Skipped. Replay it after the last mission.")
                elif key in ("\n", "\r", curses.KEY_ENTER) and not self.active:
                    h, w = self.screen.getmaxyx()
                    if h >= 16 and w >= 38:
                        try:
                            self.start_practice(mission)
                        except (ShellError, OSError, KeyError, ValueError) as exc:
                            self.notice = str(exc)
                elif isinstance(key, str) and key.isprintable():
                    self.notice = "Use the full Super+Alt shortcut. Typing its letter does not perform the mission."


def run_game(context=None):
    own = os.environ.get("HERDR_PANE_ID")
    if own:
        context = resolve_context(SimpleNamespace(pane=own, active=False))
    if context is None:
        raise ShellError("Key Quest must run in its practice plugin pane.")
    if os.environ.get("HERDR_SHELL_GAME_WORKSPACE") != context.workspace:
        raise ShellError("Key Quest was not opened in its own practice workspace. Run herdr-shell learn.")
    context.validate()
    context.client.call("pane.zoom", pane_id=context.pane, mode="off")
    from . import practice
    try:
        curses.wrapper(lambda screen: Quest(screen, context).run())
    finally:
        practice.cleanup(context)
