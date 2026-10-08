"""Scoring deterministico (§8.4) — nessun modello, solo aritmetica riproducibile.

``opportunity_score`` è in 0–100. I pesi vivono in ``Weights`` (default del codice,
sovrascrivibili da ``settings_overrides``) e ``score_version`` li identifica.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class Weights:
    quality: float = 0.40
    engagement: float = 0.25
    evidence: float = 0.20
    economy: float = 0.10
    confidence: float = 0.05


@dataclass(frozen=True)
class ScoreInputs:
    feasibility: int
    economics: int
    competition: int
    seed_score: int | None
    seed_num_comments: int | None
    seed_is_comment: bool
    engagement_saturation: int
    valid_evidence_quotes: int
    monetization_model: str
    confidence: float


def quality(feasibility: int, economics: int, competition: int) -> float:
    """Media pesata dei tre giudizi 1–5, normalizzata a 0–1 (competizione invertita)."""
    return 0.35 * (feasibility - 1) / 4 + 0.35 * (economics - 1) / 4 + 0.30 * (5 - competition) / 4


def engagement(inp: ScoreInputs) -> float:
    """Engagement del seed in 0–1, saturato dalla costante della sorgente."""
    if inp.seed_score is None:
        return 0.0
    denominator = math.log1p(max(inp.engagement_saturation, 1))
    if inp.seed_is_comment:
        raw = math.log1p(max(inp.seed_score, 0))
    else:
        raw = math.log1p(max(inp.seed_score, 0) + 2 * max(inp.seed_num_comments or 0, 0))
    return min(1.0, raw / denominator)


def evidence(inp: ScoreInputs) -> float:
    """Citazioni valide (già filtrate da §7.6) saturate a 5."""
    return min(1.0, inp.valid_evidence_quotes / 5)


def economy(inp: ScoreInputs) -> float:
    """1.0 se il modello di ricavo è identificato, 0.3 altrimenti."""
    return 0.3 if inp.monetization_model == "unknown" else 1.0


def _round_half_up(value: float) -> int:
    return int(Decimal(repr(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def opportunity_score(inp: ScoreInputs, weights: Weights | None = None) -> int:
    """Punteggio 0–100 di §8.4, arrotondato half-up."""
    w = weights or Weights()
    total = (
        w.quality * quality(inp.feasibility, inp.economics, inp.competition)
        + w.engagement * engagement(inp)
        + w.evidence * evidence(inp)
        + w.economy * economy(inp)
        + w.confidence * inp.confidence
    )
    return _round_half_up(100 * total)
