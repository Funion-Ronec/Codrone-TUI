"""Example user routine: a square lap that threads the large cube on the way.

Every ``*.py`` file in this folder is imported when the TUI starts, and each ``@routine``
function in it shows up in the Missions list. Files whose name starts with "_" are skipped.
A file with a mistake does not break the others: it appears as "<file> (load error)".

Rules of thumb for routines (they run on the flight thread, not the UI thread):
  * fly only through ``ctx`` so the Esc / abort key can stop you between steps
  * distances are cm, turns are degrees (positive = clockwise / right)
  * ``ctx.credit`` / ``ctx.through`` record points the routine *thinks* it earned; the
    scorecard still lets you undo what the drone actually missed
"""

from codrone_tui.adc.routine import routine


@routine("example: square lap", "Fly a 100 cm square at the takeoff altitude, then land in place.")
def square_lap(ctx):
    ctx.takeoff()
    for corner in range(4):
        ctx.log(f"side {corner + 1} of 4")
        ctx.forward(100)
        ctx.turn(90)
        ctx.hover(0.5)
    ctx.land()


@routine("example: cube then pad", "Fly through the large cube (field.toml) and land on the pad.")
def cube_then_pad(ctx):
    ctx.takeoff()
    ctx.through("large_cube")  # lines up, flies through, claims the 25 points
    ctx.land_on("pad")  # flies over the pad from field.toml and lands, claiming the landing
