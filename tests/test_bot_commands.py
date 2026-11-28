"""Bot shell commands (nav, ahead, approach, scan, ask, plan, avoid, llm) and the bot inside the real app."""

import asyncio
import time

import pytest
from textual.widgets import Input

from codrone_tui import commands
from codrone_tui.adc.field import default_field
from codrone_tui.backend import Cap, FlightExecutor
from codrone_tui.bot import BotSettings, TextPlanner, World, WorldMock, box, demo_world
from codrone_tui.bot_ui import BotController
from codrone_tui.commands import Host, Shell, raw_args
from codrone_tui.edu_app import EduApp

FIELD = default_field()


class BotHost(Host):
    def __init__(self, world=None, far=None):
        self.backend = WorldMock("edu", world or World(), time_scale=0.0, far_reading=far)
        self.lines = []
        self.executor = FlightExecutor(self._on_job)

    def _on_job(self, kind, job):
        if kind == "failed":
            self.lines.append((f"job failed: {job.label}: {job.error}", "error"))

    def out(self, text, kind="info"):
        self.lines.append((text, kind))

    def text(self):
        return "\n".join(t for t, _ in self.lines)

    def connect_backend(self, port):
        self.executor.submit("pair", lambda: self.backend.connect(port))

    def latest_telemetry(self):
        return self.backend.read_telemetry() if self.backend.connected else None


