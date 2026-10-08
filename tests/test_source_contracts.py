"""Contratti degli adapter sorgente (§3.1, §5.3, §5.6).

Le fixture in ``tests/fixtures/<kind>/`` sono dict puri **nella forma che l'adapter
stesso produce/consuma**: i test non richiedono credenziali Reddit né rete. Ogni test
confronta l'intero ``NormalizedItem`` atteso, campo per campo, contro ``normalize``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ideai.adapters.sources.pullpush import RedditPullpushAdapter
from ideai.adapters.sources.reddit import RedditAdapter
from ideai.adapters.sources.registry import ADAPTERS, build_adapters, get_adapter
from ideai.config import Settings
from ideai.domain import (
    NormalizedItem,
    RatePolicy,
    SourceCapabilities,
    UnknownSourceKind,
    author_hash,
    content_hash,
)

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = Settings(api_key="test-key", salt="test-salt")
REDDIT_URL = "https://www.reddit.com/r/ItaliaPersonalFinance/comments/1abc2de/come_gestite_il_fondo_emergenza/"


def _load(kind: str, name: str) -> dict:
    return json.loads((FIXTURES / kind / name).read_text(encoding="utf-8"))


def _epoch(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


# --------------------------------------------------------------------------- #
# reddit (Async PRAW)
# --------------------------------------------------------------------------- #
def test_reddit_capabilities_and_rate_policy() -> None:
    adapter = RedditAdapter(SETTINGS)
    assert adapter.kind == "reddit"
    assert adapter.capabilities() == SourceCapabilities(
        has_comments=True,
        supports_incremental=True,
        supports_backfill=False,
        supports_search=True,
    )
    assert adapter.rate_policy() == RatePolicy(
        requests_per_minute=100, burst=100, cost_per_call_usd=0.0, min_interval_ms=600
    )


def test_reddit_submission_normalize_all_fields() -> None:
    payload = _load("reddit", "submission.json")
    item = RedditAdapter(SETTINGS).normalize(payload)
    expected = NormalizedItem(
        source_kind="reddit",
        external_id="t3_1abc2de",
        kind="post",
        thread_external_id="t3_1abc2de",
        parent_external_id=None,
        title="Come gestite il fondo di emergenza con l'inflazione al 5%?",
        body=(
            "Ho circa 10.000 euro fermi sul conto corrente e l'inflazione mi sta erodendo "
            "il potere d'acquisto. Cerco uno strumento semplice e svincolabile per gestirlo "
            "senza rischiare il capitale."
        ),
        author_hash=author_hash("utente_alpha", "test-salt"),
        url=REDDIT_URL,
        score=143,
        num_comments=57,
        lang=None,
        created_at=_epoch(1759300000.0),
        edited_at=None,
        raw=payload,
        content_hash=content_hash(payload["selftext"]),
    )
    assert item == expected
    assert item.thread_external_id == item.external_id
    assert item.lang is None


def test_reddit_comment_normalize_all_fields() -> None:
    payload = _load("reddit", "comment.json")
    item = RedditAdapter(SETTINGS).normalize(payload)
    expected = NormalizedItem(
        source_kind="reddit",
        external_id="t1_9zz8yy7",
        kind="comment",
        thread_external_id="t3_1abc2de",
        parent_external_id="t3_1abc2de",
        title=None,
        body=(
            "Io uso un conto deposito svincolato: rendimento modesto ma il capitale torna "
            "disponibile in 24 ore e non ho vincoli."
        ),
        author_hash=author_hash("utente_beta", "test-salt"),
        url=f"{REDDIT_URL}9zz8yy7/",
        score=28,
        num_comments=None,
        lang=None,
        created_at=_epoch(1759303600.0),
        edited_at=None,
        raw=payload,
        content_hash=content_hash(payload["body"]),
    )
    assert item == expected
    assert item.parent_external_id == "t3_1abc2de"


def test_reddit_deleted_author_hashes_to_none() -> None:
    payload = _load("reddit", "comment_deleted_author.json")
    item = RedditAdapter(SETTINGS).normalize(payload)
    assert item.author_hash is None
    assert item.kind == "comment"
    assert item.external_id == "t1_8yy7xx6"
    assert item.parent_external_id == "t1_9zz8yy7"
    assert item.body == "[deleted]"
    assert item.content_hash == content_hash("[deleted]")
    assert item.raw == payload


def test_reddit_removed_post_normalizes_normally() -> None:
    payload = _load("reddit", "post_removed.json")
    item = RedditAdapter(SETTINGS).normalize(payload)
    assert item.kind == "post"
    assert item.body == "[removed]"
    assert item.content_hash == content_hash("[removed]")
    assert item.edited_at == _epoch(1759310500.0)
    assert item.num_comments == 3
    assert item.thread_external_id == item.external_id == "t3_1removed"


# --------------------------------------------------------------------------- #
# reddit_pullpush (backfill)
# --------------------------------------------------------------------------- #
def test_pullpush_capabilities_and_rate_policy() -> None:
    adapter = RedditPullpushAdapter(SETTINGS)
    assert adapter.kind == "reddit_pullpush"
    assert adapter.capabilities() == SourceCapabilities(
        has_comments=True,
        supports_incremental=True,
        supports_backfill=True,
        supports_search=False,
    )
    assert adapter.rate_policy() == RatePolicy(
        requests_per_minute=60, burst=60, cost_per_call_usd=0.0, min_interval_ms=1000
    )


def test_pullpush_submission_normalize_all_fields() -> None:
    payload = _load("reddit_pullpush", "submission.json")
    item = RedditPullpushAdapter(SETTINGS).normalize(payload)
    expected = NormalizedItem(
        source_kind="reddit_pullpush",
        external_id="t3_1abc2de",
        kind="post",
        thread_external_id="t3_1abc2de",
        parent_external_id=None,
        title="Come gestite il fondo di emergenza con l'inflazione al 5%?",
        body=(
            "Ho circa 10.000 euro fermi sul conto corrente e l'inflazione mi sta erodendo "
            "il potere d'acquisto. Cerco uno strumento semplice e svincolabile per gestirlo "
            "senza rischiare il capitale."
        ),
        author_hash=author_hash("utente_alpha", "test-salt"),
        url=REDDIT_URL,
        score=143,
        num_comments=57,
        lang=None,
        created_at=_epoch(1759300000),
        edited_at=None,
        raw=payload,
        content_hash=content_hash(payload["selftext"]),
    )
    assert item == expected
    assert item.thread_external_id == item.external_id


def test_pullpush_comment_normalize_all_fields() -> None:
    payload = _load("reddit_pullpush", "comment.json")
    item = RedditPullpushAdapter(SETTINGS).normalize(payload)
    expected = NormalizedItem(
        source_kind="reddit_pullpush",
        external_id="t1_9zz8yy7",
        kind="comment",
        thread_external_id="t3_1abc2de",
        parent_external_id="t3_1abc2de",
        title=None,
        body=(
            "Io uso un conto deposito svincolato: rendimento modesto ma il capitale torna "
            "disponibile in 24 ore e non ho vincoli."
        ),
        author_hash=author_hash("utente_beta", "test-salt"),
        url=f"{REDDIT_URL}9zz8yy7/",
        score=28,
        num_comments=None,
        lang=None,
        created_at=_epoch(1759303600),
        edited_at=None,
        raw=payload,
        content_hash=content_hash(payload["body"]),
    )
    assert item == expected
    assert item.parent_external_id == "t3_1abc2de"


def test_pullpush_deleted_author_and_link_fallback() -> None:
    payload = _load("reddit_pullpush", "comment_deleted_author.json")
    item = RedditPullpushAdapter(SETTINGS).normalize(payload)
    assert item.author_hash is None
    assert item.kind == "comment"
    assert item.parent_external_id == "t1_9zz8yy7"
    # ``link_id`` assente: il thread è ricavato dal campo ``link`` (permalink).
    assert item.thread_external_id == "t3_1abc2de"
    assert item.body == "[deleted]"
    assert item.content_hash == content_hash("[deleted]")


def test_pullpush_removed_post_normalize_all_fields() -> None:
    payload = _load("reddit_pullpush", "post_removed.json")
    item = RedditPullpushAdapter(SETTINGS).normalize(payload)
    assert item.kind == "post"
    assert item.external_id == "t3_1removed"
    assert item.body == "[removed]"
    assert item.content_hash == content_hash("[removed]")
    assert item.thread_external_id == "t3_1removed"
    assert item.edited_at == _epoch(1759310500)
    assert item.author_hash == author_hash("promoter_x", "test-salt")
    assert item.raw == payload


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_registry_exposes_two_adapters() -> None:
    assert sorted(ADAPTERS) == ["reddit", "reddit_pullpush"]
    assert ADAPTERS["reddit"] is RedditAdapter
    assert ADAPTERS["reddit_pullpush"] is RedditPullpushAdapter


def test_build_adapters_returns_instances() -> None:
    adapters = build_adapters(SETTINGS)
    assert set(adapters) == {"reddit", "reddit_pullpush"}
    assert isinstance(adapters["reddit"], RedditAdapter)
    assert isinstance(adapters["reddit_pullpush"], RedditPullpushAdapter)


@pytest.mark.parametrize("kind", ["hackernews", "rss", "discourse", "github_issues"])
def test_get_adapter_unknown_kind_raises(kind: str) -> None:
    with pytest.raises(UnknownSourceKind) as excinfo:
        get_adapter(kind)
    message = str(excinfo.value)
    assert kind in message
    assert "reddit" in message


def test_get_adapter_known_kind_returns_class() -> None:
    assert get_adapter("reddit") is RedditAdapter
