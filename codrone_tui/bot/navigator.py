# Obstacle-aware flying with one forward range sensor.
#   forward()   guarded move: never further than (free space ahead - margin), re-measured every step
#   approach()  fly forward until something is a given distance away
# Both creep by default (settings.motion): steady, slow stick flight with a fresh reading every creep_tick_cm
# (5 cm), so a move is cancelled the moment something comes within the margin, instead of separate move
# commands that each fly blind and then wait for the drone to settle (about a second each on a real EDU).
#   scan()      turn in place and measure a fan of headings
#   goto()      A* route on the occupancy grid; when the sensor blocks a leg, scan, re-route and carry on
# The sensor only looks forward, so the navigator always turns to face where it is about to fly.

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from ..adc.field import FieldConfig
from ..backend.base import BackendError, Cap, Direction, DroneBackend, Pose
from .grid import OccupancyGrid
from .sensing import CLEAR, HIT, Reading, read_front
from .settings import BotSettings
from .world import element_boxes

SOFT_COST = 10.0  # route cost per cell flown closer than the margin to something; detours are usually cheaper
MIN_TURN_DEG = 2.0


class NavError(BackendError):
    pass


class Blocked(NavError):
    def __init__(self, message: str, reading: Reading, moved_cm: float) -> None:
        super().__init__(message)
        self.reading = reading
        self.moved_cm = moved_cm


class ExecutorLike(Protocol):
    def check(self) -> None: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class ScanPoint:
    heading: float  # absolute heading the reading was taken at
    offset: float  # relative to the scan's centre heading
    reading: Reading


def bearing(x0: float, y0: float, x1: float, y1: float) -> float:
    # heading (degrees clockwise from +x) pointing from (x0, y0) to (x1, y1)
    return math.degrees(math.atan2(-(y1 - y0), x1 - x0)) % 360.0


def turn_delta(current: float, target: float) -> float:
    return ((target - current + 180.0) % 360.0) - 180.0


