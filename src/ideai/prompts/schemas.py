"""Schemi Pydantic v2 dei ruoli LLM: triage (§7.5) e analisi (§7.6).

``TriageBatch.model_json_schema()`` e ``AnalysisPayload.model_json_schema()`` sono
gli schemi passati al provider: il decoding vincolato (``format`` su Ollama) e la
validazione della risposta usano esattamente questi oggetti. ``extra="forbid"`` fa
sì che un campo inatteso sia un errore, non un dato silenziosamente ignorato.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ideai.domain import ItemCategory, MonetizationModel, Verdict

#: Versione dello schema di analisi (entra in ``llm_calls.schema_version``).
SCHEMA_VERSION = "v1"


class TriageItem(BaseModel):
    """Giudizio su un singolo item in ingresso (§7.5)."""

    model_config = ConfigDict(extra="forbid")

    index: int
    confidence: float = Field(ge=0, le=1)
    category: ItemCategory
    one_line_summary: str
    problem: str | None = None
    audience: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=5)
    dup_of_idea_id: int | None = None


class TriageBatch(BaseModel):
    """Lotto di risultati, allineato per indice all'input (§7.4)."""

    model_config = ConfigDict(extra="forbid")

    results: list[TriageItem]


class Monetization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: MonetizationModel
    price_hypothesis: str
    unit_economics_note: str


class Feasibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1, le=5)
    rationale: str
    hard_blockers: list[str]
    tech_stack_hint: list[str]


class Economics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1, le=5)
    tam_signal: str
    rationale: str


class Competition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1, le=5)
    named_players: list[str]
    rationale: str


class Risk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk: str
    severity: int = Field(ge=1, le=5)
    mitigation: str


class Effort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weeks_to_mvp: int
    team_size: int
    confidence: float = Field(ge=0, le=1)


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_external_id: str
    quote: str


class AnalysisPayload(BaseModel):
    """Valutazione strutturata di un'idea (§7.6), nell'ordine esatto del documento."""

    model_config = ConfigDict(extra="forbid")

    problem: str
    target_customer: str
    current_alternatives: list[str]
    proposed_solution: str
    mvp_scope: list[str]
    differentiators: list[str]
    monetization: Monetization
    feasibility: Feasibility
    economics: Economics
    competition: Competition
    risks: list[Risk]
    effort: Effort
    evidence_quotes: list[EvidenceQuote]
    verdict: Verdict
    confidence: float = Field(ge=0, le=1)
    notes: str


#: Modello Pydantic atteso per ``(nome_prompt, versione_prompt)``: il gateway valida con
#: Pydantic v2 (§7.3) e rispedisce al modello l'errore di validazione per la riparazione.
SCHEMA_MODELS: dict[tuple[str, str], type[BaseModel]] = {
    ("triage", "v1"): TriageBatch,
    ("analysis", "v1"): AnalysisPayload,
}


__all__ = [
    "SCHEMA_MODELS",
    "SCHEMA_VERSION",
    "AnalysisPayload",
    "Competition",
    "Economics",
    "Effort",
    "EvidenceQuote",
    "Feasibility",
    "Monetization",
    "Risk",
    "TriageBatch",
    "TriageItem",
]
