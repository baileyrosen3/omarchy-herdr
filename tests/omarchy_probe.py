"""Opt-in real Omarchy-manager lifecycle probe, isolated from the user's desktop.

Run: python3 tests/omarchy_probe.py --run [--keep]

The installed add/update/remove/enable/disable scripts, manifest validator, Git,
Herdr offline plugin registry, native config validation, bridge file lifecycle,
and detached supervisor are real. A local Git URL adapter, shell IPC fixture,
and a fixture-only compositor registry replace network/QML/Hyprland effects.
Collision detection, reconciliation and live-generation ownership checks are real.
QML loading and physical chords belong to the separate desktop probe.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "blr.herdr-shell"
OTHER = "qa.unrelated"
URL = "https://github.com/herdr-shell-qa/isolated-fixture.git"

BASE = '''# Personal config must survive the manager lifecycle.
onboarding=false
[update]
version_check=false
manifest_check=false
[ui]
pane_gaps=false
[ui.sound]
enabled=false
[keys]
prefix="ctrl+space"
[[keys.command]]
key="prefix+o"
type="plugin_action"
command="qa.unrelated.menu"
description="Personal command"
[[keys.command]]
key="prefix+space"
type="plugin_action"
command="qa.unrelated.menu"
description="Personal menu recovery owner"
[[keys.command]]
key="prefix+alt+k"
type="plugin_action"
command="qa.unrelated.menu"
description="Personal keybindings recovery owner"
'''
HYPR = '-- Personal desktop settings must survive.\nrequire("default.hypr.omarchy")\n'


SHELL_STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ["HOME"])
config = home / ".config/omarchy/shell.json"
plugins = home / ".config/omarchy/plugins"
args = sys.argv[1:]
if not args or args[0] != "shell":
    raise SystemExit("QA shell fixture only implements shell IPC")
with (Path(os.environ["HERDR_SHELL_QA_ROOT"]) / "shell-ipc.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
data = json.loads(config.read_text())
def enabled(plugin):
    return plugin not in data.get("disabledPlugins", []) and any(
        row == plugin or isinstance(row, dict) and row.get("id") == plugin
        for row in data.get("plugins", []))
op = args[1]
if op == "rescanPlugins":
    print("ok")
elif op == "listPlugins":
    rows = []
    for path in sorted(plugins.glob("*/manifest.json")):
        if path.parent.name.startswith("."):
            continue
        row = json.loads(path.read_text())
        rows.append({**row, "enabled": enabled(row["id"]), "firstParty": False})
    print(json.dumps(rows))
elif op in ("enablePlugin", "setPluginEnabled"):
    plugin = args[2]
    if not (plugins / plugin / "manifest.json").exists():
        print("unknown")
    else:
        on = op == "enablePlugin" or args[3] == "true"
        data["plugins"] = [row for row in data.get("plugins", [])
                           if not (row == plugin or isinstance(row, dict) and row.get("id") == plugin)]
        data["disabledPlugins"] = [row for row in data.get("disabledPlugins", []) if row != plugin]
        data["plugins" if on else "disabledPlugins"].append(plugin)
        temporary = config.with_suffix(".qa-tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.replace(config)
        print("ok")
else:
    raise SystemExit("Unexpected shell IPC: " + repr(args))
'''

DESKTOP_FIXTURE = r'''"""Only this disposable Git fixture may fake compositor reads/dispatch."""
import json, os, re, sys
from pathlib import Path
from . import desktop, keymap, omarchy
from .runtime import ShellError

def install():
    root = Path(os.environ["HERDR_SHELL_QA_ROOT"]).resolve()
    location = Path(__file__).resolve()
    if root not in location.parents:
        raise RuntimeError("QA fixture escaped its temporary root")
    fields = Path("/proc/self/stat").read_text().split(") ", 1)[1].split()
    event = {"pid": os.getpid(), "start": fields[19], "argv": sys.argv,
             "file": str(location)}
    with (root / "processes.jsonl").open("a") as log:
        log.write(json.dumps(event) + "\n")
    state_path = root / "compositor-state.json"
    def read_state():
        if not state_path.exists():
            return {"generation": None, "reloads": 0, "owned": {}}
        return json.loads(state_path.read_text())
    def write_state(state):
        desktop.atomic_write(state_path, json.dumps(state))
    def bindings():
        state = read_state()
        foreign = json.loads((root / "compositor-foreign.json").read_text())
        return [*foreign, *state["owned"].values()]
    def hypr(*args):
        with (root / "desktop-ipc.jsonl").open("a") as log:
            log.write(json.dumps(list(args)) + "\n")
        if args == ("configerrors",):
            return ""
        if args == ("reload",):
            state = read_state()
            state["reloads"] += 1
            state["generation"] = "QAfixtureGeneration" + str(state["reloads"]) if desktop.installed() else None
            state["owned"] = {}
            write_state(state)
            return ""
        if args[0] == "repl" and "generation" in args[1]:
            return read_state()["generation"] or "nil"
        if args == ("-j", "binds"):
            return json.dumps(bindings())
        if args[:2] == ("-j", "getoption"):
            return json.dumps({"str": "us" if args[-1] == "input:kb_layout" else ""})
        if args[0] == "eval":
            state = read_state()
            code = args[1]
            guard = re.search(r'generation == ("[^"]+")', code)
            if guard is None or "herdr_shell_bridge.register(" not in code:
                raise ShellError("Unexpected eval in isolated fixture: " + code)
            expected = json.loads(guard[1])
            allowed = set(re.findall(r'\["([^"]+)"\]=true', code))
            known = {action for _, action, _ in desktop.MAPPINGS}
            if not allowed <= known:
                raise ShellError("Fixture cannot register unknown actions")
            with (root / "desktop-registration.jsonl").open("a") as log:
                log.write(json.dumps({"expected": expected, "generation": state["generation"],
                                      "allowed": sorted(allowed), "profile_enabled": desktop.enabled(),
                                      "python_revision": getattr(keymap, "QA_RECONCILE_REVISION", "initial")}) + "\n")
            if expected != state["generation"]:
                return ""  # A stale generation cannot claim a new registration.
            # register() owns only these handles; external bindings survive.
            state["owned"] = {action: row for action, row in state["owned"].items() if action in allowed}
            for keys, action, _ in desktop.MAPPINGS:
                if action in allowed:
                    mask, name = desktop.chord(keys)
                    state["owned"].setdefault(action, {"modmask": mask, "key": name, "keycode": 0,
                        "submap": "", "dispatcher": "__lua", "description": "Herdr Shell: " + action})
            write_state(state)
            return ""
        raise ShellError("Unexpected compositor call in isolated fixture: " + repr(args))
    desktop.hypr = hypr
    # Keep actual binding_snapshot(), collision detection, reconciliation,
    # generation rechecks, and exact per-action ownership verification.
    # Preserve the stable-observation/retry algorithm while shortening QA waits.
    omarchy.POLL_SECONDS = .15
'''

ENTRY = '''#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herdr_shell.qa_desktop import install
install()
from herdr_shell.cli import main
raise SystemExit(main())
'''


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Probe:
    def __init__(self, root):
        self.root = root
        self.home = root / "h"
        self.config = self.home / ".config"
        self.state = root / "s/herdr-shell/omarchy"
        self.runtime = root / "d/herdr-shell/omarchy/runtime"
        self.upstream = root / "upstream"
        self.source = self.config / "omarchy/plugins" / PLUGIN
        self.native_config = self.config / "herdr/config.toml"
        self.hypr = self.config / "hypr/hyprland.lua"
        self.prefs = self.config / "herdr-shell"
        self.helper = self.home / ".local/bin/herdr-shell"
        self.compositor = root / "compositor-state.json"
        self.foreign = [{"modmask": 72, "key": "Z", "keycode": 0, "submap": "", "dispatcher": "exec",
                         "description": "QA reserved desktop zoom"}]
        self.git = shutil.which("git")
        self.herdr = shutil.which("herdr")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("HERDR_", "XDG_", "HYPRLAND_")) and key not in
                    ("HOME", "TMUX", "WAYLAND_DISPLAY", "DISPLAY", "GIT_DIR", "GIT_WORK_TREE")}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.config),
                        XDG_DATA_HOME=str(root / "d"), XDG_STATE_HOME=str(root / "s"),
                        XDG_CACHE_HOME=str(root / "c"), OMARCHY_PATH="/usr/share/omarchy",
                        HERDR_BIN_PATH=self.herdr, HERDR_CONFIG_PATH=str(self.native_config),
                        HERDR_SOCKET_PATH=str(root / "offline.sock"),
                        HERDR_SHELL_QA_ROOT=str(root), GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL="/dev/null", PYTHONDONTWRITEBYTECODE="1")
        self.passed = []

    def run(self, argv, timeout=25):
        require(self.root.parent == Path(tempfile.gettempdir()) and self.root.name.startswith("ho-")
                and not self.root.is_symlink(), "Probe must run in its owned temporary directory")
        for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                     "HERDR_CONFIG_PATH", "HERDR_SOCKET_PATH", "HERDR_SHELL_QA_ROOT"):
            value = Path(self.env[name]).resolve()
            require(value == self.root or self.root in value.parents, name + " escaped the isolated fixture")
        result = subprocess.run([str(arg) for arg in argv], env=self.env,
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise AssertionError("Command failed: " + repr([str(arg) for arg in argv]) +
                                 "\n" + (result.stderr + result.stdout).strip())
        return result.stdout

    def eventually(self, predicate, description, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.08)
        log = self.state / "service.log"
        raise AssertionError(description + "\nStatus: " + repr(read_json(self.state / "status.json")) +
                             "\nService log:\n" + (log.read_text()[-6000:] if log.exists() else "<missing>"))

    def status(self, wanted, revision=None):
        result = read_json(self.state / "status.json", {})
        return result if result.get("state") == wanted and (revision is None or result.get("revision") == revision) else None

    def native_plugins(self):
        result = json.loads(self.run([self.herdr, "plugin", "list", "--json"]))
        return result["result"]["plugins"]

    def native(self):
        return next((p for p in self.native_plugins() if p["plugin_id"] == PLUGIN), None)

    def commands(self):
        return tomllib.loads(self.native_config.read_text()).get("keys", {}).get("command", [])

    def prepare(self):
        for name in ("omarchy/plugins", "herdr", "hypr"):
            (self.config / name).mkdir(parents=True, exist_ok=True)
        self.native_config.write_text(BASE)
        self.hypr.write_text(HYPR)
        (self.root / "compositor-foreign.json").write_text(json.dumps(self.foreign))
        shell = self.config / "omarchy/shell.json"
        shell.write_text(json.dumps({"plugins": [OTHER], "disabledPlugins": [], "qaPersonal": {"keep": True}}))
        other = self.config / "omarchy/plugins" / OTHER
        other.mkdir()
        (other / "Service.qml").write_text("import QtQuick\nItem {}\n")
        (other / "manifest.json").write_text(json.dumps({"schemaVersion": 1, "id": OTHER,
            "name": "Unrelated QA plugin", "version": "0.1.0", "kinds": ["service"],
            "entryPoints": {"service": "Service.qml"}}))
        (other / "herdr-plugin.toml").write_text('id="qa.unrelated"\nname="Unrelated QA plugin"\nversion="0.1.0"\nmin_herdr_version="0.9.3"\nplatforms=["linux"]\n')
        self.run([self.herdr, "plugin", "link", other, "--enabled"])
        require([p["plugin_id"] for p in self.native_plugins()] == [OTHER], "Native registry is not isolated")
        for name in ("bin", "herdr_shell", "integrations", "omarchy"):
            shutil.copytree(ROOT / name, self.upstream / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("manifest.json", "herdr-plugin.toml", "LICENSE"):
            shutil.copy2(ROOT / name, self.upstream / name)
        (self.upstream / "assets/branding").mkdir(parents=True)
        shutil.copy2(ROOT / "assets/branding/README.md", self.upstream / "assets/branding/README.md")
        (self.upstream / "herdr_shell/qa_desktop.py").write_text(DESKTOP_FIXTURE)
        (self.upstream / "bin/herdr-shell").write_text(ENTRY)
        (self.upstream / "bin/herdr-shell").chmod(0o755)
        self.run([self.git, "init", "--quiet", "--initial-branch=main", self.upstream])
        self.commit("Initial disposable plugin")
        tools = self.root / "bin"
        tools.mkdir()
        (tools / "omarchy-shell").write_text(SHELL_STUB)
        adapter = '''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if len(args) == 4 and args[:2] == ["clone", "--"] and args[2] == %r:
    args[2] = %r
os.execv(%r, [%r, *args])
''' % (URL, str(self.upstream), self.git, self.git)
        (tools / "git").write_text(adapter)
        for path in tools.iterdir():
            path.chmod(0o755)
        self.env["PATH"] = str(tools) + os.pathsep + self.env["PATH"]
        for parent, name in ((self.root / "s/herdr-shell", "personal-history.txt"),
                             (self.root / "c", "personal-cache.txt")):
            parent.mkdir(parents=True, exist_ok=True)
            (parent / name).write_text("keep user data\n")

    def commit(self, message):
        self.run([self.git, "-C", self.upstream, "add", "."])
        self.run([self.git, "-C", self.upstream, "-c", "user.name=Herdr QA",
                  "-c", "user.email=qa@example.invalid", "commit", "--quiet", "-m", message])

    def start(self):
        result = json.loads(self.run([sys.executable, self.source / "bin/herdr-shell", "_omarchy", "start"]))
        require(result.get("started"), "Installed service did not start")
        return result

    def check_owned(self):
        plugin = self.native()
        require(plugin is not None and Path(plugin["plugin_root"]).resolve() == self.runtime.resolve(),
                "Herdr must link the stable runtime, not Omarchy's removable checkout")
        require(self.helper.is_symlink() and self.helper.resolve() == self.runtime / "bin/herdr-shell",
                "CLI helper must point at the stable runtime")
        require((self.prefs / "plugin-root").read_text().strip() == str(self.runtime), "Bridge owner differs")
        require("BEGIN HERDR SHELL DESKTOP CONTROLS" in self.hypr.read_text(), "Desktop block missing")
        fallback = {c["command"]: c["key"] for c in self.commands() if c["type"] == "plugin_action"}
        require(fallback.get(PLUGIN + ".menu") == "", "Reserved native menu fallback must install unbound")
        require(fallback.get(PLUGIN + ".keybindings") == "", "Reserved native keybindings fallback must install unbound")
        owners = [c for c in self.commands() if c["command"] == OTHER + ".menu"]
        require(owners == tomllib.loads(BASE)["keys"]["command"], "Installation changed personal native key owners")

    def check_registered(self, *, active, python_revision="initial"):
        # Ask the actual runtime CLI to run its real effective-binding checks.
        status = json.loads(self.run([self.helper, "desktop", "status"]))
        rows = {r["action_id"]: r for r in status["bindings"] if r.get("action_id")}
        require(len(rows) == 26, "Desktop status omitted profile actions")
        require(rows["pane-zoom"]["status"] == "conflict" and not rows["pane-zoom"]["active"]
                and "QA reserved desktop zoom" in rows["pane-zoom"]["reason"],
                "Reserved desktop chord was reported active or lost its owner reason")
        free = set(rows) - {"pane-zoom"}
        require(all(rows[action]["active"] == active and rows[action]["status"] == ("ready" if active else "disabled")
                    for action in free), "Free desktop chords did not reflect the actual profile preference")
        state = read_json(self.compositor)
        require(set(state["owned"]) == free and len(state["owned"]) == 25,
                "Compositor fixture registered reserved/missing actions")
        require(read_json(self.root / "compositor-foreign.json") == self.foreign,
                "Registration changed an external desktop owner")
        report = read_json(self.prefs / "binding-status.json")
        require(report["generation"] == state["generation"] and set(report["conflicts"]) == {"pane-zoom"},
                "Binding status was persisted without the current live generation/conflict set")
        registrations = lines(self.root / "desktop-registration.jsonl")
        require(registrations and set(registrations[-1]["allowed"]) == free
                and registrations[-1]["expected"] == state["generation"]
                and registrations[-1]["generation"] == state["generation"],
                "Live registration did not use the verified generation and exact free set")
        require(registrations[-1]["profile_enabled"] is False,
                "Shortcut ownership must be verified before enabling the profile")
        require(registrations[-1]["python_revision"] == python_revision,
                "Updated Python conflict checker was not loaded for reconciliation")

    def exercise(self):
        self.prepare()
        self.run(["omarchy-plugin-add", URL, "--enable", "--yes"])
        require(self.source.is_dir() and (self.source / ".git").is_dir(), "Actual manager did not add a checkout")
        require(not self.runtime.exists(), "IPC fixture must not pretend to instantiate QML")
        self.start()  # Deliberately stand in for Service.qml's execDetached call.
        first = self.eventually(lambda: self.status("ready"), "Add did not activate native integration")
        self.check_owned()
        reservations = json.loads(first["result"])["reserved_shortcuts"]
        require({row["action"] for row in reservations} == {"menu", "keybindings"}
                and all(row["owners"] and "Reserved by" in row["reason"] for row in reservations),
                "Managed setup did not report both native fallback reservations")
        self.check_registered(active=True)
        require(self.native()["enabled"] and (self.prefs / "desktop-enabled").read_text().strip() == "1",
                "Fresh install should enable both owned controls")
        self.passed.append("actual Omarchy add/validate/enable succeeds with both native fallback keys reserved; preserves their owners and installs recovery actions unbound")
        self.passed.append("true Super+Alt+Z desktop conflict reserves zoom while exactly 25 free chords register and verify ownership in the live fixture generation before activation")
        receipt = self.state / "setup.json"
        initial_receipt = receipt.stat().st_mtime_ns
        self.start()
        self.eventually(lambda: any("already running" in line for line in (self.state / "service.log").read_text().splitlines()),
                        "Repeated service start did not honor its singleton lock")
        require(receipt.stat().st_mtime_ns == initial_receipt, "Unchanged repeated start must not reinstall")
        self.passed.append("repeated service start is a singleton and leaves the setup receipt unchanged")

        self.run([self.helper, "desktop", "disable"])
        self.run([self.herdr, "plugin", "link", self.runtime, "--disabled"])
        import tomlkit
        edited = tomlkit.parse(self.native_config.read_text())
        menu = next(c for c in edited["keys"]["command"] if c["command"] == PLUGIN + ".menu")
        menu["key"] = "prefix+m"
        text = tomlkit.dumps(edited)
        self.native_config.write_text(text)
        bridge_before = (self.prefs / "hyprland.lua").read_text()
        registered_before = len(lines(self.root / "desktop-registration.jsonl"))
        generation_before = read_json(self.compositor)["generation"]
        checker = self.upstream / "herdr_shell/keymap.py"
        checker.write_text(checker.read_text() + '\nQA_RECONCILE_REVISION = "updated-manager-checker"\n')
        self.commit("Python-only conflict checker update fixture")
        self.run(["omarchy-plugin-update", PLUGIN, "--yes"])
        updated = self.eventually(lambda: (value if (value := self.status("ready")) and value["revision"] != first["revision"] else None),
                                  "Manager source update did not refresh the runtime and restart the supervisor", timeout=45)
        require((self.runtime / "herdr_shell/keymap.py").read_text() == (self.source / "herdr_shell/keymap.py").read_text(),
                "Updated manager conflict checker did not reach the runtime")
        require((self.prefs / "hyprland.lua").read_text() == bridge_before,
                "Python-only update unexpectedly changed generated bridge Lua")
        require(len(lines(self.root / "desktop-registration.jsonl")) > registered_before
                and read_json(self.compositor)["generation"] != generation_before,
                "Python-only update skipped desktop reload and fresh registration")
        self.check_registered(active=False, python_revision="updated-manager-checker")
        watch_events = [row for row in lines(self.root / "processes.jsonl") if "watch" in row["argv"]]
        require(any(updated["revision"] in row["argv"] and "--wait" in row["argv"] for row in watch_events),
                "Update must execute a replacement supervisor with the new revision")
        require(not self.native()["enabled"] and (self.prefs / "desktop-enabled").read_text().strip() == "0",
                "Source update reenabled a user's disabled controls")
        require(self.native_config.read_text() == text, "Update changed a customized fallback or personal config")
        self.passed.append("Python-only manager update leaves generated Lua identical but reloads/verifies 25 free chords with the new checker, reenters the supervisor, and preserves disabled controls/custom fallback")

        self.run([self.herdr, "plugin", "link", self.runtime, "--enabled"])
        self.run(["omarchy-plugin-disable", PLUGIN])
        self.eventually(lambda: self.status("disabled"), "Manager disable did not suspend integration")
        suspended = read_json(self.state / "suspended.json")
        require(suspended["native_enabled"] is True and suspended["desktop_enabled"] is False,
                "Service suspension did not save the separate native/profile preferences")
        require(not self.native()["enabled"] and self.helper.is_symlink(), "Suspension must disable native registration and retain its helper")
        require(self.native_config.read_text() == text and self.runtime.exists(), "Suspension deleted user config or runtime")
        self.run(["omarchy-plugin-enable", PLUGIN])
        self.eventually(lambda: self.status("ready", updated["revision"]), "Manager resume did not activate integration")
        require(self.native()["enabled"] and (self.prefs / "desktop-enabled").read_text().strip() == "0",
                "Resume must restore native-on/profile-off separately")
        require(not (self.state / "suspended.json").exists(), "Resume left stale suspended preferences")
        self.check_registered(active=False, python_revision="updated-manager-checker")
        self.passed.append("actual manager disable/resume suspends controls and restores native-on/profile-off preferences without changing user config")

        (self.runtime / "personal-runtime.txt").write_text("keep runtime data\n")
        self.prefs.mkdir(exist_ok=True)
        (self.prefs / "personal-setting.txt").write_text("keep personal desktop preference\n")
        self.run(["omarchy-plugin-remove", PLUGIN, "--yes"])
        require(not self.source.exists(), "Actual manager must delete its source checkout")
        self.eventually(lambda: self.status("removed"), "Cached supervisor did not finish cleanup after source deletion")
        require(self.native() is None, "Removal left the native Herdr registration")
        require(not self.helper.exists() and not self.helper.is_symlink(), "Removal left the owned CLI helper")
        require(self.hypr.read_text() == HYPR, "Removal did not restore the exact personal desktop config")
        require(all(not (self.prefs / name).exists() for name in
                    ("plugin-root", "hyprland.lua", "desktop-enabled", "binding-status.json")),
                "Removal left owned desktop preference files")
        commands = self.commands()
        require(commands == tomllib.loads(BASE)["keys"]["command"], "Removal touched unrelated native key owners or left plugin commands")
        require(read_json(self.compositor)["owned"] == {} and read_json(self.root / "compositor-foreign.json") == self.foreign,
                "Removal left owned handles or removed an external desktop owner")
        require(not tomllib.loads(self.native_config.read_text())["ui"]["pane_gaps"], "Removal changed personal UI settings")
        require(self.runtime.is_dir() and (self.runtime / "personal-runtime.txt").exists(), "Removal must retain the owned runtime cache and user data")
        require((self.prefs / "personal-setting.txt").exists() and (self.root / "s/herdr-shell/personal-history.txt").exists()
                and (self.root / "c/personal-cache.txt").exists(), "Removal deleted personal state/cache/settings")
        require([p["plugin_id"] for p in self.native_plugins()] == [OTHER], "Removal touched an unrelated Herdr plugin")
        shell = read_json(self.config / "omarchy/shell.json")
        require(shell["qaPersonal"] == {"keep": True} and OTHER in shell["plugins"]
                and (self.config / "omarchy/plugins" / OTHER).is_dir(), "Removal touched unrelated Omarchy config/plugin")
        self.passed.append("actual Omarchy removal deletes source first; cached supervisor removes only owned links/bindings/files and preserves runtime, state, cache, preferences and unrelated plugins")
        return {"passed": self.passed, "real": ["installed Omarchy manager scripts and manifest validation", "local Git clone/fetch/fast-forward",
                "actual Herdr offline plugin list/link/uninstall", "native config validation and generated Lua syntax validation",
                "actual desktop collision detection, generation rechecks and per-free-action ownership verification",
                "managed detached supervisor, source update, singleton, disable/resume and source-gone cleanup"],
                "fixtures": ["validated HTTPS clone URL maps to a local Git repository", "shell IPC updates only temporary shell.json; QML startup is invoked manually",
                             "compositor registry/generation/reload/eval simulated in the disposable checkout only", "supervisor polling is shortened to 150ms"],
                "limits": ["does not prove real QML instantiation or physical key routing; use desktop_probe.py for those"]}

    def stop(self):
        # Detached replacements are recorded by the fixture wrapper. Never signal
        # a reused PID or any process lacking this probe's exact temporary root.
        targets = {}
        for event in lines(self.root / "processes.jsonl"):
            targets[event["pid"]] = event["start"]
        for pid, start in targets.items():
            proc = Path("/proc") / str(pid)
            try:
                fields = (proc / "stat").read_text().split(") ", 1)[1].split()
                env = (proc / "environ").read_bytes().split(b"\0")
                owned = ("HERDR_SHELL_QA_ROOT=" + str(self.root)).encode() in env
                if fields[19] == start and owned:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, IndexError):
                pass
        time.sleep(.15)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", action="store_true", help="explicitly run the isolated lifecycle probe")
    parser.add_argument("--keep", action="store_true", help="retain temporary fixtures/logs after stopping their processes")
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return 0
    required = ["git", "jq", "luac", "python3", "herdr", "omarchy-plugin-add", "omarchy-plugin-update",
                "omarchy-plugin-remove", "omarchy-plugin-enable", "omarchy-plugin-disable", "omarchy-plugin-validate"]
    missing = [name for name in required if shutil.which(name) is None]
    if importlib.util.find_spec("tomlkit") is None:
        missing.append("python tomlkit")
    if missing:
        parser.error("missing probe prerequisites: " + ", ".join(missing))
    root = Path(tempfile.mkdtemp(prefix="ho-"))
    probe = Probe(root)
    try:
        result = probe.exercise()
        if args.keep:
            result["fixture"] = str(root)
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "fixture": str(root), "passed": probe.passed}, indent=2), file=sys.stderr)
        return 1
    finally:
        probe.stop()
        if not args.keep:
            shutil.rmtree(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
