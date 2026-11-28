# A tiny world of boxes for the demo drone, the tests and plan dry runs: ray-cast front range readings and
# crash checks. Boxes are axis-aligned in the field frame (cm, +x forward, +y left) and have a height range.

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ..adc.field import Element, FieldConfig
from ..backend.base import BackendError, Direction, Telemetry
from ..backend.mock import MockBackend

POST_CM = 8.0  # arch legs, keyhole stands
FRAME_CM = 15.0  # width of a keyhole's ring band
THIN_CM = 6.0  # depth of a gate frame
WALL_CM = 3.0  # tunnel walls
DRONE_RADIUS_CM = 9.0  # CoDrone EDU with prop guards, roughly


BOTTOM_CONE_DEG = 25.0  # assumed field of view of the bottom range sensor (VL53L0X-class; check on a real EDU)

@dataclass(frozen=True)
class Box:
    name: str
    x0: float
    y0: float
    x1: float
    y1: float
    z0: float = 0.0
    z1: float = 1000.0

    def at_height(self, z: float, pad: float = 0.0) -> bool:
        return self.z0 - pad <= z <= self.z1 + pad

    def distance(self, x: float, y: float) -> float:
        dx = max(self.x0 - x, 0.0, x - self.x1)
        dy = max(self.y0 - y, 0.0, y - self.y1)
        return math.hypot(dx, dy)

    def ray(self, ox: float, oy: float, dx: float, dy: float) -> float | None:
        # distance along a unit ray to the box (slab method), None if it misses
        tmin, tmax = -math.inf, math.inf
        for o, d, lo, hi in ((ox, dx, self.x0, self.x1), (oy, dy, self.y0, self.y1)):
            if abs(d) < 1e-12:
                if o < lo or o > hi:
                    return None
                continue
            t1, t2 = (lo - o) / d, (hi - o) / d
            if t1 > t2:
                t1, t2 = t2, t1
            tmin, tmax = max(tmin, t1), min(tmax, t2)
            if tmin > tmax:
                return None
        if tmax < 0:
            return None
        return max(tmin, 0.0)


def box(name: str, cx: float, cy: float, sx: float, sy: float, z0: float = 0.0, z1: float = 1000.0) -> Box:
    return Box(name, cx - sx / 2, cy - sy / 2, cx + sx / 2, cy + sy / 2, z0, z1)


class World:
    def __init__(self, boxes: list[Box] | tuple[Box, ...] = (), name: str = "") -> None:
        self.boxes = list(boxes)
        self.name = name

    def raycast(self, x: float, y: float, z: float, heading_deg: float, max_cm: float, cone_deg: float = 10.0,
                rays: int = 9, pad: float = 5.0) -> float | None:
        # nearest thing the beam sees (rays fanned across the cone), None if nothing within max_cm
        best: float | None = None
        for i in range(rays):
            a = 0.0 if rays == 1 else -cone_deg + 2 * cone_deg * i / (rays - 1)
            h = math.radians(heading_deg + a)
            dx, dy = math.cos(h), -math.sin(h)
            for b in self.boxes:
                if not b.at_height(z, pad):
                    continue
                t = b.ray(x, y, dx, dy)
                if t is not None and t <= max_cm and (best is None or t < best):
                    best = t
        return best

    def collision(self, x0: float, y0: float, x1: float, y1: float, z: float, radius: float
                  ) -> tuple[Box, float] | None:
        # first box the drone touches flying (x0, y0) -> (x1, y1), with the fraction of the way it got
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / 2.0))
        for i in range(n + 1):
            f = i / n
            px, py = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
            for b in self.boxes:
                if b.at_height(z, 5.0) and b.distance(px, py) < radius:
                    return b, f
        return None


