"""Keyboard flying (PILOT mode): the rebindable keymap, held-key detection and the stick sender.

UI-agnostic like ``commands.py``: ``pilot_ui.py`` wires it into the Textual app.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .backend import BackendError, DroneBackend
from .config import PROJECT_ROOT

KEYS_FILE = PROJECT_ROOT / "pilot_keys.toml"

ACTIONS: dict[str, tuple[str, str]] = {
    "forward": ("w", "forward"),
    "back": ("s", "back"),
    "left": ("a", "left"),
    "right": ("d", "right"),
    "turn_left": ("j", "turn left"),
    "turn_right": ("l", "turn right"),
    "up": ("e", "up"),
    "down": ("q", "down"),
    "speed_up": ("i", "speed +"),
    "speed_down": ("k", "speed -"),
    "takeoff": ("t", "take off"),
    "land": ("g", "land"),
    "flip_front": ("1", "flip front"),
    "flip_back": ("2", "flip back"),
    "flip_left": ("3", "flip left"),
    "flip_right": ("4", "flip right"),
    "toggle": ("f", "enter / leave PILOT"),
}
AXIS_ACTIONS = ("forward", "back", "left", "right", "turn_left", "turn_right", "up", "down")
RESERVED_KEYS = frozenset({"escape", "ctrl+x", "ctrl+l", "ctrl+c", "ctrl+q", "ctrl+p"})

DEFAULT_COMMANDS: dict[str, dict[str, str]] = {
    "edu": {"f1": "shape square", "f2": "shape circle", "f3": "shape spiral", "f4": "shape triangle",
            "f5": "mission run hover_test", "f6": "mission run takeoff_land"},
    "sim": {"f5": "mission run hover_test", "f6": "mission run takeoff_land"},
}

SPEEDS = (15, 25, 40, 60, 80)
DEFAULT_SPEED = 2

FIRST_HOLD_S = 0.55
REPEAT_HOLD_S = 0.15
ZERO_PACKETS = 3


def norm_key(key: str) -> str:
    key = key.strip()
    return key.lower() if len(key) == 1 else key


def check_key(key: str) -> str:
    key = norm_key(key)
    if not key or any(c.isspace() for c in key):
        raise ValueError("key name can't be empty or contain spaces")
    if key in RESERVED_KEYS:
        raise ValueError(f"{key} is reserved (safety / leave keys)")
    return key


class KeyMap:
    """Action -> key, plus key -> shell command line for the free slots (F-keys by default)."""

    def __init__(self, keys: dict[str, str], commands: dict[str, str]) -> None:
        self.keys = dict(keys)
        self.commands = dict(commands)

    @classmethod
    def defaults(cls, kind: str) -> "KeyMap":
        return cls({a: k for a, (k, _label) in ACTIONS.items()}, DEFAULT_COMMANDS.get(kind, {}))

    @classmethod
    def load(cls, kind: str, path: Path | None = None) -> tuple["KeyMap", str | None]:
        km = cls.defaults(kind)
        p = path or KEYS_FILE
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return km, None
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return km, f"{p.name}: {exc} (using default keys)"
        problems = []
        for action, key in dict(data.get("keys", {})).items():
            if action not in ACTIONS:
                problems.append(f"unknown action '{action}'")
                continue
            if key == "":
                km.keys[action] = ""
                continue
            try:
                km.bind(action, str(key), force=True)
            except ValueError as exc:
                problems.append(f"{action}: {exc}")
        if "commands" in data:
            km.commands = {}
            for key, line in dict(data["commands"]).items():
                try:
                    km.bind_command(str(key), str(line), force=True)
                except ValueError as exc:
                    problems.append(f"{key}: {exc}")
        return km, (f"{p.name}: " + "; ".join(problems)) if problems else None

    def save(self, path: Path | None = None) -> Path:
        p = path or KEYS_FILE
        lines = ["# PILOT mode keys, written by `pilot bind` / `pilot run` / `pilot reset`", "", "[keys]"]
        lines += [f"{a} = {json.dumps(self.keys.get(a, ''))}" for a in ACTIONS]
        lines += ["", "[commands]"]
        lines += [f"{json.dumps(k)} = {json.dumps(v)}" for k, v in sorted(self.commands.items())]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def action_for(self, key: str) -> str | None:
        key = norm_key(key)
        for action, k in self.keys.items():
            if k and k == key:
                return action
        return None

    def command_for(self, key: str) -> str | None:
        return self.commands.get(norm_key(key))

    def owner(self, key: str) -> str | None:
        """What a key is bound to right now: an action name, ``"command"`` or ``None``."""
        if self.action_for(key):
            return self.action_for(key)
        return "command" if self.command_for(key) else None

    def _free(self, key: str, force: bool, keep: str | None = None) -> None:
        other = self.action_for(key)
        if other and other != keep:
            if not force:
                raise ValueError(f"{key} is already {other} (add -f to take it)")
            self.keys[other] = ""
        if key in self.commands and keep != "command":
            if not force:
                raise ValueError(f"{key} already runs `{self.commands[key]}` (add -f to take it)")
            del self.commands[key]

    def bind(self, action: str, key: str, *, force: bool = False, blocked: frozenset[str] = frozenset()) -> None:
        if action not in ACTIONS:
            raise ValueError(f"no action '{action}' -- try: {', '.join(ACTIONS)}")
        key = check_key(key)
        if action == "toggle" and key in blocked:
            raise ValueError(f"{key} is a normal-mode key, it can't also open PILOT mode")
        self._free(key, force, keep=action)
        self.keys[action] = key

    def bind_command(self, key: str, line: str, *, force: bool = False) -> None:
        key = check_key(key)
        line = line.strip()
        if not line:
            raise ValueError("missing command")
        if line.split()[0].lower() == "pilot":
            raise ValueError("a pilot key can't run `pilot` itself")
        self._free(key, force, keep="command")
        self.commands[key] = line

    def unbind(self, key: str) -> str | None:
        """Free a key. Returns what it was bound to, or ``None``."""
        key = norm_key(key)
        action = self.action_for(key)
        if action:
            if action == "toggle":
                raise ValueError("toggle can't be unbound, rebind it instead (or use the `pilot` command)")
            self.keys[action] = ""
            return action
        if key in self.commands:
            return f"`{self.commands.pop(key)}`"
        return None


def sticks_for(held: set[str] | frozenset[str], power: int) -> tuple[int, int, int, int]:
    """Held axis actions -> (roll, pitch, yaw, throttle) in the backend's ``stick`` convention."""

    def axis(pos: str, neg: str) -> int:
        return power * ((pos in held) - (neg in held))

    return axis("right", "left"), axis("forward", "back"), axis("turn_right", "turn_left"), axis("up", "down")


