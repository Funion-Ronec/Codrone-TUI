"""The phone camera: snapshots, MJPEG streams, and the stills live mode saves next to its decision log."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codrone_tui import commands
from codrone_tui.backend import FlightExecutor
from codrone_tui.bot import BotContext, BotSettings, World, WorldMock
from codrone_tui.bot.camera import Camera, CameraError, FrameRecorder, normalise_url, safe_url
from codrone_tui.bot.guided import GuidedRun
from codrone_tui.bot.procedure import Plan, Step

from test_bot_live import FIELD, make_shell
from test_bot_commands import drain

JPEG = b"\xff\xd8\xff\xe0" + b"fake jpeg body" + b"\xff\xd9"


class FakeCam(BaseHTTPRequestHandler):
    hits: list = []
    delay = 0.0

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeCam.hits.append((self.path, self.headers.get("Authorization")))
        if FakeCam.delay:
            time.sleep(FakeCam.delay)
        if self.path.startswith("/shot.jpg"):
            self._send("image/jpeg", JPEG)
        elif self.path == "/video":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            part = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n"
            try:
                for _ in range(50):  # a stream that goes on; the client must stop after one frame
                    self.wfile.write(part)
                    self.wfile.flush()
                    time.sleep(0.01)
            except OSError:
                pass
        elif self.path == "/":
            self._send("text/html", b"<html>IP Webcam</html>")
        else:
            self.send_response(404)
            self.end_headers()

    def _send(self, kind, body):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def cam():
    FakeCam.hits, FakeCam.delay = [], 0.0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCam)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_snapshot_stream_and_wrong_urls(cam):
    assert Camera(f"http://{cam}/shot.jpg").snapshot() == (JPEG, "jpg")
    assert Camera(f"http://{cam}/video").snapshot() == (JPEG, "jpg")  # first frame of the MJPEG stream
    with pytest.raises(CameraError, match="text/html, not a picture.*shot.jpg"):
        Camera(f"http://{cam}/").snapshot()
    with pytest.raises(CameraError, match="HTTP 404"):
        Camera(f"http://{cam}/nope").snapshot()
    with pytest.raises(CameraError, match="cannot reach"):
        Camera("http://127.0.0.1:1/shot.jpg", timeout_s=1).snapshot()
    assert normalise_url("192.168.1.5:8080/shot.jpg") == "http://192.168.1.5:8080/shot.jpg"
    with pytest.raises(CameraError):
        normalise_url("ftp://phone/shot.jpg")


def test_a_camera_login_is_sent_but_never_shown(cam):
    c = Camera(f"http://me:s3cret@{cam}/shot.jpg")
    assert c.snapshot()[0] == JPEG
    assert FakeCam.hits[-1][1] == "Basic bWU6czNjcmV0"  # me:s3cret
    assert "s3cret" not in repr(c) and "s3cret" not in safe_url(c.url)
    assert "s3cret" not in commands.redact(f"camera http://me:s3cret@{cam}/shot.jpg")


def test_recorder_never_waits_for_a_slow_camera(cam, tmp_path):
    FakeCam.delay = 0.3
    rec = FrameRecorder(Camera(f"http://{cam}/shot.jpg"), tmp_path / "frames")
    t0 = time.monotonic()
    assert rec.grab("001") == "001"
    assert rec.grab("002") is None  # still loading 001: skipped, not queued behind it
    assert time.monotonic() - t0 < 0.1
    rec.idle(2)
    rec.close()
    assert rec.saved == 1 and rec.skipped == 1 and (tmp_path / "frames" / "001.jpg").read_bytes() == JPEG


def _guided(tmp_path, camera, steps, every_s=2.0):
    s = BotSettings(settle_s=0.0, side_look_deg=0.0, camera_every_s=every_s)
    backend = WorldMock("edu", World(), time_scale=0.0)
    backend.connect(None)
    ex = FlightExecutor()
    ctx = BotContext(backend, ex, field=FIELD, settings=s, log=lambda m: None)
    said = []
    run = GuidedRun(Plan(text="look around", steps=[Step.from_dict(x) for x in steps]), s,
                    say=lambda t, k="info": said.append(t), record_dir=tmp_path, camera=camera)
    try:
        run.fly(ctx)
    finally:
        ex.shutdown()
    return run, said


def test_live_saves_a_frame_per_step_and_every_few_seconds(cam, tmp_path):
    steps = [{"op": "takeoff"}, {"op": "hover", "seconds": 0.1}, {"op": "move", "direction": "forward",
             "distance_cm": 50}, {"op": "land"}]
    run, said = _guided(tmp_path, Camera(f"http://{cam}/shot.jpg"), steps)
    assert run.ok
    lines = [json.loads(l) for l in run.record_path.read_text().splitlines()]
    folder = tmp_path / lines[0]["frames"]
    frames = [l["frame"] for l in lines[1:] if "frame" in l]
    assert frames and frames[0] == f"{folder.name}/001"
    for f in frames:
        assert (tmp_path / f"{f}.jpg").read_bytes() == JPEG
    assert "frame" in lines[-1] and lines[-1]["end"] == "done"
    assert any("camera frame(s) saved to" in t for t in said)


def test_a_dead_camera_does_not_stop_the_flight(tmp_path):
    run, said = _guided(tmp_path, Camera("http://127.0.0.1:1/shot.jpg", timeout_s=0.5),
                        [{"op": "takeoff"}, {"op": "land"}])
    assert run.ok and run.status == ["done", "done"]
    assert sum("cannot reach the camera" in t for t in said) == 1  # said once, not every frame


def test_camera_command(cam, tmp_path):
    sh, ctl = make_shell(tmp_path, "http://127.0.0.1:1")
    try:
        sh.run_line("camera")
        assert "camera off" in sh.host.text()
        sh.run_line(f"camera {cam}/shot.jpg")  # sets it and grabs one to prove it works
        out = sh.host.text()
        assert ctl.settings.camera_url == f"http://{cam}/shot.jpg" and "camera ok" in out
        assert list(tmp_path.glob("camera-test-*.jpg"))
        assert ctl.panel_state()["camera"] is True
        sh.run_line(f"camera {cam}/")
        assert "not a picture" in sh.host.text()
        sh.run_line("camera off")
        assert ctl.settings.camera_url == "" and ctl.panel_state()["camera"] is False
    finally:
        sh.host.executor.shutdown()
