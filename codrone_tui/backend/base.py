"""Backend contract shared by the CoDrone EDU TUI, the Simulator TUI and the ADC module.

Conventions (used everywhere in the project):
    * distances are centimetres, angles are degrees, durations are seconds
    * pose frame: +x forward, +y left, +z up, origin = where the drone took off
    * heading is clockwise-positive (so a right turn *adds* to the heading)
    * every backend method is BLOCKING and is meant to be called from a worker thread,
      never from the UI thread
"""

from __future__ import annotations

import abc
import enum
import math
import time
from dataclasses import dataclass, field


class BackendError(RuntimeError):
    """Anything that goes wrong talking to a drone (connection, library, timeout)."""


class NotSupported(BackendError):
    """The command exists in the TUI but this drone/simulator cannot do it."""


class Cap(str, enum.Enum):
    """Things a backend may or may not be able to do; the UI hides what is missing."""

    TELEMETRY = "telemetry"
    POSITION = "position"
    RANGE = "range"
    VERTICAL = "vertical"
    FLIP = "flip"
    SHAPES = "shapes"
    LED = "led"
    BUZZER = "buzzer"
    GRAB = "grab"
    TELEPORT = "teleport"
    ESTOP = "estop"
    COLOR = "color"
    TRIM = "trim"


class Direction(str, enum.Enum):
    FORWARD = "forward"
    BACKWARD = "backward"
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"


@dataclass
class Telemetry:
    """One snapshot. ``None`` means 'this backend cannot report it', never 'zero'."""

    timestamp: float = field(default_factory=time.time)
    battery: int | None = None
    flight_state: str | None = None
    movement_state: str | None = None
    front_range_cm: float | None = None
    bottom_range_cm: float | None = None
    wall_detected: bool | None = None
    pos_cm: tuple[float, float, float] | None = None
    angles_deg: tuple[float, float, float] | None = None
    accel: tuple[float, float, float] | None = None
    gyro: tuple[float, float, float] | None = None
    flow_cm_s: tuple[float, float] | None = None
    temperature_c: float | None = None
    pressure_pa: float | None = None
    flight_time_s: float | None = None
    pos_is_estimate: bool = False


