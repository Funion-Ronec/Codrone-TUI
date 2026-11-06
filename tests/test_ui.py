"""Headless Textual tests: both TUIs driven with real key presses against the mock drone."""

import asyncio
import json

import pytest
from textual.command import CommandPalette
from textual.widgets import Input

from codrone_tui import commands
from codrone_tui.backend import MockBackend
from codrone_tui.edu_app import EduApp
from codrone_tui.screens import ConfirmScreen, HelpScreen
from codrone_tui.sim_app import SimApp


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "LOG_DIR", tmp_path / "logs")


def make(kind, tmp_path, **kw):
    app = (EduApp if kind == "edu" else SimApp)(MockBackend(kind, time_scale=0.003), demo=True, **kw)
    app.adc_store = tmp_path / "adc.json"
    return app


async def wait_for(pilot, pred, timeout=4.0):
    waited = 0.0
    while waited < timeout:
        if pred():
            return True
        await pilot.pause(0.05)
        waited += 0.05
    return False


async def type_line(pilot, line, real=False):
    """Submit a command. ``real=True`` sends one key event per character (slow); the default sets
    the Input's value and presses Enter, which exercises the same submit path."""
    if not isinstance(pilot.app.focused, Input):
        await pilot.press("i")
    if real:
        for ch in line:
            await pilot.press("space" if ch == " " else ch)
    else:
        pilot.app.console_panel.input.value = line
    await pilot.press("enter")
    await pilot.pause(0.1)


