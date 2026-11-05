"""The CoDrone Simulator TUI: talks to the Unity simulator over UDP."""

from __future__ import annotations

from .app import CodroneApp
from .widgets.panels import SideItem


class SimApp(CodroneApp):
    KIND = "sim"
    TITLE_TEXT = "CoDrone Simulator"
    OWN_THEME = "codrone-sim"

    def quick_sections(self):
        return super().quick_sections() + [
            ("SIM TOOLS", [
                SideItem("grab", "grab"),
                SideItem("release", "release"),
                SideItem("reset position", "resetpos"),
                SideItem("teleport…", "teleport ", True, "x y z"),
            ]),
            ("GADGETS", [
                SideItem("led blue", "led 0 120 255"),
                SideItem("led off", "led off"),
                SideItem("beep", "beep"),
            ]),
        ]
