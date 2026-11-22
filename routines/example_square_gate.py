# basic ass example I pulled from reddit

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
    ctx.through("large_cube")
    ctx.land_on("pad")
