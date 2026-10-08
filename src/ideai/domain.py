"""Strutture dati condivise, enumerazioni di dominio ed errori tipizzati.

Le dataclass sono la copia verbatim di §3.4 del documento di architettura; le
enumerazioni riflettono i vincoli ``CHECK`` dell'Appendice B.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

Cursor = dict[str, Any]  # alias, non un dataclass: forma nota solo all'adapter che lo produce


# --------------------------------------------------------------------------- #
# Enum di dominio (vincoli CHECK dell'Appendice B)
# --------------------------------------------------------------------------- #
class ItemKind(StrEnum):
    POST = "post"
    COMMENT = "comment"


class ItemState(StrEnum):
    NEW = "new"
    TRIAGED = "triaged"
    REJECTED = "rejected"
    CANDIDATE = "candidate"
    ANALYZED = "analyzed"
    ARCHIVED = "archived"


class ItemCategory(StrEnum):
    PRODUCT_IDEA = "product_idea"
    PAIN_POINT = "pain_point"
    MARKET_SIGNAL = "market_signal"
    QUESTION = "question"
    ANNOUNCEMENT = "announcement"
    SPAM = "spam"
    OFF_TOPIC = "off_topic"


class RejectReason(StrEnum):
    EXACT_DUP = "exact_dup"
    NOT_IDEA = "not_idea"
    OFF_TOPIC = "off_topic"
    SPAM = "spam"
    REMOVED = "removed"


class IdeaStatus(StrEnum):
    NEW = "new"
    ANALYZED = "analyzed"
    WATCHING = "watching"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class IdeaItemRole(StrEnum):
    SEED = "seed"
    EVIDENCE = "evidence"
    COMMENT = "comment"
    UPDATE = "update"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"


class JobTopic(StrEnum):
    SCRAPE = "pipeline.scrape"
    TRIAGE = "pipeline.triage"
    CLUSTER = "pipeline.cluster"
    ANALYZE = "pipeline.analyze"
    SCORE = "pipeline.score"
    WATCH = "pipeline.watch"
    RETENTION = "maintenance.retention"
    REEMBED = "maintenance.reembed"


class LlmRole(StrEnum):
    TRIAGE = "triage"
    ANALYST = "analyst"
    EMBED = "embed"


class LlmCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    INVALID_JSON = "invalid_json"


class WatchMode(StrEnum):
    COMMENTS_ONLY = "comments_only"
    THREAD_FULL = "thread_full"


class WatchAddedBy(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class Verdict(StrEnum):
    STRONG = "strong"
    PROMISING = "promising"
    WEAK = "weak"
    REJECT = "reject"


class SourceKind(StrEnum):
    REDDIT = "reddit"
    REDDIT_PULLPUSH = "reddit_pullpush"
    HACKERNEWS = "hackernews"
    RSS = "rss"
    DISCOURSE = "discourse"
    GITHUB_ISSUES = "github_issues"


class OwnerKind(StrEnum):
    ITEM = "item"
    IDEA = "idea"


class UpdateKind(StrEnum):
    NEW_COMMENT = "new_comment"
    NEW_POST = "new_post"
    EDIT = "edit"
    ANALYSIS_REVISION = "analysis_revision"
    WATCH_PAUSED = "watch_paused"
    MERGED = "merged"


class MonetizationModel(StrEnum):
    SUBSCRIPTION = "subscription"
    ONE_OFF = "one_off"
    USAGE = "usage"
    MARKETPLACE = "marketplace"
    ADS = "ads"
    UNKNOWN = "unknown"


TARGET_KINDS = ("subreddit", "thread", "feed", "query")


# --------------------------------------------------------------------------- #
# Errori tipizzati
# --------------------------------------------------------------------------- #
class NonRetryableError(Exception):
    """Errore che non ha senso ritentare: il job va direttamente in ``dead``."""


class SourceUnavailable(Exception):
    """La sorgente esterna non è raggiungibile o ha risposto 5xx/timeout (retryable)."""


class SourceRateLimited(SourceUnavailable):
    """La sorgente ha risposto 429 (retryable, con backoff)."""


class LlmInvalidJSON(Exception):
    """Il modello non ha prodotto JSON conforme allo schema dopo i tentativi di riparazione."""


class BudgetExhausted(Exception):
    """Il budget LLM giornaliero è esaurito: il job va rinviato, non fallito."""


class UnknownSourceKind(Exception):
    """``sources.kind`` non ha un adapter registrato."""


class EmbeddingDimMismatch(Exception):
    """La dimensione reale dell'embedding non coincide con ``IDEAI_EMBED_DIM``."""


# --------------------------------------------------------------------------- #
# Helper puri
# --------------------------------------------------------------------------- #
_WHITESPACE = re.compile(r"\s+")
_DELETED_AUTHORS = ("[deleted]", "[removed]")

EMBED_PROMPT_HASH = hashlib.sha256(b"ideai.embed.v1").hexdigest()


def content_hash(body: str) -> str:
    """``sha256`` del corpo normalizzato (spazi collassati, minuscolo, strisce)."""
    normalized = _WHITESPACE.sub(" ", body).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def author_hash(name: str | None, salt: str) -> str | None:
    """``sha256(autore + salt)`` oppure ``None`` per autori cancellati/rimossi."""
    if name is None or name in _DELETED_AUTHORS:
        return None
    return hashlib.sha256((name + salt).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Dataclass condivise (§3.4)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExternalRef:
    source_kind: str  # "reddit", "reddit_pullpush", ...
    external_id: str  # fullname "t3_abc" / "t1_xyz", o id equivalente


@dataclass(frozen=True)
class SourceTarget:
    id: int
    source_id: int
    target_ref: str  # "r/ItaliaPersonalFinance", feed URL, thread fullname
    target_kind: str  # "subreddit" | "thread" | "feed" | "query"
    cursor: Cursor | None


@dataclass(frozen=True)
class SourceCapabilities:
    has_comments: bool
    supports_incremental: bool
    supports_backfill: bool
    supports_search: bool


@dataclass(frozen=True)
class RatePolicy:
    requests_per_minute: int
    burst: int
    cost_per_call_usd: float
    min_interval_ms: int


@dataclass(frozen=True)
class NormalizedItem:
    source_kind: str  # "reddit", "reddit_pullpush", ...
    external_id: str  # id nella sorgente (es. fullname "t3_abc")
    kind: str  # "post" | "comment"
    thread_external_id: str  # post radice del thread (per un post: se stesso)
    parent_external_id: str | None
    title: str | None  # NULL per i commenti
    body: str
    author_hash: str | None  # sha256(autore + IDEAI_SALT), NULL se [deleted]
    url: str | None
    score: int | None
    num_comments: int | None
    lang: str | None
    created_at: datetime
    edited_at: datetime | None
    raw: dict  # payload grezzo, azzerato dopo l'analisi
    content_hash: str  # sha256(body normalizzato)


@dataclass(frozen=True)
class FetchPage:
    items: list[NormalizedItem]
    next_cursor: Cursor | None
    exhausted: bool


@dataclass(frozen=True)
class LLMResult:
    content: dict  # già validato contro lo schema del ruolo
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    cache_hit: bool


@dataclass(frozen=True)
class Job:
    id: int
    topic: str
    payload: dict
    state: str
    attempts: int
    max_attempts: int
    locked_by: str | None
    run_after: datetime
