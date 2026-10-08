"""Calcolo del costo di una chiamata LLM a partire da ``IDEAI_LLM_PRICES`` (§7.8).

I prezzi sono per **milione di token** e mappano il ``model_spec`` completo
(``<backend>/<model>``). Un modello non elencato non inventa un prezzo: vale zero
e produce un avviso a log.
"""

from __future__ import annotations

from decimal import Decimal

from ideai.config import PriceConfig
from ideai.logging import get_logger

log = get_logger("ideai.llm.pricing")

#: ``numeric(12, 6)``: la stessa precisione della colonna ``llm_calls.cost_usd``.
_QUANTUM = Decimal("0.000001")
_MILLION = Decimal(10**6)


def cost_usd(
    spec: str,
    tokens_in: int,
    tokens_out: int,
    prices: dict[str, PriceConfig],
    *,
    warn_unpriced: bool = True,
) -> Decimal:
    """Costo in USD quantizzato a 6 decimali (``0`` se il prezzo non è dichiarato).

    ``warn_unpriced`` distingue il caso legittimo (backend locale nativo, che per
    definizione costa zero) dal dato mancante su un backend a pagamento: nel primo
    caso l'assenza di prezzo non è un'anomalia e non sporca i log a ogni chiamata.
    """
    price = prices.get(spec)
    if price is None:
        if warn_unpriced:
            log.warning("prezzo non dichiarato per %s: costo 0", spec)
        else:
            log.debug("backend locale non a listino (%s): costo 0", spec)
        return Decimal(0)
    raw = (Decimal(tokens_in) / _MILLION) * price.in_ + (Decimal(tokens_out) / _MILLION) * price.out
    return raw.quantize(_QUANTUM)