class Navigator:
    def __init__(self, backend: DroneBackend, executor: ExecutorLike, field: FieldConfig | None = None,
                 settings: BotSettings | None = None, log: Callable[[str], object] | None = None) -> None:
        self.backend = backend
        self.executor = executor
        self.field = field
        self.s = settings or BotSettings()
        self._log = log or (lambda m: None)
        self.grid = OccupancyGrid(self.s.geofence_cm + 2 * self.s.margin_cm, self.s.cell_cm)
        self._static_key: int | None = None
        self.last_scan: list[ScanPoint] = []
        self.stats = {"legs": 0, "replans": 0, "scans": 0}

    @property
    def pose(self) -> Pose:
        return self.backend.pose

    def log(self, message: str) -> None:
        self._log(message)

    def check(self) -> None:
        self.executor.check()

    def require_range(self) -> None:
        if not self.backend.has(Cap.RANGE):
            raise NavError(f"{self.backend.title} has no range sensor")

    def inside_fence(self, x: float, y: float) -> bool:
        return math.hypot(x, y) <= self.s.geofence_cm + 1e-6

    def check_altitude(self, z: float) -> None:
        if not self.s.min_alt_cm - 0.5 <= z <= self.s.max_alt_cm + 0.5:
            raise NavError(f"altitude {z:.0f} cm is outside the allowed {self.s.min_alt_cm:.0f}-{self.s.max_alt_cm:.0f} cm")

    # sensing -----------------------------------------------------------------------------------------

    def measure(self, samples: int | None = None) -> Reading:
        self.require_range()
        r = read_front(self.backend, self.s, samples)
        p = self.pose
        self.grid.mark(p.x, p.y, p.heading, r.kind, r.cm or 0.0, self.s.sensor_max_cm, self.s.cone_deg)
        return r

    def free_ahead(self, r: Reading) -> float:
        # how far forward this reading says it is safe to fly
        if r.kind == HIT:
            return max(0.0, r.cm - self.s.margin_cm)
        if r.kind == CLEAR:
            return max(0.0, self.s.sensor_max_cm - self.s.margin_cm)
        return self.s.unknown_step_cm

    def face(self, heading: float) -> None:
        delta = turn_delta(self.pose.heading, heading)
        if abs(delta) >= MIN_TURN_DEG:
            self.check()
            self.backend.turn(delta)
            if self.s.settle_s > 0:
                self.executor.sleep(self.s.settle_s)

    # moves -------------------------------------------------------------------------------------------

    def forward(self, cm: float, speed: float | None = None, corridor: bool = False) -> float:
        # Fly forward up to `cm`, stopping before anything the sensor sees. Returns cm flown; raises Blocked.
        # corridor=True also stops for anything already on the map within the drone's width of the path.
        self.require_range()
        if self.creeping:
            moved, _ = self._creep(float(cm), corridor=corridor)
            return moved
        s = self.s
        remaining, moved = float(cm), 0.0
        # The beam narrows as the drone closes in, so something seen at its edge can drop out of view while it is
        # still in the way. Remember the closest hit of this leg and never fly past it, whatever comes after.
        limit = math.inf
        while remaining >= s.min_move_cm:
            self.check()
            r = self.measure()
            if r.kind == HIT:
                limit = min(limit, moved + r.cm - s.margin_cm)
            step = min(remaining, self.free_ahead(r), s.max_step_cm, limit - moved)
            if step < s.min_move_cm:
                raise Blocked(f"blocked by something {r.text(s)} ahead after {moved:.0f} of {cm:.0f} cm", r, moved)
            if corridor and not self.corridor_clear(self.pose.heading, step):
                raise Blocked(f"something on the map is in the way after {moved:.0f} of {cm:.0f} cm", r, moved)
            self._fly_forward(step, speed, moved)
            moved += step
            remaining -= step
        return moved

    def approach(self, stop_cm: float, max_cm: float | None = None, speed: float | None = None) -> Reading:
        # Fly forward until the front reading is about `stop_cm`, or `max_cm` has been flown. Returns the last reading.
        self.require_range()
        s = self.s
        stop = max(float(stop_cm), s.drone_radius_cm + 5.0)
        limit = s.geofence_cm if max_cm is None else float(max_cm)
        if self.creeping:
            moved, r = self._creep(limit, stop_cm=stop)
            back = min(stop - r.cm, moved) if r.hit else 0.0  # too close: back off, only along the way we came
            if back >= s.min_move_cm:
                self.backend.move(Direction.BACKWARD, back, speed)
            return r
        seen = math.inf  # as in forward(): never fly past the closest hit seen so far
        moved = 0.0
        while True:
            self.check()
            r = self.measure()
            if r.kind == HIT and r.cm <= stop + s.min_move_cm:
                back = min(stop - r.cm, moved)  # too close: back off, but only along the way we came
                if back >= s.min_move_cm:
                    self.backend.move(Direction.BACKWARD, back, speed)
                return r
            if r.kind == HIT:
                seen = min(seen, moved + r.cm - stop)
                step = r.cm - stop
            elif r.kind == CLEAR:
                step = s.sensor_max_cm - stop
            else:
                step = s.unknown_step_cm
            step = min(step, s.max_step_cm, limit - moved, seen - moved)
            if step < s.min_move_cm:
                return r
            self._fly_forward(step, speed, moved)
            moved += step

    # creeping ---------------------------------------------------------------------------------------

    @property
    def creeping(self) -> bool:
        return self.s.motion == "creep" and type(self.backend).stick is not DroneBackend.stick

    def _creep(self, cm: float, corridor: bool = False, stop_cm: float | None = None) -> tuple[float, Reading]:
        # Fly forward on the sticks, `creep_tick_cm` at a time, reading the sensor every tick.
        #   stop_cm None: fly `cm`, never closer than the margin to a hit; raises Blocked if it can't finish.
        #   stop_cm set:  approach: fly until a hit is about stop_cm away (or `cm` flown); returns the last reading.
        # The drone keeps flying while the sensor is read, so that time is counted as flown too.
        s, b = self.s, self.backend
        pitch = max(1, min(100, round(100.0 * s.creep_cm_s / max(1.0, b.stick_cm_s))))
        cm_s = pitch * b.stick_cm_s / 100.0
        tick = s.creep_tick_cm
        moved, limit = 0.0, math.inf  # limit: never past the closest hit seen on this creep (see forward())
        moving = False
        r = Reading(CLEAR)
        try:
            while True:
                self.check()
                t0 = time.monotonic()
                r = self.measure(samples=1)
                if moving and b.realtime:  # the sticks were still held forward while reading
                    extra = time.monotonic() - t0
                    b.stick(0, pitch, 0, 0, extra)
                    moved += extra * cm_s
                if stop_cm is None:
                    if r.hit:
                        limit = min(limit, moved + r.cm - s.margin_cm)
                    room = min(cm - moved, self.free_ahead(r), limit - moved)
                    if cm - moved < 1.0:
                        return moved, r
                    if room < 1.0:
                        raise Blocked(f"blocked by something {r.text(s)} ahead after {moved:.0f} of {cm:.0f} cm", r, moved)
                else:
                    if r.hit and r.cm <= stop_cm + 1.0:
                        return moved, r
                    if r.hit:
                        limit = min(limit, moved + r.cm - stop_cm)
                    room = min(cm - moved, limit - moved, math.inf if r.kind != HIT else r.cm - stop_cm)
                    if room < 1.0:
                        return moved, r
                step = min(tick, room)
                # look a margin ahead of this tick, but never past where the move ends
                if corridor and not self.corridor_clear(self.pose.heading, min(cm - moved, step + s.margin_cm)):
                    raise Blocked(f"something on the map is in the way after {moved:.0f} of {cm:.0f} cm", r, moved)
                end = self.pose.copy()
                end.apply_move(Direction.FORWARD, step)
                if not self.inside_fence(end.x, end.y):
                    raise NavError(f"stopped after {moved:.0f} cm: going on would leave the {s.geofence_cm:.0f} cm geofence")
                hold = step / cm_s
                b.stick(0, pitch, 0, 0, hold)
                moving = True
                self.executor.sleep(b.stick_time(hold))
                moved += step
        finally:
            if moving:
                try:
                    b.stick(0, 0, 0, 0, 0.0)  # sticks centred: the drone brakes and holds
                    if s.creep_brake_s > 0:
                        self.executor.sleep(b.stick_time(s.creep_brake_s))
                except Exception:  # noqa: BLE001 - an abort or crash is already on its way up
                    pass

    def look_sides(self, heading: float) -> None:
        # Up close the beam is narrower than the drone. Glancing to both sides covers the start of the next leg.
        for off in (-self.s.side_look_deg, self.s.side_look_deg):
            self.face((heading + off) % 360.0)
            self.measure()
        self.face(heading)

    def corridor_clear(self, heading: float, length: float) -> bool:
        # nothing on the map within the drone's width along the next `length` cm (the drone's own spot excepted)
        s = self.s
        hard, _ = self._zones()
        h = math.radians(heading)
        fx, fy = math.cos(h), -math.sin(h)
        x, y = self.pose.x, self.pose.y
        d = s.drone_radius_cm
        while d <= length + 1e-6:
            if self.grid.cell_of(x + fx * d, y + fy * d) in hard:
                return False
            d += self.grid.cell / 2.0
        return True

    def _zones(self):
        s = self.s
        return self.grid.zones(s.drone_radius_cm, s.margin_cm, s.drone_radius_cm, s.drone_radius_cm + s.prior_clearance_cm)

    def _fly_forward(self, step: float, speed: float | None, moved: float) -> None:
        end = self.pose.copy()
        end.apply_move(Direction.FORWARD, step)
        if not self.inside_fence(end.x, end.y):
            raise NavError(f"stopped after {moved:.0f} cm: going on would leave the {self.s.geofence_cm:.0f} cm geofence")
        self.backend.move(Direction.FORWARD, step, speed)

    def scan(self, center: float | None = None, angles: tuple[float, ...] | None = None) -> list[ScanPoint]:
        self.require_range()
        c = self.pose.heading if center is None else center
        points = []
        for off in sorted(angles if angles is not None else self.s.scan_angles):
            self.check()
            self.face((c + off) % 360.0)
            points.append(ScanPoint(self.pose.heading, off, self.measure()))
        self.last_scan = points
        self.stats["scans"] += 1
        return points

    def goto(self, x: float, y: float, z: float | None = None, speed: float | None = None) -> None:
        # Fly to (x, y) in the field frame, routing around what field.toml and the sensor know about.
        self.require_range()
        s = self.s
        if not self.inside_fence(x, y):
            raise NavError(f"({x:.0f}, {y:.0f}) is outside the {s.geofence_cm:.0f} cm geofence around the takeoff point")
        if z is not None:
            self.check_altitude(z)
            if z - self.pose.z >= s.min_move_cm:  # climb first: the way across is clearer higher up
                self._vertical(Direction.UP, z - self.pose.z, speed)
        self.stats = {"legs": 0, "replans": 0, "scans": 0}
        while True:
            self.check()
            p = self.pose
            if math.hypot(x - p.x, y - p.y) <= s.arrive_cm:
                break
            if self.stats["legs"] >= s.max_legs:
                raise NavError(f"gave up on ({x:.0f}, {y:.0f}) after {s.max_legs} legs")
            self._seed_static(p.z)
            route = self.grid.route((p.x, p.y), (x, y), hit_hard=s.drone_radius_cm, hit_soft=s.margin_cm,
                                    static_hard=s.drone_radius_cm, static_soft=s.drone_radius_cm + s.prior_clearance_cm,
                                    soft_cost=SOFT_COST)
            if route is None:
                raise NavError(f"no obstacle-free route from ({p.x:.0f}, {p.y:.0f}) to ({x:.0f}, {y:.0f})")
            wx, wy = next(((a, b) for a, b in route if math.hypot(a - p.x, b - p.y) >= s.min_move_cm), route[-1])
            leg = math.hypot(wx - p.x, wy - p.y)
            if leg < s.min_move_cm:
                break  # closer than dead reckoning can resolve
            heading = bearing(p.x, p.y, wx, wy)
            self.face(heading)
            if s.side_look_deg > 0:
                self.look_sides(heading)
                if not self.corridor_clear(heading, leg):
                    self._replanned(x, y, "something beside the way")
                    continue
            self.stats["legs"] += 1
            try:
                self.forward(leg, speed, corridor=True)
            except Blocked as exc:
                self._replanned(x, y, str(exc))
                self.scan()
        if z is not None and self.pose.z - z >= s.min_move_cm:
            self._vertical(Direction.DOWN, self.pose.z - z, speed)

    def vertical_blocker(self, z_to: float) -> str | None:
        # a field.toml element the drone would touch climbing or descending from here to z_to (nothing looks up or
        # down, so the map is all there is to go by)
        if not self.s.field_obstacles or self.field is None:
            return None
        p = self.pose
        lo, hi = sorted((p.z, z_to))
        for el in self.field.elements.values():
            for b in element_boxes(el):
                if b.z0 - 5.0 <= hi and lo <= b.z1 + 5.0 and b.distance(p.x, p.y) < self.s.drone_radius_cm:
                    return b.name
        return None

    def _replanned(self, x: float, y: float, why: str) -> None:
        self.stats["replans"] += 1
        if self.stats["replans"] > self.s.max_replans:
            raise NavError(f"gave up on ({x:.0f}, {y:.0f}) after {self.s.max_replans} re-routes: {why}")
        self.log(f"{why} -- re-routing")

    def _vertical(self, direction: Direction, cm: float, speed: float | None) -> None:
        if not self.backend.has(Cap.VERTICAL):
            self.log(f"altitude change of {cm:.0f} cm skipped: {self.backend.title} cannot move vertically by distance")
            return
        p = self.pose
        target = p.z + cm if direction is Direction.UP else p.z - cm
        blocker = self.vertical_blocker(target)
        if blocker is not None:
            word = "climb" if direction is Direction.UP else "come down"
            raise NavError(f"can't {word} to {target:.0f} cm at ({p.x:.0f}, {p.y:.0f}): {blocker} is in the way")
        self.check()
        self.backend.move(direction, cm, speed)

    def _seed_static(self, z: float) -> None:
        # field.toml elements at this altitude are known obstacles (thin posts are easy for the beam to miss)
        if not self.s.field_obstacles or self.field is None:
            return
        key = int(round(z / 10.0))
        if key == self._static_key:
            return
        self._static_key = key
        cells = []
        for el in self.field.elements.values():
            for b in element_boxes(el):
                if b.at_height(z, 10.0):
                    cells += self.grid.box_cells(b.x0, b.y0, b.x1, b.y1)
        self.grid.set_static(cells)


def describe_scan(points: list[ScanPoint], settings: BotSettings) -> list[str]:
    if not points:
        return []
    best = max(points, key=lambda p: (p.reading.kind == CLEAR, p.reading.cm or 0.0, -abs(p.offset)))
    lines = []
    for p in points:
        side = "ahead" if abs(p.offset) < 1 else (f"{abs(p.offset):.0f}° {'right' if p.offset > 0 else 'left'}")
        mark = "   <- most room" if p is best and best.reading.kind != "unknown" else ""
        lines.append(f"{side:>10} (heading {p.heading:5.1f}°)  {p.reading.text(settings)}{mark}")
    return lines
