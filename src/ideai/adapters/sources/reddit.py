"""Adapter Reddit primario via OAuth (§5.3), costruito su Async PRAW.

Il client Async PRAW è creato **lazy** all'interno di ``_client()``: importare il modulo
e normalizzare un payload non richiede credenziali né rete, così i test di contratto
girano offline. La conversione da oggetti PRAW a dict puri è separata dalla
``normalize`` pura e sincrona, che consuma quei dict (stessa forma dei file di fixture).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import asyncpraw

from ideai.config import Settings
from ideai.domain import (
    Cursor,
    ExternalRef,
    FetchPage,
    NonRetryableError,
    NormalizedItem,
    RatePolicy,
    SourceCapabilities,
    SourceTarget,
    author_hash,
    content_hash,
)

_REDDIT_BASE_URL = "https://www.reddit.com"


def _epoch_to_datetime(value: Any) -> datetime:
    """Epoch UTC → ``datetime`` con fuso orario."""
    return datetime.fromtimestamp(float(value), tz=UTC)


def _edited_to_datetime(value: Any) -> datetime | None:
    """``edited`` di Reddit vale ``False`` oppure un epoch; ``False`` → ``None``."""
    if value is None or value is False:
        return None
    return _epoch_to_datetime(value)


class RedditAdapter:
    """Sorgente primaria Reddit: going-forward su subreddit/thread/query, watch dei thread."""

    kind = "reddit"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._reddit: asyncpraw.Reddit | None = None

    # ------------------------------------------------------------------ #
    # Costruzione lazy del client
    # ------------------------------------------------------------------ #
    def _client(self) -> asyncpraw.Reddit:
        if self._reddit is None:
            settings = self._settings
            self._reddit = asyncpraw.Reddit(
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
                user_agent=settings.reddit_user_agent or "ideai/0.1.0 by /u/ideai",
                check_for_updates=False,
                ratelimit_seconds=min(10, max(1, settings.rate_limit_window_s)),
                validate_on_submit=False,
            )
        return self._reddit

    # ------------------------------------------------------------------ #
    # Capacità e politica di rate (nessuna rete)
    # ------------------------------------------------------------------ #
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            has_comments=True,
            supports_incremental=True,
            supports_backfill=False,
            supports_search=True,
        )

    def rate_policy(self) -> RatePolicy:
        rpm = self._settings.reddit_rpm
        return RatePolicy(
            requests_per_minute=rpm,
            burst=rpm,
            cost_per_call_usd=0.0,
            min_interval_ms=int(60000 / rpm),
        )

    # ------------------------------------------------------------------ #
    # Going-forward
    # ------------------------------------------------------------------ #
    async def fetch_new(self, target: SourceTarget, cursor: Cursor | None, limit: int) -> FetchPage:
        cursor = cursor or {}
        last_created_utc = cursor.get("last_created_utc")
        last_fullname = cursor.get("last_fullname")

        if target.target_kind == "feed":
            raise NonRetryableError(
                "reddit non supporta target_kind='feed': usa 'subreddit', 'thread' o 'query'"
            )

        reddit = self._client()
        payloads = await self._fetch_payloads(reddit, target, limit)
        if not payloads and target.target_kind == "subreddit":
            # ``new`` può non produrre nulla in subreddit poco attivi: ``hot`` recupera
            # il contenuto che sta salendo (§5.3).
            payloads = await self._hot_payloads(reddit, target, limit)

        fresh = [
            payload
            for payload in payloads
            if not self._already_seen(payload, last_created_utc, last_fullname)
        ]
        items = [self.normalize(payload) for payload in fresh]

        next_cursor: Cursor | None = None
        if items:
            newest = items[-1]
            next_cursor = {
                "last_fullname": newest.external_id,
                "last_created_utc": int(newest.created_at.timestamp()),
            }
        return FetchPage(items=items, next_cursor=next_cursor, exhausted=len(payloads) < limit)

    async def _fetch_payloads(
        self, reddit: asyncpraw.Reddit, target: SourceTarget, limit: int
    ) -> list[dict]:
        if target.target_kind == "subreddit":
            subreddit = await reddit.subreddit(target.target_ref)
            payloads = [
                self._submission_to_payload(submission)
                async for submission in subreddit.new(limit=limit)
            ]
        elif target.target_kind == "thread":
            payloads = [
                self._submission_to_payload(submission)
                async for submission in reddit.info(fullnames=[target.target_ref])
            ]
        elif target.target_kind == "query":
            subreddit = await reddit.subreddit("all")
            payloads = [
                self._submission_to_payload(submission)
                async for submission in subreddit.search(target.target_ref, limit=limit)
            ]
        else:
            raise NonRetryableError(
                f"target_kind {target.target_kind!r} non supportato dall'adapter reddit"
            )
        await self._respect_limits(reddit)
        return payloads

    async def _hot_payloads(
        self, reddit: asyncpraw.Reddit, target: SourceTarget, limit: int
    ) -> list[dict]:
        subreddit = await reddit.subreddit(target.target_ref)
        payloads = [
            self._submission_to_payload(submission)
            async for submission in subreddit.hot(limit=limit)
        ]
        await self._respect_limits(reddit)
        return payloads

    @staticmethod
    def _already_seen(
        payload: dict, last_created_utc: int | None, last_fullname: str | None
    ) -> bool:
        """Scarta riordini e item già visti (§5.4): fullname uguale o ``created_utc`` coperto."""
        if last_fullname is not None and payload.get("name") == last_fullname:
            return True
        created = payload.get("created_utc")
        return (
            last_created_utc is not None
            and created is not None
            and float(created) <= float(last_created_utc)
        )

    async def _respect_limits(self, reddit: asyncpraw.Reddit) -> None:
        """Se l'header di rate limit scende sotto il 10% del tetto, attende il reset (§5.3)."""
        limits = reddit.auth.limits or {}
        remaining = limits.get("remaining")
        reset_timestamp = limits.get("reset_timestamp")
        rpm = self._settings.reddit_rpm
        if remaining is None or reset_timestamp is None or remaining >= 0.1 * rpm:
            return
        delay = float(reset_timestamp) + 1 - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    # Watch di un thread (§5.5)
    # ------------------------------------------------------------------ #
    async def fetch_thread(self, thread_ref: ExternalRef, since: datetime | None) -> FetchPage:
        reddit = self._client()
        submission_id = thread_ref.external_id.removeprefix("t3_")
        submission = await reddit.submission(id=submission_id)
        # Il post di testa è sempre presente: serve al refresh di score/num_comments
        # del seed in modalità ``thread_full`` (§5.5).
        payloads = [self._submission_to_payload(submission)]
        await submission.comments.replace_more(limit=0)
        since_epoch = since.timestamp() if since is not None else None
        for comment in submission.comments.list():
            if since_epoch is None or float(comment.created_utc) > since_epoch:
                payloads.append(self._comment_to_payload(comment))
        await self._respect_limits(reddit)
        items = [self.normalize(payload) for payload in payloads]
        return FetchPage(items=items, next_cursor=None, exhausted=True)

    # ------------------------------------------------------------------ #
    # Conversione oggetti PRAW → dict puri
    # ------------------------------------------------------------------ #
    def _submission_to_payload(self, submission: Any) -> dict:
        author = submission.author.name if submission.author is not None else None
        return {
            "kind": "post",
            "id": submission.id,
            "name": f"t3_{submission.id}",
            "parent_id": None,
            "link_id": None,
            "title": submission.title,
            "selftext": submission.selftext,
            "body": None,
            "author": author,
            "permalink": submission.permalink,
            "score": submission.score,
            "num_comments": submission.num_comments,
            "created_utc": submission.created_utc,
            "edited": submission.edited,
            "removed_by_category": getattr(submission, "removed_by_category", None),
        }

    def _comment_to_payload(self, comment: Any) -> dict:
        author = comment.author.name if comment.author is not None else None
        return {
            "kind": "comment",
            "id": comment.id,
            "name": f"t1_{comment.id}",
            "parent_id": comment.parent_id,
            "link_id": comment.link_id,
            "title": None,
            "selftext": None,
            "body": comment.body,
            "author": author,
            "permalink": comment.permalink,
            "score": comment.score,
            "num_comments": None,
            "created_utc": comment.created_utc,
            "edited": comment.edited,
            "removed_by_category": None,
        }

    # ------------------------------------------------------------------ #
    # Normalizzazione (pura, sincrona)
    # ------------------------------------------------------------------ #
    def normalize(self, payload: dict) -> NormalizedItem:
        kind = payload["kind"]
        external_id = payload.get("name") or payload.get("id")
        if kind == "post":
            thread_external_id = external_id
            parent_external_id = None
            body = payload.get("selftext") or ""
            title = payload.get("title")
            num_comments = payload.get("num_comments")
        else:
            thread_external_id = payload.get("link_id") or external_id
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
            lang=None,  # Reddit non fornisce la lingua (divergenza 8 del piano)
            created_at=_epoch_to_datetime(payload["created_utc"]),
            edited_at=_edited_to_datetime(payload.get("edited")),
            raw=dict(payload),
            content_hash=content_hash(body),
        )
