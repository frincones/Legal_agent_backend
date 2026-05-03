"""draft_pleading tool · genera escritos procesales usando plantillas
profesionales colombianas (agent/tools/legal_templates.py).

v2 (mayo 2026): plantillas reescritas con estructura formal completa
(encabezamiento → partes → hechos numerados → pretensiones principales
y subsidiarias → fundamentos de derecho con citas → pruebas (documental,
testimonial, interrogatorio, inspección) → anexos → notificaciones →
juramento estimatorio CGP Art. 206 → firma con T.P.).

Output queda en markdown con headings H1/H2 que el editor TipTap
renderiza como secciones formales. Listo para edición humana o
modificaciones del agente vía canvas_replace_section/canvas_append.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from agent.tools.legal_templates import TEMPLATES, render_template, list_templates  # noqa: F401

logger = logging.getLogger(__name__)


async def draft_pleading_tool(args: dict, ctx: dict) -> dict:
    """Adaptador del tool para OpenAI Realtime.

    Args:
      kind: tipo de documento (demanda_ordinaria_laboral, tutela, contestacion,
            recurso_apelacion, derecho_peticion, carta_requerimiento,
            contrato, escrito)
      matter_id: caso al cual asociar
      facts: hechos del caso (nombre, cédula, fechas, salario, etc.)
      citations: lista de citation_refs verificadas a incluir

    Returns:
      { id, kind, content, citations_count }
      `content` es markdown profesional listo para Canvas.
    """
    kind = args.get("kind", "demanda_ordinaria_laboral")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    facts = dict(args.get("facts") or {})
    citations = args.get("citations") or []
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")

    if kind not in TEMPLATES:
        return {
            "error": (
                f"kind '{kind}' no soportado · use uno de {list(TEMPLATES.keys())}"
            ),
        }

    # Inyectar bloque de jurisprudencia formateado a partir de citations[]
    if citations and "jurisprudencia_aplicable" not in facts:
        cit_lines = []
        for c in citations:
            if isinstance(c, dict):
                ref = c.get("citation_ref") or c.get("numero") or "—"
                rubro = c.get("rubro") or ""
                cit_lines.append(f"- **{ref}** — {rubro}".rstrip(" —"))
            else:
                cit_lines.append(f"- **{c}**")
        facts["jurisprudencia_aplicable"] = "\n".join(cit_lines) if cit_lines else (
            "No se identificó jurisprudencia verificada para este punto."
        )
    elif "jurisprudencia_aplicable" not in facts:
        facts["jurisprudencia_aplicable"] = (
            "_Pendiente: el agente debe llamar research_jurisprudence y completar este bloque._"
        )

    try:
        content = render_template(kind, facts)
    except Exception as e:
        logger.exception("template render failed: %s", e)
        return {"error": f"render template falló: {e}"}

    # Persist as matter_documents row (status=completed para que get_document_content lo lea)
    doc_id = str(uuid.uuid4())
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if hasattr(storage, "pool"):
            async with storage.pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into matter_documents
                      (id, firm_id, matter_id, kind, titulo, status,
                       uploaded_by, mime_type, sha256, pages, ocr_done, resumen_ia)
                    values
                      ($1::uuid, $2::uuid, $3::uuid, 'generado'::doc_kind, $4,
                       'completed', $5::uuid, 'text/markdown', $6, $7, false, $8)
                    """,
                    doc_id,
                    firm_id,
                    matter_id,
                    f"{TEMPLATES[kind]['title']} · LexAI",
                    user_id,
                    f"sha-{doc_id[:16]}",
                    max(1, len(content) // 2500),  # estimación de páginas
                    content[:500],  # resumen breve para el listado
                )
                # Versión 1 con el contenido completo (canvas-ready)
                await conn.execute(
                    """
                    insert into matter_document_versions
                      (matter_document_id, firm_id, version, generated_by, diff_from_prev)
                    values ($1::uuid, $2::uuid, 1, 'draft_pleading', $3::jsonb)
                    """,
                    doc_id, firm_id,
                    json.dumps({"text": content, "byte_size": len(content)}),
                )
    except Exception as e:
        logger.warning("draft_pleading persist failed: %s", e)

    return {
        "id": doc_id,
        "kind": kind,
        "matter_id": matter_id,
        "title": TEMPLATES[kind]["title"],
        "content": content,
        "citations_count": len(citations),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


async def list_legal_templates_tool(args: dict, ctx: dict) -> dict:
    """Lista las plantillas legales disponibles para drafting.

    Útil cuando el abogado pregunta '¿qué plantillas tienes?' o cuando
    el agente necesita decidir cuál usar antes de llamar draft_pleading.
    """
    return {"count": len(TEMPLATES), "templates": list_templates()}
