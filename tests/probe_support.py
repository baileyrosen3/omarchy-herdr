"""Shared synchronization and terminal support for isolated live probes."""
import os
import shutil
import subprocess
import time

from herdr_shell.menu import pane_spacing_supported
from herdr_shell.runtime import ShellError


def eventually(predicate, label="isolated server state", timeout=8, interval=.08):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (ShellError, OSError) as exc:
            last = exc
        time.sleep(interval)
    raise AssertionError(f"Timed out: {label}; last result {last}")


class TmuxSession:
    def __init__(self, socket, target, env=None):
        self.socket = str(socket)
        self.target = target
        source = os.environ if env is None else env
        self.env = {k: v for k, v in source.items() if k not in
                    ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID")}

    def __call__(self, *args):
        result = subprocess.run(["tmux", "-S", self.socket, *args], capture_output=True,
                                text=True, timeout=8, env=self.env)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    def screen(self):
        return self("capture-pane", "-p", "-t", self.target)

    def styled_screen(self):
        return self("capture-pane", "-p", "-e", "-t", self.target)

    def keys(self, *values):
        self("send-keys", "-t", self.target, *values)

    def literal(self, value):
        self("send-keys", "-t", self.target, "-l", value)

    def wait_focus(self, title, label="attached client pane focus", *, popup_absent=None,
                   timeout=8):
        # RPC replies and server snapshots precede client keyboard routing.
        # Only the destination's bold rendered title acknowledges its focus.
        def ready():
            return (f"\x1b[1m {title} \x1b[0m" in self.styled_screen() and
                    (popup_absent is None or popup_absent not in self.screen()))
        return eventually(ready, label, timeout=timeout)


def herdr_details():
    executable = shutil.which("herdr")
    if executable is None:
        raise FileNotFoundError("herdr executable is unavailable")
    return {"herdr_path": os.path.realpath(executable),
            "pane_spacing": pane_spacing_supported()}
