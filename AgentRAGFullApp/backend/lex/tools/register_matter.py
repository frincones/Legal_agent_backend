"""Tool · register_matter · crea o switch un matter workspace para el firm actual.

Sprint M21.S2 (Sprint 2 Backend). Wrapper sobre matters_workspace table de Sprint 1.

Casos de uso:
  - El usuario menciona en el prompt "este es un caso nuevo de fueros sindicales
    contra Transportes SAS" -> Brain invoca register_matter con slug/title/area.
  - El usuario invoca explicitamente /v1/matters POST -> endpoint crea via este tool.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Normaliza titulo a slug url-safe."""
    s = (text or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s[:60] or "matter-sin-slug"


class RegisterMatterTool(ToolDef):
    name = "register_matter"
    description = (
        "★ USAR cuando el usuario menciona un caso nuevo en el prompt (e.g., 'caso "
        "de fueros sindicales contra Transportes SAS', 'demanda laboral por reintegro') "
        "o cuando hace 'switch' a un matter existente. Crea workspace en matters_workspace "
        "con slug + title + area + side + jurisdiction. Si el slug ya existe, hace switch "
        "(retorna el existing matter_id). Despues, todas las generaciones del Brain "
        "appenden eventos a matter_history vinculados a este matter_id."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Titulo descriptivo del caso (ej: 'Demanda laboral Moreno vs Transportes SAS')"},
            "slug": {"type": "string", "description": "(Opcional) slug url-safe. Se auto-genera desde title si no se provee."},
            "area": {
                "type": "string",
                "description": "Area de practica: 'notarial', 'judicial_civil', 'judicial_laboral', 'judicial_admin', 'contractual', 'corporate', 'penal', 'constitucional', 'petitorio'",
            },
            "side": {
                "type": "string",
                "description": "Posicion del cliente: 'demandante', 'demandado', 'comprador', 'vendedor', 'arrendador', 'arrendatario', 'neutral'",
            },
            "jurisdiction": {"type": "string", "default": "CO"},
            "phase": {
                "type": "string",
                "description": "(Opcional) fase: 'pre_litigation', 'discovery', 'trial', 'appeal', 'closed'",
            },
            "theory_md": {"type": "string", "description": "(Opcional) teoria del caso en markdown"},
            "opposing_party": {"type": "string", "description": "(Opcional) nombre de la contraparte"},
            "client_name": {"type": "string", "description": "(Opcional) nombre del cliente"},
            "switch_if_exists": {
                "type": "boolean",
                "default": True,
                "description": "Si True (default) y el slug ya existe, retorna el existing en vez de error",
            },
        },
        "required": ["title", "area"],
    }
    timeout_seconds = 10.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(
        self,
        ctx: ToolContext,
        title: str,
        area: str,
        side: Optional[str] = None,
        slug: Optional[str] = None,
        jurisdiction: str = "CO",
        phase: Optional[str] = None,
        theory_md: Optional[str] = None,
        opposing_party: Optional[str] = None,
        client_name: Optional[str] = None,
        switch_if_exists: bool = True,
    ) -> dict:
        pool = self.pool or ctx.pool
        if pool is None:
            return {
                "_warning": "no_pool",
                "matter_id": None,
                "slug": slug or _slugify(title),
                "note": "Pool Supabase no disponible. Matter no persistido.",
            }
        if ctx.firm_id is None:
            raise ToolError("firm_id requerido en context para registrar matter")

        slug = slug or _slugify(title)

        # Check si ya existe
        existing_id = None
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select matter_id, title, area, side, phase, active, created_at
                  from matters_workspace
                 where firm_id = $1 and slug = $2
                 limit 1
                """,
                str(ctx.firm_id), slug,
            )
            if row:
                existing_id = str(row["matter_id"])

        if existing_id:
            if switch_if_exists:
                logger.info("register_matter: matter ya existe slug=%s id=%s (switch)", slug, existing_id)
                # Audit: append a history que se hizo switch
                try:
                    await self._append_history(
                        pool, matter_id=existing_id, firm_id=str(ctx.firm_id),
                        event_type="matter_switched",
                        actor_user_id=str(ctx.user_id) if ctx.user_id else None,
                        summary=f"Switch a matter existente: {row['title']}",
                    )
                except Exception as e:
                    logger.debug("history append on switch failed: %s", e)
                return {
                    "matter_id": existing_id,
                    "slug": slug,
                    "title": row["title"],
                    "area": row["area"],
                    "side": row["side"],
                    "phase": row["phase"],
                    "active": row["active"],
                    "switched": True,
                    "created": False,
                }
            else:
                raise ToolError(f"matter con slug {slug!r} ya existe en este firm; pase switch_if_exists=true para reusar")

        # Crear nuevo matter
        async with pool.acquire() as conn:
            new_id = await conn.fetchval(
                """
                insert into matters_workspace
                    (firm_id, slug, title, area, side, jurisdiction, phase,
                     theory_md, opposing_party, client_name, created_by_user_id)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                returning matter_id
                """,
                str(ctx.firm_id), slug, title, area, side, jurisdiction, phase,
                theory_md, opposing_party, client_name,
                str(ctx.user_id) if ctx.user_id else None,
            )
        new_id = str(new_id)

        # Append history event 'matter_created'
        try:
            await self._append_history(
                pool, matter_id=new_id, firm_id=str(ctx.firm_id),
                event_type="matter_created",
                actor_user_id=str(ctx.user_id) if ctx.user_id else None,
                summary=f"Matter creado: {title} ({area}/{side or 'sin_side'})",
                details={"slug": slug, "jurisdiction": jurisdiction, "phase": phase},
            )
        except Exception as e:
            logger.warning("register_matter: history append fallo: %s", e)

        logger.info("register_matter: nuevo matter id=%s slug=%s area=%s", new_id, slug, area)

        return {
            "matter_id": new_id,
            "slug": slug,
            "title": title,
            "area": area,
            "side": side,
            "jurisdiction": jurisdiction,
            "phase": phase,
            "switched": False,
            "created": True,
        }

    @staticmethod
    async def _append_history(
        pool, *, matter_id: str, firm_id: str, event_type: str,
        actor_user_id: Optional[str] = None, actor_agent: str = "lean_brain",
        summary: str = "", details: Optional[dict] = None,
    ) -> None:
        """Append event a matter_history (helper compartido con otras tools)."""
        import json as _json
        async with pool.acquire() as conn:
            await conn.execute(
                """
                insert into matter_history
                    (matter_id, firm_id, event_type, actor_user_id, actor_agent,
                     summary, details)
                values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb)
                """,
                matter_id, firm_id, event_type,
                actor_user_id, actor_agent, summary[:500],
                _json.dumps(details or {}, default=str, ensure_ascii=False),
            )


def build_tool(pool=None, **_: Any) -> ToolDef:
    return RegisterMatterTool(pool=pool)
