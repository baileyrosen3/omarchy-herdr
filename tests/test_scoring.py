from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from herdr_shell.scoring import ActiveTimer, SpeedScore, read_results, save_result


class ActiveTimerTests(unittest.TestCase):
    def test_fixture_preparation_verification_and_pause_do_not_spend_run_time(self):
        timer = ActiveTimer(120)
        self.assertEqual(timer.remaining(100), 120)
        timer.arm(100)
        self.assertEqual(timer.response(102), 2)
        # The API verifies the action for ten seconds before the next target.
        self.assertEqual(timer.remaining(112), 118)
        timer.arm(112)
        timer.suspend(115)
        # Focus was lost while the response was armed.
        timer.resume(200)
        self.assertEqual(timer.response(202), 5)
        self.assertEqual(timer.remaining(250), 113)

    def test_wrong_chord_does_not_reset_response_time_or_charge_feedback(self):
        timer = ActiveTimer(10)
        timer.arm(0)
        self.assertEqual(timer.response(2), 2)
        timer.resume(5)
        self.assertEqual(timer.response(8), 5)
        self.assertEqual(timer.remaining(20), 5)

    def test_receipt_time_wins_over_a_later_ui_poll(self):
        timer = ActiveTimer(10)
        timer.arm(100)
        self.assertEqual(timer.remaining(105), 5)
        self.assertEqual(timer.response(101.25), 1.25)
        self.assertEqual(timer.remaining(110), 8.75)

    def test_resetting_response_preserves_time_already_used(self):
        timer = ActiveTimer(10)
        timer.arm(0)
        timer.arm(3)
        self.assertEqual(timer.response(5), 2)
        self.assertEqual(timer.elapsed(20), 5)

    def test_repeated_suspend_and_resume_do_not_double_count(self):
        timer = ActiveTimer(10)
        timer.arm(0)
        timer.resume(1)
        timer.suspend(2)
        timer.suspend(3)
        timer.resume(4)
        timer.resume(5)
        self.assertEqual(timer.response(6), 4)

    def test_budget_expires_without_negative_remaining_time(self):
        timer = ActiveTimer(5)
        timer.arm(10)
        self.assertFalse(timer.expired(14.99))
        self.assertTrue(timer.expired(15))
        self.assertEqual(timer.remaining(20), 0)

    def test_disarmed_time_does_not_allow_unprepared_responses(self):
        timer = ActiveTimer(5)
        timer.arm(0)
        timer.disarm(1)
        self.assertEqual(timer.remaining(100), 4)
        with self.assertRaises(RuntimeError):
            timer.response(100)
        with self.assertRaises(RuntimeError):
            timer.resume(100)

    def test_technical_failure_refunds_only_the_current_response(self):
        timer = ActiveTimer(120)
        timer.arm(0)
        self.assertEqual(timer.response(3), 3)
        timer.arm(10)
        self.assertEqual(timer.response(12), 2)
        timer.resume(20)
        # Failure happens after another four active seconds and a long pause.
        timer.suspend(24)
        timer.cancel_response(100, refund=True)
        self.assertEqual(timer.elapsed(200), 3)
        self.assertEqual(timer.remaining(200), 117)
        self.assertFalse(timer.armed)
        timer.arm(200)
        self.assertEqual(timer.response(201), 1)
        self.assertEqual(timer.elapsed(300), 4)

    def test_refund_cancels_a_running_response_and_is_idempotent(self):
        timer = ActiveTimer(10)
        timer.arm(0)
        timer.cancel_response(4, refund=True)
        timer.cancel_response(6, refund=True)
        self.assertEqual(timer.remaining(20), 10)
        self.assertFalse(timer.running)
        with self.assertRaises(RuntimeError):
            timer.resume(20)

    def test_normal_cancellation_keeps_elapsed_time_without_carrying_response(self):
        timer = ActiveTimer(10)
        timer.arm(0)
        timer.cancel_response(4)
        self.assertEqual(timer.remaining(10), 6)
        timer.arm(10)
        self.assertEqual(timer.response(12), 2)
        self.assertEqual(timer.remaining(15), 4)

    def test_bad_timestamps_cannot_change_the_clock(self):
        timer = ActiveTimer(5)
        timer.arm(10)
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                timer.response(value)
        with self.assertRaises(ValueError):
            timer.response(9)
        self.assertEqual(timer.response(12), 2)

    def test_invalid_budget_is_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), "not a duration"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ActiveTimer(value)


