# Live mode: the language model turns what the pilot says into guidance once (a checked, simulated plan that may
# use the algorithm's behaviours: explore, home, land_spot), and the flight algorithm flies it on its own. No model
# calls while flying, so nothing waits on a model; every forward move creeps and stops for obstacles.
#
# GuidedRun is the flight: it runs the steps, keeps the progress the bot panel shows, records what happened to
# logs/bot-live-*.jsonl, and saves camera stills (one per step plus one every camera_every_s) next to it.

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from ..adc.routine import RoutineError
from ..backend.base import BackendError
from ..backend.executor import Aborted
from .camera import Camera, FrameRecorder
from .context import BotContext
from .navigator import Blocked
from .procedure import Plan, Step, run_step
from .settings import BotSettings

EVENTS_SHOWN = 8

PENDING, ACTIVE, DONE, STOPPED, FAILED = "pending", "active", "done", "stopped", "failed"


class GuidedRun:
    def __init__(
        self,
        plan: Plan,
        settings: BotSettings,
        *,
        say: Callable[[str, str], Any] = lambda text, kind="info": None,
        record_dir: Path | None = None,
        camera: Camera | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.plan = plan
        self.s = settings
        self.say = say
        self.record_dir = record_dir
        self.camera = camera
        self.clock = clock
        self.steps: list[Step] = list(plan.steps)
        # progress, read by the bot panel from the UI thread
        self.goal = plan.text
        self.phase = "ready"  # ready, flying, ended
        self.status = [PENDING] * len(self.steps)
        self.current = -1
        self.events: deque[str] = deque(maxlen=EVENTS_SHOWN)
        self.started = 0.0
        self.ended_s: float | None = None
        self.reason = ""
        self.ok = False
        self.landed_for_safety = False
        self.record_path: Path | None = None
        self.frames: FrameRecorder | None = None
        self._lock = threading.Lock()
        self._ctx: BotContext | None = None

    # the flight -------------------------------------------------------------------------------------------

    def fly(self, ctx: BotContext) -> None:
        self._ctx = ctx
        inner_log = ctx.log
        ctx.log = lambda m: (self._event(m), inner_log(m))  # the algorithm's own log lines become panel events
        if hasattr(ctx, "nav"):
            ctx.nav.log = ctx.log  # type: ignore[assignment]
        self.started, self.phase = self.clock(), "flying"
        self._open_record()
        stop_camera = threading.Event()
        cam_thread = None
        if self.frames is not None:
            cam_thread = threading.Thread(target=self._camera_loop, args=(stop_camera,), name="live-frames", daemon=True)
            cam_thread.start()
        aborted = False
        try:
            for i, st in enumerate(self.steps):
                ctx.check()
                self.current, self.status[i] = i, ACTIVE
                line = st.line()
                self._record({"event": "step", "step": i + 1, "line": line, "pose": self._pose(), "frame": self._grab()})
                self.say(f"live {i + 1}/{len(self.steps)}: {line}", "info")
                try:
                    run_step(ctx, st)
                except Aborted:
                    raise
                except Blocked as exc:
                    # the move was cancelled because something is in the way: that's the guard working, not a
                    # failure. The drone is hovering short of it; carry on with the next step.
                    self.status[i] = STOPPED
                    self._event(f"step {i + 1} cancelled: {exc}")
                    continue
                except (BackendError, RoutineError, KeyError, ValueError) as exc:
                    self.status[i] = FAILED
                    self.reason = f"step {i + 1} ({line}) failed: {exc}"
                    break
                self.status[i] = DONE
            else:
                self.ok = True
                self.reason = "done"
        except Aborted:
            aborted = True
            self.reason = "stopped by the operator"
            raise
        except BaseException as exc:
            self.reason = f"error: {type(exc).__name__}: {exc}"
            raise
        finally:
            b = ctx.backend
            # A failed step leaves nobody in charge of a hovering drone: land, unless the operator took over
            # (esc / ctrl+l / ctrl+x already decided) or the guidance meant to end in the air.
            if not aborted and not self.ok and b.flying:
                self.say(f"live: {self.reason} -- landing", "warn")
                try:
                    b.land()
                    self.landed_for_safety = True
                except Exception as exc:  # noqa: BLE001
                    self.say(f"live: LANDING FAILED ({exc}) -- land it by hand (ctrl+l)", "error")
            stop_camera.set()
            if cam_thread is not None:
                cam_thread.join(2.0)
            end: dict[str, Any] = {"end": self.reason, "ok": self.ok, "landed_for_safety": self.landed_for_safety,
                                   "pose": self._pose()}
            if self.frames is not None:
                self.frames.idle(self.camera.timeout_s + 1.0)
                end["frame"] = self._grab()
                self.frames.close()
                f = self.frames
                self.say(f"live: {f.saved} camera frame(s) saved to {f.folder}" +
                         (f" ({f.failed} failed, {f.skipped} skipped while the camera was slow)" if f.failed or f.skipped
                          else ""), "dim" if f.saved else "warn")
            self._record(end)
            self.ended_s = self.clock() - self.started
            self.phase = "ended"

    # progress and records -----------------------------------------------------------------------------------

    def _event(self, message: str) -> None:
        text = message.strip()
        if text:
            self.events.append(text)
            self._record({"event": "log", "step": self.current + 1, "text": text})

    def _pose(self) -> dict[str, float]:
        if self._ctx is None:
            return {}
        p = self._ctx.backend.pose
        return {"x_cm": round(p.x), "y_cm": round(p.y), "z_cm": round(p.z), "heading_deg": round(p.heading % 360.0)}

    def _open_record(self) -> None:
        if self.record_dir is None:
            if self.camera is not None:
                self.say("live: camera frames need the command log on (they go next to it in logs/)", "warn")
            return
        stem = f"bot-live-{time.strftime('%Y_%m_%d_%H_%M_%S')}"
        p = self.plan
        head: dict[str, Any] = {"goal": p.text, "summary": p.summary, "source": p.source,
                                "steps": [s.to_dict() for s in self.steps], "planned_s": round(p.seconds, 2),
                                "simulated_s": p.dry.duration_s if p.dry is not None else None,
                                "motion": self.s.motion, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        if self.camera is not None:
            self.frames = FrameRecorder(self.camera, self.record_dir / stem, say=self.say)
            head["frames"] = stem
        if self.s.live_record:
            self.record_path = self.record_dir / f"{stem}.jsonl"
            self._record(head)

    def _record(self, entry: dict[str, Any]) -> None:
        if self.record_path is None:
            return
        entry = {"t": round(self.clock() - self.started, 2), **{k: v for k, v in entry.items() if v is not None}}
        with self._lock:
            try:
                with self.record_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
            except OSError:
                self.record_path = None  # recording is a bonus; never stop a flight for it

    def _grab(self) -> str | None:
        f = self.frames
        if f is None:
            return None
        with self._lock:
            self._seq = getattr(self, "_seq", 0) + 1
            name = f"{self._seq:03d}"
        got = f.grab(name)
        return f"{f.folder.name}/{got}" if got else None

    def _camera_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.s.camera_every_s):
            frame = self._grab()
            if frame:
                self._record({"event": "frame", "step": self.current + 1, "pose": self._pose(), "frame": frame})
