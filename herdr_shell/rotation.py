"""Rotate a two-pane sibling split without replacing its terminals."""
from copy import deepcopy
import fcntl
import hashlib

from .runtime import ShellError, state_path


def _nearest(node, pane):
    if node.get("type") == "pane":
        return (node.get("pane_id") == pane), None
    if node.get("type") != "split":
        raise ShellError("Herdr returned an unsupported layout.")
    for side in ("first", "second"):
        found, parent = _nearest(node[side], pane)
        if found:
            return True, parent or node
    return False, None


def _tree(node):
    if node["type"] == "pane":
        return {"type": "pane", "pane_id": node["pane_id"]}
    return {"type": "split", "direction": node["direction"], "ratio": node["ratio"],
            "first": _tree(node["first"]), "second": _tree(node["second"])}


def _identities(snapshot, tab):
    return sorted((p["pane_id"], p["terminal_id"]) for p in snapshot["panes"] if p["tab_id"] == tab)


def _export(client, tab):
    result = client.call("layout.export", tab_id=tab)
    return result.get("layout", result)


def rotation_unavailable(context):
    """Read-only eligibility hint; nested groups require an upstream rotate API."""
    try:
        layout = _export(context.client, context.tab)
        found, split = _nearest(layout["root"], context.pane)
        if not found:
            return "The originating pane is no longer in this layout."
        if split is None:
            return "Split this tab into two panes before rotating."
        if any(split[side].get("type") != "pane" for side in ("first", "second")):
            return "Rotation needs two sibling panes; this split contains a nested pane group."
        return ""
    except (ShellError, OSError, KeyError, TypeError, ValueError) as exc:
        return "Cannot verify whether this split can rotate: " + str(exc)


def rotate_pane(context):
    # Herdr 0.9.3 layout.apply recreates terminals. A same-workspace staging
    # move preserves pane IDs and terminals; never substitute layout.apply.
    root = state_path() / "cycles"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(context.socket.encode()).hexdigest()[:24]
    with (root / (key + "-rotation.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        context.validate()
        client = context.client
        layout = _export(client, context.tab)
        found, split = _nearest(layout["root"], context.pane)
        if not found:
            raise ShellError("The originating pane is no longer in this layout.")
        if split is None:
            raise ShellError("Split this tab into two panes before rotating.")
        if any(split[side].get("type") != "pane" for side in ("first", "second")):
            raise ShellError("This split contains a nested pane group. Herdr cannot rotate that group while preserving its terminals; rotate a two-pane sibling split instead.")
        first, second = split["first"]["pane_id"], split["second"]["pane_id"]
        original = _tree(layout["root"])
        desired = deepcopy(original)
        _, changed = _nearest(desired, context.pane)
        changed["direction"] = "down" if split["direction"] == "right" else "right"
        identities = _identities(client.snapshot(), context.tab)
        if _tree(_export(client, context.tab)["root"]) != original:
            raise ShellError("The pane layout changed. Press the shortcut again.")
        focus = layout.get("focused_pane_id")

        def restore_focus():
            if focus:
                client.call("pane.focus", pane_id=focus)
            if layout.get("zoomed"):
                client.call("pane.zoom", pane_id=focus or context.pane, mode="on")

        # Herdr 0.9.3 can restore the old split orientation when moving a pane
        # back into a zoomed tab. Expose the layout during the move, then restore
        # the captured zoom after its existing terminals return.
        if layout.get("zoomed"):
            client.call("pane.zoom", pane_id=focus or context.pane, mode="off")
            if _tree(_export(client, context.tab)["root"]) != original:
                restore_focus()
                raise ShellError("The pane layout changed while restoring its size. Press the shortcut again.")
        try:
            moved = client.call("pane.move", pane_id=second, focus=False,
                                destination={"type": "new_tab", "workspace_id": context.workspace,
                                             "label": "Herdr rotation"})
        except Exception:
            restore_focus()
            raise
        staged = moved.get("move_result", moved).get("pane", {})
        source_identity = next((i for i in identities if i[0] == second), None)
        if (staged.get("pane_id"), staged.get("terminal_id")) != source_identity:
            raise ShellError("Herdr changed a pane identity while staging rotation. Its process remains in the rotation tab; no pane was closed.")
        try:
            client.call("pane.move", pane_id=second, focus=False,
                        destination={"type": "tab", "tab_id": context.tab, "target_pane_id": first,
                                     "split": changed["direction"], "ratio": split["ratio"]})
        except Exception as exc:
            try:
                client.call("pane.move", pane_id=second, focus=False,
                            destination={"type": "tab", "tab_id": context.tab, "target_pane_id": first,
                                         "split": split["direction"], "ratio": split["ratio"]})
                restore_focus()
                recovered_tree = _tree(_export(client, context.tab)["root"])
                if _identities(client.snapshot(), context.tab) != identities:
                    raise ShellError("Pane identities changed during recovery")
                if recovered_tree == desired:
                    # The first return may have completed before its response
                    # was lost. Same-tab move is a no-op, so verify success.
                    return {"changed": True, "direction": changed["direction"], "pane_ids": [first, second]}
                if recovered_tree != original:
                    raise ShellError("The original split could not be verified after recovery")
            except Exception as recovery:
                raise ShellError(f"Rotation could not finish or restore the layout. Pane {second} remains running; check the rotation tab. {recovery}") from exc
            raise ShellError("Rotation failed; the original split was restored: " + str(exc)) from exc
        restore_focus()
        if _identities(client.snapshot(), context.tab) != identities or _tree(_export(client, context.tab)["root"]) != desired:
            raise ShellError("Herdr did not preserve the expected rotated layout. The existing panes remain running; inspect their positions.")
        return {"changed": True, "direction": changed["direction"], "pane_ids": [first, second]}
