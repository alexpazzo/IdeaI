"""Schema iniziale: 15 tabelle, 12 indici e 3 viste (Appendice B, copia verbatim).

Revision ID: 0001
Revises:
Create Date: 2026-10-07

"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


STATEMENTS: list[str] = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
    CREATE TABLE sources (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      kind text NOT NULL CHECK (kind IN ('reddit','reddit_pullpush','hackernews','rss','discourse','github_issues')),
      name text NOT NULL, config jsonb NOT NULL DEFAULT '{}'::jsonb,
      enabled boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (kind, name))
    """,
    """
    CREATE TABLE source_targets (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      source_id bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      target_ref text NOT NULL, target_kind text NOT NULL CHECK (target_kind IN ('subreddit','thread','feed','query')),
      cursor jsonb, last_polled_at timestamptz, next_poll_at timestamptz NOT NULL DEFAULT now(),
      poll_interval_s integer NOT NULL DEFAULT 300, enabled boolean NOT NULL DEFAULT true,
      error_count integer NOT NULL DEFAULT 0, last_error text, UNIQUE (source_id, target_ref))
    """,
    "CREATE INDEX source_targets_due_idx ON source_targets (next_poll_at) WHERE enabled",
    """
    CREATE TABLE idea_clusters (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      title text NOT NULL, canonical_summary text NOT NULL,
      status text NOT NULL DEFAULT 'new' CHECK (status IN ('new','analyzed','watching','archived','rejected')),
      opportunity_score integer, score_version text NOT NULL DEFAULT 'v1',
      first_seen_at timestamptz NOT NULL DEFAULT now(), last_activity_at timestamptz NOT NULL DEFAULT now(),
      item_count integer NOT NULL DEFAULT 0, source_kinds text[] NOT NULL DEFAULT '{}')
    """,
    "CREATE INDEX idea_clusters_score_idx ON idea_clusters (opportunity_score DESC NULLS LAST)",
    """
    CREATE TABLE items (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      source_id bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      external_id text NOT NULL, kind text NOT NULL CHECK (kind IN ('post','comment')),
      thread_external_id text, parent_external_id text, title text, body text NOT NULL,
      author_hash text, url text, score integer, num_comments integer, lang text,
      created_at timestamptz NOT NULL, edited_at timestamptz, fetched_at timestamptz NOT NULL DEFAULT now(),
      content_hash text NOT NULL, raw jsonb,
      state text NOT NULL DEFAULT 'new' CHECK (state IN ('new','triaged','rejected','candidate','analyzed','archived')),
      category text CHECK (category IN ('product_idea','pain_point','market_signal','question','announcement','spam','off_topic')),
      triage_confidence real, tags text[] NOT NULL DEFAULT '{}',
      idea_id bigint REFERENCES idea_clusters(id) ON DELETE SET NULL, attempts integer NOT NULL DEFAULT 0,
      reject_reason text CHECK (reject_reason IN ('exact_dup','not_idea','off_topic','spam','removed')),
      UNIQUE (source_id, external_id))
    """,
    "CREATE INDEX items_state_idx ON items (state)",
    "CREATE INDEX items_thread_idx ON items (thread_external_id)",
    "CREATE INDEX items_idea_idx ON items (idea_id)",
    "CREATE INDEX items_tags_idx ON items USING gin (tags)",
    "CREATE INDEX items_content_hash_idx ON items (content_hash)",
    """
    CREATE TABLE idea_items (
      idea_id bigint NOT NULL REFERENCES idea_clusters(id) ON DELETE CASCADE,
      item_id bigint NOT NULL REFERENCES items(id) ON DELETE CASCADE,
      role text NOT NULL CHECK (role IN ('seed','evidence','comment','update')),
      similarity real, added_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (idea_id, item_id))
    """,
    """
    CREATE TABLE analyses (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      idea_id bigint NOT NULL REFERENCES idea_clusters(id) ON DELETE CASCADE,
      revision integer NOT NULL, lang text NOT NULL DEFAULT 'it',
      model_spec text NOT NULL, provider text NOT NULL, prompt_version text NOT NULL, schema_version text NOT NULL,
      payload jsonb NOT NULL,
      feasibility_score smallint CHECK (feasibility_score BETWEEN 1 AND 5),
      economics_score smallint CHECK (economics_score BETWEEN 1 AND 5),
      competition_score smallint CHECK (competition_score BETWEEN 1 AND 5),
      verdict text CHECK (verdict IN ('strong','promising','weak','reject')),
      confidence real CHECK (confidence BETWEEN 0 AND 1), opportunity_score integer,
      tokens_in integer NOT NULL DEFAULT 0, tokens_out integer NOT NULL DEFAULT 0,
      cost_usd numeric(12,6) NOT NULL DEFAULT 0, latency_ms integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      superseded_by bigint REFERENCES analyses(id) ON DELETE SET NULL,
      UNIQUE (idea_id, revision))
    """,
    """
    CREATE TABLE embeddings (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      owner_kind text NOT NULL CHECK (owner_kind IN ('item','idea')),
      owner_id bigint NOT NULL, model text NOT NULL, dim integer NOT NULL,
      vec vector(1024) NOT NULL, UNIQUE (owner_kind, owner_id, model))
    """,
    "CREATE INDEX embeddings_idea_vec_idx ON embeddings USING hnsw (vec vector_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE owner_kind = 'idea'",
    """
    CREATE TABLE jobs (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      topic text NOT NULL CHECK (topic IN ('pipeline.scrape','pipeline.triage','pipeline.cluster','pipeline.analyze','pipeline.score','pipeline.watch','maintenance.retention','maintenance.reembed')),
      dedup_key text, payload jsonb NOT NULL DEFAULT '{}'::jsonb,
      state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','done','dead')),
      priority integer NOT NULL DEFAULT 100, attempts integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 5,
      run_after timestamptz NOT NULL DEFAULT now(), locked_by text, locked_at timestamptz, heartbeat_at timestamptz,
      last_error text, created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz)
    """,
    "CREATE UNIQUE INDEX jobs_dedup_idx ON jobs (dedup_key) WHERE state IN ('pending','running')",
    "CREATE INDEX jobs_claim_idx ON jobs (topic, priority, run_after) WHERE state = 'pending'",
    """
    CREATE TABLE rate_limits (
      bucket text PRIMARY KEY, window_start timestamptz NOT NULL, used integer NOT NULL DEFAULT 0)
    """,
    """
    CREATE TABLE source_calls_daily (
      day date NOT NULL,
      source_id bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      calls integer NOT NULL DEFAULT 0, cost_usd numeric(12,6) NOT NULL DEFAULT 0,
      PRIMARY KEY (day, source_id))
    """,
    """
    CREATE TABLE llm_calls (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      role text NOT NULL CHECK (role IN ('triage','analyst','embed')),
      provider text NOT NULL, model text NOT NULL, prompt_hash text NOT NULL, schema_version text,
      tokens_in integer NOT NULL DEFAULT 0, tokens_out integer NOT NULL DEFAULT 0,
      cost_usd numeric(12,6) NOT NULL DEFAULT 0, latency_ms integer NOT NULL DEFAULT 0,
      status text NOT NULL CHECK (status IN ('ok','error','invalid_json')), error text,
      created_at timestamptz NOT NULL DEFAULT now())
    """,
    "CREATE INDEX llm_calls_day_idx ON llm_calls (created_at, role)",
    """
    CREATE TABLE llm_cache (
      cache_key text PRIMARY KEY, model_spec text NOT NULL, response jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now())
    """,
    """
    CREATE TABLE watchlist (
      idea_id bigint PRIMARY KEY REFERENCES idea_clusters(id) ON DELETE CASCADE,
      enabled boolean NOT NULL DEFAULT true, interval_s integer NOT NULL DEFAULT 3600,
      mode text NOT NULL DEFAULT 'thread_full' CHECK (mode IN ('comments_only','thread_full')),
      last_checked_at timestamptz, last_change_at timestamptz,
      added_by text NOT NULL DEFAULT 'auto' CHECK (added_by IN ('auto','manual')))
    """,
    """
    CREATE TABLE idea_updates (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      idea_id bigint NOT NULL REFERENCES idea_clusters(id) ON DELETE CASCADE,
      item_id bigint REFERENCES items(id) ON DELETE SET NULL,
      kind text NOT NULL CHECK (kind IN ('new_comment','new_post','edit','analysis_revision','watch_paused','merged')),
      summary text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())
    """,
    "CREATE INDEX idea_updates_idea_idx ON idea_updates (idea_id, created_at DESC)",
    """
    CREATE TABLE settings_overrides (
      key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())
    """,
    """
    CREATE VIEW v_idea_ranking AS
    SELECT c.id, c.title, c.canonical_summary, c.status, c.opportunity_score, c.score_version,
           c.item_count, c.source_kinds, c.first_seen_at, c.last_activity_at,
           s.category, s.tags,
           COALESCE(w.enabled, false) AS watch_enabled, w.mode AS watch_mode, w.interval_s AS watch_interval_s,
           a.revision, a.verdict, a.confidence, a.feasibility_score, a.economics_score,
           a.competition_score, a.model_spec, a.created_at AS analyzed_at
      FROM idea_clusters c
      LEFT JOIN LATERAL (SELECT * FROM analyses x WHERE x.idea_id = c.id AND x.superseded_by IS NULL
                         ORDER BY x.revision DESC LIMIT 1) a ON true
      LEFT JOIN LATERAL (SELECT i.category, i.tags FROM idea_items ii JOIN items i ON i.id = ii.item_id
                         WHERE ii.idea_id = c.id AND ii.role = 'seed'
                         ORDER BY ii.added_at LIMIT 1) s ON true
      LEFT JOIN watchlist w ON w.idea_id = c.id
    """,
    """
    CREATE VIEW v_daily_llm_cost AS
    SELECT date_trunc('day', created_at AT TIME ZONE 'UTC') AS day, role, count(*) AS calls,
           sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out,
           sum(cost_usd) AS cost_usd, sum(latency_ms) AS latency_ms
      FROM llm_calls GROUP BY 1, 2
    """,
    """
    CREATE VIEW v_queue_health AS
    SELECT topic, state, count(*) AS jobs,
           min(run_after) FILTER (WHERE state = 'pending') AS oldest_pending,
           max(finished_at) FILTER (WHERE state = 'done') AS last_done,
           count(*) FILTER (WHERE state = 'dead') AS dead
      FROM jobs GROUP BY 1, 2
    """,
]

TABLES_IN_DROP_ORDER = [
    "settings_overrides",
    "idea_updates",
    "watchlist",
    "llm_cache",
    "llm_calls",
    "source_calls_daily",
    "rate_limits",
    "jobs",
    "embeddings",
    "analyses",
    "idea_items",
    "items",
    "idea_clusters",
    "source_targets",
    "sources",
]

VIEWS_IN_DROP_ORDER = ["v_queue_health", "v_daily_llm_cost", "v_idea_ranking"]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for view in VIEWS_IN_DROP_ORDER:
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for table in TABLES_IN_DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP EXTENSION IF EXISTS vector")
