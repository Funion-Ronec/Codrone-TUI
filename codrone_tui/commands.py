"""The command shell: one registry shared by both TUIs, replacing the draft's if/elif loop.

Commands come in two kinds:
    * instant  - run immediately on the caller's (UI) thread, must not block (help, status ...)
    * flight   - queued on the FlightExecutor so the UI never freezes (takeoff, forward ...)
Safety commands (estop, land -e, stop) bypass the queue on purpose.
"""

from __future__ import annotations

import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .backend import BackendError, Cap, Direction, DroneBackend, FlightExecutor, NotSupported
from .config import PROJECT_ROOT


class UsageError(Exception):
    """Bad arguments; the message is shown to the user with the command's usage line."""


class Host:
    """What the shell needs from the application. The Textual app implements this."""

    backend: DroneBackend
    executor: FlightExecutor

    def out(self, text: str, kind: str = "info") -> None: ...
    def clear_console(self) -> None: ...
    def connect_backend(self, port: str | None) -> None: ...
    def disconnect_backend(self) -> None: ...
    def request_exit(self, land_first: bool) -> None: ...
    def set_theme(self, name: str | None) -> None: ...
    def apply_layout(self, name: str | None) -> None: ...
    def toggle_panel(self, name: str) -> None: ...
    def latest_telemetry(self): ...
    def list_ports(self) -> list[tuple[str, str, bool]]:
        return []


Handler = Callable[["Shell", list[str], set[str]], None]


@dataclass
class Command:
    name: str
    handler: Handler
    usage: str
    summary: str
    group: str = "general"
    aliases: tuple[str, ...] = ()
    requires: Cap | None = None
    needs_link: bool = False
    flight: bool = False


def split_args(tokens: list[str]) -> tuple[list[str], set[str]]:
    """Positional arguments and ``-flags`` (``-30`` is a number, not a flag)."""
    pos, flags = [], set()
    for t in tokens:
        if len(t) > 1 and t[0] == "-" and not t[1].isdigit() and t[1] != ".":
            flags.add(t.lstrip("-").lower())
        else:
            pos.append(t)
    return pos, flags


def num(tok: str, what: str, lo: float | None = None, hi: float | None = None) -> float:
    try:
        v = float(tok)
    except (TypeError, ValueError):
        raise UsageError(f"{what} must be a number, got '{tok}'") from None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise UsageError(f"{what} must be between {lo:g} and {hi:g}")
    return v


DIRECTION_WORDS = {
    "forward": Direction.FORWARD, "fwd": Direction.FORWARD, "f": Direction.FORWARD,
    "backward": Direction.BACKWARD, "back": Direction.BACKWARD, "b": Direction.BACKWARD,
    "left": Direction.LEFT, "l": Direction.LEFT,
    "right": Direction.RIGHT, "r": Direction.RIGHT,
    "up": Direction.UP, "u": Direction.UP,
    "down": Direction.DOWN, "d": Direction.DOWN,
}
MAX_MOVE_CM = 1000.0
LOG_DIR = PROJECT_ROOT / "logs"


