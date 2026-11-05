"""Runs blocking drone calls one at a time on a worker thread, so the UI never freezes."""

from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import BackendError


class Aborted(Exception):
    """Raised by ``FlightExecutor.check()`` inside a job after ``abort()`` was requested."""


@dataclass
class Job:
    id: int
    label: str
    fn: Callable[[], Any]
    state: str = "queued"  # queued | running | done | failed | cancelled
    error: str | None = None
    result: Any = None
    submitted: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.finished or time.time()) - self.started


Listener = Callable[[str, Job], None]


class FlightExecutor:
    """Single worker thread + FIFO queue.

    ``abort()`` is the soft stop: it empties the queue and sets ``stop_event`` so that a
    cooperative job (a mission routine) can bail out between steps by calling ``check()``.
    It cannot interrupt a blocking library call; for that use the backend's
    ``emergency_stop()`` / ``land()``, which are called from the *caller's* thread.
    """

    def __init__(self, listener: Listener | None = None, history: int = 50) -> None:
        self._listener = listener
        self._q: "queue.Queue[Job | None]" = queue.Queue()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self.current: Job | None = None
        self.jobs: deque[Job] = deque(maxlen=history)
        self._thread = threading.Thread(target=self._run, name="flight-executor", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- public API
    def submit(self, label: str, fn: Callable[[], Any]) -> Job:
        job = Job(next(self._ids), label, fn)
        with self._lock:
            self.jobs.append(job)
        self._emit("queued", job)
        self._q.put(job)
        return job

    @property
    def busy(self) -> bool:
        return self.current is not None or not self._q.empty()

    @property
    def pending(self) -> list[Job]:
        with self._lock:
            return [j for j in self.jobs if j.state == "queued"]

    def abort(self) -> int:
        """Drop everything queued and ask the running job to stop. Returns jobs dropped."""
        self.stop_event.set()
        dropped = 0
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                break
            if job is not None:
                job.state = "cancelled"
                job.finished = time.time()
                dropped += 1
                self._emit("cancelled", job)
        return dropped

    def check(self) -> None:
        """Call between steps of a long job; raises ``Aborted`` once ``abort()`` was requested."""
        if self.stop_event.is_set():
            raise Aborted()

    def sleep(self, seconds: float) -> None:
        """Interruptible sleep for jobs: wakes immediately on abort."""
        if self.stop_event.wait(max(0.0, seconds)):
            raise Aborted()

    def shutdown(self) -> None:
        self.abort()
        self._q.put(None)

    # --------------------------------------------------------------- internals
    def _emit(self, kind: str, job: Job) -> None:
        if self._listener is not None:
            try:
                self._listener(kind, job)
            except Exception:  # a UI bug must never kill the flight thread
                pass

    def _run(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            if job.state == "cancelled":
                continue
            self.stop_event.clear()
            self.current = job
            job.state, job.started = "running", time.time()
            self._emit("started", job)
            try:
                job.result = job.fn()
                job.state = "done"
            except Aborted:
                job.state = "cancelled"
            except SystemExit as exc:  # codrone_edu calls exit() when it cannot connect
                job.state, job.error = "failed", f"library tried to exit ({exc.code!r})"
            except BackendError as exc:
                job.state, job.error = "failed", str(exc)
            except Exception as exc:  # noqa: BLE001 - report anything the library throws
                job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
            finally:
                job.finished = time.time()
                self.current = None
                self._emit({"done": "finished"}.get(job.state, job.state), job)
