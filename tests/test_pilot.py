"""PILOT mode: keymap + rebinding, held-key detection, the stick sender and the keystroke HUD."""

import asyncio

import pytest

from codrone_tui import commands, pilot
from codrone_tui.backend import MockBackend
from codrone_tui.edu_app import EduApp
from codrone_tui.pilot import FIRST_HOLD_S, REPEAT_HOLD_S, KeyMap, PilotController, RepeatKeys, sticks_for
from codrone_tui.sim_app import SimApp
from codrone_tui.widgets.graph import _FALLBACK
from codrone_tui.widgets.pilot import render_hud


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(pilot, "KEYS_FILE", tmp_path / "pilot_keys.toml")


class FakeExecutor:
    busy = False


def flying_mock(kind="edu"):
    b = MockBackend(kind, time_scale=0.0)
    b.connect()
    b.takeoff()
    return b


def test_default_keys_match_the_layout():
    km = KeyMap.defaults("edu")
    assert [km.keys[a] for a in ("forward", "left", "back", "right")] == ["w", "a", "s", "d"]
    assert (km.keys["turn_left"], km.keys["turn_right"], km.keys["speed_up"], km.keys["speed_down"]) == ("j", "l", "i", "k")
    assert (km.keys["down"], km.keys["up"]) == ("q", "e")
    assert km.command_for("f1") == "shape square"
    assert "f1" not in KeyMap.defaults("sim").commands


def test_rebinding_refuses_conflicts_unless_forced():
    km = KeyMap.defaults("edu")
    km.bind("forward", "up")
    assert km.action_for("up") == "forward" and km.action_for("w") is None
    with pytest.raises(ValueError, match="already back"):
        km.bind("forward", "s")
    km.bind("forward", "s", force=True)
    assert km.keys["back"] == "" and km.keys["forward"] == "s"
    with pytest.raises(ValueError, match="already runs"):
        km.bind("land", "f1")


def test_reserved_and_normal_mode_keys_are_refused():
    km = KeyMap.defaults("edu")
    for key in ("escape", "ctrl+x", "ctrl+l"):
        with pytest.raises(ValueError, match="reserved"):
            km.bind("forward", key)
    with pytest.raises(ValueError, match="normal-mode"):
        km.bind("toggle", "p", blocked=frozenset({"p"}))
    with pytest.raises(ValueError, match="toggle can't be unbound"):
        km.unbind("f")
    with pytest.raises(ValueError, match="pilot"):
        km.bind_command("f9", "pilot off")


def test_keymap_round_trips_through_the_file(tmp_path):
    km = KeyMap.defaults("edu")
    km.bind("forward", "up")
    km.bind_command("f7", "mission run phase1")
    km.unbind("f1")
    km.save()
    loaded, err = KeyMap.load("edu")
    assert err is None
    assert loaded.keys == km.keys and loaded.commands == km.commands


def test_a_broken_keys_file_falls_back_to_defaults(tmp_path):
    pilot.KEYS_FILE.write_text("[keys\nforward = ", encoding="utf-8")
    km, err = KeyMap.load("edu")
    assert err and "default keys" in err
    assert km.keys["forward"] == "w"


def test_sticks_follow_the_project_convention():
    assert sticks_for({"forward"}, 40) == (0, 40, 0, 0)
    assert sticks_for({"back", "left"}, 40) == (-40, -40, 0, 0)
    assert sticks_for({"turn_right", "up"}, 25) == (0, 0, 25, 25)
    assert sticks_for({"forward", "back"}, 40) == (0, 0, 0, 0)


def test_repeat_keys_bridge_the_auto_repeat_delay():
    rk = RepeatKeys()
    rk.note("w", now=0.0)
    assert rk.held({"w"}, now=FIRST_HOLD_S - 0.01) == {"w"}
    rk.note("w", now=0.5)
    rk.note("w", now=0.53)
    assert rk.held({"w"}, now=0.53 + REPEAT_HOLD_S - 0.01) == {"w"}
    assert rk.held({"w"}, now=0.53 + REPEAT_HOLD_S + 0.01) == set()


