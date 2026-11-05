import time
from pathlib import Path

import pytest

from codrone_tui.adc import (
    MATCH_SECONDS,
    FieldConfig,
    MatchController,
    Routine,
    RoutineContext,
    RoutineError,
    Scorecard,
    builtin_routines,
    default_field,
    dry_run,
    load_routines,
    max_score,
)
from codrone_tui.adc.routine import routine
from codrone_tui.backend import Aborted, Cap, Direction, FlightExecutor, MockBackend

BUILTIN = {r.name: r for r in builtin_routines()}


@pytest.fixture
def flying_ctx():
    backend = MockBackend("edu", time_scale=0.0)
    backend.connect()
    ex = FlightExecutor()
    lines = []
    ctx = RoutineContext(backend, ex, field=default_field(), log=lines.append)
    ctx.lines = lines
    yield ctx
    ex.shutdown()


# ------------------------------------------------------------------ RoutineContext
def test_context_actions_move_the_backend_and_log(flying_ctx):
    ctx = flying_ctx
    ctx.takeoff()
    ctx.forward(100)
    ctx.right(40)
    ctx.turn(90)
    ctx.hover(0.1)
    ctx.land()
    names = [entry[0] for entry in ctx.backend.log]
    assert names == ["connect", "takeoff", "move", "move", "turn", "hover", "land"]
    assert ctx.scorecard.counts["takeoff"] == 1
    assert any("forward 100 cm" in line for line in ctx.lines)


def test_negative_distance_flips_direction(flying_ctx):
    flying_ctx.takeoff()
    flying_ctx.forward(-50)
    assert ("move", "backward", 50, None) in flying_ctx.backend.log


def test_goto_uses_pose_and_heading(flying_ctx):
    ctx = flying_ctx
    ctx.takeoff(credit=False)
    ctx.goto(200, 100, 150)
    p = ctx.backend.pose
    assert (round(p.x), round(p.y), round(p.z)) == (200, 100, 150)
    ctx.turn(90)  # facing right: field +y is now the drone's backward
    ctx.goto(200, 0)
    assert (round(p.x), round(p.y)) == (200, 0)
    assert ("move", "backward", pytest.approx(100), None) in ctx.backend.log or any(
        e[:2] == ("move", "forward") for e in ctx.backend.log
    )


def test_goto_skips_sub_threshold_moves(flying_ctx):
    flying_ctx.takeoff(credit=False)
    before = len(flying_ctx.backend.log)
    flying_ctx.goto(2, 2, 101)
    assert len(flying_ctx.backend.log) == before


def test_goto_skips_altitude_on_backends_without_vertical_moves():
    backend = MockBackend("sim", time_scale=0.0)
    backend.caps = backend.caps - {Cap.VERTICAL}  # a drone that cannot climb by distance
    backend.connect()
    ex = FlightExecutor()
    lines = []
    ctx = RoutineContext(backend, ex, log=lines.append)
    ctx.takeoff(credit=False)
    ctx.goto(100, 0, 200)
    assert backend.pose.z == 100  # unchanged, did not raise
    assert any("altitude change" in line for line in lines)
    assert not backend.has(Cap.VERTICAL)
    ex.shutdown()


def test_face_takes_the_short_way(flying_ctx):
    ctx = flying_ctx
    ctx.takeoff(credit=False)
    ctx.face(270)
    assert ("turn", -90) in ctx.backend.log
    ctx.face(270)  # already there
    assert [e for e in ctx.backend.log if e[0] == "turn"] == [("turn", -90)]


def moves(ctx):
    return [(e[1], e[2]) for e in ctx.backend.log if e[0] == "move"]


def test_tunnel_is_flown_down_through_not_sideways(flying_ctx):
    """Manual fig 3.1.5: the tunnel is entered from the top. A horizontal pass would never score."""
    ctx = flying_ctx
    ctx.takeoff()
    el = ctx.field.get("tunnel")
    assert el.vertical
    res = ctx.through("tunnel")
    p = ctx.backend.pose
    assert res.ok and ctx.scorecard.counts["tunnel"] == 1
    assert (round(p.x), round(p.y)) == (round(el.x), round(el.y))
    top, bottom = el.z, el.z - el.height
    assert p.z < bottom  # ended below the bottom opening
    assert ("down", pytest.approx(ctx.CLEARANCE_CM + el.height + 15.0)) in moves(ctx)
    assert moves(ctx)[-1][0] == "down"  # the last move is the descent through the tube


