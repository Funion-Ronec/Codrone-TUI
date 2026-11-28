# A flight plan ("procedure"): a short list of steps of a few kinds, whoever wrote it (the phrase parser or the
# language model). A plan is normalised, checked (validate_plan), simulated (dry_run_plan) and only then flown.

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ..adc.field import FieldConfig
from ..adc.routine import (
    DRY_LAND_S,
    DRY_SPEED_CM_S,
    DRY_TAKEOFF_S,
    DRY_TURN_S_PER_90,
    DryRunReport,
    Routine,
    RoutineContext,
    RoutineError,
)
from ..adc.rules import Scorecard
from ..backend.base import BackendError, Cap
from ..backend.executor import Aborted
from . import behaviours
from .context import BotContext
from .settings import BotSettings
from .world import WorldMock, field_world

DIRECTIONS = ("forward", "backward", "left", "right", "up", "down")
GATE_KINDS = ("arch", "keyhole", "cube", "mini_keyhole", "tunnel")
LANDING_TARGETS = ("pad", "bullseye", "cube")
COLORS: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 200, 0),
    "white": (255, 255, 255), "purple": (160, 0, 255), "cyan": (0, 255, 255), "orange": (255, 100, 0),
    "pink": (255, 60, 150), "off": (0, 0, 0),
}
MAX_STEPS = 40
MAX_MOVE_CM = 500.0
MAX_TOTAL_CM = 3000.0  # horizontal distance of all moves in one plan
MIN_APPROACH_CM = 20.0  # closest an `approach` may get; the guard keeps margin_cm everywhere else
MOVE_OVERHEAD_S = 1.0  # a real EDU move blocks about a second longer than distance / speed
DRY_LIMIT_S = 900.0
DRY_MAX_ACTIONS = 20_000

# op -> (fields it needs, help text). The order here is the order the language model sees.
OPS: dict[str, tuple[tuple[str, ...], str]] = {
    "takeoff": ((), "take off and hover; always the first step"),
    "land": ((), "land where the drone is"),
    "land_on": (("target",), "fly to a landing target and land on it: pad, bullseye or cube"),
    "move": (("direction", "distance_cm"),
             "move forward/backward/left/right/up/down by distance_cm; forward moves stop for obstacles"),
    "turn": (("degrees",), "turn by degrees: positive = clockwise (right), negative = left"),
    "face": (("degrees",), "turn to an absolute heading (0 = the takeoff direction, 90 = right, 270 = left)"),
    "nav": (("x_cm", "y_cm"), "fly to the point x_cm, y_cm (z_cm = altitude, optional), routing around obstacles"),
    "through": (("target",), "fly through a field element"),
    "approach": (("distance_cm",), "fly forward until an obstacle is distance_cm away (20 to 140)"),
    "scan": ((), "turn in place and measure the way ahead"),
    "hover": (("seconds",), "hold position for seconds"),
    "wait": (("seconds",), "pause for seconds"),
    "led": (("color",), "set the LED colour"),
    "beep": ((), "sound the buzzer"),
    # behaviours: the flight algorithm decides the moves itself, from the sensor
    "explore": (("seconds",), "wander the space for seconds: fly forward until something is in the way, turn"
                " somewhere open, repeat (never just hovers); 3-300 s. Optional z_cm: also change height between"
                " legs, anywhere from 40 cm up to z_cm"),
    "home": ((), "fly back to the takeoff point around obstacles and face the takeoff direction"),
    "land_spot": ((), "look around and land somewhere open (moves away from anything close first)"),
}
AIRBORNE_OPS = {"land", "land_on", "move", "turn", "face", "nav", "through", "approach", "scan", "hover",
                "explore", "home", "land_spot"}
