"""The bot panel: which model writes the guidance, a live flight step by step, and the plan `ask` made."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from .graph import palette
from .panels import Panel


class BotPanel(Panel):
    DEFAULT_CSS = """
    BotPanel VerticalScroll { height: 1fr; scrollbar-size-vertical: 1; }
    """

    def __init__(self, **kw) -> None:
        super().__init__("Bot", hotkey="9", **kw)
        self.rendered = None

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("bot features not loaded", id="bot")

    def show(self, st: dict) -> None:
        live = st.get("live") or {}
        sig = (repr(st), self.size.width, self.app.theme)
        if not self.changed(sig):
            return
        pal = palette(self.app)
        out = Table.grid(expand=True)
        out.add_column(ratio=1, overflow="fold")

        # who is deciding
        head = Text(no_wrap=True, overflow="ellipsis")
        prov = st["provider"]
        if prov == "off":
            head.append("model off", style=pal["muted"])
            head.append("  phrases only", style=pal["muted"])
        else:
            head.append(prov, style=f"bold {pal['primary']}")
            head.append(f" · {st['model'] or 'no model set'}", style=pal["foreground"] if st["model"] else pal["error"])
        out.add_row(head)
        if st["needs_key"]:
            k = Text(no_wrap=True, overflow="ellipsis")
            if st["key"]:
                k.append("key ", style=pal["muted"])
                k.append(st["key"], style=pal["success"])
                k.append(f"  {_short_source(st['key_source'])}", style=pal["muted"])
            else:
                k.append("no API key", style=f"bold {pal['error']}")
                k.append("  `llm key`", style=pal["muted"])
            out.add_row(k)
        g = Text(no_wrap=True)
        g.append("mission guard ", style=pal["muted"])
        g.append("ON" if st["guard"] else "off", style=pal["success"] if st["guard"] else pal["muted"])
        g.append("  motion ", style=pal["muted"])
        g.append(st.get("motion", "creep"), style=pal["foreground"])
        if st.get("camera"):
            g.append("  camera ", style=pal["muted"])
            g.append("●", style=pal["error"])
        out.add_row(g)
        out.add_row(Text(""))

        if live:
            out.add_row(self._live(live, pal))
        if st["planning"]:
            out.add_row(Text("writing the guidance…", style=f"bold {pal['warning']}"))
        if st.get("plan") and not (live and live.get("phase") == "flying"):
            out.add_row(self._plan(st["plan"], pal))
        if not live and not st.get("plan") and not st["planning"]:
            hint = Text(style=pal["muted"])
            hint.append("idle\n\n")
            hint.append("live <goal>", style=pal["accent"])
            hint.append("  guidance from your words, flown by the algorithm\n")
            hint.append("ask <words>", style=pal["accent"])
            hint.append("  a plan to read first")
            out.add_row(hint)
        self.rendered = out  # kept for tests and debugging
        self.query_one("#bot", Static).update(out)

    def _live(self, lv: dict, pal: dict):
        t = Table.grid(expand=True)
        t.add_column(ratio=1, overflow="fold")
        phase = lv["phase"]
        bar = Text(no_wrap=True, overflow="ellipsis")
        done = sum(1 for _, status in lv["steps"] if status == "done")
        if phase == "flying":
            bar.append(" LIVE ", style=f"bold #000000 on {pal['accent']}")
            bar.append(f" step {lv['current'] + 1}/{len(lv['steps'])}", style=f"bold {pal['foreground']}")
        else:
            res = lv.get("result") or {}
            ok = res.get("ok")
            bar.append(" DONE " if ok else " ENDED ", style=f"bold #000000 on {pal['success'] if ok else pal['warning']}")
            bar.append(f" {done}/{len(lv['steps'])} steps", style=pal["muted"])
        if lv.get("elapsed") is not None:
            s = int(lv["elapsed"])
            bar.append(f"  {s // 60}:{s % 60:02d}", style=pal["muted"])
        t.add_row(bar)
        t.add_row(Text(lv["goal"], style=f"italic {pal['foreground']}"))
        if phase == "ended":
            res = lv.get("result") or {}
            why = Text(res.get("reason", ""), style=pal["success"] if res.get("ok") else pal["warning"])
            if res.get("landed"):
                why.append("  (landed for safety)", style=pal["muted"])
            t.add_row(why)
        t.add_row(Text(""))
        marks = {"done": ("✓", pal["success"]), "active": ("▶", pal["accent"]), "failed": ("✗", pal["error"]),
                 "stopped": ("■", pal["warning"]),
                 "pending": ("·", pal["muted"])}
        for i, (line, status) in enumerate(lv["steps"], 1):
            mark, colour = marks.get(status, ("·", pal["muted"]))
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(f"{mark} ", style=colour)
            row.append(f"{i:>2} ", style=pal["muted"])
            row.append(line, style=f"bold {pal['foreground']}" if status == "active" else pal["foreground"])
            t.add_row(row)
        if lv.get("events"):
            t.add_row(Text(""))
            for e in lv["events"]:
                t.add_row(Text(f"» {e}", style=pal["primary"], no_wrap=True, overflow="ellipsis"))
        if lv.get("frames") is not None:
            t.add_row(Text(f"{lv['frames']} camera frame(s) saved", style=pal["muted"]))
        return t

    @staticmethod
    def _plan(plan: dict, pal: dict) -> Text:
        t = Text()
        t.append(" PLAN ", style=f"bold #000000 on {pal['primary']}")
        t.append(f" {plan['steps']} steps", style=pal["foreground"])
        if plan.get("seconds"):
            t.append(f" · ~{plan['seconds']:.0f} s", style=pal["muted"])
        t.append(f"\n{plan['summary']}\n", style=f"italic {pal['foreground']}")
        t.append("plan run", style=pal["accent"])
        t.append(" flies it · ", style=pal["muted"])
        t.append("plan", style=pal["accent"])
        t.append(" shows it", style=pal["muted"])
        return t


def _short_source(source: str) -> str:
    if source.startswith("$") or source == "this session":
        return source
    return "saved" if source else ""

