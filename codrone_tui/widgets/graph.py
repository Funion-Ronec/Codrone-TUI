"""btop-style graphics: braille area graphs and gradient meters."""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from rich.text import Text
from textual.widget import Widget

BRAILLE = 0x2800
_LEFT = (0x40, 0x04, 0x02, 0x01)
_RIGHT = (0x80, 0x20, 0x10, 0x08)

_FALLBACK = {"success": "#4caf50", "warning": "#ffc107", "error": "#f44336", "primary": "#4cc9f0",
             "foreground": "#e0e0e0", "accent": "#ffb703", "muted": "#7a8794"}


_PALETTES: dict[str, dict[str, str]] = {}


def palette(app) -> dict[str, str]:
    """Theme colours as hex strings, with safe fallbacks. Cached per theme: ``get_css_variables`` rebuilds
    the whole variable table (~2 ms) and every widget used to call this on every render."""
    key = getattr(app, "theme", "") or ""
    hit = _PALETTES.get(key)
    if hit is not None:
        return hit
    try:
        v = app.get_css_variables()
    except Exception:  # noqa: BLE001
        return dict(_FALLBACK)
    out = dict(_FALLBACK)
    for k in ("success", "warning", "error", "primary", "foreground", "accent"):
        if isinstance(v.get(k), str) and v[k].startswith("#"):
            out[k] = v[k][:7]
    for k in ("text-muted", "foreground-muted"):
        if isinstance(v.get(k), str) and v[k].startswith("#"):
            out["muted"] = v[k][:7]
            break
    _PALETTES[key] = out
    return out


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def lerp_color(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ra, ga, ba = _hex(a)
    rb, gb, bb = _hex(b)
    return "#{:02x}{:02x}{:02x}".format(round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


def gradient(norm: float, low: str, mid: str, high: str) -> str:
    """Three-stop gradient, norm 0..1 (btop's green -> yellow -> red)."""
    norm = max(0.0, min(1.0, norm))
    return lerp_color(low, mid, norm * 2) if norm < 0.5 else lerp_color(mid, high, (norm - 0.5) * 2)


RAMP_STEPS = 24


@lru_cache(maxsize=64)
def ramp(low: str, mid: str, high: str) -> tuple[str, ...]:
    """Pre-computed gradient: one colour lookup per cell instead of three lerps."""
    return tuple(gradient(i / (RAMP_STEPS - 1), low, mid, high) for i in range(RAMP_STEPS))


def append_runs(out: Text, cells: Iterable[tuple[str, str | None]]) -> None:
    """Append ``(char, style)`` cells to ``out`` merging neighbours that share a style. A graph row is
    ~100 cells but only a handful of runs; far fewer spans makes Rich noticeably cheaper to render."""
    run: list[str] = []
    run_style: str | None = None
    for ch, style in cells:
        if style != run_style and run:
            out.append("".join(run), style=run_style)
            run = []
        run_style = style
        run.append(ch)
    if run:
        out.append("".join(run), style=run_style)


def levels(values: list[float | None], vmin: float, vmax: float, rows: int) -> list[int]:
    """Map samples to 0..4*rows dot heights; ``None`` (no sample) -> 0."""
    span = (vmax - vmin) or 1.0
    top = 4 * rows
    out = []
    for v in values:
        if v is None:
            out.append(0)
            continue
        n = (v - vmin) / span
        out.append(max(1, min(top, round(n * top))) if n > 0 else (1 if v > vmin else 0))
    return out


def braille_rows(values: list[float], width: int, height: int, vmin: float, vmax: float
                 ) -> list[list[tuple[str, float]]]:
    """Area graph: ``height`` rows of ``width`` cells, newest sample at the right edge.

    Each cell is ``(char, norm)`` where ``norm`` (0..1) is the taller of its two columns, for colouring.
    """
    if width <= 0 or height <= 0:
        return []
    need = width * 2
    vals: list[float | None] = list(values[-need:])
    vals = [None] * (need - len(vals)) + vals
    lv = levels(vals, vmin, vmax, height)
    span = (vmax - vmin) or 1.0
    norms = [0.0 if v is None else max(0.0, min(1.0, (v - vmin) / span)) for v in vals]
    rows: list[list[tuple[str, float]]] = []
    for r in range(height):
        base = 4 * (height - 1 - r)
        line = []
        for c in range(width):
            bits = 0
            for col, table in ((2 * c, _LEFT), (2 * c + 1, _RIGHT)):
                filled = max(0, min(4, lv[col] - base))
                for k in range(filled):
                    bits |= table[k]
            line.append((chr(BRAILLE + bits) if bits else " ", max(norms[2 * c], norms[2 * c + 1])))
        rows.append(line)
    return rows


class Graph(Widget):
    """A titled braille area graph. Feed it with ``set_data``; it paints in the theme's colours."""

    DEFAULT_CSS = "Graph { height: 5; }"

    def __init__(self, label: str, unit: str = "", vmin: float = 0.0, vmax: float | None = None,
                 rows: int = 3, invert: bool = False, flat: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.label, self.unit = label, unit
        self.vmin, self.vmax_fixed = vmin, vmax
        self.rows = rows
        self.invert = invert
        self.flat = flat
        self.values: list[float] = []
        self.current: float | None = None
        self.styles.height = rows + 1

    def set_data(self, values: list[float], current: float | None = None) -> None:
        self.values = list(values)
        self.current = current if current is not None else (self.values[-1] if self.values else None)
        self.refresh()

    def render(self) -> Text:
        pal = palette(self.app)
        w = max(1, self.size.width)
        vmax = self.vmax_fixed
        if vmax is None:
            vmax = max([self.vmin + 1.0, *self.values]) * 1.1
        shown = "--" if self.current is None else f"{self.current:.0f}{self.unit}"
        head = Text()
        head.append(self.label, style=f"bold {pal['foreground']}")
        gap = max(1, w - len(self.label) - len(shown))
        head.append(" " * gap)
        head.append(shown, style=pal["primary"])
        out = Text(no_wrap=True, overflow="crop")
        out.append_text(head)
        lo, hi = (pal["error"], pal["success"]) if self.invert else (pal["success"], pal["error"])
        colours = ramp(lo, pal["warning"], hi)
        top = RAMP_STEPS - 1
        for line in braille_rows(self.values, w, self.rows, self.vmin, vmax):
            out.append("\n")
            append_runs(out, (
                (ch, None if ch == " " else pal["primary"] if self.flat else colours[int(norm * top)])
                for ch, norm in line
            ))
        return out


class Meter(Widget):
    """`LABEL ■■■■■■□□□□ 82%` with a btop-like gradient."""

    DEFAULT_CSS = "Meter { height: 1; }"

    def __init__(self, label: str, unit: str = "%", maximum: float = 100.0, invert: bool = True, **kw) -> None:
        super().__init__(**kw)
        self.label, self.unit, self.maximum, self.invert = label, unit, maximum, invert
        self.value: float | None = None

    def set_value(self, value: float | None) -> None:
        self.value = value
        self.refresh()

    def render(self) -> Text:
        pal = palette(self.app)
        w = max(8, self.size.width)
        shown = "--" if self.value is None else f"{self.value:.0f}{self.unit}"
        bar_w = max(3, w - len(self.label) - len(shown) - 2)
        frac = 0.0 if self.value is None else max(0.0, min(1.0, self.value / self.maximum))
        filled = round(frac * bar_w)
        lo, hi = (pal["error"], pal["success"]) if self.invert else (pal["success"], pal["error"])
        t = Text(no_wrap=True, overflow="crop")
        t.append(self.label + " ", style=f"bold {pal['foreground']}")
        level = gradient(frac, lo, pal["warning"], hi)
        for i in range(bar_w):
            if i < filled:
                t.append("■", style=level if self.invert else gradient(i / max(1, bar_w - 1), lo, pal["warning"], hi))
            else:
                t.append("■", style=pal["muted"] + " dim")
        t.append(" " + shown, style=pal["primary"])
        return t
