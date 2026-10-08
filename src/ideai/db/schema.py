"""Metadata SQLAlchemy delle 15 tabelle (Appendice B).

La copia autorevole del DDL è ``migrations/versions/0001_initial_schema.py``: qui
le tabelle sono rispecchiate per il type checking, la introspezione di Alembic e i
test che leggono la forma dello schema. Nessun DDL viene creato da questo modulo.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.types import UserDefinedType

metadata = MetaData()


class Vector(UserDefinedType):
    """Tipo ``vector(n)`` di pgvector senza dipendere dal pacchetto ``pgvector``.

    I vettori viaggiano come stringhe ``"[0.1,0.2,…]"``; le query ANN usano
    ``CAST(:vec AS vector)``.
    """

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **kw) -> str:  # noqa: ARG002
        return f"vector({self.dim})"

    def bind_processor(self, dialect):  # noqa: ARG002
        def process(value):
            if isinstance(value, str):
                return value
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process


TIMESTAMPTZ = DateTime(timezone=True)
jsonb = JSONB
text_array = ARRAY(Text)

_identity = Identity(always=True)

sources = Table(
    "sources",
    metadata,
    Column("id", BigInteger, _identity, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("config", jsonb, nullable=False, server_default=text("'{}'::jsonb")),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
    UniqueConstraint("kind", "name"),
    CheckConstraint(
        "kind IN ('reddit','reddit_pullpush','hackernews','rss','discourse','github_issues')"
    ),
)

source_targets = Table(
    "source_targets",
    metadata,
    Column("id", BigInteger, _identity, primary_key=True),
    Column("source_id", BigInteger, nullable=False),
    Column("target_ref", Text, nullable=False),
    Column("target_kind", Text, nullable=False),
    Column("cursor", jsonb, nullable=True),
    Column("last_polled_at", TIMESTAMPTZ, nullable=True),
    Column("next_poll_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
    Column("poll_interval_s", Integer, nullable=False, server_default=text("300")),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("error_count", Integer, nullable=False, server_default=text("0")),
    Column("last_error", Text, nullable=True),
    UniqueConstraint("source_id", "target_ref"),
)

idea_clusters = Table(
    "idea_clusters",
    metadata,
    Column("id", BigInteger, _identity, primary_key=True),
    Column("title", Text, nullable=False),
    Column("canonical_summary", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'new'")),
    Column("opportunity_score", Integer, nullable=True),
    Column("score_version", Text, nullable=False, server_default=text("'v1'")),
    Column("first_seen_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
    Column("last_activity_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
    Column("item_count", Integer, nullable=False, server_default=text("0")),
    Column("source_kinds", text_array, nullable=False, server_default=text("'{}'")),
)

items = Table(
    "items",
    metadata,
    Column("id", BigInteger, _identity, primary_key=True),
    Column("source_id", BigInteger, nullable=False),
    Column("external_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("thread_external_id", Text, nullable=True),
    Column("parent_external_id", Text, nullable=True),
    Column("title", Text, nullable=True),
    Column("body", Text, nullable=False),
    Column("author_hash", Text, nullable=True),
    Column("url", Text, nullable=True),
    Column("score", Integer, nullable=True),
    Column("num_comments", Integer, nullable=True),
    Column("lang", Text, nullable=True),
    Column("created_at", TIMESTAMPTZ, nullable=False),
    Column("edited_at", TIMESTAMPTZ, nullable=True),
    Column("fetched_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
    Column("content_hash", Text, nullable=False),
    Column("raw", jsonb, nullable=True),
    Column("state", Text, nullable=False, server_default=text("'new'")),
    Column("category", Text, nullable=True),
    Column("triage_confidence", SmallInteger, nullable=True),
    Column("tags", text_array, nullable=False, server_default=text("'{}'")),
    Column("idea_id", BigInteger, nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("reject_reason", Text, nullable=True),
    UniqueConstraint("source_id", "external_id"),
)

idea_items = Table(
    "idea_items",
    metadata,
    Column("idea_id", BigInteger, nullable=False),
    Column("item_id", BigInteger, nullable=False),
    Column("role", Text, nullable=False),
    Column("similarity", Numeric(24, 12), nullable=True),
    Column("added_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("idea_id", "item_id"),
)

analyses = Table(
    "analyses",
    metadata,
    Column("id", BigInteger, _identity, primary_key=True),
    Column("idea_id", BigInteger, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("lang", Text, nullable=False, server_default=text("'it'")),
    Column("model_spec", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("payload", jsonb, nullable=False),
    Column("feasibility_score", SmallInteger, nullable=True),
    Column("economics_score", SmallInteger, nullable=True),
    Column("competition_score", SmallInteger, nullable=True),
    Column("verdict", Text, nullable=True),
    Column("confidence", Numeric(24, 12), nullable=True),
    Column("opportunity_score", Integer, nullable=True),
    Column("tokens_in", Integer, nullable=False, server_default=text("0")),
    Column("tokens_out", Integer, nullable=False, server_default=text("0")),
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default=text("0")),
    Column("latency_ms", Integer, nullable=False, server_default=text("0")),
    Column("created_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
    Column("superseded_by", BigInteger, nullable=True),
    UniqueConstraint("idea_id", "revision"),
)

embeddings = Table(
    "embeddings",
    metadata,
    Column("id", BigInteger, _identity, primary_key=True),
    Column("owner_kind", Text, nullable=False),
    Column("owner_id", BigInteger, nullable=False),
    Column("model", Text, nullable=False),
    Column("dim", Integer, nullable=False),
    Column("vec", Vector(1024), nullable=False),
    UniqueConstraint("owner_kind", "owner_id", "model"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", BigInteger, _identity, primary_key=True),
    Column("topic", Text, nullable=False),
    Column("dedup_key", Text, nullable=True),
    Column("payload", jsonb, nullable=False, server_default=text("'{}'::jsonb")),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    Column("priority", Integer, nullable=False, server_default=text("100")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("5")),
    Column("run_after", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
    Column("locked_by", Text, nullable=True),
    Column("locked_at", TIMESTAMPTZ, nullable=True),
    Column("heartbeat_at", TIMESTAMPTZ, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
    Column("finished_at", TIMESTAMPTZ, nullable=True),
)

rate_limits = Table(
    "rate_limits",
    metadata,
    Column("bucket", Text, primary_key=True),
    Column("window_start", TIMESTAMPTZ, nullable=False),
    Column("used", Integer, nullable=False, server_default=text("0")),
)

source_calls_daily = Table(
    "source_calls_daily",
    metadata,
    Column("day", Date, nullable=False),
    Column("source_id", BigInteger, nullable=False),
    Column("calls", Integer, nullable=False, server_default=text("0")),
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("day", "source_id"),
)

llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", BigInteger, _identity, primary_key=True),
    Column("role", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_hash", Text, nullable=False),
    Column("schema_version", Text, nullable=True),
    Column("tokens_in", Integer, nullable=False, server_default=text("0")),
    Column("tokens_out", Integer, nullable=False, server_default=text("0")),
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default=text("0")),
    Column("latency_ms", Integer, nullable=False, server_default=text("0")),
    Column("status", Text, nullable=False),
    Column("error", Text, nullable=True),
    Column("created_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
)

llm_cache = Table(
    "llm_cache",
    metadata,
    Column("cache_key", Text, primary_key=True),
    Column("model_spec", Text, nullable=False),
    Column("response", jsonb, nullable=False),
    Column("created_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
)

watchlist = Table(
    "watchlist",
    metadata,
    Column("idea_id", BigInteger, primary_key=True),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("interval_s", Integer, nullable=False, server_default=text("3600")),
    Column("mode", Text, nullable=False, server_default=text("'thread_full'")),
    Column("last_checked_at", TIMESTAMPTZ, nullable=True),
    Column("last_change_at", TIMESTAMPTZ, nullable=True),
    Column("added_by", Text, nullable=False, server_default=text("'auto'")),
)

idea_updates = Table(
    "idea_updates",
    metadata,
    Column("id", BigInteger, _identity, primary_key=True),
    Column("idea_id", BigInteger, nullable=False),
    Column("item_id", BigInteger, nullable=True),
    Column("kind", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("created_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
)

settings_overrides = Table(
    "settings_overrides",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", jsonb, nullable=False),
    Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=text("now()")),
)

# Indici: elencati per completezza di metadata; il DDL li crea la migrazione.
Index("source_targets_due_idx", source_targets.c.next_poll_at, postgresql_where=text("enabled"))
Index(
    "idea_clusters_score_idx",
    idea_clusters.c.opportunity_score.desc().nullslast(),
)
Index("items_state_idx", items.c.state)
Index("items_thread_idx", items.c.thread_external_id)
Index("items_idea_idx", items.c.idea_id)
Index("items_tags_idx", items.c.tags, postgresql_using="gin")
Index("items_content_hash_idx", items.c.content_hash)
Index(
    "embeddings_idea_vec_idx",
    embeddings.c.vec,
    postgresql_using="hnsw",
    postgresql_ops={"vec": "vector_cosine_ops"},
    postgresql_with={"m": 16, "ef_construction": 64},
    postgresql_where=text("owner_kind = 'idea'"),
)
Index(
    "jobs_dedup_idx",
    jobs.c.dedup_key,
    unique=True,
    postgresql_where=text("state IN ('pending','running')"),
)
Index(
    "jobs_claim_idx",
    jobs.c.topic,
    jobs.c.priority,
    jobs.c.run_after,
    postgresql_where=text("state = 'pending'"),
)
Index("llm_calls_day_idx", llm_calls.c.created_at, llm_calls.c.role)
Index("idea_updates_idea_idx", idea_updates.c.idea_id, idea_updates.c.created_at.desc())