LANDING_OPS = ("land", "land_on", "land_spot")
BEHAVIOUR_OPS = ("explore", "home", "land_spot")
_FIELDS = ("direction", "distance_cm", "degrees", "seconds", "x_cm", "y_cm", "z_cm", "target", "color")
_TEXT_FIELDS = {"direction", "target", "color"}
_MOVE_METHOD = {d: d for d in DIRECTIONS}
_OP_ALIASES = {"take_off": "takeoff", "take off": "takeoff", "landon": "land_on", "land on": "land_on",
               "goto": "nav", "go_to": "nav", "navigate": "nav", "rotate": "turn", "yaw": "turn", "pause": "wait",
               "wander": "explore", "roam": "explore", "go_home": "home", "return_home": "home", "return": "home",
               "landspot": "land_spot", "land_open": "land_spot", "land_somewhere": "land_spot"}
_DIRECTION_ALIASES = {"forwards": "forward", "ahead": "forward", "straight": "forward", "back": "backward",
                      "backwards": "backward", "upward": "up", "upwards": "up", "downward": "down", "downwards": "down"}
_LANDING_ALIASES = {"landing pad": "pad", "landing_pad": "pad", "bulls eye": "bullseye", "bull's eye": "bullseye",
                    "bull": "bullseye", "landing_bullseye": "bullseye", "large cube": "cube", "large_cube": "cube",
                    "landing_cube": "cube"}
_STAY_UP = re.compile(r"\b(stay|keep|remain)\s+(in the air|airborne|hovering|flying|up)\b"
                      r"|\b(don'?t|do not|no|never)\s+land\b", re.I)


class PlanError(ValueError):
    pass


@dataclass
class Step:
    op: str
    direction: str | None = None
    distance_cm: float | None = None
    degrees: float | None = None
    seconds: float | None = None
    x_cm: float | None = None
    y_cm: float | None = None
    z_cm: float | None = None
    target: str | None = None
    color: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "Step":
        if not isinstance(data, dict):
            raise PlanError(f"a step must be an object, not {data!r}")
        op = str(data.get("op") or "").strip().lower()
        kw: dict[str, Any] = {}
        for name in _FIELDS:
            v = data.get(name)
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            if name in _TEXT_FIELDS:
                kw[name] = str(v).strip().lower()
                continue
            try:
                number = float(v)
            except (TypeError, ValueError):
                raise PlanError(f"{op or 'step'}: {name} must be a number, not {v!r}") from None
            if not math.isfinite(number):
                raise PlanError(f"{op or 'step'}: {name} must be a finite number")
            kw[name] = number
        return cls(op=op, **kw)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def line(self) -> str:
        # the same step as a command line, so a saved plan can be run with `source`
        op = self.op
        if op in ("takeoff", "land", "scan", "beep", "home"):
            return op
        if op == "land_spot":
            return "landspot"
        if op == "explore":
            return f"explore {_n(self.seconds)}" + (f" {_n(self.z_cm)}" if self.z_cm is not None else "")
        if op == "land_on":
            return f"landon {self.target}"
        if op == "move":
            return f"{'ahead' if self.direction == 'forward' else self.direction} {_n(self.distance_cm)}"
        if op == "turn":
            d = self.degrees or 0.0
            return f"turn {'right' if d >= 0 else 'left'} {_n(abs(d))}"
        if op == "face":
            return f"face {_n(self.degrees)}"
        if op == "nav":
            return f"nav {_n(self.x_cm)} {_n(self.y_cm)}" + (f" {_n(self.z_cm)}" if self.z_cm is not None else "")
        if op in ("through", "approach"):
            return f"{op} {self.target if op == 'through' else _n(self.distance_cm)}"
        if op in ("hover", "wait"):
            return f"{op} {_n(self.seconds)}"
        if op == "led":
            if self.color == "off":
                return "led off"
            r, g, b = COLORS.get(self.color or "", (0, 0, 0))
            return f"led {r} {g} {b}"
        return op or "?"


def _n(v: float | None) -> str:
    return "?" if v is None else f"{round(float(v), 1):g}"


@dataclass
class Plan:
    text: str
    steps: list[Step] = field(default_factory=list)
    summary: str = ""
    source: str = ""  # "phrases" or "ollama <model>"
    notes: list[str] = field(default_factory=list)  # what normalising changed
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)  # the model asking for detail
    dry: DryRunReport | None = None
    seconds: float = 0.0  # time spent planning

    @property
    def runnable(self) -> bool:
        return bool(self.steps) and not self.errors and self.dry is not None and self.dry.ok

    def lines(self) -> list[str]:
        return [s.line() for s in self.steps]


