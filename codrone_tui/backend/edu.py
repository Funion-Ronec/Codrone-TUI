"""Real CoDrone EDU over USB serial, wrapping the vendor ``codrone_edu`` library.

Things the vendor library does that a TUI has to defend against:
    * it calls ``exit()`` (``SystemExit``) when the controller cannot be opened
    * ``pair()`` returns True even when the drone is off, so we check the flight state ourselves
    * every request/response helper sleeps, and writes to the serial port from whatever thread
      calls it -- pyserial is not safe for two writers, so ``transfer`` is wrapped in a lock
    * it ``print()``s status text (Textual captures that; see app.py)
"""

from __future__ import annotations

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

CDE_CONTROLLER_VID = 1155
MIN_FLIP_BATTERY = 50
DEFAULT_HOVER_CM = 100.0
STICK_CM_S = 100.0
STICK_DEG_S = 120.0

_SHAPES = {"square", "triangle", "circle", "spiral", "sway"}


_LIB_LOCK = threading.Lock()


def load_edu_lib():
    """Import ``codrone_edu.drone`` without letting it break asyncio. Safe to call from any thread.

    The vendor module overwrites ``asyncio.sleep`` at import time with a browser-only version that
    calls an undefined ``checkInterrupt()``; on desktop every later ``await asyncio.sleep()`` --
    including Textual's own -- would die with a NameError. The real one is ``asyncio.tasks.sleep``.

    The import takes ~1.7 s (numpy, Pillow), so it can run on a background thread while the UI is
    already up. For the instant between the vendor's patch and our restore, a harmless
    ``checkInterrupt`` is provided so the patched sleep still behaves like the real one.
    """
    import asyncio
    import builtins

    with _LIB_LOCK:
        if not hasattr(builtins, "checkInterrupt"):
            builtins.checkInterrupt = lambda: None
        try:
            from codrone_edu import drone as lib
        finally:
            asyncio.sleep = asyncio.tasks.sleep
        return lib


def prewarm_edu_lib() -> None:
    """Start importing the vendor library in the background so ``pair`` does not pay for it."""

    def work() -> None:
        try:
            load_edu_lib()
        except BaseException:  # noqa: BLE001
            pass

    threading.Thread(target=work, name="edu-lib-prewarm", daemon=True).start()


def find_controller_ports() -> list[tuple[str, str, bool]]:
    """(device, description, looks_like_codrone_controller) for every serial port."""
    try:
        from serial.tools.list_ports import comports
    except Exception:
        return []
    return [(p.device, p.description or "", p.vid == CDE_CONTROLLER_VID) for p in comports()]


