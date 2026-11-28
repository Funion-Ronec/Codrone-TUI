# Offline parser for simple instructions ("take off, fly forward 1 m, turn left and land"). Anything it can't
# read with certainty goes to the language model instead, so it errs on the side of not matching.

from __future__ import annotations

import re
from typing import Callable

from ..adc.field import FieldConfig
from .procedure import COLORS, Step, resolve_element


class PhraseError(ValueError):
    def __init__(self, unknown: list[str]) -> None:
        super().__init__("could not understand " + ", ".join(f"'{u}'" for u in unknown))
        self.unknown = unknown


_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "half a": 0.5, "half an": 0.5,
    "half": 0.5,
}
NUM = (r"(\d+(?:\.\d+)?|half an?|half|a hundred|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|"
       r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)")
UNIT = r"(centimet(?:er|re)s?|cm|millimet(?:er|re)s?|mm|met(?:er|re)s?|m|inch(?:es)?|in|feet|foot|ft)"
DIR = r"(forwards?|straight ahead|straight|ahead|backwards?|back|left|right|upwards?|up|downwards?|down)"
SECS = r"(?:s|secs?|seconds?)"
DEG = r"(?:deg|degs|degrees?|°)"
COLOR = "(" + "|".join(sorted(COLORS, key=len, reverse=True)) + ")"
GO = r"(?:go|fly|move|head|travel)"
THING = r"(?:wall|obstacle|object|box|something|anything)"

_SPLIT = re.compile(r"\s*(?:[,;!]|\.(?!\d)|\b(?:and then|then|after that|afterwards|next|finally|and)\b)\s*", re.I)
_LEAD = re.compile(r"^(?:please|now|first(?:ly)?|second(?:ly)?|lastly|also|ok(?:ay)?|"
                   r"(?:make|have|let|tell) the drone(?: to)?|the drone should|you should|can you|could you|"
                   r"i want (?:you|it) to)\s+")
_TIMES = re.compile(r"\s+(?:x\s*(\d+)|(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+times|(twice)|(thrice))$")
# keep "go to 200, 50" and "(200, 50)" together: those commas separate coordinates, not instructions
_PAREN_PAIR = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*(?:cm)?\s*,\s*(-?\d+(?:\.\d+)?)\s*(?:cm)?\s*\)")
_TO_PAIR = re.compile(r"(\bto\s+(?:the\s+)?(?:point\s+|position\s+|coordinates?\s+|spot\s+)?\(?\s*(?:x\s*=?\s*)?"
                      r"-?\d+(?:\.\d+)?\s*(?:cm)?)\s*,\s*(?=(?:y\s*=?\s*)?-?\d)", re.I)
_PAIR_Z = re.compile(r"(\bto\s+[^,;]*?-?\d+(?:\.\d+)?\s+(?:y\s*=?\s*)?-?\d+(?:\.\d+)?\s*(?:cm)?\)?)\s*,\s*"
                     r"(?=(?:at|altitude|height|z)\b)", re.I)
_FILLER_ONLY = {"first", "firstly", "then", "next", "now", "please", "ok", "okay", "finally", "lastly", "also"}
_DIRECTION = {"forward": "forward", "forwards": "forward", "ahead": "forward", "straight": "forward",
              "straight ahead": "forward", "back": "backward", "backward": "backward", "backwards": "backward",
              "up": "up", "upward": "up", "upwards": "up", "down": "down", "downward": "down", "downwards": "down",
              "left": "left", "right": "right"}
_LANDING = {"pad": "pad", "landing pad": "pad", "bullseye": "bullseye", "bulls eye": "bullseye",
            "bull's eye": "bullseye", "centre": "bullseye", "center": "bullseye", "centre of the pad": "bullseye",
            "center of the pad": "bullseye", "cube": "cube", "large cube": "cube", "box": "cube"}


def _num(tok: str) -> float:
    tok = tok.strip().lower()
    return float(_WORDS[tok]) if tok in _WORDS else float(tok)


def _cm(value: float, unit: str | None) -> float:
    u = (unit or "cm").lower()
    if u == "cm" or u.startswith("centi"):
        f = 1.0
    elif u == "mm" or u.startswith("milli"):
        f = 0.1
    elif u == "m" or u.startswith("met"):
        f = 100.0
    elif u == "in" or u.startswith("inch"):
        f = 2.54
    else:
        f = 30.48
    return round(value * f, 1)


def _move(direction: str, value: str, unit: str | None) -> list[Step]:
    return [Step("move", direction=_DIRECTION[direction], distance_cm=_cm(_num(value), unit))]


def _turn(side: str, value: str | None) -> list[Step]:
    deg = _num(value) if value else 90.0
    return [Step("turn", degrees=-deg if side == "left" else deg)]


def _square(value: str | None, unit: str | None) -> list[Step]:
    side = _cm(_num(value), unit) if value else 50.0
    return [Step(op, **kw) for _ in range(4)
            for op, kw in (("move", {"direction": "forward", "distance_cm": side}), ("turn", {"degrees": 90.0}))]


