"""Live mode (the model writes guidance once, the flight algorithm flies it), creep flight, the algorithm's
behaviours, the hosted API clients, and API key handling."""

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codrone_tui import commands
from codrone_tui.adc.field import default_field
from codrone_tui.backend import FlightExecutor
from codrone_tui.bot import BotContext, BotSettings, TextPlanner, World, WorldMock, box, demo_world
from codrone_tui.bot import behaviours
from codrone_tui.bot.guided import GuidedRun
from codrone_tui.bot.llm import (
    AnthropicClient,
    KeyStore,
    LlmError,
    OpenAIClient,
    check_api_url,
    make_client,
)
from codrone_tui.bot.navigator import Blocked, Navigator
from codrone_tui.bot.procedure import OPS, Plan, Step, _VirtualExecutor, _VirtualWorld, plan_schema
from codrone_tui.bot.planner import system_prompt
from codrone_tui.bot_ui import BotController
from codrone_tui.commands import Shell

from test_bot_commands import BotHost, drain

FIELD = default_field()
SECRET = "sk-test-0123456789abcdef"


def act(op, note="", **fields):
    return {"note": note, "action": {"op": op, **fields}}


def guidance(*steps, summary="test guidance"):
    return {"summary": summary, "steps": [{"op": op, **f} for op, f in steps], "questions": []}


def virtual(world, settings=None):
    # a drone in `world` with a virtual clock: behaviours that run for N seconds finish at once
    s = settings or BotSettings(settle_s=0.0, side_look_deg=0.0)
    vw = _VirtualWorld("edu", None, s)
    vw.world = world
    ctx = BotContext(vw, _VirtualExecutor(vw), field=None, settings=s, log=lambda m: None, clock=lambda: vw.vt)
    return vw, ctx


def room(half=200.0):
    t = 10.0
    return World([box("north wall", 0, half, 2 * half, t), box("south wall", 0, -half, 2 * half, t),
                  box("east wall", half, 0, t, 2 * half), box("west wall", -half, 0, t, 2 * half)])


def fly_guided(world, *steps, settings=None):
    s = settings or BotSettings(settle_s=0.0, side_look_deg=0.0)
    backend = WorldMock("edu", world, time_scale=0.0)
    backend.connect(None)
    ex = FlightExecutor()
    ctx = BotContext(backend, ex, field=FIELD, settings=s, log=lambda m: None)
    said = []
    run = GuidedRun(Plan(text="test", steps=[Step.from_dict(x) for x in steps]), s,
                    say=lambda t, k="info": said.append((t, k)))
    try:
        run.fly(ctx)
    finally:
        ex.shutdown()
    return run, backend, said


# creep: 5 cm ticks, a reading every tick ---------------------------------------------------------------------

class Counting(WorldMock):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reads = 0
        self.after_reads = None  # (n, box): this box appears after n readings

    def read_telemetry(self):
        self.reads += 1
        if self.after_reads and self.reads == self.after_reads[0]:
            self.world.boxes.append(self.after_reads[1])
        return super().read_telemetry()


def flying_counting(world, s):
    m = Counting("edu", world, time_scale=0.0)
    m.connect(None)
    m.takeoff()
    return m, Navigator(m, _Exe(), settings=s)


class _Exe:
    def check(self):
        pass

    def sleep(self, seconds):
        pass


def test_creep_reads_the_sensor_every_5_cm_and_stops_short_of_a_wall():
    s = BotSettings(settle_s=0.0)
    m, nav = flying_counting(World([box("wall", 205, 0, 10, 400)]), s)
    with pytest.raises(Blocked):
        nav.forward(300)
    # the wall face is at x=200: stopped a margin short of it, having looked every 5 cm on the way
    assert 200 - s.margin_cm - 6 <= m.pose.x <= 200 - s.margin_cm + 0.5 and m.crashed is None
    assert m.reads >= m.pose.x / s.creep_tick_cm
    assert m.last_stick == (0, 0, 0, 0)  # sticks centred at the end: hovering
    assert not any(e[0] == "move" for e in m.log)  # no blind move commands


