"""Contratto di configurazione (Appendice A) e risoluzione dei modelli.

Ogni campo corrisponde alla variabile ``IDEAI_<NOME_MAIUSCOLO>``. La precedenza è
quella di §10.4: variabile d'ambiente esplicita, poi profilo hardware, poi default
nel codice — qui il profilo tocca **solo** ``analyst_model`` (§10.2).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackendConfig(BaseModel):
    """Descrizione di un backend di inferenza (§7.2)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["openai_compat", "anthropic", "ollama_native"]
    base_url: str
    api_key_env: str | None = None
    timeout_s: int = 60
    max_concurrency: int = 4
    rpm: int | None = None


class PriceConfig(BaseModel):
    """Prezzo per milione di token in USD."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    in_: Decimal = Field(alias="in")
    out: Decimal


#: Default di §7.2, con la divergenza 2 del piano: ``local`` usa ``ollama_native``
#: (decoding vincolato da ``format`` con JSON Schema) su ``127.0.0.1:11434``.
DEFAULT_BACKENDS: dict[str, BackendConfig] = {
    "local": BackendConfig(
        type="ollama_native",
        base_url="http://127.0.0.1:11434",
        api_key_env=None,
        timeout_s=120,
        max_concurrency=2,
        rpm=None,
    ),
    "deepseek": BackendConfig(
        type="openai_compat",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        timeout_s=60,
        max_concurrency=4,
        rpm=None,
    ),
    "anthropic": BackendConfig(
        type="anthropic",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        timeout_s=60,
        max_concurrency=4,
        rpm=None,
    ),
}

#: Profilo hardware -> modello dell'analista (§10.2). Gli altri ruoli non cambiano.
PROFILE_ANALYST_MODEL: dict[str, str] = {
    "cpu": "deepseek/deepseek-chat",
    "gpu-8gb": "local/qwen3:8b",
    "gpu-16gb+": "local/qwen3:8b",
    "cloud": "deepseek/deepseek-chat",
}


class Settings(BaseSettings):
    """Configurazione completa del processo, letta dall'ambiente e da ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="IDEAI_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://ideai:ideai@postgres:5432/ideai"
    api_key: str
    api_base_url: str = "http://api:8000"
    salt: str

    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str | None = None
    reddit_rpm: int = 100
    pullpush_rpm: int = 60

    scrape_page_size: int = 100
    default_poll_interval_s: int = 300
    rate_limit_window_s: int = 60
    job_lease_s: int = 120
    job_heartbeat_s: int = 30
    max_attempts: int = 5
    job_retention_days: int = 7
    scheduler_tick_s: int = 30
    worker_concurrency: int = 4

    llm_backends: dict[str, BackendConfig] = Field(default_factory=lambda: dict(DEFAULT_BACKENDS))
    llm_prices: dict[str, PriceConfig] = Field(default_factory=dict)
    llm_daily_budget_usd: Decimal = Decimal("5.00")
    llm_cache_max_age_days: int = 30

    triage_model: str = "local/qwen3:8b"
    analyst_model: str = "local/qwen3:8b"
    embed_model: str = "local/bge-m3:567m"
    embed_dim: int = 1024

    triage_batch: int = 10
    dup_sim_high: float = 0.92
    dup_sim_low: float = 0.80
    g0_window_days: int = 7

    analysis_max_evidence: int = 20
    quote_max_chars: int = 280
    retention_days: int = 30

    watch_threshold: int = 70
    watch_min_delta: int = 3
    watch_min_interval_s: int = 900
    watch_max_interval_s: int = 86400
    watch_expire_days: int = 30

    hardware_profile: Literal["cpu", "gpu-8gb", "gpu-16gb+", "cloud"] = "gpu-8gb"

    @model_validator(mode="after")
    def _apply_profile(self) -> Settings:
        """Il profilo riempie ``analyst_model`` solo se non impostato esplicitamente."""
        if "analyst_model" not in self.model_fields_set:
            self.analyst_model = PROFILE_ANALYST_MODEL[self.hardware_profile]
        return self

    @model_validator(mode="after")
    def _validate_ranges(self) -> Settings:
        if self.dup_sim_low >= self.dup_sim_high:
            raise ValueError(
                "IDEAI_DUP_SIM_LOW deve essere strettamente minore di IDEAI_DUP_SIM_HIGH"
            )
        if self.embed_dim <= 0:
            raise ValueError("IDEAI_EMBED_DIM deve essere positivo")
        if self.watch_min_interval_s > self.watch_max_interval_s:
            raise ValueError(
                "IDEAI_WATCH_MIN_INTERVAL_S non può superare IDEAI_WATCH_MAX_INTERVAL_S"
            )
        for name, backend in self.llm_backends.items():
            if not backend.base_url:
                raise ValueError(f"backend {name!r}: base_url mancante")
        return self


def resolve_model_spec(spec: str, backends: dict[str, BackendConfig]) -> tuple[str, str]:
    """Divide ``<backend>/<model>`` sul **primo** slash e valida il backend (§3.5)."""
    if "/" not in spec:
        raise ValueError(f"model spec {spec!r} non valida: atteso il formato <backend>/<model>")
    backend, model = spec.split("/", 1)
    if not backend or not model:
        raise ValueError(f"model spec {spec!r} non valida: atteso il formato <backend>/<model>")
    if backend not in backends:
        available = ", ".join(sorted(backends))
        raise ValueError(
            f"backend {backend!r} sconosciuto nella model spec {spec!r}; "
            f"backend dichiarati: {available}"
        )
    return backend, model


def require_reddit_credentials(settings: Settings) -> None:
    """Solleva ``ValueError`` elencando le credenziali Reddit mancanti (§Appendice A)."""
    missing = [
        name
        for name, value in (
            ("IDEAI_REDDIT_CLIENT_ID", settings.reddit_client_id),
            ("IDEAI_REDDIT_CLIENT_SECRET", settings.reddit_client_secret),
            ("IDEAI_REDDIT_USER_AGENT", settings.reddit_user_agent),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "credenziali Reddit mancanti: "
            + ", ".join(missing)
            + " (obbligatorie per i topic pipeline.scrape e pipeline.watch)"
        )
