"""CLI di IdeaI: migrazioni, worker, scheduler, dottore, reembed, versione (§10.3).

Gli import pesanti (worker, scheduler, dottore) sono dentro i comandi, così
``ideai version`` e ``ideai db upgrade`` restano immediati e usabili anche quando
l'inferenza non è configurata.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ideai import __version__
from ideai.config import Settings, require_reddit_credentials, resolve_model_spec
from ideai.domain import JobTopic
from ideai.logging import configure_logging, get_logger

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="IdeaI — catalogo di idee imprenditoriali da conversazioni pubbliche.",
)

db_app = typer.Typer(no_args_is_help=True, help="Migrazioni del database.")
app.add_typer(db_app, name="db")

log = get_logger("ideai.cli")


def _repo_root() -> Path:
    """Radice del repository (dove stanno ``alembic.ini`` e ``migrations/``)."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "alembic.ini").exists():
        return candidate
    return Path.cwd()


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Applica tutte le migrazioni Alembic (``alembic upgrade head``)."""
    from alembic import command
    from alembic.config import Config

    settings = Settings()
    configure_logging(settings)
    root = _repo_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "head")
    typer.echo("migrazioni applicate")


@app.command()
def version() -> None:
    """Stampa la versione di IdeaI."""
    typer.echo(__version__)


@app.command()
def worker(
    topics: str = typer.Option(..., "--topics", help="Topic separati da virgola."),
    concurrency: int | None = typer.Option(
        None, "--concurrency", help="Task asyncio per processo."
    ),
) -> None:
    """Avvia un worker sui topic indicati."""
    import asyncio

    from ideai.adapters.sources.registry import get_adapter
    from ideai.db.session import create_engine_and_session
    from ideai.runtime.worker import run_worker

    settings = Settings()
    configure_logging(settings)

    requested = [t.strip() for t in topics.split(",") if t.strip()]
    valid = {t.value for t in JobTopic}
    unknown = [t for t in requested if t not in valid]
    if unknown:
        typer.echo(
            f"topic sconosciuti: {', '.join(unknown)}; validi: {', '.join(sorted(valid))}",
            err=True,
        )
        raise typer.Exit(2)

    # Le credenziali Reddit servono solo ai processi che parlano con la sorgente.
    if {JobTopic.SCRAPE.value, JobTopic.WATCH.value} & set(requested):
        try:
            require_reddit_credentials(settings)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc

    asyncio.run(
        _run_worker_checks_and_loop(
            settings, requested, concurrency, get_adapter, create_engine_and_session, run_worker
        )
    )


async def _run_worker_checks_and_loop(
    settings: Settings,
    topics: list[str],
    concurrency: int | None,
    get_adapter,
    create_engine_and_session,
    run_worker,
) -> None:
    """Verifica sorgenti e modelli prima di partire, poi esegue il worker."""
    from sqlalchemy import text

    engine, session_factory = create_engine_and_session(settings)
    try:
        async with session_factory() as session:
            kinds = (
                (
                    await session.execute(
                        text("SELECT DISTINCT kind FROM sources WHERE enabled ORDER BY kind")
                    )
                )
                .scalars()
                .all()
            )
        for kind in kinds:
            try:
                get_adapter(kind)
            except Exception as exc:  # UnknownSourceKind e affini
                typer.echo(f"sorgente {kind!r}: {exc}", err=True)
                raise typer.Exit(2) from exc

        await _check_models(settings)
        await run_worker(
            settings, session_factory, topics, concurrency or settings.worker_concurrency
        )
    finally:
        await engine.dispose()


async def _check_models(settings: Settings) -> None:
    """Verifica che i modelli dei tre ruoli esistano sul rispettivo backend."""
    from ideai.adapters.llm import build_transports

    transports = build_transports(settings)
    for role, spec in (
        ("triage", settings.triage_model),
        ("analyst", settings.analyst_model),
        ("embed", settings.embed_model),
    ):
        try:
            backend, model = resolve_model_spec(spec, settings.llm_backends)
        except ValueError as exc:
            typer.echo(f"ruolo {role}: {exc}", err=True)
            raise typer.Exit(2) from exc
        transport = transports.get(backend)
        if transport is None:
            typer.echo(f"ruolo {role}: nessun trasporto per il backend {backend!r}", err=True)
            raise typer.Exit(2)
        try:
            available = await transport.available_models()
        except ValueError:
            continue  # backend senza probe (es. anthropic)
        except Exception as exc:
            typer.echo(f"ruolo {role}: backend {backend} non raggiungibile ({exc!r})", err=True)
            raise typer.Exit(2) from exc
        if model not in available:
            typer.echo(
                f"ruolo {role}: modello {model!r} assente sul backend {backend!r}; "
                f"disponibili: {', '.join(sorted(available)) or 'nessuno'}",
                err=True,
            )
            raise typer.Exit(2)
    typer.echo("modelli verificati")


@app.command()
def scheduler() -> None:
    """Avvia lo scheduler (singolo, protetto da advisory lock)."""
    import asyncio

    from ideai.adapters.queue_pg import PgQueue
    from ideai.db.session import create_engine_and_session
    from ideai.runtime.scheduler import run_scheduler

    settings = Settings()
    configure_logging(settings)

    async def _main() -> None:
        engine, session_factory = create_engine_and_session(settings)
        try:
            await run_scheduler(settings, session_factory, PgQueue(session_factory, settings))
        finally:
            await engine.dispose()

    asyncio.run(_main())


@app.command()
def doctor(
    skip_llm: bool = typer.Option(False, "--skip-llm", help="Salta i passi 2-5 (offline)."),
) -> None:
    """Bootstrap: verifica Postgres, inferenza, modelli e prova la pipeline."""
    from ideai.doctor import run_doctor

    settings = Settings()
    configure_logging(settings)
    raise typer.Exit(run_doctor(settings, skip_llm=skip_llm))


@app.command()
def reembed(
    all_: bool = typer.Option(False, "--all", help="Ricalcola tutti gli embedding."),
    idea: int | None = typer.Option(None, "--idea", help="Ricalcola solo questa idea."),
) -> None:
    """Accoda il ricalcolo degli embedding (job ``maintenance.reembed``)."""
    import asyncio

    from ideai.adapters.queue_pg import PgQueue
    from ideai.db.session import create_engine_and_session

    if not all_ and idea is None:
        typer.echo("specifica --all oppure --idea <id>", err=True)
        raise typer.Exit(2)

    settings = Settings()
    configure_logging(settings)
    payload = {"scope": "all"} if all_ else {"scope": "idea", "idea_id": idea}

    async def _main() -> int:
        engine, session_factory = create_engine_and_session(settings)
        try:
            return await PgQueue(session_factory, settings).enqueue(JobTopic.REEMBED, payload)
        finally:
            await engine.dispose()

    job_id = asyncio.run(_main())
    typer.echo(f"job {job_id} accodato")


if __name__ == "__main__":
    app()