ZERO = (0, 0, 0, 0)


class RepeatKeys:
    """Guesses held keys from terminal key events (terminals never send a key-up). A key counts as
    held for ``FIRST_HOLD_S`` after its first press, which covers the OS auto-repeat delay, then for
    ``REPEAT_HOLD_S`` after each repeat."""

    name = "key-repeat"

    def __init__(self) -> None:
        self._seen: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _window(count: int) -> float:
        return FIRST_HOLD_S if count <= 1 else REPEAT_HOLD_S

    def note(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            rec = self._seen.get(key)
            if rec is not None and now - rec[0] <= self._window(rec[1]):
                self._seen[key] = (now, rec[1] + 1)
            else:
                self._seen[key] = (now, 1)

    def held(self, keys, now: float | None = None) -> set[str]:
        now = time.monotonic() if now is None else now
        with self._lock:
            return {k for k in keys if (r := self._seen.get(k)) is not None and now - r[0] <= self._window(r[1])}

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


_VK = {
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "semicolon": 0xBA, "equals_sign": 0xBB, "comma": 0xBC, "minus": 0xBD, "full_stop": 0xBE,
    "slash": 0xBF, "left_square_bracket": 0xDB, "right_square_bracket": 0xDD, "apostrophe": 0xDE,
}


class Win32Keys:
    """Real key-down state from Windows (``GetAsyncKeyState``), only while our window is in front."""

    name = "held keys"

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetAncestor.restype = wintypes.HWND
        self._user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.GetConsoleWindow.restype = wintypes.HWND
        self._dword = wintypes.DWORD
        self._ancestors = _ancestor_pids(ctypes, wintypes, self._kernel32)

    @staticmethod
    def supports(key: str) -> bool:
        return key in _VK

    def window_focused(self) -> bool:
        fg = self._user32.GetForegroundWindow()
        if not fg:
            return False
        con = self._kernel32.GetConsoleWindow()
        if con and (con == fg or self._user32.GetAncestor(con, 3) == fg):
            return True
        pid = self._dword()
        self._user32.GetWindowThreadProcessId(fg, self._ct.byref(pid))
        return pid.value in self._ancestors

    def held(self, keys) -> set[str]:
        return {k for k in keys if k in _VK and self._user32.GetAsyncKeyState(_VK[k]) & 0x8000}


def _ancestor_pids(ctypes, wintypes, kernel32) -> frozenset[int]:
    """Our process and every parent above it: the terminal hosting us is one of them."""

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return frozenset({os.getpid()})
    parent: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            parent[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    chain, pid = {os.getpid()}, os.getpid()
    for _ in range(32):
        pid = parent.get(pid, 0)
        if not pid or pid in chain:
            break
        chain.add(pid)
    return frozenset(chain)


def make_win32_keys() -> Win32Keys | None:
    if sys.platform != "win32":
        return None
    try:
        return Win32Keys()
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class PilotState:
    """One snapshot for the HUD."""

    active: bool
    status: str
    speed: int
    sticks: tuple[int, int, int, int]
    held: frozenset[str]
    source: str
    error: str


class PilotController:
    """Turns held keys into live stick packets on its own thread, ``rate_hz`` times a second.

    Sticks only go out while PILOT mode is active, the drone is linked and flying, and the flight
    executor is idle; any queued command (takeoff, flip, a mission) pauses the sticks. Releasing every
    key, leaving PILOT mode or losing window focus sends a few zero packets, then nothing.
    """

    def __init__(self, backend: DroneBackend, executor, keymap: KeyMap, *, rate_hz: float = 20.0,
                 speed: int = DEFAULT_SPEED, win32: Win32Keys | None = None, app_focused=lambda: True,
                 on_error=None) -> None:
        self.backend = backend
        self.executor = executor
        self.keymap = keymap
        self.rate_hz = max(2.0, min(float(rate_hz), float(getattr(backend, "stick_hz", rate_hz))))
        self.speed = max(1, min(len(SPEEDS), int(speed)))
        self.win32 = win32
        self.app_focused = app_focused
        self.on_error = on_error
        self.repeat = RepeatKeys()
        self.active = False
        self.status = "off"
        self.error = ""
        self.sticks = ZERO
        self.held: frozenset[str] = frozenset()
        self._zeros_left = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def source(self) -> str:
        return self.win32.name if self.win32 is not None else self.repeat.name

    @property
    def power(self) -> int:
        return SPEEDS[self.speed - 1]

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pilot-sticks", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def activate(self) -> None:
        self.repeat.clear()
        self.error = ""
        self.active = True

    def deactivate(self) -> None:
        self.active = False
        self.repeat.clear()

    def change_speed(self, step: int) -> int:
        self.speed = max(1, min(len(SPEEDS), self.speed + step))
        return self.speed

    def note_key(self, key: str, now: float | None = None) -> None:
        self.repeat.note(norm_key(key), now)

    def _held(self, now: float) -> set[str]:
        bound = {a: self.keymap.keys.get(a, "") for a in AXIS_ACTIONS}
        bound = {a: k for a, k in bound.items() if k}
        real = {k for k in bound.values() if self.win32 is not None and self.win32.supports(k)}
        down = self.repeat.held(set(bound.values()) - real, now)
        if real and self._focused():
            down |= self.win32.held(real)
        return {a for a, k in bound.items() if k in down}

    def _focused(self) -> bool:
        try:
            return bool(self.app_focused()) and self.win32.window_focused()
        except Exception:  # noqa: BLE001
            return False

    def _blocked(self) -> str | None:
        b = self.backend
        if not b.connected:
            return "offline"
        if not b.flying:
            return "landed"
        if self.executor.busy:
            return "busy"
        if self.win32 is not None and not self._focused():
            return "no focus"
        return None

    def snapshot(self) -> PilotState:
        return PilotState(self.active, self.status, self.speed, self.sticks, self.held, self.source, self.error)

    def step(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if not self.active:
                held, status = set(), "off"
            else:
                blocked = self._blocked()
                held = self._held(now) if blocked in (None, "landed", "busy") else set()
                status = blocked or "live"
            sticks = sticks_for(held, self.power) if status == "live" else ZERO
            self.held, self.sticks = frozenset(held), sticks
            if sticks != ZERO:
                self._zeros_left = ZERO_PACKETS
                self._send(sticks)
            elif self._zeros_left > 0 and self.backend.connected:
                self._zeros_left -= 1
                self._send(ZERO)
            self.status = "error" if self.error and status == "live" else status

    def _send(self, sticks: tuple[int, int, int, int]) -> None:
        try:
            self.backend.stick(*sticks, 1.0 / self.rate_hz)
        except BaseException as exc:  # noqa: BLE001
            msg = str(exc) if isinstance(exc, BackendError) else f"{type(exc).__name__}: {exc}"
            if msg != self.error and self.on_error is not None:
                self.on_error(msg)
            self.error = msg
            self._zeros_left = 0
        else:
            self.error = ""

    def _run(self) -> None:
        period = 1.0 / self.rate_hz
        while not self._stop.wait(period):
            self.step()
        self.active = False
        self.step()
