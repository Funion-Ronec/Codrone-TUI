"""Autonomous routines: the API a mission script flies with, the loader, and the dry-run.

A routine is a function ``fn(ctx)`` decorated with ``@routine(name, description)``. It runs on the
flight-executor thread, calls ``ctx`` methods (each one first checks for an abort request), and
claims points with ``ctx.credit`` / ``ctx.through``. Those credits are what the *routine believes*
it scored (source="routine"); nothing here can see the drone pass a gate, so a human judge should
confirm or undo them on the scorecard.

``load_routines`` imports user ``*.py`` files, i.e. it executes code from the routines folder.
"""

from __future__ import annotations

import importlib.util
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..backend.base import BackendError, Cap, Direction, DroneBackend
from ..backend.executor import Aborted
from ..backend.mock import MockBackend
from .field import PROJECT_ROOT, Element, FieldConfig, default_field
from .match import MatchController
from .rules import MATCH_SECONDS, TASKS, CreditResult, Scorecard

DEFAULT_ROUTINES_DIR = PROJECT_ROOT / "routines"

_OPPOSITE = {
    Direction.FORWARD: Direction.BACKWARD,
    Direction.BACKWARD: Direction.FORWARD,
    Direction.LEFT: Direction.RIGHT,
    Direction.RIGHT: Direction.LEFT,
    Direction.UP: Direction.DOWN,
    Direction.DOWN: Direction.UP,
}

# land_on(target) -> (field element to fly over, scorecard landing key)
_LANDING_TARGETS = {
    "pad": ("landing_pad", "landing_pad"),
    "bullseye": ("bullseye", "landing_bullseye"),
    "cube": ("large_cube", "landing_cube"),
}


class RoutineError(RuntimeError):
    """A routine asked for something impossible (unknown field element, bad argument ...)."""


class ExecutorLike(Protocol):
    def check(self) -> None: ...
    def sleep(self, seconds: float) -> None: ...


