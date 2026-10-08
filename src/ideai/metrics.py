"""Metriche Prometheus esposte su ``/metrics`` (§11.3)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

jobs_total = Counter(
    "ideai_jobs_total",
    "Job transitati per stato e topic.",
    ["state", "topic"],
)

queue_oldest_pending_seconds = Gauge(
    "ideai_queue_oldest_pending_seconds",
    "Età in secondi del job pending più vecchio.",
)

llm_calls_total = Counter(
    "ideai_llm_calls_total",
    "Chiamate LLM per ruolo ed esito.",
    ["role", "status"],
)

llm_tokens_total = Counter(
    "ideai_llm_tokens_total",
    "Token consumati per ruolo e direzione.",
    ["role", "direction"],
)

llm_cost_usd_total = Counter(
    "ideai_llm_cost_usd_total",
    "Costo LLM cumulato in USD per ruolo.",
    ["role"],
)

llm_latency_seconds = Histogram(
    "ideai_llm_latency_seconds",
    "Latenza delle chiamate LLM per ruolo.",
    ["role"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)

scrape_total = Counter(
    "ideai_scrape_total",
    "Cicli di scrape per sorgente ed esito.",
    ["source_kind", "outcome"],
)

ideas_total = Gauge(
    "ideai_ideas_total",
    "Idee per stato.",
    ["status"],
)
