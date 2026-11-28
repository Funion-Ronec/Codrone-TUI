# Language model providers for the bot: the local Ollama server, plus hosted APIs reached with an API key
# (Anthropic, and anything that speaks the OpenAI chat-completions format: OpenAI, OpenRouter, Groq, LM Studio...).
# Every client has the same two calls: chat_json(messages, schema) -> (object, meta) and models().
#
# API keys: never in config.toml (it lives in the project folder). A key comes from, in this order, the session
# (`llm key` dialog), the provider's environment variable, or keys.toml in the user's config folder (mode 0600).

from __future__ import annotations

import json
import os
import socket
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .ollama import OllamaClient, OllamaError

PROVIDERS = ("ollama", "anthropic", "openai")
API_PROVIDERS = ("anthropic", "openai")
DEFAULT_URL = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}
DEFAULT_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
# A fast model by default: `live` waits for the guidance before it takes off.
DEFAULT_MODEL = {"anthropic": "claude-haiku-5-5", "openai": ""}
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 2048

_OPENER = urllib.request.build_opener()


class LlmError(OllamaError):
    # subclass of OllamaError so existing `except OllamaError` handlers cover every provider
    pass


def mask(key: str) -> str:
    return "…" + key[-4:] if len(key) > 8 else "set"


def _local(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def check_api_url(url: str) -> None:
    # Keys must not cross the network in clear text; plain http is fine only to this machine (LM Studio, vLLM...).
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and _local(url)):
        return
    raise LlmError(f"refusing to send an API key to {url}: use https (plain http only works for localhost)")


# keys ------------------------------------------------------------------------------------------------------

def key_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (os.environ.get("APPDATA") if os.name == "nt" else None)
    return Path(base or Path.home() / ".config") / "codrone-tui" / "keys.toml"


class KeyStore:
    def __init__(self, path: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.path = path or key_file()
        self._env = env  # tests pass a dict; None means os.environ
        self._session: dict[str, str] = {}

    def _environ(self) -> dict[str, str]:
        return os.environ if self._env is None else self._env  # type: ignore[return-value]

    def _saved(self) -> dict[str, str]:
        try:
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, str) and v}

    def lookup(self, provider: str, env_name: str = "") -> tuple[str | None, str]:
        # (key, where it came from)
        if provider in self._session:
            return self._session[provider], "this session"
        name = env_name or DEFAULT_KEY_ENV.get(provider, "")
        if name and self._environ().get(name, "").strip():
            return self._environ()[name].strip(), f"${name}"
        saved = self._saved().get(provider)
        if saved:
            return saved, str(self.path)
        return None, ""

    def set(self, provider: str, key: str, save: bool = False) -> None:
        key = key.strip()
        if not key or any(c.isspace() for c in key):
            raise LlmError("that doesn't look like an API key (empty or contains spaces)")
        self._session[provider] = key
        if save:
            data = self._saved()
            data[provider] = key
            self._write(data)

    def forget(self, provider: str) -> bool:
        had = self._session.pop(provider, None) is not None
        data = self._saved()
        if provider in data:
            del data[provider]
            self._write(data)
            had = True
        return had

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "# API keys for Codrone-TUI's bot. Keep this file private.\n" + "".join(
            f"{k} = {json.dumps(v)}\n" for k, v in sorted(data.items()))
        # write a private temp file next to it, then swap it in: the key is never readable by others, even briefly
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".keys-")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# hosted clients ----------------------------------------------------------------------------------------------

