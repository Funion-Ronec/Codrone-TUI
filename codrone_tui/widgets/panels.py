"""The rovr-style bordered panels that make up both TUIs."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.suggester import Suggester
from textual.widget import Widget
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from ..backend import Telemetry
from .graph import Graph, Meter, append_runs, palette


class Panel(Vertical):
    """A rounded box with its title set into the top border (the rovr look)."""

    DEFAULT_CSS = """
    Panel {
        border: round $primary-darken-2;
        border-title-color: $primary;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        background: $surface;
        padding: 0 1;
    }
    Panel:focus-within {
        border: round $accent;
        border-title-color: $accent;
    }
    """

    def __init__(self, title: str, *children: Widget, hotkey: str | None = None, **kw) -> None:
        super().__init__(*children, **kw)
        self.border_title = f"{hotkey} {title}" if hotkey else title
        self._last_sig: object = None

    def changed(self, sig: object) -> bool:
        """True (and remembers it) when ``sig`` differs from the last call. Panels redrawn from a timer
        use this so a quiet panel costs nothing instead of a full re-layout four times a second."""
        if sig == self._last_sig:
            return False
        self._last_sig = sig
        return True


# --------------------------------------------------------------------------- status bar
class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        height: 3;
        border: round $primary-darken-2;
        background: $surface;
        padding: 0 1;
    }
    """


# ------------------------------------------------------------------------------ console
class ShellSuggester(Suggester):
    def __init__(self, shell) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self.shell = shell

    async def get_suggestion(self, value: str) -> str | None:
        return self.shell.complete(value)


class CmdInput(Input):
    BINDINGS = [
        Binding("escape", "leave", "leave input", show=False),
        Binding("up", "history(-1)", "previous command", show=False),
        Binding("down", "history(1)", "next command", show=False),
        Binding("tab", "complete", "complete", show=False),
    ]

    class Leave(Message):
        pass

    def __init__(self, shell, **kw) -> None:
        super().__init__(placeholder="type a command · help · tab completes",
                         suggester=ShellSuggester(shell), compact=True, **kw)
        self.shell = shell
        self._hist_pos: int | None = None
        self._draft = ""

    def action_leave(self) -> None:
        self.post_message(self.Leave())

    def action_complete(self) -> None:
        if self._suggestion:
            self.value = self._suggestion
            self.action_end()

    def action_history(self, step: int) -> None:
        hist = self.shell.history
        if not hist:
            return
        if self._hist_pos is None:
            if step > 0:
                return
            self._draft, self._hist_pos = self.value, len(hist)
        pos = self._hist_pos + step
        if pos >= len(hist):
            self._hist_pos, self.value = None, self._draft
        else:
            self._hist_pos = max(0, pos)
            self.value = hist[self._hist_pos]
        self.action_end()

    def reset_history(self) -> None:
        self._hist_pos = None


class ConsolePanel(Panel):
    DEFAULT_CSS = """
    ConsolePanel #log { height: 1fr; background: $surface; scrollbar-size-vertical: 1; overflow-x: hidden; }
    ConsolePanel #prompt-row { height: 1; }
    ConsolePanel #prompt { width: 2; color: $accent; text-style: bold; }
    ConsolePanel CmdInput { width: 1fr; background: $surface; border: none; padding: 0; }
    """

    def __init__(self, shell, **kw) -> None:
        super().__init__("Console", hotkey="1", **kw)
        self.shell = shell

    def compose(self) -> ComposeResult:
        # min_width defaults to 78, which in a narrower panel adds a horizontal scrollbar and clips lines
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=3000, min_width=16)
        with Horizontal(id="prompt-row"):
            yield Static("❯", id="prompt")
            yield CmdInput(self.shell, id="cmd")

    @property
    def log_widget(self) -> RichLog:
        return self.query_one("#log", RichLog)

    @property
    def input(self) -> CmdInput:
        return self.query_one("#cmd", CmdInput)

    def write(self, text: str, kind: str = "info") -> None:
        pal = palette(self.app)
        style = {"info": pal["foreground"], "ok": pal["success"], "warn": pal["warning"],
                 "error": f"bold {pal['error']}", "dim": pal["muted"], "cmd": pal["primary"]}.get(kind, pal["foreground"])
        line = Text()
        line.append(time.strftime("%H:%M:%S "), style=pal["muted"])
        line.append(text, style=style)
        self.log_widget.write(line)

    def clear(self) -> None:
        self.log_widget.clear()