def test_creep_finishes_a_clear_move_exactly_and_hovers():
    s = BotSettings(settle_s=0.0)
    m, nav = flying_counting(World(), s)
    assert nav.forward(100) == pytest.approx(100, abs=0.5)
    assert m.pose.x == pytest.approx(100, abs=0.5) and m.reads == 21 and m.last_stick == (0, 0, 0, 0)


def test_creep_cancels_for_something_that_appears_mid_move():
    # an obstacle that shows up after the move started: a 60 cm blind step would hit it, a 5 cm tick doesn't
    s = BotSettings(settle_s=0.0)
    m, nav = flying_counting(World(), s)
    m.after_reads = (6, box("someone's hand", 25 + 40 + 5, 0, 10, 60))  # 40 cm ahead of where it is by then
    with pytest.raises(Blocked):
        nav.forward(200)
    assert m.crashed is None and m.pose.x < 70 - s.margin_cm + 1


def test_steps_motion_still_available():
    s = BotSettings(settle_s=0.0, motion="steps")
    m, nav = flying_counting(World(), s)
    nav.forward(100)
    assert any(e[0] == "move" for e in m.log) and m.reads < 21  # a reading per 60 cm step, not per 5 cm


# the algorithm's behaviours ----------------------------------------------------------------------------------

def test_explore_keeps_moving_and_never_touches_the_walls():
    vw, ctx = virtual(room())
    vw.takeoff()
    stats = behaviours.explore(ctx, 60, seed=3)
    assert vw.crashed is None and vw.flying
    assert stats["flown_cm"] > 300 and stats["legs"] >= 4  # it wandered, it didn't hover
    assert abs(vw.pose.x) < 200 and abs(vw.pose.y) < 200
    assert vw.vt >= 60


def test_land_spot_moves_away_from_something_close_before_landing():
    vw, ctx = virtual(World([box("crate", 60, 0, 30, 60)]))  # 45 cm ahead of the takeoff point
    vw.takeoff()
    x, y = behaviours.land_spot(ctx)
    assert not vw.flying and vw.crashed is None
    assert ((x - 60) ** 2 + y ** 2) ** 0.5 > 45 + 15  # further from the crate than where it started


def test_home_comes_back_around_an_obstacle():
    vw, ctx = virtual(World([box("crate", 100, 0, 30, 60)]))
    vw.takeoff()
    vw.pose.x = 200
    behaviours.home(ctx)
    assert abs(vw.pose.x) < 15 and abs(vw.pose.y) < 15 and vw.crashed is None and round(vw.pose.heading) % 360 == 0


# guided runs -------------------------------------------------------------------------------------------------

def test_a_guided_run_flies_every_step_and_records_it(tmp_path):
    s = BotSettings(settle_s=0.0, side_look_deg=0.0)
    steps = [{"op": "takeoff"}, {"op": "move", "direction": "forward", "distance_cm": 80}, {"op": "home"},
             {"op": "land_spot"}]
    backend = WorldMock("edu", World(), time_scale=0.0)
    backend.connect(None)
    ex = FlightExecutor()
    ctx = BotContext(backend, ex, field=FIELD, settings=s, log=lambda m: None)
    run = GuidedRun(Plan(text="out and back", summary="out and back", steps=[Step.from_dict(x) for x in steps]), s,
                    record_dir=tmp_path)
    try:
        run.fly(ctx)
    finally:
        ex.shutdown()
    assert run.ok and run.status == ["done"] * 4 and run.phase == "ended" and not backend.flying
    lines = [json.loads(l) for l in run.record_path.read_text().splitlines()]
    assert lines[0]["goal"] == "out and back" and lines[0]["motion"] == "creep" and len(lines[0]["steps"]) == 4
    assert [l["line"] for l in lines if l.get("event") == "step"] == ["takeoff", "ahead 80", "home", "landspot"]
    assert lines[-1]["end"] == "done"


def test_a_cancelled_move_does_not_end_the_guidance():
    run, b, _ = fly_guided(World([box("wall", 105, 0, 10, 400)]),
                           {"op": "takeoff"}, {"op": "move", "direction": "forward", "distance_cm": 200},
                           {"op": "turn", "degrees": 180}, {"op": "move", "direction": "forward", "distance_cm": 50},
                           {"op": "land"})
    assert run.ok and run.status == ["done", "stopped", "done", "done", "done"] and b.crashed is None
    assert any("cancelled" in e for e in run.events)