class RoutineContext:
    """What a routine flies with. All distances in cm, angles in degrees, see ``backend.base``."""

    APPROACH_CM = 60.0  # how far before an element ``through`` lines up
    PAST_CM = 35.0  # how far beyond a thin gate the pass ends: enough for the whole drone to be clear of it
    CUBE_MARGIN_CM = 30.0  # beyond the far wall of a cube (which is as deep as it is wide)
    MIN_RUNUP_CM = 25.0  # closer than this to an element's far side we back up to get a run-up
    CLEARANCE_CM = 40.0  # how far above a vertical element (the tunnel) the drone lines up
    MIN_MOVE_CM = 5.0  # smaller corrections are skipped; dead reckoning is not that precise
    MIN_TURN_DEG = 2.0

    def __init__(
        self,
        backend: DroneBackend,
        executor: ExecutorLike,
        scorecard: Scorecard | None = None,
        field: FieldConfig | None = None,
        match: MatchController | None = None,
        log: Callable[[str], Any] = print,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self.executor = executor
        self.field = field if field is not None else default_field()
        self.match = match
        self.rejected: list[str] = []  # credits the scorecard refused (task already maxed out)
        self._explicit_card = scorecard
        self._private_card: Scorecard | None = None
        self._log = log
        self._clock = clock
        self._t0 = clock()

    # ------------------------------------------------------------------- state
    @property
    def scorecard(self) -> Scorecard:
        if self._explicit_card is not None:
            return self._explicit_card
        if self.match is not None and self.match.scorecard is not None:
            return self.match.scorecard
        if self._private_card is None:
            self._private_card = Scorecard()
        return self._private_card

    def elapsed(self) -> float:
        if self.match is not None and self.match.state == "running":
            return self.match.elapsed
        return self._clock() - self._t0

    def time_left(self) -> float:
        return max(0.0, MATCH_SECONDS - self.elapsed())

    def log(self, message: str) -> None:
        self._log(message)

    def check(self) -> None:
        """Raises ``Aborted`` if the operator asked to stop. Every action calls this first."""
        self.executor.check()

    # ----------------------------------------------------------------- actions
    def takeoff(self, credit: bool = True) -> None:
        self.check()
        self.log("takeoff")
        self.backend.takeoff()
        if credit:
            self.credit("takeoff")

    def land(self, landing: str | None = None) -> None:
        """Land here. ``landing`` is a landing key (``landing_pad`` ...) to score; None scores nothing."""
        self.check()
        self.log("land")
        self.backend.land()
        if landing is not None:
            res = self.scorecard.set_landing(landing)
            self.log(res.message)

    def land_on(self, target: str) -> None:
        """Fly over ``"pad"``, ``"bullseye"`` or ``"cube"`` and land, scoring that landing."""
        if target not in _LANDING_TARGETS:
            raise RoutineError(f"land_on target must be one of {sorted(_LANDING_TARGETS)}, not {target!r}")
        name, key = _LANDING_TARGETS[target]
        el = self._element(name)
        self.log(f"land on {target}")
        hover_z = el.height + 30.0 if target == "cube" and el.height > 0 else None
        self.goto(el.x, el.y, hover_z)
        self.land(landing=key)

    def forward(self, cm: float, speed: float | None = None) -> None:
        self._move(Direction.FORWARD, cm, speed)

    def backward(self, cm: float, speed: float | None = None) -> None:
        self._move(Direction.BACKWARD, cm, speed)

    def left(self, cm: float, speed: float | None = None) -> None:
        self._move(Direction.LEFT, cm, speed)

    def right(self, cm: float, speed: float | None = None) -> None:
        self._move(Direction.RIGHT, cm, speed)

    def up(self, cm: float, speed: float | None = None) -> None:
        self._move(Direction.UP, cm, speed)

    def down(self, cm: float, speed: float | None = None) -> None:
        self._move(Direction.DOWN, cm, speed)

    def turn(self, degrees: float) -> None:
        """Positive = clockwise (right)."""
        self.check()
        if degrees == 0:
            return
        self.log(f"turn {degrees:g} deg")
        self.backend.turn(degrees)

    def hover(self, seconds: float = 1.0) -> None:
        self.check()
        self.log(f"hover {seconds:g} s")
        self.backend.hover(seconds)

    def wait(self, seconds: float) -> None:
        """Pause without commanding the drone; wakes immediately on abort."""
        self.check()
        self.executor.sleep(seconds)

    def face(self, heading: float) -> None:
        """Turn the short way to an absolute heading (clockwise from the start direction)."""
        delta = ((heading - self.backend.pose.heading + 180.0) % 360.0) - 180.0
        if abs(delta) >= self.MIN_TURN_DEG:
            self.turn(delta)

    def goto(self, x: float, y: float, z: float | None = None, speed: float | None = None) -> None:
        """Fly to (x, y[, z]) in the field frame without changing heading.

        Uses the dead-reckoned ``backend.pose``, which assumes the drone started on the first foot
        of the mat facing +x and that every earlier move completed.
        """
        self.check()
        pose = self.backend.pose
        dx, dy = x - pose.x, y - pose.y
        h = math.radians(pose.heading)
        fx, fy = math.cos(h), -math.sin(h)
        forward = dx * fx + dy * fy
        left = dx * -fy + dy * fx
        dz = 0.0 if z is None else z - pose.z

        def vertical() -> None:
            if abs(dz) < self.MIN_MOVE_CM:
                return
            if self.backend.has(Cap.VERTICAL):
                self.up(dz, speed)
            else:
                self.log(f"altitude change of {dz:+.0f} cm skipped: {self.backend.title} cannot move vertically by distance")

        if dz > 0:  # climb first so the drone is already high enough when it crosses the field
            vertical()
        if abs(forward) >= self.MIN_MOVE_CM:
            self.forward(forward, speed)
        if abs(left) >= self.MIN_MOVE_CM:
            self.left(left, speed)
        if dz < 0:  # descend last
            vertical()

    def through(
        self,
        name: str,
        speed: float | None = None,
        approach_cm: float | None = None,
        pass_cm: float | None = None,
        credit: bool = True,
    ) -> CreditResult | None:
        """Line up ``approach_cm`` before a field element along its heading, fly through it, then
        claim its task (if it has one). Returns the credit result, or None.

        Vertical elements (the tunnel) are entered from above and flown DOWN through, as in manual
        figure 3.1.5. If the drone is already within the run-up of the element it does not back up
        first (gates are only ~80 cm apart on the second leg).
        """
        el = self._element(name)
        if el.vertical:
            return self._through_vertical(el, speed, credit)
        # the drone only has to clear the element's depth: ~35 cm for a thin gate, half the width plus a
        # margin for a cube (overshooting by more just means backing up for the next element)
        past = el.width / 2 + self.CUBE_MARGIN_CM if el.kind == "cube" and el.width else self.PAST_CM
        approach = self.APPROACH_CM if approach_cm is None else approach_cm
        hd = math.radians(el.heading)
        dx, dy = math.cos(hd), -math.sin(hd)  # unit vector of the direction of travel
        self.log(f"through {name}")
        self.face(el.heading)
        pose = self.backend.pose
        ahead = (el.x - pose.x) * dx + (el.y - pose.y) * dy  # >0: the element is in front of us
        if ahead >= self.MIN_RUNUP_CM:
            approach = min(approach, ahead)
        self.goto(el.x - approach * dx, el.y - approach * dy, el.z, speed)
        self.forward(pass_cm if pass_cm is not None else approach + past, speed)
        if credit and name in TASKS:
            return self.credit(name)
        return None

    def _through_vertical(self, el: Element, speed: float | None, credit: bool) -> CreditResult | None:
        if not self.backend.has(Cap.VERTICAL):
            self.log(f"{el.name} must be flown down through, but {self.backend.title} cannot move vertically by distance: skipped")
            return None
        self.log(f"down through {el.name}")
        self.goto(el.x, el.y, el.z + self.CLEARANCE_CM, speed)  # climbs first, then slides over the top
        self.down(self.CLEARANCE_CM + el.height + 15.0, speed)  # past the bottom opening
        if credit and el.name in TASKS:
            return self.credit(el.name)
        return None

    def credit(self, key: str) -> CreditResult:
        self.check()
        res = self.scorecard.credit(key, t=self.elapsed(), source="routine")
        if not res.ok:
            self.rejected.append(key)
        self.log(res.message)
        return res

    # ---------------------------------------------------------------- internals
    def _move(self, direction: Direction, cm: float, speed: float | None) -> None:
        self.check()
        if cm < 0:
            direction, cm = _OPPOSITE[direction], -cm
        if cm == 0:
            return
        self.log(f"{direction.value} {cm:g} cm")
        self.backend.move(direction, cm, speed)

    def _element(self, name: str) -> Element:
        try:
            return self.field.get(name)
        except KeyError as exc:
            raise RoutineError(str(exc.args[0])) from None


# ----------------------------------------------------------------- routine objects
@dataclass
class Routine:
    name: str
    description: str
    fn: Callable[[RoutineContext], Any]
    tags: tuple[str, ...] = ()
    source: str = ""  # file it was loaded from, "" for built-ins
    error: str | None = None  # set on placeholder entries for files that failed to load

    @property
    def ok(self) -> bool:
        return self.error is None

    def __call__(self, ctx: RoutineContext) -> Any:
        return self.fn(ctx)


def routine(name: str, description: str = "", tags: tuple[str, ...] = ()) -> Callable[[Callable], Routine]:
    """Decorator: ``@routine("my run", "what it does")`` turns ``fn(ctx)`` into a ``Routine``."""

    def wrap(fn: Callable[[RoutineContext], Any]) -> Routine:
        doc = (fn.__doc__ or "").strip().splitlines()
        return Routine(name=name, description=description or (doc[0] if doc else ""), fn=fn, tags=tuple(tags))

    return wrap


def builtin_routines() -> list[Routine]:
    from .builtin import BUILTIN

    return list(BUILTIN)


def load_routines(directory: str | Path | None = None) -> list[Routine]:
    """Import every ``*.py`` (not starting with ``_``) in ``directory`` and collect its Routines.

    A file that fails to import yields an entry with ``error`` set instead of raising, so one bad
    script cannot hide the others. Duplicate names keep the first and flag the later file.
    """
    folder = Path(directory) if directory is not None else DEFAULT_ROUTINES_DIR
    if not folder.is_dir():
        return []
    found: list[Routine] = []
    seen: set[str] = set()
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            routines = _import_routines(path)
        except (Exception, SystemExit) as exc:
            found.append(_error_entry(path, f"{type(exc).__name__}: {exc}"))
            continue
        for r in routines:
            if r.name in seen:
                found.append(_error_entry(path, f"duplicate routine name '{r.name}'", name=f"{path.stem}:{r.name}"))
                continue
            seen.add(r.name)
            r.source = str(path)
            found.append(r)
    return found


def _import_routines(path: Path) -> list[Routine]:
    module_name = f"_codrone_routine_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses & pickling need the module to be registered
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return [v for v in vars(module).values() if isinstance(v, Routine)]


def _error_entry(path: Path, message: str, name: str | None = None) -> Routine:
    def fn(ctx: RoutineContext) -> None:
        raise RoutineError(f"{path.name} did not load: {message}")

    return Routine(name or f"{path.stem} (load error)", message, fn, ("error",), str(path), message)


# ------------------------------------------------------------------------ dry run
DRY_TAKEOFF_S = 5.0  # a real EDU takeoff blocks ~8 s; this is an optimistic planning figure
DRY_LAND_S = 3.0
DRY_TURN_S_PER_90 = 1.5
DRY_SPEED_CM_S = 50.0
DRY_LIMIT_S = 600.0  # virtual seconds before a dry run gives up on a routine that never ends
DRY_MAX_ACTIONS = 5000


@dataclass
class DryRunReport:
    points: int
    duration_s: float
    steps: list[str]
    warnings: list[str]
    routine: str = ""
    breakdown: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class _VirtualBackend(MockBackend):
    """MockBackend on a virtual clock: every wait advances ``vt`` instead of sleeping."""

    def __init__(self, flavor: str, speed_cm_s: float) -> None:
        super().__init__(flavor, time_scale=0.0, speed_cm_s=speed_cm_s)
        self.vt = 0.0
        self.connect()

    def _wait(self, seconds: float) -> None:
        self.vt += seconds

    def takeoff(self) -> None:
        super().takeoff()
        self.vt += DRY_TAKEOFF_S - 3.0

    def land(self) -> None:
        super().land()
        self.vt += DRY_LAND_S - 2.0

    def turn(self, degrees: float) -> None:
        super().turn(degrees)
        self.vt += abs(degrees) / 90.0 * (DRY_TURN_S_PER_90 - 1.0)


class _VirtualExecutor:
    def __init__(self, backend: _VirtualBackend) -> None:
        self._backend = backend
        self._calls = 0

    def check(self) -> None:
        # every ctx action calls this, so it also bounds loops that never advance virtual time
        self._calls += 1
        if self._backend.vt > DRY_LIMIT_S or self._calls > DRY_MAX_ACTIONS:
            raise Aborted()

    def sleep(self, seconds: float) -> None:
        self._backend.vt += max(0.0, seconds)
        self.check()


def dry_run(
    routine: Routine,
    field: FieldConfig | None = None,
    *,
    flavor: str = "edu",
    speed_cm_s: float = DRY_SPEED_CM_S,
) -> DryRunReport:
    """Run a routine against a virtual drone to predict its score and duration. No hardware.

    The numbers are planning estimates: the real drone's speed, takeoff time and gate geometry
    will differ, and credits are what the routine *claims* (it cannot miss a gate here).
    """
    cfg = field if field is not None else default_field()
    backend = _VirtualBackend(flavor, speed_cm_s)
    card = Scorecard()
    steps: list[str] = []
    ctx = RoutineContext(
        backend, _VirtualExecutor(backend), scorecard=card, field=cfg, log=steps.append, clock=lambda: backend.vt
    )
    warnings = list(cfg.warnings())
    error: str | None = None
    try:
        routine.fn(ctx)
    except Aborted:
        warnings.append(f"dry run stopped: routine still going after {backend.vt:.0f} s simulated / {DRY_MAX_ACTIONS} actions (endless loop?)")
    except (RoutineError, BackendError) as exc:
        error = str(exc)
    except (Exception, SystemExit) as exc:
        error = f"{type(exc).__name__}: {exc}"

    if backend.vt > MATCH_SECONDS:
        warnings.append(f"plan takes {backend.vt:.0f} s but the match ends at {MATCH_SECONDS} s; later points will not count")
    if backend.flying:
        warnings.append("routine ends with the drone still in the air; it would not score a landing")
    elif card.landing is None and error is None:
        warnings.append("no landing points: the routine never scores a landing")
    if ctx.rejected:
        warnings.append("credits that scored nothing (already at max): " + ", ".join(ctx.rejected))
    return DryRunReport(
        points=card.total,
        duration_s=round(backend.vt, 1),
        steps=steps,
        warnings=warnings,
        routine=routine.name,
        breakdown=card.breakdown(),
        error=error,
    )
