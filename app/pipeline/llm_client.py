"""Minimal Bedrock chat client (Converse API, with OpenAI-compatible fallback).

* Official AWS host for the configured region only; no redirects; key never logged or returned.
* Returns text + token usage + latency so cost can be computed.
* Reasoning blocks (reasoningContent / <think>) are discarded; only the final answer is returned.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    stop_reason: str | None = None
    api: str = ""
    model: str = ""
    raw_keys: list[str] = field(default_factory=list)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.removeprefix("export ").split("=", 1)
                values[k.strip()] = v.strip().strip("\"'")
    # EC2/systemd supplies these directly. They must work even when no .env file
    # exists in the release directory.
    keys = set(values) | {"AWS_REGION", "AWS_DEFAULT_REGION", "AWS_BEARER_TOKEN_BEDROCK",
                          "EV_LLM_MODEL", "EV_LLM_FALLBACK_MODEL"}
    return {**values, **{k: os.environ[k] for k in keys if k in os.environ}}


def _strip_reasoning(text: str) -> str:
    text = text.strip()
    for tag in ("think", "reasoning"):
        if text.startswith(f"<{tag}>") and f"</{tag}>" in text:
            text = text.split(f"</{tag}>", 1)[1].strip()
    return text


class BedrockClient:
    def __init__(self, timeout: float = 120):
        cfg = _env()
        self.region = cfg.get("AWS_REGION") or cfg.get("AWS_DEFAULT_REGION") or "ap-southeast-2"
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", self.region):
            raise LLMError("Invalid AWS_REGION.")
        self._key = cfg.get("AWS_BEARER_TOKEN_BEDROCK", "")
        if not self._key:
            raise LLMError("AWS_BEARER_TOKEN_BEDROCK is not set in .env.")
        self.host = f"bedrock-runtime.{self.region}.amazonaws.com"
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"https://{self.host}{path}", data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read(4_000_000))
        except urllib.error.HTTPError as exc:
            body = exc.read(600).decode("utf-8", "replace")
            message = re.sub(r"(?i)bearer\s+\S+", "bearer ***", body)
            raise LLMError(f"HTTP {exc.code}: {message[:200]}", exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"connection problem ({type(exc).__name__})") from None
        except ValueError:
            raise LLMError("response was not valid JSON") from None

    def converse(self, model: str, system: str, user: str, *, max_tokens: int, temperature: float = 0.1) -> Completion:
        payload = {
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        start = time.perf_counter()
        _, data = self._post(f"/model/{urllib.parse.quote(model, safe='')}/converse", payload)
        elapsed = time.perf_counter() - start
        blocks = data.get("output", {}).get("message", {}).get("content", [])
        text = "".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b)
        usage = data.get("usage", {})
        return Completion(_strip_reasoning(text), int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0)),
                          elapsed, data.get("stopReason"), "converse", model, sorted({k for b in blocks for k in b}))

    def openai(self, model: str, system: str, user: str, *, max_tokens: int, temperature: float = 0.1) -> Completion:
        payload = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        start = time.perf_counter()
        _, data = self._post("/openai/v1/chat/completions", payload)
        elapsed = time.perf_counter() - start
        choice = data["choices"][0]
        usage = data.get("usage", {})
        return Completion(_strip_reasoning(choice["message"].get("content") or ""), int(usage.get("prompt_tokens", 0)),
                          int(usage.get("completion_tokens", 0)), elapsed, choice.get("finish_reason"), "openai", model)

    def complete(self, model: str, system: str, user: str, *, max_tokens: int, temperature: float = 0.1, api: str | None = None) -> Completion:
        """Try the requested API first, then the other one, and report which worked."""
        order = [api] if api else ["converse", "openai"]
        if api is None:
            pass
        last: LLMError | None = None
        for which in order:
            try:
                return getattr(self, which)(model, system, user, max_tokens=max_tokens, temperature=temperature)
            except LLMError as exc:
                last = exc
                if exc.status in (401, 403, 429):      # auth / quota: the other API will not help
                    break
        raise last or LLMError("no API attempted")
