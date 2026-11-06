"""CoDrone Simulator over the vendor ``codrone_simulator`` UDP link (127.0.0.1:5000).

What is different from the real drone, and handled here:
    * the library never learns whether the simulator is running -- UDP sends just vanish -- so
      ``connect`` probes a range query and fails clearly if nothing answers
    * its socket has no timeout, so any query would hang forever; we set one
    * query replies are bare datagrams: concurrent queries (telemetry thread + a routine) would
      steal each other's reply, so queries are serialised and stale datagrams are drained first
    * moves return before the simulator finishes them, so we sleep for an *estimated* duration;
      ``cm_per_s`` is a tunable guess, not a measured simulator constant
    * no battery, position, attitude or flight-state read-back exists: position is dead-reckoned
"""

from __future__ import annotations

import select
import socket
import struct
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

DEFAULT_ADDR = ("127.0.0.1", 5000)
QUERY_TIMEOUT_S = 1.0
DEFAULT_HOVER_CM = 100.0
SIM_YAW_SIGN = -1
STICK_DEG_S = 120.0


class SimBackend(DroneBackend):
    kind = "sim"
    title = "CoDrone Simulator"
    caps = frozenset({Cap.RANGE, Cap.VERTICAL, Cap.LED, Cap.BUZZER, Cap.GRAB, Cap.TELEPORT})
    stick_hz = 10.0

    def __init__(self, verify: bool = True, cm_per_s: float = 80.0, vertical_cm_per_s: float = 60.0) -> None:
        super().__init__()
        self.verify = verify
        self.cm_per_s = cm_per_s
        self.vertical_cm_per_s = vertical_cm_per_s
        self._drone = None
        self._addr = DEFAULT_ADDR
        self._query_lock = threading.Lock()
        self._connected = False

    def connect(self, port: str | None = None) -> str:
        """``port`` may be ``"host:port"`` to reach a simulator on another machine."""
        try:
            import codrone_simulator as lib
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"codrone_simulator could not be imported: {exc}") from exc

        addr = self._parse_addr(port)
        try:
            drone = lib.Drone()
            if addr != DEFAULT_ADDR:
                drone._socket.close()
                drone._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                drone._socket.connect(addr)
            drone._socket.settimeout(QUERY_TIMEOUT_S)
            drone.pair()
        except OSError as exc:
            raise BackendError(f"could not open a UDP link to {addr[0]}:{addr[1]}: {exc}") from exc
        except SystemExit as exc:
            raise BackendError("the simulator library tried to exit") from exc

        self._drone, self._addr = drone, addr
        if self.verify:
            try:
                self._query("get_front_range,cm", 4)
            except BackendError:
                self._close_socket()
                self._drone = None
                raise BackendError(
                    f"no answer from the simulator at {addr[0]}:{addr[1]} -- open the CoDrone "
                    "Simulator and press play (or connect with verification off)"
                ) from None
        self._connected = True
        self.flying = False
        self.pose.reset()
        return f"{addr[0]}:{addr[1]}"

    @staticmethod
    def _parse_addr(port: str | None) -> tuple[str, int]:
        if not port:
            return DEFAULT_ADDR
        host, _, p = port.rpartition(":")
        if not host:
            host, p = DEFAULT_ADDR[0], port
        try:
            return host, int(p)
        except ValueError:
            raise BackendError(f"bad simulator address '{port}' (use host:port)") from None

    def _close_socket(self) -> None:
        try:
            if self._drone is not None:
                self._drone._socket.close()
        except Exception:  # noqa: BLE001
            pass

    def disconnect(self) -> None:
        self._connected = False
        self.flying = False
        self._close_socket()
        self._drone = None

    @property
    def connected(self) -> bool:
        return self._connected and self._drone is not None

    @property
    def _d(self):
        if not self.connected:
            raise BackendError("not connected -- run `pair` first")
        return self._drone

    def _need_flying(self) -> None:
        if not self.flying:
            raise BackendError("drone is not flying -- `takeoff` first")

    def _query(self, message: str, size: int) -> bytes:
        """Send a query and wait (briefly) for its datagram reply."""
        d = self._drone
        if d is None:
            raise BackendError("not connected")
        with self._query_lock:
            sock = d._socket
            try:
                while select.select([sock], [], [], 0)[0]:
                    sock.recvfrom(4096)
                sock.send(message.encode())
                return sock.recvfrom(size)[0]
            except (socket.timeout, TimeoutError):
                raise BackendError("simulator did not answer in time") from None
            except OSError as exc:
                raise BackendError(f"simulator link error: {exc}") from exc

    def _float_query(self, name: str) -> float:
        raw = self._query(f"{name},cm", 4)
        if len(raw) < 4:
            raise BackendError(f"short reply to {name}")
        return struct.unpack("f", raw[:4])[0]

    def _sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def takeoff(self) -> None:
        self._d.takeoff()
        self.flying = True
        self.pose.x = self.pose.y = 0.0
        try:
            z = self._float_query("get_bottom_range")
            self.pose.z = z if z >= 0 else DEFAULT_HOVER_CM
        except BackendError:
            self.pose.z = DEFAULT_HOVER_CM

    def land(self) -> None:
        self._d.land()
        self._sleep(1.5)
        self.flying = False
        self.pose.z = 0.0

    def emergency_stop(self) -> None:
        """The simulator has no motor cut: the hardest stop it offers is an immediate land."""
        self.flying = False
        d = self._drone
        if d is None:
            return
        try:
            d._send_to_server("land")
        except BaseException:  # noqa: BLE001 - estop must never raise, no matter what thread call it
            pass

    def hover(self, seconds: float = 1.0) -> None:
        self._d.hover(max(0.01, float(seconds)))

    def move(self, direction: Direction, distance_cm: float, speed: float | None = None) -> None:
        direction = Direction(direction)
        self._need_flying()
        d = self._d
        dist = abs(float(distance_cm))
        if dist == 0:
            return
        spd = 1.0 if speed is None else max(0.1, float(speed))
        if direction in (Direction.UP, Direction.DOWN):
            sign = 1 if direction is Direction.UP else -1
            seconds = dist / self.vertical_cm_per_s
            d.go(0, 0, 0, sign * 50, seconds)
        else:
            {
                Direction.FORWARD: d.move_forward,
                Direction.BACKWARD: d.move_backward,
                Direction.LEFT: d.move_left,
                Direction.RIGHT: d.move_right,
            }[direction](dist, "cm", spd)
            self._sleep(dist / (self.cm_per_s * spd))
        self.pose.apply_move(direction, dist)

    def turn(self, degrees: float) -> None:
        self._need_flying()
        deg = float(degrees)
        if abs(deg) < 1:
            return
        (self._d.turn_right if deg > 0 else self._d.turn_left)(abs(int(round(deg))))
        self.pose.apply_turn(deg)

    def go(self, roll: int, pitch: int, yaw: int, throttle: int, seconds: float) -> None:
        """Raw sticks in the simulator's native directions. Does not update the dead-reckoned pose."""
        self._d.go(int(roll), int(pitch), int(yaw), int(throttle), float(seconds))

    def stick(self, roll: int, pitch: int, yaw: int, throttle: int, hold_s: float) -> None:
        # each go only last one tick, so if the packets stop the sim should stop too
        self._d._send_to_server(f"go,{int(roll)},{int(pitch)},{SIM_YAW_SIGN * int(yaw)},{int(throttle)},{hold_s:.3f}")
        self._drift(roll, pitch, yaw, throttle, hold_s, self.cm_per_s, STICK_DEG_S)

    def set_led(self, r: int, g: int, b: int, brightness: int = 255) -> None:
        self._d.set_drone_LED(int(r), int(g), int(b), int(brightness))

    def led_off(self) -> None:
        self._d.drone_LED_off()

    def beep(self, hz: int = 440, ms: int = 200) -> None:
        self._d.drone_buzzer(int(hz), int(ms))

    def grab(self) -> None:
        self._d.grab()

    def release(self) -> None:
        self._d.release()

    def teleport(self, x: float, y: float, z: float) -> None:
        self._d.teleport_to(x, y, z)
        self.pose.x, self.pose.y, self.pose.z = float(x), float(y), float(z)

    def reset_position(self) -> None:
        self._d.reset_position_and_rotation()
        self.pose.reset()
        self.flying = False

    def read_telemetry(self) -> Telemetry:
        if not self.connected:
            raise BackendError("not connected")
        front = self._float_query("get_front_range")
        bottom = self._float_query("get_bottom_range")
        valid = lambda v: None if v is None or v < 0 else round(v, 1)  # noqa: E731
        front, bottom = valid(front), valid(bottom)
        return Telemetry(
            flight_state="Flight" if self.flying else "Ready",
            front_range_cm=front,
            bottom_range_cm=bottom,
            wall_detected=None if front is None else front < 50,
            pos_cm=self.pose.as_tuple(),
            pos_is_estimate=True,
        )

    def info(self) -> dict[str, str]:
        return {"backend": self.title, "link": f"{self._addr[0]}:{self._addr[1]}",
                "pace": f"{self.cm_per_s:g} cm/s (estimate)"}
