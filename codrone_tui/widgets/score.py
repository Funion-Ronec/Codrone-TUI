"""Score / match panel for the Solo Autonomous mission."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from .graph import gradient, palette
from .panels import Panel

SHORT = {
    "takeoff": "takeoff", "red_arch": "red arch", "blue_arch": "blue arch", "green_keyhole": "green key",
    "yellow_keyhole": "yellow key", "large_cube": "cube", "mini_1": "mini 1", "mini_2": "mini 2",
    "mini_3": "mini 3", "spiral_bonus": "spiral", "tunnel": "tunnel",
}
LANDING_SHORT = {"landing_pad": "pad +5", "landing_cube": "cube +10", "landing_bullseye": "bullseye +15"}


def _bar(frac: float, width: int, colour_of, pal: dict) -> Text:
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    t = Text(no_wrap=True)
    for i in range(width):
        if i < filled:
            t.append("■", style=colour_of(i / max(1, width - 1)))
        else:
            t.append("■", style=pal["muted"] + " dim")
    return t


def clock(seconds: float) -> str:
    s = max(0, int(seconds + 0.999))
    return f"{s // 60}:{s % 60:02d}"


class ScorePanel(Panel):
    def __init__(self, **kw) -> None:
        super().__init__("Score", hotkey="6", **kw)

    def compose(self) -> ComposeResult:
        yield Static("mission features not loaded", id="score")

    def show(self, adc) -> None:
        from ..adc import MATCH_SECONDS, MAX_ATTEMPTS, max_score

        m = adc.match
        card = adc.scorecard
        sig = (m.state, clock(m.remaining) if m.state == "running" else "", card.total,
               tuple(card.counts.values()), card.landing, m.best_score, m.piloting_best, len(m.attempts),
               m.resets, adc.running, adc.current_name, self.size.width, self.app.theme)
        if not self.changed(sig):  # called 4x a second; the panel only changes when the clock or score does
            return
        pal = palette(self.app)
        width = max(10, self.size.width - 4)
        out = Table.grid(expand=True)
        out.add_column(ratio=1)

        # header: state + clock
        head = Text(no_wrap=True)
        if m.state == "running":
            low = m.remaining < 30
            head.append(f"▶ MATCH {len(m.attempts) + 1}/{MAX_ATTEMPTS}  ", style=f"bold {pal['accent']}")
            head.append(clock(m.remaining), style=f"bold {pal['error'] if low else pal['success']}")
            head.append(f"  resets {m.resets}", style=pal["muted"])
        elif m.state == "finished":
            r = m.last_result
            head.append(f"■ DONE {r.number}/{MAX_ATTEMPTS}  ", style=f"bold {pal['primary']}")
            head.append(f"{r.score} pts" + (" (time up)" if r.expired else ""), style=pal["foreground"])
        else:
            head.append(f"● PRACTICE  attempts left {m.attempts_left}/{MAX_ATTEMPTS}", style=pal["muted"])
        out.add_row(head)

        if m.state == "running":
            out.add_row(_bar(m.elapsed / MATCH_SECONDS, width,
                             lambda f: gradient(f, pal["success"], pal["warning"], pal["error"]), pal))
        total = card.total
        line = Text(no_wrap=True)
        line.append("SCORE ", style=f"bold {pal['foreground']}")
        line.append(f"{total}", style=f"bold {pal['accent']}")
        line.append(f" / {max_score()}", style=pal["muted"])
        out.add_row(line)
        out.add_row(_bar(total / max_score(), width,
                         lambda f: gradient(1 - f, pal["success"], pal["warning"], pal["error"]), pal))

        tasks = Table.grid(expand=True, padding=(0, 1))
        for _ in range(2):
            tasks.add_column(ratio=1, no_wrap=True)
            tasks.add_column(justify="right", no_wrap=True)
        cells: list[tuple[Text, Text]] = []
        for row in card.rows():
            if row.group == "landing":
                continue
            done = row.count >= row.max_count
            colour = pal["success"] if done else pal["warning"] if row.count else pal["muted"]
            cells.append((Text(SHORT.get(row.key, row.key), style=colour), Text(f"{row.count}/{row.max_count}", style=colour)))
        land = card.landing
        cells.append((Text("landing", style=pal["success"] if land else pal["muted"]),
                      Text(LANDING_SHORT.get(land, "-") if land else "-", style=pal["success"] if land else pal["muted"])))
        for i in range(0, len(cells), 2):
            pair = cells[i:i + 2]
            if len(pair) == 1:
                pair.append((Text(""), Text("")))
            tasks.add_row(pair[0][0], pair[0][1], pair[1][0], pair[1][1])
        out.add_row(tasks)

        foot = Text(no_wrap=True, overflow="ellipsis")
        foot.append(f"best {m.best_score}", style=pal["muted"])
        if m.piloting_best is not None:
            foot.append(f" · piloting {m.piloting_best} · skills {m.skills_total}", style=pal["muted"])
        if adc.running:
            foot.append(f" · ⏵ {adc.current_name}", style=pal["warning"])
        out.add_row(foot)
        self.query_one("#score", Static).update(out)
