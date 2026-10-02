"""Optional native footer configuration with exact provider ownership."""
import json
from pathlib import Path
import shlex
import sys
import tomllib

from .config import MISSING
from .runtime import ShellError, run_herdr


ROOT = Path(__file__).resolve().parents[1]


def provider_command(root=None):
    """Use absolute paths so the native renderer needs no working directory."""
    executable = Path(sys.executable).absolute()
    entrypoint = Path(root or ROOT).expanduser().resolve() / "bin/herdr-shell"
    return shlex.join([str(executable), str(entrypoint), "footer"])


def supported():
    """Only an explicit native capability permits adding the footer field.

    Stock versions reject this read-only flag. Unknown config fields can be
    silently ignored, so a successful config check is not a capability probe.
    """
    try:
        reply = json.loads(run_herdr(["--client-capabilities"]))
    except (ShellError, OSError, ValueError, TypeError):
        return False
    return isinstance(reply, dict) and type(reply.get("footer")) is int and reply["footer"] == 1


def changes(existing, root=None, *, remove=False, enabled=True, capability=None):
    """Return a config transaction without touching personal footer settings.

    The exact command identifies our entry, even after its intervals are edited.
    Removing the last owned entry removes the field, which also permits a safe
    downgrade to stock Herdr. An occupied personal footer is never replaced.
    """
    doc = tomllib.loads(existing)
    footer = doc.get("ui", {}).get("footer", MISSING)
    command = provider_command(root)
    # Malformed or future footer shapes are user configuration, not our property.
    if footer != MISSING and (not isinstance(footer, list) or
                              any(not isinstance(entry, dict) for entry in footer)):
        return []
    entries = [] if footer == MISSING else footer
    owned = lambda entry: entry.get("type") == "command" and entry.get("command") == command
    if remove or not enabled:
        remaining = [entry for entry in entries if not owned(entry)]
        return [(["ui", "footer"], remaining if remaining else MISSING)] if remaining != entries else []
    native_support = supported() if capability is None else capability
    if not native_support:
        # Remove only our provider when support disappears; preserve personal
        # entries and footer separators, theme, tab position, and other fields.
        return changes(existing, root, remove=True)
    if footer != MISSING:
        # An explicit empty array is also a personal choice to hide the row.
        return []
    entry = {"type": "command", "command": command,
             "interval_seconds": 5, "timeout_seconds": 2}
    return [(["ui", "footer"], [entry])]