# field element names --------------------------------------------------------------------------------------

def element_phrases(field: FieldConfig) -> dict[str, str]:
    # words people use for each element -> element name
    out: dict[str, str] = {}
    cubes = [n for n, el in field.elements.items() if el.kind == "cube"]
    for name, el in field.elements.items():
        if el.kind not in GATE_KINDS:
            continue
        out[name] = name
        out[name.replace("_", " ")] = name
        label = re.sub(r"\s+", " ", re.sub(r"\(.*?\)", "", el.label.lower()).replace("#", "")).strip()
        out[label] = name
        out[label.replace(" gate", "")] = name
        num = re.search(r"\d+", name)
        if el.kind == "mini_keyhole" and num:
            out[f"mini keyhole {num.group()}"] = name
            out[f"mini {num.group()}"] = name
    if len(cubes) == 1:
        out["cube"] = out["box"] = cubes[0]
    return out


def resolve_element(text: str, field: FieldConfig, strict: bool = False) -> str | None:
    # strict: the words must name the element exactly; otherwise a unique partial match is accepted too
    t = re.sub(r"[^a-z0-9_ ]+", " ", text.lower())
    t = re.sub(r"\b(the|a|an|gate)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    phrases = element_phrases(field)
    if t in phrases:
        return phrases[t]
    if strict:
        return None
    hits = {name for p, name in phrases.items() if p and (p in t or t in p)} if t else set()
    return hits.pop() if len(hits) == 1 else None


# normalising and checking -----------------------------------------------------------------------------------

def fix_step(step: Step, field: FieldConfig | None = None) -> Step:
    # Small, unambiguous fixes (aliases, turn direction words, default durations). Anything else is an error.
    s = replace(step)
    s.op = _OP_ALIASES.get(s.op, s.op)
    if s.direction:
        s.direction = _DIRECTION_ALIASES.get(s.direction, s.direction)
    if s.op == "turn":
        if s.direction in ("left", "right"):
            size = abs(s.degrees) if s.degrees is not None else 90.0
            s.degrees = -size if s.direction == "left" else size
        s.direction = None
    if s.op == "face" and s.degrees is not None:
        s.degrees = s.degrees % 360.0
    if s.target:
        t = s.target.replace("-", " ").strip()
        if s.op == "land_on":
            s.target = _LANDING_ALIASES.get(t, t)
        elif s.op == "through" and field is not None:
            s.target = resolve_element(t, field) or t.replace(" ", "_")
    if s.op in ("hover", "wait") and s.seconds is None:
        s.seconds = 1.0
    if s.op == "approach" and s.distance_cm is None:
        s.distance_cm = 40.0
    return s


def normalize(steps: list[Step], text: str = "", field: FieldConfig | None = None) -> tuple[list[Step], list[str]]:
    # Plans start on the ground: add a takeoff if the first flying step has none, and a landing at the end
    # unless the instructions say to stay up.
    steps = [fix_step(s, field) for s in steps]
    notes: list[str] = []
    first = next((s for s in steps if s.op in AIRBORNE_OPS or s.op == "takeoff"), None)
    if first is not None and first.op != "takeoff":
        steps.insert(0, Step("takeoff"))
        notes.append("added takeoff at the start")
    flying = False
    for s in steps:
        if s.op == "takeoff":
            flying = True
        elif s.op in LANDING_OPS:
            flying = False
    if flying and not _STAY_UP.search(text or ""):
        steps.append(Step("land"))
        notes.append("added land at the end")
    return steps, notes


def validate_plan(steps: list[Step], field: FieldConfig | None, settings: BotSettings) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if len(steps) > MAX_STEPS:
        errors.append(f"too many steps ({len(steps)}; the limit is {MAX_STEPS})")
    gates = {n for n, el in field.elements.items() if el.kind in GATE_KINDS} if field is not None else set()
    flying = False
    total = 0.0
    uses_field = False
    for i, st in enumerate(steps, 1):
        where = f"step {i} ({st.op or '?'})"
        errors += [f"{where}: {m}" for m in _check_step(st, gates, settings)]
        if st.op == "takeoff":
            if flying:
                errors.append(f"{where}: the drone is already flying")
            flying = True
        elif st.op in AIRBORNE_OPS:
            if not flying:
                errors.append(f"{where}: the drone has to take off first")
            if st.op in LANDING_OPS:
                flying = False
        if st.op == "move" and st.direction in ("left", "right", "backward") \
                and (st.distance_cm or 0) > settings.blind_warn_cm:
            warnings.append(f"{where}: {st.direction} {st.distance_cm:g} cm is flown blind (the range sensor only "
                            "looks forward); turning and flying ahead is safer")
        if st.op == "move" and st.direction not in ("up", "down"):
            total += st.distance_cm or 0.0
        if st.op == "nav" and field is not None and st.x_cm is not None and st.y_cm is not None:
            inside = _gate_at(st.x_cm, st.y_cm, field)
            if inside is not None:
                warnings.append(f"{where}: ({st.x_cm:g}, {st.y_cm:g}) is inside {inside}; `through {inside}` flies "
                                "through it, a point beside it stops short")
        uses_field = uses_field or st.op in ("through", "land_on")
    if total > MAX_TOTAL_CM:
        errors.append(f"the moves add up to {total:.0f} cm; the limit for one plan is {MAX_TOTAL_CM:.0f} cm")
    if uses_field and field is not None and not field.calibrated:
        warnings.append("field.toml is not calibrated: element positions are estimates")
    return errors, warnings


def _gate_at(x: float, y: float, field: FieldConfig) -> str | None:
    # the element whose opening (x, y) is in: within its width across, and within its depth along its heading
    for name, el in field.elements.items():
        if el.kind not in GATE_KINDS:
            continue
        h = math.radians(el.heading)
        dx, dy = x - el.x, y - el.y
        along = dx * math.cos(h) - dy * math.sin(h)
        across = dx * math.sin(h) + dy * math.cos(h)
        depth = el.width / 2 if el.kind in ("cube", "tunnel") else 20.0
        if abs(along) <= depth and abs(across) <= max(el.width / 2, 15.0):
            return name
    return None


def _check_step(st: Step, gates: set[str], settings: BotSettings) -> list[str]:
    op = st.op
    if op not in OPS:
        return [f"unknown step type '{op}'" + (" (flips are not allowed in plans)" if op == "flip" else "")]
    missing = [f"missing {name}" for name in OPS[op][0] if getattr(st, name) is None]
    if missing:
        return missing
    out = []
    if op == "move":
        if st.direction not in DIRECTIONS:
            out.append(f"direction must be one of {', '.join(DIRECTIONS)}")
        if not 1 <= st.distance_cm <= MAX_MOVE_CM:
            out.append(f"distance_cm must be 1-{MAX_MOVE_CM:g}")
    elif op == "turn":
        if st.degrees == 0 or abs(st.degrees) > 360:
            out.append("degrees must be between -360 and 360, and not 0")
    elif op == "face":
        if not 0 <= st.degrees < 360:
            out.append("degrees must be 0-359")
    elif op == "nav":
        if math.hypot(st.x_cm, st.y_cm) > settings.geofence_cm:
            out.append(f"({st.x_cm:g}, {st.y_cm:g}) is outside the {settings.geofence_cm:g} cm geofence")
        if st.z_cm is not None and not settings.min_alt_cm <= st.z_cm <= settings.max_alt_cm:
            out.append(f"z_cm must be {settings.min_alt_cm:g}-{settings.max_alt_cm:g}")
    elif op == "through":
        if st.target not in gates:
            out.append(f"unknown element '{st.target}'" + (f" (known: {', '.join(sorted(gates))})" if gates else ""))
    elif op == "land_on":
        if st.target not in LANDING_TARGETS:
            out.append("target must be pad, bullseye or cube")
    elif op == "approach":
        top = settings.sensor_max_cm - 10
        if not MIN_APPROACH_CM <= st.distance_cm <= top:
            out.append(f"distance_cm must be {MIN_APPROACH_CM:g}-{top:g}")
    elif op == "hover":
        if not 0.1 <= st.seconds <= 60:
            out.append("seconds must be 0.1-60")
    elif op == "wait":
        if not 0.1 <= st.seconds <= 300:
            out.append("seconds must be 0.1-300")
    elif op == "explore":
        if not 3 <= st.seconds <= 300:
            out.append("seconds must be 3-300")
        if st.z_cm is not None and not behaviours.EXPLORE_LOW_CM + 15 <= st.z_cm <= settings.max_alt_cm:
            out.append(f"z_cm (the top height) must be {behaviours.EXPLORE_LOW_CM + 15:g}-{settings.max_alt_cm:g}")
    elif op == "led":
        if st.color not in COLORS:
            out.append(f"color must be one of {', '.join(COLORS)}")
    return out


def steps_from_json(data: Any) -> tuple[list[Step], list[str]]:
    raw = data.get("steps") if isinstance(data, dict) else None
    if raw is None:
        return [], ["the reply has no steps list"]
    if not isinstance(raw, list):
        return [], ["steps must be a list"]
    steps, problems = [], []
    for i, item in enumerate(raw, 1):
        try:
            steps.append(Step.from_dict(item))
        except PlanError as exc:
            problems.append(f"step {i}: {exc}")
    return steps, problems


def plan_schema(field: FieldConfig | None) -> dict[str, Any]:
    # JSON schema for the model's reply (Ollama `format`), so it can only produce well-formed plans
    gates = sorted(n for n, el in field.elements.items() if el.kind in GATE_KINDS) if field is not None else []
    targets = gates + [t for t in LANDING_TARGETS if t not in gates]
    step = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": list(OPS)},
            "direction": {"type": "string", "enum": list(DIRECTIONS)},
            "distance_cm": {"type": "number"},
            "degrees": {"type": "number"},
            "seconds": {"type": "number"},
            "x_cm": {"type": "number"},
            "y_cm": {"type": "number"},
            "z_cm": {"type": "number"},
            "target": {"type": "string", "enum": targets},
            "color": {"type": "string", "enum": list(COLORS)},
        },
        "required": ["op"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "steps": {"type": "array", "items": step, "maxItems": MAX_STEPS},
            "questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "steps", "questions"],
        "additionalProperties": False,
    }


