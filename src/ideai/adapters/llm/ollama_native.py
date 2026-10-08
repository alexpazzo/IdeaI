"""Trasporto nativo Ollama (§7.2).

Usato quando serve ``format`` con un JSON Schema completo (decoding vincolato da
grammatica, più stringente di ``json_object``). ``think: false`` è fisso nel
trasporto: gran parte del tempo di risposta di un modello reasoning sarebbe catena
di pensiero, e il ragionamento separato non deve entrare nel JSON.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

from ideai.adapters.llm.transport import RawCompletion, RawEmbedding
from ideai.config import BackendConfig
from ideai.domain import NonRetryableError, SourceUnavailable


class OllamaNativeTransport:
    """Client dell'API nativa Ollama (``/api/*``)."""

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

    async def _post(self, path: str, body: dict) -> tuple[httpx.Response, int]:
        started = time.perf_counter()
        try:
            response = await self._get_client().post(path, json=body)
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
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        response, latency_ms = await self._post("/api/chat", body)
        data = response.json()
        return RawCompletion(
            content=data["message"]["content"],
            tokens_in=int(data.get("prompt_eval_count") or 0),
            tokens_out=int(data.get("eval_count") or 0),
            latency_ms=latency_ms,
        )

    async def embed(self, *, model: str, texts: list[str]) -> RawEmbedding:
        body = {"model": model, "input": texts}
        response, latency_ms = await self._post("/api/embed", body)
        data = response.json()
        return RawEmbedding(
            vectors=data["embeddings"],
            tokens_in=int(data.get("prompt_eval_count") or 0),
            latency_ms=latency_ms,
        )

    async def available_models(self) -> list[str]:
        try:
            response = await self._get_client().get("/api/tags")
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{self.name}: {exc!r}") from exc
        if response.status_code >= 400:
            raise SourceUnavailable(f"{self.name}: HTTP {response.status_code}")
        return [item["name"] for item in response.json().get("models", [])]

    async def pull(self, model: str, progress: Callable[[str], None]) -> None:
        """Scarica ``model`` trasmettendo l'avanzamento riga per riga (NDJSON)."""
        client = self._get_client()
        try:
            async with client.stream(
                "POST", "/api/pull", json={"model": model, "stream": True}
            ) as response:
                if response.status_code >= 400:
                    raise SourceUnavailable(f"{self.name}: HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise SourceUnavailable(f"{self.name}: pull fallito: {event['error']}")
                    if event.get("status"):
                        progress(str(event["status"]))
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{self.name}: {exc!r}") from exc
