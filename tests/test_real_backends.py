"""The real backends against fakes: a UDP server for the simulator, a stand-in Drone for the EDU."""

import socket
import struct
import threading
import time

import pytest

from codrone_tui.backend import BackendError, Cap, Direction
from codrone_tui.backend.edu import EduBackend
from codrone_tui.backend.sim import SimBackend


# ----------------------------------------------------------------------------- simulator
class FakeSimServer:
    """Answers get_front_range / get_bottom_range like the Unity simulator, records the rest."""

    def __init__(self, answer=True):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.received: list[str] = []
        self.answer = answer
        self.front, self.bottom = 123.5, 42.0
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        self.sock.settimeout(0.05)
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(1024)
            except OSError:
                continue
            msg = data.decode()
            self.received.append(msg)
            if not self.answer:
                continue
            if msg.startswith("get_front_range"):
                self.sock.sendto(struct.pack("f", self.front), addr)
            elif msg.startswith("get_bottom_range"):
                self.sock.sendto(struct.pack("f", self.bottom), addr)

    def close(self):
        self._stop = True
        self._t.join(timeout=1)
        self.sock.close()


@pytest.fixture
def sim_server():
    s = FakeSimServer()
    yield s
    s.close()


def test_sim_connect_probe_and_telemetry(sim_server):
    b = SimBackend(cm_per_s=10000)  # make estimated sleeps negligible
    assert b.connect(f"127.0.0.1:{sim_server.port}") == f"127.0.0.1:{sim_server.port}"
    t = b.read_telemetry()
    assert t.front_range_cm == 123.5 and t.bottom_range_cm == 42.0
    assert t.battery is None and t.pos_is_estimate
    b.disconnect()
    assert not b.connected


def test_sim_connect_fails_clearly_when_nothing_answers():
    s = FakeSimServer(answer=False)
    try:
        b = SimBackend()
        with pytest.raises(BackendError, match="no answer from the simulator"):
            b.connect(f"127.0.0.1:{s.port}")
        assert not b.connected
    finally:
        s.close()


def test_sim_no_verify_connects_without_a_server():
    b = SimBackend(verify=False)
    b.connect("127.0.0.1:59999")
    assert b.connected
    b.disconnect()


def test_sim_move_requires_flight_and_updates_pose(sim_server):
    b = SimBackend(cm_per_s=100000)
    b.connect(f"127.0.0.1:{sim_server.port}")
    with pytest.raises(BackendError, match="not flying"):
        b.move(Direction.FORWARD, 50)
    b.flying = True
    b.move(Direction.FORWARD, 50)
    b.move(Direction.LEFT, 20)
    assert (round(b.pose.x), round(b.pose.y)) == (50, 20)
    time.sleep(0.1)
    assert any(m.startswith("move_forward,50") for m in sim_server.received)
    b.disconnect()


def test_sim_emergency_stop_sends_land_and_never_raises(sim_server):
    b = SimBackend()
    b.emergency_stop()  # not connected: must not raise
    b.connect(f"127.0.0.1:{sim_server.port}")
    b.flying = True
    b.emergency_stop()
    time.sleep(0.15)
    assert "land" in sim_server.received and not b.flying
    b.disconnect()


def test_sim_capabilities():
    b = SimBackend()
    assert b.has(Cap.GRAB) and b.has(Cap.TELEPORT) and not b.has(Cap.FLIP) and not b.has(Cap.TELEMETRY)


# --------------------------------------------------------------------------------- EDU
class FakeEduDrone:
    """Behaves like the vendor class where it matters to us."""

    mode = "ok"  # ok | exit | not_ready

    def __init__(self, **kwargs):
        from codrone_tui.backend.edu import load_edu_lib

        lib = load_edu_lib()
        self.lib = lib
        self.transfer = lambda h, d: None
        self.state_data = [0, None, lib.ModeFlight.Ready, None, None, None, 87, lib.ModeMovement.Hovering, 2]
        self.position_data = [0, 0.5, -0.25, 1.0]
        self.range_data = [0, 1200, -1000]  # mm; -1000 = no reading
        self.motion_data = [0, 5, -3, -98, 1, 2, 3, 4, -5, 90]
        self.flow_data = [0, 0.1, -0.2]
        self.altitude_data = [0, 29.5, 101200.0, 3.0, 1.0]
        self.information_data = [0, "m", "1.2.3", "c", "4.5.6"]
        self.sent = []
        self.closed = False

    def pair(self, port=None):
        if FakeEduDrone.mode == "exit":
            exit()  # what the vendor library does
        if FakeEduDrone.mode == "not_ready":
            self.state_data[2] = self.lib.ModeFlight.None_
        return True  # the vendor returns True even when the drone is off

    def get_state_data(self, delay=0.01):
        return self.state_data

    def get_information_data(self, delay=0.05):
        return self.information_data

    def sendRequest(self, device, kind):
        self.sent.append(kind)

    def isOpen(self):
        return not self.closed

    def close(self):
        self.closed = True

    def takeoff(self):
        pass

    def land(self):
        pass

    def emergency_stop(self):
        self.sent.append("estop")

    def turn_left(self, deg):
        self.sent.append(("left", deg))

    def turn_right(self, deg):
        self.sent.append(("right", deg))

    def move_forward(self, dist, unit, **kw):
        self.sent.append(("fwd", dist, unit, kw))

    def flip(self, direction):
        self.sent.append(("flip", direction))


