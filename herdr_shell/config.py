"""Comment-preserving config transactions, diagnostics and field-level undo."""
from contextlib import contextmanager
from functools import lru_cache
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import tomllib
import uuid

import tomlkit
from tomlkit.container import OutOfOrderTableProxy

from . import PLUGIN_ID
from .runtime import ShellError, config_path, run_herdr, state_path

MISSING = {"__herdr_shell_missing__": True}


def command_id(command):
    identity = [command.get("type", "shell"), command.get("command", ""), command.get("description", "")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]


def plain(value):
    return value.unwrap() if hasattr(value, "unwrap") else value


def get_value(doc, path):
    node = doc
    for part in path:
        if part.startswith("@"):
            matches = [c for c in node if command_id(c) == part[1:]]
            if len(matches) > 1:
                raise ShellError("Duplicate custom commands have an ambiguous identity; edit the file directly.")
            if not matches:
                return MISSING
            node = matches[0]
        else:
            if not isinstance(node, dict) or part not in node:
                return MISSING
            node = node[part]
    return plain(node)


def put_value(doc, path, value):
    node = doc
    attachments = []
    for part in path[:-1]:
        parent = node
        if part.startswith("@"):
            matches = [c for c in node if command_id(c) == part[1:]]
            if len(matches) != 1:
                raise ShellError("The custom command changed. Refresh the editor.")
            node = matches[0]
        else:
            if part not in node:
                node[part] = tomlkit.aot() if part == "command" else tomlkit.table()
            node = node[part]
        attachments.append((parent, part, node))
    leaf = path[-1]
    if leaf.startswith("@"):
        indexes = [i for i, c in enumerate(node) if command_id(c) == leaf[1:]]
        if len(indexes) > 1:
            raise ShellError("Duplicate custom commands cannot be edited safely.")
        if indexes:
            if value == MISSING:
                del node[indexes[0]]
            else:
                node[indexes[0]] = value
        elif value != MISSING:
            item = tomlkit.table()
            for k, v in value.items():
                item[k] = v
            node.append(item)
    elif value == MISSING:
        node.pop(leaf, None)
    else:
        node[leaf] = value
    # A table declared in separate parts of a TOML file returns a proxy. Its
    # merged child AoT is a copy, so in-place edits need writing back to it.
    for parent, part, child in reversed(attachments):
        if isinstance(parent, OutOfOrderTableProxy):
            parent[part] = child


@lru_cache(maxsize=1)
def defaults():
    text = run_herdr(["--default-config"])
    result = {}
    in_keys = False
    for line in text.splitlines():
        line = re.sub(r"^\s*#\s?", "", line).strip()
        if line.startswith("["):
            in_keys = line == "[keys]"
            continue
        if not in_keys or not re.match(r"^[a-z_]+\s*=", line):
            continue
        try:
            pair = tomllib.loads(line)
        except tomllib.TOMLDecodeError:
            continue
        for key, value in pair.items():
            if isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                result[key] = value
    if "prefix" not in result or "focus_pane_left" not in result:
        raise ShellError("Cannot read key defaults from this Herdr version.")
    return result


def values(value):
    return [value] if isinstance(value, str) else list(value)


def normalized(chord):
    """Expand ranges and modifier aliases for collision checks, not input synthesis."""
    aliases = {"control": "ctrl", "cmd": "super", "command": "super", "option": "alt",
               "return": "enter", "escape": "esc", "spacebar": "space", "-": "minus",
               ",": "comma", ".": "period", "/": "slash", ";": "semicolon", "[": "leftbracket",
               "]": "rightbracket", "`": "backtick", "?": "question"}
    chord = chord.strip().lower()
    if not chord:
        return set()
    if chord.endswith("+"):
        chord = chord[:-1] + "plus"
    parts = [aliases.get(p.strip(), p.strip()) for p in chord.split("+")]
    key = parts[-1]
    modifiers = parts[:-1]
    match = re.fullmatch(r"([1-9])\.\.([1-9])", key)
    keys = [str(i) for i in range(int(match[1]), int(match[2]) + 1)] if match else [key]
    return {"+".join(sorted(set(modifiers)) + [k]) for k in keys}


def bindings(text):
    doc = tomllib.loads(text)
    user = doc.get("keys", {})
    base = defaults()
    combined = {**base, **{k: v for k, v in user.items() if isinstance(v, (str, list))}}
    rows = []
    for key, value in combined.items():
        if not (isinstance(value, str) or isinstance(value, list) and all(isinstance(v, str) for v in value)):
            continue
        rows.append({"id": key, "label": key.replace("_", " ").capitalize(),
                     "keys": values(value), "source": "user" if key in user else "default",
                     "path": ["keys", key], "mode": "navigate" if key.startswith("navigate_") else "normal"})
    for command in user.get("command", []):
        rows.append({"id": "custom:" + command_id(command),
                     "label": command.get("description") or command.get("command", "Custom command"),
                     "keys": values(command.get("key", "")), "source": "custom",
                     "path": ["keys", "command", "@" + command_id(command), "key"], "mode": "normal",
                     "action_id": command.get("command") if command.get("type") == "plugin_action" else None})
    # Legacy indexed config remains active in older profiles.
    for category, modifier in user.get("indexed", {}).items():
        if modifier:
            rows.append({"id": "indexed:" + category, "label": "Legacy indexed " + category,
                         "keys": [modifier + "+1..9"], "source": "legacy", "path": None, "mode": "normal"})
    return rows


