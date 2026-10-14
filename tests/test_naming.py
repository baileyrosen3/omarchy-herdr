import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from herdr_shell import naming


# Shape recorded from a live Herdr 0.9.3 session: omp sets an agent-style
# terminal title, herdr-agent-usage publishes quota_* tokens; there is no
# "summary" token and no pane "title"/"label".
OMP_PANE = {
    "pane_id": "w1B:p3", "tab_id": "w1B:t3", "workspace_id": "w1B", "agent": "omp",
    "agent_status": "working", "cwd": "/home/u/src/xeneon-edge-agents",
    "foreground_cwd": "/home/u/src/xeneon-edge-agents",
    "terminal_title": "π ⠹ Complete cast and error-radio integration",
    "terminal_title_stripped": "π ⠹ Complete cast and error-radio integration",
    "tokens": {"quota_topic": "Complete cast and error-radio integration",
               "quota_group": "xeneon-edge-agents", "quota_cwd": "omarchy/xeneon-edge-agents",
               "edge_dialog_instance": "2afde3b13fd03904bc14f2cd05bfe102"},
}


class NamingPreferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, HERDR_SHELL_DESKTOP_CONFIG_HOME=str(Path(temporary.name) / "config"))
        environment.start()
        self.addCleanup(environment.stop)

    def test_defaults_follow_agent_tasks_and_choices_persist(self):
        self.assertEqual(naming.preferences(), {"tab_mode": "task", "workspace_mode": "project"})
        self.assertTrue(naming.set_preference("tab_mode", "numbered"))
        self.assertFalse(naming.set_preference("tab_mode", "numbered"))
        self.assertTrue(naming.set_preference("workspace_mode", "folder"))
        self.assertEqual(json.loads(naming.preferences_path().read_text()),
                         {"tab_mode": "numbered", "workspace_mode": "folder"})
        with self.assertRaises(ValueError):
            naming.set_preference("pane_mode", "native")

    def test_unknown_or_retired_values_fall_back_to_defaults(self):
        path = naming.preferences_path()
        path.parent.mkdir(parents=True)
        path.write_text('{"tab_mode": "directory", "workspace_mode": 1, "pane_mode": "native"}')
        self.assertEqual(naming.preferences(), {"tab_mode": "task", "workspace_mode": "project"})
        path.write_text("not JSON")
        self.assertEqual(naming.preferences(), {"tab_mode": "task", "workspace_mode": "project"})


class TaskTitleTests(unittest.TestCase):
    def test_agent_style_titles_yield_the_task_in_every_state(self):
        for title in ("π ⠹ Fix login redirect", "π > Fix login redirect", "✳ Fix login redirect",
                      "π ⠸ Fix\u2060 login\u200b  redirect"):
            with self.subTest(title=title):
                self.assertEqual(naming.title_topic(title), "Fix login redirect")

    def test_shell_and_editor_titles_and_bare_prefixes_are_not_tasks(self):
        for title in ("fish ~/src/riptide", "nvim notes.md", "riptide", "π ⠹", "", None):
            with self.subTest(title=title):
                self.assertIsNone(naming.title_topic(title))

    def test_live_omp_record_names_its_task(self):
        self.assertEqual(naming.agent_topic(OMP_PANE), "Complete cast and error-radio integration")

    def test_chat_title_token_covers_agents_without_agent_style_titles(self):
        pane = dict(OMP_PANE, agent="claude", terminal_title_stripped="claude", terminal_title="claude",
                    tokens={"quota_topic": "Review parser errors"})
        self.assertEqual(naming.agent_topic(pane), "Review parser errors")

    def test_never_names_from_agent_name_directory_untitled_or_labels(self):
        cases = {
            "agent name": dict(OMP_PANE, terminal_title_stripped="π omp", tokens={}),
            "directory": dict(OMP_PANE, terminal_title_stripped="π ⠹ xeneon-edge-agents", tokens={}),
            "untitled": dict(OMP_PANE, terminal_title_stripped="π", tokens={"quota_topic": "Untitled"}),
            # A label the namer set earlier must never feed back in as a task.
            "label": {"pane_id": "p", "agent": "omp", "label": "riptide · Fix login", "tokens": {}},
            "no agent": dict(OMP_PANE, agent=None),
        }
        for name, pane in cases.items():
            with self.subTest(name):
                self.assertIsNone(naming.agent_topic(pane))

    def test_short_label_fits_tab_bar_at_word_boundary(self):
        self.assertEqual(naming.short_label("Fix login"), "Fix login")
        label = naming.short_label("Complete cast and error-radio integration")
        self.assertEqual(label, "Complete cast and…")
        self.assertLessEqual(len(label), naming.TAB_LABEL_LIMIT)
        self.assertEqual(naming.short_label("x" * 40), "x" * (naming.TAB_LABEL_LIMIT - 1) + "…")


class TabIdentityTests(unittest.TestCase):
    def test_default_label_is_live_position_within_each_workspace(self):
        snapshot = {"tabs": [{"tab_id": "w1:t1", "workspace_id": "w1"}, {"tab_id": "w2:t9", "workspace_id": "w2"},
                             {"tab_id": "w1:t3", "workspace_id": "w1"}]}
        self.assertEqual(naming.tab_positions(snapshot), {"w1:t1": "1", "w2:t9": "1", "w1:t3": "2"})



class WorkspaceLabelTests(unittest.TestCase):
    def test_project_root_then_branch_then_suffix_never_a_task(self):
        def git(cwd, *args):
            return "/repos/project" if args[0] == "rev-parse" else "feature"
        with patch.object(naming, "_git", side_effect=git):
            self.assertEqual(naming.workspace_label("/repos/project/sub", "project", ["other"]), "project")
            self.assertEqual(naming.workspace_label("/repos/project/sub", "project", ["Project"]), "project · feature")
            self.assertEqual(naming.workspace_label("/repos/project/sub", "project",
                                                    ["project", "project · feature"]), "project · 2")

    def test_folder_mode_and_non_git_directories_use_the_directory(self):
        with patch.object(naming, "_git", return_value=None) as git:
            self.assertEqual(naming.workspace_label("/tmp/scratch", "project", []), "scratch")
        with patch.object(naming, "_git") as git:
            self.assertEqual(naming.workspace_label("/repos/project/sub", "folder", []), "sub")
            git.assert_not_called()
