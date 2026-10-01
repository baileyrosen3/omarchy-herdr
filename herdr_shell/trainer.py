"""Hands-on walkthrough and Speed Run in disposable Herdr fixtures.

Real shortcut receipts trigger verified actions. The untimed walkthrough also
offers Demo through the same scoped broker; ordinary letters never act.
"""
from dataclasses import dataclass
import curses
import hashlib
import json
import os
import random
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


def open_game(context, mode='speed'):
    """Create an isolated place to play; never clean up potentially used panes."""
    from .desktop import popup_environment
    if mode not in ('hands-on', 'speed'):
        raise ShellError('Unknown learning mode.')
    context.validate()
    directory = Path(tempfile.mkdtemp(prefix="herdr-key-quest-"))
    (directory / "README.txt").write_text(
        "Herdr Learning practice directory\n\n"
        "These panes were created for shortcut practice. They are left in place\n"
        "when the game exits. Close the workspace yourself when finished; Herdr\n"
        "Shell checks for running work before closing. Practice agents are harmless\n"
        "simulations. Your desktop shortcuts and configuration are unchanged.\n")
    created = context.client.call("workspace.create", label="Herdr Hands-on" if mode == "hands-on" else "Herdr Speed Run", cwd=str(directory),
                                  source_workspace_id=context.workspace, focus=True)
    workspace = created["workspace"]["workspace_id"]
    try:
        root = {k: created["root_pane"][k] for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")}
        result = context.client.call("plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="trainer",
                                     placement="split", direction="right", target_pane_id=created["root_pane"]["pane_id"],
                                     cwd=str(directory), focus=True,
                                     env={**popup_environment(), "HERDR_SHELL_GAME_WORKSPACE": workspace,
                                          "HERDR_SHELL_GAME_ROOT": json.dumps(root), "HERDR_SHELL_LEARNING_MODE": mode})
    except Exception as exc:
        # Another client could have started work in the new shell already.
        # Preserve it, rather than rolling back by closing an entire workspace.
        raise ShellError(f"The practice workspace {workspace} remains available, but the learning guide could not open: {exc}") from exc
    return {**result, "practice_workspace": workspace, "practice_directory": str(directory)}


class Quest:
    """One safe practice engine with an untimed tour and a timed recall game."""
    def __init__(self, screen, context, deck=None, mode=None):
        self.screen, self.context = screen, context
        self.input = None
        self.practice_identities = {(context.pane, context.terminal, context.workspace, context.tab)}
        try:
            root = json.loads(os.environ.get("HERDR_SHELL_GAME_ROOT", "null"))
            self.initial_identities = [] if root is None else [root]
        except ValueError as exc:
            raise ShellError("The game's initial terminal identity is invalid.") from exc
        self.scroll, self.scroll_max, self.page_title = 0, 0, ""
        self.accent = curses.A_BOLD
        self.demo_rows = set()
        self.desktop_cache = (0, False)
        try:
            curses.curs_set(0)
            curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            self.accent |= curses.color_pair(1)
        screen.keypad(True)
        screen.timeout(100)
        self.reset(mode or os.environ.get("HERDR_SHELL_LEARNING_MODE", "speed"), deck)

    def reset(self, mode, deck=None):
        from .scoring import ActiveTimer, SpeedScore, read_results
        if mode not in ("hands-on", "speed"):
            raise ShellError("Unknown learning mode.")
        self.mode = mode
        self.deck = list(missions() if deck is None else deck)
        if mode == "speed" and deck is None:
            random.SystemRandom().shuffle(self.deck)
        self.index, self.mastered, self.missed = 0, set(), []
        self.timer, self.race = ActiveTimer(120), SpeedScore()
        self.hinted, self.details, self.paused = False, False, False
        self.notice, self.suspension, self.phase = "", "", "next"
        self.active, self.plan, self.advance_pending = False, None, False
        self.effect_until, self.response_started_at = 0, 0
        self.failures, self.saved_result, self.wrong_seen = 0, None, 0
        self.finished_reason = ""
        mapping = json.dumps([(m.id, m.answer) for m in missions()], sort_keys=True)
        self.profile = hashlib.sha256(mapping.encode()).hexdigest()
        try:
            self.previous = read_results(self.profile)
        except (OSError, ValueError):
            self.previous = {"best": None}

    def usable(self):
        h, w = self.screen.getmaxyx()
        return h >= 16 and w >= 38

    def write(self, y, text, attr=0):
        h, w = self.screen.getmaxyx()
        if 0 <= y < h - 1 and w > 4:
            try:
                self.screen.addstr(y, 2, fit(clean(text), w - 4), attr)
            except curses.error:
                pass

    def dialog_write(self, y, x, text, attr=0):
        h, w = self.screen.getmaxyx()
        if 0 <= y < h - 1 and 0 <= x < w - 1:
            try:
                self.screen.addstr(y, x, fit(clean(text), w - x - 1), attr)
            except curses.error:
                pass

    def page(self, title, lines, footer):
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        self.demo_rows.clear()
        if not self.usable():
            self.write(1, "HERDR LEARNING", self.accent)
            self.write(3, "Resize to 38 × 16. Clock paused.")
            self.write(5, "Esc leaves. F3 skips.")
            self.screen.refresh()
            return
        self.write(1, "HERDR × OMARCHY", self.accent)
        if self.mode == "speed":
            left = int(self.timer.remaining() + .999)
            self.write(2, f"{left // 60:02}:{left % 60:02} left · {self.race.points} pts · streak {self.race.streak}", self.accent)
        else:
            self.write(2, "Hands-on walkthrough · no timer", curses.A_DIM)
        self.write(3, title, self.accent)
        if self.page_title != title:
            self.scroll, self.page_title = 0, title
        wrapped = [part for line in lines for part in wrap_cells(line, max(1, w - 4))]
        visible = max(1, h - 9)
        self.scroll_max = max(0, len(wrapped) - visible)
        self.scroll = max(0, min(self.scroll, self.scroll_max))
        for row, part in enumerate(wrapped[self.scroll:self.scroll + visible], 5):
            self.write(row, part, self.accent if part.startswith("▶ Demo") else 0)
            if part.startswith("▶ Demo"):
                self.demo_rows.add(row)
        if self.scroll_max:
            self.write(h - 4, "↑↓ / PageUp PageDown scroll", curses.A_DIM)
        hints = [hint.strip() for hint in footer.split("  ") if hint.strip()]
        if len(hints) > 1 and w < 78:
            # Keep Escape discoverable even in a narrow practice terminal.
            primary = [hint for hint in hints if hint.startswith(("Space", "F1", "Esc"))]
            secondary = [hint for hint in hints if hint not in primary]
            self.write(h - 3, " · ".join(primary), self.accent)
            self.write(h - 2, " · ".join(secondary), self.accent)
        else:
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
            if self.mode == "hands-on":
                title = "Try the keys. Watch their effects."
                lines = ["26 ordered exercises show each full shortcut and explain it.",
                         "Press the actual chord, click Demo, or use F4. The next target prepares automatically.",
                         "F2 repeats an exercise; F3 skips. Space pauses. No timer or score."]
            else:
                title = "Speed Run · recall, speed, accuracy"
                lines = ["26 shuffled missions; 120 seconds of active response time.",
                         "Read the goal and press its real shortcut. Answers stay hidden; F1 reveals an assisted hint and pauses the clock.",
                         "Correct: 100 base + up to 100 speed + streak bonus. Wrong chords cost 25 and break the streak.",
                         "Setup, verification and animations cost no time. Space pauses; F3 skips. Only a full unassisted run competes for your personal best."]
            lines += ["Agents are harmless simulations; Git uses a private temporary repository. Close targets are disposable. Your work stays outside practice.",
                      "The menu opens for real: repeat its chord to close it. Zoom uses two presses; agent cycles use four."]
            self.page(title, lines, "Space Start   Esc Leave")
            key = self.key()
            if self.scroll_key(key):
                continue
            if key in ("\x1b", "\x03"):
                return False
            if self.usable() and key in (" ", "\n", "\r", curses.KEY_ENTER):
                return True

    def desktop_focused(self):
        from . import desktop
        now = time.monotonic()
        if now < self.desktop_cache[0]:
            return self.desktop_cache[1]
        try:
            window = json.loads(desktop.hypr("-j", "activewindow"))
            clients = desktop.find_clients(window.get("pid", 0))
            result = len(clients) == 1 and Path(desktop.socket_for(*clients[0])).resolve() == Path(self.context.socket).resolve()
        except (ShellError, OSError, ValueError, KeyError, TypeError):
            result = False
        self.desktop_cache = (now + .25, bool(result))
        return bool(result)

    def visible_context(self):
        if not self.usable():
            return False, "Resize to 38 × 16; the clock is paused."
        if self.mode == "speed" and not self.desktop_focused():
            return False, "Focus this Herdr window; the clock is paused."
        snapshot = self.context.client.snapshot()
        pane = next((p for p in snapshot["panes"] if p["pane_id"] == snapshot.get("focused_pane_id")), None)
        if pane and (pane["pane_id"], pane["terminal_id"], pane["workspace_id"], pane["tab_id"]) == (
                self.context.pane, self.context.terminal, self.context.workspace, self.context.tab):
            return True, ""
        if self.plan and self.plan.action == "menu" and self.plan.presses == 1 and pane and (
                pane["pane_id"], pane["terminal_id"], pane["workspace_id"], pane["tab_id"]) == (
                self.plan.target.pane, self.plan.target.terminal, self.plan.target.workspace, self.plan.target.tab):
            from .desktop import menu_running
            if menu_running(self.context.socket) == self.plan.expected.get("menu_marker"):
                return True, ""
        return False, "Return to the guide; the clock is paused."

    def remember_practice(self):
        if self.plan:
            self.practice_identities.update((p["pane_id"], p["terminal_id"], p["workspace_id"], p["tab_id"])
                                            for p in self.plan.pane_identities)

    def return_guide(self):
        self.context.validate()
        self.remember_practice()
        snapshot = self.context.client.snapshot()
        current = next((p for p in snapshot["panes"] if p["pane_id"] == snapshot.get("focused_pane_id")), None)
        if current and tuple(current.get(k) for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")) in self.practice_identities:
            self.context.client.call("workspace.focus", workspace_id=self.context.workspace)
            self.context.client.call("pane.focus", pane_id=self.context.pane)

    def start_practice(self, mission):
        from . import practice
        from .desktop import effective_bindings
        binding = effective_bindings().get(mission.id, {})
        if self.mode == "speed" and not binding.get("active"):
            raise ShellError("Shortcut unavailable: " + binding.get("reason", "Cannot verify the desktop bridge.") + " F2 retries; F3 skips without a timeout penalty.")
        self.context.validate()
        if self.context.client.snapshot().get("focused_pane_id") != self.context.pane:
            raise ShellError("Return to the learning guide before preparing a target.")
        self.plan = practice.prepare(mission.id, self.context, display_chord=self.mode == "hands-on")
        plan = self.plan
        plan.on_adopt = lambda: self.input.update(plan)
        self.remember_practice()
        self.active, self.phase = True, "ready"
        if self.mode == "hands-on" and not binding.get("active"):
            self.notice = "Desktop chord unavailable. Click Demo or press F4 to see the action."
        self.draw(mission)
        self.wrong_seen = self.input.metrics()["wrong_count"]
        self.input.feedback()  # Old feedback belongs to the previous target.
        self.input.arm(plan)
        self.response_started_at = time.monotonic()
        if self.mode == "speed":
            self.timer.arm(self.response_started_at)
            if self.hinted:
                self.timer.suspend(self.response_started_at)

    def prepare_next(self):
        if self.paused:
            return
        visible, reason = self.visible_context()
        if not visible:
            self.suspension = reason
            return
        self.suspension = ""
        mission = self.deck[self.index]
        self.phase = "preparing"
        self.page(f"{self.index + 1}/{len(self.deck)} missions · preparing", [mission.prompt, "Setting up a disposable target…"], "Esc Leave")
        try:
            self.start_practice(mission)
        except (ShellError, OSError, KeyError, ValueError) as exc:
            self.failures += 1
            self.phase, self.active, self.notice = "blocked", False, str(exc)
            self.input.disarm()

    def sync_visibility(self):
        if self.phase != "ready":
            return
        if not self.paused and self.plan.action == "menu" and self.plan.presses == 1:
            from .desktop import menu_running
            if menu_running(self.context.socket) is None:
                # Esc can dismiss the real popup outside the guide. Recover
                # only from our exact owned fixture, never unrelated work.
                snapshot = self.context.client.snapshot()
                current = next((p for p in snapshot["panes"] if p["pane_id"] == snapshot.get("focused_pane_id")), None)
                target = self.plan.target
                if current and tuple(current.get(k) for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")) == (
                        target.pane, target.terminal, target.workspace, target.tab):
                    self.input.disarm()
                    if self.mode == "speed":
                        self.timer.cancel_response(refund=True)
                    self.failures += 1
                    self.phase, self.active = "blocked", False
                    self.notice = "Practice menu closed early. F2 prepares a fresh target; F3 skips."
                    try:
                        self.return_guide()
                    except (ShellError, OSError, KeyError, ValueError):
                        self.notice += " Return to the guide."
                    return
        visible, reason = (False, "Paused.") if self.paused else self.visible_context()
        suspended = self.paused or not visible
        if suspended and not self.suspension:
            self.input.disarm()
            # Disarming takes the broker lock before draining mistakes, so
            # receipts accepted just before the pause keep their fair penalty.
            self.feedback_metrics()
            if self.mode == "speed":
                self.timer.suspend()
        if suspended:
            self.suspension = "Paused. Space resumes." if self.paused else reason
        elif self.suspension:
            self.wrong_seen = self.input.metrics()["wrong_count"]
            self.input.feedback()
            self.input.arm(self.plan)
            self.response_started_at = time.monotonic()
            if self.mode == "speed" and not self.hinted:
                self.timer.resume(self.response_started_at)
            self.suspension = ""

    def feedback_metrics(self):
        metrics = self.input.metrics()
        count = metrics["wrong_count"]
        delta = max(0, count - self.wrong_seen)
        self.wrong_seen = count
        if self.mode == "speed" and self.phase == "ready" and not self.suspension and self.timer.armed:
            events = metrics["wrong_events"]
            eligible = sum(not self.timer.expired(max(e["received_at"], self.response_started_at)) for e in events)
            # The queue is bounded; every extra eligible press still counts.
            if delta > len(events) and not self.timer.expired():
                eligible += delta - len(events)
            for _ in range(min(delta, eligible)):
                deduction = self.race.mistake()
                self.notice = f"Wrong chord · −{deduction} points · streak reset. F1 reveals the answer."
        elif self.mode == "hands-on":
            feedback = self.input.feedback()
            if feedback:
                self.notice = feedback[-1]
        if self.mode == "speed":
            self.input.feedback()

    def poll_practice(self):
        self.feedback_metrics()
        event = self.input.next_event()
        if event is None:
            return
        action, incoming = event
        received = max(self.input.last_received_at or time.monotonic(), self.response_started_at)
        if self.mode == "speed" and self.timer.expired(received):
            self.end_run("TIME UP")
            self.input.complete()
            return
        duration = self.timer.response(received) if self.mode == "speed" else 0
        self.perform_step(action, duration)

    def perform_step(self, action, duration=0):
        from . import practice
        try:
            practice.perform(self.plan, action)
            self.remember_practice()
            self.input.update(self.plan)
            valid = practice.verified(self.plan)
            if valid:
                time.sleep(.08)
                valid = practice.verified(self.plan)
            if not valid:
                raise ShellError("The action result could not be verified. F2 retries with a new target; no points were awarded.")
            points = self.race.award(duration, hinted=self.hinted) if self.mode == "speed" else 0
            if self.mode == "speed":
                # A later setup failure must never refund a verified response.
                self.timer.cancel_response()
            self.notice = (f"Verified · +{points} points · {duration:.2f}s" if self.mode == "speed" else "Verified! Watch the effect; the next exercise starts automatically.")
            self.advance_pending = self.plan.presses >= self.plan.required_presses
            if self.advance_pending:
                self.mastered.add(self.deck[self.index].id)
            if self.plan.action == "menu" and not self.advance_pending:
                # The actual menu stays visible until the second chord.
                self.response_started_at = time.monotonic()
                if self.mode == "speed":
                    self.timer.arm(self.response_started_at)
                    if self.hinted:
                        self.timer.suspend(self.response_started_at)
            else:
                self.input.disarm()
                self.phase = "effect"
                self.effect_until = time.monotonic() + (.35 if self.mode == "speed" else 1.1)
        except (ShellError, OSError, KeyError, ValueError) as exc:
            self.failures += 1
            if self.mode == "speed":
                self.timer.cancel_response(refund=True)
            self.notice, self.phase, self.active = str(exc), "blocked", False
            self.input.disarm()
            try:
                self.return_guide()
            except (ShellError, OSError, KeyError, ValueError):
                pass
        finally:
            self.input.complete()

    def finish_effect(self):
        if self.phase != "effect" or time.monotonic() < self.effect_until:
            return
        try:
            self.return_guide()
        except (ShellError, OSError, KeyError, ValueError) as exc:
            self.failures += 1
            self.phase, self.active, self.notice = "blocked", False, "Could not return to the guide: " + str(exc)
            self.input.disarm()
            return
        if self.advance_pending:
            self.advance()
        else:
            self.phase = "ready"
            self.draw(self.deck[self.index])
            self.input.arm(self.plan)
            self.response_started_at = time.monotonic()
            if self.mode == "speed":
                self.timer.arm(self.response_started_at)
                if self.hinted:
                    self.timer.suspend(self.response_started_at)

    def advance(self, *, skipped=False):
        self.input.disarm()
        if self.mode == "speed":
            self.timer.cancel_response()
        if skipped:
            self.missed.append(self.deck[self.index])
            self.race.streak = 0
            self.notice = "Skipped. The next available mission prepares automatically."
        self.index += 1
        self.hinted, self.details = False, False
        self.active, self.plan, self.phase = False, None, "next"
        self.scroll, self.suspension = 0, ""

    def close_owned_menu(self):
        if self.plan and self.plan.action == "menu" and self.plan.presses == 1:
            from .desktop import menu_running
            if menu_running(self.context.socket) == self.plan.expected.get("menu_marker"):
                self.context.client.call("popup.close")

    def end_run(self, reason):
        self.input.disarm()
        self.timer.suspend()
        self.finished_reason = reason
        try:
            self.close_owned_menu()
            self.return_guide()
        except (ShellError, OSError, KeyError, ValueError) as exc:
            self.notice = "The run has stopped. Return to the guide to view results: " + str(exc)
        self.phase, self.active = "finished", False

    def demo(self):
        if self.mode != "hands-on" or self.phase != "ready" or self.suspension:
            return
        from .game_input import route
        result = route(self.plan.action, self.context)
        if result is None or result.get("game") not in ("queued", "busy"):
            self.notice = "Demo is not ready. Return to the guide or use F2 to retry."
        elif result.get("game") == "busy":
            self.notice = "Wait for the current action to finish, then try Demo again."

    def draw(self, mission):
        lines = [mission.prompt, ""]
        if self.mode == "hands-on":
            lines += ["▶ Demo  Super+Alt+" + mission.answer, mission.hint]
        elif self.hinted:
            lines += ["Assisted hint · Super+Alt+" + mission.answer, mission.hint, "Clock paused for this assisted response."]
        else:
            lines.append("Recall the full Super+Alt shortcut. F1 reveals it.")
        if self.plan:
            lines.append(f"{self.plan.presses}/{self.plan.required_presses} verified presses")
            if self.mode == "hands-on":
                lines += ["", self.plan.instructions]
        if self.details:
            lines += ["", "Close drills use fresh idle shells. Normal closes confirm running work. Native Ctrl+Alt+arrows focus panes; Shift resizes. Settings and themes remain in the control menu."]
        if self.suspension or self.paused:
            lines += ["", self.suspension or "Paused. Space resumes."]
        elif self.phase == "ready":
            lines.append("Ready · press your chord.")
        elif self.phase == "blocked":
            lines.append("Target unavailable · F2 Retry · F3 Skip")
        if self.notice:
            lines += ["", self.notice]
        footer = "Space Pause  F1 Help  F2 Replay  F3 Skip  F4 Demo  Esc" if self.mode == "hands-on" else "Space Pause  F1 Hint  F2 Retry  F3 Skip  Esc"
        self.page(f"{self.index + 1}/{len(self.deck)} missions · {mission.round}", lines, footer)

    def save_score(self):
        from .scoring import save_result
        if self.saved_result is not None:
            return
        completed = len(self.mastered) == len(missions()) and not self.missed
        eligible = completed and self.failures == 0 and self.race.hinted_correct == 0
        try:
            self.saved_result = save_result(self.profile, self.race.summary(), self.timer.elapsed(), eligible=eligible, completed=completed)
        except (OSError, ValueError) as exc:
            self.saved_result = {"best": self.previous.get("best")}
            self.notice = "Could not save this local score: " + str(exc)

    def activity_choices(self):
        from . import onboarding, practice
        self.input.disarm()
        self.timer.suspend()
        self.close_owned_menu()
        self.return_guide()
        practice.cleanup(self.context)
        completed = self.mode == "hands-on" and self.index >= len(self.deck)
        if completed:
            try:
                onboarding.mark_learning_completed()
            except OSError:
                self.notice = "Walkthrough finished; could not save this device's welcome preference."
        facade = SimpleNamespace(screen=self.screen, write=self.dialog_write, accent=self.accent,
                                 selection=self.accent | curses.A_REVERSE, error=self.accent, notice="")
        self.context.client.call("pane.zoom", pane_id=self.context.pane, mode="on")
        self.screen.timeout(-1)
        try:
            choice = onboarding.welcome(facade, title="Hands-on complete" if completed else "Welcome to Herdr Shell",
                                        completed=True if completed else None)
            while choice == "walkthrough":
                choice = onboarding.walkthrough(facade)
            return "hands-on" if choice == "hands-on" else "speed" if choice == "game" else None
        finally:
            self.screen.timeout(100)
            self.context.client.call("pane.zoom", pane_id=self.context.pane, mode="off")

    def finish(self):
        if self.mode == "hands-on":
            return self.activity_choices()
        self.save_score()
        while True:
            summary = self.race.summary()
            best = (self.saved_result or {}).get("best")
            lines = [f"{summary['points']} points · {len(self.mastered)}/{len(self.deck)} missions verified",
                     f"Accuracy {summary['accuracy']:.0f}% · correct {summary['correct']} · wrong {summary['wrong']}",
                     f"Best streak {summary['best_streak']} · active response time {self.timer.elapsed():.1f}s",
                     "Average response " + (f"{summary['average_time']:.2f}s" if summary['average_time'] is not None else "—"),
                     f"Skipped {len(self.missed)} · assisted presses {summary['hinted_correct']}",
                     "Personal best " + (str(best['points']) if best else "— finish all 26 without hints or skips to set one."),
                     "Practice spaces remain. Demo agents stop when you leave."]
            if self.notice:
                lines += ["", self.notice]
            self.page(self.finished_reason or "SPEED RUN COMPLETE", lines, "F2 Play again   Space Learning choices   Esc Exit")
            key = self.key()
            if self.scroll_key(key):
                continue
            if key in ("\x1b", "\x03", "\n", "\r", curses.KEY_ENTER):
                return None
            if self.usable() and key == curses.KEY_F2:
                from . import practice
                practice.cleanup(self.context)
                return "speed"
            if self.usable() and key == " ":
                return self.activity_choices()

    def handle_key(self, key):
        if key in ("\x1b", "\x03"):
            return False
        if key is None or key == curses.KEY_RESIZE or self.scroll_key(key):
            return True
        if key == " ":
            self.paused = not self.paused
            if self.phase == "ready":
                self.sync_visibility()
        elif key == curses.KEY_F1:
            self.details = not self.details
            if self.mode == "speed" and self.phase == "ready":
                self.hinted = True
                self.timer.suspend()
        elif key == curses.KEY_F2 and (self.mode == "hands-on" or self.phase == "blocked"):
            self.input.disarm()
            if self.mode == "speed":
                self.timer.cancel_response()
            self.active, self.plan, self.phase = False, None, "next"
            self.hinted, self.details, self.suspension = False, False, ""
            self.advance_pending = False
            self.notice = "Preparing a fresh target for this exercise."
        elif key == curses.KEY_F3:
            self.close_owned_menu()
            self.return_guide()
            self.advance(skipped=True)
        elif key == curses.KEY_F4:
            self.demo()
        elif key == curses.KEY_MOUSE and self.mode == "hands-on":
            try:
                _, x, y, _, state = curses.getmouse()
                if 2 <= x < self.screen.getmaxyx()[1] - 2 and y in self.demo_rows and state & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED):
                    self.demo()
            except curses.error:
                pass
        elif isinstance(key, str) and key.isprintable():
            self.notice = "Use the actual shortcut. Typing its letter does not perform the mission."
        return True

    def run(self):
        from .game_input import Broker
        with Broker(self.context, owned_identities=self.initial_identities) as self.input:
            for row in self.initial_identities:
                self.practice_identities.add(tuple(row[k] for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")))
            if not self.welcome():
                return
            while True:
                if self.index >= len(self.deck) or self.phase == "finished":
                    self.input.disarm()
                    self.timer.suspend()
                    choice = self.finish()
                    if choice is None:
                        return
                    # A replay gets a fresh guide and finite ownership record.
                    # Preserve old practice panes rather than closing them or
                    # growing a broker's historical scope without a bound.
                    self.input.close()
                    open_game(self.context, mode=choice)
                    return
                if self.phase == "ready" and not self.suspension:
                    # Consume receipt timestamps before committing any later
                    # resize/focus/pause observation or checking the deadline.
                    self.poll_practice()
                if self.phase == "effect":
                    self.finish_effect()
                if self.index >= len(self.deck):
                    continue
                if self.phase == "next":
                    self.prepare_next()
                self.sync_visibility()
                if self.mode == "speed" and self.phase == "ready" and not self.suspension and self.timer.expired():
                    self.poll_practice()
                    if self.phase == "ready" and self.timer.expired():
                        self.end_run("TIME UP")
                        continue
                self.draw(self.deck[self.index])
                key = self.key()
                if self.phase == "ready" and not self.suspension:
                    self.poll_practice()
                if self.phase == "finished":
                    continue
                if not self.handle_key(key):
                    self.close_owned_menu()
                    if self.mode == "speed":
                        self.timer.suspend()
                        self.save_score()
                    return


def run_game(context=None):
    own = os.environ.get("HERDR_PANE_ID")
    if own:
        context = resolve_context(SimpleNamespace(pane=own, active=False))
    if context is None:
        raise ShellError("Learning must run in its practice plugin pane.")
    if os.environ.get("HERDR_SHELL_GAME_WORKSPACE") != context.workspace:
        raise ShellError("Learning was not opened in its own practice workspace. Run herdr-shell learn.")
    context.validate()
    context.client.call("pane.zoom", pane_id=context.pane, mode="off")
    from . import practice
    try:
        curses.wrapper(lambda screen: Quest(screen, context).run())
    finally:
        practice.cleanup(context)
