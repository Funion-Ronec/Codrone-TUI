"""Bot plans: the phrase parser, plan checks and dry runs, and the Ollama planner (against a fake server)."""

import pytest

from codrone_tui.adc.field import default_field
from codrone_tui.bot import (
    BotSettings,
    OllamaClient,
    OllamaError,
    PhraseError,
    PlanError,
    Step,
    TextPlanner,
    dry_run_plan,
    normalize,
    parse_phrases,
    validate_plan,
)
from codrone_tui.bot.procedure import plan_schema, steps_from_json

FIELD = default_field()
S = BotSettings(settle_s=0.0)


def lines(text):
    return [s.line() for s in parse_phrases(text, FIELD)]


# phrase parser ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("take off, fly forward 1 m, turn left and land", ["takeoff", "ahead 100", "turn left 90", "land"]),
    ("Takeoff. Go up 30 cm then hover for 2 seconds; land", ["takeoff", "up 30", "hover 2", "land"]),
    ("fly 50 cm to the right", ["right 50"]),
    ("go to the left 20 cm", ["left 20"]),
    ("move back 2 feet", ["backward 61"]),
    ("go forward half a meter twice", ["ahead 50", "ahead 50"]),
    ("turn 45 degrees to the right", ["turn right 45"]),
    ("turn around", ["turn right 180"]),
    ("rotate -30", ["turn left 30"]),
    ("face 270", ["face 270"]),
    ("climb 20 cm and descend 10 cm", ["up 20", "down 10"]),
    ("go to 200, 50", ["nav 200 50"]),
    ("fly to (120, -40) at 90", ["nav 120 -40 90"]),
    ("come back home", ["nav 0 0"]),
    ("fly through the red arch, then the green keyhole, then the cube", ["through red_arch", "through green_keyhole",
                                                                         "through large_cube"]),
    ("land on the bullseye", ["landon bullseye"]),
    ("go to the landing pad", ["nav 86 0"]),
    ("approach the wall to 40 cm", ["approach 40"]),
    ("fly forward until you are 30 cm from the wall", ["approach 30"]),
    ("keep going until you see an obstacle", ["approach 40"]),
    ("scan", ["scan"]),
    ("set the led to green and beep", ["led 0 255 0", "beep"]),
    ("turn off the lights", ["led off"]),
    ("do a 1 m square", ["ahead 100", "turn right 90"] * 4),
    ("please take off. ok, now land!", ["takeoff", "land"]),
])
def test_phrases(text, expected):
    assert lines(text) == expected


@pytest.mark.parametrize("text", [
    "fly through the keyhole",  # which one?
    "through the red arch, then go around the box",  # needs judgement: model
    "go a bit left",
    "do a flip",
    "dance",
    "",
])
def test_phrases_that_need_the_model(text):
    with pytest.raises(PhraseError):
        parse_phrases(text, FIELD)


# steps, checks, dry runs -------------------------------------------------------------------------------------

def test_step_from_dict_and_back():
    st = Step.from_dict({"op": "move", "direction": "Forward", "distance_cm": "120", "junk": 1})
    assert st == Step("move", direction="forward", distance_cm=120.0)
    assert st.to_dict() == {"op": "move", "direction": "forward", "distance_cm": 120.0}
    with pytest.raises(PlanError):
        Step.from_dict({"op": "move", "distance_cm": "far"})
    with pytest.raises(PlanError):
        Step.from_dict(["move"])


def test_step_lines_are_commands():
    assert Step("move", direction="left", distance_cm=30).line() == "left 30"
    assert Step("turn", degrees=-90).line() == "turn left 90"
    assert Step("nav", x_cm=100, y_cm=-20, z_cm=80).line() == "nav 100 -20 80"
    assert Step("land_on", target="pad").line() == "landon pad"
    assert Step("led", color="red").line() == "led 255 0 0"
    assert Step("wait", seconds=1.5).line() == "wait 1.5"


def test_normalize_adds_takeoff_and_land_but_respects_stay_up():
    steps, notes = normalize([Step("move", direction="forward", distance_cm=50)], "forward 50")
    assert [s.op for s in steps] == ["takeoff", "move", "land"] and len(notes) == 2
    steps, _ = normalize([Step("takeoff"), Step("hover", seconds=3)], "take off and stay in the air")
    assert [s.op for s in steps] == ["takeoff", "hover"]
    steps, _ = normalize([Step("turn", direction="left", degrees=45)])
    assert steps[1].degrees == -45 and steps[1].direction is None