def test_controller_flies_only_when_safe_and_zeros_on_release():
    b = MockBackend("edu", time_scale=0.0)
    ex = FakeExecutor()
    ctl = PilotController(b, ex, KeyMap.defaults("edu"), rate_hz=20)
    ctl.activate()
    ctl.note_key("w", now=0.0)
    ctl.step(now=0.01)
    assert ctl.status == "offline" and not any(c[0] == "stick" for c in b.log)

    b.connect()
    ctl.step(now=0.02)
    assert ctl.status == "landed" and not any(c[0] == "stick" for c in b.log)

    b.takeoff()
    ex.busy = True
    ctl.step(now=0.03)
    assert ctl.status == "busy" and not any(c[0] == "stick" for c in b.log)

    ex.busy = False
    ctl.step(now=0.04)
    assert ctl.status == "live" and ("stick", 0, 25, 0, 0) in b.log
    x0 = b.pose.x
    ctl.step(now=0.09)
    assert b.pose.x > x0

    ctl.step(now=5.0)
    assert b.log[-1] == ("stick", 0, 0, 0, 0)


def test_leaving_pilot_mode_sends_zero_sticks():
    b = flying_mock()
    ctl = PilotController(b, FakeExecutor(), KeyMap.defaults("edu"))
    ctl.activate()
    ctl.note_key("j", now=0.0)
    ctl.step(now=0.01)
    assert b.last_stick == (0, 0, -25, 0)
    ctl.deactivate()
    ctl.step(now=0.02)
    assert b.last_stick == (0, 0, 0, 0) and ctl.status == "off"


def test_lost_window_focus_stops_the_drone():
    class FakeWin32:
        name = "held keys"
        focused = True

        def supports(self, key):
            return True

        def window_focused(self):
            return self.focused

        def held(self, keys):
            return {"w"} & set(keys)

    b, win = flying_mock(), FakeWin32()
    ctl = PilotController(b, FakeExecutor(), KeyMap.defaults("edu"), win32=win)
    ctl.activate()
    ctl.step()
    assert b.last_stick == (0, 25, 0, 0)
    win.focused = False
    ctl.step()
    assert ctl.status == "no focus" and b.last_stick == (0, 0, 0, 0)


def test_hud_shows_the_bound_keys_and_lights_held_ones():
    km = KeyMap.defaults("edu")
    km.bind("forward", "up")
    ctl = PilotController(flying_mock(), FakeExecutor(), km)
    pal = dict(_FALLBACK)
    text = render_hud(ctl.snapshot(), km, pal, lit={"forward"}, has_flip=True)
    plain = text.plain
    assert "↑" in plain and "square" in plain and "front" in plain and "OFF" in plain
    lit_style = next(s for s in text.spans if text.plain[s.start:s.end].strip() == "↑").style
    assert pal["accent"] in str(lit_style)
    assert "not on this drone" in render_hud(ctl.snapshot(), km, pal, lit=set(), has_flip=False).plain
    assert "press a key for FORWARD" in render_hud(ctl.snapshot(), km, pal, lit=set(), has_flip=True,
                                                   capture="FORWARD").plain


def make(kind, tmp_path):
    app = (EduApp if kind == "edu" else SimApp)(MockBackend(kind, time_scale=0.003), demo=True)
    app.adc_store = tmp_path / "adc.json"
    return app


async def wait_for(pilot_, pred, timeout=4.0):
    waited = 0.0
    while waited < timeout:
        if pred():
            return True
        await pilot_.pause(0.05)
        waited += 0.05
    return False


async def type_line(p, line):
    await p.press("colon")
    assert p.app.mode == "INSERT"
    p.app.console_panel.input.value = line
    await p.press("enter")
    await p.pause(0.1)
    await p.press("escape")