class Shell:
    def __init__(self, host: Host, log_dir: Path | None = None) -> None:
        self.host = host
        self.commands: dict[str, Command] = {}
        self._alias: dict[str, str] = {}
        self.history: list[str] = []
        self.log_path = (log_dir or LOG_DIR) / f"codrone-command-log-{time.strftime('%Y_%m_%d_%H_%M_%S')}.txt"
        self._log_failed = False
        self._tls = threading.local()
        self.extra_completions: dict[str, Callable[[], list[str]]] = {}
        _register_builtins(self)

    @property
    def backend(self) -> DroneBackend:
        return self.host.backend

    def register(self, cmd: Command) -> None:
        self.commands[cmd.name] = cmd
        for a in cmd.aliases:
            self._alias[a] = cmd.name

    def lookup(self, word: str) -> Command | None:
        word = word.lower()
        return self.commands.get(self._alias.get(word, word))

    def available(self, cmd: Command) -> bool:
        return cmd.requires is None or self.backend.has(cmd.requires)

    def out(self, text: str, kind: str = "info") -> None:
        self.host.out(text, kind)

    def fly(self, label: str, fn: Callable[[], object]):
        """Queue blocking work on the flight thread (or run it in place when already inside a script)."""
        if getattr(self._tls, "inline", False):
            return fn()
        return self.host.executor.submit(label, fn)

    def run_inline(self, line: str) -> None:
        """Run one command to completion on the current (flight) thread; used by `source`."""
        self._tls.inline = True
        try:
            self.run_line(line)
        finally:
            self._tls.inline = False

    def run_line(self, line: str, *, record: bool = True) -> None:
        line = line.strip()
        if not line or line.startswith("#"):
            return
        if record:
            self.history.append(line)
            self._log(line)
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            self.out(f"parse error: {exc}", "error")
            return
        cmd = self.lookup(tokens[0])
        if cmd is None:
            self.out(f"unknown command '{tokens[0]}' -- type `help`", "error")
            return
        if not self.available(cmd):
            self.out(f"`{cmd.name}` is not available on {self.backend.title}", "warn")
            return
        if cmd.needs_link and not self.backend.connected:
            self.out("not connected -- run `pair` first", "warn")
            return
        pos, flags = split_args(tokens[1:])
        try:
            cmd.handler(self, pos, flags)
        except UsageError as exc:
            self.out(f"{exc}", "error")
            self.out(f"usage: {cmd.usage}", "dim")
        except NotSupported as exc:
            self.out(str(exc), "warn")
        except BackendError as exc:
            self.out(str(exc), "error")
        except Exception as exc:  # noqa: BLE001
            self.out(f"{type(exc).__name__}: {exc}", "error")

    def _log(self, line: str) -> None:
        if self._log_failed:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')}  {line}\n")
        except OSError:
            self._log_failed = True

    def complete(self, text: str) -> str | None:
        """Best single completion for the text typed so far (used by the input suggester)."""
        if not text or text.endswith("  "):
            return None
        parts = text.split(" ")
        if len(parts) == 1:
            names = sorted(set(self.commands) | set(self._alias))
            for n in names:
                if n.startswith(parts[0].lower()) and n != parts[0].lower():
                    cmd = self.lookup(n)
                    if cmd is not None and self.available(cmd):
                        return n
            return None
        cmd = self.lookup(parts[0])
        if cmd is None:
            return None
        options: list[str] = []
        if cmd.name == "go":
            options = ["forward", "backward", "left", "right", "up", "down"]
        elif cmd.name == "turn":
            options = ["left", "right"]
        elif cmd.name == "shape":
            options = ["square", "triangle", "circle", "spiral", "sway"]
        elif cmd.name == "flip":
            options = ["front", "back", "left", "right"]
        elif cmd.name == "help":
            options = sorted(self.commands)
        elif cmd.name in self.extra_completions:
            options = self.extra_completions[cmd.name]()
        last = parts[-1]
        for o in options:
            if o.startswith(last.lower()) and o != last:
                return " ".join(parts[:-1] + [o])
        return None


def h_help(sh: Shell, pos, flags) -> None:
    if pos:
        cmd = sh.lookup(pos[0])
        if cmd is None:
            raise UsageError(f"no such command '{pos[0]}'")
        sh.out(f"{cmd.name} -- {cmd.summary}", "info")
        sh.out(f"usage: {cmd.usage}", "dim")
        if cmd.aliases:
            sh.out(f"aliases: {', '.join(cmd.aliases)}", "dim")
        return
    groups: dict[str, list[Command]] = {}
    for c in sh.commands.values():
        groups.setdefault(c.group, []).append(c)
    for g, cmds in groups.items():
        sh.out(f"[{g}]", "ok")
        for c in sorted(cmds, key=lambda c: c.name):
            if sh.available(c):
                sh.out(f"  {c.usage:<34} {c.summary}", "info")
    sh.out("keys: i/: type a command · esc leave input · ctrl+x ESTOP · ctrl+l land · ? help screen", "dim")


def h_clear(sh: Shell, pos, flags) -> None:
    sh.host.clear_console()


def h_echo(sh: Shell, pos, flags) -> None:
    sh.out(" ".join(pos), "info")  # plain text only, the old draft ran this thru os.system() which is a damn shell


def h_cat(sh: Shell, pos, flags) -> None:
    if len(pos) != 1:
        raise UsageError("expected exactly one file")
    path = Path(pos[0])
    try:
        if path.stat().st_size > 256_000:
            raise UsageError(f"{path} is too large to print (limit 256 KB)")
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise UsageError(f"{path} not found") from None
    except IsADirectoryError:
        raise UsageError(f"{path} is a directory") from None
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror or exc}") from None
    for ln in text.splitlines():
        sh.out(ln, "info")


def h_history(sh: Shell, pos, flags) -> None:
    for i, ln in enumerate(sh.history[-30:], 1):
        sh.out(f"{i:>3}  {ln}", "dim")
    sh.out(f"log file: {sh.log_path}", "dim")


