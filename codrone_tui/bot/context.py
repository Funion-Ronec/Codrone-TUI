# RoutineContext with eyes: forward moves stop for obstacles and goto() routes around them. Because through()
# and land_on() are built on goto() and forward(), they get the same protection for free.

from __future__ import annotations

import time
from typing import Any, Callable

from ..adc.field import FieldConfig
from ..adc.match import MatchController
from ..adc.routine import ExecutorLike, RoutineContext
from ..adc.rules import Scorecard
from ..backend.base import Cap, Direction, DroneBackend
from .navigator import NavError, Navigator, ScanPoint, describe_scan
from .sensing import Reading
from .settings import BotSettings

_OPPOSITE = {
    Direction.FORWARD: Direction.BACKWARD, Direction.BACKWARD: Direction.FORWARD,
    Direction.LEFT: Direction.RIGHT, Direction.RIGHT: Direction.LEFT,
    Direction.UP: Direction.DOWN, Direction.DOWN: Direction.UP,
}


class BotContext(RoutineContext):

    def __init__(
        self,
        backend: DroneBackend,
        executor: ExecutorLike,
        scorecard: Scorecard | None = None,
        field: FieldConfig | None = None,
        match: MatchController | None = None,
        log: Callable[[str], Any] = print,
        clock: Callable[[], float] = time.monotonic,
        *,
        settings: BotSettings | None = None,
        guard: bool = True,
        navigator: Navigator | None = None,
    ) -> None:
        super().__init__(backend, executor, scorecard=scorecard, field=field, match=match, log=log, clock=clock)
        self.settings = settings or BotSettings()
        self.guard = bool(guard) and backend.has(Cap.RANGE)  # nothing to guard with on a backend without one
        self.nav = navigator or Navigator(backend, executor, self.field, self.settings, log=self.log)
        self._in_gate = False

    def through(self, name: str, *args, **kwargs):
        was, self._in_gate = self._in_gate, True
        try:
            return super().through(name, *args, **kwargs)
        finally:
            self._in_gate = was

    def _move(self, direction: Direction, cm: float, speed: float | None) -> None:
        self.check()
        direction = Direction(direction)
        if cm < 0:
            direction, cm = _OPPOSITE[direction], -cm
        if cm == 0:
            return
        if direction is Direction.UP and self.backend.pose.z + cm > self.settings.max_alt_cm + 0.5:
            raise NavError(f"up {cm:g} cm would climb past the {self.settings.max_alt_cm:g} cm ceiling")
        if self.guard and direction is Direction.FORWARD:
            self.log(f"ahead {cm:g} cm")
            # Up close the beam is narrower than the drone: glance both ways first, then also stop for anything
            # the map knows is beside the path (a real flight hit a crate's corner the beam slid past).
            # (Not while crossing a gate: the gate itself is on the map, and through() has lined up on its opening.)
            heading = self.backend.pose.heading
            if self.settings.side_look_deg > 0 and not self._in_gate:
                self.nav.look_sides(heading)
            self.nav.forward(cm, speed, corridor=not self._in_gate)
            return
        super()._move(direction, cm, speed)

    def goto(self, x: float, y: float, z: float | None = None, speed: float | None = None) -> None:
        if not self.guard:
            super().goto(x, y, z, speed)
            return
        self.check()
        self.log(f"nav to ({x:.0f}, {y:.0f}" + (f", {z:.0f})" if z is not None else ")"))
        heading = self.backend.pose.heading
        self.nav.goto(x, y, z, speed)
        if self.nav.stats["replans"]:
            self.log(f"arrived after {self.nav.stats['replans']} re-route(s)")
        # RoutineContext.goto never turns (it strafes) and through() relies on that: face the way we started
        self.nav.face(heading)

    def approach(self, stop_cm: float, max_cm: float | None = None, speed: float | None = None) -> Reading:
        self.check()
        self.log(f"approach until {stop_cm:g} cm")
        if not self.backend.has(Cap.RANGE):
            raise NavError(f"approach needs a range sensor, which {self.backend.title} doesn't have")
        r = self.nav.approach(stop_cm, max_cm, speed)
        self.log(f"stopped with {r.text(self.settings)} ahead")
        return r

    def scan(self) -> list[ScanPoint]:
        self.check()
        self.log("scan")
        if not self.backend.has(Cap.RANGE):
            raise NavError(f"scan needs a range sensor, which {self.backend.title} doesn't have")
        points = self.nav.scan()
        for line in describe_scan(points, self.settings):
            self.log(f"  {line}")
        return points
