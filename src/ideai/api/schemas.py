"""Modelli Pydantic di richiesta e risposta dell'API (§9.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ideai.domain import TARGET_KINDS, WatchMode


class WatchInfo(BaseModel):
    enabled: bool
    mode: str
    interval_s: int


class IdeaListItem(BaseModel):
    id: int
    title: str
    canonical_summary: str
    status: str
    opportunity_score: int | None = None
    score_version: str
    verdict: str | None = None
    confidence: float | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_kinds: list[str] = Field(default_factory=list)
    item_count: int
    first_seen_at: datetime
    last_activity_at: datetime
    watch: WatchInfo


class IdeaListResponse(BaseModel):
    total: int
    items: list[IdeaListItem]


class AnalysisRevision(BaseModel):
    revision: int
    superseded_by: int | None = None
    verdict: str | None = None
    confidence: float | None = None
    feasibility_score: int | None = None
    economics_score: int | None = None
    competition_score: int | None = None
    opportunity_score: int | None = None
    model_spec: str
    provider: str
    prompt_version: str
    schema_version: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    created_at: datetime
    payload: dict


class IdeaDetailItem(BaseModel):
    id: int
    external_id: str
    kind: str
    title: str | None = None
    body: str
    url: str | None = None
    score: int | None = None
    num_comments: int | None = None
    role: str
    similarity: float | None = None
    state: str


class IdeaUpdate(BaseModel):
    id: int
    item_id: int | None = None
    kind: str
    summary: str
    created_at: datetime


class IdeaDetailResponse(BaseModel):
    idea: IdeaListItem
    current_analysis: AnalysisRevision | None = None
    revisions: list[AnalysisRevision]
    items: list[IdeaDetailItem]
    timeline: list[IdeaUpdate]


class WatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    mode: WatchMode = WatchMode.THREAD_FULL
    interval_s: int = Field(default=3600, ge=60)


class ReanalyzeResponse(BaseModel):
    job_id: int


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    into_id: int


class SourceTargetInfo(BaseModel):
    id: int
    target_ref: str
    target_kind: str
    enabled: bool
    cursor: dict | None = None
    last_polled_at: datetime | None = None
    next_poll_at: datetime | None = None
    poll_interval_s: int
    error_count: int
    last_error: str | None = None


class RateLimitInfo(BaseModel):
    bucket: str
    window_start: datetime | None = None
    used: int | None = None
    limit: int | None = None


class SourceCallsToday(BaseModel):
    calls: int
    cost_usd: float


class SourceInfo(BaseModel):
    id: int
    kind: str
    name: str
    enabled: bool
    config: dict
    targets: list[SourceTargetInfo]
    rate_limit: RateLimitInfo
    calls_today: SourceCallsToday


class TargetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_ref: str = Field(min_length=1)
    target_kind: Literal["subreddit", "thread", "feed", "query"]
    poll_interval_s: int | None = Field(default=None, ge=30)


class TargetPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    poll_interval_s: int | None = Field(default=None, ge=30)


class JobInfo(BaseModel):
    id: int
    topic: str
    state: str
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_by: str | None = None
    last_error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    dedup_key: str | None = None


class JobListResponse(BaseModel):
    total: int
    oldest_pending: datetime | None = None
    items: list[JobInfo]


class IdeasStats(BaseModel):
    total: int
    new: int
    analyzed: int
    watching: int
    rejected: int
    archived: int


class LlmCostRow(BaseModel):
    day: datetime
    role: str
    calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float


class SourceCostRow(BaseModel):
    day: datetime
    source_id: int
    calls: int
    cost_usd: float


class QueueHealthRow(BaseModel):
    topic: str
    state: str
    jobs: int
    oldest_pending: datetime | None = None
    last_done: datetime | None = None
    dead: int


class FailedLlmCall(BaseModel):
    id: int
    role: str
    model: str
    status: str
    error: str | None = None
    created_at: datetime


class StatsResponse(BaseModel):
    ideas: IdeasStats
    analyses: int
    watch_active: int
    llm_cost_7d: list[LlmCostRow]
    source_cost_7d: list[SourceCostRow]
    queue: list[QueueHealthRow]
    failed_llm_calls: list[FailedLlmCall]


class HealthResponse(BaseModel):
    status: str
    checks: dict | None = None
    missing: list[str] | None = None


__all__ = [
    "TARGET_KINDS",
    "AnalysisRevision",
    "FailedLlmCall",
    "HealthResponse",
    "IdeaDetailItem",
    "IdeaDetailResponse",
    "IdeaListItem",
    "IdeaListResponse",
    "IdeaUpdate",
    "IdeasStats",
    "JobInfo",
    "JobListResponse",
    "LlmCostRow",
    "MergeRequest",
    "QueueHealthRow",
    "RateLimitInfo",
    "ReanalyzeResponse",
    "SourceCallsToday",
    "SourceCostRow",
    "SourceInfo",
    "SourceTargetInfo",
    "StatsResponse",
    "TargetCreateRequest",
    "TargetPatchRequest",
    "WatchInfo",
    "WatchRequest",
]
