"""Prompt versionati e relativi schemi JSON (§7.7)."""

from __future__ import annotations

#: Versione corrente dei prompt per ruolo. È una scelta di release, non di deployment:
#: si cambia qui quando nasce una nuova versione (es. ``analysis_v2.md``).
PROMPT_CURRENT: dict[str, str] = {"triage": "v1", "analyst": "v1"}

__all__ = ["PROMPT_CURRENT"]
