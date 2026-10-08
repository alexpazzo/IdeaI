"""`ideai doctor`: bootstrap in sei passi (§10.3).

1. Postgres raggiungibile e pgvector ≥ 0.8.0 (richiesto da ``hnsw.iterative_scan``)
2. backend di inferenza raggiungibili
3. modelli mancanti scaricati (Ollama) o verificati, con l'elenco delle alternative
4. dimensione reale dell'embedding == ``IDEAI_EMBED_DIM``
5. prova end-to-end su una sorgente effimera, **in una transazione annullata**
6. esito complessivo

Divergenza 7 del piano: la prova del passo 5 usa una sorgente ``name='doctor'`` e
finisce con ``ROLLBACK``; le scritture (item, idea, analisi, job, righe ``llm_calls``)
sono legate alla stessa transazione e quindi non restano nel catalogo.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import typer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ideai import __version__
from ideai.config import Settings, resolve_model_spec
from ideai.domain import Job, JobTopic

MIN_PGVECTOR = (0, 8, 0)

DOCTOR_SOURCE_NAME = "doctor"
DOCTOR_ITEM_BODY = (
    "Ogni mese perdo due giorni a riconciliare le fatture dei fornitori con il "
    "gestionale: pago per uno strumento che legga le fatture PDF e le registri da solo."
)


def _ok(step: int, message: str) -> None:
    typer.echo(f"[{step}/6] ok    {message}")


def _warn(step: int, message: str) -> None:
    typer.echo(f"[{step}/6] avviso {message}")


def _fail(step: int, message: str) -> None:
    typer.echo(f"[{step}/6] errore {message}", err=True)


def _version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in raw.split("."):
        digits = "".join(c for c in piece if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def run_doctor(settings: Settings, *, skip_llm: bool = False) -> int:
    """Esegue i sei passi e ritorna l'exit code (0 = tutto a posto)."""
    typer.echo(f"IdeaI {__version__} — verifica dei prerequisiti")
    return asyncio.run(_run(settings, skip_llm=skip_llm))