# ----------------------------------------------------------------------------- sidebar
@dataclass(frozen=True)
class SideItem:
    label: str
    command: str
    prefill: bool = False  # True: put the command in the input instead of running it
    hint: str = ""


class SidebarPanel(Panel):
    DEFAULT_CSS = "SidebarPanel OptionList { background: $surface; border: none; padding: 0; height: 1fr; }"

    class Chosen(Message):
        def __init__(self, item: SideItem) -> None:
            super().__init__()
            self.item = item

    def __init__(self, **kw) -> None:
        super().__init__("Sidebar", hotkey="2", **kw)
        self._items: dict[str, SideItem] = {}

    def compose(self) -> ComposeResult:
        yield OptionList(id="side")

    def set_sections(self, sections: list[tuple[str, list[SideItem]]]) -> None:
        ol = self.query_one("#side", OptionList)
        ol.clear_options()
        self._items.clear()
        pal = palette(self.app)
        n = 0
        for title, items in sections:
            ol.add_option(Option(Text(f"▾ {title}", style=f"bold {pal['accent']}"), disabled=True))
            for it in items:
                oid = f"i{n}"
                n += 1
                self._items[oid] = it
                label = Text("  " + it.label)
                if it.hint:
                    label.append("  " + it.hint, style=pal["muted"])
                ol.add_option(Option(label, id=oid))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        item = self._items.get(event.option_id or "")
        if item:
            self.post_message(self.Chosen(item))


# --------------------------------------------------------------------------- telemetry
class TelemetryPanel(Panel):
    DEFAULT_CSS = """
    TelemetryPanel VerticalScroll { height: 1fr; scrollbar-size-vertical: 1; }
    TelemetryPanel Graph { margin-bottom: 1; }
    TelemetryPanel #kv { height: auto; }
    """

    def __init__(self, kind: str, **kw) -> None:
        super().__init__("Telemetry", hotkey="3", **kw)
        self.kind = kind
        self.speed_hist: deque[float] = deque(maxlen=240)

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            if self.kind == "edu":
                yield Meter("BAT", id="bat")
            yield Graph("FRONT RANGE", " cm", vmin=0, rows=3, invert=True, id="front")
            yield Graph("HEIGHT (bottom)", " cm", vmin=0, rows=3, flat=True, id="bottom")
            if self.kind == "edu":
                yield Graph("SPEED (flow)", " cm/s", vmin=0, vmax=None, rows=2, flat=True, id="speed")
            yield Static(id="kv")

    def update_from(self, t: Telemetry, series) -> None:
        """``series(attr)`` returns the poller's history for a Telemetry field."""
        if self.kind == "edu":
            self.query_one("#bat", Meter).set_value(t.battery)
        front = series("front_range_cm")
        self.query_one("#front", Graph).set_data(front, t.front_range_cm)
        self.query_one("#bottom", Graph).set_data(series("bottom_range_cm"), t.bottom_range_cm)
        if self.kind == "edu":
            if t.flow_cm_s is not None:
                self.speed_hist.append(math.hypot(*t.flow_cm_s))
            self.query_one("#speed", Graph).set_data(list(self.speed_hist))
        self.query_one("#kv", Static).update(self._kv(t))

    def _kv(self, t: Telemetry):
        pal = palette(self.app)
        g = Table.grid(padding=(0, 1))
        g.add_column(style=pal["muted"], no_wrap=True)
        g.add_column(style=pal["foreground"], overflow="fold")

        def row(label: str, val) -> None:
            if val is not None:
                g.add_row(label, val)

        def vec(v, fmt="{:+.0f}"):
            return None if v is None else "  ".join(fmt.format(c) for c in v)

        row("state", t.flight_state)
        row("movement", t.movement_state)
        row("wall", None if t.wall_detected is None else ("YES" if t.wall_detected else "no"))
        pos = vec(t.pos_cm)
        row("pos cm" + (" ~est" if t.pos_is_estimate else ""), pos)
        row("roll pitch yaw", vec(t.angles_deg))
        row("accel m/s²", vec(t.accel, "{:+.1f}"))
        row("gyro °/s", vec(t.gyro))
        row("flow cm/s", vec(t.flow_cm_s, "{:+.1f}"))
        if t.temperature_c is not None:
            row("temp / press", f"{t.temperature_c:.1f}°C  {t.pressure_pa / 100:.0f} hPa" if t.pressure_pa else f"{t.temperature_c:.1f}°C")
        if self.kind == "sim":
            g.add_row("", Text("simulator: no battery / attitude\nread-back; position is dead-reckoned",
                               style=f"italic {pal['muted']}"))
        return g