@dataclass
class Pose:
    """Dead-reckoned pose, advanced from the commands we send (see ``Pose.apply_*``)."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    heading: float = 0.0  # degrees, clockwise is positive

    def reset(self) -> None:
        self.x = self.y = self.z = self.heading = 0.0

    def copy(self) -> "Pose":
        return Pose(self.x, self.y, self.z, self.heading)

    def apply_move(self, direction: Direction, distance_cm: float) -> None:
        h = math.radians(self.heading)
        fx, fy = math.cos(h), -math.sin(h)
        lx, ly = -fy, fx
        d = float(distance_cm)
        if direction is Direction.FORWARD:
            self.x, self.y = self.x + fx * d, self.y + fy * d
        elif direction is Direction.BACKWARD:
            self.x, self.y = self.x - fx * d, self.y - fy * d
        elif direction is Direction.LEFT:
            self.x, self.y = self.x + lx * d, self.y + ly * d
        elif direction is Direction.RIGHT:
            self.x, self.y = self.x - lx * d, self.y - ly * d
        elif direction is Direction.UP:
            self.z += d
        elif direction is Direction.DOWN:
            self.z = max(0.0, self.z - d)

    def apply_turn(self, degrees_clockwise: float) -> None:
        self.heading = (self.heading + degrees_clockwise) % 360.0

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


class DroneBackend(abc.ABC):
    """Everything the TUI / ADC routines may ask of a drone.

    Subclasses must keep ``self.pose`` in step with the moves they perform and must make
    ``read_telemetry`` fast (well under a second) and safe to call while another thread
    is mid-move.
    """

    kind: str = "base"
    title: str = "Drone"
    caps: frozenset[Cap] = frozenset()
    stick_hz: float = 20.0

    def __init__(self) -> None:
        self.pose = Pose()
        self.flying = False

    @abc.abstractmethod
    def connect(self, port: str | None = None) -> str:
        """Open the link. Returns a short human description ("COM5", "127.0.0.1:5000" ...).

        Must raise ``BackendError`` (never ``SystemExit``) if the drone cannot be reached."""

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    def has(self, cap: Cap) -> bool:
        return cap in self.caps

    def require(self, cap: Cap, what: str = "") -> None:
        if cap not in self.caps:
            raise NotSupported(f"{what or cap.value} is not available on {self.title}")

    @abc.abstractmethod
    def takeoff(self) -> None: ...

    @abc.abstractmethod
    def land(self) -> None: ...

    @abc.abstractmethod
    def emergency_stop(self) -> None:
        """Cut motors / land as hard as this backend allows. Must be safe to call from ANY
        thread while another command is blocking, and must not raise."""

    @abc.abstractmethod
    def hover(self, seconds: float = 1.0) -> None: ...

    @abc.abstractmethod
    def move(self, direction: Direction, distance_cm: float, speed: float | None = None) -> None:
        """Relative move. ``speed`` is backend-native (m/s on EDU); ``None`` = backend default."""

    @abc.abstractmethod
    def turn(self, degrees: float) -> None:
        """Positive = clockwise (right), negative = counter-clockwise (left)."""

    @abc.abstractmethod
    def go(self, roll: int, pitch: int, yaw: int, throttle: int, seconds: float) -> None:
        """Raw stick control, each axis -100..100, held for ``seconds``."""

    def stick(self, roll: int, pitch: int, yaw: int, throttle: int, hold_s: float) -> None:
        """Live sticks for PILOT mode. NON-blocking and called ``stick_hz`` times a second, so unlike
        ``go`` it uses the project's convention: +roll right, +pitch forward, +yaw clockwise, +throttle
        up. ``hold_s`` is the time until the next call."""
        raise NotSupported(f"live stick control is not available on {self.title}")

    def _drift(self, roll: int, pitch: int, yaw: int, throttle: int, seconds: float,
               cm_s: float, deg_s: float) -> None:
        """Advance the dead-reckoned pose by ``seconds`` of stick input (``cm_s`` / ``deg_s`` at 100)."""
        s = cm_s * seconds / 100.0
        self.pose.apply_move(Direction.FORWARD, pitch * s)
        self.pose.apply_move(Direction.RIGHT, roll * s)
        self.pose.z = max(0.0, self.pose.z + throttle * s)
        self.pose.apply_turn(yaw * deg_s * seconds / 100.0)

    def flip(self, direction: str = "back") -> None:
        raise NotSupported(f"flip is not available on {self.title}")

    def shape(self, name: str, **kwargs) -> None:
        raise NotSupported(f"shape '{name}' is not available on {self.title}")

    def set_led(self, r: int, g: int, b: int, brightness: int = 255) -> None:
        raise NotSupported(f"LED is not available on {self.title}")

    def led_off(self) -> None:
        self.set_led(0, 0, 0, 0)

    def beep(self, hz: int = 440, ms: int = 200) -> None:
        raise NotSupported(f"buzzer is not available on {self.title}")

    def grab(self) -> None:
        raise NotSupported(f"gripper is not available on {self.title}")

    def release(self) -> None:
        raise NotSupported(f"gripper is not available on {self.title}")

    def teleport(self, x: float, y: float, z: float) -> None:
        raise NotSupported(f"teleport is not available on {self.title}")

    def reset_position(self) -> None:
        """Put the drone back at its start (sim) or just zero the dead-reckoned pose (EDU)."""
        self.pose.reset()

    @abc.abstractmethod
    def read_telemetry(self) -> Telemetry:
        """Never block for long; unavailable fields stay ``None``."""

    def info(self) -> dict[str, str]:
        """Key/value facts for the 'Metadata' panel (port, firmware, counters ...)."""
        return {"backend": self.title}