def _approach(value: str | None, unit: str | None) -> list[Step]:
    return [Step("approach", distance_cm=_cm(_num(value), unit) if value else 40.0)]


Rule = tuple["re.Pattern[str]", Callable[..., "list[Step] | None"]]


def _rules(field: FieldConfig | None) -> list[Rule]:
    def through(m: re.Match) -> list[Step] | None:
        name = resolve_element(m.group(1), field) if field is not None else None
        return [Step("through", target=name)] if name else None

    def land_on(m: re.Match) -> list[Step] | None:
        t = re.sub(r"^(?:the|a)\s+", "", m.group(1).strip())
        return [Step("land_on", target=_LANDING[t])] if t in _LANDING else None

    def nav_element(m: re.Match) -> list[Step] | None:
        words = re.sub(r"^(?:the|a)\s+", "", m.group(1).strip())
        if field is None:
            return None
        if words in _LANDING and _LANDING[words] in ("pad", "bullseye"):
            el = field.elements.get("landing_pad" if _LANDING[words] == "pad" else "bullseye")
            return [Step("nav", x_cm=el.x, y_cm=el.y)] if el else None
        name = resolve_element(words, field)
        if not name:
            return None
        el = field.elements[name]
        return [Step("nav", x_cm=el.x, y_cm=el.y, z_cm=el.z if el.z > 0 else None)]

    def nav_point(m: re.Match) -> list[Step]:
        z = float(m.group(3)) if m.group(3) else None
        return [Step("nav", x_cm=float(m.group(1)), y_cm=float(m.group(2)), z_cm=z)]

    R = re.compile
    return [
        (R(r"^(?:take ?off|lift ?off|launch|start flying|get airborne)$"), lambda m: [Step("takeoff")]),
        (R(r"^(?:land|touch down|set down)\s+(?:on|at|onto)\s+(.+)$"), land_on),
        (R(r"^(?:land|land it|land the drone|touch down|set down|come down and land)$"), lambda m: [Step("land")]),
        (R(r"^(?:stay|keep|remain)\s+(?:in the air|airborne|hovering|flying|up)$|^(?:don'?t|do not)\s+land$"),
         lambda m: []),
        (R(rf"^(hover|stay|hold(?: still| position)?)(?:\s+(?:for\s+)?{NUM}\s*{SECS}?)?$"),
         lambda m: [Step("hover", seconds=_num(m.group(2)) if m.group(2) else 1.0)]),
        (R(rf"^(?:wait|pause)(?:\s+(?:for\s+)?{NUM}\s*{SECS}?)?$"),
         lambda m: [Step("wait", seconds=_num(m.group(1)) if m.group(1) else 1.0)]),
        (R(rf"^(?:{GO}\s+)?(?:to the\s+)?{DIR}\s+(?:by\s+|for\s+)?{NUM}\s*{UNIT}?$"),
         lambda m: _move(m.group(1), m.group(2), m.group(3))),
        (R(rf"^{GO}\s+{NUM}\s*{UNIT}?\s+(?:to the\s+)?{DIR}$"), lambda m: _move(m.group(3), m.group(1), m.group(2))),
        (R(rf"^(?:climb|rise|ascend|go higher|fly higher)\s+(?:by\s+)?{NUM}\s*{UNIT}?$"),
         lambda m: _move("up", m.group(1), m.group(2))),
        (R(rf"^(?:descend|drop|lower|go lower|fly lower|sink)\s+(?:by\s+)?{NUM}\s*{UNIT}?$"),
         lambda m: _move("down", m.group(1), m.group(2))),
        (R(rf"^(?:turn|rotate|yaw|spin)\s+(left|right)(?:\s+(?:by\s+)?{NUM}\s*{DEG}?)?$"),
         lambda m: _turn(m.group(1), m.group(2))),
        (R(rf"^(?:turn|rotate|yaw|spin)\s+(?:by\s+)?{NUM}\s*{DEG}?\s+(?:to the\s+)?(left|right)$"),
         lambda m: _turn(m.group(2), m.group(1))),
        (R(r"^(?:turn|rotate|spin)\s+around$|^(?:do an?\s+|make an?\s+)?u-?turn$|^about face$"),
         lambda m: [Step("turn", degrees=180.0)]),
        (R(rf"^(?:turn|rotate|yaw|spin)\s+(?:by\s+)?(-?\d+(?:\.\d+)?)\s*{DEG}?$"),
         lambda m: [Step("turn", degrees=float(m.group(1)))]),
        (R(rf"^(?:face|point|turn to face|turn to)\s+(?:heading\s+)?(\d+(?:\.\d+)?)\s*{DEG}?$"),
         lambda m: [Step("face", degrees=float(m.group(1)))]),
        (R(r"^(?:go|come|fly|return|head)\s+(?:back\s+)?(?:home|to (?:the )?start(?:ing point)?|"
           r"to (?:the )?takeoff(?: point)?)$|^(?:come|go|fly)\s+back$|^return$"),
         lambda m: [Step("nav", x_cm=0.0, y_cm=0.0)]),
        (R(r"^(?:go|fly|navigate|move|head)\s+to\s+(?:the\s+)?(?:point\s+|position\s+|coordinates?\s+|spot\s+)?"
           r"\(?\s*(?:x\s*=?\s*)?(-?\d+(?:\.\d+)?)\s*(?:cm)?\s*(?:,\s*|\s+)(?:y\s*=?\s*)?(-?\d+(?:\.\d+)?)\s*(?:cm)?"
           r"\s*\)?(?:\s*,?\s*(?:at\s+)?(?:altitude|height|z)?\s*=?\s*(\d+(?:\.\d+)?)\s*(?:cm)?)?$"), nav_point),
        (R(r"^(?:go|fly|pass|move)?\s*through\s+(.+)$"), through),
        (R(rf"^(?:approach|close in on|get close to)(?:\s+(?:the|a))?(?:\s+{THING})?"
           rf"(?:\s+(?:(?:to|until|at)\s+)?{NUM}\s*{UNIT}?(?:\s+away)?)?$"), lambda m: _approach(m.group(1), m.group(2))),
        (R(rf"^(?:{GO}|keep going)(?:\s+(?:forward|ahead|straight))?\s+until\s+(?:you(?:'re| are)\s+|it(?:'s| is)\s+)?"
           rf"(?:about\s+)?{NUM}\s*{UNIT}?\s+(?:away\s+)?(?:from|before|in front of)\s+(?:the\s+|an?\s+)?{THING}$"),
         lambda m: _approach(m.group(1), m.group(2))),
        (R(rf"^(?:{GO}|keep going)(?:\s+(?:forward|ahead|straight))?\s+until\s+(?:you\s+)?(?:see|reach|find|detect|hit)"
           rf"\s+(?:the\s+|an?\s+)?{THING}$"), lambda m: _approach(None, None)),
        (R(r"^(?:go|fly|navigate|move|head)\s+(?:over\s+)?to\s+(.+)$"), nav_element),
        (R(r"^(?:scan|look around|check (?:around|the surroundings)|sweep|survey)"
           r"(?:\s+(?:for obstacles|the area|around))?$"), lambda m: [Step("scan")]),
        (R(rf"^(?:set\s+|turn\s+|make\s+|change\s+)?(?:the\s+)?(?:led|leds|light|lights)\s+(?:colou?r\s+)?(?:to\s+)?{COLOR}$"),
         lambda m: [Step("led", color=m.group(1))]),
        (R(r"^(?:turn\s+)?(?:the\s+)?(?:led|leds|light|lights)\s+off$|^turn\s+off\s+(?:the\s+)?(?:led|leds|light|lights)$"),
         lambda m: [Step("led", color="off")]),
        (R(rf"^(?:glow|light up|flash)\s+{COLOR}$"), lambda m: [Step("led", color=m.group(1))]),
        (R(r"^(?:beep|buzz|honk|make a sound|sound the buzzer)$"), lambda m: [Step("beep")]),
        (R(rf"^(?:fly|do|make|draw|trace)?\s*(?:an?\s+)?square(?:\s+(?:of|with)?\s*(?:sides?\s+)?(?:of\s+)?{NUM}\s*{UNIT}?)?$"),
         lambda m: _square(m.group(1), m.group(2))),
        (R(rf"^(?:fly|do|make|draw|trace)?\s*(?:an?\s+)?{NUM}\s*{UNIT}?\s+square$"), lambda m: _square(m.group(1), m.group(2))),
    ]


