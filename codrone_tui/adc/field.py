"""Field layout the autonomous routines navigate by (``field.toml``).

Frame: centimetres, origin = the first foot of the programming mat (3.2.2), +x forward,
+y left, +z up. ``heading`` is the direction the drone must travel to score the element,
in degrees clockwise from +x (so 270 means "toward +y"). ``z`` is the altitude to cross it at.

The official RECF assembly document with element dimensions could not be fetched while this
was written (HTTP 403), and the manual allows +-3 in of variance between fields (7.3.2). The
built-in layout is therefore PLACEHOLDER geometry, always reported as uncalibrated.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIELD_PATH = PROJECT_ROOT / "field.toml"

DEFAULT_MAT_ORIGIN = (
    "centre of the green first foot of the programming mat (where the drone starts); +x forward up the "
    "long leg, +y left (pilot side), +z up; start heading 0; the field turns RIGHT near the far end"
)
DEFAULT_SPIRAL_ORDER = ("mini_1", "mini_2", "mini_3")
IN = 2.54  # the manual gives element sizes in inches

# Elements the built-in routines need.
REQUIRED_ELEMENTS = (
    "red_arch", "blue_arch", "green_keyhole", "yellow_keyhole", "large_cube",
    "mini_1", "mini_2", "mini_3", "tunnel", "landing_pad", "bullseye",
)


@dataclass(frozen=True)
class Element:
    name: str
    label: str
    x: float
    y: float
    z: float = 0.0
    heading: float = 0.0
    width: float = 0.0  # 0 = unknown / not measured
    height: float = 0.0
    kind: str = ""
    vertical: bool = False  # True: scored by flying DOWN through it (the tunnel, manual fig 3.1.5)


@dataclass
class FieldConfig:
    calibrated: bool = False
    elements: dict[str, Element] = field(default_factory=dict)
    mat_origin: str = DEFAULT_MAT_ORIGIN
    notes: str = ""
    spiral_order: tuple[str, ...] = DEFAULT_SPIRAL_ORDER
    source: str = "built-in placeholder layout"
    load_errors: list[str] = field(default_factory=list)

    def get(self, name: str) -> Element:
        try:
            return self.elements[name]
        except KeyError:
            raise KeyError(f"field has no element '{name}' (known: {', '.join(sorted(self.elements)) or 'none'})") from None

    def warnings(self) -> list[str]:
        out = list(self.load_errors)
        if not self.calibrated:
            out.append("field is NOT calibrated: positions are estimates read from the manual's Figure 3.2.2, "
                       "measure your field before flying")
        missing = [n for n in REQUIRED_ELEMENTS if n not in self.elements]
        if missing:
            out.append("field.toml is missing: " + ", ".join(missing))
        return out

    def to_toml(self) -> str:
        lines = [
            f"calibrated = {_b(self.calibrated)}",
            f"mat_origin = {_s(self.mat_origin)}",
            f"notes = {_s(self.notes)}",
            "spiral_order = [" + ", ".join(_s(n) for n in self.spiral_order) + "]",
        ]
        for el in self.elements.values():
            lines += ["", f"[elements.{_key(el.name)}]", f"label = {_s(el.label)}", f"kind = {_s(el.kind)}"]
            lines += [f"{k} = {float(getattr(el, k))!r}" for k in ("x", "y", "z", "heading", "width", "height")]
            lines.append(f"vertical = {_b(el.vertical)}")
        return "\n".join(lines) + "\n"


# The solo field (manual Figure 3.2.2) is an L: two 96" x 42" mats end to end up the long leg (x),
# then one more mat turning RIGHT (-y). Sizes below are the manual's section 1.5.2 figures in cm
# (arch opening 60" x 63", keyhole inner diameter 24", large cube 17" x 20", pad 30" / bullseye 10",
# tunnel 23.5" x 19"). x / y are ESTIMATES read off the figure (about +-10 cm, before the manual's own
# +-3 in field-to-field variance); z (crossing heights) is not in the manual at all and is a guess.
# name, label, kind, x, y, z, heading, width, height, vertical
_PLACEHOLDER = (
    ("red_arch", "Red arch gate", "arch", 49, 0, 80, 0, 60 * IN, 63 * IN, False),
    ("green_keyhole", "Green keyhole gate", "keyhole", 183, 0, 110, 0, 24 * IN, 24 * IN, False),
    ("large_cube", "Large cube", "cube", 255, 0, 25, 0, 17 * IN, 20 * IN, False),
    ("mini_1", "Mini keyhole #1 (lowest)", "mini_keyhole", 422, 30, 60, 270, 0, 0, False),
    ("mini_2", "Mini keyhole #2 (middle)", "mini_keyhole", 422, 30, 100, 270, 0, 0, False),
    ("mini_3", "Mini keyhole #3 (top)", "mini_keyhole", 422, 30, 140, 270, 0, 0, False),
    ("blue_arch", "Blue arch gate", "arch", 422, -96, 80, 90, 60 * IN, 63 * IN, False),
    ("yellow_keyhole", "Yellow keyhole gate", "keyhole", 422, -175, 120, 90, 24 * IN, 24 * IN, False),
    ("tunnel", "Tunnel (fly DOWN through it; z = height of its top opening)", "tunnel", 422, -254, 110, 0,
     23.5 * IN, 19 * IN, True),
    ("landing_pad", "Landing pad", "pad", 86, 0, 0, 0, 30 * IN, 0, False),
    ("bullseye", "Bullseye (centre ring of the pad)", "pad", 86, 0, 0, 0, 10 * IN, 0, False),
)


def default_field() -> FieldConfig:
    return FieldConfig(
        calibrated=False,
        elements={
            n: Element(n, label, float(x), float(y), float(z), float(h), round(float(w), 1), round(float(ht), 1), kind, v)
            for n, label, kind, x, y, z, h, w, ht, v in _PLACEHOLDER
        },
        notes="ESTIMATED layout: sizes from the manual (1.5.2), x/y read from its Figure 3.2.2, z guessed. "
              "Measure your field and set calibrated = true.",
        source="built-in estimated layout",
    )


def load_field(path: str | Path | None = None) -> FieldConfig:
    """Read a field TOML. A missing file gives the placeholder layout; a bad one gives it too,
    with the reason in ``load_errors`` (so the UI can show it) instead of crashing."""
    target = Path(path) if path is not None else DEFAULT_FIELD_PATH
    if not target.exists():
        cfg = default_field()
        cfg.load_errors.append(f"{target} not found, using the built-in placeholder layout")
        return cfg
    try:
        with target.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        cfg = default_field()
        cfg.load_errors.append(f"could not read {target.name}: {exc}; using the built-in placeholder layout")
        return cfg
    return _from_dict(data, target)


def _from_dict(data: dict[str, Any], source: Path) -> FieldConfig:
    errors: list[str] = []
    elements: dict[str, Element] = {}
    for name, raw in (data.get("elements") or {}).items():
        try:
            elements[name] = Element(
                name=name,
                label=str(raw.get("label", name)),
                x=float(raw["x"]),
                y=float(raw["y"]),
                z=float(raw.get("z", 0.0)),
                heading=float(raw.get("heading", 0.0)),
                width=float(raw.get("width", 0.0)),
                height=float(raw.get("height", 0.0)),
                kind=str(raw.get("kind", "")),
                vertical=raw.get("vertical") is True,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            errors.append(f"element '{name}' ignored: {type(exc).__name__}: {exc}")
    order = data.get("spiral_order", DEFAULT_SPIRAL_ORDER)
    return FieldConfig(
        calibrated=data.get("calibrated") is True,
        elements=elements,
        mat_origin=str(data.get("mat_origin", DEFAULT_MAT_ORIGIN)),
        notes=str(data.get("notes", "")),
        spiral_order=tuple(str(n) for n in order) if isinstance(order, list) else DEFAULT_SPIRAL_ORDER,
        source=str(source),
        load_errors=errors,
    )


def _s(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def _b(value: bool) -> str:
    return "true" if value else "false"


def _key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _s(name)
