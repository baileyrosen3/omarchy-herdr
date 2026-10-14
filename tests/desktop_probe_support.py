"""Private popup-surface observations for the opt-in desktop input probe."""
import json
import os
from pathlib import Path
import time

from tests.probe_support import eventually


def read_popup_observation(directory, pid):
    try:
        return json.loads((Path(directory) / (str(pid) + ".json")).read_text())
    except (OSError, ValueError):
        return None


def wait_for_confirmation_ui(directory, processes, page):
    """Require the current popup's actual refreshed confirmation before input."""
    if page not in ("confirm-pane-close", "confirm-tab-close", "confirm-workspace-close"):
        raise ValueError("Not a close confirmation page: " + page)
    kind = page.removeprefix("confirm-").removesuffix("-close")

    def ready():
        current = processes()
        if len(current) != 1 or next(iter(current.values())) != page:
            return None
        pid = next(iter(current))
        observation = read_popup_observation(directory, pid)
        if not isinstance(observation, dict) or observation.get("pid") != pid or observation.get("page") != page:
            return None
        title, text = observation.get("title", ""), observation.get("text", "")
        if (observation.get("phase") == "awaiting-input" and title.startswith("Close " + kind + " ")
                and "Closing this " + kind + " stops " in text
                and "Cancel — keep the " + kind in text and "Esc Cancel" in text):
            return observation
        return None
    return eventually(ready, "rendered close confirmation " + page)


def install_popup_observer(directory):
    """Observe the real refreshed curses surface and keys in fixture popups only.

    Installed by a temporary sitecustomize module in the disposable server's
    environment. This does not install anything in the user's Python or desktop
    configuration and does not emit input. Frame.key keeps its original refresh
    followed by get_wch ordering.
    """
    from herdr_shell.dialogs import Frame

    output = Path(directory) / (str(os.getpid()) + ".json")
    received = []

    def publish(phase, title="", text=""):
        document = {"pid": os.getpid(), "page": os.environ.get("HERDR_SHELL_PAGE"),
                    "phase": phase, "time_ns": time.monotonic_ns(), "title": title,
                    "text": text, "received": received}
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(document))
        temporary.replace(output)

    publish("starting")

    def observed_key(frame):
        screen = frame.menu.screen
        screen.refresh()
        height, width = screen.getmaxyx()
        text = "\n".join(screen.instr(row, 0, width).decode("utf-8", errors="replace").rstrip()
                         for row in range(height))
        title = "" if frame.small else screen.instr(frame.y + 2, frame.x + 3, frame.inner).decode(
            "utf-8", errors="replace").strip()
        publish("awaiting-input", title, text)
        key = screen.get_wch()
        received.append({"key": key, "time_ns": time.monotonic_ns()})
        publish("received-input", title, text)
        return key

    Frame.key = observed_key
