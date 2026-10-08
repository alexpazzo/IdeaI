"""Adapter di backfill Reddit su Pullpush (§5.6).

``api.pullpush.io`` non richiede autenticazione e offre contenuto storico che l'API OAuth
non espone in modo economico. È una sorgente **distinta** da ``reddit`` perché ha rate
policy, affidabilità e resa diverse; la deduplicazione è garantita dai cancelli G0/G1 a
valle, non dalla chiave ``(source_id, external_id)`` (che resta comunque il vincolo locale).

A differenza di Async PRAW il client HTTP è creato lazy e i payload sono dict puri già in
uscita dall'API: ``normalize`` li consuma direttamente ed è pura e sincrona.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from ideai.config import Settings
from ideai.domain import (
    Cursor,
    ExternalRef,
    FetchPage,
    NonRetryableError,
    NormalizedItem,
    RatePolicy,
    SourceCapabilities,
    SourceRateLimited,
    SourceTarget,
    SourceUnavailable,
    author_hash,
    content_hash,
)

_BASE_URL = "https://api.pullpush.io"
_REDDIT_BASE_URL = "https://www.reddit.com"


def _epoch_to_datetime(value: Any) -> datetime:
    return datetime.fromtimestamp(float(value), tz=UTC)


def _edited_to_datetime(value: Any) -> datetime | None:
    if value is None or value is False:
        return None
    return _epoch_to_datetime(value)


class RedditPullpushAdapter:
    """Cold-start di un subreddit con contenuto storico, e lettura dei commenti di un thread."""

    kind = "reddit_pullpush"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=_BASE_URL, timeout=30.0)
        return self._client

    # ------------------------------------------------------------------ #
    # Capacità e politica di rate (nessuna rete)
    # ------------------------------------------------------------------ #
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            has_comments=True,
            supports_incremental=True,
            supports_backfill=True,
            supports_search=False,
        )

    def rate_policy(self) -> RatePolicy:
        rpm = self._settings.pullpush_rpm
        return RatePolicy(
            requests_per_minute=rpm,
            burst=rpm,
            cost_per_call_usd=0.0,
            min_interval_ms=int(60000 / rpm),
        )

    # ------------------------------------------------------------------ #
    # Lettura
    # ------------------------------------------------------------------ #
    async def fetch_new(self, target: SourceTarget, cursor: Cursor | None, limit: int) -> FetchPage:
        cursor = cursor or {}
        before = cursor.get("before")

        if target.target_kind == "subreddit":
            endpoint = "submission"
            params: dict[str, Any] = {
                "subreddit": target.target_ref.removeprefix("r/"),
                "size": limit,
            }
        elif target.target_kind == "thread":
            endpoint = "comment"
            params = {"link_id": target.target_ref, "size": limit}
        else:
            raise NonRetryableError(
                "reddit_pullpush supporta solo target_kind 'subreddit' e 'thread'"
            )

        if before is not None:
            params["before"] = before

        payloads = await self._get(f"/reddit/search/{endpoint}/", params)
        next_cursor: Cursor | None = None
        if payloads:
            next_cursor = {"before": min(int(payload["created_utc"]) for payload in payloads)}
        items = [self.normalize(payload) for payload in payloads]
        return FetchPage(items=items, next_cursor=next_cursor, exhausted=len(payloads) < limit)

    async def fetch_thread(self, thread_ref: ExternalRef, since: datetime | None) -> FetchPage:
        limit = self._settings.scrape_page_size
        params: dict[str, Any] = {"link_id": thread_ref.external_id, "size": limit}
        if since is not None:
            params["after"] = int(since.timestamp())
        payloads = await self._get("/reddit/search/comment/", params)
        items = [self.normalize(payload) for payload in payloads]
        return FetchPage(items=items, next_cursor=None, exhausted=len(payloads) < limit)

    async def _get(self, path: str, params: dict[str, Any]) -> list[dict]:
        try:
            response = await self._http().get(path, params=params)
        except httpx.TimeoutException as exc:
            raise SourceUnavailable(f"pullpush non ha risposto in tempo: {exc}") from exc
        except httpx.TransportError as exc:
            raise SourceUnavailable(f"pullpush irraggiungibile: {exc}") from exc

        if response.status_code == 429:
            raise SourceRateLimited("pullpush ha risposto 429")
        if response.status_code >= 500:
            raise SourceUnavailable(f"pullpush ha risposto {response.status_code}")
        if response.status_code >= 400:
            raise NonRetryableError(f"pullpush ha risposto {response.status_code}")

        payload = response.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return list(data)

    # ------------------------------------------------------------------ #
    # Normalizzazione (pura, sincrona)
    # ------------------------------------------------------------------ #
    def normalize(self, payload: dict) -> NormalizedItem:
        kind = self._kind_of(payload)
        raw_id = str(payload["id"])
        if kind == "post":
            external_id = raw_id if raw_id.startswith("t3_") else f"t3_{raw_id}"
            thread_external_id = external_id
            parent_external_id = None
            body = payload.get("selftext") or ""
            title = payload.get("title")
            num_comments = payload.get("num_comments")
        else:
            external_id = raw_id if raw_id.startswith("t1_") else f"t1_{raw_id}"
            thread_external_id = self._thread_from_link(
                payload.get("link_id") or payload.get("link")
            )
            parent_external_id = payload.get("parent_id")
            body = payload.get("body") or ""
            title = None
            num_comments = None

        permalink = payload.get("permalink")
        url = f"{_REDDIT_BASE_URL}{permalink}" if permalink else None
        return NormalizedItem(
            source_kind=self.kind,
            external_id=external_id,
            kind=kind,
            thread_external_id=thread_external_id,
            parent_external_id=parent_external_id,
            title=title,
            body=body,
            author_hash=author_hash(payload.get("author"), self._settings.salt),
            url=url,
            score=payload.get("score"),
            num_comments=num_comments,
            lang=None,
            created_at=_epoch_to_datetime(payload["created_utc"]),
            edited_at=_edited_to_datetime(payload.get("edited")),
            raw=dict(payload),
            content_hash=content_hash(body),
        )

    @staticmethod
    def _kind_of(payload: dict) -> str:
        """Pullpush non marca il tipo: un commento ha ``parent_id``/``link_id``/``link``."""
        if payload.get("parent_id") or payload.get("link_id") or payload.get("link"):
            return "comment"
        return "post"

    @staticmethod
    def _thread_from_link(value: str | None) -> str | None:
        """Ricava il fullname ``t3_*`` del thread da ``link_id`` o dal permalink ``link``."""
        if not value:
            return None
        if value.startswith("t3_"):
            return value
        marker = "/comments/"
        if marker in value:
            return f"t3_{value.split(marker, 1)[1].split('/')[0]}"
        return f"t3_{value}"
