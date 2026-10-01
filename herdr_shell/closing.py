"""Activity-aware close proposals pinned to the reviewed pane identities."""
from pathlib import Path

from .runtime import ShellError


CLOSE_ACTIONS = {"pane-close": "pane", "tab-close": "tab", "workspace-close": "workspace"}


def shell_jobs(shell_pid, proc_root=Path("/proc")):
    """Find live background/suspended children even while the shell owns the TTY."""
    if not isinstance(shell_pid, int) or isinstance(shell_pid, bool) or shell_pid <= 0:
        raise ShellError("Cannot identify the pane shell process")
    folder = proc_root / str(shell_pid)
    children = (folder / "task" / str(shell_pid) / "children").read_text().split()
    if len(children) > 256:
        raise ShellError("The pane shell process tree is too large to verify")
    jobs = []
    for child in children:
        if not child.isdecimal():
            raise ShellError("Cannot read the pane shell process tree")
        try:
            stat = (proc_root / child / "stat").read_text()
        except FileNotFoundError:
            # A child that exited between the list and stat reads is no work
            # that the proposed close would terminate.
            continue
        end = stat.rfind(")")
        start = stat.find("(")
        fields = stat[end + 2:].split()
        if start < 0 or end <= start or len(fields) < 2:
            raise ShellError("Cannot read a pane shell child process")
        state, parent = fields[0], int(fields[1])
        if parent != shell_pid or state in ("Z", "X", "x"):
            continue
        name = stat[start + 1:end] or "command"
        jobs.append(("Suspended job " if state in ("T", "t") else "Background job ") + name)
    return jobs


def _scope(kind, context, snapshot):
    target = getattr(context, kind if kind != "workspace" else "workspace")
    if kind == "pane":
        panes = [p for p in snapshot["panes"] if p["pane_id"] == target]
    elif kind == "tab":
        panes = [p for p in snapshot["panes"] if p["tab_id"] == target]
    else:
        panes = [p for p in snapshot["panes"] if p["workspace_id"] == target]
    if not panes:
        raise ShellError("The close target no longer contains its original panes. Reopen the menu.")
    tabs = [t for t in snapshot["tabs"] if
            (t["workspace_id"] == target if kind == "workspace" else t["tab_id"] == context.tab)]
    identities = sorted(({k: p[k] for k in ("pane_id", "terminal_id", "workspace_id", "tab_id")} for p in panes),
                        key=lambda p: p["pane_id"])
    topology = sorted(({"tab_id": t["tab_id"], "workspace_id": t["workspace_id"]} for t in tabs),
                      key=lambda t: t["tab_id"])
    return target, panes, identities, topology


def _activity(context, snapshot, panes):
    agents = {a["pane_id"]: a for a in snapshot.get("agents", [])}
    active = []
    for pane in panes:
        label = pane.get("label") or pane.get("title") or pane["pane_id"]
        agent = agents.get(pane["pane_id"])
        if agent is not None or pane.get("agent"):
            info = agent or pane
            reason = "Agent " + (info.get("agent") or info.get("name") or "present") + " (" + info.get("agent_status", "unknown") + ")"
        else:
            try:
                result = context.client.call("pane.process_info", pane_id=pane["pane_id"])
                info = result.get("process_info", result)
                shell = info.get("shell_pid")
                processes = info.get("foreground_processes", [])
                clean = (shell is not None and info.get("foreground_process_group_id") == shell and
                         bool(processes) and all(p.get("pid") == shell for p in processes))
                if clean:
                    jobs = shell_jobs(shell)
                    if not jobs:
                        continue
                    reason = "; ".join(jobs)
                else:
                    foreground = [p for p in processes if p.get("pid") != shell]
                    reason = "Running " + ", ".join(p.get("name") or "command" for p in foreground) if foreground else "Process activity could not be verified"
            except (ShellError, OSError, KeyError, TypeError, ValueError):
                reason = "Process activity could not be verified"
        active.append({"pane_id": pane["pane_id"], "label": label, "reason": reason})
    return active


def close_plan(action_id, context):
    if action_id not in CLOSE_ACTIONS:
        raise ShellError("Unknown close action: " + action_id)
    context.validate()
    snapshot = context.client.snapshot()
    kind = CLOSE_ACTIONS[action_id]
    target, panes, identities, topology = _scope(kind, context, snapshot)
    rows = snapshot["panes"] if kind == "pane" else snapshot[kind + "s"]
    row = next((r for r in rows if r[kind + "_id"] == target), {})
    label = row.get("label") or row.get("title") or target
    activity = _activity(context, snapshot, panes)
    return {"action_id": action_id, "kind": kind, "target_id": target, "title": f"Close {kind} {label}?",
            "pane_identities": identities, "tab_identities": topology, "count": len(identities),
            "activity": activity, "needs_confirmation": bool(activity)}


def execute_close(plan, context):
    """Execute a confirmed plan, rejecting newly added, moved, or replaced panes."""
    action = plan.get("action_id")
    if CLOSE_ACTIONS.get(action) != plan.get("kind"):
        raise ShellError("Invalid close proposal. Reopen the menu.")
    current = close_plan(action, context)
    for field in ("kind", "target_id", "pane_identities", "tab_identities", "count"):
        if current[field] != plan.get(field):
            raise ShellError("The panes affected by closing changed. Reopen the menu and review the close again.")
    if current["needs_confirmation"] and not plan.get("needs_confirmation") and not plan.get("confirmed"):
        raise ShellError("A command or agent started after the close was prepared. Review the close again.")
    return context.client.call(current["kind"] + ".close", **{current["kind"] + "_id": current["target_id"]})
