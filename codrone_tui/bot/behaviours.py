# The flight algorithm's behaviours: things a plan step can ask for that aren't a fixed move. The language model
# only picks them and their numbers (from what the pilot said); how they fly is decided here, from the sensor,
# with the same guard as everything else (forward flight creeps and stops for obstacles).
#   explore    wander for N seconds: fly forward until something is in the way or a leg ends, turn somewhere
#              open, repeat. Keeps inside the geofence by heading back toward the takeoff point near its edge.
#              Given a top height, it also changes altitude between legs. Nothing looks up, so climbs rely on
#              max_alt_cm and field.toml; before coming down it reads the bottom sensor and stays above what's below.
#   home       back to the takeoff point, around obstacles, facing the takeoff direction.
#   land_spot  look around; land if nothing is close, otherwise move away from the nearest thing and look again.

from __future__ import annotations

import math
import random

from ..backend.base import Direction
from .context import BotContext
from .navigator import Blocked, NavError, bearing
from .sensing import HIT

EXPLORE_LEG_CM = (40.0, 120.0)  # each straight leg is a random length in this range
EXPLORE_TURN_DEG = (60.0, 150.0)  # ...then a random turn this far, left or right
EXPLORE_TRIES = 4  # headings tried before turning right round
EXPLORE_OPEN_CM = 40.0  # a heading counts as open with at least this much free room ahead
FENCE_EDGE_CM = 80.0  # this close to the geofence, the next leg points back inward
EXPLORE_LOW_CM = 40.0  # lowest height explore picks (lower is where boxes and chairs are)
EXPLORE_HEIGHT_CHANCE = 0.7  # chance of a new height before each leg, when a top height is given
EXPLORE_HEIGHT_STEP_CM = 25.0  # a new height is at least this far from the current one, so it shows
MIN_HEIGHT_CHANGE_CM = 15.0
BELOW_CLEAR_CM = 30.0  # coming down, stay at least this far above whatever the bottom sensor sees
SPOT_CLEAR_CM = 60.0  # a landing spot needs nothing nearer than this in any direction looked
SPOT_TRIES = 4
SPOT_SHIFT_CM = 40.0
SPOT_ANGLES = (0.0, 90.0, 180.0, 270.0)


def explore(ctx: BotContext, seconds: float, seed: int | None = None, top_cm: float | None = None) -> dict[str, float]:
    nav, s = ctx.nav, ctx.settings
    rng = random.Random(seed)
    clock = ctx._clock  # the routine's clock: real time in flight, virtual time in a dry run
    end = clock() + float(seconds)
    flown = legs = turns = climbs = 0.0
    low = max(EXPLORE_LOW_CM, s.min_alt_cm)
    top = None if top_cm is None else min(float(top_cm), s.max_alt_cm)
    if top is not None and top - low < MIN_HEIGHT_CHANGE_CM:
        top = None  # no room to vary
    ctx.log(f"explore {seconds:g} s" + (f", heights {low:.0f}-{top:.0f} cm" if top is not None else ""))
    while clock() < end:
        ctx.check()
        p = nav.pose
        if math.hypot(p.x, p.y) > s.geofence_cm - FENCE_EDGE_CM:
            nav.face((bearing(p.x, p.y, 0.0, 0.0) + rng.uniform(-30, 30)) % 360.0)  # near the edge: back inward
            turns += 1
        if top is not None and legs and rng.random() < EXPLORE_HEIGHT_CHANCE:
            z = nav.pose.z
            options = [h for h in (rng.uniform(low, top) for _ in range(6)) if abs(h - z) >= EXPLORE_HEIGHT_STEP_CM]
            if options:
                climbs += change_height(ctx, options[0])
        want = rng.uniform(*EXPLORE_LEG_CM)
        try:
            flown += nav.forward(want, corridor=True)
        except Blocked as exc:
            flown += exc.moved_cm
            ctx.log(f"  something ahead after {exc.moved_cm:.0f} cm: turning away")
        except NavError:
            pass  # the geofence: the next leg turns back
        legs += 1
        if clock() >= end:
            break
        heading = nav.pose.heading
        for _ in range(EXPLORE_TRIES):
            off = rng.choice((-1.0, 1.0)) * rng.uniform(*EXPLORE_TURN_DEG)
            nav.face((heading + off) % 360.0)
            turns += 1
            if nav.free_ahead(nav.measure()) >= EXPLORE_OPEN_CM:
                break
        else:
            nav.face((heading + 180.0) % 360.0)
            turns += 1
    ctx.log(f"explored {flown:.0f} cm in {legs:.0f} legs and {turns:.0f} turns"
            + (f", {climbs:.0f} height change(s)" if top is not None else ""))
    return {"flown_cm": flown, "legs": legs, "turns": turns, "height_changes": climbs}


def change_height(ctx: BotContext, target: float) -> int:
    # Climb or descend toward `target`, within the altitude limits. Returns 1 if it moved. Up and down are flown
    # blind except for the bottom sensor, which keeps a descent above whatever is under the drone.
    nav, s = ctx.nav, ctx.settings
    p = nav.pose
    target = max(s.min_alt_cm, min(s.max_alt_cm, target))
    if target < p.z:
        below = _below(ctx)
        if below is not None and below < p.z - 5.0:
            # something under the drone is higher than the floor: never come down closer than BELOW_CLEAR_CM to it
            target = max(target, p.z - below + BELOW_CLEAR_CM)
    delta = target - p.z
    if abs(delta) < MIN_HEIGHT_CHANGE_CM:
        return 0
    try:
        nav._vertical(Direction.UP if delta > 0 else Direction.DOWN, abs(delta), None)
    except NavError as exc:
        ctx.log(f"  height change skipped: {exc}")
        return 0
    ctx.log(f"  {'up' if delta > 0 else 'down'} to {nav.pose.z:.0f} cm")
    return 1


def _below(ctx: BotContext) -> float | None:
    # cm from the drone down to whatever is under it (floor or something on it); None if there's no reading
    try:
        v = ctx.backend.read_telemetry().bottom_range_cm
    except Exception:  # noqa: BLE001 - no reading means we only have the altitude limits
        return None
    return None if v is None or v < 0 else float(v)


def home(ctx: BotContext) -> None:
    ctx.goto(0.0, 0.0)
    ctx.nav.face(0.0)


def land_spot(ctx: BotContext) -> tuple[float, float]:
    nav = ctx.nav
    for attempt in range(1, SPOT_TRIES + 1):
        ctx.check()
        start = nav.pose.heading
        points = nav.scan(center=start, angles=SPOT_ANGLES)
        near = [pt for pt in points if pt.reading.kind == HIT and pt.reading.cm < SPOT_CLEAR_CM]
        if not near:
            break
        closest = min(near, key=lambda pt: pt.reading.cm)
        ctx.log(f"  something {closest.reading.cm:.0f} cm away: moving off to look again ({attempt}/{SPOT_TRIES})")
        away = max(points, key=lambda pt: math.inf if pt.reading.kind != HIT else pt.reading.cm)
        nav.face(away.heading)
        try:
            nav.forward(SPOT_SHIFT_CM, corridor=True)
        except (Blocked, NavError):
            pass
    else:
        ctx.log("no spot with room all round: landing at the most open one found")
    p = nav.pose
    ctx.log(f"landing at ({p.x:.0f}, {p.y:.0f})")
    ctx.land()
    return p.x, p.y
