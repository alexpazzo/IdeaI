"""Doppi deterministici per i test del gateway LLM e della pipeline.

``FakeTransport`` è un trasporto conforme a :class:`ideai.adapters.llm.transport.LlmTransport`
che risponde da una coda preparata (o da un callable), **registra** ogni chiamata
ricevuta — messaggio di sistema, messaggio utente, schema, token e latenza — e ne
conta il numero. È pensato per essere riusato dai test della pipeline (M5) oltre
che dai test del gateway (M3).

Esempio d'uso::

    transport = FakeTransport(['{"results": [...]}'])
    transport = FakeTransport(responder=lambda call: '{"results": []}')
    transport = FakeTransport(embed_fn=lambda text: [0.1, 0.2, 0.3, 0.4])

``NullLimiter`` è un ``RateLimiterLike`` che non blocca mai e registra le richieste.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ideai.adapters.llm.transport import RawCompletion, RawEmbedding

__all__ = ["FakeTransport", "NullLimiter", "RecordedCall"]


@dataclass
class RecordedCall:
    """Una chiamata ricevuta dal doppio: il contenuto esatto inviato dal gateway."""

    method: str  # "chat_json" | "embed"
    model: str
    system: str | None = None
    user: str | None = None
    schema: dict | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    texts: list[str] | None = None


class FakeTransport:
    """Trasporto sanitario deterministico.

    Parameters
    ----------
    responses:
        coda di risposte per ``chat_json``. Ogni elemento è una stringa JSON, un
        :class:`RawCompletion` già pronto, oppure un'eccezione da sollevare.
    responder:
        callable ``(RecordedCall) -> str | RawCompletion | BaseException`` valutato
        al posto della coda; utile per risposte dinamiche.
    default_response:
        risposta usata quando la coda è vuota e non c'è ``responder``.
    embed_fn:
        ``(text) -> list[float]``; il default produce un vettore deterministico di
        ``embed_dim`` componenti derivate da ``sha256(text)``.
    name:
        nome del backend esposto come ``LlmTransport.name``.
    """

    def __init__(
        self,
        responses: Iterable[object] | None = None,
        *,
        responder: Callable[[RecordedCall], object] | None = None,
        default_response: object | None = None,
        embed_fn: Callable[[str], list[float]] | None = None,
        name: str = "fake",
        embed_dim: int = 4,
    ) -> None:
        self.name = name
        self.embed_dim = embed_dim
        self._responses: deque[object] = deque(responses or [])
        self._responder = responder
        self._default = default_response
        self._embed_fn = embed_fn
        self.calls: list[RecordedCall] = []
        self.chat_calls = 0
        self.embed_calls = 0

    def queue(self, *responses: object) -> None:
        """Accoda altre risposte per le successive chiamate ``chat_json``."""
        self._responses.extend(responses)

    @property
    def requests(self) -> list[RecordedCall]:
        """Alias di ``calls``: le richieste ricevute, in ordine."""
        return self.calls

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
        self.chat_calls += 1
        call = RecordedCall(
            method="chat_json",
            model=model,
            system=system,
            user=user,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        self.calls.append(call)
        if self._responder is not None:
            result = self._responder(call)
        elif self._responses:
            result = self._responses.popleft()
        elif self._default is not None:
            result = self._default
        else:
            raise AssertionError("FakeTransport: nessuna risposta in coda per chat_json")
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, RawCompletion):
            return result
        return RawCompletion(content=str(result), tokens_in=7, tokens_out=3, latency_ms=1)

    async def embed(self, *, model: str, texts: list[str]) -> RawEmbedding:
        self.embed_calls += 1
        self.calls.append(RecordedCall(method="embed", model=model, texts=list(texts)))
        vectors = [self._embed(text) for text in texts]
        tokens = sum(len(text.split()) for text in texts)
        return RawEmbedding(vectors=vectors, tokens_in=tokens, latency_ms=1)

    def _embed(self, text: str) -> list[float]:
        if self._embed_fn is not None:
            return self._embed_fn(text)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i] / 255.0 for i in range(self.embed_dim)]

    async def available_models(self) -> list[str]:
        return ["fake-model"]

    async def pull(self, model: str, progress: Callable[[str], None]) -> None:
        raise NotImplementedError("pull non supportato dal doppio di test")


class NullLimiter:
    """``RateLimiterLike`` che non serializza nulla e registra le acquisizioni."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    async def acquire(self, bucket: str, requests_per_minute: int, window_s: int) -> None:
        self.calls.append((bucket, requests_per_minute, window_s))