async def _run(settings: Settings, *, skip_llm: bool) -> int:
    engine = create_async_engine(settings.database_url, poolclass=None, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        # --- passo 1: Postgres e pgvector -------------------------------------
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
                version = await session.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            if not version:
                _fail(
                    1,
                    "estensione pgvector assente: serve ≥ 0.8.0 per hnsw.iterative_scan",
                )
                return 1
            if _version_tuple(version) < MIN_PGVECTOR:
                _fail(1, f"pgvector {version} troppo vecchia: serve ≥ 0.8.0")
                return 1
            _ok(1, f"Postgres raggiungibile, pgvector {version}")
        except Exception as exc:
            _fail(1, f"Postgres non raggiungibile: {exc!r}")
            return 1

        if skip_llm:
            _warn(2, "--skip-llm: passi 2-5 saltati")
            typer.echo("esito: prerequisiti di stato verificati")
            return 0

        from ideai.adapters.llm import build_transports

        transports = build_transports(settings)

        # --- passo 2: backend raggiungibili -----------------------------------
        role_specs = {
            "triage": settings.triage_model,
            "analyst": settings.analyst_model,
            "embed": settings.embed_model,
        }
        backends: dict[str, str] = {}
        for role, spec in role_specs.items():
            try:
                backend, _ = resolve_model_spec(spec, settings.llm_backends)
            except ValueError as exc:
                _fail(2, f"ruolo {role}: {exc}")
                return 1
            backends[role] = backend

        available: dict[str, list[str] | None] = {}
        step2_failed = False
        for backend in sorted(set(backends.values())):
            transport = transports[backend]
            try:
                available[backend] = await transport.available_models()
            except ValueError:
                available[backend] = None  # backend senza probe (anthropic)
                _warn(2, f"backend {backend}: probe non disponibile")
            except Exception as exc:
                _fail(2, f"backend {backend} non raggiungibile: {exc!r}")
                step2_failed = True
        if step2_failed:
            return 1
        if available:
            _ok(2, "backend di inferenza raggiungibili: " + ", ".join(sorted(available)))

        # --- passo 3: modelli -------------------------------------------------
        for role, spec in role_specs.items():
            backend, model = resolve_model_spec(spec, settings.llm_backends)
            transport = transports[backend]
            models = available.get(backend)
            if models is not None and model in models:
                _ok(3, f"ruolo {role}: {spec} presente")
                continue
            if backend_config_type(settings, backend) == "ollama_native":
                typer.echo(f"[3/6] …     scarico {spec} (può richiedere alcuni minuti)")

                def _progress(status: str, *, _spec: str = spec) -> None:
                    typer.echo(f"        {_spec}: {status}")

                try:
                    await transport.pull(model, _progress)
                except Exception as exc:
                    _fail(3, f"ruolo {role}: pull di {spec} fallito: {exc!r}")
                    return 1
                models = await transport.available_models()
                available[backend] = models
                if model not in models:
                    _fail(3, f"ruolo {role}: {spec} ancora assente dopo il pull")
                    return 1
                _ok(3, f"ruolo {role}: {spec} scaricato")
            else:
                alternatives = ", ".join(sorted(models or [])) or "nessuno"
                _fail(
                    3,
                    f"ruolo {role}: modello {model!r} assente sul backend {backend!r}; "
                    f"disponibili: {alternatives}",
                )
                return 1

        # --- passo 4: dimensione dell'embedding -------------------------------
        embed_backend, embed_model = resolve_model_spec(settings.embed_model, settings.llm_backends)
        try:
            raw_embedding = await transports[embed_backend].embed(
                model=embed_model, texts=["prova"]
            )
            vectors = raw_embedding.vectors
        except Exception as exc:
            _fail(4, f"embedding non calcolabile: {exc!r}")
            return 1
        if not vectors or len(vectors[0]) != settings.embed_dim:
            dim = len(vectors[0]) if vectors else 0
            _fail(4, f"dimensione embedding {dim} != IDEAI_EMBED_DIM {settings.embed_dim}")
            return 1
        _ok(4, f"embedding {embed_model} produce {settings.embed_dim} dimensioni")

        # --- passo 5: prova end-to-end in transazione annullata ---------------
        try:
            report = await _prova_end_to_end(settings, engine)
        except Exception as exc:
            _fail(5, f"prova end-to-end fallita: {exc!r}")
            return 1
        for line in report:
            typer.echo(f"        {line}")
        _ok(5, "prova end-to-end completata (transazione annullata: nessun residuo)")

        typer.echo("esito: tutti i prerequisiti soddisfatti")
        return 0
    finally:
        await engine.dispose()


def backend_config_type(settings: Settings, backend: str) -> str:
    return settings.llm_backends[backend].type


@asynccontextmanager
async def _transaction_bound(engine):
    """Session factory legata a una transazione esterna, annullata all'uscita.

    In SQLAlchemy 2.0 una ``Session`` legata a una ``Connection`` con transazione attiva
    non committa la transazione esterna: il ``commit()`` degli handler si limita a
    flushare, quindi il ``rollback`` finale annulla tutto.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(bind=connection, expire_on_commit=False)
        try:
            yield factory
        finally:
            await transaction.rollback()


# Dentro la transazione della prova `now()` di PostgreSQL è `transaction_timestamp()`,
# fissato all'inizio della transazione: i job appena accodati hanno `run_after` dal
# wall-clock di Python (successivo), quindi il gate temporale di §6.2 li escluderebbe.
# La query seguente è identica a quella di §6.2 senza quel gate: qui i job sono tutti
# appena creati dalla stessa transazione.
_DOCTOR_CLAIM_SQL = text(
    """
UPDATE jobs
   SET state = 'running',
       locked_by = :worker_id,
       locked_at = now(),
       heartbeat_at = now(),
       attempts = attempts + 1
 WHERE id = (
       SELECT id FROM jobs
        WHERE state = 'pending'
          AND topic = ANY(CAST(:topics AS text[]))
        ORDER BY priority, run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1)
 RETURNING id, topic, payload, state, attempts, max_attempts, locked_by, run_after
"""
)


async def _claim_any(session, topics: list[str], worker_id: str):
    """Reclama il job pending più urgente fra ``topics`` (senza gate su ``run_after``)."""
    row = (
        (await session.execute(_DOCTOR_CLAIM_SQL, {"worker_id": worker_id, "topics": topics}))
        .mappings()
        .first()
    )
    if row is None:
        return None
    return Job(
        id=row["id"],
        topic=row["topic"],
        payload=row["payload"],
        state=row["state"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        locked_by=row["locked_by"],
        run_after=row["run_after"],
    )


async def _prova_end_to_end(settings: Settings, engine) -> list[str]:
    """Ingest fittizio → triage → cluster → analyze, tutto dentro una transazione."""
    from ideai.adapters.llm import build_transports
    from ideai.adapters.llm.gateway import LlmGateway
    from ideai.adapters.queue_pg import PgQueue
    from ideai.adapters.sources.registry import build_adapters
    from ideai.pipeline.context import HandlerContext
    from ideai.pipeline.registry import handler_for
    from ideai.runtime.ratelimit import RateLimiter

    report: list[str] = []
    async with _transaction_bound(engine) as factory:
        queue = PgQueue(factory, settings)
        limiter = RateLimiter(factory)
        gateway = LlmGateway(settings, factory, build_transports(settings), limiter)
        context = HandlerContext(
            settings=settings,
            db=factory,
            queue=queue,
            llm=gateway,
            rate=limiter,
            adapters=build_adapters(settings),
            now=lambda: datetime.now(UTC),
        )

        # Sorgente effimera (disabilitata) + target + item fittizio in italiano.
        async with factory() as session:
            source_id = await session.scalar(
                text(
                    "INSERT INTO sources (kind, name, enabled) "
                    "VALUES ('reddit_pullpush', :name, false) RETURNING id"
                ),
                {"name": DOCTOR_SOURCE_NAME},
            )
            target_id = await session.scalar(
                text(
                    "INSERT INTO source_targets (source_id, target_ref, target_kind) "
                    "VALUES (:source_id, 'doctor', 'subreddit') RETURNING id"
                ),
                {"source_id": source_id},
            )
            # `kind = 'comment'` così il cluster accoda direttamente l'analisi: il watch
            # iniziale richiederebbe rete verso la sorgente e non è una diagnostica utile.
            item_id = await session.scalar(
                text(
                    """
INSERT INTO items (source_id, external_id, kind, thread_external_id, parent_external_id,
                   body, content_hash, state, created_at, raw)
VALUES (:source_id, 't1_doctor', 'comment', 't3_doctor', 't3_doctor', :body,
        :content_hash, 'new', now(), CAST('{"doctor": true}' AS jsonb))
RETURNING id
"""
                ),
                {
                    "source_id": source_id,
                    "body": DOCTOR_ITEM_BODY,
                    "content_hash": "doctor-fixture-hash",
                },
            )
            await session.commit()

        report.append(f"sorgente effimera id={source_id}, target id={target_id}, item id={item_id}")
        await queue.enqueue(JobTopic.TRIAGE, {"item_ids": [item_id]})

        processed: list[str] = []
        topics = [
            JobTopic.TRIAGE.value,
            JobTopic.CLUSTER.value,
            JobTopic.ANALYZE.value,
            JobTopic.SCORE.value,
        ]
        for _ in range(24):
            async with factory() as session:
                job = await _claim_any(session, topics, "doctor")
                await session.commit()
            if job is None:
                break
            await handler_for(job.topic)(job, context)
            processed.append(job.topic)
        report.append("job eseguiti: " + (", ".join(processed) or "nessuno"))

        async with factory() as session:
            item = (
                await session.execute(
                    text(
                        "SELECT state, category, idea_id, reject_reason FROM items WHERE id = :id"
                    ),
                    {"id": item_id},
                )
            ).first()
            report.append(
                "item: state={state} category={category} idea_id={idea_id} "
                "reject_reason={reject_reason}".format(**dict(item._mapping))
            )
            if item._mapping["idea_id"] is not None:
                analysis = (
                    await session.execute(
                        text(
                            "SELECT revision, verdict, confidence, feasibility_score, "
                            "economics_score, competition_score, opportunity_score, payload "
                            "FROM analyses WHERE idea_id = :id ORDER BY revision DESC LIMIT 1"
                        ),
                        {"id": item._mapping["idea_id"]},
                    )
                ).first()
                if analysis is None:
                    report.append("idea creata ma nessuna analisi prodotta")
                else:
                    mapping = dict(analysis._mapping)
                    payload = mapping.pop("payload") or {}
                    report.append(
                        "analisi rev={revision} verdict={verdict} confidence={confidence} "
                        "score={opportunity_score} (f/e/c={feasibility_score}/"
                        "{economics_score}/{competition_score})".format(**mapping)
                    )
                    report.append("problema: " + str(payload.get("problem", ""))[:200])
            else:
                report.append("l'item non ha generato né agganciato un'idea (esito del triage)")
        return report
