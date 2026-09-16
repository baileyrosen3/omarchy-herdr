"""Consistent, resize-aware dialogs for the keyboard menu."""
import curses

from .interface import cell_width, clean, fit, wrap_cells

CANCEL = object()
RESET = object()
ENTER = ("\n", "\r", curses.KEY_ENTER)
ESCAPE = ("\x1b", "\x03")


class Frame:
    def __init__(self, menu, title, height=20, width=94):
        self.menu = menu
        screen_h, screen_w = menu.screen.getmaxyx()
        self.small = screen_h < 16 or screen_w < 46
        self.width = max(1, min(width, screen_w - 4))
        self.height = max(1, min(height, screen_h - 2))
        self.x = max(0, (screen_w - self.width) // 2)
        self.y = max(0, (screen_h - self.height) // 2)
        self.inner = self.width - 6
        self.top = 5
        self.bottom = self.height - 4
        menu.screen.erase()
        if self.small:
            menu.write(1, 2, "Enlarge the terminal to 46 × 16.", menu.accent)
            menu.write(3, 2, "Esc cancels. Your input is kept on resize.")
            return
        self.text(0, "┌" + "─" * (self.width - 2) + "┐", curses.A_DIM, x=0, width=self.width)
        self.text(self.height - 1, "└" + "─" * (self.width - 2) + "┘", curses.A_DIM, x=0, width=self.width)
        for y in range(1, self.height - 1):
            self.text(y, "│", curses.A_DIM, x=0, width=1)
            self.text(y, "│", curses.A_DIM, x=self.width - 1, width=1)
        self.text(2, title, menu.accent)
        self.text(3, "─" * self.inner, curses.A_DIM)

    def text(self, y, value, attr=0, *, x=3, width=None):
        if not self.small:
            self.menu.write(self.y + y, self.x + x, fit(value, self.inner if width is None else width), attr)

    def footer(self, value):
        self.text(self.height - 2, value, self.menu.accent)

    def key(self):
        self.menu.screen.refresh()
        return self.menu.screen.get_wch()


def prompt(menu, title, initial="", *, help_text="", error="", reset=False):
    value = clean(initial)
    cursor = len(value)
    start = 0
    while True:
        frame = Frame(menu, title)
        if not frame.small:
            for i, line in enumerate(wrap_cells(error or help_text, frame.inner)[:2]):
                frame.text(5 + i, line, menu.error if error else curses.A_NORMAL)
            # Scroll the input around its cursor instead of hiding the end.
            start = min(start, cursor)
            room = frame.inner - 2
            while cell_width(value[start:cursor]) >= room and start < cursor:
                start += 1
            frame.text(8, fit(value[start:] + " ", room, True), menu.selection)
            column = cell_width(value[start:cursor])
            frame.text(8, value[cursor:cursor + 1] or " ", curses.A_REVERSE | curses.A_BOLD,
                       x=3 + column, width=max(1, cell_width(value[cursor:cursor + 1])))
            frame.text(9, "← → Move   Home / End   Ctrl+U Clear", curses.A_DIM)
            if reset:
                frame.text(10, "Ctrl+D Restore inherited default", curses.A_DIM)
            frame.footer("Enter Review   Esc Cancel")
        key = frame.key()
        if key in ESCAPE:
            return CANCEL
        if frame.small or key == curses.KEY_RESIZE:
            continue
        if key in ENTER:
            return value
        if key == "\x04" and reset:
            return RESET
        if key in (curses.KEY_LEFT, "\x02"):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_RIGHT, "\x06"):
            cursor = min(len(value), cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            cursor = 0
        elif key in (curses.KEY_END, "\x05"):
            cursor = len(value)
        elif key == "\x15":
            value, cursor, start = "", 0, 0
        elif key == "\x0b":
            value = value[:cursor]
        elif key == "\x17":
            end = len(value[:cursor].rstrip())
            begin = value.rfind(" ", 0, end) + 1
            value, cursor = value[:begin] + value[cursor:], begin
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b") and cursor:
            value, cursor = value[:cursor - 1] + value[cursor:], cursor - 1
        elif key == curses.KEY_DC:
            value = value[:cursor] + value[cursor + 1:]
        elif isinstance(key, str) and key.isprintable():
            value = value[:cursor] + key + value[cursor:]
            cursor += len(key)


def choose(menu, title, options, selected=0, *, help_text="", footer="↑↓ Select  Enter Review  Esc Cancel"):
    """Options are (value, readable label); CANCEL is distinct from False."""
    selected = max(0, min(selected, len(options) - 1))
    while True:
        frame = Frame(menu, title, height=min(30, 13 + len(options)))
        if not frame.small:
            help_lines = wrap_cells(help_text, frame.inner)[:2]
            for i, line in enumerate(help_lines):
                frame.text(5 + i, line, curses.A_DIM)
            top = 6 + len(help_lines)
            visible = max(1, frame.bottom - top)
            offset = max(0, selected - visible + 1)
            for index, (_, label) in enumerate(options[offset:offset + visible], offset):
                active = index == selected
                frame.text(top + index - offset, fit(("› " if active else "  ") + label, frame.inner, True),
                           menu.selection if active else curses.A_NORMAL)
            frame.footer(footer)
        key = frame.key()
        if key in ESCAPE:
            return CANCEL
        if frame.small or key == curses.KEY_RESIZE:
            continue
        if key in ENTER:
            return options[selected][0]
        if key in (curses.KEY_UP, curses.KEY_BTAB):
            selected = (selected - 1) % len(options)
        elif key in (curses.KEY_DOWN, "\t"):
            selected = (selected + 1) % len(options)
        elif key == curses.KEY_HOME:
            selected = 0
        elif key == curses.KEY_END:
            selected = len(options) - 1


def document(menu, title, lines, *, apply=False):
    """Wrap long values; all content remains reachable with paging."""
    offset = 0
    while True:
        frame = Frame(menu, title, height=44, width=110)
        if not frame.small:
            wrapped = []
            for line in lines:
                attr = menu.accent if line.startswith("+") else menu.error if line.startswith("-") else curses.A_NORMAL
                wrapped.extend((part, attr) for part in wrap_cells(line, frame.inner))
            visible = max(1, frame.bottom - frame.top)
            offset = max(0, min(offset, max(0, len(wrapped) - visible)))
            for y, (line, attr) in enumerate(wrapped[offset:offset + visible], frame.top):
                frame.text(y, line, attr)
            end = min(len(wrapped), offset + visible)
            frame.text(frame.height - 3, f"{offset + 1 if wrapped else 0}–{end} of {len(wrapped)} lines · ↑↓ / PgUp PgDn", curses.A_DIM)
            frame.footer("Enter Apply + reload   Esc Back" if apply else "Enter / Esc Back")
        key = frame.key()
        if key in ESCAPE:
            return False
        if frame.small or key == curses.KEY_RESIZE:
            continue
        if key in ENTER:
            return True
        if key == curses.KEY_DOWN:
            offset += 1
        elif key == curses.KEY_UP:
            offset -= 1
        elif key == curses.KEY_NPAGE:
            offset += visible
        elif key == curses.KEY_PPAGE:
            offset -= visible
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = len(wrapped)
