"""ADC 2026-27 "Mission 2027: Fast Track" rules as data, plus the Solo Autonomous scorecard.

Source: RECF ADC Game Manual v1.1 (Sept 29 2026), sections 3.1 (task definitions, landing
criteria) and 4 (Solo Autonomous Flight Mission). Re-check the manual and the official Q&A
before relying on any number here; rules can be revised mid-season.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

MATCH_SECONDS = 180
MAX_ATTEMPTS = 3

SPIRAL_KEY = "spiral_bonus"
SPIRAL_SEQUENCE = ("mini_1", "mini_2", "mini_3")
AUTO_SOURCE = "auto"


@dataclass(frozen=True)
class TaskDef:
    key: str
    label: str
    points: int
    max_count: int
    group: str


def _defs(*rows: tuple) -> dict[str, TaskDef]:
    return {r[0]: TaskDef(*r) for r in rows}


TASKS: dict[str, TaskDef] = _defs(
    ("takeoff", "Take off", 5, 1, "takeoff"),
    ("red_arch", "Fly under the red arch gate", 5, 2, "arch"),
    ("blue_arch", "Fly under the blue arch gate", 5, 2, "arch"),
    ("green_keyhole", "Fly through the green keyhole gate", 10, 2, "keyhole"),
    ("yellow_keyhole", "Fly through the yellow keyhole gate", 20, 2, "keyhole"),
    ("large_cube", "Fly through the large cube", 25, 2, "cube"),
    ("mini_1", "Fly through mini keyhole #1 (lowest)", 15, 2, "mini"),
    ("mini_2", "Fly through mini keyhole #2 (middle)", 15, 2, "mini"),
    ("mini_3", "Fly through mini keyhole #3 (top)", 15, 2, "mini"),
    (SPIRAL_KEY, "Spiral bonus (3 mini keyholes in order)", 10, 2, "bonus"),
    ("tunnel", "Fly through the tunnel", 15, 2, "tunnel"),
)

LANDINGS: dict[str, TaskDef] = _defs(
    ("landing_pad", "Land on a landing pad", 5, 1, "landing"),
    ("landing_cube", "Land on any cube", 10, 1, "landing"),
    ("landing_bullseye", "Land on the bullseye", 15, 1, "landing"),
)


def max_score() -> int:
    """Theoretical maximum for one Solo Autonomous match (290 under manual v1.1)."""
    return sum(t.points * t.max_count for t in TASKS.values()) + max(t.points for t in LANDINGS.values())


@dataclass(frozen=True)
class PilotingPhase:
    number: int
    points: int
    steps: tuple[str, ...]


PILOTING_TAKEOFF_POINTS = 5
PILOTING_PHASES: tuple[PilotingPhase, ...] = (
    PilotingPhase(1, 5, ("red_arch", "green_keyhole", "large_cube")),
    PilotingPhase(2, 10, ("mini_1", "mini_2", "mini_3")),
    PilotingPhase(3, 15, ("blue_arch", "yellow_loop", "tunnel", "blue_arch")),
    PilotingPhase(4, 10, ("mini_1", "mini_2", "mini_3")),
)


@dataclass(frozen=True)
class CreditEvent:
    t: float
    key: str
    points: int
    source: str


@dataclass(frozen=True)
class CreditResult:
    ok: bool
    points: int = 0
    message: str = ""
    auto: list[str] = field(default_factory=list)
    auto_points: int = 0

    @property
    def total_points(self) -> int:
        return self.points + self.auto_points


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    points: int
    count: int
    max_count: int
    earned: int
    group: str = "task"


class Scorecard:
    """Points for one Solo Autonomous attempt.

    Spiral bonus (4.2.2): crediting ``mini_3`` awards ``spiral_bonus`` automatically when the
    three most recently *credited* scoring tasks, ignoring spiral bonuses themselves, are exactly
    ``mini_1, mini_2, mini_3`` in that order. Any other credited task in between breaks the run,
    because "order is strictly enforced; any deviation or interruption disqualifies". Credits that
    were rejected (task already maxed out) never reached the card and so cannot break it. The bonus
    stops at 2 per match, and ``spiral_bonus`` cannot be credited by hand.
    """

    def __init__(self) -> None:
        self._events: list[CreditEvent] = []
        self._landing: str | None = None
        self._t0 = time.monotonic()

    @property
    def events(self) -> list[CreditEvent]:
        return list(self._events)

    @property
    def landing(self) -> str | None:
        return self._landing

    @property
    def landing_points(self) -> int:
        return LANDINGS[self._landing].points if self._landing else 0

    @property
    def total(self) -> int:
        return sum(e.points for e in self._events) + self.landing_points

    @property
    def counts(self) -> dict[str, int]:
        out = {key: 0 for key in TASKS}
        for e in self._events:
            out[e.key] += 1
        out.update({key: int(key == self._landing) for key in LANDINGS})
        return out

    def count(self, key: str) -> int:
        return sum(1 for e in self._events if e.key == key)

    def remaining(self, key: str) -> int:
        if key in LANDINGS:
            return 0 if self._landing else 1
        task = TASKS.get(key)
        return 0 if task is None else max(0, task.max_count - self.count(key))

    def rows(self) -> list[Row]:
        rows = [
            Row(t.key, t.label, t.points, self.count(t.key), t.max_count, self.count(t.key) * t.points, t.group)
            for t in TASKS.values()
        ]
        for t in LANDINGS.values():
            hit = int(self._landing == t.key)
            rows.append(Row(t.key, t.label, t.points, hit, t.max_count, hit * t.points, t.group))
        return rows

    def breakdown(self) -> dict[str, int]:
        """Points earned per task key (only keys that scored)."""
        out: dict[str, int] = {}
        for e in self._events:
            out[e.key] = out.get(e.key, 0) + e.points
        if self._landing:
            out[self._landing] = self.landing_points
        return out

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def credit(self, key: str, t: float | None = None, source: str = "manual") -> CreditResult:
        if key in LANDINGS:
            return self.set_landing(key)
        task = TASKS.get(key)
        if task is None:
            return CreditResult(False, 0, f"unknown task '{key}'")
        if key == SPIRAL_KEY:
            return CreditResult(False, 0, "spiral_bonus is awarded automatically after mini_1, mini_2, mini_3 in order")
        if self.count(key) >= task.max_count:
            return CreditResult(False, 0, f"{task.label}: already scored {task.max_count}x (max)")

        when = self._now() if t is None else float(t)
        self._events.append(CreditEvent(when, key, task.points, source))
        message = f"+{task.points} {task.label}"
        auto: list[str] = []
        auto_points = 0

        if key == SPIRAL_SEQUENCE[-1] and self._spiral_completed():
            bonus = TASKS[SPIRAL_KEY]
            self._events.append(CreditEvent(when, SPIRAL_KEY, bonus.points, AUTO_SOURCE))
            auto, auto_points = [SPIRAL_KEY], bonus.points
            message += f" (+{bonus.points} spiral bonus)"
        return CreditResult(True, task.points, message, auto, auto_points)

    def _spiral_completed(self) -> bool:
        recent = [e.key for e in self._events if e.key != SPIRAL_KEY][-len(SPIRAL_SEQUENCE):]
        return tuple(recent) == SPIRAL_SEQUENCE

    def undo(self) -> CreditResult | None:
        """Remove the latest credit; an auto-awarded spiral bonus goes together with its trigger."""
        if not self._events:
            return None
        removed = [self._events.pop()]
        if removed[0].source == AUTO_SOURCE and self._events:
            removed.append(self._events.pop())
        points = sum(e.points for e in removed)
        names = " and ".join(TASKS[e.key].label for e in reversed(removed))
        auto = [e.key for e in removed if e.source == AUTO_SOURCE]
        return CreditResult(True, -points, f"undid {names} (-{points})", auto, 0)

    def set_landing(self, key: str | None) -> CreditResult:
        """Record the landing (one slot: a new landing replaces the old). ``None`` clears it."""
        if key is None:
            had, self._landing = self._landing, None
            return CreditResult(True, -LANDINGS[had].points if had else 0, "landing cleared")
        task = LANDINGS.get(key)
        if task is None:
            return CreditResult(False, 0, f"unknown landing '{key}'")
        previous = self.landing_points
        self._landing = key
        return CreditResult(True, task.points - previous, f"+{task.points} {task.label}")

    def reset(self) -> None:
        self._events.clear()
        self._landing = None
        self._t0 = time.monotonic()

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [{"t": e.t, "key": e.key, "points": e.points, "source": e.source} for e in self._events],
            "landing": self._landing,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scorecard":
        """Rebuild by replaying the stored credits, so caps and the spiral rule are re-applied.

        Unknown or over-cap entries are dropped; auto-awarded bonuses are regenerated, not copied.
        """
        card = cls()
        for item in data.get("events", []):
            if not isinstance(item, dict) or item.get("key") == SPIRAL_KEY:
                continue
            card.credit(str(item.get("key")), t=float(item.get("t", 0.0)), source=str(item.get("source", "manual")))
        landing = data.get("landing")
        if landing in LANDINGS:
            card.set_landing(landing)
        return card