def log_text(app):
    return "\n".join(s.text for s in app.console_panel.log_widget.lines)


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("kind", ["edu", "sim"])
def test_app_starts_and_connects(kind, tmp_path):
    async def go():
        app = make(kind, tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            assert await wait_for(pilot, lambda: "connected: mock://localhost" in log_text(app))
            assert app.backend.connected
            assert app.errors_seen == 0
            assert app.title.startswith("CoDrone")
            await pilot.pause(0.5)
            assert app.poller.samples > 0 and app.poller.errors == 0

    run(go())


def test_edu_and_sim_are_visibly_different(tmp_path):
    async def go():
        edu, sim = make("edu", tmp_path), make("sim", tmp_path)
        async with edu.run_test(size=(160, 48)) as p1:
            await p1.pause(0.3)
            assert len(edu.query("#bat")) == 1 and edu.theme == "codrone-edu"
        async with sim.run_test(size=(160, 48)) as p2:
            await p2.pause(0.3)
            assert len(sim.query("#bat")) == 0 and sim.theme == "codrone-sim"

    run(go())


def test_command_flow_and_history(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await wait_for(pilot, lambda: app.backend.connected)
            await type_line(pilot, "takeoff", real=True)
            for line in ("forward 50", "tr 90", "land"):
                await type_line(pilot, line)
            assert await wait_for(pilot, lambda: ("land",) in app.backend.log)
            ops = [c[0] for c in app.backend.log if c[0] in ("takeoff", "move", "turn", "land")]
            assert ops == ["takeoff", "move", "turn", "land"]
            await pilot.press("up")
            assert app.console_panel.input.value == "land"

    run(go())


def test_estop_key_works_even_while_typing(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await wait_for(pilot, lambda: app.backend.connected)
            await pilot.press("i")
            for ch in "forw":
                await pilot.press(ch)
            await pilot.press("ctrl+x")  # priority binding, gotta beat the Input's own ctrl+x (cut) or estop dont work while typing
            assert await wait_for(pilot, lambda: ("emergency_stop",) in app.backend.log)
            assert "EMERGENCY STOP" in log_text(app)
            await pilot.press("ctrl+l")
            await pilot.pause(0.2)
            assert "not connected" not in log_text(app)

    run(go())


def test_sim_estop_is_honest_about_having_no_motor_cut(tmp_path):
    async def go():
        app = make("sim", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await wait_for(pilot, lambda: app.backend.connected)
            await pilot.press("ctrl+x")
            await pilot.pause(0.2)
            assert "simulator has no motor cut" in log_text(app)

    run(go())


def test_panel_toggles_layouts_theme_and_help(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.pause(0.3)
            assert app.query_one("#telemetry-panel").display
            await pilot.press("3")
            assert not app.query_one("#telemetry-panel").display
            await pilot.press("3")
            assert app.query_one("#telemetry-panel").display

            await type_line(pilot, "layout focus")
            assert not app.query_one("#telemetry-panel").display and not app.query_one("#bottom").display
            await type_line(pilot, "layout full")
            assert app.query_one("#bottom").display and app.query_one("#score-panel").display

            await type_line(pilot, "theme btop")
            assert app.theme == "btop"
            await type_line(pilot, "theme nonsense")
            assert "no theme 'nonsense'" in log_text(app)

            await pilot.press("escape")
            assert app.mode == "NORMAL"
            await pilot.press("question_mark")
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            assert not isinstance(app.screen, HelpScreen)

    run(go())


def test_command_palette_opens(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("ctrl+p")
            await pilot.pause(0.3)
            assert isinstance(app.screen, CommandPalette)

    run(go())


def test_timed_match_with_routine_then_finish(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await wait_for(pilot, lambda: app.backend.connected)
            await type_line(pilot, "preflight confirm all")
            await type_line(pilot, "mission run takeoff_land -m")
            assert await wait_for(pilot, lambda: app.adc.match.state == "running")
            assert await wait_for(pilot, lambda: not app.adc.running and not app.backend.flying)
            assert app.adc.scorecard.total >= 5
            await type_line(pilot, "match finish pad")
            m = app.adc.match
            assert m.state == "finished" and len(m.attempts) == 1 and m.best_score >= 10
            data = json.loads((tmp_path / "adc.json").read_text(encoding="utf-8"))
            assert data

    run(go())


def test_manual_credit_undo_and_spiral(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            await type_line(pilot, "credit mini1 mini2 mini3")
            assert app.adc.scorecard.total == 15 * 3 + 10
            await type_line(pilot, "undo")
            assert app.adc.scorecard.total == 15 * 2
            await type_line(pilot, "credit bogus")
            assert "unknown task 'bogus'" in log_text(app)

    run(go())


def test_mission_refuses_when_unsafe(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await wait_for(pilot, lambda: app.backend.connected)
            await type_line(pilot, "takeoff")
            await wait_for(pilot, lambda: app.backend.flying)
            await type_line(pilot, "mission run phase1")
            assert "land first" in log_text(app)
            await type_line(pilot, "mission run nosuchroutine")
            assert "no routine 'nosuchroutine'" in log_text(app)

    run(go())


def test_real_edu_hardware_asks_for_confirmation(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        app.demo = False
        async with app.run_test(size=(160, 48)) as pilot:
            await wait_for(pilot, lambda: app.backend.connected)
            await type_line(pilot, "mission run takeoff_land")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(0.2)
            assert not app.adc.running and not any(c[0] == "takeoff" for c in app.backend.log)

    run(go())


def test_exit_lands_a_flying_drone_first(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await wait_for(pilot, lambda: app.backend.connected)
            await type_line(pilot, "takeoff")
            await wait_for(pilot, lambda: app.backend.flying)
            await type_line(pilot, "exit")
            assert await wait_for(pilot, lambda: not app.backend.flying and not app.backend.connected)
        assert ("land",) in app.backend.log

    run(go())


def test_library_print_from_the_flight_thread_lands_in_the_console(tmp_path):
    """codrone_edu print()s coloured status text from its own threads; it must not corrupt the screen."""
    import threading

    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            t = threading.Thread(target=lambda: print("\x1b[32mBattery = 87%\x1b[0m"))
            t.start()
            t.join()
            assert await wait_for(pilot, lambda: "lib: Battery = 87%" in log_text(app))

    run(go())


def test_safety_net_lands_a_drone_left_airborne():
    from codrone_tui.cli import safety_net

    b = MockBackend("edu", time_scale=0.001)
    b.connect()
    b.flying = True
    safety_net(b)
    assert ("land",) in b.log and not b.connected

    class Stubborn(MockBackend):
        def land(self):
            raise SystemExit(1)

    s = Stubborn("edu")
    s.connect()
    s.flying = True
    safety_net(s)


def test_help_command_in_app_lists_mission_commands(tmp_path):
    async def go():
        app = make("sim", tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            await type_line(pilot, "help")
            text = log_text(app)
            assert "mission list|run" in text and "teleport" in text and "flip" not in text

    run(go())
