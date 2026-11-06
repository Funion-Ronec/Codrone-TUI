"""PILOT mode for the TUIs: held keys fly the drone, one-shot keys run shell commands, the HUD and
the `pilot` command (including rebinding). Loaded by ``CodroneApp.on_mount`` like ``adc_ui``."""

from __future__ import annotations

import time

from .backend import Cap
from .commands import Command, Shell, UsageError, num
from .pilot import (
    ACTIONS,
    AXIS_ACTIONS,
    SPEEDS,
    KeyMap,
    PilotController,
    check_key,
    make_win32_keys,
    norm_key,
)
from . import pilot as pilot_mod
from .screens import ConfirmScreen
from .widgets.graph import palette
from .widgets.pilot import PilotPanel, key_label, render_hud

FLASH_S = 0.25
DEBOUNCE_S = {"speed_up": 0.2, "speed_down": 0.2}
DEFAULT_DEBOUNCE_S = 0.6
FLIPS = {"flip_front": "front", "flip_back": "back", "flip_left": "left", "flip_right": "right"}


def attach(app) -> None:
    app.pilot = PilotUI(app)
    app.set_interval(0.1, app.pilot.tick)


class PilotUI:
    def __init__(self, app) -> None:
        self.app = app
        cfg = app.cfg.get("pilot", {})
        self.keymap, err = KeyMap.load(app.KIND)
        hold = str(cfg.get("hold", "auto")).lower()
        win32 = None if hold == "repeat" or app.is_headless else make_win32_keys()
        self.ctl = PilotController(
            app.backend, app.executor, self.keymap,
            rate_hz=float(cfg.get("rate_hz", 20.0)), speed=int(cfg.get("speed", 2)), win32=win32,
            app_focused=lambda: app.app_focus,
            on_error=lambda m: app.ui(app.console_write, f"pilot: {m}", "error"),
        )
        self.capture: tuple[str, str] | None = None
        self._flash: dict[str, float] = {}
        self._last_press: dict[str, float] = {}
        self._confirmed = False
        self._layout_before: str | None = None
        self._register(app.shell)
        if err:
            app.console_write(f"pilot keys: {err}", "warn")
        self.ctl.start()

    @property
    def active(self) -> bool:
        return self.ctl.active

    @property
    def blocked_keys(self) -> frozenset[str]:
        keys = set()
        for b in self.app.BINDINGS:
            keys |= {norm_key(k) for k in b.key.split(",")}
        return frozenset(keys)

    def is_toggle(self, key: str) -> bool:
        return bool(self.keymap.keys.get("toggle")) and norm_key(key) == self.keymap.keys["toggle"]

    @property
    def panel(self) -> PilotPanel:
        return self.app.query_one("#pilot-panel", PilotPanel)

    def enter(self) -> None:
        app = self.app
        real = app.KIND == "edu" and not app.demo
        if real and not self._confirmed:
            k = {a: key_label(self.keymap.keys.get(a, "")) for a in ACTIONS}
            body = [
                "Fly the [b]REAL drone[/b] with the keyboard?", "",
                f"  hold {k['forward']}{k['left']}{k['back']}{k['right']} to move, {k['turn_left']}/{k['turn_right']} to turn, "
                f"{k['down']}/{k['up']} for altitude",
                "  let go of every key and it hovers", "",
                "Clear space around the drone, eye protection on.",
                "[b]ctrl+x[/b] cuts the motors · [b]ctrl+l[/b] lands · [b]esc[/b] leaves PILOT mode.",
            ]
            app.push_screen(ConfirmScreen("PILOT mode", body, yes="Fly"),
                            lambda ok: self._confirm(ok))
            return
        self._focus_pad()

    def _confirm(self, ok: bool) -> None:
        if ok:
            self._confirmed = True
            self._focus_pad()
        else:
            self.app.console_write("PILOT mode cancelled", "dim")

    def _focus_pad(self) -> None:
        app = self.app
        panel = self.panel
        if not panel.display:
            self._layout_before = app._layout
            app.apply_layout("pilot")
        panel.pad.focus()

    def leave(self) -> None:
        self.app.set_focus(None)

    def on_pad_focus(self) -> None:
        if self.capture is not None:
            return
        app = self.app
        if app.KIND == "edu" and not app.demo and not self._confirmed:
            app.set_focus(None)
            self.enter()
            return
        self.ctl.activate()
        k = self.keymap.keys
        app.console_write(f"PILOT mode on · {key_label(k.get('toggle', ''))} or esc leaves · "
                          f"source: {self.ctl.source}", "ok")
        if not app.backend.connected:
            app.console_write("not connected -- `pair` first, the keys do nothing until then", "warn")

    def on_pad_blur(self) -> None:
        was_active = self.ctl.active
        self.ctl.deactivate()
        if self.capture is not None:
            self.capture = None
            self.app.console_write("rebind cancelled", "dim")
        if was_active:
            self.app.console_write("PILOT mode off", "dim")
        if self._layout_before is not None:
            prev, self._layout_before = self._layout_before, None
            self.app.apply_layout(prev)

    def _debounced(self, name: str, now: float) -> bool:
        last = self._last_press.get(name)
        self._last_press[name] = now
        return last is not None and now - last < DEBOUNCE_S.get(name, DEFAULT_DEBOUNCE_S)

    def on_key(self, raw_key: str) -> bool:
        """Handle a key while the pad has focus. ``False`` lets it fall through to the normal keys."""
        key = norm_key(raw_key)
        now = time.monotonic()
        if self.capture is not None:
            self._finish_capture(key)
            return True
        if key == "escape":
            self.leave()
            return True
        action = self.keymap.action_for(key)
        if action is None:
            line = self.keymap.command_for(key)
            if line is None:
                return False
            if not self._debounced(f"cmd:{key}", now):
                self._flash[f"cmd:{key}"] = now
                self.app.shell.run_line(line)
            return True
        if action in AXIS_ACTIONS:
            self.ctl.note_key(key, now)
            return True
        if self._debounced(action, now):
            return True
        self._flash[action] = now
        sh = self.app.shell
        if action == "toggle":
            self.leave()
        elif action == "speed_up":
            self.ctl.change_speed(+1)
        elif action == "speed_down":
            self.ctl.change_speed(-1)
        elif action == "takeoff":
            sh.run_line("takeoff")
        elif action == "land":
            sh.run_line("land -e")
        elif action in FLIPS:
            if self.app.backend.has(Cap.FLIP):
                sh.run_line(f"flip {FLIPS[action]}")
            else:
                self.app.console_write(f"no flips on {self.app.backend.title}", "warn")
        return True

    def start_capture(self, target: tuple[str, str]) -> None:
        self.capture = target
        self._focus_pad()

    def _finish_capture(self, key: str) -> None:
        target, self.capture = self.capture, None
        if key == "escape":
            self.app.console_write("rebind cancelled", "dim")
        else:
            try:
                self._apply(target, key, force=False)
            except ValueError as exc:
                self.app.console_write(f"pilot: {exc}", "error")
        self.app.set_focus(None)

    def _apply(self, target: tuple[str, str], key: str, *, force: bool) -> None:
        kind, what = target
        if kind == "action":
            self.keymap.bind(what, key, force=force, blocked=self.blocked_keys)
            msg = f"{ACTIONS[what][1]} = {key_label(norm_key(key))}"
        else:
            self.keymap.bind_command(key, what, force=force)
            msg = f"{key_label(norm_key(key))} runs `{what}`"
        self._save(msg)

    def _save(self, msg: str) -> None:
        try:
            path = self.keymap.save()
        except OSError as exc:
            self.app.console_write(f"{msg} (NOT saved: {exc.strerror or exc})", "warn")
            return
        self.app.console_write(f"{msg}  (saved to {path.name})", "ok")

    def tick(self) -> None:
        try:
            panel = self.panel
        except Exception:  # noqa: BLE001
            return
        if not panel.display:
            return
        st = self.ctl.snapshot()
        now = time.monotonic()
        lit = set(st.held) | {a for a, t in self._flash.items() if now - t < FLASH_S}
        capture = None
        if self.capture is not None:
            kind, what = self.capture
            capture = ACTIONS[what][1].upper() if kind == "action" else f"`{what}`"
        has_flip = self.app.backend.has(Cap.FLIP)
        sig = (st, frozenset(lit), capture, tuple(self.keymap.keys.items()), tuple(sorted(self.keymap.commands.items())),
               has_flip, self.app.theme)
        hud = render_hud(st, self.keymap, palette(self.app), lit=lit, has_flip=has_flip, capture=capture)
        panel.show(hud, st.source, sig)

    def _register(self, sh: Shell) -> None:
        sh.register(Command("pilot", self._cmd, "pilot [on|off|keys|bind|run|unbind|reset|speed]",
                            "fly with the keyboard (PILOT mode) and rebind its keys", "flight"))
        sh.extra_completions["pilot"] = lambda: ["on", "off", "keys", "bind", "run", "unbind", "reset", "speed",
                                                 *ACTIONS]

    def _cmd(self, sh: Shell, pos: list[str], flags: set[str]) -> None:
        sub = pos[0].lower() if pos else "on"
        force = "f" in flags
        if sub == "on":
            self.enter()
        elif sub == "off":
            self.leave()
        elif sub == "keys":
            self._list(sh)
        elif sub == "bind":
            if len(pos) < 2 or pos[1].lower() not in ACTIONS:
                raise UsageError("pilot bind <action> [key] [-f]  -- actions: " + ", ".join(ACTIONS))
            target = ("action", pos[1].lower())
            if len(pos) == 2:
                self.start_capture(target)
                return
            try:
                self._apply(target, pos[2], force=force)
            except ValueError as exc:
                raise UsageError(str(exc)) from None
        elif sub == "run":
            if len(pos) < 3:
                raise UsageError('pilot run <key> <command…>  e.g. pilot run f7 mission run phase1')
            if flags - {"f"}:
                raise UsageError('quote a command that has flags: pilot run f7 "land -e"')
            try:
                check_key(pos[1])
                self._apply(("command", " ".join(pos[2:])), pos[1], force=force)
            except ValueError as exc:
                raise UsageError(str(exc)) from None
        elif sub == "unbind":
            if len(pos) != 2:
                raise UsageError("pilot unbind <key>")
            try:
                was = self.keymap.unbind(pos[1])
            except ValueError as exc:
                raise UsageError(str(exc)) from None
            if was is None:
                raise UsageError(f"{pos[1]} isn't bound to anything")
            self._save(f"{key_label(norm_key(pos[1]))} unbound (was {was})")
        elif sub == "reset":
            fresh = KeyMap.defaults(self.app.KIND)
            self.keymap.keys, self.keymap.commands = fresh.keys, fresh.commands
            self._save("PILOT keys back to defaults")
        elif sub == "speed":
            if len(pos) != 2:
                raise UsageError(f"pilot speed <1-{len(SPEEDS)}>")
            self.ctl.speed = int(num(pos[1], "speed", 1, len(SPEEDS)))
            sh.out(f"pilot speed {self.ctl.speed} ({self.ctl.power}%)", "ok")
        else:
            raise UsageError(f"unknown subcommand '{sub}' -- `help pilot`")

    def _list(self, sh: Shell) -> None:
        sh.out(f"PILOT keys ({pilot_mod.KEYS_FILE.name}) · input: {self.ctl.source}", "info")
        for action, (_default, label) in ACTIONS.items():
            sh.out(f"  {label:<20} {key_label(self.keymap.keys.get(action, ''))}", "dim")
        for key, line in sorted(self.keymap.commands.items()):
            sh.out(f"  {key_label(key):<20} {line}", "dim")
        sh.out("rebind: `pilot bind <action> [key]` · `pilot run <key> <command>` · `pilot unbind <key>`", "dim")
