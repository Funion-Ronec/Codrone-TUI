"""Autonomous-mission features for the TUIs: match clock, scorekeeping, routines, pre-flight.

Loaded by ``CodroneApp.on_mount``. If anything in here breaks, the flight console still works.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from .adc import (
    LANDINGS,
    MATCH_SECONDS,
    MAX_ATTEMPTS,
    TASKS,
    MatchController,
    MatchError,
    Routine,
    RoutineContext,
    RoutineError,
    Scorecard,
    builtin_routines,
    dry_run,
    load_field,
    load_routines,
    preflight_ok,
    run_preflight,
)
from .adc.preflight import DEFAULT_MIN_BATTERY
from .backend import Cap
from .commands import Command, Shell, UsageError, num
from .config import PROJECT_ROOT
from .screens import ConfirmScreen
from .widgets.panels import MapPanel, SideItem
from .widgets.score import ScorePanel, clock

_MARKER = {
    "red_arch": ("R", "red"), "blue_arch": ("B", "blue"), "green_keyhole": ("G", "green"),
    "yellow_keyhole": ("Y", "yellow"), "large_cube": ("C", "other"), "mini_1": ("1", "other"),
    "mini_2": ("2", "other"), "mini_3": ("3", "other"), "tunnel": ("T", "other"),
    "landing_pad": ("P", "pad"), "bullseye": ("◎", "pad"),
}
_LANDING_WORDS = {"pad": "landing_pad", "cube": "landing_cube", "bullseye": "landing_bullseye",
                  "bull": "landing_bullseye", "none": None, "clear": None}
_ICON = {"pass": "✓", "fail": "✗", "todo": "☐"}


def attach(app) -> None:
    app.adc = AdcController(app)
    app.refresh_sidebar()


class AdcController:
    def __init__(self, app) -> None:
        self.app = app
        self.field = load_field(app.field_path)
        store = getattr(app, "adc_store", None) or PROJECT_ROOT / "logs" / "adc_attempts.json"
        self.match = MatchController(store_path=store, autosave=True)
        self.match.load()
        self.practice = Scorecard()
        self.routines: list[Routine] = []
        self.hints: dict[str, str] = {}
        self.running = False
        self.current_name = ""
        self.confirmed: set[str] = set()
        self.min_battery = int(app.cfg.get("edu", {}).get("min_battery", DEFAULT_MIN_BATTERY))
        self.reload_routines()
        self._set_map_markers()
        self._register_commands(app.shell)
        if self.match.load_error:
            app.console_write(f"attempt history: {self.match.load_error}", "warn")
        for w in self.field.warnings():
            app.console_write(f"field: {w}", "warn")

    @property
    def scorecard(self) -> Scorecard:
        if self.match.state in ("running", "finished") and self.match.scorecard is not None:
            return self.match.scorecard
        return self.practice

    def reload_routines(self) -> None:
        self.routines = builtin_routines() + load_routines()
        self.hints = {}
        for r in self.routines:
            if r.ok:
                rep = dry_run(r, self.field, flavor=self.app.KIND)
                self.hints[r.name] = f"{rep.points}pt" if rep.ok else "err"
            else:
                self.hints[r.name] = "err"

    def find(self, text: str) -> Routine:
        text = text.strip().lower()
        exact = [r for r in self.routines if r.name.lower() == text]
        part = exact or [r for r in self.routines if r.name.lower().startswith(text)]
        if len(part) == 1:
            return part[0]
        if not part:
            raise UsageError(f"no routine '{text}' -- `mission list`")
        raise UsageError("ambiguous: " + ", ".join(r.name for r in part))

    def _set_map_markers(self) -> None:
        markers = []
        for name, el in self.field.elements.items():
            glyph, role = _MARKER.get(name, ("•", "other"))
            markers.append((glyph, el.x, el.y, role))
        try:
            panel = self.app.query_one("#map-panel", MapPanel)
            panel.view.set_markers(markers)
            panel.border_title = "5 Map" + ("" if self.field.calibrated else " ⚠ placeholder field")
        except Exception:  # noqa: BLE001
            pass

    def sidebar_sections(self) -> list[tuple[str, list[SideItem]]]:
        missions = [SideItem(r.name, f"mission run {r.name}", hint=self.hints.get(r.name, "")) for r in self.routines]
        return [
            ("AUTONOMOUS", missions),
            ("MATCH", [
                SideItem("preflight", "preflight"), SideItem("start match", "match start"),
                SideItem("reset drone", "match reset"), SideItem("finish", "match finish"),
                SideItem("credit…", "credit ", True, "task"), SideItem("undo", "undo"), SideItem("score", "score"),
            ]),
        ]

    def on_connected(self) -> None:
        pass

    def on_telemetry(self, t) -> None:
        pass

    def tick(self) -> None:
        res = self.match.tick()
        if res is not None:
            self.app.executor.abort()
            self.app.console_write(f"MATCH OVER -- time up. Attempt {res.number}: {res.score} pts", "error")
            if self.app.backend.connected and self.app.backend.flying:
                self.app.console_write("landing (match time expired)", "warn")
                self.app.shell.run_line("land -e", record=False)
        try:
            self.app.query_one("#score-panel", ScorePanel).show(self)
        except Exception:  # noqa: BLE001
            pass

    def status_fragment(self, pal: dict) -> Text:
        t = Text(no_wrap=True)
        if self.match.state == "running":
            low = self.match.remaining < 30
            t.append(f" ⏱ {clock(self.match.remaining)} ", style=f"bold {pal['error'] if low else pal['success']}")
            t.append(f"SCORE {self.scorecard.total}", style=f"bold {pal['accent']}")
        elif self.match.state == "finished":
            t.append(f"MATCH {self.match.last_result.number}: {self.match.last_result.score} pts", style=pal["primary"])
        return t

    def _preflight(self):
        b = self.app.backend
        return run_preflight(self.app.poller.latest, b.connected, self.min_battery, self.confirmed)

    def _preflight_lines(self, results) -> list[str]:
        return [f"  {_ICON[r.status]} {r.item.label}" + (f"  [{r.item.manual_ref}]" if r.item.manual_ref else "")
                + (f"  -- {r.detail}" if r.status != "pass" and r.detail else "") for r in results]

    def start_mission(self, name: str, in_match: bool, skip_confirm: bool) -> None:
        app, b = self.app, self.app.backend
        if self.running:
            raise UsageError("a mission is already running -- `mission stop` first")
        r = self.find(name)
        if not r.ok:
            raise UsageError(f"{r.name}: {r.error}")
        if not b.connected:
            raise UsageError("not connected -- run `pair` first")
        if b.flying:
            raise UsageError("land first: routines start with the drone on the programming mat")
        if in_match and self.match.state == "running":
            raise UsageError("a match is already running -- `match finish` or `match abort` first")
        if in_match and self.match.attempts_left <= 0:
            raise UsageError(f"all {MAX_ATTEMPTS} attempts are used (`match new` starts a fresh event)")

        results = self._preflight()
        hard_fail = [x for x in results if x.status == "fail"]
        if hard_fail:
            for line in self._preflight_lines(hard_fail):
                app.console_write(line, "error")
            raise UsageError("preflight failed -- fix the ✗ items first (`preflight`)")

        real = app.KIND == "edu" and not app.demo
        if real and not skip_confirm:
            body = [f"Fly [b]{r.name}[/b] on the REAL drone" + (" as a timed MATCH attempt?" if in_match else " (practice)?"), ""]
            body += self._preflight_lines(results)
            body += ["", "The drone will take off and fly by itself. Keep clear; eye protection on.",
                     "[b]ctrl+x[/b] cuts the motors · [b]ctrl+l[/b] lands."]
            if not self.field.calibrated:
                body += ["", "[b]⚠ the field layout is NOT calibrated[/b] -- positions are placeholders."]
            body += ["", "Run = you confirm every ☐ item."]
            app.push_screen(ConfirmScreen("Start autonomous flight", body),
                            lambda ok: self._launch(r, in_match, results) if ok else app.console_write("cancelled", "dim"))
        else:
            self._launch(r, in_match, results)

    def _launch(self, r: Routine, in_match: bool, results) -> None:
        app, b = self.app, self.app.backend
        self.confirmed |= {x.item.key for x in results if x.status == "todo"}
        if in_match:
            try:
                self.match.start()
            except MatchError as exc:
                app.console_write(str(exc), "error")
                return
        else:
            self.practice = Scorecard()
        b.pose.reset()
        app.query_one("#map-panel", MapPanel).view.clear_trail()
        self.running, self.current_name = True, r.name
        if not self.field.calibrated:
            app.console_write("⚠ field is not calibrated: gate positions are placeholders", "warn")
        app.console_write(f"▶ mission '{r.name}' {'(MATCH, clock running)' if in_match else '(practice)'}", "ok")

        def log(msg: str) -> None:
            app.ui(app.console_write, f"  {msg}", "dim")

        def work() -> None:
            ctx = RoutineContext(
                b, app.executor,
                scorecard=None if in_match else self.practice,
                field=self.field,
                match=self.match if in_match else None,
                log=log,
            )
            try:
                r.fn(ctx)
            finally:
                app.ui(self._mission_done, r.name, ctx, in_match)

        app.executor.submit(f"mission {r.name}", work)

    def _mission_done(self, name: str, ctx: RoutineContext, in_match: bool) -> None:
        self.running = False
        card = self.scorecard
        app = self.app
        app.console_write(f"■ mission '{name}' ended -- claimed {card.total} pts"
                          f"{'' if not in_match else f' · {clock(self.match.remaining)} left on the clock'}", "ok")
        if ctx.rejected:
            app.console_write("no points (already maxed): " + ", ".join(ctx.rejected), "dim")
        if app.backend.flying:
            app.console_write("drone is still in the air -- `land` (routine did not land it)", "warn")
        app.console_write("credits are what the routine CLAIMED -- `undo` any gate it actually missed", "dim")
        if in_match and self.match.state == "running":
            app.console_write("match still running: `match finish` to bank it, or `match reset` + run again", "dim")

    def _register_commands(self, sh: Shell) -> None:
        R = sh.register
        R(Command("mission", self._cmd_mission, "mission list|run|dry|info|stop|reload", "autonomous routines", "autonomous", ("m",)))
        R(Command("match", self._cmd_match, "match start|reset|finish|abort|status|new", "180 s match clock + attempts", "autonomous"))
        R(Command("credit", self._cmd_credit, "credit <task>…", "award points for a task (judge)", "autonomous", ("c",)))
        R(Command("undo", self._cmd_undo, "undo", "remove the last credit", "autonomous"))
        R(Command("landing", self._cmd_landing, "landing pad|cube|bullseye|none", "set the landing result", "autonomous"))
        R(Command("score", self._cmd_score, "score", "print the scorecard", "autonomous", ("sc",)))
        R(Command("preflight", self._cmd_preflight, "preflight [confirm <item>|all]", "pre-flight checklist (manual 2.4.1)", "autonomous", ("pf",)))
        R(Command("field", self._cmd_field, "field [reload]", "show / reload the field layout", "autonomous"))
        sh.extra_completions["mission"] = lambda: ["list", "run", "dry", "info", "stop", "reload", *[r.name for r in self.routines]]
        sh.extra_completions["match"] = lambda: ["start", "reset", "finish", "abort", "status", "new", "piloting"]
        sh.extra_completions["credit"] = lambda: list(TASKS)
        sh.extra_completions["landing"] = lambda: list(_LANDING_WORDS)
        sh.extra_completions["preflight"] = lambda: ["confirm", "all"]

    def _out(self, text: str, kind: str = "info") -> None:
        self.app.console_write(text, kind)

    def _cmd_mission(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        sub = pos[0].lower() if pos else "list"
        rest = " ".join(pos[1:])
        if sub == "list":
            for r in self.routines:
                self._out(f"  {r.name:<22} {self.hints.get(r.name, ''):>5}  {r.description}", "info" if r.ok else "error")
            self._out("run: mission run <name> [-m] [-y]   (-m = timed match, -y = skip the confirmation)", "dim")
        elif sub == "run":
            if not rest:
                raise UsageError("missing routine name")
            self.start_mission(rest, in_match="m" in flags or "match" in flags, skip_confirm="y" in flags)
        elif sub == "dry":
            if not rest:
                raise UsageError("missing routine name")
            r = self.find(rest)
            rep = dry_run(r, self.field, flavor=self.app.KIND)
            self._out(f"dry run '{r.name}': {rep.points} pts predicted in {rep.duration_s:.0f} s "
                      f"({rep.points / max(1.0, rep.duration_s) * 60:.0f} pts/min)", "ok" if rep.ok else "error")
            if rep.error:
                self._out(f"  error: {rep.error}", "error")
            for w in rep.warnings:
                self._out(f"  ⚠ {w}", "warn")
            for s in rep.steps[:14]:
                self._out(f"    {s}", "dim")
            if len(rep.steps) > 14:
                self._out(f"    … {len(rep.steps) - 14} more steps", "dim")
        elif sub == "info":
            r = self.find(rest)
            self._out(f"{r.name}: {r.description}", "info")
            self._out(f"source: {r.source or 'built-in'}", "dim")
        elif sub == "stop":
            sh.run_line("stop", record=False)
        elif sub == "reload":
            self.field = load_field(self.app.field_path)
            self.reload_routines()
            self._set_map_markers()
            self.app.refresh_sidebar()
            self._out(f"reloaded {len(self.routines)} routine(s); field: {self.field.source}", "ok")
            for w in self.field.warnings():
                self._out(f"field: {w}", "warn")
        else:
            raise UsageError(f"unknown subcommand '{sub}'")

    def _cmd_match(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        m = self.match
        sub = pos[0].lower() if pos else "status"
        try:
            if sub == "start":
                results = self._preflight()
                if any(r.status == "fail" for r in results):
                    for line in self._preflight_lines([r for r in results if r.status == "fail"]):
                        self._out(line, "error")
                    raise UsageError("preflight failed -- fix the ✗ items first")
                m.start()
                self._out(f"MATCH {len(m.attempts) + 1}/{MAX_ATTEMPTS} STARTED -- {MATCH_SECONDS} s on the clock", "ok")
                self._out("fly with `mission run <name>` or credit tasks by hand; `match finish` ends it", "dim")
            elif sub == "reset":
                n = m.reset_drone()
                self.app.executor.abort()
                self.app.backend.pose.reset()
                self._out(f"reset #{n}: the clock keeps running. Put the drone back on the mat, then run again.", "warn")
                if self.app.backend.has(Cap.TELEPORT):
                    self._out("simulator: `resetpos` puts the sim drone back at its start", "dim")
            elif sub == "finish":
                landing = None
                if len(pos) > 1:
                    landing = _LANDING_WORDS.get(pos[1].lower(), "?")
                    if landing == "?":
                        raise UsageError("landing must be pad, cube, bullseye or none")
                res = m.finish(landing)
                self._out(f"attempt {res.number}/{MAX_ATTEMPTS}: {res.score} pts in {clock(res.duration)}"
                          f" · {res.resets} reset(s) · best {m.best_score} · skills {m.skills_total}", "ok")
            elif sub == "abort":
                m.abort()
                self._out("attempt discarded (does not count)", "warn")
            elif sub == "new":
                m.new_event()
                m.save()
                self._out("new event: attempts cleared", "warn")
            elif sub == "piloting":
                if len(pos) < 2:
                    raise UsageError("missing score")
                m.piloting_best = int(num(pos[1], "piloting score", 0, 500))
                self._out(f"best piloting score {m.piloting_best} · skills total {m.skills_total}", "ok")
            elif sub == "status":
                self._out(f"match: {m.state} · attempts used {len(m.attempts)}/{MAX_ATTEMPTS} · best {m.best_score}"
                          f" · skills {m.skills_total}", "info")
                for a in m.attempts:
                    self._out(f"  #{a.number}: {a.score} pts · {clock(a.duration)} · {a.resets} reset(s)"
                              f"{' · time up' if a.expired else ''}", "dim")
            else:
                raise UsageError(f"unknown subcommand '{sub}'")
        except MatchError as exc:
            raise UsageError(str(exc)) from None

    def _resolve_task(self, tok: str) -> str:
        t = tok.lower().replace("-", "_")
        keys = [*TASKS, *LANDINGS]
        if t in keys:
            return t
        norm = t.replace("_", "")
        hits = [k for k in keys if k.replace("_", "").startswith(norm)]
        if len(hits) == 1:
            return hits[0]
        raise UsageError(f"unknown task '{tok}'" + (f" (did you mean {', '.join(hits)}?)" if hits else "") + " -- see `score`")

    def _cmd_credit(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        if not pos:
            raise UsageError("missing task")
        for tok in pos:
            key = self._resolve_task(tok)
            res = self.match.credit(key, "manual") if self.match.state == "running" else self.scorecard.credit(key, source="manual")
            self._out(res.message, "ok" if res.ok else "warn")

    def _cmd_undo(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        res = self.scorecard.undo()
        self._out(res.message if res else "nothing to undo", "ok" if res else "warn")

    def _cmd_landing(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        if not pos or pos[0].lower() not in _LANDING_WORDS:
            raise UsageError("expected pad, cube, bullseye or none")
        res = self.scorecard.set_landing(_LANDING_WORDS[pos[0].lower()])
        self._out(res.message, "ok" if res.ok else "warn")

    def _cmd_score(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        card = self.scorecard
        for row in card.rows():
            if row.count:
                self._out(f"  {row.label:<42} {row.count}/{row.max_count}  +{row.earned}", "ok")
        self._out(f"  total {card.total}   (credits from routines are claims, not judged results)", "info")

    def _cmd_preflight(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        from .adc import PREFLIGHT_ITEMS

        if pos and pos[0].lower() == "confirm":
            keys = [i.key for i in PREFLIGHT_ITEMS if not i.auto]
            wanted = keys if len(pos) < 2 or pos[1].lower() == "all" else [k for k in keys if k.startswith(pos[1].lower())]
            if not wanted:
                raise UsageError("no such item -- " + ", ".join(keys))
            self.confirmed |= set(wanted)
        elif pos and pos[0].lower() == "all":
            self.confirmed |= {i.key for i in PREFLIGHT_ITEMS if not i.auto}
        elif pos and pos[0].lower() == "clear":
            self.confirmed.clear()
        results = self._preflight()
        for line in self._preflight_lines(results):
            self._out(line, "ok" if "✓" in line[:4] else "error" if "✗" in line[:4] else "warn")
        self._out("READY" if preflight_ok(results) else "not ready -- `preflight confirm all` once the ☐ items are really done", "ok" if preflight_ok(results) else "warn")

    def _cmd_field(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        if pos and pos[0].lower() == "reload":
            self.field = load_field(self.app.field_path)
            self._set_map_markers()
        f = self.field
        self._out(f"field: {f.source} · {'CALIBRATED' if f.calibrated else 'NOT calibrated (placeholder)'}", "ok" if f.calibrated else "warn")
        for name, el in f.elements.items():
            self._out(f"  {name:<15} x={el.x:>5.0f} y={el.y:>5.0f} z={el.z:>4.0f} heading={el.heading:>3.0f}", "dim")
        for w in f.warnings():
            self._out(f"⚠ {w}", "warn")
