"""Tool · load_company_profile · lee firms_profile compartido del firm.

Sprint M21.S2. Wrapper sobre firms_profile table de Sprint 1.

Patron Anthropic: company-profile shared entre todas las areas (vs practice_profile
que es por area). Usar al inicio de cualquier generacion para tener contexto basico
de la firma (razon social, NIT, industria, jurisdicciones, etc).
"""
from __future__ import annotations

import logging
from typing import Any

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


class LoadCompanyProfileTool(ToolDef):
    name = "load_company_profile"
    description = (
        "★ LLAMAR junto con load_skill_md y load_playbook al inicio. Carga el "
        "company profile compartido del firm: razon social, NIT, industria, "
        "tamano, jurisdicciones donde opera, practice_setting (solo/firm/in_house/"
        "government/clinic), pain points. Es el SHARED context entre todas las areas. "
        "Si el firm NO completo su cold-start (no hay row en firms_profile), retorna "
        "defaults sensatos basados en jurisdiction default CO."
    )
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    cacheable = True
    cache_ttl_seconds = 600  # 10 min
    timeout_seconds = 5.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(self, ctx: ToolContext) -> dict:
        pool = self.pool or ctx.pool
        if pool is None or ctx.firm_id is None:
            return self._default_profile()

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    select firm_id, company_name, legal_name, nit, industry,
                           practice_setting, jurisdiction, size_employees,
                           pain_points_md, metadata, created_at, updated_at
                      from firms_profile
                     where firm_id = $1
                     limit 1
                    """,
                    str(ctx.firm_id),
                )
        except Exception as e:
            logger.warning("load_company_profile lookup failed: %s", e)
            return self._default_profile(note="error de consulta, usando defaults")

        if row is None:
            return self._default_profile(
                note="Firma no completo cold-start interview. Sugerir al usuario ejecutar /v2/onboarding/cold-start"
            )

        return {
            "firm_id": str(row["firm_id"]),
            "company_name": row["company_name"],
            "legal_name": row["legal_name"],
            "nit": row["nit"],
            "industry": row["industry"],
            "practice_setting": row["practice_setting"],
            "jurisdiction": row["jurisdiction"],
            "size_employees": row["size_employees"],
            "pain_points_md": row["pain_points_md"],
            "metadata": dict(row["metadata"] or {}),
            "_source": "firms_profile_table",
        }

    @staticmethod
    def _default_profile(note: str = "") -> dict:
        """Defaults seguros cuando no hay profile poblado."""
        return {
            "firm_id": None,
            "company_name": "(Firma sin onboarding)",
            "legal_name": None,
            "nit": None,
            "industry": "legal_services",
            "practice_setting": "firm",
            "jurisdiction": "CO",
            "size_employees": None,
            "pain_points_md": None,
            "metadata": {},
            "_source": "defaults",
            "_note": note or "Sin cold-start completado",
            "_action_recommended": "Sugerir al usuario completar /v2/onboarding/cold-start para personalizar outputs",
        }


def build_tool(pool=None, **_: Any) -> ToolDef:
    return LoadCompanyProfileTool(pool=pool)