def test_tunnel_is_skipped_not_faked_without_vertical_moves(flying_ctx):
    ctx = flying_ctx
    ctx.backend.caps = ctx.backend.caps - {Cap.VERTICAL}
    ctx.takeoff()
    assert ctx.through("tunnel") is None
    assert ctx.scorecard.counts["tunnel"] == 0
    assert any("skipped" in line for line in ctx.lines)


def test_goto_climbs_before_sliding_and_descends_after(flying_ctx):
    ctx = flying_ctx
    ctx.takeoff()  # mock hovers at 100 cm
    ctx.goto(200, 0, 160)
    assert [d for d, _ in moves(ctx)] == ["up", "forward"]
    ctx.backend.log.clear()
    ctx.goto(300, 0, 40)
    assert [d for d, _ in moves(ctx)] == ["forward", "down"]


def test_no_backing_up_when_the_next_gate_is_close(flying_ctx):
    """Blue arch and yellow keyhole are ~80 cm apart: after one the drone must not retreat for a run-up."""
    ctx = flying_ctx
    ctx.takeoff()
    ctx.through("blue_arch")
    ctx.backend.log.clear()
    ctx.through("yellow_keyhole")
    assert "backward" not in [d for d, _ in moves(ctx)]
    assert ctx.scorecard.counts["yellow_keyhole"] == 1


def test_backs_up_when_it_is_already_past_the_gate(flying_ctx):
    ctx = flying_ctx
    ctx.takeoff()
    ctx.goto(300, 0)  # beyond the red arch (x=49)
    ctx.backend.log.clear()
    ctx.through("red_arch")  # must go back for a run-up, then pass through
    dirs = [d for d, _ in moves(ctx)]
    assert dirs[0] == "backward" and ctx.scorecard.counts["red_arch"] == 1


def test_cube_pass_clears_the_cube_depth(flying_ctx):
    ctx = flying_ctx
    ctx.takeoff()
    el = ctx.field.get("large_cube")
    ctx.through("large_cube")
    assert ctx.backend.pose.x == pytest.approx(el.x + el.width / 2 + ctx.CUBE_MARGIN_CM, abs=1.0)


def test_through_lines_up_flies_through_and_credits(flying_ctx):
    ctx = flying_ctx
    ctx.takeoff()
    res = ctx.through("red_arch")
    el = ctx.field.get("red_arch")
    p = ctx.backend.pose
    assert res.ok and ctx.scorecard.counts["red_arch"] == 1
    assert (round(p.x), round(p.y)) == (round(el.x + ctx.PAST_CM), round(el.y))  # clear of the gate, no further
    assert ctx.scorecard.events[-1].source == "routine"
    assert ctx.through("large_cube", credit=False) is None
    assert ctx.scorecard.counts["large_cube"] == 0


def test_through_unknown_element_raises_routine_error():
    backend = MockBackend("edu", time_scale=0.0)
    backend.connect()
    ctx = RoutineContext(backend, FlightExecutor(), field=FieldConfig(), log=lambda m: None)
    with pytest.raises(RoutineError, match="no element"):
        ctx.through("red_arch")


def test_rejected_credits_are_remembered(flying_ctx):
    flying_ctx.credit("tunnel")
    flying_ctx.credit("tunnel")
    assert not flying_ctx.credit("tunnel").ok
    assert flying_ctx.rejected == ["tunnel"]


def test_land_on_scores_the_landing_and_validates_target(flying_ctx):
    ctx = flying_ctx
    ctx.takeoff()
    ctx.land_on("bullseye")
    assert ctx.scorecard.landing == "landing_bullseye"
    assert ctx.backend.flying is False
    with pytest.raises(RoutineError):
        ctx.land_on("roof")


def test_abort_stops_the_next_action(flying_ctx):
    ctx = flying_ctx
    ctx.takeoff()
    ctx.executor.abort()
    with pytest.raises(Aborted):
        ctx.forward(100)
    with pytest.raises(Aborted):
        ctx.credit("tunnel")
    assert [e for e in ctx.backend.log if e[0] == "move"] == []