class SpeedScoreTests(unittest.TestCase):
    def test_fast_verified_press_earns_bonus_and_a_following_press_builds_streak(self):
        score = SpeedScore()
        self.assertEqual(score.award(0.5), 200)
        self.assertEqual(score.award(1), 210)
        self.assertEqual(score.points, 410)
        self.assertEqual(score.best_streak, 2)
        self.assertEqual(score.attempts, 2)
        self.assertEqual(score.accuracy, 100)

    def test_speed_bonus_is_bounded_and_reduces_with_response_time(self):
        self.assertEqual(SpeedScore.speed_bonus(0), 100)
        self.assertEqual(SpeedScore.speed_bonus(1), 100)
        self.assertEqual(SpeedScore.speed_bonus(4.5), 50)
        self.assertEqual(SpeedScore.speed_bonus(8), 0)
        self.assertEqual(SpeedScore.speed_bonus(80), 0)
        score = SpeedScore()
        self.assertEqual(score.award(10), 100)

    def test_streak_bonus_stops_growing_after_ten_extra_presses(self):
        score = SpeedScore()
        awards = [score.award(0.5) for _ in range(20)]
        self.assertEqual(awards[0], 200)
        self.assertEqual(awards[10:], [300] * 10)
        self.assertEqual(score.best_streak, 20)

    def test_wrong_chord_resets_streak_deducts_points_and_counts_for_accuracy(self):
        score = SpeedScore()
        score.award(1)
        self.assertEqual(score.mistake(), 25)
        self.assertEqual(score.award(1), 200)
        self.assertEqual(score.points, 375)
        self.assertEqual(score.streak, 1)
        self.assertAlmostEqual(score.accuracy, 200 / 3)
        self.assertEqual(score.attempts, 3)

    def test_penalty_never_makes_a_new_run_negative(self):
        score = SpeedScore()
        self.assertEqual(score.mistake(), 0)
        self.assertEqual(score.points, 0)
        self.assertEqual(score.wrong, 1)
        self.assertEqual(score.accuracy, 0)

    def test_hinted_press_keeps_base_points_and_breaks_bonus_streak(self):
        score = SpeedScore()
        score.award(1)
        self.assertEqual(score.award(0.1, hinted=True), 100)
        self.assertEqual(score.streak, 0)
        self.assertEqual(score.award(1), 200)
        self.assertEqual(score.hinted_correct, 1)
        self.assertEqual(score.correct, 3)
        self.assertEqual(score.best_streak, 1)

    def test_summary_reports_observed_response_times_and_attempts(self):
        score = SpeedScore()
        self.assertIsNone(score.best_time)
        self.assertIsNone(score.average_time)
        self.assertEqual(score.summary()["attempts"], 0)
        score.award(1.5)
        score.mistake()
        score.award(4.5, hinted=True)
        summary = score.summary()
        self.assertEqual(summary["best_time"], 1.5)
        self.assertEqual(summary["average_time"], 3)
        self.assertEqual(summary["correct"], 2)
        self.assertEqual(summary["wrong"], 1)
        self.assertEqual(summary["hinted_correct"], 1)

    def test_invalid_response_never_changes_results(self):
        score = SpeedScore()
        for elapsed in (-1, float("nan"), float("inf"), "not a duration"):
            with self.subTest(elapsed=elapsed), self.assertRaises(ValueError):
                score.award(elapsed)
        self.assertEqual(score.attempts, 0)
        self.assertEqual(score.points, 0)
        self.assertEqual(score.response_times, [])


class PersonalBestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "scores.json"

    def full_score(self, response=1, hinted=False):
        score = SpeedScore()
        for _ in range(34):
            score.award(response, hinted=hinted)
        return score.summary()

    def test_reading_new_history_does_not_create_files(self):
        entry = read_results("deck-a", self.path)
        self.assertEqual(entry, {"completed_runs": 0, "best": None, "last": None})
        self.assertFalse(self.path.exists())

    def test_default_location_uses_shared_plugin_state(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": self.directory.name}):
            result = save_result("deck-a", self.full_score(), 34, eligible=True, completed=True)
            self.assertEqual(read_results("deck-a"), result)
        self.assertTrue((Path(self.directory.name) / "herdr-shell" / "learning-speed.json").exists())

    def test_best_tracks_higher_scores_then_faster_tied_runs(self):
        summary = self.full_score(2)
        first = save_result("deck-a", summary, 70, eligible=True, completed=True, path=self.path)
        second = save_result("deck-a", summary, 68, eligible=True, completed=True, path=self.path)
        self.assertEqual(first["best"]["active_elapsed"], 70)
        self.assertEqual(second["best"]["active_elapsed"], 68)
        better = self.full_score(1)
        third = save_result("deck-a", better, 75, eligible=True, completed=True, path=self.path)
        self.assertEqual(third["best"]["points"], better["points"])
        self.assertEqual(third["best"]["active_elapsed"], 75)
        self.assertEqual(third["completed_runs"], 3)

    def test_slow_or_assisted_completed_run_preserves_best_and_updates_latest(self):
        original = save_result("deck-a", self.full_score(), 34, eligible=True, completed=True, path=self.path)
        slower = save_result("deck-a", self.full_score(5), 110, eligible=True, completed=True, path=self.path)
        assisted = save_result("deck-a", self.full_score(hinted=True), 30, completed=True, path=self.path)
        self.assertEqual(slower["best"], original["best"])
        self.assertEqual(assisted["best"], original["best"])
        self.assertEqual(assisted["last"]["hinted_correct"], 34)
        self.assertEqual(assisted["completed_runs"], 3)

    def test_partial_run_is_saved_without_completing_or_setting_best(self):
        score = SpeedScore()
        score.award(2)
        entry = save_result("deck-a", score.summary(), 120, path=self.path)
        self.assertEqual(entry["last"]["correct"], 1)
        self.assertEqual(entry["completed_runs"], 0)
        self.assertIsNone(entry["best"])

    def test_profile_results_do_not_compete_across_mapping_changes(self):
        save_result("deck-a", self.full_score(), 34, eligible=True, completed=True, path=self.path)
        second = save_result("deck-b", self.full_score(8), 120, eligible=True, completed=True, path=self.path)
        self.assertGreater(read_results("deck-a", self.path)["best"]["points"], second["best"]["points"])
        self.assertEqual(read_results("deck-a", self.path)["completed_runs"], 1)

    def test_ineligible_results_cannot_be_mislabeled_as_personal_bests(self):
        summary = self.full_score()
        with self.assertRaises(ValueError):
            save_result("deck-a", summary, 34, eligible=True, path=self.path)
        with self.assertRaises(ValueError):
            save_result("deck-a", self.full_score(hinted=True), 34, eligible=True, completed=True, path=self.path)
        partial = SpeedScore()
        partial.award(1)
        with self.assertRaises(ValueError):
            save_result("deck-a", partial.summary(), 1, eligible=True, completed=True, path=self.path)
        self.assertFalse(self.path.exists())

    def test_concurrent_completed_runs_do_not_lose_history(self):
        summary = self.full_score()
        def record(index):
            return save_result("deck-a", summary, 100 - index, eligible=True, completed=True, path=self.path)
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(record, range(20)))
        result = read_results("deck-a", self.path)
        self.assertEqual(result["completed_runs"], 20)
        self.assertEqual(result["best"]["active_elapsed"], 81)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_history_is_ignored_without_crashing_a_new_run(self):
        self.path.write_text("{unfinished")
        self.assertIsNone(read_results("deck-a", self.path)["best"])
        result = save_result("deck-a", self.full_score(), 34, eligible=True, completed=True, path=self.path)
        self.assertEqual(result["completed_runs"], 1)
        self.assertIsNotNone(result["best"])

    def test_newer_score_schema_is_preserved(self):
        original = json.dumps({"version": 2, "profiles": {}})
        self.path.write_text(original)
        with self.assertRaises(ValueError):
            save_result("deck-a", self.full_score(), 34, eligible=True, completed=True, path=self.path)
        self.assertEqual(self.path.read_text(), original)

    def test_saved_results_are_detached_from_caller_mutations(self):
        summary = self.full_score()
        result = save_result("deck-a", summary, 34, eligible=True, completed=True, path=self.path)
        summary["points"] = 0
        result["best"]["points"] = 0
        self.assertGreater(read_results("deck-a", self.path)["best"]["points"], 0)


if __name__ == "__main__":
    unittest.main()
