"""Modal dialogs."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

HELP_KEYS = """\
[b]Modes[/b]   NORMAL (keys act)  ·  INSERT (typing a command)  ·  AUTO (a mission is flying)

[b]Safety (work everywhere, even while typing)[/b]
  ctrl+x   EMERGENCY STOP      motors cut on the EDU · land command on the simulator
  ctrl+l   urgent land         drops queued moves, then lands
  esc      (NORMAL) soft stop  cancels queued / automatic moves and hovers

[b]Moving around[/b]
  i  :  /  start typing a command       esc   leave the input
  tab  or  →   complete the suggestion  ↑ ↓   command history
  1-7      show / hide panel            p     cycle layout presets
  t        cycle colour theme           ctrl+p  command palette
  ?        this help                    q     land (if flying) and quit

[b]Panels[/b]
  1 Console · 2 Sidebar · 3 Telemetry · 4 Processes · 5 Map · 6 Score/Match · 7 Metadata

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
