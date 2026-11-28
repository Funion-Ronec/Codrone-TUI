# Modal dialogs

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Static

# THIS TOOK FOREVER TO MAP AND WRITE DOWN/
HELP_KEYS = """\
[b]Modes[/b]   NORMAL (keys act)  ·  INSERT (typing a command)  ·  PILOT (keyboard flying)  ·  AUTO (a mission is flying)

[b]Safety (work everywhere, even while typing)[/b]
  ctrl+x   EMERGENCY STOP      motors cut on the EDU · land command on the simulator
  ctrl+l   urgent land         drops queued moves, then lands
  esc      (NORMAL) soft stop  cancels queued / automatic moves and hovers

[b]Moving around[/b]
  i  :  /  start typing a command       esc   leave the input
  tab  or  →   complete the suggestion  ↑ ↓   command history
  1-9      show / hide panel            p     cycle layout presets
  t        cycle colour theme           ctrl+p  command palette
  ?        this help                    q     land (if flying) and quit

[b]PILOT mode[/b]   f enters it (defaults below; `pilot keys` shows yours, `pilot bind` changes them)
  hold W A S D   move              hold J / L   turn          hold Q / E   down / up
  I / K          speed + / -       T  take off   G  land       1-4  flips (EDU)
  F1-F6          bound commands (shapes, missions)             esc or f  leave (the drone hovers)

[b]Panels[/b]
  1 Console · 2 Sidebar · 3 Telemetry · 4 Processes · 5 Map · 6 Score/Match · 7 Metadata · 8 Pilot · 9 Bot

[b]Tips[/b]
  Commands queue and run in order; `land -e` and `estop` jump the queue.
  `source file.txt` runs a script of commands. `help <command>` explains one.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark,q", "dismiss", "close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen #help-box {
        width: 84; max-width: 96%; height: 80%;
        border: round $accent; border-title-color: $accent;
        background: $surface; padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        box = VerticalScroll(id="help-box")
        box.border_title = "Keys"
        box.border_subtitle = "esc to close"
        with box:
            yield Static(HELP_KEYS)

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y,enter", "yes", "yes"),
        Binding("n,escape", "no", "no"),
    ]
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen #box {
        width: 70; max-width: 96%; height: auto; max-height: 90%;
        border: round $warning; border-title-color: $warning;
        background: $surface; padding: 1 2;
    }
    ConfirmScreen #buttons { height: 3; align-horizontal: right; margin-top: 1; }
    ConfirmScreen Button { margin-left: 2; }
    """

    def __init__(self, title: str, lines: list[str], yes: str = "Run", no: str = "Cancel") -> None:
        super().__init__()
        self._title, self._lines, self._yes, self._no = title, lines, yes, no

    def compose(self) -> ComposeResult:
        box = Vertical(id="box")
        box.border_title = self._title
        with box:
            yield Static("\n".join(self._lines))
            with Horizontal(id="buttons"):
                yield Button(self._no + " (n)", id="no")
                yield Button(self._yes + " (y)", id="yes", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class SecretScreen(ModalScreen):
    # Masked entry for an API key. Dismisses with (key, remember) or None. The key never touches the console.
    BINDINGS = [Binding("escape", "cancel", "cancel")]
    DEFAULT_CSS = """
    SecretScreen { align: center middle; }
    SecretScreen #box {
        width: 72; max-width: 96%; height: auto;
        border: round $accent; background: $surface; padding: 1 2;
    }
    SecretScreen Input { margin: 1 0; }
    SecretScreen #buttons { height: 3; align-horizontal: right; margin-top: 1; }
    SecretScreen Button { margin-left: 2; }
    """

    def __init__(self, title: str, lines: list[str], remember_label: str) -> None:
        super().__init__()
        self._title, self._lines, self._remember = title, lines, remember_label

    def compose(self) -> ComposeResult:
        box = Vertical(id="box")
        box.border_title = self._title
        with box:
            yield Static("\n".join(self._lines))
            yield Input(placeholder="paste the key", password=True, id="secret")
            yield Checkbox(self._remember, id="remember")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#secret", Input).focus()

    def _finish(self) -> None:
        key = self.query_one("#secret", Input).value.strip()
        self.dismiss((key, self.query_one("#remember", Checkbox).value) if key else None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # keep it away from the app's command handler
        self._finish()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._finish()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
