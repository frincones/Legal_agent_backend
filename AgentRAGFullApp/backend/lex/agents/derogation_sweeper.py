"""Sprint M21.S4.C · Agent: derogation_sweeper.

Cron diario 04:00 UTC. Por cada firm, toma las top-N citas mas usadas en
generation_audit ultimos 30 dias y corre check_derogation. Si alguna paso
a DEROGADA, emite legal_alerts.

Trigger: cron diario.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .base import AgentContext, AgentRunResult, BackgroundAgent

logger = logging.getLogger(__name__)


class DerogationSweeperAgent(BackgroundAgent):
    name = "derogation_sweeper"
    description = "Daily sweep: verifica si las top-N citas activas pasaron a DEROGADA."
    trigger_kind = "cron"
    default_cron = "0 4 * * *"
    timeout_seconds = 240.0

    async def run(self, ctx: AgentContext) -> AgentRunResult:
        if ctx.pool is None:
            return AgentRunResult(status="skipped", output_summary="no_pool")

        firm_id = str(ctx.firm_id)
        top_n = int(ctx.config.get("top_n_citations", 100))

        # 1) Recolectar top-N citas
        try:
            top_cites = await self._collect_top_citations(ctx.pool, firm_id, top_n)
        except Exception as e:
            return AgentRunResult(status="error", error_message=f"top_citations_query_failed: {e}")

        if not top_cites:
            return AgentRunResult(status="ok", output_summary="no_citations_to_sweep")

        # 2) Para cada cita, consultar derogation_status table si existe
        async with ctx.pool.acquire() as conn:
            try:
                rows = await conn.fetch(
                    """
                    select citation_text, status, last_checked_at
                      from citation_verifications
                     where citation_text = ANY($1::text[])
                       and status in ('derogada','derogated','revoked','annulled')
                    """,
                    list(top_cites.keys()),
                )
            except Exception as e:
                logger.warning("derogation_sweeper: citation_verifications query failed: %s", e)
                rows = []

        derogated = [(r["citation_text"], top_cites.get(r["citation_text"], 0)) for r in rows]

        # 3) Emitir legal_alert por cada derogada (idempotente)
        alerts_created = 0
        if derogated:
            async with ctx.pool.acquire() as conn:
                for cite, usage in derogated:
                    try:
                        await conn.execute(
                            """
                            insert into legal_alerts
                                (firm_id, kind, severity, title, body, metadata, created_at)
                            select $1::uuid, 'derogation', 'high',
                                   'Cita derogada en uso: ' || $2,
                                   'La cita "' || $2 || '" aparece en ' || $3 || ' documentos recientes pero fue derogada. Revisar.',
                                   jsonb_build_object('citation', $2, 'usage_count', $3, 'detected_by', 'derogation_sweeper'),
                                   now()
                             where not exists (
                                 select 1 from legal_alerts
                                  where firm_id = $1::uuid and kind='derogation'
                                    and metadata->>'citation' = $2
                                    and created_at > now() - interval '7 days'
                             )
                            """,
                            firm_id, cite, str(usage),
                        )
                        alerts_created += 1
                    except Exception as e:
                        logger.debug("derogation_sweeper insert alert failed: %s", e)

        return AgentRunResult(
            status="ok",
            items_processed=len(top_cites),
            items_succeeded=len(top_cites) - len(derogated),
            items_failed=len(derogated),
            output_summary=f"Top {len(top_cites)} citas revisadas; {len(derogated)} derogadas; {alerts_created} alerts creados",
            metadata={"derogated_citations": [c for c, _ in derogated]},
        )

    @staticmethod
    async def _collect_top_citations(pool, firm_id: str, top_n: int) -> dict:
        """Aggregate citas desde generation_audit metadata.citations (jsonb array)."""
        async with pool.acquire() as conn:
            try:
                rows = await conn.fetch(
                    """
                    select coalesce(metadata->>'citation_text', value->>'text') as cite,
                           count(*) as n
                      from generation_audit ga
                      left join lateral jsonb_array_elements(ga.metadata->'citations') as value on true
                     where ga.firm_id = $1
                       and ga.started_at > now() - interval '30 days'
                       and coalesce(metadata->>'citation_text', value->>'text') is not null
                     group by 1
                     order by n desc
                     limit $2
                    """,
                    firm_id, top_n,
                )
                return {r["cite"]: int(r["n"]) for r in rows if r["cite"]}
            except Exception as e:
                logger.debug("derogation_sweeper: top_citations query failed (%s); empty set", e)
                return {}


def build_agent(**_: Any) -> BackgroundAgent:
    return DerogationSweeperAgent()
