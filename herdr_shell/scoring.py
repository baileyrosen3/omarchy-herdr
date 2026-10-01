"""Timing and scoring for live shortcut practice.

The UI arms a response only after its target is ready and visible. It suspends
the timer while preparing targets, verifying actions, showing animations, or
waiting for the player to return to the guide. Receipt timestamps stop the
clock at the actual shortcut, rather than at the next UI poll.
"""
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from .config import atomic_write
from .runtime import state_path


SCORE_VERSION = 1
FULL_RUN_PRESSES = 34


def _seconds(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number.")
    return result


class ActiveTimer:
    """A response clock and run budget which advance only while armed.

    ``suspend`` preserves a response so focus changes, hints, and wrong-chord
    feedback can pause it. ``arm`` starts a fresh response while retaining the
    consumed run budget. Querying time never commits it: a queued event can
    still be measured at its earlier receipt timestamp.
    """

    def __init__(self, seconds=120):
        self.seconds = _seconds(seconds, "Run duration")
        if self.seconds <= 0:
            raise ValueError("Run duration must be positive.")
        self.armed = False
        self._started = None
        self._used = 0.0
        self._response = 0.0

    @staticmethod
    def _now(now):
        return _seconds(time.monotonic() if now is None else now, "Timestamp")

    def _segment(self, now):
        if self._started is None:
            return 0.0
        if now < self._started:
            raise ValueError("Timestamp precedes the armed response.")
        return now - self._started

    @property
    def running(self):
        return self._started is not None

    def arm(self, now=None):
        now = self._now(now)
        self.suspend(now)
        self.armed = True
        self._response = 0.0
        self._started = now

    def suspend(self, now=None):
        now = self._now(now)
        segment = self._segment(now)
        self._used += segment
        self._response += segment
        self._started = None

    def resume(self, now=None):
        now = self._now(now)
        if not self.armed:
            raise RuntimeError("Prepare a response before resuming the timer.")
        if self._started is None:
            self._started = now

    def disarm(self, now=None):
        self.suspend(now)
        self.armed = False

    def cancel_response(self, now=None, refund=False):
        """Discard the current response, optionally refunding its active time.

        A technical failure can invalidate the entire prepared response,
        including intervals before wrong-chord feedback. It must not erase
        time spent on earlier successful missions. Score penalties are kept
        separately by ``SpeedScore`` and are unaffected by a timing refund.
        """
        self.disarm(now)
        if refund:
            self._used = max(0.0, self._used - self._response)
        self._response = 0.0

    def response(self, received_at=None):
        """Freeze at a shortcut receipt and return its active response time.

        This leaves the response armed. Resume it after wrong-chord feedback,
        or arm a fresh response after a verified correct action.
        """
        if not self.armed:
            raise RuntimeError("Prepare a response before receiving a shortcut.")
        self.suspend(received_at)
        return self._response

    def elapsed(self, now=None):
        return self._used + self._segment(self._now(now))

    def remaining(self, now=None):
        return max(0.0, self.seconds - self.elapsed(now))

    def expired(self, now=None):
        return self.remaining(now) <= 0


class SpeedScore:
    """Score only verified shortcut results; typed letters are not attempts."""

    BASE_POINTS = 100
    WRONG_PENALTY = 25

    def __init__(self):
        self.points = 0
        self.correct = 0
        self.wrong = 0
        self.streak = 0
        self.best_streak = 0
        self.hinted_correct = 0
        self.response_times = []

    @staticmethod
    def speed_bonus(elapsed):
        elapsed = _seconds(elapsed, "Response time")
        if elapsed < 0:
            raise ValueError("Response time cannot be negative.")
        if elapsed <= 1:
            return 100
        if elapsed >= 8:
            return 0
        return int(100 * (8 - elapsed) / 7)

    def award(self, elapsed, hinted=False):
        """Add one verified correct press and return the points it earned."""
        elapsed = _seconds(elapsed, "Response time")
        bonus = self.speed_bonus(elapsed)
        self.correct += 1
        self.response_times.append(elapsed)
        if hinted:
            self.hinted_correct += 1
            self.streak = 0
            points = self.BASE_POINTS
        else:
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            points = self.BASE_POINTS + bonus + 10 * min(self.streak - 1, 10)
        self.points += points
        return points

    def mistake(self):
        """Count one eligible wrong chord and return its actual deduction."""
        self.wrong += 1
        self.streak = 0
        deduction = min(self.points, self.WRONG_PENALTY)
        self.points -= deduction
        return deduction

    @property
    def attempts(self):
        return self.correct + self.wrong

    @property
    def accuracy(self):
        return 100 * self.correct / self.attempts if self.attempts else 0.0

    @property
    def best_time(self):
        return min(self.response_times) if self.response_times else None

    @property
    def average_time(self):
        return sum(self.response_times) / self.correct if self.correct else None

    def summary(self):
        return {"points": self.points, "correct": self.correct, "wrong": self.wrong,
                "attempts": self.attempts, "accuracy": self.accuracy,
                "best_streak": self.best_streak, "hinted_correct": self.hinted_correct,
                "best_time": self.best_time, "average_time": self.average_time}


def _profile_key(profile):
    if not isinstance(profile, str) or not profile or len(profile) > 256:
        raise ValueError("A shortcut profile identifier is required.")
    return hashlib.sha256(f"{SCORE_VERSION}:{profile}".encode()).hexdigest()


def _empty_result():
    return {"completed_runs": 0, "best": None, "last": None}


def _read_data(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(256 * 1024 + 1)
        data = json.loads(raw) if len(raw) <= 256 * 1024 else None
    except (FileNotFoundError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"version": SCORE_VERSION, "profiles": {}}
    if type(data.get("version")) is int and data["version"] != SCORE_VERSION:
        raise ValueError("Saved learning scores use a different score version.")
    if data.get("version") != SCORE_VERSION or not isinstance(data.get("profiles"), dict):
        return {"version": SCORE_VERSION, "profiles": {}}
    return data


def _summary_record(summary, active_elapsed):
    if not isinstance(summary, dict):
        raise ValueError("A score summary is required.")
    result = {}
    for field in ("points", "correct", "wrong", "best_streak", "hinted_correct"):
        value = summary.get(field)
        if type(value) is not int or not 0 <= value <= 10**9:
            raise ValueError(f"Invalid {field} in the score summary.")
        result[field] = value
    if result["best_streak"] > result["correct"] or result["hinted_correct"] > result["correct"]:
        raise ValueError("The score summary contains impossible correct counts.")
    result["attempts"] = result["correct"] + result["wrong"]
    result["accuracy"] = 100 * result["correct"] / result["attempts"] if result["attempts"] else 0.0
    for field in ("best_time", "average_time"):
        value = summary.get(field)
        if value is None and result["correct"] == 0:
            result[field] = None
        else:
            value = _seconds(value, field)
            if value < 0 or not result["correct"]:
                raise ValueError(f"Invalid {field} in the score summary.")
            result[field] = value
    if result["correct"] and result["best_time"] > result["average_time"]:
        raise ValueError("Best response time exceeds the average.")
    result["active_elapsed"] = _seconds(active_elapsed, "Active run time")
    if result["active_elapsed"] < 0:
        raise ValueError("Active run time cannot be negative.")
    return result


def _stored_entry(value):
    """Discard corrupt metrics instead of displaying or comparing them."""
    if not isinstance(value, dict) or type(value.get("completed_runs")) is not int or value["completed_runs"] < 0:
        return _empty_result()
    result = {"completed_runs": value["completed_runs"], "best": None, "last": None}
    for field in ("best", "last"):
        record = value.get(field)
        if record is None:
            continue
        try:
            result[field] = _summary_record(record, record.get("active_elapsed"))
        except (AttributeError, ValueError):
            continue
    best = result["best"]
    if best is not None and (best["correct"] != FULL_RUN_PRESSES or best["hinted_correct"]):
        result["best"] = None
    return result


def read_results(profile, path=None):
    """Read personal results for this exact deck/profile without creating files."""
    key = _profile_key(profile)
    path = Path(path) if path is not None else state_path() / "learning-speed.json"
    return deepcopy(_stored_entry(_read_data(path)["profiles"].get(key)))


def save_result(profile, summary, active_elapsed, *, eligible=False, completed=False, path=None):
    """Store a run and return its local history and personal best.

    The controller supplies ``completed`` and eligibility after checking all
    missions, skips, hints, and technical failures. Even eligible results must
    contain the full 34 verified presses and zero hinted presses. Incomplete or
    assisted runs remain visible as the latest result without changing a best.
    Scores are compared first; equal scores favor less active response time.
    """
    key = _profile_key(profile)
    record = _summary_record(summary, active_elapsed)
    if eligible and (not completed or record["correct"] != FULL_RUN_PRESSES or record["hinted_correct"]):
        raise ValueError("Only a complete, unhinted 34-press run can set a personal best.")
    path = Path(path) if path is not None else state_path() / "learning-speed.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("Learning scores' lock is not a private file.")
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read_data(path)
        entry = _stored_entry(data["profiles"].get(key))
        entry["last"] = record
        if completed:
            entry["completed_runs"] += 1
        best = entry["best"]
        if eligible and (best is None or record["points"] > best["points"] or
                         (record["points"] == best["points"] and record["active_elapsed"] < best["active_elapsed"])):
            entry["best"] = record
        data["profiles"][key] = entry
        atomic_write(path, json.dumps(data, allow_nan=False) + "\n", mode=0o600)
    return deepcopy(entry)
