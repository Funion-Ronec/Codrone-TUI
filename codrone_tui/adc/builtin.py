# field layout in field.toml.
"""
semgents end by landing in place (no landing points) so each can be test flown on its own;
full_run is the only one that goes for the bullseye. like all of them are only as good as the
field numbers: with the placeholder layout they are a plan to dry-run, not something to fly.
"""

from __future__ import annotations

from .routine import Routine, RoutineContext, routine

LAP_MARGIN_S = 30.0  # time kept in hand after a lap: fly back to the start lane + land on the bullseye


def _phase1(ctx: RoutineContext) -> None:
    # Red arch -> green keyhole -> large cube (manual 3.1.8)
    ctx.through("red_arch")
    ctx.through("green_keyhole")
    ctx.through("large_cube")


def _spiral(ctx: RoutineContext) -> None:
    # Mini keyholes 1 -> 2 -> 3 back to back, which earns the spiral bonus (4.2.2)
    for name in ctx.field.spiral_order:
        ctx.through(name)


def _phase3(ctx: RoutineContext) -> None:
    # Blue arch -> yellow keyhole -> tunnel (3.1.10, autonomous scores a fly-through, not the loop)
    ctx.through("blue_arch")
    ctx.through("yellow_keyhole")
    ctx.through("tunnel")


@routine("hover_test", "Take off, hover 3 s, land in place. The safe first flight (5 pts).", ("test",))
def hover_test(ctx: RoutineContext) -> None:
    ctx.takeoff()
    ctx.hover(3)
    ctx.land()


@routine("takeoff_land", "Take off, hover, fly to the landing pad and land on it (10 pts).", ("test",))
def takeoff_land(ctx: RoutineContext) -> None:
    ctx.takeoff()
    ctx.hover(2)
    ctx.land_on("pad")


@routine("phase1", "Red arch -> green keyhole -> large cube, then land in place.", ("segment",))
def phase1(ctx: RoutineContext) -> None:
    ctx.takeoff()
    _phase1(ctx)
    ctx.land()


@routine("spiral", "Mini keyholes 1 -> 2 -> 3 for the spiral bonus, then land in place.", ("segment",))
def spiral(ctx: RoutineContext) -> None:
    ctx.takeoff()
    _spiral(ctx)
    ctx.land()


@routine("phase3", "Blue arch -> yellow keyhole -> tunnel, then land in place.", ("segment",))
def phase3(ctx: RoutineContext) -> None:
    ctx.takeoff()
    _phase3(ctx)
    ctx.land()


@routine("full_run", "Laps of phase 1 + spiral + phase 3 while points and time remain, then bullseye.", ("match",))
def full_run(ctx: RoutineContext) -> None:
    ctx.takeoff()
    longest_lap = 0.0
    while True:
        began = ctx.elapsed()
        _phase1(ctx)
        _spiral(ctx)
        _phase3(ctx)
        longest_lap = max(longest_lap, ctx.elapsed() - began)
        if ctx.scorecard.remaining("red_arch") <= 0:  # every gate in the lap is capped at 2 passes
            break
        if ctx.time_left() < longest_lap + LAP_MARGIN_S:
            ctx.log("not enough time for another lap, heading for the landing")
            break
    ctx.land_on("bullseye")


BUILTIN: tuple[Routine, ...] = (hover_test, takeoff_land, phase1, spiral, phase3, full_run)
