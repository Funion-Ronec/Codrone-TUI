# Developer Notes

Implementation details for working on Codrone-TUI. See `documentation.txt` for how to use the app.

## Coordinates and units

- Field frame: the origin is the takeoff point, `+x` is forward, `+y` is left, and `+z` is up. Field distances are in cm.
- Heading is in degrees and clockwise-positive. The map arrows in `_ARROWS` (`widgets/panels.py`) start at heading 0 and step 45 degrees clockwise, with `+x` drawn to the right.
- `Pose.apply_move` uses a forward unit vector of `(cos h, -sin h)` and a left unit vector that is forward rotated 90 degrees counter-clockwise.
- `Telemetry`: `battery` is a percentage, `flight_state` is a value such as `Ready`, `TakeOff`, `Flight`, or `Landing`, `pos_cm` is `(x, y, z)`, and `angles_deg` is `(roll, pitch, yaw)`.
- `Telemetry.pos_is_estimate` is true when `pos_cm` is dead-reckoned from commands rather than read back.
- `DroneBackend.flying` is best-effort and is set by `takeoff()` and `land()`.

## Backend capabilities

| Capability | Meaning |
|---|---|
| `TELEMETRY` | Battery, flight state, attitude, and similar readings |
| `POSITION` | Real position read-back, not dead reckoning |
| `RANGE` | Front and bottom range sensors |
| `VERTICAL` | Up and down moves by distance |
| `SHAPES` | Built-in shapes such as square, circle, and spiral |
| `GRAB` | Simulator gripper |
| `TELEPORT` | Simulator reset and teleport tools |
| `ESTOP` | True motor cut |
| `COLOR` | Colour sensors |

`DroneBackend.kind` is `"edu"`, `"sim"`, or `"mock"`.

## EDU backend

- `CDE_CONTROLLER_VID` is the USB vendor ID the vendor library looks for.
- The library silently refuses flips below `MIN_FLIP_BATTERY`.
- `DEFAULT_HOVER_CM` is the assumed hover height when the range sensor gives no reading. A sensor error must not fail a takeoff.
- `_lib` holds the `codrone_edu.drone` module, which is where the enums live.
- `prewarm_edu_lib()` runs the 1.7 s vendor import in the background while the UI comes up.
- The serial lock wraps `drone.transfer` because `sendRequest` and `sendControl` both route through it.
- Telemetry fires every request first, gives the receiver thread one short window, then reads the cached arrays. That takes about 60 ms per snapshot, compared with about 10 ms per sensor in series.
- Range sensors report `-1000` when they have no reading, and `mm_to_cm` converts that to `None`.
- Takeoff resets `x` and `y` because the frame origin is the takeoff point.

## Simulator backend

- The vendor class hard-codes `localhost:5000`, so other addresses replace its socket.
- A bare port such as `5001` means localhost on that port.
- Late replies from earlier timeouts are dropped before each query.
- `cm_per_s` is the estimated speed at `speed=1`, and is only used to pace sleeps. `vertical_cm_per_s` is the estimated climb rate at throttle 50.
- Vendor `takeoff()` sleeps 1 s itself. `go()` blocks for `seconds`, and turns sleep their own timeout.
- `land()` returns immediately, so the backend waits 1.5 s for the descent.
- Hover height comes from the downward range query, with `DEFAULT_HOVER_CM` as the fallback.
- `flight_state` comes from the backend's own `flying` flag, not from the simulator.

## Mock backend

- It mirrors `SimBackend.caps`, because the real simulator climbs using timed throttle bursts.
- The pretend wall in front of the start point provides the range sensor's distance.
- `log` records every command received, for test assertions.
- `fail_next` can be set to a command name to make that command raise once.
- Manual `go` movement is crude: full stick equals `speed_cm_s`, positive pitch is forward, and positive roll is right.

## PILOT mode

The code is split across three files:

- `pilot.py` holds the keymap, held-key detection and `PilotController`. It doesn't depend on the UI.
- `pilot_ui.py` connects that to the app: key handling, the `pilot` command, rebinding and the HUD refresh.
- `widgets/pilot.py` renders the HUD.

How it works:

- PILOT mode is on exactly while `PilotPad` has focus. Clicking elsewhere, opening the prompt or showing a modal blurs the pad, which stops the sticks.
- The pad consumes only keys bound in `KeyMap`. Unbound keys bubble up to the normal app bindings.
- The toggle key is handled in `CodroneApp.on_key` rather than a `Binding`, so it can be rebound. It can't be bound to a key that a normal-mode binding already uses (`PilotUI.blocked_keys`).
- `PilotController` runs on its own `pilot-sticks` thread at `rate_hz`, capped at `DroneBackend.stick_hz` (20 on EDU and mock, 10 on the simulator). Sticks are only sent while the drone is connected and flying, the executor is idle and, with the Windows reader, the window is focused. When sticks go back to zero it sends `ZERO_PACKETS` zero packets, then nothing.
- One-shot keys (takeoff, land, flips, F-key commands) go through `Shell.run_line`, so they are queued, logged and appear in history. They are debounced, so auto-repeat can't queue the same action several times.
- There is a small race: a stick packet can go out just as a job starts on the executor. A few zero packets may overlap a takeoff or land.

