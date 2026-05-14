"""Sprint 17 · Case similarity (vector search sobre case_lessons).

Encuentra matters históricos similares al caso actual usando el embedding
de su lesson o, si no tiene, de su descripción (titulo + materia + cuantia).

Devuelve filas listas para usar como evidencia en `predict_outcome`:
  matter_id, lesson_id, similarity, titulo, outcome, summary, tags
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def find_similar_matters(
    conn,
    firm_id: str,
    matter_id: str,
    matter_desc: str,
    limit: int = 8,
) -> list[dict]:
    """Devuelve hasta `limit` matters similares al input.

    Estrategia:
      1. Si el matter tiene una lesson con embedding, usa ese embedding.
      2. Si no, genera embedding de `matter_desc` ahora.
      3. Llama RPC lexai_similar_matters (excluye el matter actual).
    """
    from utils.embeddings import embed_text, vec_to_pg

    own_lesson = await conn.fetchrow(
        """
        select embedding from case_lessons
         where firm_id = $1::uuid and matter_id = $2::uuid
           and embedding is not null
         order by updated_at desc limit 1
        """,
        firm_id, matter_id,
    )

    if own_lesson and own_lesson["embedding"] is not None:
        # asyncpg devuelve el vector como string '[v1,v2,...]' → re-encode no-op
        # pero el RPC espera vector(1536) que asyncpg passes via ::vector cast
        embedding_pg = str(own_lesson["embedding"])
    else:
        v = await embed_text(matter_desc, purpose="case_similarity")
        if not v:
            return []
        embedding_pg = vec_to_pg(v)

    try:
        rows = await conn.fetch(
            """
            select * from lexai_similar_matters($1::uuid, $2::vector, $3::uuid, $4)
            """,
            firm_id, embedding_pg, matter_id, limit,
        )
    except Exception as e:
        logger.warning("similar_matters RPC failed: %s", e)
        return []

    return [
        {
            "matter_id": str(r["matter_id"]),
            "lesson_id": str(r["lesson_id"]),
            "similarity": float(r["similarity"] or 0),
            "titulo": r["titulo"],
            "outcome": r["outcome"],
            "summary": r["summary"],
            "tags": list(r["tags"] or []),
        }
        for r in rows
    ]


def hash_inputs(parts: list) -> str:
    """Hash determinístico de los inputs · usado para detectar staleness."""
    h = hashlib.sha256()
    for p in parts:
        if p is None:
            h.update(b"\x00")
        else:
            h.update(str(p).encode("utf-8", errors="ignore"))
            h.update(b"|")
    return h.hexdigest()[:32]
