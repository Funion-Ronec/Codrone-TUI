"""Bot navigation: sensor readings, the occupancy grid / A*, the box world, the navigator and BotContext."""

import math

import pytest

from codrone_tui.adc.field import Element, FieldConfig, default_field
from codrone_tui.adc.rules import Scorecard
from codrone_tui.backend import Aborted, Cap, Direction, MockBackend
from codrone_tui.bot import (
    CLEAR,
    HIT,
    UNKNOWN,
    BotContext,
    BotSettings,
    Blocked,
    NavError,
    Navigator,
    SensorError,
    World,
    WorldMock,
    bearing,
    box,
    element_boxes,
    field_world,
    read_front,
)
from codrone_tui.bot.grid import OccupancyGrid
from codrone_tui.bot.sensing import classify


class Exe:
    """Instant executor: check() raises once stop() was called."""

    def __init__(self):
        self.stopped = False
        self.slept = 0.0

    def check(self):
        if self.stopped:
            raise Aborted()

    def sleep(self, seconds):
        self.slept += seconds
        self.check()


def settings(**kw):
    return BotSettings(settle_s=0.0, **kw)


def flying(world=None, far=None, **kw):
    m = WorldMock("edu", world or World(), time_scale=0.0, far_reading=far, **kw)
    m.connect()
    m.takeoff()
    return m


WALL_200 = World([box("wall", 205, 0, 10, 400)])  # front face at x = 200


# sensing --------------------------------------------------------------------------------------------------

def test_classify_hit_clear_unknown():
    s = settings()
    assert classify([None, None], s).kind == UNKNOWN
    assert classify([None, -1], settings(none_is_clear=True)).kind == CLEAR
    assert classify([800.0, 810.0, 820.0], s).kind == CLEAR
    r = classify([50.0, 500.0, 52.0, None], s)  # median of the valid samples
    assert r.kind == HIT and r.cm == 52.0


def test_read_front_raises_when_every_read_fails():
    class Broken(MockBackend):
        def read_telemetry(self):
            raise RuntimeError("serial gone")

    b = Broken()
    b.connect()
    with pytest.raises(SensorError):
        read_front(b, settings())


# grid / A* ------------------------------------------------------------------------------------------------

def zones(g):
    return dict(hit_hard=10, hit_soft=30, static_hard=10, static_soft=20)


def test_route_is_straight_in_open_space():
    g = OccupancyGrid(300, 10)
    assert g.route((0, 0), (100, 0), **zones(g)) == [(100.0, 0.0)]


def test_route_goes_around_sensed_obstacles():
    g = OccupancyGrid(300, 10)
    for y in range(-60, 61, 10):  # a wall across x = 100
        g.hits[g.cell_of(100, y)] = 4
    route = g.route((0, 0), (200, 0), **zones(g))
    assert route is not None and route[-1] == (200.0, 0.0) and len(route) >= 2
    hard, _ = g.zones(**zones(g))
    pts = [(0.0, 0.0)] + route
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        for k in range(51):
            t = k / 50
            assert g.cell_of(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t) not in hard


def test_route_is_none_when_goal_is_inside_an_obstacle():
    g = OccupancyGrid(300, 10)
    g.hits[g.cell_of(100, 0)] = 4
    assert g.route((0, 0), (100, 0), **zones(g)) is None


def test_clear_readings_remove_a_stale_hit():
    g = OccupancyGrid(300, 10)
    g.mark(0, 0, 0, HIT, 80, 150, 0)
    assert g.cell_of(80, 0) in g.hits
    for _ in range(3):
        g.mark(0, 0, 0, CLEAR, 150, 150, 0)
    assert g.cell_of(80, 0) not in g.hits


def test_bearing_matches_the_field_frame():
    assert bearing(0, 0, 100, 0) == pytest.approx(0)
    assert bearing(0, 0, 0, -100) == pytest.approx(90)  # -y is to the right
    assert bearing(0, 0, 0, 100) == pytest.approx(270)


# world ----------------------------------------------------------------------------------------------------

def test_world_raycast_and_crash():
    m = flying(WALL_200, far=None)
    assert m.read_telemetry().front_range_cm is None  # 200 cm is past the 150 cm sensor
    m.move(Direction.FORWARD, 100)
    assert m.read_telemetry().front_range_cm == pytest.approx(100, abs=0.5)
    m.turn(90)
    assert m.read_telemetry().front_range_cm is None
    m.turn(-90)
    with pytest.raises(Exception, match="CRASH"):
        m.move(Direction.FORWARD, 150)
    assert m.crashed == "wall" and not m.flying and m.pose.x < 200


