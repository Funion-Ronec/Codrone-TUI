"""Pre-flight checklist (manual 2.4.1: "required before each match").

Items the TUI can verify from telemetry are computed; the rest need an explicit human tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..backend.base import Telemetry

# just our team default, NOT a RECF number. manual (2.1.2) only says a low battery drone cant launch, no percent given
DEFAULT_MIN_BATTERY = 40


@dataclass(frozen=True)
class PreflightItem:
    key: str
    label: str
    manual_ref: str
    auto: bool


ITEMS: tuple[PreflightItem, ...] = (
    PreflightItem("connected", "Drone link is up", "", True),
    PreflightItem("battery", "Battery charged (no low-battery indication)", "2.1.2", True),
    PreflightItem("flight_state", "Drone reports Ready (not flying, no error)", "", True),
    PreflightItem("firmware", "Firmware is on Robolink's approved list", "2.1.1", False),
    PreflightItem("unmodified", "Drone and electronics unmodified, official parts only", "2.1.2", False),
    PreflightItem("eye_protection", "Everyone at the field is wearing eye protection", "2.4.3", False),
    PreflightItem("flight_zone", "Flying only inside the designated flight zone, area clear", "2.4.2", False),
    PreflightItem("observer", "Visual observer in place and ready to catch the drone", "4.1.1", False),
    PreflightItem("antenna", "Controller / antenna placed for a reliable link", "4.1.2", False),
    PreflightItem("start_position", "Drone resting on the first foot of the programming mat", "3.2.2", False),
)


@dataclass(frozen=True)
class PreflightResult:
    item: PreflightItem
    status: str
    detail: str = ""


def run_preflight(
    telemetry: Telemetry | None,
    connected: bool,
    min_battery: int = DEFAULT_MIN_BATTERY,
    confirmed: Iterable[str] = (),
) -> list[PreflightResult]:
    done = set(confirmed)
    out: list[PreflightResult] = []
    for item in ITEMS:
        if item.auto:
            status, detail = _auto_check(item.key, telemetry, connected, min_battery)
        elif item.key in done:
            status, detail = "pass", "confirmed"
        else:
            status, detail = "todo", "needs confirmation"
        out.append(PreflightResult(item, status, detail))
    return out


def preflight_ok(results: Iterable[PreflightResult]) -> bool:
    return all(r.status == "pass" for r in results)


def _auto_check(key: str, t: Telemetry | None, connected: bool, min_battery: int) -> tuple[str, str]:
    if key == "connected":
        return ("pass", "connected") if connected else ("fail", "not connected")
    if not connected:
        return "fail", "not connected"
    if t is None:
        return "todo", "waiting for telemetry"
    if key == "battery":
        if t.battery is None:
            return "pass", "n/a (this backend does not report battery, e.g. the simulator)"
        if t.battery >= min_battery:
            return "pass", f"{t.battery}% (min {min_battery}%)"
        return "fail", f"{t.battery}% is below the team minimum of {min_battery}%"
    if key == "flight_state":
        if t.flight_state is None:
            return "pass", "n/a (not reported)"
        if t.flight_state.lower() == "ready":
            return "pass", t.flight_state
        return "fail", f"state is {t.flight_state}"
    return "todo", ""
