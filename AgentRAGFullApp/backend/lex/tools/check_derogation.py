"""Tool 10 · check_derogation · wrap DerogationVerifier."""
from __future__ import annotations

import logging
from typing import Any

from lex.verify.derogation_verifier import DerogationVerifier

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


class CheckDerogationTool(ToolDef):
    name = "check_derogation"
    description = (
        "Verifica si una norma colombiana está vigente o derogada. "
        "Útil cuando la cita está confirmada (existe) pero queremos asegurar "
        "que no haya sido derogada implícita o explícitamente. "
        "Para verificación completa (existencia + vigencia + fuente_url) usar verify_citation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "norma_text": {
                "type": "string",
                "description": "Texto de la norma, e.g. 'Ley 50 de 1990' o 'Decreto 100 de 1980'",
            },
        },
        "required": ["norma_text"],
    }
    cacheable = True
    cache_ttl_seconds = 86400
    timeout_seconds = 10.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(self, ctx: ToolContext, norma_text: str) -> dict:
        pool = self.pool or ctx.pool
        if pool is None:
            return {"norma": norma_text, "vigente": True, "_warning": "pool no disponible (asume vigente)"}

        verifier = DerogationVerifier(pool=pool)
        res = await verifier.check(norma_text)
        return {
            "norma": res.norma,
            "vigente": bool(res.vigente),
            "derogada_por": res.derogada_por,
            "fecha_derogacion": res.fecha_derogacion,
            "confidence": float(res.confidence),
        }


def build_tool(pool=None, **_: Any) -> ToolDef:
    return CheckDerogationTool(pool=pool)