def test_wait_wakes_immediately_on_abort(flying_ctx):
    ex = flying_ctx.executor
    job = ex.submit("wait", lambda: flying_ctx.wait(30))
    time.sleep(0.05)
    started = time.time()
    ex.abort()
    deadline = started + 2
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert job.state == "cancelled" and time.time() - started < 1.5


def test_time_left_uses_clock_without_a_match():
    now = [100.0]
    ctx = RoutineContext(MockBackend(), FlightExecutor(), clock=lambda: now[0], log=lambda m: None)
    assert ctx.time_left() == MATCH_SECONDS
    now[0] += 50
    assert ctx.time_left() == pytest.approx(130)
    now[0] += 500
    assert ctx.time_left() == 0


def test_context_follows_the_match_controller():
    now = [0.0]
    match = MatchController(clock=lambda: now[0])
    card = match.start()
    backend = MockBackend("edu", time_scale=0.0)
    backend.connect()
    ctx = RoutineContext(backend, FlightExecutor(), match=match, log=lambda m: None)
    now[0] += 25
    assert ctx.elapsed() == pytest.approx(25) and ctx.time_left() == pytest.approx(MATCH_SECONDS - 25)
    ctx.credit("takeoff")
    assert ctx.scorecard is card and card.total == 5
    assert card.events[0].t == pytest.approx(25)


# ---------------------------------------------------------------------- dry runs
def test_builtin_routine_names():
    assert set(BUILTIN) == {"hover_test", "takeoff_land", "phase1", "spiral", "phase3", "full_run"}
    assert all(r.description and r.ok for r in BUILTIN.values())


@pytest.mark.parametrize(
    "name, points",
    [("hover_test", 5), ("takeoff_land", 10), ("phase1", 5 + 5 + 10 + 25), ("spiral", 5 + 45 + 10), ("phase3", 5 + 5 + 20 + 15)],
)
def test_builtin_dry_run_scores(name, points):
    report = dry_run(BUILTIN[name], default_field())
    assert report.ok and report.points == points, report.steps
    assert report.duration_s > 0 and report.steps


def test_full_run_reaches_the_theoretical_maximum_inside_the_match():
    report = dry_run(BUILTIN["full_run"], default_field())
    assert report.ok
    assert report.points == max_score() == 290
    assert report.breakdown["spiral_bonus"] == 20 and report.breakdown["landing_bullseye"] == 15
    assert report.duration_s < MATCH_SECONDS
    assert not any("rejected" in w or "already at max" in w for w in report.warnings)


def test_spiral_routine_earns_the_spiral_bonus():
    assert dry_run(BUILTIN["spiral"]).breakdown["spiral_bonus"] == 10


def test_dry_run_warns_when_uncalibrated_and_not_when_calibrated():
    assert any("NOT calibrated" in w for w in dry_run(BUILTIN["phase1"], default_field()).warnings)
    measured = default_field()
    measured.calibrated = True
    assert not any("calibrated" in w for w in dry_run(BUILTIN["phase1"], measured).warnings)


def test_dry_run_warns_about_missing_landing_and_ending_airborne():
    assert any("no landing points" in w for w in dry_run(BUILTIN["phase1"]).warnings)

    @routine("never lands")
    def never_lands(ctx):
        ctx.takeoff()

    assert any("still in the air" in w for w in dry_run(never_lands).warnings)


def test_dry_run_flags_a_plan_longer_than_the_match():
    @routine("slow")
    def slow(ctx):
        ctx.takeoff()
        ctx.hover(100)
        ctx.hover(100)
        ctx.land()

    report = dry_run(slow)
    assert report.duration_s > MATCH_SECONDS
    assert any("match ends" in w for w in report.warnings)


def test_dry_run_reports_credits_that_would_score_nothing():
    @routine("greedy")
    def greedy(ctx):
        ctx.takeoff()
        for _ in range(3):
            ctx.credit("tunnel")
        ctx.land()

    report = dry_run(greedy)
    assert report.points == 5 + 30
    assert any("tunnel" in w and "max" in w for w in report.warnings)


