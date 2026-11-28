# Plain English -> checked flight plan. The phrase parser answers simple wording instantly and offline; anything
# else goes to a local language model (Ollama) whose reply is forced into the plan schema, then checked,
# repaired once if needed, and simulated. Nothing here flies: `plan run` does that after you've read the plan.

from __future__ import annotations

import json
import time
from typing import Callable

from ..adc.field import FieldConfig
from ..adc.rules import MATCH_SECONDS
from .llm import KeyStore, make_client, model_for
from .ollama import OllamaClient, OllamaError
from .phrases import PhraseError, parse_phrases
from .procedure import (
    GATE_KINDS,
    LANDING_TARGETS,
    OPS,
    Plan,
    dry_run_plan,
    normalize,
    plan_schema,
    steps_from_json,
    validate_plan,
)
from .settings import BotSettings

_EXAMPLES = [
    ("take off, go forward a meter and land",
     {"summary": "Fly 1 m forward and land.", "steps": [
         {"op": "takeoff"}, {"op": "move", "direction": "forward", "distance_cm": 100}, {"op": "land"}],
      "questions": []}),
    ("fly through the red arch, then the green keyhole, then land on the pad",
     {"summary": "Red arch, green keyhole, land on the pad.", "steps": [
         {"op": "takeoff"}, {"op": "through", "target": "red_arch"}, {"op": "through", "target": "green_keyhole"},
         {"op": "land_on", "target": "pad"}], "questions": []}),
    ("do the spiral and finish on the bullseye",
     {"summary": "Mini keyholes 1-3, then the bullseye.", "steps": [
         {"op": "takeoff"}, {"op": "through", "target": "mini_1"}, {"op": "through", "target": "mini_2"},
         {"op": "through", "target": "mini_3"}, {"op": "land_on", "target": "bullseye"}], "questions": []}),
    ("get to the spot 2 m ahead and 1 m to the left without hitting the box in the way, then come back",
     {"summary": "Fly to (200, 100) around obstacles and return.", "steps": [
         {"op": "takeoff"}, {"op": "nav", "x_cm": 200, "y_cm": 100}, {"op": "nav", "x_cm": 0, "y_cm": 0},
         {"op": "land"}], "questions": []}),
    ("creep up to the wall until you're 40 cm from it, look around, turn back and hover for 3 seconds",
     {"summary": "Approach the wall to 40 cm, scan, turn around, hover.", "steps": [
         {"op": "takeoff"}, {"op": "approach", "distance_cm": 40}, {"op": "scan"}, {"op": "turn", "degrees": 180},
         {"op": "hover", "seconds": 3}, {"op": "land"}], "questions": []}),
    ("fly around the space in random directions while avoiding the walls, don't just hover, after 20 seconds find a"
     " good landing spot and land",
     {"summary": "Explore for 20 s, then land somewhere open.", "steps": [
         {"op": "takeoff"}, {"op": "explore", "seconds": 20}, {"op": "land_spot"}], "questions": []}),
    ("have a look around for a bit, then come back and land",
     {"summary": "Explore for about 15 s, return to the start, land.", "steps": [
         {"op": "takeoff"}, {"op": "explore", "seconds": 15}, {"op": "home"}, {"op": "land"}], "questions": []}),
    ("fly over to the kitchen",
     {"summary": "Unclear destination.", "steps": [],
      "questions": ["Where is the kitchen from the takeoff point, in metres forward and left?"]}),
]


def system_prompt(field: FieldConfig | None, settings: BotSettings) -> str:
    gates = [el for el in (field.elements.values() if field is not None else ()) if el.kind in GATE_KINDS]
    lines = [
        "You turn a drone pilot's instructions into guidance for the flight algorithm of a CoDrone EDU quadcopter:",
        "a short list of steps it carries out by itself. Answer with JSON only.",
        "",
        "Read the instructions as intent, not word-for-word commands: pick the steps that do what the pilot means.",
        "The algorithm handles the details while flying: forward flight creeps 5 cm at a time and stops the moment",
        "the sensor sees something; nav routes around obstacles; explore, home and land_spot decide their own moves.",
        "So never break travel into many short moves, and prefer a behaviour when the pilot describes one.",
        "",
        "Frame: centimetres. The drone takes off at x=0, y=0 facing +x. +x is forward, +y is LEFT, +z is up.",
        "Headings are degrees clockwise from +x: 0 = the takeoff direction, 90 = right, 180 = back, 270 = left.",
        f"The drone has one forward-looking range sensor (about {settings.sensor_max_cm:.0f} cm). "
        "It cannot see sideways, behind or above itself.",
        "After takeoff it hovers about 100 cm above the floor; up and down moves are relative to where it is.",
        "",
        "Step types (op, then the fields it uses):",
        *[f"- {op}{' ' + ' '.join(names) if names else ''}: {help_text}" for op, (names, help_text) in OPS.items()],
        "",
    ]
    if gates:
        lines += ["Field elements you can fly through (name: what it is, centre x y, crossing height z, width w):",
                  *[f"- {el.name}: {el.label}, x={el.x:.0f} y={el.y:.0f} z={el.z:.0f}"
                    + (f" w={el.width:.0f}" if el.width > 0 else "") for el in gates],
                  "through lines the drone up and flies through by itself: never put nav steps before or between"
                  " through steps. Fly only the elements the pilot names, in the order named"
                  " (\"the spiral\" = mini_1, mini_2, mini_3).",
                  "Only to end up next to, past or behind an element without flying through it, nav to a point at"
                  " least half its width plus 40 cm from its centre.", ""]
    lines += [
        f"land_on targets: {', '.join(LANDING_TARGETS)} (the bullseye is the centre of the pad).",
        "",
        "Rules:",
        "- Start with takeoff. End with land or land_on unless the pilot says to stay in the air.",
        "- Distances are in cm: 1 m = 100 cm, 1 ft = 30 cm, 1 inch = 2.5 cm.",
        f"- Altitude must stay between {settings.min_alt_cm:.0f} and {settings.max_alt_cm:.0f} cm; "
        f"stay within {settings.geofence_cm:.0f} cm of the takeoff point.",
        "- Prefer move forward and nav for travel: they stop for obstacles. Avoid long sideways or backward moves;"
        " turn and go forward instead.",
        "- 'Around', 'avoid' or 'without hitting' an obstacle: nav to where the pilot wants to end up; obstacles are"
        " handled automatically.",
        "- 'Come back', 'return' or 'go home': home.",
        "- Wandering, roaming, 'fly around', 'look around the room', 'random directions', 'don't just hover': explore,"
        " with seconds from what was said (no time given: 20). If the pilot also wants it to go up and down or fly at"
        f" different heights, add z_cm, the highest it may go (no height given: {settings.max_alt_cm - 10:.0f}).",
        "- 'Find a (good, safe, open) spot and land', 'land somewhere': land_spot. 'Land on the pad': land_on pad.",
        "- approach without a distance (\"close to the wall\", \"until you see something\"): use 40 cm.",
        "- Repeat steps when asked: a square is four times (move forward, turn 90).",
        "- Never flip. If the instruction is unsafe, impossible or unclear, return no steps and ask one short"
        " question in questions.",
        "- summary is one short sentence describing the flight.",
        "",
        "Examples:",
    ]
    for said, plan in _EXAMPLES:
        lines += [f"Pilot: {said}", json.dumps(plan)]
    return "\n".join(lines)