def h_pair(sh: Shell, pos, flags) -> None:
    if len(pos) > 1:
        raise UsageError("expected at most one port")
    sh.host.connect_backend(pos[0] if pos else None)


def h_disconnect(sh: Shell, pos, flags) -> None:
    sh.host.disconnect_backend()


def h_ports(sh: Shell, pos, flags) -> None:
    ports = sh.host.list_ports()
    if not ports:
        sh.out("no serial ports found", "warn")
    for dev, desc, is_cde in ports:
        sh.out(f"{dev:<8} {desc}{'   <- CoDrone EDU controller' if is_cde else ''}", "ok" if is_cde else "info")


def h_takeoff(sh: Shell, pos, flags) -> None:
    sh.fly("takeoff", sh.backend.takeoff)


def h_land(sh: Shell, pos, flags) -> None:
    if "e" in flags:
        dropped = sh.host.executor.abort()
        if dropped:
            sh.out(f"dropped {dropped} queued command(s)", "warn")
        sh.fly("land -e", sh.backend.land)
    else:
        sh.fly("land", sh.backend.land)


def h_stop(sh: Shell, pos, flags) -> None:
    """Soft stop: cancel everything queued / running between steps, then hover."""
    n = sh.host.executor.abort()
    sh.out(f"stop requested ({n} queued dropped); the current move finishes first", "warn")
    if sh.backend.connected:
        sh.fly("hover", lambda: sh.backend.hover(0.2))


def h_estop(sh: Shell, pos, flags) -> None:
    sh.host.executor.abort()
    sh.backend.emergency_stop()
    kind = "MOTORS CUT" if sh.backend.has(Cap.ESTOP) else "LAND SENT (simulator has no motor cut)"
    sh.out(f"EMERGENCY STOP -- {kind}", "error")


def h_hover(sh: Shell, pos, flags) -> None:
    secs = num(pos[0], "seconds", 0.1, 60) if pos else 1.0
    sh.fly(f"hover {secs:g}s", lambda: sh.backend.hover(secs))


def _move(sh: Shell, direction: Direction, pos: list[str]) -> None:
    if not pos:
        raise UsageError("missing distance")
    dist = num(pos[0], "distance (cm)", 1, MAX_MOVE_CM)
    speed = num(pos[1], "speed", 0.1, 2.0) if len(pos) > 1 else None
    sh.fly(f"{direction.value} {dist:g}cm", lambda: sh.backend.move(direction, dist, speed))


def _make_move(direction: Direction) -> Handler:
    return lambda sh, pos, flags: _move(sh, direction, pos)


def h_go(sh: Shell, pos, flags) -> None:
    if not pos or pos[0].lower() not in DIRECTION_WORDS:
        raise UsageError("first argument must be forward/backward/left/right/up/down")
    _move(sh, DIRECTION_WORDS[pos[0].lower()], pos[1:])


def h_turn(sh: Shell, pos, flags) -> None:
    if not pos:
        raise UsageError("missing angle")
    sign = 1.0
    if pos[0].lower() in ("left", "right"):
        sign = -1.0 if pos[0].lower() == "left" else 1.0
        pos = pos[1:] or ["90"]
    deg = sign * num(pos[0], "degrees", -360, 360)
    sh.fly(f"turn {deg:+g}°", lambda: sh.backend.turn(deg))


def h_turn_dir(sign: float) -> Handler:
    def handler(sh: Shell, pos, flags) -> None:
        deg = sign * num(pos[0] if pos else "90", "degrees", 0, 360)
        sh.fly(f"turn {deg:+g}°", lambda: sh.backend.turn(deg))

    return handler


def h_stick(sh: Shell, pos, flags) -> None:
    if len(pos) != 5:
        raise UsageError("expected roll pitch yaw throttle seconds")
    r, p, y, t = (int(num(v, n, -100, 100)) for v, n in zip(pos[:4], ("roll", "pitch", "yaw", "throttle")))
    secs = num(pos[4], "seconds", 0.05, 10)
    sh.fly(f"stick r{r} p{p} y{y} t{t} {secs:g}s", lambda: sh.backend.go(r, p, y, t, secs))


def h_flip(sh: Shell, pos, flags) -> None:
    direction = pos[0].lower() if pos else "back"
    if direction not in ("front", "back", "left", "right"):
        raise UsageError("direction must be front, back, left or right")
    sh.fly(f"flip {direction}", lambda: sh.backend.flip(direction))


