"""Porta delle sorgenti (§3.1): il contratto che ogni adapter di ingestione rispetta.

Il core conosce solo questa porta e ``NormalizedItem``, mai Reddit o Pullpush: aggiungere
una sorgente significa aggiungere una classe in ``adapters/sources/`` e una riga in
``sources``, senza toccare pipeline, schema, scoring o dashboard (§5.1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ideai.domain import (
    Cursor,
    ExternalRef,
    FetchPage,
    NormalizedItem,
    RatePolicy,
    SourceCapabilities,
    SourceTarget,
)


class SourceAdapter(Protocol):
    """Adapter di una sorgente esterna (§3.1)."""

    kind: str  # "reddit", "reddit_pullpush", "hackernews", "rss", "discourse"

    def capabilities(self) -> SourceCapabilities:
        """Capacità dichiarate della sorgente (§5.1): commenti, incremental, backfill, search."""
        ...

    def rate_policy(self) -> RatePolicy:
        """Politica di cortesia: rpm, burst, costo per chiamata, intervallo minimo."""
        ...

    async def fetch_new(self, target: SourceTarget, cursor: Cursor | None, limit: int) -> FetchPage:
        """Legge il contenuto nuovo (going-forward) di un target, a partire dal cursore."""
        ...

    async def fetch_thread(self, thread_ref: ExternalRef, since: datetime | None) -> FetchPage:
        """Legge un thread (post + commenti) filtrando i commenti precedenti a ``since``."""
        ...

    def normalize(self, payload: dict) -> NormalizedItem:
        """Trasforma un payload grezzo della sorgente in un item canonico (pura, sincrona)."""
        ...
