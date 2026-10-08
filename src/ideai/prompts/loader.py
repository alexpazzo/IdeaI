"""Loader dei prompt versionati (§7.7).

Un prompt è un file markdown con front matter YAML delimitato da due righe ``---``
(``version``, ``schema_version``, ``model_hint``, ``temperature``, ``max_tokens``).
Il corpo è diviso in due sezioni dal marcatore ``<!-- user_template -->``: la parte
``system`` precede il marcatore, la parte ``user_template`` lo segue.

``prompt_hash`` è lo ``sha256`` dei **byte grezzi** del file: qualunque modifica,
anche al solo front matter, cambia l'hash e quindi le chiavi di cache. Per questo
cambiare un prompt significa creare una nuova versione, mai modificarne una esistente.

Il ``user_template`` usa segnaposto ``str.format`` — ``{batch}`` per il triage;
``{idea}``, ``{evidence}``, ``{delta}`` per l'analisi — e non deve contenere altre
graffe: usare :meth:`PromptFile.render_user` per l'interpolazione.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).resolve().parent

#: Marcatore che separa la sezione ``system`` dal ``user_template``.
USER_TEMPLATE_MARKER = "<!-- user_template -->"


@dataclass(frozen=True)
class PromptFile:
    """Prompt caricato: metadati, sezioni risolte e hash dei byte grezzi."""

    path: Path
    version: str
    schema_version: str
    model_hint: str
    temperature: float
    max_tokens: int
    system: str
    user_template: str
    prompt_hash: str

    def render_user(self, **values: object) -> str:
        """Interpola il ``user_template`` con i segnaposto del ruolo."""
        return self.user_template.format(**values)


def load_prompt(name: str, version: str) -> PromptFile:
    """Carica ``src/ideai/prompts/{name}_{version}.md`` e ne estrae le sezioni."""
    path = PROMPTS_DIR / f"{name}_{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt non trovato: {path}")

    raw = path.read_bytes()
    prompt_hash = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path}: front matter YAML assente (prima riga attesa: '---')")
    try:
        closing = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError(f"{path}: front matter YAML non chiuso") from exc

    meta = yaml.safe_load("\n".join(lines[1:closing])) or {}
    body = "\n".join(lines[closing + 1 :])
    if USER_TEMPLATE_MARKER not in body:
        raise ValueError(f"{path}: marcatore {USER_TEMPLATE_MARKER!r} assente nel corpo")
    system, _, user_template = body.partition(USER_TEMPLATE_MARKER)

    return PromptFile(
        path=path,
        version=str(meta["version"]),
        schema_version=str(meta["schema_version"]),
        model_hint=str(meta["model_hint"]),
        temperature=float(meta["temperature"]),
        max_tokens=int(meta["max_tokens"]),
        system=system.strip(),
        user_template=user_template.strip(),
        prompt_hash=prompt_hash,
    )


__all__ = ["PROMPTS_DIR", "USER_TEMPLATE_MARKER", "PromptFile", "load_prompt"]
