"""PILOT panel: the keystroke HUD. Focusing its pad *is* PILOT mode."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.widgets import Static

from ..pilot import SPEEDS, KeyMap, PilotState
from .graph import lerp_color
from .panels import Panel

_NAMES = {"up": "↑", "down": "↓", "left": "←", "right": "→", "space": "spc", "enter": "ent",
          "backspace": "bks", "tab": "tab", "comma": ",", "full_stop": ".", "semicolon": ";",
          "slash": "/", "minus": "-", "equals_sign": "=", "apostrophe": "'",
          "left_square_bracket": "[", "right_square_bracket": "]"}
_STATUS = {
    "off": ("○ OFF", "muted", "press {toggle} to fly with keys"),
    "live": ("● LIVE", "success", "hold keys to fly"),
    "landed": ("◌ LANDED", "warning", "{takeoff} takes off"),
    "busy": ("⏵ BUSY", "warning", "a command is running"),
    "offline": ("○ OFFLINE", "error", "`pair` first"),
    "no focus": ("◌ NO FOCUS", "warning", "click the TUI window"),
    "error": ("✗ ERROR", "error", ""),
}
HUD_WIDTH = 42


def key_label(key: str) -> str:
    if not key:
        return "·"
    if key in _NAMES:
        return _NAMES[key]
    return key.upper() if len(key) <= 3 else key[:3].upper()


def _short(line: str) -> str:
    words = line.split()
    if words[:1] == ["shape"] or words[:2] == ["mission", "run"]:
        return words[-1]
    return line if len(line) <= 14 else line[:13] + "…"


def render_hud(state: PilotState, km: KeyMap, pal: dict, *, lit: set[str], has_flip: bool,
               capture: str | None = None) -> Text:
    """``lit`` = actions to light up (held axes, recently pressed one-shots, ``cmd:<key>`` slots)."""
    on_style = f"bold #000000 on {pal['accent']}"
    off_style = f"bold {pal['foreground']} on {lerp_color(pal['muted'], '#000000', 0.6)}"
    muted = pal["muted"]
    out = Text(no_wrap=True, overflow="crop")

    def cap(action: str) -> None:
        key = km.keys.get(action, "")
        lbl = key_label(key).center(3)[:3]
        out.append(lbl, style=on_style if action in lit else (off_style if key else muted))

    def gap(n: int) -> None:
        out.append(" " * n)

    if capture:
        out.append(f" press a key for {capture} ", style=f"bold #000000 on {pal['warning']}")
        out.append("  esc cancels\n", style=muted)
    else:
        label, colour, hint = _STATUS.get(state.status, _STATUS["off"])
        out.append(label, style=f"bold {pal.get(colour, muted)}")
        hint = hint.format(toggle=key_label(km.keys.get("toggle", "")), takeoff=key_label(km.keys.get("takeoff", "")))
        out.append(f"  {state.error or hint}\n", style=pal["error"] if state.error else muted)

    gap(4)
    cap("forward")
    gap(9)
    cap("speed_up")
    gap(5)
    cap("up")
    out.append(" up\n", style=muted)
    gap(1)
    for action in ("left", "back", "right"):
        cap(action)
    gap(3)
    for action in ("turn_left", "speed_down", "turn_right"):
        cap(action)
    gap(2)
    cap("down")
    out.append(" down\n", style=muted)
    out.append("  move        turn·speed  ", style=muted)
    for i in range(len(SPEEDS)):
        out.append("▮" if i < state.speed else "▯", style=pal["accent"] if i < state.speed else muted)
    out.append(f" {state.speed} {SPEEDS[state.speed - 1]}%\n", style=muted)

    def bar(name: str, v: int) -> None:
        half = 4
        n = round(abs(v) / 100 * half)
        out.append(f"{name:<5}", style=muted)
        for i in range(-half, half + 1):
            if i == 0:
                out.append("┼", style=muted)
            elif (v > 0 and 0 < i <= n) or (v < 0 and -n <= i < 0):
                out.append("━", style=pal["accent"])
            else:
                out.append("─", style=muted)
        out.append(f"{v:+4d}", style=pal["foreground"] if v else muted)

    roll, pitch, yaw, thr = state.sticks
    for (n1, v1), (n2, v2) in ((("fwd", pitch), ("side", roll)), (("turn", yaw), ("alt", thr))):
        bar(n1, v1)
        gap(2)
        bar(n2, v2)
        out.append("\n")

    cap("takeoff")
    out.append(" takeoff ", style=muted)
    cap("land")
    out.append(" land  ", style=muted)
    out.append("esc", style=f"bold {pal['foreground']}")
    out.append(" leave\n", style=muted)
    if has_flip:
        for action, word in (("flip_front", "front"), ("flip_back", "back"), ("flip_left", "left"), ("flip_right", "right")):
            cap(action)
            out.append(f" {word} ", style=muted)
    else:
        out.append("flips: not on this drone", style=muted)

    col, rows = HUD_WIDTH, 0
    items = sorted(km.commands.items(), key=lambda kv: (len(kv[0]), kv[0]))
    for i, (key, line) in enumerate(items):
        lbl, word = f" {key_label(key)} ", f" {_short(line)} "
        if col + len(lbl) + len(word) > HUD_WIDTH:
            if rows == 2:
                out.append(f"+{len(items) - i}", style=muted)
                break
            out.append("\n")
            col, rows = 0, rows + 1
        out.append(lbl, style=on_style if f"cmd:{key}" in lit else off_style)
        out.append(word, style=muted)
        col += len(lbl) + len(word)
    return out


class PilotPad(Static, can_focus=True):
    DEFAULT_CSS = "PilotPad { height: 1fr; }"

    async def on_key(self, event: events.Key) -> None:
        pilot = getattr(self.app, "pilot", None)
        if pilot is not None and pilot.on_key(event.key):
            event.stop()
            event.prevent_default()

    def on_focus(self, _event: events.Focus) -> None:
        pilot = getattr(self.app, "pilot", None)
        if pilot is not None:
            pilot.on_pad_focus()

    def on_blur(self, _event: events.Blur) -> None:
        pilot = getattr(self.app, "pilot", None)
        if pilot is not None:
            pilot.on_pad_blur()


class PilotPanel(Panel):
    def __init__(self, **kw) -> None:
        super().__init__("Pilot", hotkey="8", **kw)

    def compose(self) -> ComposeResult:
        yield PilotPad("PILOT mode not loaded", id="pilot-pad")

    @property
    def pad(self) -> PilotPad:
        return self.query_one("#pilot-pad", PilotPad)

    def show(self, hud: Text, source: str, sig: object) -> None:
        if self.changed(sig):
            self.border_subtitle = f"input: {source}"
            self.pad.update(hud)
