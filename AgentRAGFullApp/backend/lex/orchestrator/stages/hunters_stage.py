"""Stage 5: Hunters — ejecuta queries RAG paralelas del template."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run_template_hunters(template, client, pool) -> list[dict[str, Any]]:
    """Ejecuta los hunters del template y devuelve hits aplanados.

    Returns:
        Lista de dicts con metadata para feed al block_generator:
        [{id, mp, corte, fecha, ratio, chunk_id, similarity}, ...]
    """
    if not template or not template.hunters:
        return []

    if pool is None or client is None:
        return []

    try:
        from lex.hunters import run_hunters
        queries = [
            {
                "query": h.query,
                "hunter": h.hunter,
                "top_k": h.top_k,
                "min_similarity": h.min_similarity,
            }
            for h in template.hunters
        ]
        results = await run_hunters(queries, client, pool)

        # Aplanar todos los hits
        all_hits: list[dict[str, Any]] = []
        seen_ids = set()
        for query, hits in results.items():
            for h in hits:
                d = h.to_dict()
                key = (d.get("id"), d.get("chunk_id"))
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                d["query_origen"] = query
                # Normalizar para block_generator
                d["ratio"] = d.get("text", "")[:600]
                all_hits.append(d)
        return all_hits
    except Exception as e:
        logger.warning("template hunters failed: %s", e)
        return []