class FakeApi:
    """Answers like Anthropic's /v1/messages and OpenAI's /chat/completions. Records requests and headers."""

    def __init__(self, replies=(), status=None):
        self.replies, self.status, self.requests = list(replies), list(status or []), []
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                owner.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, None))
                self._send(200, {"data": [{"id": "model-a"}, {"id": "claude-haiku-5-5"}]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                owner.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if owner.status:
                    code, msg = owner.status.pop(0)
                    return self._send(code, {"error": {"message": msg}})
                reply = owner.replies.pop(0) if owner.replies else {"note": "", "action": {"op": "done"}}
                if self.path.endswith("/v1/messages"):
                    self._send(200, {"content": [{"type": "text", "text": "ok"},
                                                 {"type": "tool_use", "name": "answer", "input": reply}],
                                     "stop_reason": "tool_use", "usage": {"output_tokens": 9}})
                else:
                    self._send(200, {"choices": [{"message": {"content": json.dumps(reply)}}],
                                     "usage": {"completion_tokens": 9}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    made = []

    def make(*replies, status=None):
        srv = FakeApi(replies, status)
        made.append(srv)
        return srv

    yield make
    for srv in made:
        srv.close()


def test_anthropic_client_forces_the_schema_through_a_tool(api):
    srv = api({"note": "hi", "action": {"op": "takeoff"}})
    c = AnthropicClient(srv.url, "claude-haiku-5-5", SECRET, 5)
    data, meta = c.chat_json([{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}], {"type": "object"})
    path, headers, body = srv.requests[0]
    assert path == "/v1/messages" and headers["x-api-key"] == SECRET and headers["anthropic-version"]
    assert body["system"] == "sys" and body["messages"] == [{"role": "user", "content": "go"}]
    assert body["tool_choice"] == {"type": "tool", "name": "answer"} and body["tools"][0]["input_schema"] == {"type": "object"}
    assert data["action"]["op"] == "takeoff" and meta["tokens"] == 9
    assert SECRET not in repr(c)


def test_openai_client_and_its_plain_json_fallback(api):
    srv = api({"note": "", "action": {"op": "land"}}, status=[(400, "response_format json_schema not supported")])
    c = OpenAIClient(srv.url, "local-model", SECRET, 5)
    data, _ = c.chat_json([{"role": "user", "content": "go"}], {"type": "object"})
    assert data["action"]["op"] == "land"
    first, second = srv.requests[0][2], srv.requests[1][2]
    assert srv.requests[0][1]["authorization"] == f"Bearer {SECRET}"
    assert first["response_format"]["type"] == "json_schema" and second["response_format"] == {"type": "json_object"}
    assert c.models() == ["claude-haiku-5-5", "model-a"]


def test_a_rejected_key_says_how_to_fix_it(api):
    srv = api(status=[(401, "invalid x-api-key")])
    with pytest.raises(LlmError, match="rejected the API key.*llm key"):
        AnthropicClient(srv.url, "m", SECRET, 5).chat_json([{"role": "user", "content": "x"}], {})


def test_keys_never_go_over_plain_http_to_another_machine():
    check_api_url("https://api.anthropic.com")
    check_api_url("http://localhost:1234/v1")
    with pytest.raises(LlmError, match="refusing"):
        check_api_url("http://192.168.1.20:8000/v1")


def test_key_store_order_and_private_file(tmp_path):
    path = tmp_path / "cfg" / "keys.toml"
    ks = KeyStore(path, env={})
    assert ks.lookup("anthropic") == (None, "")
    ks.set("anthropic", "sk-saved-aaaaaaaa", save=True)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    fresh = KeyStore(path, env={"ANTHROPIC_API_KEY": "sk-env-bbbbbbbb"})
    assert fresh.lookup("anthropic") == ("sk-env-bbbbbbbb", "$ANTHROPIC_API_KEY")  # env beats the file
    assert KeyStore(path, env={}).lookup("anthropic")[0] == "sk-saved-aaaaaaaa"
    fresh.set("anthropic", "sk-session-cccccccc")
    assert fresh.lookup("anthropic")[1] == "this session"  # the session beats both
    assert fresh.forget("anthropic") and KeyStore(path, env={}).lookup("anthropic") == (None, "")
    with pytest.raises(LlmError):
        ks.set("openai", "has spaces in it")


def test_make_client_needs_a_key_and_picks_the_default_model(tmp_path):
    ks = KeyStore(tmp_path / "k.toml", env={})
    s = BotSettings(llm="anthropic")
    with pytest.raises(LlmError, match="no anthropic API key"):
        make_client(s, ks)
    ks.set("anthropic", SECRET)
    c = make_client(s, ks)
    assert isinstance(c, AnthropicClient) and c.model == "claude-haiku-5-5" and c.url == "https://api.anthropic.com"


def make_shell(tmp_path, srv_url, world=None):
    host = BotHost(world)
    sh = Shell(host, log_dir=tmp_path)
    s = BotSettings(settle_s=0.0, side_look_deg=0.0, llm="openai", api_url=srv_url, api_model="m", timeout_s=5)
    keys = KeyStore(tmp_path / "keys.toml", env={})
    ctl = BotController(sh, settings=s, field=lambda: FIELD, planner=TextPlanner(s, keys=keys), background=False, keys=keys)
    return sh, ctl


def test_a_typed_key_is_masked_everywhere(tmp_path, api):
    srv = api()
    sh, ctl = make_shell(tmp_path, srv.url)
    try:
        sh.run_line(f"llm key {SECRET}")
        assert ctl.keys.lookup("openai")[0] == SECRET
        assert SECRET not in sh.log_path.read_text() and SECRET not in "\n".join(sh.history)
        assert "llm key ********" in sh.history[-1]
        sh.run_line("llm status")
        out = sh.host.text()
        assert SECRET not in out and "…cdef" in out and "key accepted" in out
        assert commands.redact("llm key clear") == "llm key clear"
    finally:
        sh.host.executor.shutdown()


def test_live_stop_and_provider_switching(tmp_path, api):
    srv = api()
    sh, ctl = make_shell(tmp_path, srv.url)
    try:
        sh.run_line("live stop")
        assert "nothing is flying" in sh.host.text()
        sh.run_line("llm provider anthropic")
        assert ctl.settings.llm == "anthropic" and "no anthropic API key yet" in sh.host.text()
        sh.run_line("llm use claude-sonnet-5-5")
        assert ctl.settings.api_model == "claude-sonnet-5-5"
        sh.run_line("llm provider bogus")
        assert "expected one of" in sh.host.text()
    finally:
        sh.host.executor.shutdown()


# A real per-move live flight (the old live mode, qwen3.6:35b in the demo world) crashed here: at (150, 50) facing
# 225 deg, an unguarded forward move of 30 cm, with the beam passing just beside the crate's corner.

def test_the_real_crash_spot_is_cancelled_by_creep():
    run, b, _ = fly_guided(demo_world(FIELD), {"op": "takeoff"}, {"op": "nav", "x_cm": 150, "y_cm": 50},
                           {"op": "face", "degrees": 225}, {"op": "move", "direction": "forward", "distance_cm": 30},
                           {"op": "land"}, settings=BotSettings(settle_s=0.0))
    assert b.crashed is None and run.ok and run.status[3] == "stopped"


def test_a_crash_ends_the_guidance_at_once():
    wall = World([box("wall", -60, 0, 10, 400)])  # behind the drone, where nothing looks
    run, b, said = fly_guided(wall, {"op": "takeoff"}, {"op": "move", "direction": "backward", "distance_cm": 80},
                              {"op": "move", "direction": "forward", "distance_cm": 50}, {"op": "land"})
    assert b.crashed == "wall" and not run.ok and run.status[:3] == ["done", "failed", "pending"]
    assert "CRASH" in run.reason and not run.landed_for_safety


def test_the_model_knows_the_behaviours():
    text = system_prompt(FIELD, BotSettings())
    assert "intent, not word-for-word" in text and "explore" in text and "land_spot" in text
    ops = plan_schema(FIELD)["properties"]["steps"]["items"]["properties"]["op"]["enum"]
    assert {"explore", "home", "land_spot"} <= set(ops) and {"explore", "home", "land_spot"} <= set(OPS)


def test_live_command_asks_the_model_once_then_flies_without_it(tmp_path, api):
    srv = api(guidance(("takeoff", {}), ("move", {"direction": "forward", "distance_cm": 60}), ("home", {}),
                       ("land_spot", {}), summary="Out, back, land somewhere open."))
    sh, ctl = make_shell(tmp_path, srv.url, World([box("crate", 200, 0, 40, 40)]))
    try:
        sh.run_line("live")
        assert "say what you want" in sh.host.text()
        sh.run_line("pair")
        drain(sh.host)
        sh.run_line("live potter forward a bit then come back and land somewhere sensible")  # no key yet
        drain(sh.host)
        assert "no openai API key" in sh.host.text() and not srv.requests
        ctl.keys.set("openai", SECRET)
        sh.run_line("live potter forward a bit then come back and land somewhere sensible")
        drain(sh.host, timeout=15)
        out = sh.host.text()
        assert "no model calls now" in out and "live: done" in out
        run = ctl.live_run
        assert run.ok and run.status == ["done"] * 4 and not sh.backend.flying
        assert len(srv.requests) == 1  # one guidance request; nothing during the flight (the first try had no key)
        st = ctl.panel_state()["live"]
        assert st["phase"] == "ended" and st["steps"][1] == ("ahead 60", "done") and st["result"]["ok"]
        rec = next(tmp_path.glob("bot-live-*.jsonl")).read_text()
        assert SECRET not in rec and "potter forward" in rec
    finally:
        sh.host.executor.shutdown()


def test_behaviour_commands(tmp_path, api):
    sh, ctl = make_shell(tmp_path, api().url, World())
    try:
        sh.run_line("pair")
        drain(sh.host)
        sh.run_line("takeoff")
        drain(sh.host)
        sh.run_line("ahead 50")
        drain(sh.host)
        sh.run_line("home")
        drain(sh.host)
        assert abs(sh.backend.pose.x) < 15
        sh.run_line("landspot")
        drain(sh.host)
        assert not sh.backend.flying
        sh.run_line("explore 1")
        assert "seconds" in sh.host.text()
    finally:
        sh.host.executor.shutdown()


# explore with height changes -----------------------------------------------------------------------------------

def test_explore_changes_height_within_the_limits():
    total = 0
    for world in (room(), demo_world(FIELD)):
        for seed in range(5):
            vw, ctx = virtual(world, BotSettings(side_look_deg=0.0))
            vw.takeoff()
            heights = [vw.pose.z]
            orig = vw.move

            def move(direction, cm, speed=None, _orig=orig):
                _orig(direction, cm, speed)
                heights.append(vw.pose.z)

            vw.move = move
            stats = behaviours.explore(ctx, 60, seed=seed, top_cm=140)
            assert vw.crashed is None and vw.flying
            assert stats["height_changes"] >= 1 and len({round(h) for h in heights}) >= 2
            assert all(40 - 0.5 <= h <= 140 + 0.5 for h in heights)
            total += stats["height_changes"]
    assert total >= 20  # about every other leg or more, across the 10 flights


def test_coming_down_stays_above_what_the_bottom_sensor_sees():
    vw, ctx = virtual(World([box("table", 0, 0, 80, 80, 0, 70)]))
    vw.takeoff()
    vw.pose.z = 140  # above the table, which the front sensor can't see from up here
    assert behaviours.change_height(ctx, 40) == 1
    assert vw.crashed is None and vw.pose.z >= 70 + behaviours.BELOW_CLEAR_CM - 0.5
    # the mock really would crash: a blind descent onto the table
    vw2, _ = virtual(World([box("table", 0, 0, 80, 80, 0, 70)]))
    vw2.takeoff()
    vw2.pose.z = 140
    with pytest.raises(Exception, match="came down on table"):
        vw2.move("down", 100)


def test_explore_top_height_in_plans_and_commands():
    from codrone_tui.bot.procedure import validate_plan
    st = Step.from_dict({"op": "explore", "seconds": 20, "z_cm": 130})
    assert st.line() == "explore 20 130"
    assert not validate_plan([Step("takeoff"), st, Step("land")], FIELD, BotSettings())[0]
    bad = Step.from_dict({"op": "explore", "seconds": 20, "z_cm": 400})
    assert validate_plan([Step("takeoff"), bad, Step("land")], FIELD, BotSettings())[0]
    assert "different heights" in system_prompt(FIELD, BotSettings())
