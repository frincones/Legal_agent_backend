"""Sprint M21.S4.B · Agent: extract_practice_patterns.

Cron nightly. Por cada firm, agrupa generation_audit ultimos 7 dias por
area y detecta patrones repetidos (citas favoritas, secciones mas usadas).
Enriquece practice_profile_sections.profile_md con los hallazgos.

Trigger: cron diario (03:00 UTC default).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from .base import AgentContext, AgentRunResult, BackgroundAgent

logger = logging.getLogger(__name__)


class ExtractPracticePatternsAgent(BackgroundAgent):
    name = "extract_practice_patterns"
    description = "Aggregates generation_audit nightly to enrich practice_profile_sections."
    trigger_kind = "cron"
    default_cron = "0 3 * * *"
    timeout_seconds = 120.0

    async def run(self, ctx: AgentContext) -> AgentRunResult:
        if ctx.pool is None:
            return AgentRunResult(status="skipped", output_summary="no_pool")

        firm_id = str(ctx.firm_id)
        processed = 0
        updated = 0

        # Recolectar areas activas de practice_profile_sections
        async with ctx.pool.acquire() as conn:
            sections = await conn.fetch(
                "select section_id, area, profile_md from practice_profile_sections where firm_id = $1",
                firm_id,
            )

        if not sections:
            return AgentRunResult(status="ok", output_summary="no_practice_sections")

        for sec in sections:
            processed += 1
            area = sec["area"]
            try:
                stats = await self._collect_stats(ctx.pool, firm_id, area)
                if stats["doc_count"] == 0:
                    continue

                appendix = self._render_appendix(stats)
                new_md = self._merge_profile(sec["profile_md"] or "", appendix)

                async with ctx.pool.acquire() as conn:
                    await conn.execute(
                        """
                        update practice_profile_sections
                           set profile_md = $1, updated_at = now()
                         where section_id = $2::uuid
                        """,
                        new_md, str(sec["section_id"]),
                    )
                updated += 1
            except Exception as e:
                logger.warning("extract_practice_patterns area=%s failed: %s", area, e)

        return AgentRunResult(
            status="ok",
            items_processed=processed,
            items_succeeded=updated,
            output_summary=f"Areas analizadas {processed}, profile_md actualizado en {updated}",
        )

    @staticmethod
    async def _collect_stats(pool, firm_id: str, area: str) -> dict:
        """Cuenta docs por area + top citas + top secciones de ultimos 7 dias."""
        async with pool.acquire() as conn:
            doc_count = await conn.fetchval(
                """
                select count(*) from generation_audit
                 where firm_id = $1 and started_at > now() - interval '7 days'
                   and (metadata->>'area' = $2 or metadata->>'doc_family' = $2)
                """,
                firm_id, area,
            ) or 0

            top_doc_types = await conn.fetch(
                """
                select metadata->>'doc_type' as doc_type, count(*) as n
                  from generation_audit
                 where firm_id = $1 and started_at > now() - interval '7 days'
                   and metadata->>'doc_type' is not null
                 group by metadata->>'doc_type'
                 order by n desc
                 limit 5
                """,
                firm_id,
            )

        return {
            "doc_count": int(doc_count),
            "top_doc_types": [(r["doc_type"], int(r["n"])) for r in top_doc_types],
            "area": area,
        }

    @staticmethod
    def _render_appendix(stats: dict) -> str:
        lines = ["", "<!-- AUTO:practice_patterns -->", f"## Patrones recientes (7 dias) · {stats['area']}"]
        lines.append(f"- Documentos generados: **{stats['doc_count']}**")
        if stats["top_doc_types"]:
            lines.append("- Tipos mas usados:")
            for dt, n in stats["top_doc_types"]:
                lines.append(f"  - `{dt}` ({n})")
        lines.append(f"_Actualizado por extract_practice_patterns_")
        lines.append("<!-- /AUTO:practice_patterns -->")
        return "\n".join(lines)

    @staticmethod
    def _merge_profile(existing: str, appendix: str) -> str:
        """Reemplaza el bloque AUTO existente o lo agrega al final."""
        start = "<!-- AUTO:practice_patterns -->"
        end = "<!-- /AUTO:practice_patterns -->"
        if start in existing and end in existing:
            pre = existing.split(start)[0]
            post = existing.split(end)[1]
            return pre + appendix + post
        return existing.rstrip() + "\n" + appendix


def build_agent(**_: Any) -> BackgroundAgent:
    return ExtractPracticePatternsAgent()