def test_field_elements_become_boxes():
    f = default_field()
    legs = element_boxes(f.get("red_arch"))
    assert len(legs) == 2 and all(abs((b.y0 + b.y1) / 2) > 70 for b in legs)
    assert element_boxes(f.get("landing_pad")) == [] and element_boxes(f.get("mini_1")) == []
    assert len(field_world(f).boxes) > 10


# navigator ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("far", [None, 819.0])
def test_forward_stops_a_margin_before_the_wall(far):
    m = flying(WALL_200, far=far)
    nav = Navigator(m, Exe(), settings=settings())
    with pytest.raises(Blocked) as exc:
        nav.forward(500)
    assert m.crashed is None
    assert m.pose.x == pytest.approx(170, abs=1)  # 200 - 30 cm margin
    assert exc.value.moved_cm == pytest.approx(170, abs=1)


def test_forward_flies_the_whole_way_when_clear():
    m = flying(World(), far=819.0)
    assert Navigator(m, Exe(), settings=settings()).forward(250) == pytest.approx(250)
    assert m.pose.x == pytest.approx(250)


def test_forward_respects_the_geofence():
    m = flying(World(), far=819.0)
    with pytest.raises(NavError, match="geofence"):
        Navigator(m, Exe(), settings=settings(geofence_cm=100)).forward(300)
    assert m.pose.x <= 100


def test_forward_can_be_aborted():
    m = flying(World(), far=819.0)
    exe = Exe()
    exe.stopped = True
    with pytest.raises(Aborted):
        Navigator(m, exe, settings=settings()).forward(100)
    assert m.pose.x == 0


def test_approach_stops_at_the_requested_distance():
    m = flying(WALL_200)
    r = Navigator(m, Exe(), settings=settings()).approach(40)
    assert r.kind == HIT and r.cm == pytest.approx(40, abs=5)
    assert m.crashed is None


def test_approach_gives_up_after_max_distance():
    m = flying(World(), far=819.0)
    r = Navigator(m, Exe(), settings=settings()).approach(40, max_cm=120)
    assert r.kind == CLEAR and m.pose.x == pytest.approx(120)


def test_approach_never_backs_up_blind():
    m = flying(World([box("box", 120, 0, 10, 200)]))
    m.pose.x = 100  # only 15 cm from the box, without having flown there
    Navigator(m, Exe(), settings=settings()).approach(40)
    assert m.pose.x == 100


def test_scan_measures_every_angle_and_maps_hits():
    m = flying(World([box("wall", 105, 0, 10, 40)]))
    nav = Navigator(m, Exe(), settings=settings())
    points = nav.scan()
    assert [p.offset for p in points] == [-90, -45, 0, 45, 90]
    ahead = next(p for p in points if p.offset == 0)
    assert ahead.reading.kind == HIT and ahead.reading.cm == pytest.approx(100, abs=1)
    assert nav.grid.hits


@pytest.mark.parametrize("far", [None, 819.0])
def test_goto_routes_around_an_unseen_crate(far):
    m = flying(World([box("crate", 150, 0, 40, 60)]), far=far)
    nav = Navigator(m, Exe(), settings=settings())
    nav.goto(300, 0)
    assert m.crashed is None
    assert math.hypot(m.pose.x - 300, m.pose.y) <= 10
    assert nav.stats["replans"] >= 1


def test_goto_flies_through_a_wide_gap():
    world = World([box("left", 150, 120, 20, 140), box("right", 150, -120, 20, 140)])  # 100 cm gap at y = 0
    m = flying(world, far=819.0)
    nav = Navigator(m, Exe(), settings=settings())
    nav.goto(300, 0)
    assert m.crashed is None and math.hypot(m.pose.x - 300, m.pose.y) <= 10
    assert nav.stats["replans"] == 0


def test_an_obstacle_seen_at_the_beam_edge_is_remembered():
    # a post 8 cm off the path: the beam's edge catches it from afar, then loses it up close
    m = flying(World([box("post", 150, 8, 4, 4)]), far=819.0)
    with pytest.raises(Blocked):
        Navigator(m, Exe(), settings=settings()).forward(300)
    assert m.crashed is None


@pytest.mark.parametrize("seed", range(20))
def test_goto_never_crashes_in_random_clutter(seed):
    import random

    rng = random.Random(seed)
    boxes = []
    while len(boxes) < 5:
        cx, cy = rng.uniform(80, 320), rng.uniform(-120, 120)
        if math.hypot(cx, cy) < 60 or math.hypot(cx - 400, cy) < 60:
            continue  # keep the start and the goal clear
        boxes.append(box(f"box {len(boxes)}", cx, cy, rng.uniform(15, 50), rng.uniform(15, 50)))
    m = flying(World(boxes), far=rng.choice([None, 819.0]), noise_cm=2.0, seed=seed)
    nav = Navigator(m, Exe(), settings=settings())
    try:
        nav.goto(400, 0)
    except NavError:
        pass  # giving up is allowed; crashing is not
    assert m.crashed is None


