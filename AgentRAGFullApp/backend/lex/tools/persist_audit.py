"""Tool 18 · persist_audit · llama AuditRepo + BlocksRepo + agent_traces."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .base import ToolContext, ToolDef

logger = logging.getLogger(__name__)


class PersistAuditTool(ToolDef):
    name = "persist_audit"
    description = (
        "Persiste el audit completo de la generación: generation_audit (alto nivel), "
        "document_blocks (cada bloque generado), agent_traces (trace del Brain) y "
        "opcionalmente agent_memory (memoria semántica para recall futuro). "
        "Llamar UNA vez al final de la generación, después de build_docx."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "template_id": {"type": "string"},
            "blocks": {"type": "array", "items": {"type": "object"}, "default": []},
            "duration_seconds": {"type": "number"},
            "cost_usd": {"type": "number", "default": 0},
            "citations": {"type": "array", "items": {"type": "object"}, "default": []},
            "calculations": {"type": "object", "default": {}},
            "validation_passed": {"type": "boolean", "default": False},
            "qa_score": {"type": "number"},
            "warnings": {"type": "array", "items": {"type": "string"}, "default": []},
            "model_used": {"type": "object", "default": {}},
            "save_to_memory": {"type": "boolean", "default": False},
            "memory_key": {"type": "string", "default": ""},
            "memory_value": {"type": "object", "default": {}},
        },
        "required": ["template_id"],
    }
    timeout_seconds = 20.0

    def __init__(self, pool=None, **_: Any):
        self.pool = pool

    async def run(
        self,
        ctx: ToolContext,
        template_id: str,
        document_id: Optional[str] = None,
        blocks: Optional[list] = None,
        duration_seconds: float = 0,
        cost_usd: float = 0,
        citations: Optional[list] = None,
        calculations: Optional[dict] = None,
        validation_passed: bool = False,
        qa_score: Optional[float] = None,
        warnings: Optional[list] = None,
        model_used: Optional[dict] = None,
        save_to_memory: bool = False,
        memory_key: str = "",
        memory_value: Optional[dict] = None,
    ) -> dict:
        pool = self.pool or ctx.pool
        if pool is None:
            return {"persisted": False, "_warning": "pool no disponible"}

        results: dict[str, Any] = {}
        firm_id_str = str(ctx.firm_id) if ctx.firm_id else None

        # 1) generation_audit
        try:
            from lex.storage.audit_repo import AuditRepo
            repo = AuditRepo(pool=pool)
            audit_id = await repo.insert_audit(
                generation_id=str(ctx.generation_id),
                template_id=template_id,
                firm_id=firm_id_str,
                document_id=document_id,
                duration_seconds=duration_seconds,
                cost_usd=cost_usd,
                citations=citations or [],
                calculations=calculations or {},
                validation_passed=validation_passed,
                qa_score=qa_score,
                warnings=warnings or [],
                model_used=model_used or {},
                audit_json={
                    "orchestrator_kind": "lean",
                    "tools_iteration_count": ctx.metadata.get("iterations", 0),
                },
            )
            results["generation_audit_id"] = audit_id
        except Exception as e:
            logger.warning("persist_audit generation_audit failed: %s", e)

        # 1.5) actualizar orchestrator_kind explícitamente
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "update generation_audit set orchestrator_kind = 'lean' where generation_id = $1::uuid",
                    str(ctx.generation_id),
                )
        except Exception as e:
            logger.debug("orchestrator_kind update non-critical fail: %s", e)

        # 2) document_blocks (si tenemos document_id)
        if document_id and blocks:
            try:
                from lex.storage.blocks_repo import BlocksRepo
                brepo = BlocksRepo(pool=pool)
                order = 0
                inserted = 0
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    block_id = b.get("block_id") or f"b-{order:04d}"
                    section_key = b.get("section_key") or "default"
                    block_type = b.get("type") or b.get("block_type") or "paragraph"
                    block_data = b.get("block_data") or b
                    try:
                        await brepo.insert_block(
                            document_id=document_id,
                            generation_id=str(ctx.generation_id),
                            section_key=section_key,
                            block_order=order,
                            block_id=block_id,
                            block_type=block_type,
                            block_data=block_data,
                        )
                        inserted += 1
                    except Exception as e:
                        logger.debug("insert_block %s failed: %s", block_id, e)
                    order += 1
                results["blocks_inserted"] = inserted
            except Exception as e:
                logger.warning("persist_audit document_blocks failed: %s", e)

        # 3) agent_traces (trace de la sesión Brain)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into agent_traces (
                      firm_id, session_id, turn_number, llm_model,
                      messages, response, duration_ms, metadata
                    )
                    values ($1, $2, $3, $4, '[]'::jsonb, '{}'::jsonb, $5, $6::jsonb)
                    """,
                    ctx.firm_id,
                    str(ctx.generation_id),
                    int(ctx.iteration or 0),
                    (model_used or {}).get("brain") or "claude-sonnet-4-6",
                    int(duration_seconds * 1000),
                    json.dumps({
                        "template_id": template_id,
                        "tools_used": list((model_used or {}).get("tools_used", [])),
                        "cost_usd": cost_usd,
                    }, default=str),
                )
            results["agent_trace_inserted"] = True
        except Exception as e:
            logger.debug("agent_traces insert non-critical: %s", e)

        # 4) agent_memory (semántica, opcional)
        if save_to_memory and memory_key:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        insert into agent_memory (
                          firm_id, session_id, kind, key, value, metadata
                        )
                        values ($1, $2, 'episodic', $3, $4::jsonb, '{}'::jsonb)
                        """,
                        ctx.firm_id, str(ctx.generation_id),
                        memory_key,
                        json.dumps(memory_value or {}, default=str),
                    )
                results["memory_persisted"] = True
            except Exception as e:
                logger.debug("agent_memory insert non-critical: %s", e)

        results["persisted"] = True
        return results


def build_tool(pool=None, **_: Any) -> ToolDef:
    return PersistAuditTool(pool=pool)
