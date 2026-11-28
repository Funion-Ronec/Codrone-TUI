# The bot's shell commands: obstacle-aware flying (nav, ahead, approach, scan, face, through, landon) and
# plain-English plans (ask, plan), live mode (the model writes guidance, the flight algorithm flies it), the
# algorithm's behaviours (explore, home, landspot), the language model and its API key (llm), the phone camera,
# plus `avoid` to give the ADC missions the same obstacle guard.

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .adc.field import FieldConfig, load_field
from .adc.routine import Routine, RoutineContext
from .backend import Cap
from .bot import BotContext, BotSettings, Navigator, Plan, TextPlanner, compile_routine, describe_scan
from .bot.camera import Camera, CameraError, safe_url
from .bot import behaviours
from .bot.guided import GuidedRun
from .bot.llm import API_PROVIDERS, DEFAULT_KEY_ENV, PROVIDERS, KeyStore, LlmError, make_client, mask, model_for
from .bot.ollama import OllamaError
from .bot.procedure import GATE_KINDS, MIN_APPROACH_CM, Step
from .commands import Command, Shell, UsageError, num
from .screens import SecretScreen
from .widgets.panels import SideItem


def attach(app) -> None:
    settings, problems = BotSettings.from_config(app.cfg.get("bot"))
    app.bot = BotController(
        app.shell,
        settings=settings,
        field=lambda: app.adc.field if app.adc is not None else None,
        adc=lambda: app.adc,
        field_path=app.field_path,
        ask_secret=lambda title, lines, label, done: app.push_screen(SecretScreen(title, lines, label), done),
        run_ui=app.ui,
    )
    for p in problems:
        app.console_write(f"config [bot]: {p}", "warn")
    if settings.guard_missions:
        app.bot.set_guard(True)


