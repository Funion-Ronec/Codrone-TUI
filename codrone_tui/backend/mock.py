"""A fake drone: lets the TUIs (``--demo``) and the tests run without hardware or Unity.

It imitates the *capabilities* of either real backend (``flavor="edu"`` or ``"sim"``) and
runs on a compressed clock (``time_scale``) so a 3-minute mission does not take 3 minutes.
"""

from __future__ import annotations

import math
import random
import threading
import time

from .base import (
    BackendError,
    Cap,
    Direction,
    DroneBackend,
    NotSupported,
    Telemetry,
)

EDU_CAPS = frozenset(
    {Cap.TELEMETRY, Cap.POSITION, Cap.RANGE, Cap.VERTICAL, Cap.FLIP, Cap.SHAPES, Cap.LED,
     Cap.BUZZER, Cap.ESTOP, Cap.COLOR, Cap.TRIM}
)
SIM_CAPS = frozenset({Cap.RANGE, Cap.VERTICAL, Cap.LED, Cap.BUZZER, Cap.GRAB, Cap.TELEPORT})

WALL_X_CM = 450.0


class MockBackend(DroneBackend):
    kind = "mock"

    def __init__(self, flavor: str = "edu", time_scale: float = 0.02, speed_cm_s: float = 80.0) -> None:
        super().__init__()
        if flavor not in ("edu", "sim"):
            raise ValueError("flavor must be 'edu' or 'sim'")
        self.flavor = flavor
        self.title = "Mock CoDrone EDU" if flavor == "edu" else "Mock CoDrone Simulator"
        self.caps = EDU_CAPS if flavor == "edu" else SIM_CAPS
        self.time_scale = time_scale
        self.speed_cm_s = speed_cm_s
        self._connected = False
        self._battery = 100.0
        self._t0 = time.time()
        self._interrupt = threading.Event()
        self.log: list[tuple] = []
        self.fail_next: str | None = None
        self._rng = random.Random(7)
        self.led = (0, 0, 0, 0)
        self.holding = False

    def connect(self, port: str | None = None) -> str:
        if self.fail_next == "connect":
            self.fail_next = None
            raise BackendError("mock connection refused")
        self._connected = True
        self._t0 = time.time()
        self.log.append(("connect", port))
        return port or "mock://localhost"

    def disconnect(self) -> None:
        self._connected = False
        self.log.append(("disconnect",))

    @property
    def connected(self) -> bool:
        return self._connected

    def _check(self, name: str) -> None:
        if not self._connected:
            raise BackendError("not connected")
        if self.fail_next == name:
            self.fail_next = None
            raise BackendError(f"mock {name} failure")

    def _wait(self, seconds: float) -> None:
        """Compressed, interruptible sleep (emergency_stop interrupts it)."""
        if self._interrupt.wait(max(0.0, seconds * self.time_scale)):
            self._interrupt.clear()
            raise BackendError("interrupted by emergency stop")
        self._battery = max(0.0, self._battery - 0.02 * seconds)

    def takeoff(self) -> None:
        self._check("takeoff")
        self.log.append(("takeoff",))
        self._wait(3.0)
        self.flying = True
        self.pose.z = 100.0

    def land(self) -> None:
        self._check("land")
        self.log.append(("land",))
        self._wait(2.0)
        self.flying = False
        self.pose.z = 0.0

    def emergency_stop(self) -> None:
        self.log.append(("emergency_stop",))
        self._interrupt.set()
        self.flying = False
        self.pose.z = 0.0

    def hover(self, seconds: float = 1.0) -> None:
        self._check("hover")
        self.log.append(("hover", seconds))
        self._wait(seconds)

    def move(self, direction: Direction, distance_cm: float, speed: float | None = None) -> None:
        self._check("move")
        direction = Direction(direction)
        if direction in (Direction.UP, Direction.DOWN):
            self.require(Cap.VERTICAL, "vertical moves")
        if not self.flying:
            raise BackendError("drone is not flying")
        self.log.append(("move", direction.value, distance_cm, speed))
        self._wait(abs(distance_cm) / self.speed_cm_s)
        self.pose.apply_move(direction, distance_cm)

    def turn(self, degrees: float) -> None:
        self._check("turn")
        if not self.flying:
            raise BackendError("drone is not flying")
        self.log.append(("turn", degrees))
        self._wait(abs(degrees) / 90.0)
        self.pose.apply_turn(degrees)

    def go(self, roll: int, pitch: int, yaw: int, throttle: int, seconds: float) -> None:
        self._check("go")
        self.log.append(("go", roll, pitch, yaw, throttle, seconds))
        self._wait(seconds)
        s = self.speed_cm_s * seconds / 100.0
        self.pose.apply_move(Direction.FORWARD, pitch * s)
        self.pose.apply_move(Direction.RIGHT, roll * s)
        self.pose.apply_move(Direction.UP, throttle * s)
        self.pose.apply_turn(yaw * seconds * 0.9)

    def flip(self, direction: str = "back") -> None:
        self.require(Cap.FLIP, "flip")
        self._check("flip")
        self.log.append(("flip", direction))
        self._wait(1.5)

    def shape(self, name: str, **kwargs) -> None:
        self.require(Cap.SHAPES, f"shape '{name}'")
        self._check("shape")
        self.log.append(("shape", name, kwargs))
        self._wait(4.0)

    def set_led(self, r: int, g: int, b: int, brightness: int = 255) -> None:
        self.require(Cap.LED, "LED")
        self._check("led")
        self.led = (r, g, b, brightness)
        self.log.append(("led", r, g, b, brightness))

    def beep(self, hz: int = 440, ms: int = 200) -> None:
        self.require(Cap.BUZZER, "buzzer")
        self._check("beep")
        self.log.append(("beep", hz, ms))

    def grab(self) -> None:
        self.require(Cap.GRAB, "gripper")
        self._check("grab")
        self.holding = True
        self.log.append(("grab",))

    def release(self) -> None:
        self.require(Cap.GRAB, "gripper")
        self._check("release")
        self.holding = False
        self.log.append(("release",))

    def teleport(self, x: float, y: float, z: float) -> None:
        self.require(Cap.TELEPORT, "teleport")
        self._check("teleport")
        self.pose.x, self.pose.y, self.pose.z = x, y, z
        self.log.append(("teleport", x, y, z))

    def read_telemetry(self) -> Telemetry:
        if not self._connected:
            raise BackendError("not connected")
        p = self.pose
        jitter = lambda s: self._rng.uniform(-s, s)  # noqa: E731
        front = max(5.0, WALL_X_CM - p.x) * max(0.2, math.cos(math.radians(p.heading))) + jitter(1.5)
        t = Telemetry(
            flight_state="Flight" if self.flying else "Ready",
            front_range_cm=round(front, 1),
            bottom_range_cm=round(max(0.0, p.z) + jitter(0.8), 1),
            wall_detected=front < 50,
        )
        if self.flavor == "edu":
            t.battery = int(self._battery)
            t.movement_state = "Hovering" if self.flying else "Ready"
            t.pos_cm = (round(p.x, 1), round(p.y, 1), round(p.z, 1))
            t.angles_deg = (round(jitter(1.2), 1), round(jitter(1.2), 1), round(p.heading, 1))
            t.accel = (round(jitter(0.05), 2), round(jitter(0.05), 2), round(-1 + jitter(0.03), 2))
            t.gyro = (round(jitter(2), 1), round(jitter(2), 1), round(jitter(2), 1))
            t.flow_cm_s = (round(jitter(1.0), 1), round(jitter(1.0), 1))
            t.temperature_c = round(31.0 + 4 * math.sin((time.time() - self._t0) / 40), 1)
            t.pressure_pa = round(101325 - p.z * 12.0 + jitter(5), 0)
            t.flight_time_s = round(time.time() - self._t0, 1)
        return t

    def info(self) -> dict[str, str]:
        return {"backend": self.title, "link": "mock://localhost", "time scale": f"x{1 / self.time_scale:.0f}"}
