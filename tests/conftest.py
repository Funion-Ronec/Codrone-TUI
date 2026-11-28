import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture(autouse=True)
def real_asyncio_sleep():
    """The vendor codrone_edu module replaces asyncio.sleep when imported (see backend.edu.load_edu_lib);
    make sure no test leaks that into the next one."""
    yield
    asyncio.sleep = asyncio.tasks.sleep


class FakeOllama:
    """A tiny HTTP server standing in for Ollama: /api/chat answers with queued replies (a dict becomes the
    model's JSON content, a (status, message) tuple an error), /api/tags lists `models`. Requests are recorded."""

    def __init__(self, replies=(), models=("qwen3.5:9b",)):
        self.replies = list(replies)
        self.models = list(models)
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, {"models": [{"name": n} for n in owner.models]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                owner.requests.append(json.loads(self.rfile.read(length)))
                reply = owner.replies.pop(0) if owner.replies else {"summary": "", "steps": [], "questions": []}
                if isinstance(reply, tuple):
                    self._send(reply[0], {"error": reply[1]})
                else:
                    self._send(200, {"message": {"role": "assistant", "content": json.dumps(reply)},
                                     "total_duration": 1_500_000_000, "eval_count": 42})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama():
    servers = []

    def make(*replies, **kw):
        srv = FakeOllama(replies, **kw)
        servers.append(srv)
        return srv

    yield make
    for srv in servers:
        srv.close()
