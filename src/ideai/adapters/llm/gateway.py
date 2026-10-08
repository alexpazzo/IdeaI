"""Gateway LLM: ruoli, cache, budget, riparazione e contabilizzazione (§7).

Il gateway è l'unico punto di contatto con i modelli. Espone tre ruoli
(``triage``, ``analyst``, ``embed``), ciascuno con modello, prompt e schema propri,
e applica nell'ordine: cache, budget, semaforo/rpm, trasporto, validazione,
riparazione, contabilizzazione (§7.3, §6.8, §6.9).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal

from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from ideai.adapters.llm import cache as cache_mod
from ideai.adapters.llm.pricing import cost_usd
from ideai.adapters.llm.transport import LlmTransport
from ideai.config import Settings, resolve_model_spec
from ideai.domain import (
    EMBED_PROMPT_HASH,
    BudgetExhausted,
    LlmCallStatus,
    LlmInvalidJSON,
    LLMResult,
    LlmRole,
    NonRetryableError,
)
from ideai.logging import get_logger
from ideai.metrics import (
    llm_calls_total,
    llm_cost_usd_total,
    llm_latency_seconds,
    llm_tokens_total,
)
from ideai.ports.llm import RateLimiterLike
from ideai.prompts import PROMPT_CURRENT
from ideai.prompts.loader import PromptFile, load_prompt
from ideai.prompts.schemas import SCHEMA_MODELS

log = get_logger("ideai.llm.gateway")

#: Tentativi di riparazione dopo il primo fallimento (§7.3).
_REPAIRS = 2

#: Ruolo di generazione -> (nome del file di prompt, versione corrente).
_GENERATION_PROMPTS: dict[LlmRole, tuple[str, str]] = {
    LlmRole.TRIAGE: ("triage", PROMPT_CURRENT["triage"]),
    LlmRole.ANALYST: ("analysis", PROMPT_CURRENT["analyst"]),
}


# --------------------------------------------------------------------------- #
# Validazione minima di JSON Schema
# --------------------------------------------------------------------------- #
def _matches_type(value: object, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _resolve_ref(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"$ref non supportato: {ref}")
    node: object = root
    for part in ref[2:].split("/"):
        node = node[part]  # type: ignore[index]
    if not isinstance(node, dict):
        raise ValueError(f"$ref non risolve un oggetto: {ref}")
    return node


def _violation(instance: object, schema: dict, root: dict, path: str) -> str | None:
    """Ritorna la prima violazione dello schema, oppure ``None`` se conforme."""
    if "$ref" in schema:
        return _violation(instance, _resolve_ref(schema["$ref"], root), root, path)

    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            if _violation(instance, sub, root, path) is None:
                return None
        return f"{path}: nessun ramo di anyOf soddisfatto"

    if "enum" in schema and instance not in schema["enum"]:
        return f"{path}: {instance!r} non è tra {schema['enum']}"

    expected = schema.get("type")
    if expected is not None and not _matches_type(instance, expected):
        return f"{path}: atteso {expected}, ricevuto {type(instance).__name__}"

    if expected == "object" or (expected is None and isinstance(instance, dict)):
        assert isinstance(instance, dict)
        for name in schema.get("required", []):
            if name not in instance:
                return f"{path}: campo obbligatorio {name!r} mancante"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(properties))
            if extra:
                return f"{path}: campi non ammessi {extra}"
        for name, sub in properties.items():
            if name in instance:
                error = _violation(instance[name], sub, root, f"{path}.{name}")
                if error is not None:
                    return error

    if expected == "array":
        assert isinstance(instance, list)
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            return f"{path}: più di {schema['maxItems']} elementi"
        if "items" in schema:
            for index, elem in enumerate(instance):
                error = _violation(elem, schema["items"], root, f"{path}[{index}]")
                if error is not None:
                    return error

    if expected in ("number", "integer") and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:  # type: ignore[operator]
            return f"{path}: {instance} < minimo {schema['minimum']}"
        if "maximum" in schema and instance > schema["maximum"]:  # type: ignore[operator]
            return f"{path}: {instance} > massimo {schema['maximum']}"

    if expected == "string" and "maxLength" in schema and len(instance) > schema["maxLength"]:  # type: ignore[arg-type]
        return f"{path}: stringa troppo lunga (max {schema['maxLength']})"

    return None


def validate(instance: object, schema: dict) -> str | None:
    """Valida ``instance`` contro ``schema`` (sottoinsieme dei costrutti di Pydantic v2)."""
    return _violation(instance, schema, schema, "$")


def validate_with_model(
    instance: object, schema: dict, model: type[BaseModel] | None
) -> str | None:
    """Valida con Pydantic v2 se il modello è noto, altrimenti contro lo schema JSON.

    L'errore restituito è quello di Pydantic, quindi la riparazione di §7.3 rispedisce al
    modello un messaggio di validazione completo (percorso del campo incluso).
    """
    if model is None:
        return validate(instance, schema)
    try:
        model.model_validate(instance)
    except ValidationError as exc:
        return str(exc)
    return None


# --------------------------------------------------------------------------- #
# Ruoli
# --------------------------------------------------------------------------- #
class RoleProvider:
    """Provider di un ruolo: il ``model`` delle chiamate deve coincidere col modello risolto."""

    def __init__(
        self,
        gateway: LlmGateway,
        role: LlmRole,
        model_spec: str,
        prompt: PromptFile | None,
    ) -> None:
        self._gateway = gateway
        self.role = role
        self.model_spec = model_spec
        self.name = model_spec
        self._prompt = prompt

    def _check_model(self, model: str) -> None:
        if model != self.model_spec:
            raise ValueError(
                f"ruolo {self.role.value}: atteso il modello {self.model_spec!r}, "
                f"ricevuto {model!r}"
            )

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        self._check_model(model)
        if self._prompt is None:
            raise NonRetryableError(f"il ruolo {self.role.value} non produce JSON")
        return await self._gateway.complete_json(
            role=self.role,
            model_spec=self.model_spec,
            prompt_hash=self._prompt.prompt_hash,
            schema_version=self._prompt.schema_version,
            system=system,
            user=user,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        self._check_model(model)
        if self.role is not LlmRole.EMBED:
            raise NonRetryableError(f"il ruolo {self.role.value} non produce embedding")
        return await self._gateway.embed(model_spec=self.model_spec, texts=texts)


# --------------------------------------------------------------------------- #
# Gateway
# --------------------------------------------------------------------------- #
class LlmGateway:
    """Unico punto di contatto con i modelli (§7.1)."""

    def __init__(
        self,
        settings: Settings,
        session_factory,
        transports: Mapping[str, LlmTransport],
        limiter: RateLimiterLike,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.transports = transports
        self.limiter = limiter
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._prompts: dict[LlmRole, PromptFile] = {}

    # -- ruoli -------------------------------------------------------------- #
    def triage(self) -> RoleProvider:
        return self._provider(LlmRole.TRIAGE)

    def analyst(self) -> RoleProvider:
        return self._provider(LlmRole.ANALYST)

    def embedder(self) -> RoleProvider:
        return self._provider(LlmRole.EMBED)

    def _provider(self, role: LlmRole) -> RoleProvider:
        if role is LlmRole.EMBED:
            return RoleProvider(self, role, self.settings.embed_model, None)
        if role not in self._prompts:
            name, version = _GENERATION_PROMPTS[role]
            self._prompts[role] = load_prompt(name, version)
        model_spec = (
            self.settings.triage_model if role is LlmRole.TRIAGE else self.settings.analyst_model
        )
        return RoleProvider(self, role, model_spec, self._prompts[role])

    # -- supporto ----------------------------------------------------------- #
    def _transport(self, backend_name: str) -> LlmTransport:
        try:
            return self.transports[backend_name]
        except KeyError as exc:
            raise NonRetryableError(f"nessun trasporto per il backend {backend_name!r}") from exc

    def _semaphore(self, backend_name: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(backend_name)
        if semaphore is None:
            backend = self.settings.llm_backends[backend_name]
            semaphore = asyncio.Semaphore(backend.max_concurrency)
            self._semaphores[backend_name] = semaphore
        return semaphore

    async def _check_budget(self, model_spec: str) -> None:
        """Read-then-act sul costo del giorno UTC; solo per i modelli con prezzo (§6.8)."""
        if model_spec not in self.settings.llm_prices:
            return
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT coalesce(sum(cost_usd), 0) FROM llm_calls "
                    "WHERE created_at >= "
                    "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
                )
            )
            spent = result.scalar_one()
        if Decimal(spent) >= self.settings.llm_daily_budget_usd:
            raise BudgetExhausted(
                f"budget LLM giornaliero esaurito: {spent} >= "
                f"{self.settings.llm_daily_budget_usd} USD"
            )

    async def _record(
        self,
        *,
        role: LlmRole,
        provider: str,
        model: str,
        prompt_hash: str,
        schema_version: str | None,
        tokens_in: int,
        tokens_out: int,
        cost: Decimal,
        latency_ms: int,
        status: LlmCallStatus,
        error: str | None,
    ) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO llm_calls "
                    "(role, provider, model, prompt_hash, schema_version, tokens_in, tokens_out, "
                    " cost_usd, latency_ms, status, error) "
                    "VALUES (:role, :provider, :model, :prompt_hash, :schema_version, :tokens_in, "
                    " :tokens_out, :cost_usd, :latency_ms, :status, :error)"
                ),
                {
                    "role": role.value,
                    "provider": provider,
                    "model": model,
                    "prompt_hash": prompt_hash,
                    "schema_version": schema_version,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd": cost,
                    "latency_ms": latency_ms,
                    "status": status.value,
                    "error": error,
                },
            )
            await session.commit()

        llm_calls_total.labels(role=role.value, status=status.value).inc()
        if tokens_in:
            llm_tokens_total.labels(role=role.value, direction="in").inc(tokens_in)
        if tokens_out:
            llm_tokens_total.labels(role=role.value, direction="out").inc(tokens_out)
        if cost:
            llm_cost_usd_total.labels(role=role.value).inc(float(cost))
        llm_latency_seconds.labels(role=role.value).observe(latency_ms / 1000.0)

    # -- chiamate ----------------------------------------------------------- #
    def _validate_candidate(
        self, candidate: object, schema: dict, role: LlmRole, prompt_hash: str
    ) -> str | None:
        """Pydantic sul prompt corrente del ruolo, altrimenti validazione da schema JSON."""
        prompt = self._prompts.get(role)
        generation = _GENERATION_PROMPTS.get(role)
        model: type[BaseModel] | None = None
        if prompt is not None and generation is not None and prompt.prompt_hash == prompt_hash:
            model = SCHEMA_MODELS.get(generation)
        return validate_with_model(candidate, schema, model)

    async def complete_json(
        self,
        *,
        role: LlmRole,
        model_spec: str,
        prompt_hash: str,
        schema_version: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        backend_name, backend_model = resolve_model_spec(model_spec, self.settings.llm_backends)
        backend = self.settings.llm_backends[backend_name]
        transport = self._transport(backend_name)
        key = cache_mod.cache_key(prompt_hash, schema_version, model_spec, temperature, user)

        async with self.session_factory() as session:
            cached = await cache_mod.get(session, key)
        if cached is not None:
            await self._record(
                role=role,
                provider=backend_name,
                model=backend_model,
                prompt_hash=prompt_hash,
                schema_version=schema_version,
                tokens_in=0,
                tokens_out=0,
                cost=Decimal(0),
                latency_ms=0,
                status=LlmCallStatus.OK,
                error=None,
            )
            log.info("cache hit", model_spec=model_spec, role=role.value)
            return LLMResult(
                content=cached,
                provider=backend_name,
                model=backend_model,
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                latency_ms=0,
                cache_hit=True,
            )

        await self._check_budget(model_spec)

        total_in = total_out = latency_ms = 0
        previous: str | None = None
        error: str | None = None
        parsed: dict | None = None

        async with self._semaphore(backend_name):
            if backend.rpm is not None:
                await self.limiter.acquire(
                    f"llm:{backend_name}", backend.rpm, self.settings.rate_limit_window_s
                )
            for _ in range(_REPAIRS + 1):
                call_user = user
                if previous is not None:
                    call_user = (
                        f"{user}\n\nRisposta precedente:\n{previous}"
                        f"\n\nErrore di validazione:\n{error}"
                    )
                raw = await transport.chat_json(
                    model=backend_model,
                    system=system,
                    user=call_user,
                    schema=schema,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                total_in += raw.tokens_in
                total_out += raw.tokens_out
                latency_ms += raw.latency_ms
                previous = raw.content
                try:
                    candidate = json.loads(raw.content)
                except json.JSONDecodeError as exc:
                    error = f"JSON non valido: {exc}"
                    continue
                violation = self._validate_candidate(candidate, schema, role, prompt_hash)
                if violation is None:
                    parsed = candidate
                    error = None
                    break
                error = violation

        cost = cost_usd(
            model_spec,
            total_in,
            total_out,
            self.settings.llm_prices,
            warn_unpriced=backend.type != "ollama_native",
        )
        if parsed is None:
            await self._record(
                role=role,
                provider=backend_name,
                model=backend_model,
                prompt_hash=prompt_hash,
                schema_version=schema_version,
                tokens_in=total_in,
                tokens_out=total_out,
                cost=cost,
                latency_ms=latency_ms,
                status=LlmCallStatus.INVALID_JSON,
                error=error,
            )
            raise LlmInvalidJSON(
                f"ruolo {role.value}: risposta non conforme dopo {_REPAIRS} riparazioni: {error}"
            )

        async with self.session_factory() as session:
            await cache_mod.put(session, key, model_spec, parsed)
            await session.commit()
        await self._record(
            role=role,
            provider=backend_name,
            model=backend_model,
            prompt_hash=prompt_hash,
            schema_version=schema_version,
            tokens_in=total_in,
            tokens_out=total_out,
            cost=cost,
            latency_ms=latency_ms,
            status=LlmCallStatus.OK,
            error=None,
        )
        return LLMResult(
            content=parsed,
            provider=backend_name,
            model=backend_model,
            tokens_in=total_in,
            tokens_out=total_out,
            cost_usd=float(cost),
            latency_ms=latency_ms,
            cache_hit=False,
        )

    async def embed(self, *, model_spec: str, texts: list[str]) -> list[list[float]]:
        backend_name, backend_model = resolve_model_spec(model_spec, self.settings.llm_backends)
        backend = self.settings.llm_backends[backend_name]
        transport = self._transport(backend_name)
        key = cache_mod.cache_key(EMBED_PROMPT_HASH, "", model_spec, 0.0, "\n".join(texts))

        async with self.session_factory() as session:
            cached = await cache_mod.get(session, key)
        if cached is not None:
            await self._record(
                role=LlmRole.EMBED,
                provider=backend_name,
                model=backend_model,
                prompt_hash=EMBED_PROMPT_HASH,
                schema_version="",
                tokens_in=0,
                tokens_out=0,
                cost=Decimal(0),
                latency_ms=0,
                status=LlmCallStatus.OK,
                error=None,
            )
            vectors = cached["vectors"] if isinstance(cached, dict) else cached
            return vectors

        await self._check_budget(model_spec)

        async with self._semaphore(backend_name):
            if backend.rpm is not None:
                await self.limiter.acquire(
                    f"llm:{backend_name}", backend.rpm, self.settings.rate_limit_window_s
                )
            raw = await transport.embed(model=backend_model, texts=texts)

        cost = cost_usd(
            model_spec,
            raw.tokens_in,
            0,
            self.settings.llm_prices,
            warn_unpriced=backend.type != "ollama_native",
        )
        async with self.session_factory() as session:
            await cache_mod.put(session, key, model_spec, {"vectors": raw.vectors})
            await session.commit()
        await self._record(
            role=LlmRole.EMBED,
            provider=backend_name,
            model=backend_model,
            prompt_hash=EMBED_PROMPT_HASH,
            schema_version="",
            tokens_in=raw.tokens_in,
            tokens_out=0,
            cost=cost,
            latency_ms=raw.latency_ms,
            status=LlmCallStatus.OK,
            error=None,
        )
        return raw.vectors


__all__ = ["LlmGateway", "RoleProvider", "validate", "validate_with_model"]
