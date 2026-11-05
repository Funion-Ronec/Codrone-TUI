"""Codrone-TUI launcher.

    python Codrone-TUI.py edu [options]     CoDrone EDU (real drone, USB controller)
    python Codrone-TUI.py sim [options]     CoDrone Simulator (Unity sim on this PC)
    python Codrone-TUI.py                   ask which one

Options are the same as edu_tui.py / sim_tui.py: --port, --connect, --demo, --theme, --field.
(The original single-file draft is kept in legacy/Codrone-TUI.draft.py.)
"""

import sys

from codrone_tui.cli import main


def ask() -> str:
    print("Codrone-TUI\n  1) CoDrone EDU        real drone over the USB controller"
          "\n  2) CoDrone Simulator  the simulator running on this computer\n")
    while True:
        choice = input("which one? [1/2, q to quit] ").strip().lower()
        if choice in ("1", "edu", "e"):
            return "edu"
        if choice in ("2", "sim", "s"):
            return "sim"
        if choice in ("q", "quit", ""):
            raise SystemExit(0)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0].lower() in ("edu", "sim"):
        kind, rest = args[0].lower(), args[1:]
    else:
        kind, rest = ask(), args
    sys.exit(main(kind, rest))