def element_boxes(el: Element) -> list[Box]:
    # The solid parts of an ADC element, roughly. The manual gives opening sizes, not frame or stand sizes, so
    # those are guesses (docs/DEVELOPMENT.md, Bot). Pads lie flat and mini keyholes aren't measured: no boxes.
    h = math.radians(el.heading)
    fx, fy = math.cos(h), -math.sin(h)  # direction of travel through the element
    lx, ly = -fy, fx  # left of that

    def piece(tag: str, along: float, left: float, s_along: float, s_left: float, z0: float, z1: float) -> Box:
        cx, cy = el.x + fx * along + lx * left, el.y + fy * along + ly * left
        ex = abs(fx) * s_along / 2 + abs(lx) * s_left / 2
        ey = abs(fy) * s_along / 2 + abs(ly) * s_left / 2
        return Box(f"{el.name} {tag}", cx - ex, cy - ey, cx + ex, cy + ey, z0, z1)

    w = el.width
    if w <= 0:
        return []
    if el.kind == "arch":
        top = el.height if el.height > 0 else 160.0
        off = w / 2 + POST_CM / 2
        return [piece("left leg", 0, off, POST_CM, POST_CM, 0, top),
                piece("right leg", 0, -off, POST_CM, POST_CM, 0, top)]
    if el.kind == "keyhole":
        r = w / 2
        top = el.z + r + FRAME_CM
        off = r + FRAME_CM / 2
        parts = [piece("left frame", 0, off, THIN_CM, FRAME_CM, 0, top),
                 piece("right frame", 0, -off, THIN_CM, FRAME_CM, 0, top)]
        if el.z - r > 0:
            parts.append(piece("stand", 0, 0, POST_CM, POST_CM, 0, el.z - r))
        return parts
    if el.kind == "cube":
        top = el.height if el.height > 0 else w
        c = w / 2
        parts = [piece(f"post {n}", a * c, b * c, 4.0, 4.0, 0, top)
                 for n, (a, b) in enumerate(((1, 1), (1, -1), (-1, 1), (-1, -1)), 1)]
        parts.append(piece("top", 0, 0, w, w, top - 4.0, top))
        return parts
    if el.kind == "tunnel":
        # a short upright tube: z is its top opening and, like RoutineContext._through_vertical, its depth is taken
        # as `height`, so the drone comes out underneath (field.toml has the opening as width x height)
        d = el.height if el.height > 0 else w
        z0 = max(0.0, el.z - d)
        return [piece("front wall", d / 2, 0, WALL_CM, w, z0, el.z), piece("back wall", -d / 2, 0, WALL_CM, w, z0, el.z),
                piece("left wall", 0, w / 2, d, WALL_CM, z0, el.z), piece("right wall", 0, -w / 2, d, WALL_CM, z0, el.z)]
    return []


def field_world(field: FieldConfig | None) -> World:
    boxes = [b for el in (field.elements.values() if field is not None else ()) for b in element_boxes(el)]
    return World(boxes, name="field")


def demo_world(field: FieldConfig | None = None) -> World:
    # the field plus a few things off the scoring lanes, so built-in missions still fly clean in --demo
    w = field_world(field)
    w.boxes += [
        box("crate", 150, 100, 40, 40, 0, 120),
        box("post", 60, -110, 15, 15, 0, 200),
        box("far wall", 515, -100, 10, 440, 0, 300),
    ]
    w.name = "demo"
    return w


