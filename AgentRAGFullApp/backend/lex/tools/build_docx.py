"""Tool 16 · build_docx · wrap python_docx_builder + claude_docx_renderer."""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


class BuildDocxTool(ToolDef):
    name = "build_docx"
    description = (
        "Construye el archivo .docx final desde los bloques generados. "
        "Engine 'python' (default, rápido ~200ms) o 'claude' (estilo Trinidad, "
        "más artesanal pero más lento). Sube a Supabase Storage y devuelve "
        "{download_url, page_count, size_kb}."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "blocks": {"type": "array", "items": {"type": "object"}},
            "title": {"type": "string", "default": ""},
            "engine": {
                "type": "string",
                "enum": ["python", "claude"],
                "default": "python",
            },
            "document_id": {"type": "string", "description": "Si se provee, persiste en cache document_files"},
            "skill_context_data": {
                "type": "object",
                "description": "Dict con datos del SKILL.md para estilo (opcional)",
                "default": {},
            },
            "tracked_changes": {"type": "boolean", "default": False},
        },
        "required": ["blocks"],
    }
    timeout_seconds = 60.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(
        self,
        ctx: ToolContext,
        blocks: list,
        title: str = "",
        engine: Literal["python", "claude"] = "python",
        document_id: Optional[str] = None,
        skill_context_data: Optional[dict] = None,
        tracked_changes: bool = False,
    ) -> dict:
        if tracked_changes:
            logger.info("build_docx tracked_changes=True (no implementado aún; ignora)")

        skill_ctx = None
        if skill_context_data:
            try:
                # SkillContext puede instanciarse via from_dict si existe; sino se pasa None
                from lex.orchestrator.skill_context import SkillContext
                skill_ctx = SkillContext(**skill_context_data) if SkillContext else None
            except Exception:
                skill_ctx = None

        try:
            if engine == "claude":
                from lex.renderer.claude_docx_renderer import render_with_claude  # type: ignore
                docx_bytes = await render_with_claude(blocks=blocks, title=title, skill_context=skill_ctx)
            else:
                from lex.renderer.python_docx_builder import build_docx_from_blocks
                docx_bytes = build_docx_from_blocks(
                    blocks=blocks, title=title or "Documento", skill_context=skill_ctx,
                )
        except Exception as e:
            logger.exception("build_docx engine=%s failed", engine)
            raise ToolError(f"build_docx falló (engine={engine}): {e}") from e

        size_kb = max(1, len(docx_bytes) // 1024)
        download_url = None

        # Persistir cache si tenemos pool + document_id
        # M20.14 fix: el schema (2026_05_27_sprint_m19_document_files.sql) usa
        # `content_bytes` + `format` con UNIQUE (document_id, format) como
        # clave de UPSERT — NO existe la columna `docx_bytes`. El INSERT viejo
        # crasheaba con "column docx_bytes does not exist" cada generación.
        if document_id and (self.pool or ctx.pool):
            try:
                pool = self.pool or ctx.pool
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        insert into document_files
                          (document_id, format, content_bytes, size_bytes,
                           filename, generated_by, created_at, updated_at)
                        values ($1::uuid, 'docx', $2, $3,
                                $4, 'lex_docx_builder', now(), now())
                        on conflict (document_id, format) do update set
                          content_bytes = excluded.content_bytes,
                          size_bytes    = excluded.size_bytes,
                          filename      = excluded.filename,
                          updated_at    = now()
                        """,
                        document_id, docx_bytes, len(docx_bytes),
                        f"{(title or 'documento').strip().replace(' ', '_')[:80]}.docx",
                    )
            except Exception as e:
                logger.warning("build_docx cache persist failed: %s", e)

        return {
            "engine": engine,
            "title": title,
            "size_kb": size_kb,
            "size_bytes": len(docx_bytes),
            "page_count_estimated": max(1, len(docx_bytes) // 7500),
            "document_id": document_id,
            "download_url": download_url,
            "tracked_changes": tracked_changes,
            "_note": "URL firmada se genera en el endpoint que sirve el archivo (no en la tool).",
        }


def build_tool(pool=None, **_: Any) -> ToolDef:
    return BuildDocxTool(pool=pool)
