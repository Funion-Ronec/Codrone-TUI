"""The Textual application shared by the CoDrone EDU TUI and the CoDrone Simulator TUI.

Threading model (important):
    * the UI thread only draws and parses; it never calls a blocking drone method
    * FlightExecutor runs queued commands/missions on one worker thread
    * TelemetryPoller samples sensors on another
    * both report back through ``App.ui()``, which hops onto the UI thread
"""

from __future__ import annotations

import re
import threading
import time
from functools import partial
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widgets import Footer, Input

from . import __version__
from .backend import BackendError, DroneBackend, FlightExecutor, Job, Telemetry, TelemetryPoller
from .backend.edu import find_controller_ports
from .commands import Host, Shell
from .config import PROJECT_ROOT, load_config
from .screens import ConfirmScreen, HelpScreen
from .widgets.graph import palette
from .widgets.pilot import PilotPanel
from .widgets.score import ScorePanel
from .widgets.panels import (
    CmdInput,
    ConsolePanel,
    MapPanel,
    MetaPanel,
    Panel,
    ProcessesPanel,
    SideItem,
    SidebarPanel,
    StatusBar,
    TelemetryPanel,
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

THEMES = [
    # EDU = cyan/amber, sim = violet/green. these can never look alike or someone gonna mistake the real drone for the sim
    Theme(name="codrone-edu", primary="#4cc9f0", secondary="#f72585", accent="#ffb703", success="#06d6a0",
          warning="#ffd166", error="#ef476f", foreground="#dbe7f0", background="#0a0e13", surface="#10161d",
          panel="#16202b", dark=True),
    Theme(name="codrone-sim", primary="#b388ff", secondary="#ff6ec7", accent="#7ee787", success="#7ee787",
          warning="#f2cc60", error="#ff7b72", foreground="#e6e1f5", background="#0d0b14", surface="#14111f",
          panel="#1b1729", dark=True),
    Theme(name="btop", primary="#5ec962", secondary="#21918c", accent="#fde725", success="#5ec962",
          warning="#fde725", error="#e76f51", foreground="#cfd8dc", background="#000000", surface="#0b0b0b",
          panel="#141414", dark=True),
]
CYCLE_AFTER_OWN = ["btop", "catppuccin-mocha", "tokyo-night", "gruvbox", "nord", "textual-light"]

PANELS = {
    "console": ("console-panel", "1"), "sidebar": ("sidebar", "2"), "telemetry": ("telemetry-panel", "3"),
    "processes": ("proc-panel", "4"), "map": ("map-panel", "5"), "score": ("score-panel", "6"),
    "meta": ("meta-panel", "7"), "pilot": ("pilot-panel", "8"),
}
LAYOUTS = {
    "full": {"console", "sidebar", "telemetry", "processes", "map", "score", "meta", "pilot"},
    "flight": {"console", "telemetry", "processes", "map"},
    "pilot": {"console", "telemetry", "map", "pilot"},
    "mission": {"console", "processes", "map", "score"},
    "minimal": {"console", "telemetry"},
    "focus": {"console"},
}


class ShellCommandsProvider(Provider):
    """Puts every shell command in the ctrl+p palette."""

    async def discover(self) -> Hits:
        app = self.app
        for c in sorted(app.shell.commands.values(), key=lambda c: (c.group, c.name)):
            if app.shell.available(c):
                yield DiscoveryHit(c.usage, partial(app.palette_run, c), help=c.summary)

    async def search(self, query: str) -> Hits:
        app = self.app
        matcher = self.matcher(query)
        for c in app.shell.commands.values():
            if not app.shell.available(c):
                continue
            score = matcher.match(c.usage)
            if score > 0:
                yield Hit(score, matcher.highlight(c.usage), partial(app.palette_run, c), help=c.summary)


class _AppHost(Host):
    """Adapter so the (UI-agnostic) Shell can drive the Textual app."""

    def __init__(self, app: "CodroneApp") -> None:
        self.app = app

    @property
    def backend(self) -> DroneBackend:  # type: ignore[override]
        return self.app.backend

    @property
    def executor(self) -> FlightExecutor:  # type: ignore[override]
        return self.app.executor

    def out(self, text: str, kind: str = "info") -> None:
        self.app.ui(self.app.console_write, text, kind)

    def clear_console(self) -> None:
        self.app.ui(self.app.console_panel.clear)

    def connect_backend(self, port: str | None) -> None:
        self.app.connect_backend(port)

    def disconnect_backend(self) -> None:
        self.app.disconnect_backend()

    def request_exit(self, land_first: bool) -> None:
        self.app.request_exit(land_first)

    def set_theme(self, name: str | None) -> None:
        self.app.ui(self.app.set_theme_by_name, name)

    def apply_layout(self, name: str | None) -> None:
        self.app.ui(self.app.apply_layout, name)

    def toggle_panel(self, name: str) -> None:
        self.app.ui(self.app.toggle_panel, name)

    def latest_telemetry(self):
        return self.app.poller.latest

    def list_ports(self):
        return find_controller_ports()


class CodroneApp(App):
    CSS_PATH = "styles.tcss"
    ENABLE_COMMAND_PALETTE = True
    COMMANDS = App.COMMANDS | {ShellCommandsProvider}

    KIND = "base"
    TITLE_TEXT = "CoDrone"
    OWN_THEME = "codrone-edu"
    BANNER_FILE = PROJECT_ROOT / "CodroneCliAscii.txt"

    BINDINGS = [
        Binding("ctrl+x", "estop", "ESTOP", priority=True, key_display="^x"),
        Binding("ctrl+l", "land_now", "LAND", priority=True, key_display="^l"),
        Binding("i,colon,slash", "focus_input", "type", key_display="i"),
        Binding("escape", "soft_stop", "stop", show=False),
        Binding("1", "toggle('console')", "", show=False),
        Binding("2", "toggle('sidebar')", "", show=False),
        Binding("3", "toggle('telemetry')", "", show=False),
        Binding("4", "toggle('processes')", "", show=False),
        Binding("5", "toggle('map')", "", show=False),
        Binding("6", "toggle('score')", "", show=False),
        Binding("7", "toggle('meta')", "", show=False),
        Binding("8", "toggle('pilot')", "", show=False),
        Binding("p", "cycle_layout", "layout"),
        Binding("t", "cycle_theme", "theme"),
        Binding("question_mark", "help", "help", key_display="?"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(
        self,
        backend: DroneBackend,
        *,
        config: dict | None = None,
        port: str | None = None,
        autoconnect: bool = False,
        demo: bool = False,
        theme: str | None = None,
        field_path: Path | None = None,
        command_log: bool | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.cfg = config or load_config()
        self.port = port or (self.cfg.get(self.KIND, {}).get("port") or None)
        self.autoconnect = autoconnect or demo
        self.demo = demo
        self.requested_theme = theme or self.cfg["ui"].get("theme") or None
        self.field_path = field_path
        self.adc_store: Path | None = None
        self._ui_thread = threading.get_ident()
        self._started = time.time()
        self._layout = self.cfg["ui"].get("layout", "auto")
        self._exiting = False
        self._status_sig: tuple | None = None
        self.commands_run = 0
        self.errors_seen = 0

        self.executor = FlightExecutor(self._on_job_event)
        self.poller = TelemetryPoller(
            backend, hz=float(self.cfg["ui"].get("poll_hz", 5.0)),
            on_sample=lambda t: self.ui(self.on_telemetry, t),
            on_error=lambda m: self.ui(self.console_write, f"telemetry: {m}", "warn"),
        )
        if command_log is None:
            command_log = bool(self.cfg.get("log", {}).get("commands", True))
        self.shell = Shell(_AppHost(self), log_enabled=command_log)
        self.adc = None
        self.pilot = None

    def ui(self, fn, *args) -> None:
        """Run ``fn`` on the UI thread from any thread."""
        if threading.get_ident() == self._ui_thread:
            fn(*args)
        elif self.is_running:
            try:
                self.call_from_thread(fn, *args)
            except Exception:  # noqa: BLE001
                pass

    @property
    def console_panel(self) -> ConsolePanel:
        return self.query_one("#console-panel", ConsolePanel)

    def console_write(self, text: str, kind: str = "info") -> None:
        try:
            self.console_panel.write(text, kind)
        except Exception:  # noqa: BLE001
            return
        if kind == "error":
            self.errors_seen += 1

    @property
    def mode(self) -> str:
        if self.adc is not None and self.adc.running:
            return "AUTO"
        if self.pilot is not None and self.pilot.active:
            return "PILOT"
        return "INSERT" if isinstance(self.focused, Input) else "NORMAL"

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status")
        with Horizontal(id="main"):
            yield SidebarPanel(id="sidebar")
            yield ConsolePanel(self.shell, id="console-panel")
            yield TelemetryPanel(self.KIND, id="telemetry-panel")
        with Horizontal(id="bottom"):
            yield ProcessesPanel(id="proc-panel")
            yield MapPanel(id="map-panel")
            yield ScorePanel(id="score-panel")
            yield MetaPanel(id="meta-panel")
            yield PilotPanel(id="pilot-panel")
        yield Footer()

    def on_mount(self) -> None:
        for th in THEMES:
            self.register_theme(th)
        self.theme = self.requested_theme if self.requested_theme in self.available_themes else self.OWN_THEME
        self.begin_capture_print(self, stdout=True, stderr=True)
        self.title = f"{self.TITLE_TEXT} TUI"
        self.sub_title = "DEMO (mock drone)" if self.demo else ""

        self.query_one("#sidebar", SidebarPanel).set_sections(self.sidebar_sections())
        self._print_banner()
        if "_error" in self.cfg:
            self.console_write(f"config: {self.cfg['_error']} (using defaults)", "warn")
        try:
            from . import adc_ui

            adc_ui.attach(self)
        except Exception as exc:  # noqa: BLE001
            self.console_write(f"mission features unavailable: {type(exc).__name__}: {exc}", "warn")
        try:
            from . import pilot_ui

            pilot_ui.attach(self)
        except Exception as exc:  # noqa: BLE001
            self.console_write(f"PILOT mode unavailable: {type(exc).__name__}: {exc}", "warn")

        self.apply_layout(self._layout)
        self.set_interval(0.25, self._tick)
        self.set_focus(None)
        if self.autoconnect:
            self.shell.run_line("pair" + (f" {self.port}" if self.port else ""), record=False)
        else:
            self.console_write("type `pair` to connect · `help` for commands · ctrl+x = EMERGENCY STOP", "dim")
        if self.pilot is not None:
            self.console_write(f"press {self.pilot.keymap.keys.get('toggle', 'f').upper()} for PILOT mode "
                               "(fly with the keyboard) · `pilot keys` lists / rebinds them", "dim")

    def on_resize(self, event: events.Resize) -> None:
        if self._layout == "auto":
            self.apply_layout("auto")

    def _print_banner(self) -> None:
        try:
            art = self.BANNER_FILE.read_text(encoding="utf-8").rstrip("\n").splitlines()
        except OSError:
            art = []
        pal = palette(self)
        for line in art:
            self.console_panel.log_widget.write(Text(line, style=pal["primary"]))
        self.console_write(f"{self.TITLE_TEXT} TUI v{__version__}"
                           + ("  [DEMO: mock drone, nothing is flying]" if self.demo else ""), "ok")

    def quick_sections(self) -> list[tuple[str, list[SideItem]]]:
        return [
            ("LINK", [SideItem("pair", "pair"), SideItem("status", "status"), SideItem("disconnect", "disconnect")]),
            ("FLIGHT", [
                SideItem("takeoff", "takeoff"), SideItem("land", "land"), SideItem("hover 2s", "hover 2"),
                SideItem("forward…", "forward ", True, "cm"), SideItem("back…", "backward ", True, "cm"),
                SideItem("left…", "left ", True, "cm"), SideItem("right…", "right ", True, "cm"),
                SideItem("up…", "up ", True, "cm"), SideItem("down…", "down ", True, "cm"),
                SideItem("turn…", "turn ", True, "deg"),
                SideItem("pilot (keys)", "pilot"),
            ]),
        ]

    def sidebar_sections(self) -> list[tuple[str, list[SideItem]]]:
        sections = self.quick_sections()
        if self.adc is not None:
            sections += self.adc.sidebar_sections()
        return sections

    def refresh_sidebar(self) -> None:
        self.query_one("#sidebar", SidebarPanel).set_sections(self.sidebar_sections())

    def on_sidebar_panel_chosen(self, msg: SidebarPanel.Chosen) -> None:
        if msg.item.prefill:
            self.prefill(msg.item.command)
        else:
            self.shell.run_line(msg.item.command)

    def prefill(self, text: str) -> None:
        inp = self.console_panel.input
        inp.value = text
        inp.focus()
        inp.action_end()

    def palette_run(self, cmd) -> None:
        if cmd.usage.split()[0] == cmd.usage:
            self.shell.run_line(cmd.name)
        else:
            self.prefill(cmd.name + " ")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        if isinstance(event.input, CmdInput):
            event.input.reset_history()
        if not line:
            return
        self.commands_run += 1
        self.console_write(f"❯ {line}", "cmd")
        self.shell.run_line(line)

    def on_cmd_input_leave(self, _msg: CmdInput.Leave) -> None:
        self.set_focus(None)

    def on_key(self, event: events.Key) -> None:
        if self.pilot is not None and self.mode == "NORMAL" and self.pilot.is_toggle(event.key):
            event.stop()
            self.pilot.enter()

    def on_print(self, event: events.Print) -> None:
        for raw in event.text.splitlines():
            text = _ANSI.sub("", raw).strip()
            if text:
                self.console_write(f"lib: {text}", "dim")

    def connect_backend(self, port: str | None) -> None:
        if self.backend.connected:
            self.console_write("already connected -- `disconnect` first", "warn")
            return
        self.console_write(f"connecting to {self.backend.title}…", "dim")

        def work() -> None:
            desc = self.backend.connect(port)
            self.ui(self.on_connected, desc)

        self.executor.submit("pair", work)

    def on_connected(self, desc: str) -> None:
        self.console_write(f"connected: {desc}", "ok")
        self.poller.start()
        self.query_one("#map-panel", MapPanel).view.clear_trail()
        if self.adc is not None:
            self.adc.on_connected()

    def disconnect_backend(self) -> None:
        self.executor.abort()
        self.backend.disconnect()
        self.console_write("disconnected", "warn")

    def _on_job_event(self, kind: str, job: Job) -> None:
        self.ui(self._job_event_ui, kind, job)

    def _job_event_ui(self, kind: str, job: Job) -> None:
        if job.label in ("pair",) and kind == "finished":
            return
        if kind == "finished":
            if job.elapsed >= 0.3:
                self.console_write(f"✓ {job.label}  ({job.elapsed:.1f}s)", "ok")
        elif kind == "failed":
            self.console_write(f"✗ {job.label}: {job.error}", "error")
        elif kind == "cancelled":
            self.console_write(f"⊘ {job.label} cancelled", "warn")
        try:
            self.query_one("#proc-panel", ProcessesPanel).show(self.executor)
        except Exception:  # noqa: BLE001
            pass

    def on_telemetry(self, t: Telemetry) -> None:
        try:
            self.query_one("#telemetry-panel", TelemetryPanel).update_from(t, self.poller.series)
            view = self.query_one("#map-panel", MapPanel).view
            p = self.backend.pose
            if t.pos_cm is not None and not t.pos_is_estimate:
                view.set_pose(t.pos_cm[0], t.pos_cm[1], t.pos_cm[2], p.heading, estimated=False)
            else:
                view.set_pose(p.x, p.y, p.z, p.heading, estimated=True)
        except Exception:  # noqa: BLE001
            pass
        if self.adc is not None:
            self.adc.on_telemetry(t)

    def _tick(self) -> None:
        try:
            status = self.query_one("#status", StatusBar)
        except Exception:  # noqa: BLE001
            return
        pal = palette(self)
        b = self.backend
        t = self.poller.latest
        left = Text(no_wrap=True, overflow="ellipsis")
        left.append(f" {self.TITLE_TEXT.upper()} ", style=f"bold #000000 on {pal['accent']}")
        if self.demo:
            left.append(" DEMO ", style="bold #000000 on #ffd166")
        left.append("  ")
        if b.connected:
            left.append("● LINKED", style=f"bold {pal['success']}")
        else:
            left.append("○ OFFLINE", style=f"bold {pal['error']}")
        if t is not None and b.connected:
            if t.battery is not None:
                colour = pal["success"] if t.battery >= 50 else pal["warning"] if t.battery >= 25 else pal["error"]
                left.append(f"   BAT {t.battery}%", style=colour)
            if t.flight_state:
                left.append(f"   {t.flight_state.upper()}", style=pal["primary"])
        if b.flying:
            left.append("   ✈ FLYING", style=f"bold {pal['accent']}")
        if self.executor.busy:
            left.append(f"   ⏵ {len(self.executor.pending) + (1 if self.executor.current else 0)} job(s)", style=pal["warning"])

        right = Text(no_wrap=True, justify="right")
        if self.adc is not None:
            right.append_text(self.adc.status_fragment(pal))
            right.append("  ")
        mode = self.mode
        chip = {"NORMAL": pal["primary"], "INSERT": pal["success"], "AUTO": pal["warning"], "PILOT": pal["accent"]}[mode]
        right.append(f" {mode} ", style=f"bold #000000 on {chip}")
        up = int(time.time() - self._started)
        right.append(f"  {up // 60:02d}:{up % 60:02d}", style=pal["muted"])

        sig = (left.plain, right.plain, self.theme, self.size.width)
        if sig != self._status_sig:
            self._status_sig = sig
            grid = Table.grid(expand=True)
            grid.add_column(ratio=1)
            grid.add_column(justify="right", no_wrap=True)
            grid.add_row(left, right)
            status.update(grid)

        if self.adc is not None:
            self.adc.tick()
        try:
            self.query_one("#proc-panel", ProcessesPanel).show(self.executor)
            session = {"uptime": f"{up // 60}m{up % 60:02d}s", "commands": str(self.commands_run),
                       "errors": str(self.errors_seen), "log": self.shell.log_path.name if self.shell.log_enabled else "off",
                       "telemetry": f"{self.poller.samples} ok / {self.poller.errors} err"}
            self.query_one("#meta-panel", MetaPanel).show(self.backend.info(), session)
        except Exception:  # noqa: BLE001
            pass

    def action_estop(self) -> None:
        self.shell.run_line("estop", record=True)

    def action_land_now(self) -> None:
        self.shell.run_line("land -e", record=True)

    def action_soft_stop(self) -> None:
        if self.mode != "INSERT":
            self.shell.run_line("stop", record=False)

    def action_focus_input(self) -> None:
        self.console_panel.input.focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle(self, name: str) -> None:
        self.toggle_panel(name)

    def action_cycle_layout(self) -> None:
        order = ["auto", *LAYOUTS]
        nxt = order[(order.index(self._layout) + 1) % len(order)] if self._layout in order else "auto"
        self.apply_layout(nxt)

    def action_cycle_theme(self) -> None:
        order = [self.OWN_THEME, *[t for t in CYCLE_AFTER_OWN if t in self.available_themes]]
        cur = self.theme if self.theme in order else order[0]
        self.theme = order[(order.index(cur) + 1) % len(order)]
        self.console_write(f"theme: {self.theme}", "dim")

    async def action_quit(self) -> None:  # type: ignore[override]
        self.request_exit(True)

    def set_theme_by_name(self, name: str | None) -> None:
        if not name:
            self.console_write("themes: " + ", ".join(sorted(self.available_themes)), "info")
            self.console_write(f"current: {self.theme} · `t` cycles", "dim")
            return
        if name not in self.available_themes:
            self.console_write(f"no theme '{name}' -- `theme` lists them", "error")
            return
        self.theme = name

    def panel_widget(self, name: str):
        wid = PANELS[name][0]
        try:
            return self.query_one(f"#{wid}")
        except Exception:  # noqa: BLE001
            return None

    def toggle_panel(self, name: str) -> None:
        name = {"log": "console", "side": "sidebar", "telem": "telemetry", "procs": "processes",
                "metadata": "meta", "match": "score", "hud": "pilot", "keys": "pilot"}.get(name.lower(), name.lower())
        if name not in PANELS:
            self.console_write(f"panels: {', '.join(PANELS)}", "error")
            return
        w = self.panel_widget(name)
        if w is None:
            self.console_write(f"panel '{name}' is not available here", "warn")
            return
        w.display = not w.display
        self._sync_rows()

    def apply_layout(self, name: str | None) -> None:
        if not name:
            self.console_write("layouts: auto, " + ", ".join(LAYOUTS) + f"  (now: {self._layout})", "info")
            return
        if name == "auto":
            w, h = self.size.width, self.size.height
            chosen = "full" if (w >= 150 and h >= 36) else "flight" if w >= 100 else "minimal"
            if h < 24:
                chosen = "focus"
            self._layout = "auto"
        elif name in LAYOUTS:
            chosen, self._layout = name, name
        else:
            self.console_write(f"no layout '{name}' -- try: auto, {', '.join(LAYOUTS)}", "error")
            return
        show = LAYOUTS[chosen]
        for pname in PANELS:
            w_ = self.panel_widget(pname)
            if w_ is not None:
                w_.display = pname in show
        self._sync_rows()

    def _sync_rows(self) -> None:
        bottom = self.query_one("#bottom")
        bottom.display = any(c.display for c in bottom.children)
        main = self.query_one("#main")
        main.display = any(c.display for c in main.children)

    def request_exit(self, land_first: bool) -> None:
        if self._exiting:
            return
        self._exiting = True
        b = self.backend
        if land_first and b.connected and b.flying:
            self.console_write("landing before exit…", "warn")
            self.executor.abort()

            def land_then_exit() -> None:
                try:
                    b.land()
                finally:
                    self.ui(self._finish_exit)

            self.executor.submit("land (exit)", land_then_exit)
        else:
            self._finish_exit()

    def _finish_exit(self) -> None:
        if self.pilot is not None:
            self.pilot.ctl.stop()
        self.poller.stop()
        self.executor.shutdown()
        try:
            self.backend.disconnect()
        except BaseException:  # noqa: BLE001
            pass
        self.exit()
