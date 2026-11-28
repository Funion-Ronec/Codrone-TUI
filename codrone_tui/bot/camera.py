# A phone (or any network camera) watching the flight from outside. Live mode saves stills (one per step, and one
# every few seconds) next to the flight's log, so a run can be reviewed afterwards: where the drone thought it was
# and what the room actually looked like at that moment. The frames are for people (and reviewers) only; the model never sees them.
#
# Works with a snapshot URL (IP Webcam: http://<phone>:8080/shot.jpg) and with an MJPEG stream (the first frame is
# taken). Grabs run on one background thread and are skipped while the previous one is still loading, so a slow
# camera never holds the drone in a hover.

from __future__ import annotations

import base64
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_STREAM_SCAN = 4 * 1024 * 1024  # how much of an MJPEG stream to read looking for one whole frame
SOI, EOI = b"\xff\xd8", b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n"
HINT = "for IP Webcam (Android) use http://<phone-ip>:8080/shot.jpg; any URL that returns a JPEG or MJPEG works"

_OPENER = urllib.request.build_opener()


class CameraError(Exception):
    pass


def normalise_url(url: str) -> str:
    url = url.strip()
    if url and "://" not in url:
        url = "http://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CameraError(f"not a camera URL: {url!r} ({HINT})")
    return url


def safe_url(url: str) -> str:
    # what gets printed and logged: no user:password@ if the camera has a login
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        parts = parts._replace(netloc="***@" + host)
    return urllib.parse.urlunsplit(parts)


def _split_login(url: str) -> tuple[str, dict[str, str]]:
    # urllib can't take user:password@ in the URL itself; send it as basic auth instead
    parts = urllib.parse.urlsplit(url)
    if not (parts.username or parts.password):
        return url, {}
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    cred = f"{urllib.parse.unquote(parts.username or '')}:{urllib.parse.unquote(parts.password or '')}"
    plain = urllib.parse.urlunsplit(parts._replace(netloc=host))
    return plain, {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}


class Camera:
    def __init__(self, url: str, timeout_s: float = 3.0) -> None:
        self.url = normalise_url(url)
        self.timeout_s = float(timeout_s)

    def __repr__(self) -> str:
        return f"Camera({safe_url(self.url)!r})"

    def snapshot(self) -> tuple[bytes, str]:
        # (image bytes, file extension); raises CameraError
        plain, auth = _split_login(self.url)
        req = urllib.request.Request(plain, headers={"User-Agent": "codrone-tui", **auth})
        try:
            with _OPENER.open(req, timeout=self.timeout_s) as resp:
                kind = (resp.headers.get("Content-Type") or "").lower()
                if kind.startswith("multipart/"):
                    return _first_frame(resp), "jpg"
                data = resp.read(MAX_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise CameraError(f"camera answered HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise CameraError(f"cannot reach the camera at {safe_url(self.url)} ({exc.reason})") from None
        except (TimeoutError, OSError) as exc:
            raise CameraError(f"camera did not send a picture ({exc or 'timed out'})") from None
        if len(data) > MAX_IMAGE_BYTES:
            raise CameraError("camera sent more than 8 MB for one picture")
        if data.startswith(SOI):
            return data, "jpg"
        if data.startswith(PNG):
            return data, "png"
        what = kind.split(";")[0] or "something that isn't an image"
        raise CameraError(f"that URL sent {what}, not a picture -- {HINT}")


def _first_frame(resp) -> bytes:
    buf = b""
    while len(buf) < MAX_STREAM_SCAN:
        chunk = resp.read1(64 * 1024) if hasattr(resp, "read1") else resp.read(64 * 1024)
        if not chunk:
            break
        buf += chunk
        start = buf.find(SOI)
        if start >= 0:
            end = buf.find(EOI, start + 2)
            if end >= 0:
                return buf[start:end + 2]
    raise CameraError("no whole JPEG frame in the camera's stream")


class FrameRecorder:
    """Saves stills into one folder from a background thread. grab() never blocks."""

    def __init__(self, camera: Camera, folder: Path, say: Callable[[str, str], object] = lambda t, k="info": None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.camera = camera
        self.folder = folder
        self.say = say
        self.clock = clock
        self.saved = 0
        self.skipped = 0
        self.failed = 0
        self.last_error = ""
        self._busy = threading.Event()
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="live-camera", daemon=True)
        self._thread.start()

    def grab(self, name: str) -> str | None:
        # queue one still; returns its file name (without extension: the camera decides jpg or png), or None if
        # the last one is still loading
        if self._busy.is_set():
            self.skipped += 1
            return None
        self._busy.set()
        self._jobs.put(name)
        return name

    def idle(self, timeout_s: float) -> None:
        # wait for the grab in progress, if any
        deadline = self.clock() + timeout_s
        while self._busy.is_set() and self.clock() < deadline:
            time.sleep(0.02)

    def close(self, timeout_s: float = 5.0) -> None:
        self._jobs.put(None)
        self._thread.join(timeout_s)

    def _work(self) -> None:
        while True:
            name = self._jobs.get()
            if name is None:
                return
            try:
                data, ext = self.camera.snapshot()
                self.folder.mkdir(parents=True, exist_ok=True)
                (self.folder / f"{name}.{ext}").write_bytes(data)
                self.saved += 1
            except (CameraError, OSError) as exc:
                self.failed += 1
                if str(exc) != self.last_error:  # say each different problem once, not every frame
                    self.say(f"camera: {exc}", "warn")
                self.last_error = str(exc)
            finally:
                self._busy.clear()
