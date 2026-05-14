"""Sprint 15 · Helpers de embedding para Knowledge Base + Case Lessons.

Wrapper delgado sobre `utils.llm.llm_generate_embedding(s_batch)` que:

  - Convierte vectores Python `list[float]` al literal pgvector
    (`'[v1,v2,...]'`) que asyncpg necesita para parámetros `vector(1536)`.
  - Trunca texto a un máximo seguro de caracteres antes de embeddear
    (evita errores 400 de OpenAI con docs muy largos).
  - Devuelve `None` en fallos (red, API key, etc.) en lugar de explotar,
    para que callers puedan persistir entradas SIN embedding y procesarlas
    luego con el worker `kb_indexer`.

NO modifica ningún cliente existente · es código aditivo.
"""

from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


# OpenAI text-embedding-3-small acepta ~8191 tokens. A ~4 chars/token,
# 30 000 caracteres deja margen amplio. Truncamos para evitar 400.
MAX_EMBED_CHARS = 30_000


def vec_to_pg(v: List[float]) -> str:
    """Codifica `list[float]` al literal pgvector que asyncpg espera.

    El driver no tiene encoder nativo para `vector`, así que pasamos un
    string `'[v1,v2,...]'` y casteamos a `::vector` en el SQL.
    """
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _normalize(text: str) -> str:
    t = (text or "").strip()
    if len(t) > MAX_EMBED_CHARS:
        t = t[:MAX_EMBED_CHARS]
    return t


async def embed_text(
    text: str,
    purpose: str = "knowledge_base",
    session_id: str = "",
) -> Optional[List[float]]:
    """Embed un único texto. Devuelve `None` si falla (red, sin API key, etc.)."""
    clean = _normalize(text)
    if not clean:
        return None
    try:
        from utils.llm import llm_generate_embedding
        return await llm_generate_embedding(
            clean,
            purpose=purpose,
            session_id=session_id,
        )
    except Exception as e:
        logger.warning("embed_text failed (purpose=%s): %s", purpose, e)
        return None


async def embed_text_as_pg(
    text: str,
    purpose: str = "knowledge_base",
    session_id: str = "",
) -> Optional[str]:
    """Conveniencia: embed + encode a string pgvector directamente."""
    v = await embed_text(text, purpose=purpose, session_id=session_id)
    return vec_to_pg(v) if v else None


async def embed_texts_batch(
    texts: List[str],
    purpose: str = "knowledge_base",
    session_id: str = "",
) -> List[Optional[List[float]]]:
    """Embed un lote. Mantiene posiciones · entradas vacías → None.

    Si la llamada al API falla por completo, devuelve [None]*len(texts)
    para que el caller pueda decidir reintentar.
    """
    if not texts:
        return []
    cleaned = [_normalize(t) for t in texts]
    # OpenAI no acepta strings vacíos: filtramos y reinsertamos None.
    non_empty_idx = [i for i, t in enumerate(cleaned) if t]
    payload = [cleaned[i] for i in non_empty_idx]
    out: List[Optional[List[float]]] = [None] * len(texts)
    if not payload:
        return out
    try:
        from utils.llm import llm_generate_embeddings_batch
        vectors = await llm_generate_embeddings_batch(
            payload,
            purpose=purpose,
            session_id=session_id,
        )
        for i, v in zip(non_empty_idx, vectors):
            out[i] = v
    except Exception as e:
        logger.warning("embed_texts_batch failed (purpose=%s, n=%d): %s", purpose, len(payload), e)
    return out


def compose_kb_text(title: str, summary: Optional[str], body: Optional[str]) -> str:
    """Compone el texto a embeddear para una entrada KB.

    Prioriza title + summary (más denso semánticamente) y luego body
    truncado · evita que docs muy largos diluyan el centroide.
    """
    parts: list[str] = []
    if title:
        parts.append(title.strip())
    if summary:
        parts.append(summary.strip())
    if body:
        parts.append(body.strip())
    return "\n\n".join(p for p in parts if p)


def compose_lesson_text(title: str, lesson: str, tags: Optional[List[str]] = None) -> str:
    """Compone el texto a embeddear para una case lesson."""
    parts: list[str] = []
    if title:
        parts.append(title.strip())
    if lesson:
        parts.append(lesson.strip())
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    return "\n\n".join(p for p in parts if p)
