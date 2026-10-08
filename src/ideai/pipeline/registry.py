"""Registry degli handler della pipeline (§6.3).

Otto topic canonici, un handler ciascuno: la mappa è l'unico punto in cui il worker
(che invoca ``handler_for(job.topic)``) e la pipeline si incontrano. Un topic non
mappato è un errore non ritentabile: il job va direttamente ``dead``.
"""

from __future__ import annotations

from ideai.domain import JobTopic, NonRetryableError
from ideai.pipeline.analyze import handle_analyze
from ideai.pipeline.cluster import handle_cluster
from ideai.pipeline.context import Handler
from ideai.pipeline.reembed import handle_reembed
from ideai.pipeline.retention import handle_retention
from ideai.pipeline.score import handle_score
from ideai.pipeline.scrape import handle_scrape
from ideai.pipeline.triage import handle_triage
from ideai.pipeline.watch import handle_watch

HANDLERS: dict[JobTopic, Handler] = {
    JobTopic.SCRAPE: handle_scrape,
    JobTopic.TRIAGE: handle_triage,
    JobTopic.CLUSTER: handle_cluster,
    JobTopic.ANALYZE: handle_analyze,
    JobTopic.SCORE: handle_score,
    JobTopic.WATCH: handle_watch,
    JobTopic.RETENTION: handle_retention,
    JobTopic.REEMBED: handle_reembed,
}


def handler_for(topic: str) -> Handler:
    """Ritorna l'handler del topic; ``NonRetryableError`` se il topic non è mappato."""
    try:
        return HANDLERS[JobTopic(topic)]
    except (KeyError, ValueError) as exc:
        available = ", ".join(sorted(item.value for item in HANDLERS))
        raise NonRetryableError(
            f"nessun handler registrato per il topic {topic!r}; topic disponibili: {available}"
        ) from exc


__all__ = ["HANDLERS", "handler_for"]
