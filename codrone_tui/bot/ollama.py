# Minimal client for a local Ollama server (REST API: POST /api/chat, GET /api/tags), standard library only.
# Replies are constrained with Ollama's `format` = JSON schema, so the model can only answer in the plan shape.

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

# Ollama runs on this machine or the LAN, so ignore any http_proxy settings
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, url: str = "http://127.0.0.1:11434", model: str = "qwen3.5:9b", timeout_s: float = 180.0,
                 think: bool | None = False, max_tokens: int | None = None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self.think = think  # None leaves the model's default
        self.max_tokens = max_tokens  # Ollama's num_predict; None = no limit

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, **({"num_predict": self.max_tokens} if self.max_tokens else {})},
            "keep_alive": "15m",
        }
        if self.think is not None:
            body["think"] = bool(self.think)
        try:
            data = self._request("POST", "/api/chat", body, for_model=True)
        except OllamaError as exc:
            if "think" not in body or "think" not in str(exc).lower():
                raise
            body.pop("think")  # models without a thinking switch reject the field
            data = self._request("POST", "/api/chat", body, for_model=True)
        content = (data.get("message") or {}).get("content") or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            raise OllamaError(f"{self.model} did not answer with JSON: {content[:160]!r}") from None
        if not isinstance(parsed, dict):
            raise OllamaError(f"{self.model} answered with {type(parsed).__name__}, not a JSON object")
        meta = {
            "seconds": round((data.get("total_duration") or 0) / 1e9, 2),
            "tokens": data.get("eval_count"),
        }
        return parsed, meta

    def models(self) -> list[str]:
        data = self._request("GET", "/api/tags", None, for_model=False)
        return sorted(str(m.get("name", "")) for m in data.get("models", []) if m.get("name"))

    def _request(self, method: str, path: str, body: dict[str, Any] | None, for_model: bool) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with _OPENER.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _error_text(exc)
            if for_model and (exc.code == 404 or "not found" in detail.lower()):
                raise OllamaError(f"model '{self.model}' is not in Ollama at {self.url} -- run "
                                  f"`ollama pull {self.model}`, or pick another with `llm use <model>`") from None
            raise OllamaError(f"Ollama answered HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise OllamaError(f"Ollama at {self.url} did not answer within {self.timeout_s:.0f} s") from None
            raise OllamaError(f"cannot reach Ollama at {self.url} ({reason}) -- is `ollama serve` running?") from None
        except (TimeoutError, socket.timeout):
            raise OllamaError(f"Ollama at {self.url} did not answer within {self.timeout_s:.0f} s") from None
        except OSError as exc:
            raise OllamaError(f"connection to Ollama at {self.url} failed: {exc}") from None
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raise OllamaError(f"Ollama at {self.url} sent something that isn't JSON") from None
        return parsed if isinstance(parsed, dict) else {}


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        return str(json.loads(raw).get("error") or raw)[:300]
    except Exception:  # noqa: BLE001 - the status line is enough then
        return str(exc.reason)
