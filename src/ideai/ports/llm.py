"""Porta LLM (§3.2) e tipo minimo del rate limiter usato dal gateway.

Il gateway è l'unico componente che parla con i modelli; la pipeline conosce solo
``LLMProvider``. ``RateLimiterLike`` è il sottoinsieme di ``ideai.runtime.ratelimit``
di cui il gateway ha bisogno (§6.7): la definizione vive qui per non creare una
dipendenza circolare fra l'adapter LLM e il runtime.
"""

from __future__ import annotations

from typing import Protocol

from ideai.domain import LLMResult


class LLMProvider(Protocol):
    """Ruolo LLM (triage, analyst, embed) con modello, prompt e schema propri (§7.1)."""

    name: str

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> LLMResult: ...

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]: ...


class RateLimiterLike(Protocol):
    """Sottoinsieme di :class:`ideai.runtime.ratelimit.RateLimiter` (§6.7)."""

    async def acquire(self, bucket: str, requests_per_minute: int, window_s: int) -> None: ...