# flying a plan ---------------------------------------------------------------------------------------------

def run_step(ctx: RoutineContext, st: Step) -> None:
    op = st.op
    if op in ("nav", "approach", "scan", *BEHAVIOUR_OPS) and not isinstance(ctx, BotContext):
        raise RoutineError(f"'{op}' needs the bot's flight context")
    if op == "takeoff":
        ctx.takeoff()
    elif op == "land":
        ctx.land()
    elif op == "land_on":
        ctx.land_on(st.target)
    elif op == "move":
        getattr(ctx, _MOVE_METHOD[st.direction])(st.distance_cm)
    elif op == "turn":
        ctx.turn(st.degrees)
    elif op == "face":
        ctx.face(st.degrees)
    elif op == "nav":
        ctx.goto(st.x_cm, st.y_cm, st.z_cm)
    elif op == "through":
        ctx.through(st.target)
    elif op == "approach":
        ctx.approach(st.distance_cm)
    elif op == "scan":
        ctx.scan()
    elif op == "hover":
        ctx.hover(st.seconds)
    elif op == "wait":
        ctx.wait(st.seconds)
    elif op == "explore":
        behaviours.explore(ctx, st.seconds, top_cm=st.z_cm)
    elif op == "home":
        behaviours.home(ctx)
    elif op == "land_spot":
        behaviours.land_spot(ctx)
    elif op == "led":
        b = ctx.backend
        if not b.has(Cap.LED):
            ctx.log(f"led {st.color}: skipped, {b.title} has no LED")
            return
        ctx.check()
        if st.color == "off":
            b.led_off()
        else:
            b.set_led(*COLORS[st.color])
        ctx.log(f"led {st.color}")
    elif op == "beep":
        b = ctx.backend
        if not b.has(Cap.BUZZER):
            ctx.log(f"beep: skipped, {b.title} has no buzzer")
            return
        ctx.check()
        b.beep()
    else:
        raise RoutineError(f"unknown step '{op}'")


