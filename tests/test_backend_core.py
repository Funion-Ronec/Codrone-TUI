import time

import pytest

from codrone_tui.backend import (
    Aborted,
    BackendError,
    Cap,
    Direction,
    FlightExecutor,
    MockBackend,
    NotSupported,
    Pose,
    TelemetryPoller,
)


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_pose_moves_follow_heading():
    p = Pose()
    p.apply_move(Direction.FORWARD, 100)
    assert (round(p.x), round(p.y)) == (100, 0)
    p.apply_turn(90)
    p.apply_move(Direction.FORWARD, 50)
    assert (round(p.x), round(p.y)) == (100, -50)
    p.apply_move(Direction.LEFT, 10)
    assert (round(p.x), round(p.y)) == (110, -50)
    p.apply_move(Direction.DOWN, 999)
    assert p.z == 0.0


def test_mock_edu_vs_sim_capabilities():
    edu, sim = MockBackend("edu"), MockBackend("sim")
    assert edu.has(Cap.FLIP) and not sim.has(Cap.FLIP)
    assert sim.has(Cap.GRAB) and not edu.has(Cap.GRAB)
    sim.connect()
    with pytest.raises(NotSupported):
        sim.flip()
    assert sim.read_telemetry().battery is None
    edu.connect()
    assert edu.read_telemetry().battery == 100


def test_executor_runs_in_order_and_reports_failures():
    events = []
    ex = FlightExecutor(lambda kind, job: events.append((kind, job.label)))
    ex.submit("a", lambda: None)
    ex.submit("bad", lambda: (_ for _ in ()).throw(BackendError("boom")))
    ex.submit("exit", lambda: exit(1))
    ex.submit("c", lambda: None)
    assert wait_for(lambda: ("finished", "c") in events)
    kinds = [e for e in events if e[1] in ("bad", "exit")]
    assert ("failed", "bad") in kinds and ("failed", "exit") in kinds
    ex.shutdown()


def test_executor_abort_cancels_queue_and_running_job():
    ex = FlightExecutor()
    j1 = ex.submit("long", lambda: ex.sleep(30))
    j2 = ex.submit("queued", lambda: None)
    assert wait_for(lambda: j1.state == "running")
    assert ex.abort() == 1
    assert wait_for(lambda: j1.state == "cancelled")
    assert j2.state == "cancelled"
    j3 = ex.submit("after", lambda: None)
    assert wait_for(lambda: j3.state == "done")
    with pytest.raises(Aborted):
        ex.stop_event.set()
        ex.check()
    ex.shutdown()


def test_mock_emergency_stop_interrupts_a_blocking_move():
    m = MockBackend("edu", time_scale=1.0, speed_cm_s=10)
    m.connect()
    m.flying = True
    ex = FlightExecutor()
    job = ex.submit("slow", lambda: m.move(Direction.FORWARD, 1000))
    assert wait_for(lambda: job.state == "running")
    m.emergency_stop()
    assert wait_for(lambda: job.state == "failed", timeout=2)
    ex.shutdown()


def test_poller_collects_history():
    m = MockBackend("edu")
    m.connect()
    got = []
    poll = TelemetryPoller(m, hz=50, on_sample=got.append)
    poll.start()
    assert wait_for(lambda: len(got) >= 5)
    poll.stop()
    assert len(poll.series("front_range_cm")) >= 5