# -------------------------------------------------------------------------- processes
class ProcessesPanel(Panel):
    def __init__(self, **kw) -> None:
        super().__init__("Processes", hotkey="4", **kw)

    def compose(self) -> ComposeResult:
        yield Static(id="procs")

    def show(self, executor) -> None:
        pal = palette(self.app)
        t = Text(no_wrap=True, overflow="ellipsis")
        cur = executor.current
        if cur is not None:
            t.append(f"▶ {cur.label}  {cur.elapsed:4.1f}s\n", style=f"bold {pal['accent']}")
        pending = executor.pending
        for j in pending[:3]:
            t.append(f"· {j.label}\n", style=pal["foreground"])
        if len(pending) > 3:
            t.append(f"  +{len(pending) - 3} more queued\n", style=pal["muted"])
        done = [j for j in executor.jobs if j.state in ("done", "failed", "cancelled")][-4:]
        for j in reversed(done):
            mark, colour = {"done": ("✓", pal["success"]), "failed": ("✗", pal["error"])}.get(j.state, ("⊘", pal["warning"]))
            t.append(f"{mark} {j.label} ", style=colour)
            t.append(f"{j.elapsed:.1f}s\n", style=pal["muted"])
        if cur is None and not pending and not done:
            t.append("idle", style=pal["muted"])
        if self.changed((t.plain, self.app.theme)):
            self.query_one("#procs", Static).update(t)


# ------------------------------------------------------------------------------- map
_ARROWS = "→↘↓↙←↖↑↗"  # heading 0,45,90.. clockwise, +x drawn to the right