class _ApiClient:
    provider = ""

    def __init__(self, url: str, model: str, key: str, timeout_s: float = 60.0, max_tokens: int = MAX_TOKENS) -> None:
        self.max_tokens = int(max_tokens)
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self._key = key
        check_api_url(self.url)

    def __repr__(self) -> str:  # never show the key in a traceback or log
        return f"{type(self).__name__}(url={self.url!r}, model={self.model!r})"

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", **self._headers()}
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        name = self.provider.capitalize()
        try:
            with _OPENER.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _api_error(exc)
            if exc.code in (401, 403):
                raise LlmError(f"{name} rejected the API key (HTTP {exc.code}): {detail} -- `llm key` sets a new one") from None
            if exc.code == 404 and body is not None:
                raise LlmError(f"{name} doesn't know model '{self.model}' ({detail}) -- `llm use <model>`") from None
            if exc.code == 429:
                raise LlmError(f"{name} rate limit or quota reached: {detail}") from None
            raise LlmError(f"{name} answered HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise LlmError(f"{name} did not answer within {self.timeout_s:.0f} s") from None
            raise LlmError(f"cannot reach {self.url} ({exc.reason})") from None
        except (TimeoutError, socket.timeout):
            raise LlmError(f"{name} did not answer within {self.timeout_s:.0f} s") from None
        except OSError as exc:
            raise LlmError(f"connection to {self.url} failed: {exc}") from None
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raise LlmError(f"{self.url} sent something that isn't JSON") from None
        return parsed if isinstance(parsed, dict) else {}


class AnthropicClient(_ApiClient):
    # Messages API. The answer is forced into the schema by making the model call one tool whose input is the schema.
    provider = "anthropic"

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION}

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        convo = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
        body: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens, "messages": convo,
            "tools": [{"name": "answer", "description": "Give your answer in this exact shape.", "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "answer"},
        }
        if system:
            body["system"] = system
        data = self._request("POST", "/v1/messages", body)
        for block in data.get("content") or []:
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                usage = data.get("usage") or {}
                return block["input"], {"tokens": usage.get("output_tokens")}
        raise LlmError(f"{self.model} gave no structured answer (stop reason: {data.get('stop_reason')})")

    def models(self) -> list[str]:
        data = self._request("GET", "/v1/models?limit=100", None)
        return sorted(str(m.get("id")) for m in data.get("data") or [] if m.get("id"))


class OpenAIClient(_ApiClient):
    # /chat/completions. Asks for a strict JSON schema; servers that don't support that get plain JSON mode.
    provider = "openai"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.model:
            raise LlmError("no model set for the OpenAI-compatible API -- `llm use <model>` or [bot] api_model")
        body: dict[str, Any] = {
            "model": self.model, "messages": messages, "max_completion_tokens": self.max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}},
        }
        try:
            data = self._request("POST", "/chat/completions", body)
        except LlmError as exc:
            if "HTTP 400" not in str(exc):
                raise
            # older or simpler servers: plain JSON mode, with the schema in the prompt instead
            hint = {"role": "system", "content": "Answer with one JSON object matching this schema:\n" + json.dumps(schema)}
            body.update(messages=[*messages, hint], response_format={"type": "json_object"})
            body.pop("max_completion_tokens", None)  # some servers only know max_tokens and reject the newer name
            data = self._request("POST", "/chat/completions", body)
        choices = data.get("choices") or [{}]
        content = ((choices[0] or {}).get("message") or {}).get("content") or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            raise LlmError(f"{self.model} did not answer with JSON: {content[:160]!r}") from None
        if not isinstance(parsed, dict):
            raise LlmError(f"{self.model} answered with {type(parsed).__name__}, not a JSON object")
        return parsed, {"tokens": (data.get("usage") or {}).get("completion_tokens")}

    def models(self) -> list[str]:
        data = self._request("GET", "/models", None)
        return sorted(str(m.get("id")) for m in data.get("data") or [] if m.get("id"))


def _api_error(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        err = json.loads(raw).get("error")
        if isinstance(err, dict):
            err = err.get("message")
        return str(err or raw)[:300]
    except Exception:  # noqa: BLE001
        return str(exc.reason)


# choosing a client -------------------------------------------------------------------------------------------

def model_for(settings) -> str:
    if settings.llm == "ollama":
        return settings.model
    return settings.api_model or DEFAULT_MODEL.get(settings.llm, "")


def make_client(settings, keys: KeyStore, timeout_s: float | None = None, max_tokens: int | None = None):
    provider = settings.llm
    timeout = settings.timeout_s if timeout_s is None else timeout_s
    if provider == "ollama":
        return OllamaClient(settings.ollama_url, settings.model, timeout, settings.think, max_tokens=max_tokens)
    if provider not in API_PROVIDERS:
        raise LlmError('the language model is off ([bot] llm = "off")')
    key, _ = keys.lookup(provider, settings.api_key_env)
    if not key:
        env = settings.api_key_env or DEFAULT_KEY_ENV[provider]
        raise LlmError(f"no {provider} API key: `llm key` enters one, or set ${env}")
    url = settings.api_url or DEFAULT_URL[provider]
    cls = AnthropicClient if provider == "anthropic" else OpenAIClient
    return cls(url, model_for(settings), key, timeout, max_tokens or MAX_TOKENS)