def test_validate_plan_catches_mistakes():
    bad = [Step("move", direction="forward", distance_cm=50), Step("takeoff"), Step("takeoff"),
           Step("through", target="purple_arch"), Step("move", direction="sideways", distance_cm=9999),
           Step("nav", x_cm=5000, y_cm=0), Step("approach", distance_cm=2), Step("flip"), Step("land"),
           Step("hover", seconds=1)]
    errors, _ = validate_plan(bad, FIELD, S)
    text = "\n".join(errors)
    for expect in ("take off first", "already flying", "unknown element 'purple_arch'", "direction must be",
                   "distance_cm must be 1-500", "geofence", "distance_cm must be 20", "flips are not allowed",
                   "step 10 (hover): the drone has to take off first"):
        assert expect in text, expect


def test_validate_plan_warns_about_blind_moves_and_the_uncalibrated_field():
    steps, _ = normalize([Step("move", direction="left", distance_cm=120), Step("through", target="red_arch")])
    errors, warnings = validate_plan(steps, FIELD, S)
    assert not errors
    assert any("flown blind" in w for w in warnings) and any("not calibrated" in w for w in warnings)


def test_dry_run_flies_a_field_plan():
    steps, _ = normalize([Step("through", target="red_arch"), Step("through", target="green_keyhole"),
                          Step("land_on", target="pad")])
    rep = dry_run_plan(steps, FIELD, S)
    assert rep.ok, rep.error
    assert rep.points > 0 and 10 < rep.duration_s < 180


def test_dry_run_flies_a_whole_lap_including_the_tunnel():
    gates = ["red_arch", "green_keyhole", "large_cube", "mini_1", "mini_2", "mini_3", "blue_arch", "yellow_keyhole",
             "tunnel"]
    steps, _ = normalize([Step("through", target=g) for g in gates] + [Step("land_on", target="bullseye")])
    rep = dry_run_plan(steps, FIELD, S)
    assert rep.ok, rep.error
    assert rep.points >= 100


def test_dry_run_refuses_to_line_up_on_top_of_a_keyhole_stand():
    # from the start line, the cube's run-up point is just past the green keyhole, right above its stand
    steps, _ = normalize([Step("through", target="large_cube")])
    rep = dry_run_plan(steps, FIELD, S)
    assert not rep.ok and "come down" in rep.error and "green_keyhole stand" in rep.error
    steps, _ = normalize([Step("through", target="green_keyhole"), Step("through", target="large_cube")])
    assert dry_run_plan(steps, FIELD, S).ok  # the course order lines up past it


def test_nav_into_a_gate_gets_a_warning():
    steps, _ = normalize([Step("nav", x_cm=422, y_cm=-175)])
    _, warnings = validate_plan(steps, FIELD, S)
    assert any("inside yellow_keyhole" in w for w in warnings)
    for x, y in ((0, 0), (200, 100), (306, 0)):  # in front of the red arch, off to the side, past the cube
        _, warnings = validate_plan(normalize([Step("nav", x_cm=x, y_cm=y)])[0], FIELD, S)
        assert not any("inside" in w for w in warnings), (x, y, warnings)


def test_dry_run_catches_a_blind_move_into_a_field_element():
    # 49 cm ahead puts the drone in the red arch; 100 cm to the right flies into its leg
    steps, _ = normalize([Step("move", direction="forward", distance_cm=49),
                          Step("move", direction="right", distance_cm=100)])
    rep = dry_run_plan(steps, FIELD, S)
    assert not rep.ok and "CRASH" in rep.error and "red_arch" in rep.error


def test_dry_run_reports_an_unreachable_goal():
    steps, _ = normalize([Step("nav", x_cm=0, y_cm=0, z_cm=400)])
    rep = dry_run_plan(steps, FIELD, S)
    assert not rep.ok and "altitude" in rep.error


def test_schema_only_allows_known_ops_and_targets():
    schema = plan_schema(FIELD)
    item = schema["properties"]["steps"]["items"]
    assert "flip" not in item["properties"]["op"]["enum"]
    targets = item["properties"]["target"]["enum"]
    assert {"red_arch", "tunnel", "pad", "bullseye", "cube"} <= set(targets)
    assert "landing_pad" not in targets
    assert steps_from_json({"steps": "nope"})[1] and steps_from_json({})[1]


# planner with a fake Ollama ----------------------------------------------------------------------------------

def planner_for(srv, **kw):
    s = BotSettings(settle_s=0.0, ollama_url=srv.url, timeout_s=10, **kw)
    return TextPlanner(s)


GOOD = {"summary": "Around the box to (200, 100) and back.", "questions": [], "steps": [
    {"op": "takeoff"}, {"op": "nav", "x_cm": 200, "y_cm": 100}, {"op": "nav", "x_cm": 0, "y_cm": 0}, {"op": "land"}]}