### Held keys

- `Win32Keys` reads `GetAsyncKeyState`, and only counts keys while the foreground window is the console window, its root owner, or a window belonging to one of our ancestor processes (the terminal or IDE hosting us). Textual's `app_focus` must also be true.
- This hasn't been checked in every terminal. If a host isn't recognised, the HUD shows NO FOCUS, and `[pilot] hold = "repeat"` is the workaround.
- `RepeatKeys` is the fallback on other platforms, in headless tests, and for keys `_VK` doesn't know. A key counts as held for `FIRST_HOLD_S` (0.55 s) after its first press, which covers the OS auto-repeat delay, then for `REPEAT_HOLD_S` (0.15 s) after each repeat. That assumes a key repeat rate of at least about 7 per second.

### Stick conventions and estimates

- `DroneBackend.stick` uses the project convention: positive roll is right, positive pitch is forward, positive yaw is clockwise and positive throttle is up. `go` keeps the vendor's native directions.
- On EDU, vendor yaw is positive to the left (`turn_left` passes a positive value to `turn_direction`), so `EduBackend.stick` negates yaw.
- The simulator's yaw sign hasn't been checked in the Unity simulator. `SIM_YAW_SIGN = -1` assumes it matches the EDU API. If J and L turn the wrong way in the simulator, flip that constant.
- Each simulator stick is a `go` command lasting one tick, so the simulator should stop by itself if packets stop. That hasn't been checked either.
- `STICK_CM_S` and `STICK_DEG_S` (EDU), `cm_per_s` (simulator) and the mock's values only feed the dead-reckoned pose. They are guesses.
- `SPEEDS` maps the five speed levels to stick power: 15, 25, 40, 60 and 80.

### Keymap file

`pilot_keys.toml` has a `[keys]` table mapping each action to a key (`""` means unbound) and a `[commands]` table mapping a key to a command line. Loading starts from `KeyMap.defaults(kind)` and applies the file on top. A `[commands]` table in the file replaces the default F-key commands entirely. Bad entries are skipped and reported on startup.

## Commands and scripts

- `Host.out` kinds are `info`, `ok`, `warn`, `error`, `dim`, and `cmd`.
- `Command.requires` is the capability the backend must have. `needs_link` refuses the command when not connected. `flight` is informational and means the handler queues work on the executor.
- `land` queues in order like any other command. `land -e` is the urgent form: it drops everything planned, stops a running mission between steps, then lands.
- A script runs as one queued job, and each line executes in place. This keeps instant commands such as `echo` and `status` in order with flight commands. `stop` and `land` cancel the rest of the script.
- `LOG_DIR` is absolute, so logs land in the project wherever the app was launched from.
- `Shell.log_enabled` gates the command log. `CodroneApp` sets it from `command_log` (`--no-log` passes `False`) and otherwise from `[log] commands`. The `log` command flips it at runtime. The log file is only created on the first write, so a session with logging off leaves nothing on disk. A line is written before its command runs, so `log off` itself is logged and `log on` isn't.
- Only lines run with `record=True` are logged, which covers typed commands, sidebar picks and PILOT one-shot keys.
- If the log can't be written, for example in a read-only folder, the app keeps running and stops trying.
- Handler errors are caught so that a typo can't crash the console.
- Job states are `queued`, `running`, `done`, `failed`, and `cancelled`.

## App and UI

- `styles.tcss` uses rovr-style bordered panels arranged like btop's boxes.
- `PANELS` maps a panel name to `(widget id, hotkey)`.
- `_MARKER` in `adc_ui.py` maps a field element name to `(glyph, colour role)`.
- The accessor is called `console_panel` because `App.console` is Textual's Rich console.
- Vendor libraries print a lot, so `print` output is captured.
- Workers may report back after the app has quit, so cross-thread calls check `is_running`.
- Mission features are optional, so the flight console still starts if they fail to load. `app.adc` is set by `adc_ui.attach()`, and the score panel only exists after that.
- A draw error must not kill the telemetry sampler's callback.
- Sidebar commands that take no arguments run on click. `prefill` puts a command in the input instead of running it.
- The console's `RichLog` sets `min_width` because the default of 78 adds a horizontal scrollbar and clips lines in narrower panels.
- Redraws are skipped when nothing visible has changed: most status ticks only change the uptime, the score panel updates 4 times a second, and telemetry arrives 5 times a second.
- The map fits into `(w-2) x (h-2)` so rounding to a cell can't push a point onto or past the widget edge. Rows are about twice as tall as columns are wide.
- Consecutive trail points are connected, so a dead-reckoned jump reads as a path.
- Braille graph cells are 2 columns by 4 rows, and dot bits are listed from the bottom row up.
- Graph colours fall back to `_FALLBACK` without caching, because the app may not be ready yet.
- `invert` marks graphs where low values are bad, such as battery and distance. `flat` uses one colour instead of a gradient.
- "Good when high" meters such as battery take one colour from their level. Load-style meters sweep from green to red along the bar, like btop.

