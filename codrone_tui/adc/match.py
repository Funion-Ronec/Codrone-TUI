"""Solo Autonomous match state machine: 180 s clock, resets, three attempts, persistence."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .rules import MATCH_SECONDS, MAX_ATTEMPTS, CreditResult, Scorecard

DEFAULT_STORE = Path("logs") / "adc_attempts.json"


class MatchError(RuntimeError):
    """An illegal transition (starting a 4th attempt, finishing a match that is not running ...)."""


@dataclass(frozen=True)
class AttemptResult:
    number: int
    score: int
    duration: float
    resets: int
    breakdown: dict[str, int] = field(default_factory=dict)
    expired: bool = False
    landing: str | None = None


class MatchController:
    """idle -> running -> finished, with an injectable clock so tests never sleep.

    The clock keeps running through resets (manual 4.1.3). ``abort()`` throws the attempt away
    without using it up; only ``finish()`` / expiry records one.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        store_path: str | os.PathLike[str] | None = None,
        autosave: bool = False,
    ) -> None:
        self._clock = clock
        self.store_path = Path(store_path) if store_path is not None else DEFAULT_STORE
        self.autosave = autosave
        self.state = "idle"
        self.scorecard: Scorecard | None = None
        self.resets = 0
        self.piloting_best: int | None = None
        self.load_error: str | None = None
        self._attempts: list[AttemptResult] = []
        self._started_at = 0.0
        self._ended_after = 0.0

    @property
    def attempts(self) -> list[AttemptResult]:
        return list(self._attempts)

    @property
    def attempts_left(self) -> int:
        return max(0, MAX_ATTEMPTS - len(self._attempts))

    @property
    def best_score(self) -> int:
        return max((a.score for a in self._attempts), default=0)

    @property
    def skills_total(self) -> int:
        """Best autonomous score + best piloting score (7.4.1: highest combined sum wins skills)."""
        return self.best_score + (self.piloting_best or 0)

    @property
    def elapsed(self) -> float:
        if self.state == "running":
            return max(0.0, self._clock() - self._started_at)
        return self._ended_after if self.state == "finished" else 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, MATCH_SECONDS - self.elapsed)

    @property
    def expired(self) -> bool:
        return self.state == "running" and self.elapsed >= MATCH_SECONDS

    @property
    def last_result(self) -> AttemptResult | None:
        return self._attempts[-1] if self._attempts else None

    def start(self) -> Scorecard:
        if self.state == "running":
            raise MatchError("a match is already running")
        if self.attempts_left <= 0:
            raise MatchError(f"all {MAX_ATTEMPTS} attempts are used")
        self.scorecard = Scorecard()
        self.resets = 0
        self._started_at = self._clock()
        self.state = "running"
        return self.scorecard

    def credit(self, key: str, source: str = "manual") -> CreditResult:
        if self.state != "running" or self.scorecard is None:
            return CreditResult(False, 0, "no match is running")
        return self.scorecard.credit(key, t=self.elapsed, source=source)

    def reset_drone(self) -> int:
        """Count a drone reset (4.1.3). The timer is untouched. Returns the reset count."""
        if self.state != "running":
            raise MatchError("no match is running")
        self.resets += 1
        return self.resets

    def tick(self) -> AttemptResult | None:
        """Call regularly from the UI: ends the match when the clock hits zero."""
        if self.expired:
            return self._end(landing=None, expired=True)
        return None

    def finish(self, landing: str | None = None) -> AttemptResult:
        if self.state != "running":
            raise MatchError("no match is running")
        return self._end(landing=landing, expired=False)

    def abort(self) -> None:
        """Discard the running attempt; it does not count against the three."""
        if self.state == "running":
            self.state = "idle"
            self.scorecard = None
            self.resets = 0

    def new_event(self) -> None:
        """Forget all attempts (start of a new competition event)."""
        self._attempts.clear()
        self.state = "idle"
        self.scorecard = None
        self.resets = 0
        self.piloting_best = None

    def _end(self, landing: str | None, expired: bool) -> AttemptResult:
        assert self.scorecard is not None
        if landing is not None:
            self.scorecard.set_landing(landing)
        duration = min(self.elapsed, float(MATCH_SECONDS))
        result = AttemptResult(
            number=len(self._attempts) + 1,
            score=self.scorecard.total,
            duration=duration,
            resets=self.resets,
            breakdown=self.scorecard.breakdown(),
            expired=expired,
            landing=self.scorecard.landing,
        )
        self._attempts.append(result)
        self._ended_after = duration
        self.state = "finished"
        if self.autosave:
            self.save()
        return result

    def save(self) -> bool:
        payload = {
            "version": 1,
            "piloting_best": self.piloting_best,
            "attempts": [asdict(a) for a in self._attempts],
        }
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_name(self.store_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.store_path)
            return True
        except OSError as exc:
            self.load_error = f"could not save {self.store_path}: {exc}"
            return False

    def load(self) -> bool:
        """Read saved attempts. A missing or corrupt file leaves the current state untouched."""
        if not self.store_path.exists():
            return False
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            attempts = [_attempt_from_dict(a) for a in data["attempts"]][:MAX_ATTEMPTS]
            best = data.get("piloting_best")
            if best is not None:
                best = int(best)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.load_error = f"ignored unreadable {self.store_path}: {type(exc).__name__}: {exc}"
            return False
        self._attempts = attempts
        self.piloting_best = best
        self.load_error = None
        return True


def _attempt_from_dict(d: dict[str, Any]) -> AttemptResult:
    return AttemptResult(
        number=int(d["number"]),
        score=int(d["score"]),
        duration=float(d["duration"]),
        resets=int(d["resets"]),
        breakdown={str(k): int(v) for k, v in d.get("breakdown", {}).items()},
        expired=bool(d.get("expired", False)),
        landing=d.get("landing"),
    )
