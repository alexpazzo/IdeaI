"""Cache delle risposte LLM su ``llm_cache`` (§6.9).

La chiave è ``sha256`` di ``prompt_hash + schema_version + model_spec +
temperature + testo``: la stessa richiesta su modelli o temperature diversi non è
la stessa risposta. In caso di hit il gateway registra comunque una riga
``llm_calls`` con costo zero, così il grafico dei consumi riflette il lavoro reale.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text


def cache_key(
    prompt_hash: str,
    schema_version: str,
    model_spec: str,
    temperature: float,
    text: str,
) -> str:
    """Chiave deterministica della cache (§6.9)."""
    payload = "\n".join([prompt_hash, schema_version, model_spec, repr(float(temperature)), text])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def get(conn, key: str) -> dict | None:
    """Risposta cachata oppure ``None``; ``conn`` è una sessione o connessione async."""
    result = await conn.execute(
        text("SELECT response FROM llm_cache WHERE cache_key = :key"),
        {"key": key},
    )
    value = result.scalar_one_or_none()
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


async def put(conn, key: str, model_spec: str, response) -> None:
    """Upsert: aggiorna la riga esistente, non ne duplica una (la retention ripulisce)."""
    await conn.execute(
        text(
            "INSERT INTO llm_cache (cache_key, model_spec, response) "
            "VALUES (:key, :model_spec, CAST(:response AS jsonb)) "
            "ON CONFLICT (cache_key) DO UPDATE SET response = EXCLUDED.response"
        ),
        {"key": key, "model_spec": model_spec, "response": json.dumps(response)},
    )