def test_goto_uses_field_obstacles_it_cannot_see():
    el = Element("tower", "Tower", 100.0, 0.0, z=120.0, width=40.0, height=40.0, kind="tunnel", vertical=True)
    field = FieldConfig(elements={"tower": el})
    m = flying(field_world(field), far=819.0)
    nav = Navigator(m, Exe(), field=field, settings=settings())
    nav.goto(220, 0)
    assert m.crashed is None and math.hypot(m.pose.x - 220, m.pose.y) <= 10


def test_goto_reports_when_there_is_no_way():
    pen = World([box("n", 200, 60, 140, 10), box("s", 200, -60, 140, 10), box("e", 265, 0, 10, 130),
                 box("w", 135, 0, 10, 130)])
    m = flying(pen, far=819.0)
    with pytest.raises(NavError):
        Navigator(m, Exe(), settings=settings(max_replans=6)).goto(200, 0)
    assert m.crashed is None


def test_goto_refuses_goals_outside_the_geofence_and_ceiling():
    m = flying(World(), far=819.0)
    nav = Navigator(m, Exe(), settings=settings(geofence_cm=300))
    with pytest.raises(NavError, match="geofence"):
        nav.goto(400, 0)
    with pytest.raises(NavError, match="altitude"):
        nav.goto(100, 0, 400)


def test_goto_climbs_first_and_descends_last():
    m = flying(World(), far=819.0)
    nav = Navigator(m, Exe(), settings=settings())
    nav.goto(100, 50, 140)
    assert (round(m.pose.x), round(m.pose.y), round(m.pose.z)) == (100, 50, 140)
    nav.goto(0, 0, 60)
    assert (round(m.pose.x), round(m.pose.y), round(m.pose.z)) == (0, 0, 60)
    moves = [c for c in m.log if c[0] == "move"]
    assert moves[0][1] == "up" and moves[-1][1] == "down"


def test_navigator_needs_a_range_sensor():
    b = MockBackend()
    b.caps = frozenset({Cap.VERTICAL})  # no range sensor
    b.connect()
    b.takeoff()
    with pytest.raises(NavError, match="no range sensor"):
        Navigator(b, Exe(), settings=settings()).forward(50)


# BotContext ----------------------------------------------------------------------------------------------

def test_bot_context_flies_the_field_without_crashing():
    f = default_field()
    m = flying(field_world(f), far=None)
    card = Scorecard()
    ctx = BotContext(m, Exe(), scorecard=card, field=f, log=lambda s: None, settings=settings())
    for name in ("red_arch", "green_keyhole", "large_cube"):
        ctx.through(name)
    ctx.land_on("pad")
    assert m.crashed is None and not m.flying
    assert card.total > 0


def test_bot_context_really_passes_each_element_of_a_lap():
    f = default_field()
    m = flying(field_world(f), far=None)
    ctx = BotContext(m, Exe(), field=f, log=lambda s: None, settings=settings())
    for name in ("red_arch", "green_keyhole", "large_cube", "mini_1", "mini_2", "mini_3", "blue_arch",
                 "yellow_keyhole", "tunnel"):
        el = f.get(name)
        ctx.through(name)
        p = m.pose
        h = math.radians(el.heading)
        along = (p.x - el.x) * math.cos(h) + (p.y - el.y) * -math.sin(h)
        across = (p.x - el.x) * math.sin(h) + (p.y - el.y) * math.cos(h)
        if el.vertical:
            assert abs(p.x - el.x) < 3 and abs(p.y - el.y) < 3 and p.z < el.z - el.height, name
        else:
            assert along > 0 and abs(across) < 3, (name, along, across)
            assert abs(((p.heading - el.heading + 180) % 360) - 180) < 1, name
    assert m.crashed is None


def test_bot_context_forward_is_guarded():
    m = flying(WALL_200)
    ctx = BotContext(m, Exe(), log=lambda s: None, settings=settings())
    with pytest.raises(Blocked):
        ctx.forward(400)
    assert m.crashed is None


def test_bot_context_refuses_to_climb_past_the_ceiling():
    m = flying(World())
    ctx = BotContext(m, Exe(), log=lambda s: None, settings=settings(max_alt_cm=150))
    with pytest.raises(NavError, match="ceiling"):
        ctx.up(100)  # takeoff hover is 100 cm on the mock


def test_bot_context_without_range_flies_like_a_routine():
    b = MockBackend()
    b.caps = frozenset({Cap.VERTICAL})  # no range sensor
    b.connect()
    b.takeoff()
    ctx = BotContext(b, Exe(), log=lambda s: None, settings=settings())
    assert ctx.guard is False
    ctx.forward(50)
    ctx.goto(50, 50)
    assert (round(b.pose.x), round(b.pose.y)) == (50, 50)