class BotController:
    def __init__(
        self,
        shell: Shell,
        *,
        settings: BotSettings | None = None,
        field: Callable[[], FieldConfig | None] | None = None,
        adc: Callable[[], object] | None = None,
        field_path: Path | None = None,
        planner: TextPlanner | None = None,
        background: bool = True,
        keys: KeyStore | None = None,
        ask_secret: Callable[..., object] | None = None,
        run_ui: Callable[..., object] | None = None,
    ) -> None:
        self.shell = shell
        self.settings = settings or BotSettings()
        self._field_fn = field or (lambda: None)
        self._adc_fn = adc or (lambda: None)
        self._field_path = field_path
        self._own_field: FieldConfig | None = None
        self.keys = keys or KeyStore()
        self.planner = planner or TextPlanner(self.settings, keys=self.keys)
        self._ask_secret = ask_secret  # opens the masked key dialog (None outside the app)
        self._run_ui = run_ui or (lambda fn, *a: fn(*a))  # live mode plans off the UI thread, then starts on it
        self.live_run: GuidedRun | None = None  # the current or last live flight, for the bot panel
        self.on_live_start: Callable[[], object] | None = None  # the app shows the bot panel
        self._key_cache: tuple[float, tuple] = (0.0, ())
        self.background = background  # tests run `ask` inline
        self.pending: Plan | None = None
        self.guarding = False
        self._planning = False
        self._lock = threading.Lock()
        self._register(shell)

    # wiring ----------------------------------------------------------------------------------------------

    @property
    def backend(self):
        return self.shell.backend

    @property
    def executor(self):
        return self.shell.host.executor

    @property
    def field(self) -> FieldConfig:
        f = self._field_fn()
        if f is None:
            if self._own_field is None:
                self._own_field = load_field(self._field_path)
            f = self._own_field
        return f

    def out(self, text: str, kind: str = "info") -> None:
        self.shell.out(text, kind)

    def _log(self, message: str) -> None:
        self.out(f"  {message}", "dim")

    def mission_context(self, *args, **kwargs) -> BotContext:
        # context factory for AdcController: missions and plans fly with the obstacle guard
        return BotContext(*args, settings=self.settings, guard=True, **kwargs)

    def set_guard(self, on: bool) -> None:
        adc = self._adc_fn()
        if adc is None:
            raise UsageError("mission features are not loaded")
        adc.context_factory = self.mission_context if on else RoutineContext
        self.guarding = on

    def sidebar_sections(self) -> list[tuple[str, list[SideItem]]]:
        return [("BOT", [
            SideItem("live…", "live ", True, "goal"), SideItem("live stop", "live stop"), SideItem("camera", "camera"),
            SideItem("explore…", "explore ", True, "s"), SideItem("home", "home"), SideItem("landspot", "landspot"),
            SideItem("ask…", "ask ", True, "say it"), SideItem("plan", "plan show"), SideItem("plan run", "plan run"),
            SideItem("scan", "scan"), SideItem("ahead…", "ahead ", True, "cm"),
            SideItem("approach…", "approach ", True, "cm"), SideItem("nav…", "nav ", True, "x y"),
            SideItem("llm status", "llm status"), SideItem("llm key", "llm key"),
        ])]

    def panel_state(self) -> dict:
        # everything the bot panel shows, as plain values; called from the UI timer four times a second
        s = self.settings
        now = time.monotonic()
        stamp, cached = self._key_cache
        if now - stamp > 2.0 or not cached or cached[0] != (s.llm, s.api_key_env):
            key, source = self.keys.lookup(s.llm, s.api_key_env) if s.llm in API_PROVIDERS else (None, "")
            cached = ((s.llm, s.api_key_env), (mask(key) if key else None), source)
            self._key_cache = (now, cached)
        state = {
            "provider": s.llm, "model": model_for(s) if s.llm != "off" else "",
            "needs_key": s.llm in API_PROVIDERS, "key": cached[1], "key_source": cached[2],
            "guard": self.guarding, "motion": s.motion, "camera": bool(s.camera_url), "planning": self._planning,
            "plan": None, "live": None,
        }
        p = self.pending
        if p is not None:
            state["plan"] = {"summary": p.summary or p.text, "steps": len(p.steps),
                             "seconds": p.dry.duration_s if p.dry is not None and p.dry.ok else None}
        run = self.live_run
        if run is not None and run.phase != "ready":
            state["live"] = {
                "goal": run.goal, "summary": run.plan.summary, "phase": run.phase,
                "steps": [(st.line(), status) for st, status in zip(run.steps, run.status)],
                "current": run.current,
                "elapsed": (now - run.started) if run.phase == "flying" else run.ended_s,
                "events": list(run.events)[-4:],
                "frames": None if run.frames is None else run.frames.saved,
                "result": None if run.phase != "ended" else {"reason": run.reason, "ok": run.ok,
                                                             "landed": run.landed_for_safety},
            }
        return state

    def _ctx(self) -> BotContext:
        adc = self._adc_fn()
        card = getattr(adc, "scorecard", None)
        return BotContext(self.backend, self.executor, scorecard=card, field=self.field, log=self._log,
                          settings=self.settings, guard=True)

    def _nav(self) -> Navigator:
        return Navigator(self.backend, self.executor, self.field, self.settings, log=self._log)

    # commands --------------------------------------------------------------------------------------------

    def _register(self, sh: Shell) -> None:
        R = sh.register
        G = "bot"
        R(Command("nav", self.h_nav, "nav <x> <y> [z] | nav <element>", "fly to a point, routing around obstacles", G,
                  requires=Cap.RANGE, needs_link=True, flight=True))
        R(Command("ahead", self.h_ahead, "ahead <cm> [speed]", "forward, stopping before obstacles", G,
                  requires=Cap.RANGE, needs_link=True, flight=True))
        R(Command("approach", self.h_approach, "approach <cm> [max_cm]", "fly forward until something is <cm> away", G,
                  requires=Cap.RANGE, needs_link=True, flight=True))
        R(Command("scan", self.h_scan, "scan", "turn in place and measure the way ahead", G,
                  requires=Cap.RANGE, needs_link=True, flight=True))
        R(Command("face", self.h_face, "face <deg>", "turn to a heading (0 = takeoff direction)", G,
                  needs_link=True, flight=True))
        R(Command("through", self.h_through, "through <element>", "fly through a field element (guarded)", G,
                  needs_link=True, flight=True))
        R(Command("landon", self.h_landon, "landon pad|bullseye|cube", "fly to a landing target and land", G,
                  needs_link=True, flight=True))
        R(Command("ask", self.h_ask, "ask [-p] <instructions…>", "turn plain English into a flight plan", G, raw=True))
        R(Command("plan", self.h_plan, "plan [show|run [-y]|save <file>|clear]", "the plan `ask` made", G))
        R(Command("avoid", self.h_avoid, "avoid [on|off]", "obstacle guard for autonomous missions", G))
        R(Command("live", self.h_live, "live [-y] <what you want> | live stop",
                  "the model turns your words into guidance; the flight algorithm flies it", G, requires=Cap.RANGE,
                  raw=True))
        R(Command("explore", self.h_explore, "explore <seconds> [top_cm]", "wander, turning away from obstacles (and up/down)", G,
                  requires=Cap.RANGE, needs_link=True, flight=True))
        R(Command("home", self.h_home, "home", "back to the takeoff point around obstacles", G,
                  requires=Cap.RANGE, needs_link=True, flight=True))
        R(Command("landspot", self.h_landspot, "landspot", "look around and land somewhere open", G,
                  requires=Cap.RANGE, needs_link=True, flight=True))
        R(Command("camera", self.h_camera, "camera [<url>|test|off]",
                  "a phone camera watching live flights: stills saved with the flight's log", G))
        R(Command("llm", self.h_llm, "llm [status|models|use <model>|provider <name>|key [clear]]",
                  "the language model: local Ollama or a hosted API with a key", G))
        sh.extra_completions["plan"] = lambda: ["show", "run", "save", "clear"]
        sh.extra_completions["through"] = lambda: sorted(n for n, el in self.field.elements.items() if el.kind in GATE_KINDS)
        sh.extra_completions["landon"] = lambda: ["pad", "bullseye", "cube"]
        sh.extra_completions["nav"] = lambda: sorted(self.field.elements)
        sh.extra_completions["avoid"] = lambda: ["on", "off"]
        sh.extra_completions["llm"] = lambda: ["status", "models", "use", "provider", "key"]
        sh.extra_completions["live"] = lambda: ["stop"]

    def _element(self, word: str):
        f = self.field
        name = word.lower()
        if name in f.elements:
            return f.elements[name]
        matches = sorted(n for n in f.elements if n.startswith(name))
        if len(matches) == 1:
            return f.elements[matches[0]]
        if matches:
            raise UsageError(f"'{word}' could be {', '.join(matches)}")
        raise UsageError(f"no field element '{word}' (known: {', '.join(sorted(f.elements))})")

    def h_nav(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        s = self.settings
        if len(pos) == 1:
            el = self._element(pos[0])
            x, y, z, label = el.x, el.y, (el.z if el.z > 0 else None), el.name
        elif len(pos) in (2, 3):
            fence = s.geofence_cm
            x = num(pos[0], "x (cm)", -fence, fence)
            y = num(pos[1], "y (cm)", -fence, fence)
            z = num(pos[2], "z (cm)", s.min_alt_cm, s.max_alt_cm) if len(pos) == 3 else None
            label = f"{x:g},{y:g}" + (f",{z:g}" if z is not None else "")
        else:
            raise UsageError("expected x y [z] (cm, takeoff point = 0 0) or a field element")
        if z is not None:
            z = max(s.min_alt_cm, min(s.max_alt_cm, z))

        def work() -> None:
            ctx = self._ctx()
            ctx.goto(x, y, z)
            p = self.backend.pose
            self.out(f"nav: at ({p.x:.0f}, {p.y:.0f}, {p.z:.0f}) after {ctx.nav.stats['legs']} leg(s)", "ok")

        sh.fly(f"nav {label}", work)

    def h_ahead(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        if not pos:
            raise UsageError("missing distance")
        cm = num(pos[0], "distance (cm)", 1, 1000)
        speed = num(pos[1], "speed", 0.1, 2.0) if len(pos) > 1 else None

        def work() -> None:
            moved = self._nav().forward(cm, speed)
            self.out(f"ahead: flew {moved:.0f} cm", "ok")

        sh.fly(f"ahead {cm:g}cm", work)

    def h_approach(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        if not pos:
            raise UsageError("missing distance")
        top = self.settings.sensor_max_cm - 10
        stop = num(pos[0], "distance (cm)", MIN_APPROACH_CM, top)
        limit = num(pos[1], "max distance (cm)", 5, 1000) if len(pos) > 1 else None

        def work() -> None:
            r = self._nav().approach(stop, limit)
            self.out(f"approach: stopped with {r.text(self.settings)} ahead", "ok" if r.hit else "warn")

        sh.fly(f"approach {stop:g}cm", work)

    def h_scan(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        def work() -> None:
            nav = self._nav()
            points = nav.scan()
            p = self.backend.pose
            self.out(f"scan from ({p.x:.0f}, {p.y:.0f}):", "ok")
            for line in describe_scan(points, self.settings):
                self.out(f"  {line}", "info")

        sh.fly("scan", work)

    def h_face(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        if not pos:
            raise UsageError("missing heading")
        deg = num(pos[0], "heading (deg)", -360, 360) % 360.0
        sh.fly(f"face {deg:g}°", lambda: self._ctx().face(deg))

    def h_through(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        if len(pos) != 1:
            raise UsageError("expected one element, e.g. `through red_arch`")
        el = self._element(pos[0])
        if el.kind not in GATE_KINDS:
            raise UsageError(f"{el.name} is not something you fly through")
        sh.fly(f"through {el.name}", lambda: self._ctx().through(el.name))

    def h_landon(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        target = pos[0].lower() if pos else ""
        if target not in ("pad", "bullseye", "cube"):
            raise UsageError("expected pad, bullseye or cube")
        sh.fly(f"landon {target}", lambda: self._ctx().land_on(target))

    def h_ask(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        text = pos[0].strip() if pos else ""
        if not text:
            raise UsageError("say what to do, e.g. `ask take off, fly through the red arch, land on the pad`")
        with self._lock:
            if self._planning:
                raise UsageError("still working on the last request")
            self._planning = True
        use_llm = "p" not in flags and "phrases" not in flags
        b = self.backend
        flavor = getattr(b, "flavor", b.kind)
        field = self.field

        def work() -> None:
            try:
                plan = self.planner.plan(text, field, use_llm=use_llm, flavor=flavor, notify=lambda m: self.out(m, "dim"))
                self.show(plan)
                if plan.runnable:
                    self.pending = plan
            except Exception as exc:  # noqa: BLE001 - never let a planner bug take the console down
                self.out(f"ask failed: {type(exc).__name__}: {exc}", "error")
            finally:
                self._planning = False

        if self.background:
            threading.Thread(target=work, name="bot-ask", daemon=True).start()
        else:
            work()

    def show(self, plan: Plan, live: bool = False) -> None:
        ok = plan.runnable
        head = f"plan from {plan.source or '?'} ({plan.seconds:.1f} s)"
        self.out(f"{head}: {plan.summary}" if plan.summary else head, "ok" if ok else "warn")
        for i, line in enumerate(plan.lines(), 1):
            self.out(f"  {i:>2}  {line}", "info")
        for n in plan.notes:
            self.out(f"  note: {n}", "dim")
        for q in plan.questions:
            self.out(f"  ? {q}", "warn")
        for w in plan.warnings:
            self.out(f"  ⚠ {w}", "warn")
        for e in plan.errors:
            self.out(f"  ✗ {e}", "error")
        if plan.dry is not None and plan.dry.ok:
            self.out(f"  simulated against field.toml: about {plan.dry.duration_s:.0f} s of flight", "dim")
        again = "live" if live else "ask"
        if ok and not live:
            self.out("`plan run` flies it · `plan save <file>` keeps it · `plan clear` drops it", "dim")
        elif ok:
            pass
        elif not plan.steps and not plan.errors:
            self.out(f"nothing to fly -- add detail and `{again}` again", "warn")
        else:
            self.out(f"this can't be flown -- rephrase and `{again}` again", "warn")

    def h_plan(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        sub = pos[0].lower() if pos else "show"
        p = self.pending
        if sub == "show":
            if p is None:
                self.out("no plan -- `ask <instructions>` makes one", "dim")
            else:
                self.show(p)
        elif sub == "clear":
            self.pending = None
            self.out("plan cleared", "dim")
        elif sub == "save":
            if p is None:
                raise UsageError("no plan to save")
            if len(pos) < 2:
                raise UsageError("missing file name")
            path = Path(pos[1])
            header = [f"# plan: {p.summary or p.text}", f"# asked: {p.text}",
                      f"# made by {p.source} on {time.strftime('%Y-%m-%d %H:%M')}; run it with `source {path}`", ""]
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(header + p.lines()) + "\n", encoding="utf-8")
            except OSError as exc:
                raise UsageError(f"cannot write {path}: {exc.strerror or exc}") from None
            self.out(f"saved {len(p.steps)} step(s) to {path}", "ok")
        elif sub == "run":
            if p is None:
                raise UsageError("no plan -- `ask <instructions>` first")
            self.run_plan(p, skip_confirm="y" in flags)
        else:
            raise UsageError("expected show, run, save or clear")

    def run_plan(self, plan: Plan, skip_confirm: bool = False) -> None:
        if not plan.runnable:
            raise UsageError("this plan can't be flown")
        self._start(compile_routine(plan), skip_confirm)

    def _start(self, routine: Routine, skip_confirm: bool = False) -> None:
        adc = self._adc_fn()
        if adc is not None and hasattr(adc, "start_routine"):
            # same path as `mission run`: preflight, the real-drone confirmation, the map trail, mission stop
            adc.start_routine(routine, in_match=False, skip_confirm=skip_confirm, context_factory=self.mission_context)
            return
        b = self.backend
        if not b.connected:
            raise UsageError("not connected -- run `pair` first")
        if b.flying:
            raise UsageError("land first: the bot starts with the drone on the ground")

        def work() -> None:
            b.pose.reset()
            routine.fn(BotContext(b, self.executor, field=self.field, log=self._log, settings=self.settings))
            self.out(f"■ {routine.name} finished", "ok")

        self.shell.fly(routine.name, work)

    # live mode -------------------------------------------------------------------------------------------

    def h_live(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        goal = pos[0].strip() if pos else ""
        if goal.lower() == "stop":
            n = self.executor.abort()
            self.out("live: stopped -- the drone hovers; `land` or ctrl+l to bring it down" if n
                     else "live: nothing is flying", "warn" if n else "dim")
            return
        if not goal:
            raise UsageError("say what you want, e.g. `live fly around avoiding the walls for 20 s, then land somewhere open`")
        s = self.settings
        if s.llm == "off":
            raise UsageError('live mode needs a language model ([bot] llm is "off"); `ask -p` uses the phrase parser')
        b = self.backend
        if not b.connected:
            raise UsageError("not connected -- run `pair` first")
        if b.flying:
            raise UsageError("land first: live mode starts with the drone on the ground")
        camera = None
        if s.camera_url:
            try:
                camera = Camera(s.camera_url, s.camera_timeout_s)
            except CameraError as exc:
                raise UsageError(str(exc)) from None
        with self._lock:
            if self._planning:
                raise UsageError("still working on the last request")
            self._planning = True
        flavor = getattr(b, "flavor", b.kind)
        field = self.field
        record_dir = self.shell.log_path.parent if self.shell.log_enabled else None
        if self.on_live_start is not None:
            self.on_live_start()

        def work() -> None:
            try:
                self.out(f"live: {s.llm} {model_for(s)} is writing the guidance…", "dim")
                plan = self.planner.plan(goal, field, use_llm=True, flavor=flavor, notify=lambda m: self.out(m, "dim"))
                self.show(plan, live=True)
                if not plan.runnable:
                    return
                self.pending = plan  # `plan run` flies it again
                run = GuidedRun(plan, s, say=self.out, record_dir=record_dir, camera=camera)
                self.live_run = run
                self._run_ui(self._start_live, run, "y" in flags)
            except Exception as exc:  # noqa: BLE001 - never let a planner bug take the console down
                self.out(f"live failed: {type(exc).__name__}: {exc}", "error")
            finally:
                self._planning = False

        if self.background:
            threading.Thread(target=work, name="bot-live", daemon=True).start()
        else:
            work()

    def _start_live(self, run: GuidedRun, skip_confirm: bool) -> None:
        def fly(ctx) -> None:
            self.out(f"live: flying {len(run.steps)} step(s) -- the algorithm decides the moves; no model calls now", "ok")
            self.out("  esc / `live stop` stops it (hovers) · ctrl+l lands · ctrl+x cuts the motors", "dim")
            run.fly(ctx)
            kind = "ok" if run.ok else "warn"
            self.out(f"live: {run.reason}" + (" (landed for safety)" if run.landed_for_safety else ""), kind)
            if run.record_path is not None:
                self.out(f"  flight saved to {run.record_path}", "dim")

        title = run.plan.summary or run.goal
        short = title if len(title) <= 40 else title[:39] + "…"
        try:
            self._start(Routine(name=f"live: {short}", description=run.goal, fn=fly, tags=("bot", "live")),
                        skip_confirm=skip_confirm)
        except UsageError as exc:
            self.out(f"live: {exc}", "error")

    def h_explore(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        if not pos:
            raise UsageError("missing seconds, e.g. `explore 20`")
        seconds = num(pos[0], "seconds", 3, 300)
        s = self.settings
        top = num(pos[1], "top height (cm)", behaviours.EXPLORE_LOW_CM + 15, s.max_alt_cm) if len(pos) > 1 else None
        sh.fly(f"explore {seconds:g}s" + (f" up to {top:g}cm" if top else ""),
               lambda: behaviours.explore(self._ctx(), seconds, top_cm=top))

    def h_home(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        sh.fly("home", lambda: behaviours.home(self._ctx()))

    def h_landspot(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        sh.fly("landspot", lambda: behaviours.land_spot(self._ctx()))

    def h_camera(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        s = self.settings
        word = pos[0] if pos else ""
        if word.lower() == "off":
            s.camera_url = ""
            self.out("camera off: live mode saves no frames", "ok")
            return
        if word and word.lower() not in ("test", "status"):
            try:
                cam = Camera(word, s.camera_timeout_s)
            except CameraError as exc:
                raise UsageError(str(exc)) from None
            s.camera_url = cam.url
            self.out(f"camera: {safe_url(cam.url)} for this session (config.toml [bot] camera_url keeps it)", "ok")
            word = "test"
        if not s.camera_url:
            self.out("camera off. `camera http://<phone-ip>:8080/shot.jpg` (IP Webcam on Android) or any JPEG/MJPEG URL;"
                     " live mode then saves stills (each step, and every few seconds) next to its log", "info")
            return
        if word.lower() != "test":
            self.out(f"camera: {safe_url(s.camera_url)} · `camera test` grabs one · `camera off`", "info")
            return
        cam = Camera(s.camera_url, s.camera_timeout_s)
        folder = self.shell.log_path.parent

        def work() -> None:
            t0 = time.monotonic()
            try:
                data, ext = cam.snapshot()
            except CameraError as exc:
                self.out(f"camera: {exc}", "error")
                return
            path = folder / f"camera-test-{time.strftime('%Y_%m_%d_%H_%M_%S')}.{ext}"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except OSError as exc:
                self.out(f"camera: got a picture but could not save it ({exc})", "error")
                return
            self.out(f"camera ok: {len(data) // 1024} KB in {time.monotonic() - t0:.1f} s, saved to {path}", "ok")

        self.out(f"camera: grabbing a frame from {safe_url(s.camera_url)}…", "dim")
        if self.background:
            threading.Thread(target=work, name="bot-camera", daemon=True).start()
        else:
            work()

    def h_avoid(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        word = pos[0].lower() if pos else ""
        if word not in ("", "on", "off"):
            raise UsageError("expected on or off")
        if word:
            self.set_guard(word == "on")
        state = "ON: missions stop before obstacles and route around them" if self.guarding else \
            "OFF: missions fly by dead reckoning only"
        self.out(f"mission obstacle guard {state}", "ok" if self.guarding else "info")

    def h_llm(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        s = self.settings
        sub = pos[0].lower() if pos else "status"
        if sub == "use":
            if len(pos) < 2:
                raise UsageError("missing model name, e.g. `llm use qwen3.5:9b` or `llm use claude-haiku-5-5`")
            if s.llm == "ollama":
                s.model = pos[1]
            else:
                s.api_model = pos[1]
            self.out(f"{s.llm} model: {pos[1]} for this session (config.toml [bot] "
                     f"{'model' if s.llm == 'ollama' else 'api_model'} keeps it)", "ok")
            return
        if sub == "provider":
            choices = (*PROVIDERS, "off")
            if len(pos) < 2 or pos[1].lower() not in choices:
                raise UsageError(f"expected one of {', '.join(choices)}")
            s.llm = pos[1].lower()
            self.out(f"language model: {s.llm}" + (f" ({model_for(s) or 'no model set: `llm use <model>`'})"
                                                   if s.llm != "off" else "") + " for this session", "ok")
            if s.llm in API_PROVIDERS and not self.keys.lookup(s.llm, s.api_key_env)[0]:
                self.out(f"no {s.llm} API key yet: `llm key` enters one", "warn")
            return
        if sub == "key":
            self._key_command(pos[1:])
            return
        if sub not in ("status", "models"):
            raise UsageError("expected status, models, use <model>, provider <name> or key")
        if s.llm == "off":
            self.out('the language model is off ([bot] llm = "off"): `ask` only understands simple phrases', "warn")
            return
        if sub == "status" and s.llm in API_PROVIDERS:
            key, source = self.keys.lookup(s.llm, s.api_key_env)
            self.out(f"{s.llm}: model {model_for(s) or '(none -- `llm use <model>`)'}; API key "
                     + (f"{mask(key)} from {source}" if key else "NOT set (`llm key`)"), "ok" if key else "warn")
            if not key:
                return

        def work() -> None:
            try:
                names = make_client(s, self.keys, timeout_s=8.0).models()
            except OllamaError as exc:
                self.out(str(exc), "error")
                return
            model = model_for(s)
            if sub == "models":
                for n in names:
                    self.out(f"  {n}{'   <- in use' if n in (model, model + ':latest') else ''}", "info")
                if not names:
                    self.out("no models found" + (" (`ollama pull <model>`)" if s.llm == "ollama" else ""), "warn")
                return
            ready = model in names or f"{model}:latest" in names
            if s.llm == "ollama":
                self.out(f"Ollama at {s.ollama_url}: {len(names)} model(s); {model} "
                         + ("is ready" if ready else f"is NOT pulled (`ollama pull {model}`)"), "ok" if ready else "warn")
            else:
                self.out(f"{s.llm} API reachable, key accepted; {len(names)} model(s)"
                         + ("" if ready or not names else f"; '{model}' isn't listed (`llm models`)"),
                         "ok" if ready or not names else "warn")

        if self.background:
            threading.Thread(target=work, name="bot-llm", daemon=True).start()
        else:
            work()

    def _key_command(self, args: list[str]) -> None:
        s = self.settings
        provider = s.llm if s.llm in API_PROVIDERS else None
        if args and args[0].lower() in API_PROVIDERS:
            provider, args = args[0].lower(), args[1:]
        if provider is None:
            raise UsageError("pick a hosted provider first: `llm provider anthropic` (or openai), or `llm key anthropic`")
        if args and args[0].lower() == "clear":
            gone = self.keys.forget(provider)
            self.out(f"{provider} API key forgotten (session and saved file)" if gone
                     else f"no {provider} key was stored here", "ok" if gone else "dim")
            env = s.api_key_env or DEFAULT_KEY_ENV[provider]
            if self.keys.lookup(provider, s.api_key_env)[0]:
                self.out(f"${env} still provides one", "warn")
            return
        if args:  # typed inline: already masked in the console, history and log by Shell
            self._store_key(provider, args[0], save=False)
            return
        if self._ask_secret is None:
            raise UsageError("no dialog here: use `llm key <key>` or set the environment variable")
        lines = [f"Paste your {provider} API key. It stays in memory for this session.",
                 f"Tick the box to keep it in {self.keys.path} (only your user can read it).",
                 f"Or set ${s.api_key_env or DEFAULT_KEY_ENV[provider]} instead. The key is never written to config.toml"
                 " or the logs."]
        self._ask_secret(f"{provider} API key", lines, "remember on this computer",
                         lambda result: result and self._store_key(provider, result[0], save=bool(result[1])))

    def _store_key(self, provider: str, key: str, save: bool) -> None:
        try:
            self.keys.set(provider, key, save=save)
        except (LlmError, OSError) as exc:
            self.out(f"key not stored: {exc}", "error")
            return
        where = f"saved to {self.keys.path}" if save else "for this session"
        self.out(f"{provider} API key {mask(key)} {where}", "ok")
        if self.settings.llm != provider:
            self.out(f"`llm provider {provider}` switches the bot to it", "dim")