def test_importing_the_vendor_lib_does_not_break_asyncio_sleep():
    """Regression: codrone_edu.drone swaps asyncio.sleep for a browser-only function that raises
    NameError on desktop; after our loader it must be the genuine one again, and awaitable."""
    import asyncio
    import sys

    from codrone_tui.backend.edu import load_edu_lib

    sys.modules.pop("codrone_edu.drone", None)  # force the vendor import (and its patch) to run again
    lib = load_edu_lib()
    assert asyncio.sleep is asyncio.tasks.sleep
    asyncio.run(asyncio.sleep(0))
    # ... and even the vendor's own patched function (alive during the import window) must not blow up
    asyncio.run(lib.interruptible_sleep(0.01))


def test_vendor_lib_loads_from_a_background_thread_while_a_loop_sleeps():
    """The prewarm path: import on a thread while the UI loop keeps calling asyncio.sleep."""
    import asyncio
    import sys
    import threading

    from codrone_tui.backend.edu import load_edu_lib

    sys.modules.pop("codrone_edu.drone", None)
    done = threading.Event()
    threading.Thread(target=lambda: (load_edu_lib(), done.set()), daemon=True).start()

    async def ui_loop():
        while not done.is_set():
            await asyncio.sleep(0.001)  # would raise NameError if the vendor's patch leaked

    asyncio.run(ui_loop())
    assert asyncio.sleep is asyncio.tasks.sleep


@pytest.fixture
def edu(monkeypatch):
    from codrone_tui.backend.edu import load_edu_lib

    lib = load_edu_lib()
    FakeEduDrone.mode = "ok"
    monkeypatch.setattr(lib, "Drone", FakeEduDrone)
    return EduBackend()


def test_edu_connect_ok_caches_firmware(edu):
    edu.connect("COM9")
    assert edu.connected
    assert edu.info()["drone fw"] == "1.2.3" and edu.info()["controller fw"] == "4.5.6"


def test_edu_system_exit_becomes_backend_error(edu):
    FakeEduDrone.mode = "exit"
    with pytest.raises(BackendError, match="could not open"):
        edu.connect("COM9")
    assert not edu.connected


def test_edu_drone_off_is_detected_even_though_pair_says_true(edu, monkeypatch):
    FakeEduDrone.mode = "not_ready"
    monkeypatch.setattr("codrone_tui.backend.edu.time.sleep", lambda s: None)
    with pytest.raises(BackendError, match="did not answer"):
        edu.connect()
    assert not edu.connected


def test_edu_telemetry_unit_conversion(edu, monkeypatch):
    edu.connect()
    monkeypatch.setattr("codrone_tui.backend.edu.time.sleep", lambda s: None)
    t = edu.read_telemetry()
    assert t.battery == 87 and t.flight_state == "Ready"
    assert t.front_range_cm == 120.0 and t.bottom_range_cm is None  # mm -> cm, -1000 -> None
    assert t.wall_detected is False
    assert t.pos_cm == (50.0, -25.0, 100.0)  # metres -> cm
    assert t.accel == (0.5, -0.3, -9.8)  # raw is m/s^2 x 10
    assert t.angles_deg == (4.0, -5.0, 90.0)
    assert t.flow_cm_s == (10.0, -20.0)
    assert t.pos_is_estimate is False


def test_edu_takeoff_takes_hover_height_from_the_range_sensor(edu, monkeypatch):
    """Otherwise pose.z stays 0 and a routine's goto(z=80) would climb a further 80 cm."""
    monkeypatch.setattr("codrone_tui.backend.edu.time.sleep", lambda s: None)
    edu.connect()
    edu._drone.range_data[2] = 900  # mm
    edu.takeoff()
    assert edu.pose.z == pytest.approx(90.0)
    edu._drone.range_data[2] = -1000  # sensor has no reading
    edu.takeoff()
    assert edu.pose.z == 100.0


def test_sim_takeoff_takes_hover_height_from_the_bottom_range(sim_server):
    b = SimBackend(cm_per_s=100000)
    b.connect(f"127.0.0.1:{sim_server.port}")
    sim_server.bottom = 87.0
    b.takeoff()
    assert b.pose.z == pytest.approx(87.0)
    b.disconnect()


def test_edu_turn_convention_and_pose(edu):
    edu.connect()
    edu.flying = True
    edu.turn(90)  # ours: clockwise-positive -> vendor turn_right
    edu.turn(-30)
    assert ("right", 90) in edu._drone.sent and ("left", 30) in edu._drone.sent
    assert edu.pose.heading == 60


def test_edu_flip_refuses_low_battery(edu):
    edu.connect()
    edu.flying = True
    edu._drone.state_data[6] = 30
    with pytest.raises(BackendError, match="refuses flips"):
        edu.flip("back")
    edu._drone.state_data[6] = 80
    edu.flip("back")
    assert ("flip", "back") in edu._drone.sent


def test_edu_emergency_stop_never_raises(edu):
    edu.emergency_stop()  # not connected
    edu.connect()
    edu._drone.emergency_stop = lambda: (_ for _ in ()).throw(SystemExit(1))
    edu.emergency_stop()  # even a SystemExit from the library is swallowed
    assert not edu.flying


def test_edu_transfer_is_serialised(edu):
    """Two threads calling the wrapped transfer must never overlap (pyserial is not thread-safe)."""
    edu.connect()
    inside, overlaps = 0, 0
    guard = threading.Lock()

    def slow(h, d):
        nonlocal inside, overlaps
        with guard:
            inside += 1
            overlaps += inside > 1
        time.sleep(0.01)
        with guard:
            inside -= 1

    # re-wrap a slow transfer the same way connect() does
    edu._wrap_transfer(type("D", (), {"transfer": staticmethod(slow)})())
    d = type("D", (), {"transfer": staticmethod(slow)})()
    edu._wrap_transfer(d)
    ts = [threading.Thread(target=lambda: [d.transfer(1, 2) for _ in range(5)]) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert overlaps == 0
