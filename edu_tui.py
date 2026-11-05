"""CoDrone EDU TUI.   python edu_tui.py [--port COM5] [--connect] [--demo]"""

import sys

from codrone_tui.cli import main

if __name__ == "__main__":
    sys.exit(main("edu"))
