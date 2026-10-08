"""Costruzione dei trasporti LLM a partire da ``IDEAI_LLM_BACKENDS`` (§7.2).

``build_transports`` mappa il nome logico del backend alla sua istanza di
trasporto. La costruzione è **senza I/O di rete**: i client HTTP nascono alla
prima richiesta.
"""

from __future__ import annotations

from ideai.adapters.llm.anthropic import AnthropicTransport
from ideai.adapters.llm.ollama_native import OllamaNativeTransport
from ideai.adapters.llm.openai_compat import OpenAiCompatTransport
from ideai.adapters.llm.transport import LlmTransport
from ideai.config import Settings

_TRANSPORTS = {
    "openai_compat": OpenAiCompatTransport,
    "ollama_native": OllamaNativeTransport,
    "anthropic": AnthropicTransport,
}


def build_transports(settings: Settings) -> dict[str, LlmTransport]:
    """Una istanza di trasporto per ogni backend dichiarato in ``settings.llm_backends``."""
    transports: dict[str, LlmTransport] = {}
    for name, backend in settings.llm_backends.items():
        factory = _TRANSPORTS[backend.type]
        transports[name] = factory(backend, name=name)
    return transports


__all__ = [
    "AnthropicTransport",
    "LlmTransport",
    "OllamaNativeTransport",
    "OpenAiCompatTransport",
    "build_transports",
]
