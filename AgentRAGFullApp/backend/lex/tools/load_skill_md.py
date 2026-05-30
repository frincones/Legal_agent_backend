"""Tool 1 · load_skill_md · wrap skill_loader.load_skill_context."""
from __future__ import annotations

import logging
from typing import Any

from lex.orchestrator.stages.skill_loader import load_skill_context

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


class LoadSkillMdTool(ToolDef):
    name = "load_skill_md"
    description = (
        "Carga el SKILL.md del tipo de documento solicitado (doc_type). "
        "Retorna el SkillContext con las cláusulas/secciones esperadas, el estilo "
        "(formal/notarial/judicial), placeholders y referencias normativas obligatorias. "
        "Llamar SIEMPRE al inicio de cualquier generación de documento."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "doc_type": {
                "type": "string",
                "description": "ID del tipo de documento: 'poder_especial', 'demanda_civil_ordinaria', 'derecho_peticion', 'tutela', etc.",
            },
            "jurisdiction": {
                "type": "string",
                "enum": ["CO", "MX"],
                "default": "CO",
            },
        },
        "required": ["doc_type"],
    }
    cacheable = True
    cache_ttl_seconds = 300
    timeout_seconds = 10.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(self, ctx: ToolContext, doc_type: str, jurisdiction: str = "CO") -> dict:
        firm_id = str(ctx.firm_id) if ctx.firm_id else None
        pool = self.pool or ctx.pool
        # M20.14 fix: degradación graceful sin pool (test/dev sin Supabase).
        # Sin esto, load_skill_context(None, ...) crashea con AttributeError
        # en pool.acquire().
        if pool is None:
            return {
                "found": False,
                "doc_type": doc_type,
                "jurisdiction": jurisdiction,
                "note": ("Pool Supabase no disponible. Brain debe inferir la estructura "
                          f"del documento ({doc_type}) desde su conocimiento."),
                "_warning": "no_pool_skill_loader",
            }
        try:
            skill_ctx = await load_skill_context(
                pool, doc_type=doc_type, firm_id=firm_id, use_cache=True,
            )
        except Exception as e:
            logger.warning("load_skill_md degraded (%s): %s", type(e).__name__, e)
            return {
                "found": False,
                "doc_type": doc_type,
                "jurisdiction": jurisdiction,
                "note": f"Error cargando SKILL.md ({type(e).__name__}); Brain infiere estructura.",
                "_error": str(e)[:200],
            }
        if skill_ctx is None:
            return {
                "found": False,
                "doc_type": doc_type,
                "jurisdiction": jurisdiction,
                "note": f"No hay SKILL.md registrado para doc_type={doc_type!r}; el agente debe inferir estructura.",
            }
        return {
            "found": True,
            "doc_type": skill_ctx.doc_type,
            "skill_id": skill_ctx.skill_id,
            "skill_command": skill_ctx.skill_command,
            "source": skill_ctx.source,
            "frontmatter": {
                "doc_type": skill_ctx.parsed.frontmatter.doc_type,
                "jurisdiction": getattr(skill_ctx.parsed.frontmatter, "jurisdiction", jurisdiction),
                "allowed_tools": getattr(skill_ctx.parsed.frontmatter, "allowed_tools", []),
            },
            "system_prompt_excerpt": (skill_ctx.parsed.system_prompt or "")[:600],
            "sections": [s.get("key") if isinstance(s, dict) else str(s)
                          for s in (getattr(skill_ctx.parsed, "sections_plan", None) or [])],
            "has_references": bool(getattr(skill_ctx.parsed, "references_md", None)),
        }


def build_tool(pool=None, **_: Any) -> ToolDef:
    return LoadSkillMdTool(pool=pool)