def h_shape(sh: Shell, pos, flags) -> None:
    if not pos:
        raise UsageError("missing shape name")
    name = pos[0].lower()
    sh.fly(f"shape {name}", lambda: sh.backend.shape(name))


def h_led(sh: Shell, pos, flags) -> None:
    if pos and pos[0].lower() == "off":
        sh.fly("led off", sh.backend.led_off)
        return
    if len(pos) not in (3, 4):
        raise UsageError("expected r g b [brightness] or `off`")
    r, g, b = (int(num(v, n, 0, 255)) for v, n in zip(pos[:3], "rgb"))
    br = int(num(pos[3], "brightness", 0, 255)) if len(pos) == 4 else 255
    sh.fly(f"led {r},{g},{b}", lambda: sh.backend.set_led(r, g, b, br))


def h_beep(sh: Shell, pos, flags) -> None:
    hz = int(num(pos[0], "frequency (Hz)", 100, 8000)) if pos else 440
    ms = int(num(pos[1], "duration (ms)", 10, 5000)) if len(pos) > 1 else 200
    sh.fly(f"beep {hz}Hz", lambda: sh.backend.beep(hz, ms))


def h_grab(sh: Shell, pos, flags) -> None:
    sh.fly("grab", sh.backend.grab)


def h_release(sh: Shell, pos, flags) -> None:
    sh.fly("release", sh.backend.release)


def h_teleport(sh: Shell, pos, flags) -> None:
    if len(pos) != 3:
        raise UsageError("expected x y z")
    x, y, z = (num(v, n) for v, n in zip(pos, "xyz"))
    sh.fly(f"teleport {x:g},{y:g},{z:g}", lambda: sh.backend.teleport(x, y, z))


def h_resetpos(sh: Shell, pos, flags) -> None:
    sh.fly("reset position", sh.backend.reset_position)


def h_wait(sh: Shell, pos, flags) -> None:
    secs = num(pos[0], "seconds", 0.1, 300) if pos else 1.0
    ex = sh.host.executor
    sh.fly(f"wait {secs:g}s", lambda: ex.sleep(secs))


def h_status(sh: Shell, pos, flags) -> None:
    t = sh.host.latest_telemetry()
    b = sh.backend
    sh.out(f"{b.title}: {'connected' if b.connected else 'NOT connected'}"
           f"{' · flying' if b.flying else ''}", "ok" if b.connected else "warn")
    if t is None:
        return
    for label, val in (
        ("battery", f"{t.battery}%" if t.battery is not None else None),
        ("state", t.flight_state),
        ("front", f"{t.front_range_cm} cm" if t.front_range_cm is not None else None),
        ("bottom", f"{t.bottom_range_cm} cm" if t.bottom_range_cm is not None else None),
        ("pos", ("~" if t.pos_is_estimate else "") + str(tuple(t.pos_cm)) + " cm" if t.pos_cm else None),
    ):
        if val is not None:
            sh.out(f"  {label:<8}{val}", "info")


def h_info(sh: Shell, pos, flags) -> None:
    for k, v in sh.backend.info().items():
        sh.out(f"  {k:<14}{v}", "info")


def h_theme(sh: Shell, pos, flags) -> None:
    sh.host.set_theme(pos[0] if pos else None)


def h_layout(sh: Shell, pos, flags) -> None:
    sh.host.apply_layout(pos[0] if pos else None)


def h_toggle(sh: Shell, pos, flags) -> None:
    if not pos:
        raise UsageError("missing panel name")
    sh.host.toggle_panel(pos[0])


def h_source(sh: Shell, pos, flags) -> None:
    """Run a text file of commands in order (flight steps queue behind each other)."""
    if len(pos) != 1:
        raise UsageError("expected one script file")
    try:
        lines = Path(pos[0]).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise UsageError(f"cannot read {pos[0]}: {exc.strerror or exc}") from None
    lines = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if any(shlex.split(ln)[0].lower() in ("source", "exit", "quit") for ln in lines):
        raise UsageError("scripts may not contain source / exit / quit")
    sh.out(f"running {len(lines)} step(s) from {pos[0]}", "ok")
    def run_script() -> None:
        for ln in lines:
            sh.host.executor.check()
            sh.run_inline(ln)

    sh.fly(f"script {pos[0]}", run_script)


def h_exit(sh: Shell, pos, flags) -> None:
    sh.host.request_exit(land_first=("f" not in flags and "now" not in flags))