## ADC scoring and match rules

- `rules.py` follows the manual: `MATCH_SECONDS` is the 3-minute match and `MAX_ATTEMPTS` is the limit of three attempts (section 4).
- `TASKS` follows the order of the manual's scoring table (4.2).
- Landing scores once per match, whichever target it is (4.2).
- Solo Piloting flight path data (3.1.8-3.1.11) is kept as a reference for route templates. `yellow_loop` is the piloting-only loop over the yellow keyhole (3.1.7), not a fly-through.
- Each spiral bonus needs its own `mini_3`, and `mini_3` is capped at 2, so the bonus cap of 2 can't be reached first.
- `PilotingPhase.points` is awarded each time the whole phase is completed in order.
- `CreditEvent.source` is `manual`, `routine`, or `AUTO_SOURCE`, which marks events the scorecard awards by itself. `CreditEvent.t` is seconds since the match or scorecard started.
- `CreditResult.points` is negative for an undo or landing removal. `CreditResult.auto` lists keys awarded automatically, such as the spiral bonus.
- `AttemptResult.duration` is capped at `MATCH_SECONDS`. `expired` is true when the 180 s clock ended the attempt.
- `app.adc_store` is where attempt history is kept. The default is `logs/adc_attempts.json`.

## ADC field

The solo field (manual Figure 3.2.2) is an L: two 96" x 42" mats end to end along the long `x` leg, then one more mat turning right (`-y`).

- Element sizes come from manual section 1.5.2, converted from inches using `IN`: arch opening 60" x 63", keyhole inner diameter 24", large cube 17" x 20", pad 30", bullseye 10", and tunnel 23.5" x 19".
- `x` and `y` are estimates read off the figure, accurate to about 10 cm, before the manual's own 3 in field-to-field variance.
- The manual doesn't give crossing heights, so `z` is a guess.
- `_PLACEHOLDER` columns are `name, label, kind, x, y, z, heading, width, height, vertical`.
- A `width` of 0 means unknown or not measured. `vertical` means the element scores by flying down through it, as with the tunnel (manual Figure 3.1.5).
- `_s` uses `json.dumps` because a JSON string is a valid TOML basic string.

## ADC routines

| Constant | Meaning |
|---|---|
| `APPROACH_CM` | How far before an element `through` lines up |
| `PAST_CM` | How far beyond a thin gate the pass ends, so the whole drone is clear |
| `CUBE_MARGIN_CM` | How far beyond a cube's far wall the pass ends; cubes are as deep as they are wide |
| `MIN_RUNUP_CM` | Closer than this to an element's far side, the drone backs up for a run-up |
| `CLEARANCE_CM` | How far above a vertical element the drone lines up |
| `MIN_MOVE_CM` | Smaller corrections are skipped because dead reckoning isn't that precise |

- `land_on(target)` resolves to the field element to fly over and the scorecard landing key.
- `goto` climbs first, so the drone is high enough to cross the field, and descends last.
- `through` only has to clear the element's depth: about 35 cm for a thin gate, or half the width plus a margin for a cube. Overshooting further just means backing up for the next element.
- A vertical `through` climbs, slides over the top, then descends past the bottom opening.
- `ctx.rejected` lists credits the scorecard refused because the task was already maxed out.
- `Routine.source` is the file a routine was loaded from, or `""` for built-ins. `error` is set on placeholder entries for files that failed to load.
- `LAP_MARGIN_S` is the time kept in hand after a lap to fly back to the start lane and land on the bullseye.
- Every gate in a lap is capped at 2 passes.
- In `routines/example_square_gate.py`, `through` lines up, flies through, and claims the 25 points. `land_on` flies over the pad from `field.toml`, lands, and claims the landing.

### Dry runs

- `DRY_TAKEOFF_S` is an optimistic planning figure, since a real EDU takeoff blocks for about 8 s.
- `DRY_LIMIT_S` is how many virtual seconds pass before a dry run gives up on a routine that never ends.
- Every `ctx` action calls `check()`, so `DRY_MAX_ACTIONS` also bounds loops that never advance virtual time.

## Preflight

- `manual_ref` is the manual section, or `""` for a TUI sanity check rather than a rule.
- `auto` is true when an item is verified from telemetry, and false when a person must confirm it.
- `status` is `pass`, `fail`, or `todo`.
