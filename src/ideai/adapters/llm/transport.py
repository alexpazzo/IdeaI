"""Trasporti LLM: forme grezze e protocollo comune (§7.2).

Un trasporto incapsula **come** si parla a un backend (``openai_compat``,
``ollama_native``, ``anthropic``); il gateway si occupa di cache, budget,
riparazione e contabilizzazione. I trasporti non conoscono né la cache né il
database: ricevono testo e schema, restituiscono testo, token e latenza.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RawCompletion:
    """Risposta grezza di una chat completion (JSON ancora da validare)."""

    content: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


@dataclass(frozen=True)
class RawEmbedding:
    """Vettori grezzi prodotti dal backend di embedding."""

    vectors: list[list[float]]
    tokens_in: int
    latency_ms: int


class LlmTransport(Protocol):
    """Protocollo dei trasporti concreti; una istanza per backend dichiarato."""

    name: str

    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> RawCompletion: ...

    async def embed(self, *, model: str, texts: list[str]) -> RawEmbedding: ...

    async def available_models(self) -> list[str]: ...

    async def pull(self, model: str, progress: Callable[[str], None]) -> None: ...
