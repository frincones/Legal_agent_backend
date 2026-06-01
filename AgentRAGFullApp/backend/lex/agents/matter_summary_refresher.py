"""Sprint M21.S4.D · Agent: matter_summary_refresher.

Trigger event: cuando matter_history acumula >=N nuevos eventos sin que
matters_workspace.theory_md sea regenerada, sintetiza un resumen ejecutivo
del expediente y lo escribe en theory_md.

Trigger: event (post-insert hook) o manual.
"""
from __future__ import annotations

import logging
from typing import Any

from .base import AgentContext, AgentRunResult, BackgroundAgent

logger = logging.getLogger(__name__)


SUMMARY_PROMPT = """Eres un asistente legal colombiano. Sintetiza el siguiente expediente en un resumen ejecutivo (max 400 palabras) en markdown.

Estructura del resumen:
## Hechos clave
## Pretensiones / Solicitudes
## Estado procesal
## Proximos pasos sugeridos

Solo responde el markdown del resumen. Sin preludio."""


class MatterSummaryRefresherAgent(BackgroundAgent):
    name = "matter_summary_refresher"
    description = "Regenera theory_md de matters cuando acumulan >=N eventos nuevos."
    trigger_kind = "event"
    default_event = "matter_history_threshold"
    timeout_seconds = 90.0

    async def run(self, ctx: AgentContext) -> AgentRunResult:
        if ctx.pool is None:
            return AgentRunResult(status="skipped", output_summary="no_pool")

        firm_id = str(ctx.firm_id)
        min_events = int(ctx.config.get("min_history_events", 10))

        # Encontrar matters elegibles
        async with ctx.pool.acquire() as conn:
            candidates = await conn.fetch(
                """
                select m.matter_id, m.title, m.area, m.side, m.theory_md,
                       (select count(*) from matter_history mh
                         where mh.matter_id = m.matter_id
                           and (m.updated_at is null or mh.created_at > m.updated_at)
                       ) as new_events
                  from matters_workspace m
                 where m.firm_id = $1 and m.active = true
                """,
                firm_id,
            )

        candidates = [c for c in candidates if (c["new_events"] or 0) >= min_events]

        if not candidates:
            return AgentRunResult(status="ok", output_summary=f"No matters con >={min_events} eventos nuevos")

        refreshed = 0
        for c in candidates:
            try:
                events = await self._fetch_events(ctx.pool, str(c["matter_id"]))
                summary = await self._summarize(ctx, c, events)
                if summary:
                    async with ctx.pool.acquire() as conn:
                        await conn.execute(
                            "update matters_workspace set theory_md=$1, updated_at=now() where matter_id=$2::uuid",
                            summary, str(c["matter_id"]),
                        )
                    refreshed += 1
            except Exception as e:
                logger.warning("matter_summary_refresher matter=%s failed: %s", c["matter_id"], e)

        return AgentRunResult(
            status="ok",
            items_processed=len(candidates),
            items_succeeded=refreshed,
            output_summary=f"{refreshed}/{len(candidates)} matters resumidos",
        )

    @staticmethod
    async def _fetch_events(pool, matter_id: str) -> list[dict]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select event_type, summary, details, created_at
                  from matter_history
                 where matter_id = $1::uuid
                 order by created_at asc
                 limit 200
                """,
                matter_id,
            )
        return [
            {"type": r["event_type"], "summary": r["summary"], "ts": r["created_at"].isoformat() if r["created_at"] else None}
            for r in rows
        ]

    @staticmethod
    async def _summarize(ctx: AgentContext, matter: dict, events: list[dict]) -> str:
        if ctx.anthropic_client is None or not events:
            return MatterSummaryRefresherAgent._heuristic_summary(matter, events)
        events_md = "\n".join(f"- [{e['ts'] or '?'}] {e['type']}: {e['summary'] or ''}" for e in events[-50:])
        prompt = (
            f"Caso: {matter['title']}\nArea: {matter['area']}\nLado: {matter.get('side') or 'N/A'}\n\n"
            f"## Linea de tiempo (ultimos 50 eventos)\n{events_md}"
        )
        try:
            resp = await ctx.anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                temperature=0.2,
                system=SUMMARY_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        except Exception as e:
            logger.debug("summarize LLM failed: %s", e)
            return MatterSummaryRefresherAgent._heuristic_summary(matter, events)

    @staticmethod
    def _heuristic_summary(matter: dict, events: list[dict]) -> str:
        return (
            f"## {matter['title']}\n"
            f"**Area:** {matter['area']} · **Lado:** {matter.get('side') or 'N/A'}\n\n"
            f"## Resumen automatico\nExpediente con {len(events)} eventos registrados. "
            f"Ultimo evento: {events[-1]['type'] if events else 'sin eventos'}.\n"
            f"_Resumen heuristico (LLM no disponible)._"
        )


def build_agent(**_: Any) -> BackgroundAgent:
    return MatterSummaryRefresherAgent()
