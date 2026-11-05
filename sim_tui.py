"""CoDrone Simulator TUI.   python sim_tui.py [--port host:port] [--connect] [--demo]"""

import sys

from codrone_tui.cli import main

if __name__ == "__main__":
    sys.exit(main("sim"))
