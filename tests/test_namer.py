from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from herdr_shell import naming, namer
from herdr_shell.runtime import ShellError


def agent_pane(pane_id, tab_id, title, cwd="/src/riptide"):
    return {"pane_id": pane_id, "tab_id": tab_id, "workspace_id": "w1", "agent": "omp", "cwd": cwd,
            "terminal_title": title, "terminal_title_stripped": title, "tokens": {}}


class FakeHerdr:
    """Applies renames and metadata like the server and records call order."""

    def __init__(self, tabs, panes):
        self.tabs, self.panes, self.calls = tabs, panes, []
        self.fail_renames = False

    def snapshot(self):
        return {"tabs": deepcopy(self.tabs), "panes": deepcopy(self.panes)}

    def tab(self, tab_id):
        return next(tab for tab in self.tabs if tab["tab_id"] == tab_id)

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "tab.rename":
            if self.fail_renames:
                raise ShellError("tab busy")
            self.tab(params["tab_id"])["label"] = params["label"]
        elif method == "pane.report_metadata":
            pane = next(pane for pane in self.panes if pane["pane_id"] == params["pane_id"])
            for key, value in params["tokens"].items():
                if value is None:
                    pane["tokens"].pop(key, None)
                else:
                    pane["tokens"][key] = value
        else:
            raise AssertionError("unexpected call " + method)

    def renames(self):
        return [params["label"] for method, params in self.calls if method == "tab.rename"]

    def marked(self, pane_id):
        pane = next(pane for pane in self.panes if pane["pane_id"] == pane_id)
        return pane["tokens"].get(naming.MARKER_TOKEN) == naming.MARKER_VALUE


class NamerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, HERDR_SHELL_DESKTOP_CONFIG_HOME=str(self.folder / "config"))
        environment.start()
        self.addCleanup(environment.stop)
        self.now = 100.0

    def namer(self, herdr, ledger_name="ledger.json"):
        return namer.Namer(herdr, namer.Ledger(self.folder / ledger_name), clock=lambda: self.now)

    def full_pass(self, worker):
        worker.request_full()
        self.assertTrue(worker.step())

    def test_failed_marker_prevents_task_rename_until_retry(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}],
                          [agent_pane("p1", "w1:t1", "π > Fix login")])
        worker = self.namer(herdr)
        original = herdr.call
        def rejecting(method, **params):
            if method == "pane.report_metadata":
                raise ShellError("metadata_token_limit")
            return original(method, **params)
        herdr.call = rejecting
        self.full_pass(worker)
        self.assertEqual(herdr.renames(), [])
        herdr.call = original
        self.now += 60
        self.full_pass(worker)
        self.assertEqual(herdr.renames(), ["Fix login"])

    def test_unnamed_agent_tab_takes_short_task_title_after_marking_agents(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}],
                          [agent_pane("p1", "w1:t1", "π ⠹ Complete cast and error-radio integration")])
        self.full_pass(self.namer(herdr))
        self.assertEqual(herdr.tab("w1:t1")["label"], "Complete cast and…")
        methods = [method for method, _ in herdr.calls]
        # A dashboard that hides prompt-derived names must never see the label unmarked.
        self.assertLess(methods.index("pane.report_metadata"), methods.index("tab.rename"))
        self.assertTrue(herdr.marked("p1"))

    def test_manual_rename_pins_the_tab_and_clears_its_marker(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}],
                          [agent_pane("p1", "w1:t1", "π ⠹ Fix login")])
        worker = self.namer(herdr)
        self.full_pass(worker)
        herdr.tab("w1:t1")["label"] = "silas"
        herdr.panes[0]["terminal_title_stripped"] = "π ⠹ Harden the server"
        self.now += 60
        self.full_pass(worker)
        self.assertEqual(herdr.tab("w1:t1")["label"], "silas")
        self.assertEqual(herdr.renames(), ["Fix login"])
        self.assertFalse(herdr.marked("p1"))

    def test_position_label_hands_a_pinned_tab_back(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "silas"}],
                          [agent_pane("p1", "w1:t1", "π > Fix login")])
        worker = self.namer(herdr)
        self.full_pass(worker)
        self.assertEqual(herdr.renames(), [])
        herdr.tab("w1:t1")["label"] = "1"   # what "Name tab from agent task" sets
        self.now += 1
        self.full_pass(worker)
        self.assertEqual(herdr.tab("w1:t1")["label"], "Fix login")

    def test_tabs_named_by_other_tools_are_never_touched(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "card-12"},
                           {"tab_id": "w1:t2", "workspace_id": "w1", "label": "Practice · pane-zoom"}],
                          [agent_pane("p1", "w1:t1", "π ⠹ Implement card"),
                           agent_pane("p2", "w1:t2", "π ⠹ Practice")])
        self.full_pass(self.namer(herdr))
        self.assertEqual(herdr.calls, [])

    def test_spinner_frames_schedule_no_work_but_a_new_task_does(self):
        worker = self.namer(FakeHerdr([], []))
        update = lambda title: worker.observe({"event": "pane_updated",
                                               "data": {"pane": agent_pane("p1", "w1:t1", title)}})
        update("π ⠋ Fix login")
        worker.changed.clear()
        for frame in ("π ⠙ Fix login", "π ⠹ Fix login", "π > Fix login"):
            update(frame)
        self.assertEqual(worker.changed, {})
        update("π ⠋ Ship it")
        self.assertEqual(set(worker.changed), {"w1:t1"})

    def test_new_task_settles_and_renames_are_rate_limited(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}],
                          [agent_pane("p1", "w1:t1", "π ⠹ Fix login")])
        worker = self.namer(herdr)
        self.full_pass(worker)
        herdr.panes[0]["terminal_title_stripped"] = "π ⠹ Ship release"
        self.now += 1
        worker.observe({"event": "pane_updated", "data": {"pane": dict(herdr.panes[0])}})
        self.now += 1
        worker.step()                       # still settling
        self.now += 1.5
        worker.step()                       # settled, but within the per-tab interval
        self.assertEqual(herdr.renames(), ["Fix login"])
        self.now += 7
        worker.step()
        self.assertEqual(herdr.renames(), ["Fix login", "Ship release"])

    def test_agent_exit_keeps_the_last_task_name(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}],
                          [agent_pane("p1", "w1:t1", "π > Fix login")])
        worker = self.namer(herdr)
        self.full_pass(worker)
        herdr.panes[0].update(agent=None, terminal_title_stripped="fish ~/src/riptide")
        self.now += 60
        self.full_pass(worker)
        self.assertEqual(herdr.tab("w1:t1")["label"], "Fix login")
        self.assertFalse(herdr.marked("p1"))

    def test_herdr_default_mode_restores_positions_and_unmarks(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "notes"},
                           {"tab_id": "w1:t2", "workspace_id": "w1", "label": "2"}],
                          [agent_pane("p1", "w1:t1", "π > Keep"), agent_pane("p2", "w1:t2", "π > Fix login")])
        worker = self.namer(herdr)
        self.full_pass(worker)
        self.assertEqual(herdr.tab("w1:t2")["label"], "Fix login")
        naming.set_preference("tab_mode", "numbered")
        self.now += 60
        self.full_pass(worker)
        self.assertEqual([tab["label"] for tab in herdr.tabs], ["notes", "2"])
        self.assertFalse(herdr.marked("p2"))

    def test_first_run_preserves_manual_directory_label(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "riptide"}],
                          [agent_pane("p1", "w1:t1", "π > Fix login")])
        self.full_pass(self.namer(herdr))
        self.assertEqual(herdr.tab("w1:t1")["label"], "riptide")
        later = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "Fix login"},
                           {"tab_id": "w1:t2", "workspace_id": "w1", "label": "riptide"}],
                          [agent_pane("p1", "w1:t1", "π > Fix login"), agent_pane("p2", "w1:t2", "π > Ship it")])
        self.full_pass(self.namer(later))   # same ownership ledger
        self.assertEqual(later.tab("w1:t2")["label"], "riptide")

    def test_failed_or_interrupted_rename_keeps_the_tab_owned(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}],
                          [agent_pane("p1", "w1:t1", "π > Fix login")])
        worker = self.namer(herdr)
        herdr.fail_renames = True
        self.full_pass(worker)
        self.assertEqual(herdr.tab("w1:t1")["label"], "1")
        herdr.fail_renames = False
        self.now += 60
        self.full_pass(worker)
        self.assertEqual(herdr.tab("w1:t1")["label"], "Fix login")
        # A process stopped after saving the ledger but before renaming.
        restarted = self.namer(herdr)
        restarted.ledger.tabs["w1:t1"] = {"label": "Ship it", "previous": "Fix login", "topic": True}
        herdr.panes[0]["terminal_title_stripped"] = "π > Ship it"
        self.full_pass(restarted)
        self.assertEqual(herdr.tab("w1:t1")["label"], "Ship it")

    def test_manual_rename_during_marker_call_is_preserved(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}],
                          [agent_pane("p1", "w1:t1", "π > Fix login")])
        original = herdr.call
        def rename_during_mark(method, **params):
            result = original(method, **params)
            if method == "pane.report_metadata":
                herdr.tab("w1:t1")["label"] = "notes"
            return result
        herdr.call = rename_during_mark
        self.full_pass(self.namer(herdr))
        self.assertEqual(herdr.tab("w1:t1")["label"], "notes")

    def test_topic_marker_is_renewed_before_expiry(self):
        herdr = FakeHerdr([{"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}],
                          [agent_pane("p1", "w1:t1", "π > Fix login")])
        worker = self.namer(herdr)
        self.full_pass(worker)
        herdr.calls.clear()
        self.now += namer.MARKER_TTL_MS / 1000 - 60
        self.full_pass(worker)
        self.assertTrue(any(method == "pane.report_metadata" for method, _ in herdr.calls))
        self.assertEqual(herdr.renames(), [])

    def test_failed_pass_request_waits_five_seconds(self):
        worker = self.namer(FakeHerdr([], []))
        worker.request_full(5.0, retry=True)
        self.assertFalse(worker.step())
        self.now += 5
        self.assertTrue(worker.step())

    def test_socket_symlink_shares_daemon_paths(self):
        socket = self.folder / "server.sock"
        socket.touch()
        alias = self.folder / "alias.sock"
        alias.symlink_to(socket)
        self.assertEqual(namer.paths(socket), namer.paths(alias))

    def test_running_daemon_caps_its_log(self):
        import contextlib
        class Stopping:
            def __init__(self):
                self.passes = 0
            def is_set(self):
                self.passes += 1
                return self.passes > 4
            def wait(self, delay):
                pass
            def set(self):
                self.passes = 5
        class Client:
            def __init__(self, path):
                pass
            def call(self, method):
                return {"plugins": [{"plugin_id": namer.PLUGIN_ID}]}
            def snapshot(self):
                raise ShellError("x" * namer.LOG_LIMIT)
        log = self.folder / "daemon.log"
        with log.open("a+") as output, contextlib.redirect_stdout(output), \
             patch.object(namer, "Client", Client), \
             patch.object(namer.threading, "Event", Stopping), \
             patch.object(namer.signal, "signal"), \
             patch.object(namer, "EventStream") as stream:
            stream.return_value.alive.return_value = True
            namer.run(self.folder / "server.sock")
        self.assertLessEqual(log.stat().st_size, namer.LOG_LIMIT)