def compile_routine(plan: Plan) -> Routine:
    steps = list(plan.steps)
    title = plan.summary or plan.text
    name = "plan: " + (title if len(title) <= 40 else title[:39] + "…")

    def fly(ctx: RoutineContext) -> None:
        for i, st in enumerate(steps, 1):
            ctx.check()
            ctx.log(f"step {i}/{len(steps)}: {st.line()}")
            run_step(ctx, st)

    return Routine(name=name, description=plan.text, fn=fly, tags=("bot",))


class _VirtualWorld(WorldMock):
    # the plan's world for a dry run: field.toml elements only, and a clock instead of sleeps

    def __init__(self, flavor: str, field: FieldConfig | None, settings: BotSettings) -> None:
        super().__init__(flavor, field_world(field), time_scale=0.0, speed_cm_s=DRY_SPEED_CM_S,
                         sensor_max_cm=settings.sensor_max_cm)
        self.vt = 0.0
        self.connect()

    def _wait(self, seconds: float) -> None:
        self.vt += max(0.0, seconds)

    def takeoff(self) -> None:
        super().takeoff()
        self.vt += DRY_TAKEOFF_S - 3.0

    def land(self) -> None:
        super().land()
        self.vt += DRY_LAND_S - 2.0

    def turn(self, degrees: float) -> None:
        super().turn(degrees)
        self.vt += abs(degrees) / 90.0 * (DRY_TURN_S_PER_90 - 1.0)

    def move(self, direction, distance_cm: float, speed: float | None = None) -> None:
        super().move(direction, distance_cm, speed)
        self.vt += MOVE_OVERHEAD_S

    def stick_time(self, seconds: float) -> float:
        self.vt += max(0.0, seconds)  # virtual time; nothing to wait for
        return 0.0

    def read_telemetry(self):
        self.vt += 0.06
        return super().read_telemetry()