class EduBackend(DroneBackend):
    kind = "edu"
    title = "CoDrone EDU"
    caps = frozenset(
        {Cap.TELEMETRY, Cap.POSITION, Cap.RANGE, Cap.VERTICAL, Cap.FLIP, Cap.SHAPES, Cap.LED,
         Cap.BUZZER, Cap.ESTOP, Cap.COLOR, Cap.TRIM}
    )

    def __init__(self) -> None:
        super().__init__()
        self._drone = None
        self._lib = None
        self._port: str | None = None
        self._firmware: dict[str, str] = {}
        self._tx_lock = threading.RLock()
        prewarm_edu_lib()

    def connect(self, port: str | None = None) -> str:
        try:
            lib = load_edu_lib()
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"codrone_edu could not be imported: {exc}") from exc
        self._lib = lib

        drone = lib.Drone(flagShowDepLibraryVersion=False)
        self._wrap_transfer(drone)
        try:
            drone.pair(port)
        except SystemExit as exc:  # vendor lib just calls exit() instead of raising, real dumb
            raise BackendError(
                "could not open the CoDrone EDU controller"
                + (f" on {port}" if port else " (is it plugged in by USB?)")
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"pairing failed: {type(exc).__name__}: {exc}") from exc

        # pair() says it worked even if the drone is off, lying ass function. check we really get state back
        state = None
        for _ in range(10):
            state = drone.get_state_data()[2]
            if state is lib.ModeFlight.Ready:
                break
            time.sleep(0.15)
        if state is not lib.ModeFlight.Ready:
            self._safe_disconnect(drone)
            raise BackendError(
                "controller found, but the drone did not answer -- check that the drone is on "
                "and paired to the controller"
            )

        self._drone = drone
        self._port = port or self._guess_port()
        self.flying = False
        self.pose.reset()
        try:  # grab firmware strings now cuz info() run on the UI thread and that cant touch serial
            fw = drone.get_information_data()
            self._firmware = {"drone fw": str(fw[2]), "controller fw": str(fw[4])}
        except Exception:  # noqa: BLE001
            self._firmware = {}
        return self._port or "usb"

    def _guess_port(self) -> str | None:
        for device, _desc, is_cde in find_controller_ports():
            if is_cde:
                return device
        return None

    def _wrap_transfer(self, drone) -> None:
        original = drone.transfer

        def locked_transfer(header, data):
            with self._tx_lock:
                return original(header, data)

        drone.transfer = locked_transfer

    @staticmethod
    def _safe_disconnect(drone) -> None:
        try:
            drone.close()
        except BaseException:  # noqa: BLE001
            pass

    def disconnect(self) -> None:
        drone, self._drone = self._drone, None
        self.flying = False
        if drone is not None:
            self._safe_disconnect(drone)

    @property
    def connected(self) -> bool:
        d = self._drone
        if d is None:
            return False
        try:
            return bool(d.isOpen())
        except Exception:  # noqa: BLE001
            return False

    @property
    def _d(self):
        if self._drone is None:
            raise BackendError("not connected -- run `pair` first")
        return self._drone

    def _need_flying(self) -> None:
        if not self.flying:
            raise BackendError("drone is not flying -- `takeoff` first")

    def _battery(self) -> int | None:
        try:
            return int(self._d.get_state_data()[6])
        except Exception:  # noqa: BLE001
            return None

    def takeoff(self) -> None:
        self._d.takeoff()
        self.flying = True
        self.pose.x = self.pose.y = 0.0
        self._sync_height()

    def _sync_height(self) -> None:
        """Take the real hover height from the downward range sensor. Without this the dead-reckoned
        height stays 0 and a routine asking for z=80 would climb a further 80 cm on top of the hover."""
        try:
            d = self._d
            d.sendRequest(self._lib.DeviceType.Drone, self._lib.DataType.Range)
            time.sleep(0.05)
            mm = d.range_data[2]
            self.pose.z = float(mm) / 10.0 if mm is not None and mm >= 0 else DEFAULT_HOVER_CM
        except Exception:  # noqa: BLE001
            self.pose.z = DEFAULT_HOVER_CM

    def land(self) -> None:
        self._d.land()
        self.flying = False
        self.pose.z = 0.0

    def emergency_stop(self) -> None:
        self.flying = False
        d = self._drone
        if d is None:
            return
        try:
            d.emergency_stop()
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
        kw = {} if speed is None else {"speed": float(speed)}
        {
            Direction.FORWARD: d.move_forward,
            Direction.BACKWARD: d.move_backward,
            Direction.LEFT: d.move_left,
            Direction.RIGHT: d.move_right,
            Direction.UP: d.move_upward,
            Direction.DOWN: d.move_downward,
        }[direction](dist, "cm", **kw)
        self.pose.apply_move(direction, dist)

    def turn(self, degrees: float) -> None:
        self._need_flying()
        deg = float(degrees)
        if abs(deg) < 1:
            return
        # vendor is backwards from us, turn_left/right only take a positive number
        (self._d.turn_right if deg > 0 else self._d.turn_left)(abs(int(round(deg))))
        self.pose.apply_turn(deg)

    def go(self, roll: int, pitch: int, yaw: int, throttle: int, seconds: float) -> None:
        """Raw sticks in the vendor's native directions. Does not update the dead-reckoned pose."""
        d = self._d
        d.set_roll(int(roll))
        d.set_pitch(int(pitch))
        d.set_yaw(int(yaw))
        d.set_throttle(int(throttle))
        try:
            d.move(float(seconds))
        finally:
            d.reset_move_values()

    def stick(self, roll: int, pitch: int, yaw: int, throttle: int, hold_s: float) -> None:
        # vendor yaw goes positive = left (turn_left send +), ours is clockwise so flip it
        self._d.sendControl(int(roll), int(pitch), -int(yaw), int(throttle))
        self._drift(roll, pitch, yaw, throttle, hold_s, STICK_CM_S, STICK_DEG_S)

    def flip(self, direction: str = "back") -> None:
        if direction not in ("front", "back", "left", "right"):
            raise BackendError("flip direction must be front, back, left or right")
        self._need_flying()
        battery = self._battery()
        if battery is not None and battery < MIN_FLIP_BATTERY:
            raise BackendError(f"battery is {battery}% -- the EDU refuses flips below {MIN_FLIP_BATTERY}%")
        self._d.flip(direction)

    def shape(self, name: str, **kwargs) -> None:
        if name not in _SHAPES:
            raise NotSupported(f"unknown shape '{name}' (try: {', '.join(sorted(_SHAPES))})")
        self._need_flying()
        getattr(self._d, name)(**kwargs)

    def set_led(self, r: int, g: int, b: int, brightness: int = 255) -> None:
        self._d.set_drone_LED(int(r), int(g), int(b), int(brightness))

    def led_off(self) -> None:
        self._d.drone_LED_off()

    def beep(self, hz: int = 440, ms: int = 200) -> None:
        self._d.drone_buzzer(int(hz), int(ms))

    def reset_position(self) -> None:
        self.pose.reset()

    def read_telemetry(self) -> Telemetry:
        d, lib = self._d, self._lib
        for kind in (lib.DataType.State, lib.DataType.Position, lib.DataType.Range,
                     lib.DataType.Motion, lib.DataType.RawFlow, lib.DataType.Altitude):
            d.sendRequest(lib.DeviceType.Drone, kind)
        time.sleep(0.06)

        s, pos, rng = d.state_data, d.position_data, d.range_data
        mot, flow, alt = d.motion_data, d.flow_data, d.altitude_data

        def mm_to_cm(v):
            return None if v is None or v < 0 else round(float(v) / 10.0, 1)

        front, bottom = mm_to_cm(rng[1]), mm_to_cm(rng[2])
        return Telemetry(
            battery=int(s[6]),
            flight_state=getattr(s[2], "name", str(s[2])),
            movement_state=getattr(s[7], "name", str(s[7])),
            front_range_cm=front,
            bottom_range_cm=bottom,
            wall_detected=None if front is None else front < 50,
            pos_cm=(round(pos[1] * 100, 1), round(pos[2] * 100, 1), round(pos[3] * 100, 1)),
            angles_deg=(float(mot[7]), float(mot[8]), float(mot[9])),
            accel=(mot[1] / 10.0, mot[2] / 10.0, mot[3] / 10.0),
            gyro=(float(mot[4]), float(mot[5]), float(mot[6])),
            flow_cm_s=(round(flow[1] * 100, 1), round(flow[2] * 100, 1)),
            temperature_c=round(float(alt[1]), 1),
            pressure_pa=round(float(alt[2]), 0),
            pos_is_estimate=False,
        )

    def info(self) -> dict[str, str]:
        return {"backend": self.title, "port": self._port or "-", **self._firmware}
