"""Sprint 15 · KB Indexer worker.

Embeds (en background) entradas de knowledge_entries y case_lessons que
no tienen embedding o cuyo embedding está desactualizado (updated_at >
embedding_at). Esto cubre:

  · Entradas creadas cuando OpenAI estaba caído.
  · Entradas creadas vía bulk SQL / migración / importación que no
    pasaron por el endpoint POST.
  · Entradas editadas mientras OpenAI estaba caído.

Procesa en lotes pequeños usando la API batch de OpenAI (n llamadas → 1
HTTP). Cada lote es seguro de re-ejecutar (idempotente).

Punto de entrada:
  POST /v1/kb/reindex-now (admin) — corre un batch de hasta N entries.

Cron Railway lo invoca cada X minutos (configurable).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_BATCH = 32


async def reindex_kb_entries(limit: int = DEFAULT_BATCH) -> dict:
    """Embed batch de knowledge_entries sin embedding o desactualizado."""
    from utils.db import get_storage
    from utils.embeddings import embed_texts_batch, vec_to_pg, compose_kb_text

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}

    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, firm_id, title, body
              from knowledge_entries
             where embedding is null
                or embedding_at is null
                or embedding_at < updated_at
             order by updated_at desc
             limit $1
            """,
            limit,
        )
    if not rows:
        return {"processed": 0, "succeeded": 0, "failed": 0}

    texts = [compose_kb_text(r["title"], None, r["body"]) for r in rows]
    vectors = await embed_texts_batch(texts, purpose="kb_indexer_entries")

    succeeded = 0
    failed = 0
    async with storage.pool.acquire() as conn:
        for r, v in zip(rows, vectors):
            if v is None:
                failed += 1
                continue
            try:
                await conn.execute(
                    """
                    update knowledge_entries
                       set embedding = $1::vector,
                           embedding_at = now()
                     where firm_id = $2::uuid and id = $3::uuid
                    """,
                    vec_to_pg(v), r["firm_id"], r["id"],
                )
                succeeded += 1
            except Exception as e:
                logger.warning("kb_indexer · update embedding falló para %s: %s", r["id"], e)
                failed += 1

    return {"processed": len(rows), "succeeded": succeeded, "failed": failed}


async def reindex_case_lessons(limit: int = DEFAULT_BATCH) -> dict:
    """Embed batch de case_lessons sin embedding."""
    from utils.db import get_storage
    from utils.embeddings import embed_texts_batch, vec_to_pg, compose_lesson_text

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}

    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, firm_id, outcome, summary, strategy_used,
                   what_worked, what_failed, tags
              from case_lessons
             where embedding is null
                or embedding_at is null
                or embedding_at < updated_at
             order by updated_at desc
             limit $1
            """,
            limit,
        )
    if not rows:
        return {"processed": 0, "succeeded": 0, "failed": 0}

    texts = []
    for r in rows:
        title_hint = f"Lección · {r['outcome']}"
        lesson_text = " · ".join(
            s for s in [
                r.get("summary") if isinstance(r, dict) else r["summary"],
                r.get("strategy_used") if isinstance(r, dict) else r["strategy_used"],
                r.get("what_worked") if isinstance(r, dict) else r["what_worked"],
                r.get("what_failed") if isinstance(r, dict) else r["what_failed"],
            ] if s
        )
        tags = list(r["tags"] or [])
        texts.append(compose_lesson_text(title_hint, lesson_text, tags))

    vectors = await embed_texts_batch(texts, purpose="kb_indexer_lessons")

    succeeded = 0
    failed = 0
    async with storage.pool.acquire() as conn:
        for r, v in zip(rows, vectors):
            if v is None:
                failed += 1
                continue
            try:
                await conn.execute(
                    """
                    update case_lessons
                       set embedding = $1::vector,
                           embedding_at = now()
                     where firm_id = $2::uuid and id = $3::uuid
                    """,
                    vec_to_pg(v), r["firm_id"], r["id"],
                )
                succeeded += 1
            except Exception as e:
                logger.warning("kb_indexer · update lesson embedding falló para %s: %s", r["id"], e)
                failed += 1

    return {"processed": len(rows), "succeeded": succeeded, "failed": failed}


async def run_full_pass(entries_limit: int = DEFAULT_BATCH, lessons_limit: int = DEFAULT_BATCH) -> dict:
    """Corre un pase sobre ambas tablas."""
    e_result = await reindex_kb_entries(entries_limit)
    l_result = await reindex_case_lessons(lessons_limit)
    return {"entries": e_result, "lessons": l_result}
