"""Trasporto ``openai_compat`` (§7.2).

Parla con qualunque endpoint che esponga ``/chat/completions`` e ``/embeddings``:
Ollama, vLLM, llama.cpp server, LM Studio, Together, DeepSeek. Lo schema viene
serializzato nel messaggio di sistema e l'output è vincolato con
``response_format={"type": "json_object"}``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable

import httpx

from ideai.adapters.llm.transport import RawCompletion, RawEmbedding
from ideai.config import BackendConfig
from ideai.domain import NonRetryableError, SourceUnavailable


class OpenAiCompatTransport:
    """Client di un backend compatibile con l'API OpenAI."""

    def __init__(
        self,
        backend: BackendConfig,
        *,
        name: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.backend = backend
        self.name = name or backend.type
        self._client = client

    def _get_client(self) -> httpx.AsyncClient:
        """Client httpx creato al primo uso: la costruzione del trasporto non fa I/O."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.backend.base_url,
                timeout=self.backend.timeout_s,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        if not self.backend.api_key_env:
            return {}
        key = os.environ.get(self.backend.api_key_env)
        if not key:
            raise NonRetryableError(
                f"variabile d'ambiente {self.backend.api_key_env} non impostata "
                f"per il backend {self.name!r}"
            )
        return {"Authorization": f"Bearer {key}"}

    async def _post(self, path: str, body: dict) -> tuple[httpx.Response, int]:
        headers = self._headers()
        started = time.perf_counter()
        try:
            response = await self._get_client().post(path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{self.name}: {exc!r}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 500 or response.status_code == 429:
            raise SourceUnavailable(f"{self.name}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise NonRetryableError(
                f"{self.name}: HTTP {response.status_code}: {response.text[:500]}"
            )
        return response, latency_ms

    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> RawCompletion:
        body = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": system
                    + "\n\nSCHEMA JSON:\n"
                    + json.dumps(schema, ensure_ascii=False),
                },
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        response, latency_ms = await self._post("/chat/completions", body)
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return RawCompletion(
            content=content,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
        )

    async def embed(self, *, model: str, texts: list[str]) -> RawEmbedding:
        body = {"model": model, "input": texts}
        response, latency_ms = await self._post("/embeddings", body)
        data = response.json()
        vectors = [item["embedding"] for item in data["data"]]
        usage = data.get("usage") or {}
        return RawEmbedding(
            vectors=vectors,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            latency_ms=latency_ms,
        )

    async def available_models(self) -> list[str]:
        try:
            response = await self._get_client().get("/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{self.name}: {exc!r}") from exc
        if response.status_code >= 400:
            raise SourceUnavailable(f"{self.name}: HTTP {response.status_code}")
        return [item["id"] for item in response.json().get("data", [])]

    async def pull(self, model: str, progress: Callable[[str], None]) -> None:
        raise NotImplementedError(
            "pull è disponibile solo sul backend ollama_native; usa `ollama pull`"
        )
