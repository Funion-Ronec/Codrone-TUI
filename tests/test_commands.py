import time
from pathlib import Path

import pytest

from codrone_tui.backend import FlightExecutor, MockBackend
from codrone_tui.commands import Host, Shell, split_args


class FakeHost(Host):
    def __init__(self, flavor="edu", tmp: Path | None = None):
        self.backend = MockBackend(flavor, time_scale=0.001)
        self.lines: list[tuple[str, str]] = []
        self.executor = FlightExecutor(self._on_job)
        self.exited = None
        self.cleared = 0
        self.themes = []

    def _on_job(self, kind, job):
        if kind in ("failed",):
            self.lines.append((f"job failed: {job.label}: {job.error}", "error"))

    def out(self, text, kind="info"):
        self.lines.append((text, kind))

    def text(self):
        return "\n".join(t for t, _ in self.lines)

    def clear_console(self):
        self.cleared += 1

    def connect_backend(self, port):
        self.executor.submit("pair", lambda: self.backend.connect(port))

    def disconnect_backend(self):
        self.backend.disconnect()

    def request_exit(self, land_first):
        self.exited = land_first

    def set_theme(self, name):
        self.themes.append(name)

    def apply_layout(self, name):
        pass

    def toggle_panel(self, name):
        pass

    def latest_telemetry(self):
        return self.backend.read_telemetry() if self.backend.connected else None


def drain(host, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if not host.executor.busy:
            time.sleep(0.02)
            if not host.executor.busy:
                return
        time.sleep(0.01)
    raise AssertionError("executor never went idle")


@pytest.fixture
def sh(tmp_path):
    host = FakeHost()
    shell = Shell(host, log_dir=tmp_path)
    yield shell
    host.executor.shutdown()


def connect(sh):
    sh.run_line("pair")
    drain(sh.host)
    assert sh.backend.connected


def test_split_args_treats_negative_numbers_as_values():
    assert split_args(["-30", "-e", "x"]) == (["-30", "x"], {"e"})


def test_unknown_and_unconnected_commands_are_reported(sh):
    sh.run_line("frobnicate")
    sh.run_line("takeoff")
    assert "unknown command 'frobnicate'" in sh.host.text()
    assert "not connected" in sh.host.text()


def test_takeoff_move_land_sequence(sh):
    connect(sh)
    for line in ("takeoff", "forward 100", "tr 90", "forward 50", "land"):
        sh.run_line(line)
    drain(sh.host)
    b = sh.backend
    assert not b.flying
    assert [c[0] for c in b.log if c[0] in ("takeoff", "move", "turn", "land")] == [
        "takeoff", "move", "turn", "move", "land"]
    assert b.pose.heading == 90


def test_aliases_and_flags(sh):
    connect(sh)
    sh.run_line("to")
    sh.run_line("f 20")
    sh.run_line("turn left 45")
    sh.run_line("land")
    drain(sh.host)
    assert ("turn", -45.0) in sh.backend.log
    assert [c[0] for c in sh.backend.log if c[0] in ("takeoff", "move", "turn", "land")] == [
        "takeoff", "move", "turn", "land"]


def test_bad_arguments_never_raise_and_show_usage(sh):
    connect(sh)
    for bad in ("forward", "forward abc", "forward 99999", "turn", "led 1 2", "stick 1 2 3", "hover -5"):
        sh.run_line(bad)
    out = sh.host.text()
    assert out.count("usage:") >= 7


def test_sim_hides_edu_only_commands(tmp_path):
    host = FakeHost("sim")
    sh = Shell(host, log_dir=tmp_path)
    sh.run_line("flip")
    sh.run_line("grab")
    assert "`flip` is not available" in host.text()
    assert "not connected" in host.text()
    host.executor.shutdown()


def test_help_lists_only_available_commands(tmp_path):
    sim, edu = FakeHost("sim"), FakeHost("edu")
    s1, s2 = Shell(sim, log_dir=tmp_path), Shell(edu, log_dir=tmp_path)
    s1.run_line("help")
    s2.run_line("help")
    assert "teleport" in sim.text() and "teleport" not in edu.text()
    assert "flip" in edu.text() and "flip" not in sim.text()
    sim.executor.shutdown(); edu.executor.shutdown()


def test_urgent_land_jumps_the_queue_but_plain_land_does_not(sh):
    connect(sh)
    sh.run_line("takeoff")
    drain(sh.host)
    sh.run_line("wait 30")
    time.sleep(0.05)
    sh.run_line("forward 100")
    sh.run_line("land -e")
    drain(sh.host, timeout=3)
    moves = [c for c in sh.backend.log if c[0] == "move"]
    assert not moves and not sh.backend.flying
    assert "dropped 1 queued" in sh.host.text()


def test_estop_acts_immediately_even_while_busy(sh):
    connect(sh)
    sh.run_line("takeoff")
    drain(sh.host)
    sh.backend.time_scale = 1.0
    sh.backend.speed_cm_s = 10
    sh.run_line("forward 500")
    time.sleep(0.1)
    t0 = time.time()
    sh.run_line("estop")
    assert time.time() - t0 < 0.5 and ("emergency_stop",) in sh.backend.log
    drain(sh.host, timeout=3)
    assert "EMERGENCY STOP" in sh.host.text()


def test_cat_and_echo_are_safe(sh, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\nworld", encoding="utf-8")
    sh.run_line(f'cat "{f}"')
    sh.run_line("cat does-not-exist.txt")
    sh.run_line("echo hi && calc.exe")
    out = sh.host.text()
    assert "hello" in out and "world" in out and "not found" in out
    assert "hi && calc.exe" in out


def test_source_runs_in_order(sh, tmp_path):
    connect(sh)
    script = tmp_path / "s.txt"
    script.write_text("echo one\ntakeoff\necho two\nforward 10\nland\necho three\n", encoding="utf-8")
    sh.run_line(f'source "{script}"')
    drain(sh.host)
    texts = [t for t, _ in sh.host.lines]
    assert texts.index("one") < texts.index("two") < texts.index("three")
    assert [c[0] for c in sh.backend.log if c[0] in ("takeoff", "move", "land")] == ["takeoff", "move", "land"]


def test_source_rejects_nested_source_and_exit(sh, tmp_path):
    script = tmp_path / "bad.txt"
    script.write_text("exit\n", encoding="utf-8")
    sh.run_line(f'source "{script}"')
    assert "may not contain" in sh.host.text()


def test_command_log_is_written(sh, tmp_path):
    sh.run_line("echo logged")
    text = sh.log_path.read_text(encoding="utf-8")
    assert "echo logged" in text


def test_exit_requests_landing_unless_forced(sh):
    sh.run_line("exit")
    assert sh.host.exited is True
    sh.run_line("exit -f")
    assert sh.host.exited is False


def test_completion(sh):
    assert sh.complete("tak") == "takeoff"
    assert sh.complete("turn le") == "turn left"
    assert sh.complete("flip ") is None or sh.complete("flip b") == "flip back"
    assert sh.complete("zzz") is None
