"""Tool 11 · generate_clause · wrap block_generator atomizado por sección.

Esta es la tool más usada del Brain: redacta UNA sección/cláusula a la vez.
El Brain itera N veces (y puede paralelizar varias secciones independientes).

Reusa generate_section_blocks() del block_generator existente. NO reescribe
la lógica de redacción.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from lex.orchestrator.stages.block_generator import generate_section_blocks

from .base import ToolContext, ToolDef, ToolError

logger = logging.getLogger(__name__)


class GenerateClauseTool(ToolDef):
    name = "generate_clause"
    description = (
        "Genera UNA sección/cláusula del documento (e.g., 'objeto', 'hechos', "
        "'pretensiones', 'facultades'). El Brain debe invocar esta tool por cada "
        "sección que componga el documento. Múltiples secciones independientes "
        "PUEDEN invocarse en paralelo (asyncio.gather, hasta 10 simultáneos)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "doc_type": {"type": "string"},
            "section_key": {"type": "string", "description": "Key de la sección, e.g. 'objeto', 'hechos'"},
            "section_title": {"type": "string"},
            "section_order": {"type": "integer", "default": 1},
            "intent": {"type": "string"},
            "brief": {"type": "string", "default": ""},
            "extracted_data": {"type": "object", "default": {}},
            "section_instruction": {"type": "string", "default": ""},
            "verified_citations": {"type": "array", "items": {"type": "object"}, "default": []},
            "previous_blocks": {"type": "array", "items": {"type": "object"}, "default": []},
            "regenerate": {"type": "boolean", "default": False, "description": "Si true, fuerza re-generación ignorando cache"},
        },
        "required": ["doc_type", "section_key", "section_title", "intent"],
    }
    invokes_llm = True
    timeout_seconds = 60.0

    def __init__(self, pool=None, anthropic_client=None, openai_client=None, **_: Any):
        self.pool = pool
        self.anthropic_client = anthropic_client
        self.openai_client = openai_client

    async def run(
        self,
        ctx: ToolContext,
        doc_type: str,
        section_key: str,
        section_title: str,
        intent: str,
        section_order: int = 1,
        brief: str = "",
        extracted_data: Optional[dict] = None,
        section_instruction: str = "",
        verified_citations: Optional[list] = None,
        previous_blocks: Optional[list] = None,
        regenerate: bool = False,
    ) -> dict:
        client = self.openai_client or ctx.openai_client or self.anthropic_client or ctx.anthropic_client
        if client is None:
            raise ToolError("generate_clause requiere LLM client (OpenAI o Anthropic)")

        blocks: list[dict] = []
        try:
            async for block in generate_section_blocks(
                client=client,
                doc_type=doc_type,
                section_key=section_key,
                section_title=section_title,
                section_order=section_order,
                intent=intent,
                brief=brief or "",
                extracted_data=extracted_data or {},
                section_instruction=section_instruction or "",
                verified_citations=verified_citations or [],
                previous_blocks=previous_blocks or [],
            ):
                if hasattr(block, "model_dump"):
                    blocks.append(block.model_dump())
                elif hasattr(block, "dict"):
                    blocks.append(block.dict())
                elif isinstance(block, dict):
                    blocks.append(block)
                else:
                    blocks.append({"raw": str(block)})
        except Exception as e:
            logger.exception("generate_clause failed for %s", section_key)
            raise ToolError(f"generate_section_blocks falló: {e}") from e

        return {
            "section_key": section_key,
            "section_title": section_title,
            "section_order": section_order,
            "doc_type": doc_type,
            "blocks": blocks,
            "block_count": len(blocks),
            "regenerated": regenerate,
        }


def build_tool(pool=None, anthropic_client=None, openai_client=None, **_: Any) -> ToolDef:
    return GenerateClauseTool(
        pool=pool, anthropic_client=anthropic_client, openai_client=openai_client,
    )
