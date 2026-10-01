"""Disposable fixtures and real-state checks for every practice shortcut.

The guide/controller is never a close target. Workspace and agent navigation
see only this plan's fixtures; no desktop bindings or global agent ring change.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import actions, desktop
from .closing import CLOSE_ACTIONS, close_plan, execute_close, shell_jobs
from .config import atomic_write
from .navigation import PRIORITY
from .rotation import _nearest, _tree
from .runtime import Client, Context, ShellError


def identity(pane):
    return {k: pane[k] for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")}


def _context(client, pane):
    info = client.call("pane.get", pane_id=pane)["pane"]
    return Context(client.path, info["pane_id"], info["workspace_id"], info["tab_id"],
                   info.get("foreground_cwd") or info.get("cwd") or "/tmp", info["terminal_id"])


def _wait(predicate, message, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.04)
    raise ShellError(message)


def _clean(context):
    info = context.client.call("pane.process_info", pane_id=context.pane)["process_info"]
    shell = info.get("shell_pid")
    processes = info.get("foreground_processes", [])
    return bool(shell and info.get("foreground_process_group_id") == shell and processes
                and all(p.get("pid") == shell for p in processes) and not shell_jobs(shell))


def _banner(context, text):
    _wait(lambda: _clean(context), "The fresh practice shell is not idle; no instruction was sent.")
    context.client.call("pane.send_input", pane_id=context.pane,
                        text="printf '%s\\n' " + shlex.quote(text), keys=["enter"])
    _wait(lambda: _clean(context), "Practice instructions have not finished rendering.")


@dataclass
class Plan:
    action: str
    guide: Context
    target: Context
    origin: Context
    directory: str
    pane_identities: list
    owned_workspaces: list
    owned_tabs: list
    owned_panes: list
    before: dict
    layout: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    required_presses: int = 1
    presses: int = 0
    history: list = field(default_factory=list)
    instructions: str = ""
    pins: list = field(default_factory=list)

    def __post_init__(self):
        if not self.pins:
            self.pins = deepcopy(self.pane_identities)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        data = deepcopy(value)
        for key in ("guide", "target", "origin"):
            data[key] = Context(**data[key])
        return cls(**data)


class ScopedClient:
    """Delegate real requests while restricting target IDs and snapshot rings."""
    def __init__(self, plan):
        self.plan = plan
        self.real = plan.guide.client
        self.path = self.real.path

    def snapshot(self):
        result = deepcopy(self.real.snapshot())
        # Close validation must see even an unexpected new pane/tab in the
        # target scope; filtering it out could hide newly started work.
        if self.plan.action in CLOSE_ACTIONS:
            return result
        fixtures = set(self.plan.expected["fixture_tabs"])
        panes = set(self.plan.owned_panes) - {self.plan.guide.pane}
        spaces = set(self.plan.expected.get("workspace_ring", self.plan.owned_workspaces))
        agents = set(self.plan.expected.get("agent_ids", []))
        result["workspaces"] = [w for w in result["workspaces"] if w["workspace_id"] in spaces]
        result["tabs"] = [t for t in result["tabs"] if t["tab_id"] in fixtures]
        result["panes"] = [p for p in result["panes"] if p["pane_id"] in panes]
        result["layouts"] = [l for l in result["layouts"] if l["tab_id"] in fixtures]
        result["agents"] = [a for a in result.get("agents", []) if a["pane_id"] in agents]
        return result

    def call(self, method, **params):
        if method == "session.snapshot":
            return {"snapshot": self.snapshot()}
        destination = params.get("destination", {})
        references = {**(destination if isinstance(destination, dict) else {}), **params}
        for key in ("pane_id", "source_pane_id", "target_pane_id"):
            if references.get(key) is not None and references[key] not in self.plan.owned_panes:
                raise ShellError("The practice action cannot target an unrelated pane.")
        for key in ("tab_id", "target_tab_id"):
            if references.get(key) is not None and references[key] not in self.plan.owned_tabs:
                raise ShellError("The practice action cannot target an unrelated tab.")
        for key in ("workspace_id", "source_workspace_id"):
            if references.get(key) is not None and references[key] not in self.plan.owned_workspaces:
                raise ShellError("The practice action cannot target an unrelated workspace.")
        if method == "pane.close" and params.get("pane_id") == self.plan.guide.pane:
            raise ShellError("The guide is protected from practice closes.")
        if method == "tab.close" and params.get("tab_id") == self.plan.guide.tab:
            raise ShellError("The guide tab is protected from practice closes.")
        if method == "workspace.close" and params.get("workspace_id") == self.plan.guide.workspace:
            raise ShellError("The guide workspace is protected from practice closes.")
        if method.startswith("agent.") and params.get("target") not in self.plan.expected.get("agent_ids", []):
            raise ShellError("Practice agent navigation only visits its simulated agents.")
        creates = method in ("pane.split", "tab.create", "workspace.create", "plugin.pane.open")
        popup = method == "plugin.pane.open" and params.get("placement") == "popup"
        adopts = (creates and not popup) or method == "pane.move"
        prior = self.real.snapshot() if adopts else None
        request = dict(params)
        wants_focus = bool(creates and not popup and request.get("focus"))
        if creates and not popup:
            request["focus"] = False
        result = self.real.call(method, **request)
        if adopts:
            adopted = _adopt(self.plan, result.get("move_result", result), method=method,
                             params=request, prior=prior)
            callback = getattr(self.plan, "on_adopt", None)
            if callback:
                callback()
            if wants_focus:
                if adopted is None:
                    raise ShellError("The new practice target could not be verified; its focus was preserved.")
                _focus(_context(self.real, adopted["pane_id"]))
        return result


class ScopedContext:
    def __init__(self, plan, context=None):
        self.base = context or plan.target
        self._client = ScopedClient(plan)

    def __getattr__(self, name):
        return getattr(self.base, name)

    @property
    def client(self):
        return self._client

    def validate(self):
        info = self.client.call("pane.get", pane_id=self.pane)["pane"]
        if identity(info) != {"pane_id": self.pane, "terminal_id": self.terminal,
                              "workspace_id": self.workspace, "tab_id": self.tab}:
            raise ShellError("The practice terminal moved or was replaced.")
        return info


def _adopt(plan, result, *, method="pane.split", params=None, prior=None):
    """Adopt minted IDs or publish a trusted move without changing final pins."""
    params = params or {}
    prior = prior or plan.before
    old_panes = {p["pane_id"] for p in prior["panes"]}
    old_tabs = {t["tab_id"] for t in prior["tabs"]}
    old_spaces = {w["workspace_id"] for w in prior["workspaces"]}
    if isinstance(result.get("plugin_pane"), dict):
        result = result["plugin_pane"]
    for key in ("pane", "root_pane"):
        pane = result.get(key)
        if isinstance(pane, dict) and all(k in pane for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")):
            found = identity(pane)
            live = plan.guide.client.call("pane.get", pane_id=found["pane_id"])["pane"]
            if identity(live) != found:
                raise ShellError("The returned practice terminal changed before it could be adopted.")
            if method == "pane.move":
                pin = next((p for p in plan.pins if p["pane_id"] == found["pane_id"]), None)
                if not pin or pin["terminal_id"] != found["terminal_id"]:
                    raise ShellError("A practice move changed its terminal identity; the pane was preserved.")
            elif found["pane_id"] in old_panes or found["pane_id"] in plan.owned_panes:
                raise ShellError("A create response returned an existing pane; it was not adopted.")
            space, tab = found["workspace_id"], found["tab_id"]
            workspace_result = result.get("workspace") or result.get("created_workspace") or {}
            tab_result = result.get("tab") or result.get("created_tab") or {}
            if method == "workspace.create" and (workspace_result.get("workspace_id") != space or space in old_spaces):
                raise ShellError("A workspace create response did not return a new workspace.")
            if method in ("tab.create", "workspace.create") and (tab_result.get("tab_id") != tab or tab in old_tabs):
                raise ShellError("A tab create response did not return a new tab.")
            if space not in plan.owned_workspaces:
                if method != "workspace.create" or workspace_result.get("workspace_id") != space or space in old_spaces:
                    raise ShellError("A create response referenced an unrelated workspace; it was not adopted.")
            if tab not in plan.owned_tabs:
                if method not in ("tab.create", "workspace.create", "pane.move") or tab_result.get("tab_id") != tab or \
                        tab_result.get("workspace_id") != space or tab in old_tabs:
                    raise ShellError("A create response referenced an unrelated tab; it was not adopted.")
            if method in ("pane.split", "plugin.pane.open"):
                source = params.get("target_pane_id") or params.get("pane_id") or plan.target.pane
                origin = next((p for p in plan.pane_identities if p["pane_id"] == source), None)
                if not origin or (space, tab) != (origin["workspace_id"], origin["tab_id"]):
                    raise ShellError("The new practice pane was returned outside its requested tab.")
            if method == "tab.create" and space != params.get("workspace_id", plan.target.workspace):
                raise ShellError("The new practice tab was returned outside its requested workspace.")
            if method == "pane.move":
                destination = params.get("destination", {})
                if found["pane_id"] != params.get("pane_id") or \
                        destination.get("type") == "new_tab" and space != destination.get("workspace_id") or \
                        destination.get("type") == "tab" and tab != destination.get("tab_id"):
                    raise ShellError("The moved practice pane was returned outside its requested destination.")
            if found["pane_id"] not in plan.owned_panes:
                plan.pane_identities.append(found)
                plan.pins.append(deepcopy(found))
                plan.owned_panes.append(found["pane_id"])
            else:
                plan.pane_identities = [found if p["pane_id"] == found["pane_id"] else p for p in plan.pane_identities]
            if found["tab_id"] not in plan.owned_tabs:
                plan.owned_tabs.append(found["tab_id"])
                plan.expected["fixture_tabs"].append(found["tab_id"])
            if found["workspace_id"] not in plan.owned_workspaces:
                plan.owned_workspaces.append(found["workspace_id"])
            return found
    return None


def _refresh(plan):
    after = plan.guide.client.snapshot()
    owned = set(plan.owned_panes)
    pins = {p["pane_id"]: p for p in plan.pins}
    for pane in after["panes"]:
        if pane["pane_id"] in owned and pins.get(pane["pane_id"]) != identity(pane):
            raise ShellError("A practice terminal was replaced or moved during the shortcut; its new context was preserved.")
    plan.pane_identities = [identity(p) for p in after["panes"] if p["pane_id"] in owned]
    plan.owned_panes = [p["pane_id"] for p in plan.pane_identities]
    live_tabs = {t["tab_id"] for t in after["tabs"]}
    plan.owned_tabs = [t for t in plan.owned_tabs if t in live_tabs]
    plan.expected["fixture_tabs"] = [t for t in plan.expected["fixture_tabs"] if t in live_tabs]
    live_spaces = {w["workspace_id"] for w in after["workspaces"]}
    plan.owned_workspaces = [w for w in plan.owned_workspaces if w in live_spaces]
    return after


def _guard(plan):
    plan.guide.validate()
    after = plan.guide.client.snapshot()
    live = {p["pane_id"]: identity(p) for p in after["panes"]}
    if any(live.get(p["pane_id"]) != p for p in plan.pane_identities):
        raise ShellError("A practice terminal changed. Prepare this challenge again.")
    fixtures = set(plan.expected["fixture_tabs"])
    actual = {p["pane_id"] for p in after["panes"] if p["tab_id"] in fixtures}
    expected = set(plan.owned_panes) - {plan.guide.pane}
    if actual != expected:
        raise ShellError("The practice layout gained or lost panes. No shortcut was run.")
    for workspace in plan.owned_workspaces:
        if workspace == plan.guide.workspace:
            continue
        if any(t["tab_id"] not in fixtures for t in after["tabs"] if t["workspace_id"] == workspace):
            raise ShellError("The practice workspace gained an unrelated tab. No shortcut was run.")
    for pane, path in plan.expected.get("simulators", {}).items():
        if not _simulator_alive(_context(plan.guide.client, pane), Path(path)):
            raise ShellError("A practice simulator stopped or changed. No real agent was visited.")
    return after


def _focus(context):
    context.validate()
    context.client.call("workspace.focus", workspace_id=context.workspace)
    context.client.call("pane.focus", pane_id=context.pane)


def _simulator(directory, name="agent"):
    path = Path(directory) / (name + "-simulator.py")
    if not path.exists():
        body = ('import signal\n'
                'print("KEY QUEST: harmless ' + name + ' simulator; no real agent or model is started.", flush=True)\n'
                'print("Use the guide shortcut here; Ctrl+C stops this demo.", flush=True)\n'
                'signal.pause()\n')
        path.write_text(body)
    return path


def _git_environment(directory=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    if directory is not None:
        env.update(GIT_DIR=str(Path(directory) / ".git"), GIT_WORK_TREE=str(directory))
    return env


def _git_script(directory, executable):
    path = Path(directory) / "practice-lazygit.py"
    # The Herdr server, rather than this controller, supplies pane environment.
    # Scrub again in that child before exec so inherited Git routing cannot
    # make the real Lazygit exercise read or modify another repository.
    body = ("import os\n"
            "directory = " + repr(str(directory)) + "\n"
            "executable = " + repr(executable) + "\n"
            "env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}\n"
            "env.update(GIT_DIR=os.path.join(directory, '.git'), GIT_WORK_TREE=directory, "
            "GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null', GIT_CONFIG_NOSYSTEM='1')\n"
            "os.chdir(directory)\n"
            # Lazygit treats GIT_DIR/GIT_WORK_TREE as git-dir/work-tree options;
            # --path cannot be supplied together with those explicit pins.
            "os.execve(executable, [executable], env)\n")
    path.write_text(body)
    return path


def _git_alive(context, directory):
    info = context.client.call("pane.process_info", pane_id=context.pane)["process_info"]
    expected = {k: v for k, v in _git_environment(directory).items() if k.startswith("GIT_")}
    for process in info.get("foreground_processes", []):
        if process.get("name") != "lazygit":
            continue
        try:
            root = Path("/proc") / str(process["pid"])
            if root.joinpath("cwd").resolve() != Path(directory).resolve():
                continue
            env = dict(item.decode().split("=", 1) for item in root.joinpath("environ").read_bytes().split(b"\0") if b"=" in item)
            if {k: v for k, v in env.items() if k.startswith("GIT_")} == expected:
                return True
        except (OSError, ValueError, UnicodeError, KeyError):
            continue
    return False


def _start_git(context, directory, executable):
    path = _git_script(directory, executable)
    _wait(lambda: _clean(context), "The disposable target has started work; no Git command was launched.")
    context.client.call("pane.send_input", pane_id=context.pane,
                        text=shlex.quote(sys.executable) + " " + shlex.quote(str(path)), keys=["enter"])
    _wait(lambda: _git_alive(context, directory), "Lazygit did not start in its isolated practice repository.")
    return str(path)


def _simulator_alive(context, path):
    info = context.client.call("pane.process_info", pane_id=context.pane)["process_info"]
    for process in info.get("foreground_processes", []):
        if process.get("pid") == info.get("shell_pid"):
            continue
        try:
            args = (Path("/proc") / str(process["pid"]) / "cmdline").read_bytes().split(b"\0")
            if str(path).encode() in args:
                return True
        except (OSError, KeyError):
            continue
    return False


def _start_simulator(context, directory, name="agent"):
    path = _simulator(directory, name)
    _wait(lambda: _clean(context), "The disposable target has started work; no simulator was launched.")
    context.client.call("pane.send_input", pane_id=context.pane,
                        text=shlex.quote(sys.executable) + " " + shlex.quote(str(path)), keys=["enter"])
    _wait(lambda: _simulator_alive(context, path), "The harmless practice simulator did not start.")
    info = context.client.call("pane.process_info", pane_id=context.pane)["process_info"]
    owned = next((desktop.read_process(p["pid"]) for p in info.get("foreground_processes", [])
                  if p.get("pid") != info.get("shell_pid") and str(path) in desktop.read_process(p["pid"]).argv), None)
    if owned is None:
        raise ShellError("The simulator's process identity could not be captured; it was preserved.")
    record = {"context": asdict(context), "pid": owned.pid, "start": owned.start, "path": str(path), "reported": name == "agent",
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    atomic_write(Path(directory) / (".simulator-" + context.pane.replace("/", "_") + ".json"), json.dumps(record))
    return str(path)


def _report(context, status):
    context.client.call("pane.rename", pane_id=context.pane, label="SIMULATED agent · " + status)
    if status == "done":
        context.client.call("pane.report_agent", pane_id=context.pane, source="key-quest", agent="codex", state="working", seq=1)
    context.client.call("pane.report_agent", pane_id=context.pane, source="key-quest", agent="codex",
                        state="idle" if status == "done" else status, seq=2)
    _wait(lambda: context.client.call("agent.get", target=context.pane)["agent"].get("agent_status") == status,
          "The simulated agent state was not recognized.")


def prepare(action, guide, *, display_chord=True):
    """Create trusted fixtures without taking keyboard focus from the guide."""
    if action not in {a for _, a, _ in desktop.MAPPINGS}:
        raise ShellError("This action has no desktop shortcut lesson.")
    guide.validate()
    client = guide.client
    if client.snapshot().get("focused_pane_id") != guide.pane:
        raise ShellError("Focus the guide before preparing a practice target.")
    parent = Path(guide.cwd)
    if not parent.is_dir() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
        raise ShellError("Practice needs the game's private directory; no fixtures were created.")
    directory = tempfile.mkdtemp(prefix="practice-", dir=parent)
    key = next(k for k, a, _ in desktop.MAPPINGS if a == action)
    banner = "SHORTCUT PRACTICE · disposable target\n" + (key + "\n" if display_chord else "") + "Shortcut from the guide acts here. Your real work is outside this exercise."
    contexts, spaces, tabs = [], [guide.workspace], []

    def tab():
        result = client.call("tab.create", workspace_id=guide.workspace, label="Practice · " + action, cwd=directory, focus=False)
        context = _context(client, result["root_pane"]["pane_id"])
        contexts.append(context); tabs.append(context.tab)
        _banner(context, banner)
        return context

    def workspace():
        result = client.call("workspace.create", label="Practice · " + action, cwd=directory, focus=False)
        context = _context(client, result["root_pane"]["pane_id"])
        contexts.append(context); tabs.append(context.tab); spaces.append(context.workspace)
        _banner(context, banner)
        return context

    def split(context, direction="right"):
        result = client.call("pane.split", target_pane_id=context.pane, direction=direction, cwd=directory, focus=False)
        new = _context(client, result["pane"]["pane_id"])
        contexts.append(new)
        _banner(new, banner)
        return new

    target = workspace() if action.startswith("workspace-") else tab()
    expected = {"fixture_tabs": tabs, "workspace_ring": spaces, "agent_ids": [], "simulators": {},
                "learning_mode": "hands-on" if display_chord else "speed"}
    instructions = "Practice target: " + action.replace("-", " ") + ". The shortcut from the guide acts on this disposable target."
    presses = 1
    if action.startswith("pane-swap-"):
        direction = action.removeprefix("pane-swap-")
        neighbor = split(target, "down" if direction in ("up", "down") else "right")
        if direction in ("up", "left"):
            target, neighbor = neighbor, target
        expected["neighbor"] = neighbor.pane
    elif action in ("pane-rotate", "pane-zoom", "pane-close", "pane-cycle-next", "pane-cycle-previous"):
        split(target)
        if action == "pane-zoom":
            presses = 2
            instructions += " Press twice: zoom, then restore."
    elif action in ("tab-next", "tab-previous"):
        middle, last = tab(), tab()
        target = middle
        expected["destination"] = last.tab if action.endswith("next") else tabs[0]
    elif action == "tab-close":
        # Close the newest of two owned tabs; native previous-tab fallback now
        # lands on a known disposable tab rather than earlier/user work.
        target = tab()
    elif action == "workspace-close":
        target = workspace()
    elif action in ("workspace-next", "workspace-previous"):
        last = workspace()
        if action.endswith("previous"):
            expected["destination"] = target.workspace
            target = last
        else:
            expected["destination"] = last.workspace
    elif action.startswith("agent-cycle-"):
        statuses = sorted(PRIORITY, key=PRIORITY.get)
        for status in statuses:
            agent = split(target)
            expected["simulators"][agent.pane] = _start_simulator(agent, directory)
            _report(agent, status)
            expected["agent_ids"].append(agent.pane)
        expected["agent_ring"] = list(expected["agent_ids"] if action.endswith("next") else reversed(expected["agent_ids"]))
        presses = 4
        instructions += " Four presses visit simulated approval → done → working → idle agents (reverse with Shift). No real agents start."
    elif action == "agent-new":
        instructions += " A launches a harmless local agent simulator in this lesson. It never starts your configured real agent."
    elif action == "launch-git":
        if not shutil.which("git"):
            raise ShellError("Git is unavailable; install it to rehearse the Git pane shortcut.")
        result = subprocess.run(["git", "init", "-q", directory], env=_git_environment(), capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ShellError("The disposable Git repository could not be initialized: " + result.stderr.strip())
        expected["git_executable"] = shutil.which("lazygit")
        expected["git_mode"] = "lazygit" if expected["git_executable"] else "simulator"
        if expected["git_mode"] == "simulator":
            instructions += " Lazygit is unavailable: this shortcut opens a labeled harmless Git-pane simulator instead."
    elif action == "menu":
        presses = 2
        instructions += " Press M to explore the actual read-only menu, then M again to close it."
    before = client.snapshot()
    if before.get("focused_pane_id") != guide.pane:
        raise ShellError("Focus changed while preparing fixtures. They were preserved; return to the guide.")
    ids = [identity(p) for p in before["panes"] if p["pane_id"] in {guide.pane, *(c.pane for c in contexts)}]
    plan = Plan(action, guide, target, target, directory, ids, list(spaces), [guide.tab, *tabs],
                [p["pane_id"] for p in ids], before, expected=expected,
                required_presses=presses, instructions=instructions)
    if action.startswith("pane-") or action in ("agent-new", "launch-git"):
        plan.layout = client.call("layout.export", tab_id=target.tab)["layout"]
    if action.startswith("pane-cycle-"):
        ordered = [p["pane_id"] for p in before["panes"] if p["tab_id"] == target.tab]
        expected["destination"] = ordered[(ordered.index(target.pane) + (1 if action.endswith("next") else -1)) % len(ordered)]
    return plan


def perform(plan, pressed_action):
    """Run only the armed real action, focusing its validated disposable target."""
    if pressed_action != plan.action:
        raise ShellError("Use the armed shortcut; a different shortcut cannot mutate practice.")
    if plan.presses >= plan.required_presses:
        raise ShellError("This challenge is already complete; continue from the guide.")
    _guard(plan)
    context = ScopedContext(plan)
    if plan.action in CLOSE_ACTIONS:
        proposal = close_plan(plan.action, context)
        if proposal["needs_confirmation"]:
            raise ShellError("The disposable close target has started work. It was preserved; prepare this idle-shell drill again.")
        # Re-read activity after planning; execute_close checks the pinned scope
        # and idle proposal again immediately before the actual close.
        if any(not _clean(_context(plan.guide.client, p["pane_id"])) for p in proposal["pane_identities"]):
            raise ShellError("The disposable close target is no longer idle. No close was run.")
    if not (plan.action == "menu" and plan.presses == 1):
        _focus(plan.target)
    if plan.action.startswith("agent-cycle-"):
        selected = plan.expected["agent_ring"][plan.presses]
        result = context.client.call("agent.focus", target=selected)
        plan.target = _context(plan.guide.client, selected)
        plan.history.append(selected)
    elif plan.action in CLOSE_ACTIONS:
        result = execute_close(proposal, context)
    elif plan.action == "agent-new":
        result = context.client.call("pane.split", target_pane_id=context.pane, direction="right", cwd=plan.directory, focus=True)
        created = _context(plan.guide.client, result["pane"]["pane_id"])
        plan.expected["simulator"] = _start_simulator(created, plan.directory)
        _report(created, "idle")
        plan.expected["created"] = created.pane
        plan.target = created
    elif plan.action == "launch-git":
        result = context.client.call("pane.split", target_pane_id=context.pane, direction="right", cwd=plan.directory, focus=True)
        created = _context(plan.guide.client, result["pane"]["pane_id"])
        if plan.expected["git_mode"] == "simulator":
            plan.expected["simulator"] = _start_simulator(created, plan.directory, "Git-pane (Lazygit unavailable)")
        else:
            plan.expected["git_script"] = _start_git(created, plan.directory, plan.expected["git_executable"])
        plan.expected["created"] = created.pane
        plan.target = created
    elif plan.action == "menu":
        marker = desktop.menu_running(plan.guide.socket)
        if plan.presses == 0:
            if marker:
                raise ShellError("Another menu is open; close it before this practice lesson.")
            env = {"HERDR_SHELL_GAME_MENU": "1"}
            if plan.expected.get("learning_mode") == "speed":
                env["HERDR_SHELL_GAME_MENU_MODE"] = "speed"
            result = actions.open_ui(context, "menu", env=env)
            marker = _wait(lambda: desktop.menu_running(plan.guide.socket), "The read-only practice menu did not start.")
            if marker.get("pane") != context.pane or marker.get("terminal") != context.terminal:
                raise ShellError("The menu opened on a different terminal; it was preserved.")
            plan.expected["menu_marker"] = marker
        else:
            if marker != plan.expected.get("menu_marker"):
                raise ShellError("The practice menu changed or closed. Prepare the menu drill again.")
            result = context.client.call("popup.close")
            _wait(lambda: not desktop.menu_running(plan.guide.socket), "The practice menu has not finished closing.")
        plan.history.append("opened" if plan.presses == 0 else "closed")
    else:
        result = actions.execute(plan.action, context)
        if plan.action == "pane-zoom":
            plan.history.append(plan.guide.client.call("layout.export", tab_id=plan.origin.tab)["layout"]["zoomed"])
    plan.presses += 1
    after = _refresh(plan)
    if plan.action in ("tab-next", "tab-previous", "workspace-next", "workspace-previous", "pane-cycle-next", "pane-cycle-previous"):
        plan.target = _context(plan.guide.client, after["focused_pane_id"])
    elif plan.action.startswith("pane-split-") or plan.action in ("tab-new", "workspace-new", "launch-git"):
        if after.get("focused_pane_id") in plan.owned_panes:
            plan.target = _context(plan.guide.client, after["focused_pane_id"])
    return result


def _replace(node, replacements):
    result = deepcopy(node)
    if result["type"] == "pane":
        return replacements.get(result["pane_id"], result)
    result["first"] = _replace(result["first"], replacements)
    result["second"] = _replace(result["second"], replacements)
    return result


def verified(plan, after=None):
    """Award only actual, exact fixture outcomes after the real shortcut runs."""
    if not 1 <= plan.presses <= plan.required_presses:
        return False
    after = after or plan.guide.client.snapshot()
    live = {p["pane_id"]: p for p in after["panes"]}
    guide = live.get(plan.guide.pane)
    if not guide or identity(guide) != {"pane_id": plan.guide.pane, "terminal_id": plan.guide.terminal,
                                      "workspace_id": plan.guide.workspace, "tab_id": plan.guide.tab}:
        return False
    before = {p["pane_id"]: p for p in plan.before["panes"] if p["tab_id"] == plan.origin.tab}
    current = {p["pane_id"]: p for p in after["panes"] if p["tab_id"] == plan.origin.tab}
    action, focus = plan.action, after.get("focused_pane_id")
    if action in CLOSE_ACTIONS:
        scope = CLOSE_ACTIONS[action]
        field = scope + "_id"
        target = getattr(plan.origin, scope)
        survivors = [p for p in plan.before["panes"] if p[field] != target]
        if any(identity(live.get(p["pane_id"], {})) != identity(p) for p in survivors if p["pane_id"] in live):
            return False
        if any(p["pane_id"] not in live for p in survivors):
            return False
        return not any(p[field] == target for p in after["panes"]) and (scope == "pane" or
            not any(r[field] == target for r in after[scope + "s"]))
    if any(current.get(pid, {}).get("terminal_id") != p["terminal_id"] for pid, p in before.items()):
        return False
    if action.startswith("pane-split-") or action in ("agent-new", "launch-git"):
        added = set(current) - set(before)
        if len(added) != 1 or focus not in added:
            return False
        layout = plan.guide.client.call("layout.export", tab_id=plan.origin.tab)["layout"]
        _, pair = _nearest(layout["root"], plan.origin.pane)
        direction = action.removeprefix("pane-split-") if action.startswith("pane-split-") else "right"
        ordered = [focus, plan.origin.pane] if direction in ("left", "up") else [plan.origin.pane, focus]
        if not pair or pair["direction"] != ("down" if direction in ("up", "down") else "right") or \
                [pair[s].get("pane_id") for s in ("first", "second")] != ordered:
            return False
        if _tree(layout["root"]) != _replace(_tree(plan.layout["root"]), {plan.origin.pane: _tree(pair)}):
            return False
        if action == "agent-new":
            agents = {a["pane_id"]: a for a in after.get("agents", [])}
            return agents.get(focus, {}).get("agent_status") == "idle" and _simulator_alive(_context(plan.guide.client, focus), Path(plan.expected["simulator"]))
        if action == "launch-git":
            context = _context(plan.guide.client, focus)
            if plan.expected["git_mode"] == "simulator":
                return _simulator_alive(context, Path(plan.expected["simulator"]))
            return _git_alive(context, plan.directory)
        return True
    if action.startswith("pane-swap-"):
        layout = plan.guide.client.call("layout.export", tab_id=plan.origin.tab)["layout"]
        neighbor = plan.expected["neighbor"]
        return set(current) == set(before) and focus == plan.origin.pane and _tree(layout["root"]) == _replace(
            _tree(plan.layout["root"]), {plan.origin.pane: {"type": "pane", "pane_id": neighbor},
                                       neighbor: {"type": "pane", "pane_id": plan.origin.pane}})
    if action == "pane-rotate":
        layout = plan.guide.client.call("layout.export", tab_id=plan.origin.tab)["layout"]
        expected = _tree(plan.layout["root"])
        expected["direction"] = "down" if expected["direction"] == "right" else "right"
        return set(current) == set(before) and focus == plan.origin.pane and _tree(layout["root"]) == expected
    if action == "pane-zoom":
        layout = plan.guide.client.call("layout.export", tab_id=plan.origin.tab)["layout"]
        initial = plan.layout["zoomed"]
        expected_states = [not initial, initial][:plan.presses]
        return set(current) == set(before) and focus == plan.origin.pane and plan.history == expected_states \
            and layout["zoomed"] == expected_states[-1] and _tree(layout["root"]) == _tree(plan.layout["root"])
    if action in ("pane-cycle-next", "pane-cycle-previous"):
        return set(current) == set(before) and focus == plan.expected["destination"]
    if action in ("tab-next", "tab-previous"):
        return after.get("focused_tab_id") == plan.expected["destination"] and after.get("focused_workspace_id") == plan.origin.workspace
    if action in ("workspace-next", "workspace-previous"):
        return after.get("focused_workspace_id") == plan.expected["destination"]
    if action in ("tab-new", "workspace-new"):
        kind = action.split("-")[0]
        key = kind + "_id"
        old = {r[key] for r in plan.before[kind + "s"]}
        added = {r[key] for r in after[kind + "s"]} - old
        return len(added) == 1 and after.get("focused_" + key) in added and focus in plan.owned_panes
    if action.startswith("agent-cycle-"):
        expected_ring = plan.expected["agent_ring"][:plan.presses]
        return plan.history == expected_ring and focus == expected_ring[-1] and all(
            _simulator_alive(_context(plan.guide.client, p), Path(plan.expected["simulators"][p])) for p in expected_ring)
    if action == "menu":
        marker = desktop.menu_running(plan.guide.socket)
        return (plan.history == ["opened"] and marker == plan.expected.get("menu_marker")) if plan.presses == 1 else \
            plan.history == ["opened", "closed"] and not marker
    return False


def cleanup(guide):
    """Stop unchanged owned demos and release only their agent report source.

    Panes, tabs, workspaces, and files remain. A replacement terminal/process or
    edited simulator is user work and is retained. pidfds prevent PID reuse from
    turning cleanup into a signal to another process.
    """
    directory = Path(guide.cwd)
    result = {"stopped": [], "reports_cleared": [], "preserved": []}
    failures = []
    if not directory.is_dir() or directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        return result
    for folder in directory.glob("practice-*"):
        if not folder.is_dir() or folder.is_symlink() or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077:
            continue
        for path in folder.glob(".simulator-*.json"):
            try:
                metadata = path.lstat()
                if path.is_symlink() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 or metadata.st_size > 8192:
                    continue
                record = json.loads(path.read_text())
                context = Context(**record["context"])
                script = Path(record["path"])
                if context.socket != guide.socket or script.parent != folder or script.is_symlink() or not isinstance(record["pid"], int):
                    continue
                context.validate()
                pin = record["pid"]
                try:
                    fd = os.pidfd_open(pin)
                    try:
                        process = desktop.read_process(pin)
                        if process.start != record["start"] or str(script) not in process.argv or hashlib.sha256(script.read_bytes()).hexdigest() != record["sha256"]:
                            result["preserved"].append(context.pane)
                        else:
                            signal.pidfd_send_signal(fd, signal.SIGTERM)
                            result["stopped"].append(context.pane)
                    finally:
                        os.close(fd)
                except ProcessLookupError:
                    # A demo the player already stopped still has its own
                    # report to release; other report sources are untouched.
                    pass
                try:
                    # Source and sequence constrain this to the old exercise
                    # report even if the player has started a real agent here.
                    if record.get("reported", True):
                        context.client.call("pane.clear_agent_authority", pane_id=context.pane, source="key-quest", seq=3)
                        result["reports_cleared"].append(context.pane)
                except ShellError as exc:
                    failures.append(str(exc))
            except (ShellError, OSError, ValueError, KeyError, TypeError):
                result["preserved"].append(path.name)
    if failures:
        raise ShellError("The game's own agent reports could not be released: " + "; ".join(failures))
    return result