def test_simple_wording_never_calls_the_model(ollama):
    srv = ollama()
    plan = planner_for(srv).plan("take off, forward 50 cm, land", FIELD)
    assert plan.runnable and plan.source == "phrases" and srv.requests == []


def test_free_text_goes_to_the_model_with_the_schema(ollama):
    srv = ollama(GOOD)
    plan = planner_for(srv).plan("get to the spot 2 m ahead, 1 m left, around the box, and come back", FIELD)
    assert plan.runnable, plan.errors
    assert plan.source == "ollama qwen3.5:9b" and plan.lines() == ["takeoff", "nav 200 100", "nav 0 0", "land"]
    req = srv.requests[0]
    assert req["format"]["required"] == ["summary", "steps", "questions"]
    assert req["stream"] is False and req["think"] is False and req["options"]["temperature"] == 0
    assert "red_arch" in req["messages"][0]["content"] and req["messages"][1]["content"].startswith("get to")


def test_a_bad_reply_gets_one_repair_round(ollama):
    bad = {"summary": "x", "questions": [], "steps": [{"op": "takeoff"}, {"op": "through", "target": "blue_keyhole"}]}
    fixed = {"summary": "Blue arch.", "questions": [], "steps": [{"op": "takeoff"}, {"op": "through", "target": "blue_arch"},
                                                                 {"op": "land"}]}
    srv = ollama(bad, fixed)
    plan = planner_for(srv).plan("go through the blue gate thing", FIELD)
    assert plan.runnable and plan.lines() == ["takeoff", "through blue_arch", "land"]
    assert len(srv.requests) == 2
    assert "blue_keyhole" in srv.requests[1]["messages"][-1]["content"]


def test_a_plan_that_fails_in_simulation_also_gets_repaired(ollama):
    too_high = {"summary": "x", "questions": [], "steps": [{"op": "takeoff"}, {"op": "move", "direction": "up",
                                                                               "distance_cm": 130}]}
    fixed = {"summary": "Climb to the ceiling limit.", "questions": [], "steps": [
        {"op": "takeoff"}, {"op": "move", "direction": "up", "distance_cm": 40}, {"op": "hover", "seconds": 2},
        {"op": "land"}]}
    srv = ollama(too_high, fixed)
    plan = planner_for(srv).plan("fly up to the ceiling", FIELD)
    assert plan.runnable and plan.lines()[1] == "up 40"
    assert "ceiling" in srv.requests[1]["messages"][-1]["content"]


def test_still_bad_after_repair_is_not_flyable(ollama):
    bad = {"summary": "x", "questions": [], "steps": [{"op": "takeoff"}, {"op": "move", "direction": "up", "distance_cm": 900}]}
    srv = ollama(bad, bad)
    plan = planner_for(srv).plan("go really really high", FIELD)
    assert not plan.runnable and any("distance_cm" in e for e in plan.errors)


def test_the_model_can_ask_instead_of_guessing(ollama):
    srv = ollama({"summary": "unclear", "steps": [], "questions": ["Where is the kitchen?"]})
    plan = planner_for(srv).plan("fly over to the kitchen", FIELD)
    assert not plan.runnable and not plan.errors and plan.questions == ["Where is the kitchen?"]


def test_model_not_pulled(ollama):
    srv = ollama((404, "model 'qwen3.5:9b' not found"))
    plan = planner_for(srv).plan("do something clever", FIELD)
    assert not plan.runnable and "ollama pull qwen3.5:9b" in plan.errors[0]


def test_models_without_a_think_switch_are_retried(ollama):
    srv = ollama((400, '"qwen3.5:9b" does not support thinking'), GOOD)
    plan = planner_for(srv).plan("around the box and back", FIELD)
    assert plan.runnable and "think" in srv.requests[0] and "think" not in srv.requests[1]


def test_unreachable_ollama():
    s = BotSettings(settle_s=0.0, ollama_url="http://127.0.0.1:9", timeout_s=3)
    plan = TextPlanner(s).plan("do something clever", FIELD)
    assert not plan.runnable and "cannot reach Ollama" in plan.errors[0]


def test_llm_off_keeps_to_the_phrase_parser(ollama):
    srv = ollama(GOOD)
    plan = planner_for(srv, llm="off").plan("do something clever", FIELD)
    assert not plan.runnable and "language model is off" in plan.errors[0] and srv.requests == []


def test_client_lists_models_and_rejects_non_json(ollama):
    srv = ollama(models=("qwen3.5:9b", "glm4:latest"))
    assert OllamaClient(srv.url).models() == ["glm4:latest", "qwen3.5:9b"]
    srv.replies.append("not an object")
    with pytest.raises(OllamaError, match="not a JSON object"):
        OllamaClient(srv.url).chat_json([], {})
