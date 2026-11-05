"""The CoDrone EDU TUI: real hardware over the USB controller."""

from __future__ import annotations

from .app import CodroneApp
from .widgets.panels import SideItem


class EduApp(CodroneApp):
    KIND = "edu"
    TITLE_TEXT = "CoDrone EDU"
    OWN_THEME = "codrone-edu"

    def quick_sections(self):
        base = super().quick_sections()
        base[0][1].insert(1, SideItem("ports", "ports"))
        return base + [
            ("TRICKS", [
                SideItem("flip back", "flip back", hint="≥50%"),
                SideItem("flip front", "flip front"),
                SideItem("square", "shape square"),
                SideItem("circle", "shape circle"),
                SideItem("spiral", "shape spiral"),
            ]),
            ("GADGETS", [
                SideItem("led red", "led 255 0 0"),
                SideItem("led green", "led 0 255 0"),
                SideItem("led off", "led off"),
                SideItem("beep", "beep"),
            ]),
        ]
