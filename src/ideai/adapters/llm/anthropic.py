"""Trasporto ``anthropic`` (§7.2).

API Messages con un tool ``emit_result``: lo schema del ruolo diventa
l'``input_schema`` del tool e ``tool_choice`` forza il modello a restituire il
risultato strutturato. Anthropic non offre embedding: ``embed`` e ``pull``
sollevano ``NotImplementedError`` e ``available_models`` solleva ``ValueError``.
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

ANTHROPIC_VERSION = "2023-06-01"
TOOL_NAME = "emit_result"


class AnthropicTransport:
    """Client dell'API Messages di Anthropic."""

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
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.backend.base_url,
                timeout=self.backend.timeout_s,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        if not self.backend.api_key_env:
            raise NonRetryableError(f"backend {self.name!r}: api_key_env mancante")
        key = os.environ.get(self.backend.api_key_env)
        if not key:
            raise NonRetryableError(
                f"variabile d'ambiente {self.backend.api_key_env} non impostata "
                f"per il backend {self.name!r}"
            )
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}

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
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Restituisce il risultato strutturato",
                    "input_schema": schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "temperature": temperature,
        }
        started = time.perf_counter()
        try:
            response = await self._get_client().post(
                "/v1/messages", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{self.name}: {exc!r}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 500 or response.status_code == 429:
            raise SourceUnavailable(f"{self.name}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise NonRetryableError(
                f"{self.name}: HTTP {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        content = self._extract_tool_use(data)
        usage = data.get("usage") or {}
        return RawCompletion(
            content=content,
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            latency_ms=latency_ms,
        )

    @staticmethod
    def _extract_tool_use(data: dict) -> str:
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
                return json.dumps(block.get("input", {}), ensure_ascii=False)
        raise NonRetryableError(f"anthropic non ha restituito il tool {TOOL_NAME!r}")

    async def embed(self, *, model: str, texts: list[str]) -> RawEmbedding:
        raise NotImplementedError("anthropic non offre un endpoint di embedding")

    async def available_models(self) -> list[str]:
        raise ValueError("probe non disponibile per anthropic")

    async def pull(self, model: str, progress: Callable[[str], None]) -> None:
        raise NotImplementedError("pull è disponibile solo sul backend ollama_native")
