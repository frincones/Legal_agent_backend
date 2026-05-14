"""Sprint 20 · Judge profile embedding indexer.

Para cada juez sin embedding (o cuyo perfil cambió):
  1. Trae sus decisiones recientes desde jurisprudencia
  2. Compone texto: nombre + perfil + decisiones recientes (ratio_decidendi)
  3. Embed con text-embedding-3-small
  4. Persiste judges.embedding + embedding_at + decisions_total

Endpoint admin:
  POST /v1/judges-admin/reindex-now?limit=N

Cron Railway llama esto periódicamente (semanal típicamente · los jueces
no cambian a diario).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def reindex_judges(limit: int = 50) -> dict:
    """Recomputa embeddings + stats para jueces."""
    from utils.db import get_storage
    from utils.embeddings import embed_text, vec_to_pg
    from utils.judge_helpers import compose_judge_profile_text

    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}

    async with storage.pool.acquire() as conn:
        # Jueces sin embedding O con updated_at > embedding_at
        rows = await conn.fetch(
            """
            select id, full_name, name_variants, perfil, especialidades
              from judges
             where active = true
               and (embedding is null
                    or embedding_at is null
                    or embedding_at < updated_at)
             order by updated_at desc
             limit $1
            """,
            limit,
        )

    if not rows:
        return {"processed": 0, "succeeded": 0, "failed": 0}

    succeeded = 0
    failed = 0
    for r in rows:
        try:
            # Obtener decisiones recientes para enriquecer perfil
            async with storage.pool.acquire() as conn2:
                decisions = []
                try:
                    decision_rows = await conn2.fetch(
                        "select * from lexai_judge_decisions($1::uuid, 8)",
                        r["id"],
                    )
                    decisions = [dict(d) for d in decision_rows]
                except Exception as e:
                    logger.debug("decisions fetch failed for %s: %s", r["id"], e)

                text = compose_judge_profile_text(
                    full_name=r["full_name"],
                    perfil=r["perfil"],
                    especialidades=list(r["especialidades"] or []),
                    recent_decisions=decisions,
                )

            v = await embed_text(text, purpose="judge_profile")
            if v is None:
                failed += 1
                continue

            decisions_count = len(decisions)

            async with storage.pool.acquire() as conn3:
                await conn3.execute(
                    """
                    update judges
                       set embedding = $1::vector,
                           embedding_at = now(),
                           decisions_total = $2
                     where id = $3::uuid
                    """,
                    vec_to_pg(v), decisions_count, r["id"],
                )
            succeeded += 1
        except Exception as e:
            logger.warning("judge reindex failed for %s: %s", r["id"], e)
            failed += 1

    return {"processed": len(rows), "succeeded": succeeded, "failed": failed}