class MapView(Widget):
    """Top-down view: +x (forward from the start) to the right, +y (left) up."""

    DEFAULT_CSS = "MapView { height: 1fr; }"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.markers: list[tuple[str, float, float, str]] = []  # (glyph, x, y, role)
        self.pose = (0.0, 0.0, 0.0, 0.0)  # x y z heading
        self.trail: deque[tuple[float, float]] = deque(maxlen=400)
        self.estimated = True
        self.scale_cm = 1.0
        self._pose_key: tuple | None = None

    def set_markers(self, markers: list[tuple[str, float, float, str]]) -> None:
        self.markers = list(markers)
        self.refresh()

    def set_pose(self, x: float, y: float, z: float, heading: float, estimated: bool = True) -> None:
        key = (round(x), round(y), round(z), round(heading), estimated)
        if key == self._pose_key:  # telemetry arrives 5x a second; redraw only when the drone really moved
            return
        self._pose_key = key
        self.pose, self.estimated = (x, y, z, heading), estimated
        if not self.trail or math.hypot(x - self.trail[-1][0], y - self.trail[-1][1]) >= 3.0:
            self.trail.append((x, y))
        self.refresh()

    def clear_trail(self) -> None:
        self.trail.clear()
        self._pose_key = None  # the next pose must redraw even if it did not change
        self.refresh()

    def render(self) -> Text:
        pal = palette(self.app)
        w, h = max(8, self.size.width), max(3, self.size.height)
        pts = [(0.0, 0.0), (self.pose[0], self.pose[1]), *self.trail, *[(m[1], m[2]) for m in self.markers]]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x0, x1 = min(xs) - 20, max(xs) + 20
        y0, y1 = min(ys) - 20, max(ys) + 20
        # cm per column; rows are ~2x taller. Fit into (w-2) x (h-2) so rounding to a cell can never
        # push a point onto or past the edge of the widget.
        s = max((x1 - x0) / max(1, w - 2), (y1 - y0) / (2.0 * max(1, h - 2)), 1.0)
        self.scale_cm = s
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

        def cell(x: float, y: float) -> tuple[int, int]:
            col = int(round(w / 2 + (x - cx) / s))
            row = int(round(h / 2 - (y - cy) / (2.0 * s)))
            return col, row

        grid = [[(" ", "")] * w for _ in range(h)]

        def put(x: float, y: float, ch: str, style: str) -> None:
            c, r = cell(x, y)
            if 0 <= c < w and 0 <= r < h:
                grid[r][c] = (ch, style)

        # connect consecutive trail points so a jump (dead-reckoned moves land instantly) reads as a path
        steps = max(w, 2 * h)
        pts_t = list(self.trail) + [(self.pose[0], self.pose[1])]
        for (ax, ay), (bx, by) in zip(pts_t, pts_t[1:]):
            n = max(1, min(steps, int(max(abs(bx - ax) / s, abs(by - ay) / (2.0 * s)))))
            for k in range(n + 1):
                put(ax + (bx - ax) * k / n, ay + (by - ay) * k / n, "·", pal["muted"])
        put(0, 0, "+", pal["accent"])
        role_colour = {"red": "#ef476f", "blue": "#4cc9f0", "green": "#06d6a0", "yellow": "#ffd166",
                       "pad": pal["foreground"], "other": pal["accent"]}
        for glyph, mx, my, role in self.markers:
            put(mx, my, glyph, f"bold {role_colour.get(role, pal['accent'])}")
        px, py, _pz, ph = self.pose
        put(px, py, _ARROWS[int(round(ph / 45.0)) % 8], f"bold {pal['primary']}")

        out = Text(no_wrap=True, overflow="crop")
        for r, row in enumerate(grid):
            if r:
                out.append("\n")
            append_runs(out, ((ch, style or None) for ch, style in row))
        self.parent.border_subtitle = f"1 col = {s:.0f} cm{' · est.' if self.estimated else ''}"
        return out


class MapPanel(Panel):
    def __init__(self, **kw) -> None:
        super().__init__("Map", hotkey="5", **kw)

    def compose(self) -> ComposeResult:
        yield MapView(id="map")

    @property
    def view(self) -> MapView:
        return self.query_one("#map", MapView)


# ------------------------------------------------------------------------------ meta
class MetaPanel(Panel):
    def __init__(self, **kw) -> None:
        super().__init__("Metadata", hotkey="7", **kw)

    def compose(self) -> ComposeResult:
        yield Static(id="meta")

    def show(self, info: dict[str, str], session: dict[str, str]) -> None:
        if not self.changed((tuple(info.items()), tuple(session.items()), self.app.theme)):
            return
        pal = palette(self.app)
        g = Table.grid(padding=(0, 1))
        g.add_column(style=pal["muted"], no_wrap=True)
        g.add_column(style=pal["foreground"], overflow="fold")
        for k, v in {**info, **session}.items():
            g.add_row(k, v)
        self.query_one("#meta", Static).update(g)