class TextPlanner:
    def __init__(self, settings: BotSettings, client: OllamaClient | None = None, keys: KeyStore | None = None) -> None:
        self.settings = settings
        self.client = client  # tests inject one; otherwise a client is made per request from the settings
        self.keys = keys or KeyStore()

    def _client(self):
        if self.client is not None:
            self.client.model = model_for(self.settings)  # `llm use` can change it between requests
            return self.client
        return make_client(self.settings, self.keys)

    def plan(self, text: str, field: FieldConfig | None, *, use_llm: bool = True, flavor: str = "edu",
             notify: Callable[[str], object] | None = None) -> Plan:
        t0 = time.monotonic()
        plan = Plan(text=text.strip())
        try:
            plan.steps = parse_phrases(plan.text, field)
            plan.source = "phrases"
            plan.summary = plan.text
        except PhraseError as exc:
            if not use_llm or self.settings.llm == "off":
                plan.errors.append(f"{exc}. The language model is off, so use simpler wording or type the commands.")
                plan.seconds = time.monotonic() - t0
                return plan
            if notify is not None:
                notify(f"asking {model_for(self.settings)}"
                       + (" (the first request also loads the model, give it a moment)…" if self.settings.llm == "ollama" else "…"))
            self._ask_model(plan, field, flavor)
        if plan.steps:
            self.finish(plan, field, flavor)
        plan.seconds = time.monotonic() - t0
        return plan

    def finish(self, plan: Plan, field: FieldConfig | None, flavor: str = "edu") -> None:
        plan.steps, notes = normalize(plan.steps, plan.text, field)
        plan.notes += notes
        errors, warnings = validate_plan(plan.steps, field, self.settings)
        plan.errors += [e for e in errors if e not in plan.errors]
        plan.warnings += warnings
        if not plan.errors:
            plan.dry = dry_run_plan(plan.steps, field, self.settings, flavor)
            if plan.dry.error:
                plan.errors.append(f"in the simulation: {plan.dry.error}")
            plan.warnings += [w for w in plan.dry.warnings if w not in plan.warnings]
            if plan.dry.ok and plan.dry.duration_s > MATCH_SECONDS and any(s.op in ("through", "land_on") for s in plan.steps):
                plan.warnings.append(f"about {plan.dry.duration_s:.0f} s of flight: longer than a {MATCH_SECONDS} s ADC match")

    def _ask_model(self, plan: Plan, field: FieldConfig | None, flavor: str = "edu") -> None:
        try:
            client = self._client()
        except OllamaError as exc:
            plan.errors.append(str(exc))
            return
        plan.source = f"{self.settings.llm} {client.model}"
        schema = plan_schema(field)
        messages = [{"role": "system", "content": system_prompt(field, self.settings)},
                    {"role": "user", "content": plan.text}]
        for attempt in (1, 2):
            try:
                data, _meta = client.chat_json(messages, schema)
            except OllamaError as exc:
                plan.errors.append(str(exc))
                return
            steps, problems = steps_from_json(data)
            plan.summary = str(data.get("summary") or "").strip()
            plan.questions = [q for q in (str(q).strip() for q in data.get("questions") or []) if q]
            if not problems and steps:
                trial, _ = normalize(steps, plan.text, field)
                problems, _ = validate_plan(trial, field, self.settings)
                if not problems:
                    # geometry the model can still fix: the ceiling, the geofence, a route that doesn't exist
                    rep = dry_run_plan(trial, field, self.settings, flavor)
                    if rep.error:
                        problems = [f"in a simulation of the plan, {rep.error}"]
            if not problems:
                plan.steps = steps
                return
            if attempt == 1:
                # one repair round: show the model exactly what is wrong
                messages += [{"role": "assistant", "content": json.dumps(data)},
                             {"role": "user", "content": "That plan can't be flown:\n- " + "\n- ".join(problems)
                              + "\nReply with the corrected plan as JSON."}]
                continue
            plan.steps = steps
            plan.errors += problems
