# Front range sensor readings, filtered (median of a few samples) and sorted into hit / clear / unknown.

from __future__ import annotations

import statistics
from dataclasses import dataclass

from ..backend.base import BackendError, DroneBackend
from .settings import BotSettings

HIT = "hit"  # something is this many cm in front
CLEAR = "clear"  # nothing within the sensor's range
UNKNOWN = "unknown"  # no usable reading (the EDU reports -1000 mm, which telemetry turns into None)


class SensorError(BackendError):
    pass


@dataclass(frozen=True)
class Reading:
    kind: str
    cm: float | None = None

    @property
    def hit(self) -> bool:
        return self.kind == HIT

    def text(self, settings: BotSettings) -> str:
        if self.kind == HIT:
            return f"{self.cm:.0f} cm"
        if self.kind == CLEAR:
            return f"clear (>{settings.sensor_max_cm:.0f} cm)"
        return "no reading"


def classify(values: list[float | None], settings: BotSettings) -> Reading:
    valid = [float(v) for v in values if v is not None and v >= 0]
    if not valid:
        return Reading(CLEAR if settings.none_is_clear else UNKNOWN)
    d = statistics.median(valid)
    return Reading(CLEAR, d) if d > settings.sensor_max_cm else Reading(HIT, d)


def read_front(backend: DroneBackend, settings: BotSettings, samples: int | None = None) -> Reading:
    values: list[float | None] = []
    errors = 0
    for _ in range(max(1, settings.samples if samples is None else samples)):
        try:
            values.append(backend.read_telemetry().front_range_cm)
        except Exception:  # noqa: BLE001 - one flaky read is just a missing sample
            errors += 1
    if errors and not values:
        raise SensorError("the front range sensor could not be read")
    return classify(values, settings)