class WorldMock(MockBackend):
    # MockBackend whose range sensor sees `world` and which crashes (stops flying, raises) on contact.

    def __init__(self, flavor: str = "edu", world: World | None = None, time_scale: float = 0.02,
                 speed_cm_s: float = 80.0, *, sensor_max_cm: float = 150.0, far_reading: float | None = None,
                 noise_cm: float = 0.0, radius_cm: float = DRONE_RADIUS_CM, seed: int = 7) -> None:
        super().__init__(flavor, time_scale=time_scale, speed_cm_s=speed_cm_s)
        self.world = world if world is not None else World()
        self.sensor_max_cm = sensor_max_cm
        self.far_reading = far_reading  # what the sensor reports when nothing is in range
        self.noise_cm = noise_cm
        self.radius_cm = radius_cm
        self.crashed: str | None = None
        self._noise = random.Random(seed)

    def takeoff(self) -> None:
        super().takeoff()
        self.crashed = None

    def move(self, direction: Direction, distance_cm: float, speed: float | None = None) -> None:
        direction = Direction(direction)
        if self.connected and self.flying and direction not in (Direction.UP, Direction.DOWN) and distance_cm:
            end = self.pose.copy()
            end.apply_move(direction, distance_cm)
            hit = self.world.collision(self.pose.x, self.pose.y, end.x, end.y, self.pose.z, self.radius_cm)
            if hit is not None:
                b, f = hit
                super().move(direction, distance_cm * max(0.0, f - 0.02), speed)
                self.crashed = b.name
                self.flying = False
                self.pose.z = 0.0
                self.log.append(("crash", b.name))
                raise BackendError(f"CRASH: the drone hit {b.name} at ({self.pose.x:.0f}, {self.pose.y:.0f})")
        if self.connected and self.flying and direction in (Direction.UP, Direction.DOWN) and distance_cm:
            z0 = self.pose.z
            z1 = z0 + distance_cm if direction is Direction.UP else z0 - distance_cm
            lo, hi = sorted((z0, z1))
            for b in self.world.boxes:
                if b.distance(self.pose.x, self.pose.y) < self.radius_cm and b.z0 <= hi and lo <= b.z1 \
                        and not b.at_height(z0):
                    self.pose.z = b.z1 if direction is Direction.DOWN else b.z0
                    self.crashed = b.name
                    self.flying = False
                    self.log.append(("crash", b.name))
                    raise BackendError(f"CRASH: the drone {'came down on' if direction is Direction.DOWN else 'climbed into'}"
                                       f" {b.name} at ({self.pose.x:.0f}, {self.pose.y:.0f})")
        super().move(direction, distance_cm, speed)

    def stick(self, roll: int, pitch: int, yaw: int, throttle: int, hold_s: float) -> None:
        start = self.pose.copy()
        super().stick(roll, pitch, yaw, throttle, hold_s)
        if not (self.connected and self.flying):
            return
        end = self.pose
        hit = self.world.collision(start.x, start.y, end.x, end.y, start.z, self.radius_cm)
        if hit is not None:
            b, f = hit
            f = max(0.0, f - 0.02)
            end.x, end.y = start.x + (end.x - start.x) * f, start.y + (end.y - start.y) * f
            self.crashed = b.name
            self.flying = False
            self.pose.z = 0.0
            self.log.append(("crash", b.name))
            raise BackendError(f"CRASH: the drone hit {b.name} at ({self.pose.x:.0f}, {self.pose.y:.0f})")

    def read_telemetry(self) -> Telemetry:
        t = super().read_telemetry()
        p = self.pose
        d = self.world.raycast(p.x, p.y, p.z, p.heading, self.sensor_max_cm)
        if d is None:
            front = self.far_reading
        else:
            front = round(max(0.0, d + self._noise.uniform(-self.noise_cm, self.noise_cm)), 1)
        t.front_range_cm = front
        t.wall_detected = None if front is None else front < 50
        # The bottom sensor is a time-of-flight sensor with a cone (about 25 degrees across, BOTTOM_CONE_DEG), and
        # reports the nearest thing in it: the top of a box anywhere in its footprint, not only straight below.
        reach = max(3.0, p.z * math.tan(math.radians(BOTTOM_CONE_DEG / 2)))
        under = [b.z1 for b in self.world.boxes if b.z1 <= p.z + 1.0 and b.distance(p.x, p.y) < reach]
        if under and t.bottom_range_cm is not None:
            t.bottom_range_cm = round(max(0.0, p.z - max(under)), 1)
        return t

    def info(self) -> dict[str, str]:
        return {**super().info(), "world": f"{self.world.name or 'custom'} ({len(self.world.boxes)} boxes)"}
