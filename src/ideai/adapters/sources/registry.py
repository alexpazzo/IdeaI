"""Registry dichiarativo delle sorgenti (§5.1): ``sources.kind`` → classe adapter.

Aggiungere una sorgente significa aggiungere una classe in ``adapters/sources/`` e una
riga in ``ADAPTERS``. ``sources.kind`` ammette sei valori (Appendice B) ma qui ne sono
registrati due: i quattro kind dichiarati per la Fase 3 (``hackernews``, ``rss``,
``discourse``, ``github_issues``) non hanno un adapter e ``get_adapter`` lo segnala.
"""

from __future__ import annotations

from ideai.adapters.sources.pullpush import RedditPullpushAdapter
from ideai.adapters.sources.reddit import RedditAdapter
from ideai.config import Settings
from ideai.domain import UnknownSourceKind
from ideai.ports.sources import SourceAdapter

ADAPTERS: dict[str, type] = {
    "reddit": RedditAdapter,
    "reddit_pullpush": RedditPullpushAdapter,
}


def build_adapters(settings: Settings) -> dict[str, SourceAdapter]:
    """Istanzia un adapter per ciascun kind registrato, con i settings del processo."""
    return {kind: adapter_cls(settings) for kind, adapter_cls in ADAPTERS.items()}


def get_adapter(kind: str) -> type:
    """Ritorna la classe adapter per ``kind``; ``UnknownSourceKind`` se non registrato."""
    try:
        return ADAPTERS[kind]
    except KeyError:
        available = ", ".join(sorted(ADAPTERS))
        raise UnknownSourceKind(
            f"sorgente {kind!r} senza adapter registrato; kind disponibili: {available}. "
            "Per abilitarne uno nuovo serve un adapter in src/ideai/adapters/sources/"
            " e una riga in ADAPTERS (nessuna modifica a pipeline, schema e dashboard)."
        ) from None