def conflicts(text):
    occupied = {}
    for row in bindings(text):
        for chord in row["keys"]:
            for key in normalized(chord):
                occupied.setdefault((row["mode"], key), set()).add(row["id"])
    return {f"{mode}:{key}": sorted(ids) for (mode, key), ids in occupied.items() if len(ids) > 1}


def atomic_write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".herdr-shell-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ConfigStore:
    def __init__(self, path=None, state=None, validator=None):
        self.path = Path(path or config_path()).expanduser().resolve()
        key = hashlib.sha256(str(self.path).encode()).hexdigest()[:16]
        self.state = Path(state or state_path()) / "config" / key
        self.validator = validator or self.validate

    def read(self):
        return self.path.read_text() if self.path.exists() else ""

    @contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def validate(self, text):
        tomllib.loads(text)
        # Stage alongside the real config so relative paths preserve their meaning.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".herdr-shell-check-", suffix=".toml", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            env = dict(os.environ, HERDR_CONFIG_PATH=name)
            output = run_herdr(["config", "check"], env=env)
            if output.strip() != "config: ok":
                raise ShellError(output.strip() or "Herdr did not confirm valid configuration.")
        finally:
            os.unlink(name)

    def prepare(self, changes, base=None):
        before = self.read() if base is None else base
        doc = tomlkit.parse(before)
        records = []
        for path, value in changes:
            old = get_value(doc, path)
            if old != value:
                records.append({"path": path, "before": old, "after": value})
                put_value(doc, path, value)
        after = tomlkit.dumps(doc)
        existing = conflicts(before)
        introduced = {k: v for k, v in conflicts(after).items() if existing.get(k) != v}
        if introduced:
            raise ShellError("Shortcut collision: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in introduced.items()))
        return {"before": before, "after": after, "changes": records,
                "diff": "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                   fromfile=str(self.path), tofile=str(self.path) + " (proposed)"))}

    def apply(self, proposal, reload=None):
        if not proposal["changes"]:
            return {"changed": False}
        with self.locked():
            if self.read() != proposal["before"]:
                raise ShellError("Config changed since the preview. Refresh and try again.")
            self.validator(proposal["after"])
            if self.read() != proposal["before"]:
                raise ShellError("Config changed during validation. Refresh and try again.")
            transaction = str(time.time_ns()) + "-" + uuid.uuid4().hex[:8]
            record_path = self.state / (transaction + ".json")
            record = {"config": str(self.path), "changes": proposal["changes"], "status": "pending"}
            atomic_write(self.state / (transaction + ".toml"), proposal["before"])
            atomic_write(record_path, json.dumps(record, indent=2))
            mode = self.path.stat().st_mode & 0o777 if self.path.exists() else 0o600
            atomic_write(self.path, proposal["after"], mode)
            try:
                if reload:
                    reload()
            except Exception as exc:
                if self.read() == proposal["after"]:
                    atomic_write(self.path, proposal["before"], mode)
                    record["status"] = "rolled_back"
                    try:
                        reload()
                    except Exception:
                        pass
                else:
                    record["status"] = "reload_failed_concurrent_edit"
                atomic_write(record_path, json.dumps(record, indent=2))
                raise ShellError(f"Config reload failed ({record['status']}): {exc}") from exc
            record["status"] = "applied"
            atomic_write(record_path, json.dumps(record, indent=2))
            return {"changed": True, "transaction": transaction, "reloaded": reload is not None}

    def undo_proposal(self):
        for path in sorted(self.state.glob("*.json"), reverse=True):
            record = json.loads(path.read_text())
            if record.get("status") != "applied":
                continue
            before = self.read()
            doc = tomlkit.parse(before)
            changes = []
            for change in reversed(record["changes"]):
                current = get_value(doc, change["path"])
                if current != change["after"]:
                    raise ShellError("Undo would replace a later edit to " + ".".join(change["path"]) + ". Edit this field manually.")
                changes.append((change["path"], change["before"]))
            return self.prepare(changes, before), path
        raise ShellError("No applied configuration change to undo.")

    def undo(self, proposal, record_path, reload=None):
        result = self.apply(proposal, reload)
        # An undo is not itself an edit to undo. Preserve both audit records.
        with self.locked():
            record = json.loads(record_path.read_text())
            record["status"] = "undone"
            atomic_write(record_path, json.dumps(record, indent=2))
            if result.get("transaction"):
                new_path = self.state / (result["transaction"] + ".json")
                new_record = json.loads(new_path.read_text())
                new_record["status"] = "undo"
                atomic_write(new_path, json.dumps(new_record, indent=2))
        return result


def shortcut_changes(menu_key="prefix+space", bindings_key="prefix+alt+k", *, existing=None):
    changes = []
    commands = tomllib.loads(existing).get("keys", {}).get("command", []) if existing is not None else []
    for action, key, label in [("menu", menu_key, "Herdr Shell menu"),
                               ("keybindings", bindings_key, "Herdr Shell keybindings")]:
        # Reinstall/update keeps a user's changed, alternative, or disabled
        # fallback. Explicit config apply can still request the default keys.
        if any(command.get("type") == "plugin_action" and command.get("command") == f"{PLUGIN_ID}.{action}"
               for command in commands):
            continue
        item = {"key": key, "type": "plugin_action", "command": f"{PLUGIN_ID}.{action}", "description": label}
        changes.append((["keys", "command", "@" + command_id(item)], item))
    return changes
