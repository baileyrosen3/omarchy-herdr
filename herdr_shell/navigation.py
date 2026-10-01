"""Deterministic pane navigation and persistent, non-starving agent sweeps."""
import fcntl
import hashlib
import json

from .config import atomic_write
from .runtime import ShellError, state_path


PRIORITY = {"blocked": 0, "done": 1, "working": 2, "idle": 3}


def cycle_pane(context, step):
    panes = [p for p in context.client.snapshot()["panes"] if p["tab_id"] == context.tab]
    ids = [p["pane_id"] for p in panes]
    if context.pane not in ids:
        raise ShellError("The originating pane is no longer in this tab.")
    target = ids[(ids.index(context.pane) + step) % len(ids)]
    return context.client.call("pane.focus", pane_id=target)


def _identity(agent):
    return {"pane_id": agent["pane_id"], "terminal_id": agent["terminal_id"]}


def _new_sweep(snapshot):
    workspace_order = {w["workspace_id"]: i for i, w in enumerate(snapshot.get("workspaces", []))}
    tab_order = {t["tab_id"]: i for i, t in enumerate(snapshot.get("tabs", []))}
    pane_order = {p["pane_id"]: i for i, p in enumerate(snapshot.get("panes", []))}
    agents = [a for a in snapshot.get("agents", []) if a.get("agent_status") in PRIORITY]
    agents.sort(key=lambda a: (PRIORITY[a["agent_status"]], workspace_order.get(a["workspace_id"], 10**9),
                               tab_order.get(a["tab_id"], 10**9), pane_order.get(a["pane_id"], 10**9)))
    return [_identity(a) for a in agents]


def cycle_agent(context, step):
    """Freeze priority order until wrap; focusing Done must not reorder the ring."""
    root = state_path() / "cycles"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(context.socket.encode()).hexdigest()[:24]
    path = root / (key + ".json")
    with (root / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        snapshot = context.client.snapshot()
        live = {(a["pane_id"], a["terminal_id"]) for a in snapshot.get("agents", [])
                if a.get("agent_status") in PRIORITY}
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        order = [a for a in state.get("order", []) if (a.get("pane_id"), a.get("terminal_id")) in live]
        # An external focus choice begins a fresh attention-first sweep.
        resume = bool(order) and state.get("last") == context.pane
        index = next((i for i, a in enumerate(order) if a["pane_id"] == context.pane), None) if resume else None
        if index is None or not 0 <= index + step < len(order):
            order = _new_sweep(snapshot)
            if not order:
                raise ShellError("No agents have a known state to cycle. Unknown agents remain available in the pane picker.")
            index = 0 if step > 0 else len(order) - 1
            # A fresh cycle should move when there is another eligible agent.
            if len(order) > 1 and order[index]["pane_id"] == context.pane:
                index += step
        else:
            index += step
        selected = order[index]
        # Agent focus validates recognized agent identity and marks completion seen.
        current = context.client.call("agent.get", target=selected["pane_id"])
        agent = current.get("agent", current)
        if agent.get("terminal_id") != selected["terminal_id"]:
            raise ShellError("The selected agent changed. Press the shortcut again.")
        if agent.get("agent_status") not in PRIORITY:
            raise ShellError("The selected agent now has an unknown state. Press the shortcut again or use the pane picker.")
        result = context.client.call("agent.focus", target=selected["pane_id"])
        atomic_write(path, json.dumps({"order": order, "last": selected["pane_id"]}))
        return result