def _register_builtins(sh: Shell) -> None:
    R = sh.register
    R(Command("help", h_help, "help [command]", "list commands, or explain one", "general", ("?", "h")))
    R(Command("clear", h_clear, "clear", "clear the console", "general", ("cls",)))
    R(Command("echo", h_echo, "echo <text>", "print text", "general"))
    R(Command("cat", h_cat, "cat <file>", "print a text file", "general"))
    R(Command("history", h_history, "history", "recent commands + log file path", "general"))
    R(Command("source", h_source, "source <file>", "run a script of commands", "general", ("run",)))
    R(Command("exit", h_exit, "exit [-f]", "land if flying, then quit (-f: quit now)", "general", ("quit", "q")))

    R(Command("pair", h_pair, "pair [port]", "connect (EDU: COM port · sim: host:port)", "link", ("connect",)))
    R(Command("disconnect", h_disconnect, "disconnect", "close the link", "link", ("unpair",)))
    R(Command("ports", h_ports, "ports", "list serial ports", "link"))
    R(Command("status", h_status, "status", "link, flight state, key sensors", "link", ("st",)))
    R(Command("info", h_info, "info", "backend / firmware details", "link"))

    R(Command("takeoff", h_takeoff, "takeoff", "take off and hover", "flight", ("to",), needs_link=True, flight=True))
    R(Command("land", h_land, "land [-e]", "land (-e: urgent, drops queued moves)", "flight", needs_link=True, flight=True))
    R(Command("stop", h_stop, "stop", "cancel queued/automatic moves, hover", "flight", ("halt",), needs_link=True))
    R(Command("estop", h_estop, "estop", "EMERGENCY: cut motors (EDU) / land (sim)", "flight", ("kill",)))
    R(Command("hover", h_hover, "hover [seconds]", "hold position", "flight", needs_link=True, flight=True))
    for d, word in (("forward", ("fwd", "f")), ("backward", ("back", "b")), ("left", ("l",)),
                    ("right", ("r",)), ("up", ("u",)), ("down", ("d",))):
        R(Command(d, _make_move(DIRECTION_WORDS[d]), f"{d} <cm> [speed]", f"move {d}", "flight", word,
                  needs_link=True, flight=True))
    R(Command("go", h_go, "go <dir> <cm> [speed]", "move in any direction", "flight", needs_link=True, flight=True))
    R(Command("turn", h_turn, "turn [left|right] <deg>", "yaw (+ = clockwise)", "flight", needs_link=True, flight=True))
    R(Command("tl", h_turn_dir(-1.0), "tl [deg]", "turn left (default 90)", "flight", needs_link=True, flight=True))
    R(Command("tr", h_turn_dir(1.0), "tr [deg]", "turn right (default 90)", "flight", needs_link=True, flight=True))
    R(Command("stick", h_stick, "stick <roll> <pitch> <yaw> <thr> <sec>", "raw stick burst (-100..100)", "flight",
              needs_link=True, flight=True))
    R(Command("wait", h_wait, "wait <seconds>", "pause the command queue", "flight", ("sleep",)))
    R(Command("flip", h_flip, "flip [front|back|left|right]", "do a flip (battery >= 50%)", "flight",
              requires=Cap.FLIP, needs_link=True, flight=True))
    R(Command("shape", h_shape, "shape <name>", "square/triangle/circle/spiral/sway", "flight",
              requires=Cap.SHAPES, needs_link=True, flight=True))

    R(Command("led", h_led, "led <r> <g> <b> [bright] | off", "drone LED", "gadgets", requires=Cap.LED, needs_link=True, flight=True))
    R(Command("beep", h_beep, "beep [hz] [ms]", "drone buzzer", "gadgets", requires=Cap.BUZZER, needs_link=True, flight=True))
    R(Command("grab", h_grab, "grab", "close the gripper", "simulator", requires=Cap.GRAB, needs_link=True, flight=True))
    R(Command("release", h_release, "release", "open the gripper", "simulator", requires=Cap.GRAB, needs_link=True, flight=True))
    R(Command("teleport", h_teleport, "teleport <x> <y> <z>", "move the sim drone instantly", "simulator",
              requires=Cap.TELEPORT, needs_link=True, flight=True))
    R(Command("resetpos", h_resetpos, "resetpos", "back to the start position", "simulator", ("home",),
              needs_link=True, flight=True))

    R(Command("theme", h_theme, "theme [name]", "list / switch colour themes", "ui"))
    R(Command("layout", h_layout, "layout [name]", "list / apply panel presets", "ui"))
    R(Command("toggle", h_toggle, "toggle <panel>", "show / hide a panel", "ui", ("panel",)))