def split_clauses(text: str) -> list[str]:
    text = _PAREN_PAIR.sub(r"(\1 \2)", text or "")
    text = _PAIR_Z.sub(r"\1 ", _TO_PAIR.sub(r"\1 ", text))
    out = []
    for part in _SPLIT.split(text):
        c = re.sub(r"\s+", " ", part.strip().lower()).strip(" ?")
        while True:
            stripped = _LEAD.sub("", c)
            if stripped == c:
                break
            c = stripped
        if c and c not in _FILLER_ONLY:
            out.append(c)
    return out


def parse_clause(clause: str, field: FieldConfig | None = None) -> list[Step] | None:
    times = 1
    m = _TIMES.search(clause)
    if m:
        word = m.group(1) or m.group(2) or ("2" if m.group(3) else "3")
        times = int(_num(word))
        clause = clause[: m.start()].strip()
    for pattern, build in _rules(field):
        hit = pattern.match(clause)
        if hit:
            steps = build(hit)
            if steps is None:
                return None
            return [Step(**vars(s)) for _ in range(times) for s in steps]
    return None


def parse_phrases(text: str, field: FieldConfig | None = None) -> list[Step]:
    clauses = split_clauses(text)
    if not clauses:
        raise PhraseError([(text or "").strip() or "(nothing)"])
    steps: list[Step] = []
    unknown: list[str] = []
    for c in clauses:
        got = parse_clause(c, field)
        if got is None and steps and steps[-1].op == "through" and field is not None:
            name = resolve_element(c, field, strict=True)  # "through the red arch, then the green keyhole"
            got = [Step("through", target=name)] if name else None
        if got is None:
            unknown.append(c)
        else:
            steps += got
    if unknown:
        raise PhraseError(unknown)
    return steps
