"""Background telemetry sampling with a bounded history for the btop-style graphs."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from .base import DroneBackend, Telemetry


class TelemetryPoller:
    def __init__(
        self,
        backend: DroneBackend,
        hz: float = 5.0,
        on_sample: Callable[[Telemetry], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        history: int = 240,
    ) -> None:
        self.backend = backend
        self.period = 1.0 / max(0.5, hz)
        self.on_sample = on_sample
        self.on_error = on_error
        self.history: deque[Telemetry] = deque(maxlen=history)
        self.latest: Telemetry | None = None
        self.errors = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def series(self, attr: str) -> list[float]:
        """History of one numeric Telemetry field, skipping gaps (``None``)."""
        out: list[float] = []
        for t in list(self.history):
            v = getattr(t, attr, None)
            if isinstance(v, (int, float)):
                out.append(float(v))
        return out

    def _run(self) -> None:
        last_error = ""
        while not self._stop.is_set():
            began = time.time()
            if self.backend.connected:
                try:
                    t = self.backend.read_telemetry()
                    self.latest = t
                    self.history.append(t)
                    self.samples += 1
                    if self.on_sample:
                        self.on_sample(t)
                except BaseException as exc:  # BaseException on purpose, vendor lib can throw SystemExit
                    self.errors += 1
                    msg = f"{type(exc).__name__}: {exc}"
                    if msg != last_error and self.on_error:
                        self.on_error(msg)
                    last_error = msg
            self._stop.wait(max(0.0, self.period - (time.time() - began)))