def log_text(app):
    return "\n".join(s.text for s in app.console_panel.log_widget.lines)


def test_fly_with_the_keyboard(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as p:
            assert await wait_for(p, lambda: app.backend.connected)
            await p.press("f")
            assert app.mode == "PILOT"
            assert app.query_one("#pilot-panel").display
            await p.press("t")
            assert await wait_for(p, lambda: app.backend.flying)
            await p.press("w")
            assert await wait_for(p, lambda: ("stick", 0, 25, 0, 0) in app.backend.log)
            assert await wait_for(p, lambda: app.backend.last_stick == (0, 0, 0, 0))
            await p.press("i")
            await p.press("q")
            assert await wait_for(p, lambda: ("stick", 0, 0, 0, -40) in app.backend.log)
            assert app.is_running
            await p.press("1")
            assert await wait_for(p, lambda: ("flip", "front") in app.backend.log)
            await p.press("f5")
            assert await wait_for(p, lambda: "mission run hover_test" in app.shell.history)
            hud = app.query_one("#pilot-pad").render()
            assert "LIVE" in str(hud) or "BUSY" in str(hud)
            await p.press("escape")
            assert app.mode == "NORMAL"
            assert await wait_for(p, lambda: app.backend.last_stick == (0, 0, 0, 0))

    asyncio.run(go())


def test_rebind_from_the_console(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(160, 48)) as p:
            await wait_for(p, lambda: app.backend.connected)
            await type_line(p, "pilot bind forward up")
            assert app.pilot.keymap.keys["forward"] == "up"
            assert 'forward = "up"' in pilot.KEYS_FILE.read_text(encoding="utf-8")

            await p.press("i")
            app.console_panel.input.value = "pilot bind back"
            await p.press("enter")
            await p.pause(0.1)
            assert app.pilot.capture == ("action", "back")
            await p.press("x")
            assert app.pilot.keymap.keys["back"] == "x" and app.pilot.capture is None
            assert app.mode == "NORMAL"

            await type_line(p, "pilot bind left x")
            assert "already back" in log_text(app)
            await type_line(p, 'pilot run f7 "land -e"')
            assert app.pilot.keymap.commands["f7"] == "land -e"
            await type_line(p, "pilot bind toggle p")
            assert "normal-mode key" in log_text(app)
            await type_line(p, "pilot unbind f1")
            assert "f1" not in app.pilot.keymap.commands

            await p.press("f")
            await p.press("t")
            assert await wait_for(p, lambda: app.backend.flying)
            await p.press("up")
            assert await wait_for(p, lambda: ("stick", 0, 25, 0, 0) in app.backend.log)

            await type_line(p, "pilot reset")
            assert app.pilot.keymap.keys["forward"] == "w"

    asyncio.run(go())


def test_sim_pilot_has_no_flips_but_flies(tmp_path):
    async def go():
        app = make("sim", tmp_path)
        async with app.run_test(size=(160, 48)) as p:
            await wait_for(p, lambda: app.backend.connected)
            await p.press("f")
            await p.press("t")
            assert await wait_for(p, lambda: app.backend.flying)
            await p.press("1")
            assert "no flips on" in log_text(app)
            await p.press("l")
            assert await wait_for(p, lambda: ("stick", 0, 0, 25, 0) in app.backend.log)

    asyncio.run(go())


def test_pilot_layout_comes_back_after_leaving(tmp_path):
    async def go():
        app = make("edu", tmp_path)
        async with app.run_test(size=(120, 40)) as p:
            await wait_for(p, lambda: app.backend.connected)
            assert not app.query_one("#pilot-panel").display
            await p.press("f")
            assert app.query_one("#pilot-panel").display
            await p.press("escape")
            assert not app.query_one("#pilot-panel").display

    asyncio.run(go())