def drain(host, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if not host.executor.busy:
            time.sleep(0.02)
            if not host.executor.busy:
                return
        time.sleep(0.01)
    raise AssertionError("executor never went idle")


def make(tmp_path, world=None, far=None, llm="off", url="http://127.0.0.1:9"):
    host = BotHost(world, far)
    sh = Shell(host, log_dir=tmp_path)
    s = BotSettings(settle_s=0.0, llm=llm, ollama_url=url, timeout_s=5)
    ctl = BotController(sh, settings=s, field=lambda: FIELD, planner=TextPlanner(s), background=False)
    return sh, ctl


def up(sh):
    sh.run_line("pair")
    drain(sh.host)
    sh.run_line("takeoff")
    drain(sh.host)
    assert sh.backend.flying


@pytest.fixture
def bot(tmp_path):
    made = []

    def factory(**kw):
        sh, ctl = make(tmp_path, **kw)
        made.append(sh)
        return sh, ctl

    yield factory
    for sh in made:
        sh.host.executor.shutdown()


def test_raw_args_keep_free_text_intact():
    assert raw_args("-p don't hit the wall, ok?") == (["don't hit the wall, ok?"], {"p"})
    assert raw_args("   ") == ([], set())
    assert raw_args("-30 cm") == (["-30 cm"], set())


def test_ask_takes_apostrophes_without_quoting(bot):
    sh, ctl = bot()
    sh.run_line("ask don't take off yet")
    out = sh.host.text()
    assert "parse error" not in out and "language model is off" in out
    assert ctl.pending is None


def test_ask_then_plan_run_flies_the_plan(bot):
    sh, ctl = bot()
    sh.run_line("pair")
    drain(sh.host)
    sh.run_line("ask take off, fly forward 1 m, turn left and land")
    assert "plan from phrases" in sh.host.text() and ctl.pending is not None
    sh.run_line("plan show")
    sh.run_line("plan run")
    drain(sh.host)
    names = [c[0] for c in sh.backend.log]
    assert names.count("takeoff") == 1 and "turn" in names and names[-1] == "land"
    assert not sh.backend.flying and "finished" in sh.host.text()


def test_plan_run_needs_a_good_plan(bot):
    sh, ctl = bot()
    sh.run_line("plan run")
    assert "no plan" in sh.host.text()
    sh.run_line("ask fly through the keyhole")  # ambiguous, and the model is off
    assert ctl.pending is None
    sh.run_line("plan bogus")
    assert "expected show, run, save or clear" in sh.host.text()


def test_plan_save_writes_a_script_that_source_can_run(bot, tmp_path):
    sh, ctl = bot()
    sh.run_line("ask take off, go up 20 cm, hover for 1 second, forward 40 cm and land")
    path = tmp_path / "plans" / "plan.txt"  # the folder doesn't exist yet
    sh.run_line(f'plan save "{path}"')
    text = path.read_text(encoding="utf-8")
    assert "# plan:" in text and "ahead 40" in text.splitlines()
    sh.run_line("pair")
    drain(sh.host)
    sh.run_line(f'source "{path}"')
    drain(sh.host)
    moves = [c for c in sh.backend.log if c[0] == "move"]
    assert ("move", "up", 20.0, None) in moves and not sh.backend.flying


def test_nav_command_goes_around_a_crate(bot):
    sh, ctl = bot(world=World([box("crate", 150, 0, 40, 60)]), far=819.0)
    up(sh)
    sh.run_line("nav 300 0")
    drain(sh.host)
    p = sh.backend.pose
    assert sh.backend.crashed is None and abs(p.x - 300) <= 10 and abs(p.y) <= 10
    assert "nav: at (" in sh.host.text()


def test_ahead_stops_and_approach_closes_in(bot):
    sh, ctl = bot(world=World([box("wall", 205, 0, 10, 400)]))
    up(sh)
    sh.run_line("approach 60")
    drain(sh.host)
    assert "approach: stopped with" in sh.host.text()
    assert sh.backend.pose.x == pytest.approx(140, abs=6)
    sh.run_line("ahead 300")
    drain(sh.host)
    assert "job failed: ahead 300cm: blocked" in sh.host.text()
    assert sh.backend.crashed is None and sh.backend.pose.x == pytest.approx(170, abs=6)


def test_scan_prints_a_table(bot):
    sh, ctl = bot(world=World([box("wall", 105, 0, 10, 60)]), far=819.0)
    up(sh)
    sh.run_line("scan")
    drain(sh.host)
    out = sh.host.text()
    assert "scan from (0, 0)" in out and "ahead (heading" in out and "most room" in out


def test_through_and_landon_use_the_field(bot):
    sh, ctl = bot(world=demo_world(FIELD))
    up(sh)
    sh.run_line("through red_arch")
    sh.run_line("landon pad")
    drain(sh.host)
    assert sh.backend.crashed is None and not sh.backend.flying
    assert "Fly through the red arch" in sh.host.text() or "red_arch" in sh.host.text()
    sh.run_line("through landing_pad")
    assert "not something you fly through" in sh.host.text()


def test_range_commands_hide_on_a_blind_drone(bot):
    sh, ctl = bot()
    sh.backend.caps = frozenset({Cap.VERTICAL})
    sh.run_line("nav 10 10")
    assert "`nav` is not available" in sh.host.text()


def test_avoid_and_llm_report_when_unavailable(bot):
    sh, ctl = bot()
    sh.run_line("avoid on")
    assert "mission features are not loaded" in sh.host.text()
    sh.run_line("llm status")
    assert "language model is off" in sh.host.text()


def test_llm_status_and_models(bot, ollama):
    srv = ollama(models=("glm4:latest", "qwen3.5:9b"))
    sh, ctl = bot(llm="ollama", url=srv.url)
    sh.run_line("llm status")
    assert "qwen3.5:9b is ready" in sh.host.text()
    sh.run_line("llm use glm4")
    sh.run_line("llm models")
    assert "glm4:latest   <- in use" in sh.host.text()
    sh.run_line("llm use mistral")
    sh.run_line("llm status")
    assert "mistral is NOT pulled" in sh.host.text()


def test_settings_from_config_keep_good_values_and_report_bad_ones():
    s, problems = BotSettings.from_config({"margin_cm": 40, "model": "glm4", "bogus": 1, "samples": "three",
                                           "sensor_max_cm": 5000, "llm": "cloud", "scan_angles": [-60, 0, 60]})
    assert s.margin_cm == 40 and s.model == "glm4" and s.scan_angles == (-60.0, 0.0, 60.0)
    assert s.samples == 3 and s.sensor_max_cm == 150 and s.llm == "ollama"
    text = " | ".join(problems)
    for expect in ("unknown setting 'bogus'", "samples:", "sensor_max_cm", "llm must be"):
        assert expect in text
    assert BotSettings.from_config(None) == (BotSettings(), [])


def test_demo_backend_has_a_world_to_avoid():
    from codrone_tui.cli import demo_backend

    b = demo_backend("edu")
    assert isinstance(b, WorldMock) and any(x.name == "crate" for x in b.world.boxes)
    assert demo_backend("sim").has(Cap.RANGE)


# the bot inside the real Textual app --------------------------------------------------------------------------

async def wait_for(pilot, pred, timeout=6.0):
    waited = 0.0
    while waited < timeout:
        if pred():
            return True
        await pilot.pause(0.05)
        waited += 0.05
    return False


async def type_line(pilot, line):
    if not isinstance(pilot.app.focused, Input):
        await pilot.press("i")
    pilot.app.console_panel.input.value = line
    await pilot.press("enter")
    await pilot.pause(0.1)


def log_text(app):
    return "\n".join(s.text for s in app.console_panel.log_widget.lines)


def test_ask_and_plan_run_in_the_app_go_through_the_mission_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "LOG_DIR", tmp_path / "logs")

    async def go():
        app = EduApp(WorldMock("edu", demo_world(FIELD), time_scale=0.003), demo=True)
        app.adc_store = tmp_path / "adc.json"
        async with app.run_test(size=(160, 48)) as pilot:
            assert await wait_for(pilot, lambda: app.backend.connected)
            assert app.bot is not None and "ask" in app.shell.commands
            assert any(title == "BOT" for title, _ in app.sidebar_sections())
            await type_line(pilot, "ask take off, fly forward 50 cm and land")
            assert await wait_for(pilot, lambda: app.bot.pending is not None)
            await type_line(pilot, "plan run")
            assert await wait_for(pilot, lambda: "mission 'plan:" in log_text(app) and "ended" in log_text(app), 10)
            names = [c[0] for c in app.backend.log]
            assert "takeoff" in names and names[-1] == "land" and not app.backend.flying
            await type_line(pilot, "avoid on")
            assert app.adc.context_factory == app.bot.mission_context
            await type_line(pilot, "mission run takeoff_land")
            assert await wait_for(pilot, lambda: log_text(app).count("ended") >= 2, 10)
            assert "nav to (86, 0)" in log_text(app)  # the guarded context routed the landing approach
            assert app.errors_seen == 0

    asyncio.run(go())