def test_dry_run_catches_errors_and_endless_loops():
    @routine("bad element")
    def bad_element(ctx):
        ctx.takeoff()
        ctx.through("sky_gate")

    report = dry_run(bad_element)
    assert not report.ok and "sky_gate" in report.error and report.points == 5

    @routine("crashes")
    def crashes(ctx):
        raise ValueError("oops")

    assert dry_run(crashes).error == "ValueError: oops"

    @routine("forever")
    def forever(ctx):
        ctx.takeoff()
        while True:
            ctx.hover(1)

    report = dry_run(forever)
    assert report.ok and any("endless loop" in w for w in report.warnings)

    @routine("credit loop")
    def credit_loop(ctx):
        while True:
            ctx.credit("tunnel")

    assert dry_run(credit_loop).ok  # terminates thanks to the action limit


def test_dry_run_loop_until_time_runs_out():
    @routine("until the bell")
    def until_bell(ctx):
        ctx.takeoff()
        while ctx.time_left() > 20:
            ctx.hover(5)
        ctx.land()

    report = dry_run(until_bell)
    assert report.ok and MATCH_SECONDS - 25 <= report.duration_s <= MATCH_SECONDS


def test_dry_run_of_a_broken_loaded_routine_reports_error(tmp_path):
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    entry = load_routines(tmp_path)[0]
    report = dry_run(entry)
    assert not report.ok and "did not load" in report.error


# ------------------------------------------------------------------------ loader
GOOD_ROUTINE = '''
from codrone_tui.adc.routine import routine

@routine("loaded one", "from a file")
def one(ctx):
    ctx.takeoff()
    ctx.land()

@routine("loaded two")
def two(ctx):
    """First docstring line is the description."""
    ctx.takeoff()
'''


def test_loader_collects_routines_in_file_order(tmp_path):
    (tmp_path / "b_good.py").write_text(GOOD_ROUTINE, encoding="utf-8")
    found = load_routines(tmp_path)
    assert [r.name for r in found] == ["loaded one", "loaded two"]
    assert found[0].description == "from a file"
    assert found[1].description == "First docstring line is the description."
    assert all(isinstance(r, Routine) and r.ok and r.source.endswith("b_good.py") for r in found)
    assert dry_run(found[0]).points == 5


def test_loader_reports_broken_files_without_losing_good_ones(tmp_path):
    (tmp_path / "a_syntax.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "b_good.py").write_text(GOOD_ROUTINE, encoding="utf-8")
    (tmp_path / "c_raises.py").write_text("raise RuntimeError('boom at import')\n", encoding="utf-8")
    (tmp_path / "d_exits.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    (tmp_path / "e_missing_import.py").write_text("import not_a_real_module_xyz\n", encoding="utf-8")
    found = load_routines(tmp_path)
    names = [r.name for r in found]
    assert "loaded one" in names and "loaded two" in names
    errors = [r for r in found if not r.ok]
    assert len(errors) == 4
    assert {Path(r.source).stem for r in errors} == {"a_syntax", "c_raises", "d_exits", "e_missing_import"}
    assert any("boom at import" in r.error for r in errors)
    with pytest.raises(RoutineError, match="did not load"):
        errors[0].fn(None)


def test_loader_skips_underscore_files_and_other_extensions(tmp_path):
    (tmp_path / "_private.py").write_text(GOOD_ROUTINE, encoding="utf-8")
    (tmp_path / "notes.txt").write_text(GOOD_ROUTINE, encoding="utf-8")
    assert load_routines(tmp_path) == []


def test_loader_flags_duplicate_names(tmp_path):
    (tmp_path / "a.py").write_text(GOOD_ROUTINE, encoding="utf-8")
    (tmp_path / "b.py").write_text(GOOD_ROUTINE, encoding="utf-8")
    found = load_routines(tmp_path)
    assert [r.name for r in found if r.ok] == ["loaded one", "loaded two"]
    assert sum("duplicate" in (r.error or "") for r in found) == 2


def test_loader_missing_directory_is_empty(tmp_path):
    assert load_routines(tmp_path / "nope") == []


def test_loader_does_not_leak_modules(tmp_path):
    import sys

    (tmp_path / "leak.py").write_text(GOOD_ROUTINE, encoding="utf-8")
    load_routines(tmp_path)
    assert not any(name.startswith("_codrone_routine_") for name in sys.modules)


def test_shipped_example_routines_load_and_dry_run():
    found = load_routines()
    assert found and all(r.ok for r in found), [r.error for r in found]
    for r in found:
        report = dry_run(r)
        assert report.ok and report.points > 0, (r.name, report.error)
    cube = next(r for r in found if r.name == "example: cube then pad")
    assert dry_run(cube).points == 5 + 25 + 5
