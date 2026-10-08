"""Ricerca dei vicini canonici per i cancelli G1/G2 (§8.2).

L'ANN search lavora sull'indice HNSW parziale delle sole idee; ``hnsw.iterative_scan
= 'relaxed_order'`` evita che il filtro ``owner_kind = 'idea'`` (predicato non
indicizzato dall'indice) faccia restituire meno righe del richiesto. Il vettore
viaggia come stringa ``"[0.1,0.2,…]"`` e viene castato a ``vector`` in SQL: nessuna
dipendenza da numpy o dal tipo pgvector lato Python.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ITERATIVE_SCAN_SQL = text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")

_NEAREST_SQL = text(
    """
SELECT e.owner_id AS idea_id,
       1 - (e.vec <=> CAST(:vec AS vector)) AS sim
  FROM embeddings e
 WHERE e.owner_kind = 'idea' AND e.model = :model
 ORDER BY e.vec <=> CAST(:vec AS vector)
 LIMIT :k
"""
)


def vec_literal(values: Sequence[float]) -> str:
    """Serializza un vettore nella forma testuale accettata da pgvector."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


async def nearest_ideas(
    session: AsyncSession, vec: str, model: str, k: int = 5
) -> list[tuple[int, float]]:
    """Ritorna fino a ``k`` idee vicine con la similarità coseno, dalla più simile.

    Richiede una transazione già aperta (``SET LOCAL`` è transazionale).
    """
    await session.execute(_ITERATIVE_SCAN_SQL)
    rows = await session.execute(_NEAREST_SQL, {"vec": vec, "model": model, "k": k})
    return [(int(row["idea_id"]), float(row["sim"])) for row in rows.mappings()]


__all__ = ["nearest_ideas", "vec_literal"]
