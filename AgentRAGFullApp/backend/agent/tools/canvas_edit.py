"""F-Canvas · Tools que el agente usa para co-editar el documento en el
Live Canvas del navegador.

Las 5 tools NO escriben a la base de datos directamente — emiten
`_ui_command` que el frontend (CanvasEditor + UICommandBus) ejecuta.
El autosave del editor persiste a `matter_document_versions` cada 3s.

Tools:
  canvas_get_current        → lee el contenido actual (para analizarlo)
  canvas_set_text           → reemplaza TODO el documento (drafts desde 0)
  canvas_append             → añade párrafo o sección al final
  canvas_replace_section    → reemplaza el contenido bajo un heading
  canvas_save_version       → fuerza guardar versión

Notas:
  - canvas_get_current devuelve los datos como `summary` (visible al modelo)
    porque NO emite ui_command — sólo necesita pedir al cliente que
    devuelva el estado. Esto se hace via un evento sincronico al frontend.
    En esta v1 simplificamos: el modelo NO puede leer el contenido actual
    directamente desde una tool; debe pedirle al usuario o asumir.
    (Phase B: implementar request-response sobre WS para leer el estado.)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _ui(action: str, **payload) -> dict:
    return {"action": action, **payload}


# ─────────────────────────────────────────────────────────────────────


async def canvas_set_text_tool(args: dict, ctx: dict) -> dict:
    """Reemplaza TODO el contenido del Canvas con un markdown.

    Usar cuando rediseñas el documento desde cero (ej. después de
    draft_pleading retorna un texto completo y quieres ponerlo en Canvas).
    """
    markdown = (args.get("markdown") or "").strip()
    if not markdown:
        return {"error": "markdown requerido"}
    if len(markdown) > 100_000:
        return {"error": "markdown excede 100k caracteres"}
    return {
        "summary": f"Reemplacé el documento ({len(markdown)} caracteres)",
        "_ui_command": _ui("canvas_set_text", markdown=markdown),
    }


async def canvas_append_tool(args: dict, ctx: dict) -> dict:
    """Añade un fragmento de markdown al final del documento."""
    markdown = (args.get("markdown") or "").strip()
    if not markdown:
        return {"error": "markdown requerido"}
    if len(markdown) > 50_000:
        return {"error": "markdown excede 50k caracteres"}
    return {
        "summary": f"Añadí {len(markdown)} caracteres al final del documento",
        "_ui_command": _ui("canvas_append", markdown=markdown),
    }


async def canvas_replace_section_tool(args: dict, ctx: dict) -> dict:
    """Reemplaza el contenido bajo un heading específico (h1/h2/h3).

    Match por substring case-insensitive del título. Si no hay match,
    el frontend hace append al final como fallback.
    """
    heading = (args.get("heading") or "").strip()
    markdown = (args.get("markdown") or "").strip()
    if not heading:
        return {"error": "heading requerido"}
    if not markdown:
        return {"error": "markdown requerido"}
    if len(heading) > 200:
        return {"error": "heading muy largo"}
    return {
        "summary": f"Reemplacé sección '{heading}'",
        "_ui_command": _ui("canvas_replace_section", heading=heading, markdown=markdown),
    }


async def canvas_save_version_tool(args: dict, ctx: dict) -> dict:
    """Fuerza guardar versión del documento actual a matter_document_versions."""
    return {
        "summary": "Versión guardada",
        "_ui_command": _ui("canvas_save_version"),
    }


# ─────────────────────────────────────────────────────────────────────


async def get_document_content_tool(args: dict, ctx: dict) -> dict:
    """Devuelve el TEXTO COMPLETO de un documento del expediente.

    Distinto a:
      · summarize_document → resumen IA breve (1-3 oraciones)
      · extract_document_entities → partes/fechas/obligaciones estructuradas
      · canvas_get_current → sólo el canvas activo

    Esta tool devuelve el CONTENIDO COMPLETO de un documento (la última
    versión guardada). Fallback chain:
      1. matter_document_versions (la versión más reciente)
      2. matter_documents.resumen_ia (si no hay versions)
      3. chunks (RAG ingest, si existe ingest_doc_id)

    Casos de uso:
      · "Léeme la demanda completa" → get_document_content + reporta
      · "Carga este documento al canvas" → get_document_content + canvas_set_text
      · "Analiza la demanda" → get_document_content + análisis sobre text
    """
    document_id = args.get("document_id")
    if not document_id:
        return {"error": "document_id requerido"}
    firm_id = ctx.get("firm_id")
    if not firm_id:
        return {"error": "firm_id requerido"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}

    import json as _json

    async with storage.pool.acquire() as conn:
        # 1) Última versión del documento (canvas o draft persistido)
        ver = await conn.fetchrow(
            """
            select v.version, v.diff_from_prev, v.created_at,
                   d.titulo, d.kind, d.pages
            from matter_document_versions v
            join matter_documents d on d.id = v.matter_document_id
            where v.matter_document_id = $1::uuid
              and v.firm_id = $2::uuid
            order by v.version desc
            limit 1
            """,
            document_id, firm_id,
        )
        if ver:
            diff = ver["diff_from_prev"] or {}
            if isinstance(diff, str):
                try:
                    diff = _json.loads(diff)
                except Exception:
                    diff = {}
            html = diff.get("html") if isinstance(diff, dict) else None
            text = diff.get("text") if isinstance(diff, dict) else None
            if html or text:
                return {
                    "document_id": str(document_id),
                    "titulo": ver["titulo"],
                    "kind": ver["kind"],
                    "version": ver["version"],
                    "pages": ver["pages"],
                    "html": html,
                    "text": (text or "")[:30_000],
                    "source": "matter_document_versions",
                }

        # 2) Fallback: resumen_ia como contenido base si no hay version
        doc = await conn.fetchrow(
            """
            select id, titulo, kind, pages, status, resumen_ia, ingest_doc_id
            from matter_documents
            where id = $1::uuid and firm_id = $2::uuid
            """,
            document_id, firm_id,
        )
        if not doc:
            return {"error": "documento no encontrado"}

        if doc["resumen_ia"] and len(doc["resumen_ia"]) > 100:
            return {
                "document_id": str(doc["id"]),
                "titulo": doc["titulo"],
                "kind": doc["kind"],
                "pages": doc["pages"],
                "version": 0,
                "html": None,
                "text": doc["resumen_ia"],
                "source": "resumen_ia",
                "warning": "El documento aún no tiene versión editable; usando resumen IA como base.",
            }

        # 3) Último fallback: chunks RAG ingestados
        if doc["ingest_doc_id"]:
            chunks = await conn.fetch(
                "select content from chunks where document_id = $1::uuid "
                "order by chunk_index limit 80",
                doc["ingest_doc_id"],
            )
            if chunks:
                text = "\n\n".join(c["content"] for c in chunks if c["content"])
                if text:
                    return {
                        "document_id": str(doc["id"]),
                        "titulo": doc["titulo"],
                        "kind": doc["kind"],
                        "pages": doc["pages"],
                        "version": 0,
                        "html": None,
                        "text": text[:30_000],
                        "source": "rag_chunks",
                    }

    return {
        "document_id": str(document_id),
        "titulo": doc["titulo"] if doc else None,
        "html": None,
        "text": None,
        "warning": (
            "Este documento no tiene contenido editable disponible. "
            "Genera uno con draft_pleading + canvas_set_text."
        ),
    }


async def canvas_get_current_tool(args: dict, ctx: dict) -> dict:
    """Lee el documento actual desde DB (matter_document_versions más reciente).

    En vez de pedir al frontend que envíe el contenido (round-trip),
    leemos la última versión persistida. Como el editor autosave cada 3s,
    el delta es menor a 3s. Para el caller pedir el estado en tiempo real
    no es crítico para el LLM.
    """
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    document_id = args.get("document_id")
    if not matter_id and not document_id:
        return {"error": "matter_id o document_id requerido"}

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}

    async with storage.pool.acquire() as conn:
        if document_id:
            row = await conn.fetchrow(
                """
                select v.id, v.matter_document_id, v.version, v.diff_from_prev, v.created_at,
                       d.titulo, d.kind
                from matter_document_versions v
                join matter_documents d on d.id = v.matter_document_id
                where v.matter_document_id = $1::uuid
                  and v.firm_id = $2::uuid
                order by v.version desc
                limit 1
                """,
                document_id, ctx.get("firm_id"),
            )
        else:
            row = await conn.fetchrow(
                """
                select v.id, v.matter_document_id, v.version, v.diff_from_prev, v.created_at,
                       d.titulo, d.kind
                from matter_document_versions v
                join matter_documents d on d.id = v.matter_document_id
                where d.matter_id = $1::uuid
                  and v.firm_id = $2::uuid
                order by v.created_at desc
                limit 1
                """,
                matter_id, ctx.get("firm_id"),
            )
    if not row:
        return {
            "found": False,
            "summary": "El documento aún no tiene versiones guardadas en Canvas.",
        }
    diff = row["diff_from_prev"] or {}
    html = diff.get("html") if isinstance(diff, dict) else None
    text = diff.get("text") if isinstance(diff, dict) else None
    return {
        "found": True,
        "version": row["version"],
        "document_titulo": row["titulo"],
        "kind": row["kind"],
        "text_preview": (text or "")[:2000],
        "summary": (
            f"Documento '{row['titulo']}' versión {row['version']} · "
            f"{len(text or '')} caracteres."
        ),
    }
