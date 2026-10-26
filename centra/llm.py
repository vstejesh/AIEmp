"""Thin provider layer: one call, JSON in / JSON out, token accounting.
No framework: every prompt the system sends is visible in this repo."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx


def load_env() -> None:
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()

# Rough list prices in USD per 1M tokens (input, output); override with LLM_PRICE_IN/OUT.
# The free tier costs nothing; this shows what the run would cost on a paid key.
PRICES = {"gemini": (0.30, 2.50), "anthropic": (3.00, 15.00), "groq": (0.15, 0.60)}


COOLDOWN: dict[tuple[str, str], float] = {}  # (provider, model) -> rate-limited until (shared by all runs)


class LLMError(Exception):
    pass


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    models: set = field(default_factory=set)

    def add(self, tin: int, tout: int, secs: float, model: str):
        self.calls += 1
        self.tokens_in += tin
        self.tokens_out += tout
        self.seconds += secs
        self.models.add(model)

    def cost_usd(self, provider: str) -> float:
        pin, pout = PRICES.get(provider, (0, 0))
        pin = float(os.environ.get("LLM_PRICE_IN", pin))
        pout = float(os.environ.get("LLM_PRICE_OUT", pout))
        return round((self.tokens_in * pin + self.tokens_out * pout) / 1e6, 5)

    def as_dict(self, provider: str) -> dict:
        return {"calls": self.calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "llm_seconds": round(self.seconds, 1), "models": sorted(self.models),
                "est_cost_usd": self.cost_usd(provider)}


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


class LLM:
    def __init__(self, provider: str | None = None):
        # Ordered failover chain, e.g. LLM_PROVIDERS=groq,gemini
        self.providers = [x.strip() for x in (provider or os.environ.get(
            "LLM_PROVIDERS", os.environ.get("LLM_PROVIDER", "groq,gemini"))).split(",") if x.strip()]
        self.provider = self.providers[0]
        self.usage = Usage()
        self.http = httpx.Client(timeout=90, trust_env=False)  # ignore system proxies

    # ---------------------------------------------------------------- public
    def json(self, system: str, user: str, temperature: float = 0.2) -> dict:
        """One completion that must be a JSON object. Retries transient errors and
        one malformed-JSON reply (with the parse error fed back)."""
        last_err = None
        for attempt in range(2):
            text = self._complete(system, user if attempt == 0 else
                                  f"{user}\n\nYour previous reply was not valid JSON ({last_err}). Reply with one JSON object only.",
                                  temperature)
            try:
                out = _parse_json(text)
                if not isinstance(out, dict):
                    raise ValueError("top level must be an object")
                return out
            except (ValueError, json.JSONDecodeError) as e:
                last_err = str(e)[:120]
        raise LLMError(f"model did not return valid JSON: {last_err}")

    # ---------------------------------------------------------------- failover across providers and models
    def _complete(self, system: str, user: str, temperature: float) -> str:
        """Try every (provider, model) in order. A rate-limited model is put on cooldown
        for as long as the provider asks, and the call moves to the next one at once
        instead of sleeping. Only if all are cooling down do we wait for the earliest."""
        chain = []
        for p in self.providers:
            try:
                models, call = self._builder(p)(system, user, temperature)
            except LLMError:
                continue  # provider not configured (no key)
            chain += [(p, m, call) for m in models]
        if not chain:
            raise LLMError("no LLM provider is configured")
        errors, deadline = [], time.time() + 300
        while time.time() < deadline:
            ready = [c for c in chain if COOLDOWN.get((c[0], c[1]), 0) <= time.time()]
            if not ready:
                time.sleep(min(max(min(COOLDOWN[(c[0], c[1])] for c in chain) - time.time(), 1), 30))
                continue
            for p, m, call in ready:
                t0 = time.time()
                try:
                    text, tin, tout = call(m)
                    self.usage.add(tin, tout, time.time() - t0, f"{p}:{m}")
                    return text
                except (httpx.HTTPError, LLMError) as e:
                    errors.append(f"{p}:{m}: {str(e)[:100]}")
                    if not (isinstance(e, httpx.TransportError) or getattr(e, "retryable", False)):
                        COOLDOWN[(p, m)] = time.time() + 600   # misconfigured model: skip it for a while
                    else:
                        COOLDOWN[(p, m)] = time.time() + (getattr(e, "retry_after", None) or 8)
        raise LLMError("LLM call failed: " + " | ".join(errors[-3:]))

    def _builder(self, p: str):
        return {"gemini": self._gemini, "anthropic": self._anthropic,
                "groq": self._openai_compat_for("groq"), "openai_compat": self._openai_compat_for("openai_compat")}[p]

    def _openai_compat_for(self, p: str):
        return lambda system, user, temperature: self._openai_compat(system, user, temperature, p)

    def _raise_for(self, r: httpx.Response):
        if r.status_code >= 400:
            e = LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
            # Groq's JSON mode occasionally rejects its own generation (400 json_validate_failed): transient.
            e.retryable = r.status_code in (429, 500, 502, 503, 504) or "validate JSON" in r.text
            m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", r.text)
            ra = r.headers.get("retry-after")
            e.retry_after = (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else (float(ra) if ra else None)
            raise e

    def _gemini(self, system: str, user: str, temperature: float) -> str:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY is not set")
        models = [m.strip() for m in os.environ.get(
            "GEMINI_MODELS", "gemini-3.1-flash-lite,gemini-flash-lite-latest").split(",")]

        def call(model):
            r = self.http.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": user}]}],
                      "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"}})
            self._raise_for(r)
            d = r.json()
            try:
                text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
            except (KeyError, IndexError):
                e = LLMError(f"empty response: {str(d)[:200]}")
                e.retryable = True
                raise e
            u = d.get("usageMetadata", {})
            return text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)

        return models, call

    def _openai_compat(self, system: str, user: str, temperature: float, provider: str = "groq"):
        """Groq, or any OpenAI-compatible server (Ollama, llama.cpp, vLLM) via OPENAI_COMPAT_URL."""
        if provider == "groq":
            url, key = "https://api.groq.com/openai/v1", os.environ.get("GROQ_API_KEY")
            models = os.environ.get("GROQ_MODELS", "openai/gpt-oss-20b")
        else:
            url, key = os.environ.get("OPENAI_COMPAT_URL", "http://localhost:11434/v1"), os.environ.get("OPENAI_COMPAT_KEY", "none")
            models = os.environ.get("OPENAI_COMPAT_MODELS", "qwen2.5:7b")
        if not key:
            raise LLMError(f"API key for {provider} is not set")

        def call(model):
            r = self.http.post(f"{url}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                               json={"model": model, "temperature": temperature,
                                     "response_format": {"type": "json_object"},
                                     "messages": [{"role": "system", "content": system},
                                                  {"role": "user", "content": user}]})
            self._raise_for(r)
            d = r.json()
            u = d.get("usage", {})
            return d["choices"][0]["message"]["content"] or "", u.get("prompt_tokens", 0), u.get("completion_tokens", 0)

        return [m.strip() for m in models.split(",")], call

    def _anthropic(self, system: str, user: str, temperature: float) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        models = [os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")]

        def call(model):
            r = self.http.post("https://api.anthropic.com/v1/messages",
                               headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                               json={"model": model, "max_tokens": 2048, "temperature": temperature,
                                     "system": system + "\nReply with a single JSON object and nothing else.",
                                     "messages": [{"role": "user", "content": user}]})
            self._raise_for(r)
            d = r.json()
            text = "".join(b.get("text", "") for b in d["content"] if b["type"] == "text")
            return text, d["usage"]["input_tokens"], d["usage"]["output_tokens"]

        return models, call