class _VirtualExecutor:
    def __init__(self, backend: _VirtualWorld) -> None:
        self._backend = backend
        self._calls = 0

    def check(self) -> None:
        self._calls += 1
        if self._backend.vt > DRY_LIMIT_S or self._calls > DRY_MAX_ACTIONS:
            raise Aborted()

    def sleep(self, seconds: float) -> None:
        self._backend.vt += max(0.0, seconds)
        self.check()


def dry_run_plan(steps: list[Step], field: FieldConfig | None, settings: BotSettings, flavor: str = "edu") -> DryRunReport:
    # Fly the plan in a virtual world built from field.toml. It can't know about obstacles that aren't in the
    # file, but it catches geometry mistakes (geofence, ceiling, flying into a known element, no route).
    backend = _VirtualWorld(flavor if flavor in ("edu", "sim") else "edu", field, settings)
    card = Scorecard()
    log: list[str] = []
    ctx = BotContext(backend, _VirtualExecutor(backend), scorecard=card, field=field, log=log.append,
                     clock=lambda: backend.vt, settings=settings, guard=True)
    error: str | None = None
    i = 0
    try:
        for i, st in enumerate(steps, 1):
            log.append(f"{i}. {st.line()}")
            run_step(ctx, st)
    except Aborted:
        error = f"the simulation gave up at step {i} after {backend.vt:.0f} s (does the plan ever end?)"
    except (BackendError, RoutineError, KeyError, ValueError) as exc:
        error = f"step {i} ({steps[i - 1].line()}): {exc}"
    warnings = []
    if error is None and backend.flying:
        warnings.append("the plan ends with the drone in the air")
    return DryRunReport(points=card.total, duration_s=round(backend.vt, 1), steps=log, warnings=warnings,
                        routine="plan", breakdown=card.breakdown(), error=error)
