"""Sprint M9 · Chat conversacional sobre documento generado.

Endpoint POST /v1/documents/v2/documents/{document_id}/chat:
  Body: { message: str, history?: [{role, content}, ...] }
  Response: { reply: str, actions: [{kind: 'update_block'|'regenerate_section'|'add_block'|'info_only', ...}], blocks_changed: int }

Flow:
1. Carga bloques actuales del document_id
2. Construye contexto: bloques + historial chat + nuevo mensaje
3. gpt-4o-mini con structured output decide acción:
   - update_block: modificar un bloque existente (cambiar dato, agregar texto)
   - regenerate_section: regenerar sección completa
   - add_block: agregar bloque nuevo en posición específica
   - info_only: solo responder al usuario sin cambiar el documento
4. Aplica las acciones a document_blocks
5. Devuelve respuesta natural + lista de acciones aplicadas
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from lex.storage import BlocksRepo
from utils.db import get_storage
from utils.llm import get_openai_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/documents/v2", tags=["documents-v2-chat"])


def _flag_enabled() -> bool:
    return os.getenv("FLAG_DOCGEN_V2", "false").lower() in ("1", "true", "yes")


async def _require_session(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return {"token": auth[7:]}


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class SelectionContext(BaseModel):
    """M19.16.B3 + M19.17.A — selección de texto del usuario en el canvas editable.

    Permite que el chat reciba el contexto exacto de qué bloque y qué texto
    está seleccionado, para ediciones puntuales tipo ChatGPT Canvas
    ("resume este párrafo", "reescribe esto en tono más formal", etc.).

    Los campos anchor_before/anchor_after vienen del frontend (3-6 palabras
    de contexto inmediato) y permiten al backend hacer find-and-replace
    quirúrgico sin ambigüedad cuando el texto seleccionado aparece varias
    veces en el bloque.
    """
    block_id: str
    text: str
    instruction: str | None = None  # "resumir" | "reescribir" | "expandir" | etc.
    anchor_before: str | None = None
    anchor_after: str | None = None


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list)
    selection: SelectionContext | None = None  # M19.16.B3
    # M19.17.B — contexto de un clarify previo (block_id, selection_text,
    # original_instruction). Si presente, el LLM lo trata como respuesta a
    # la pregunta y ejecuta la acción original con el dato faltante.
    pending_context: dict[str, Any] | None = None


CHAT_SYSTEM_PROMPT = """Eres un asistente legal que ayuda a EDITAR un documento ya generado.

Recibirás:
1. El documento actual en formato resumido (bloques tipados)
2. El historial de chat con el usuario
3. Un nuevo mensaje del usuario

Tu tarea es:
- Interpretar qué quiere el usuario (agregar dato, cambiar texto, regenerar sección, hacer pregunta)
- Devolver JSON con:
  {
    "reply": "respuesta natural breve al usuario (1-2 frases)",
    "actions": [
      // 0 o más acciones a aplicar al documento
      {"kind": "replace_selection", "block_id": "<id>",
        "original_text": "texto exacto que el usuario tiene seleccionado",
        "replacement_text": "texto nuevo que reemplaza la selección",
        "anchor_before": "3-6 palabras inmediatamente antes (para evitar match ambiguo)",
        "anchor_after": "3-6 palabras inmediatamente después"},
      {"kind": "update_block", "block_id": "<id>", "new_runs": [{"text": "...", "bold": false}]},
      {"kind": "regenerate_section", "section_key": "hechos|pretensiones|..."},
      {"kind": "add_block_after",
        "after_block_id": "<id existente del bloque después del cual insertar>",
        "block": {
          "type": "paragraph|hecho|pretension|list_item|norma_citada|jurisprudencia|juramento|firma|blank|...",
          "runs": [{"text": "...", "bold": false}],   // para paragraph/hecho/pretension/list_item
          "section_key": "<misma sección del anchor o nueva>"
          // campos específicos según el type
        }},
      {"kind": "info_only"},
      {"kind": "clarify",
        "question": "Pregunta concreta para el usuario",
        "pending_context": {
          "block_id": "<id si la pregunta es sobre un bloque>",
          "selection_text": "<texto si la pregunta es sobre una selección>",
          "original_instruction": "<instrucción original del usuario que motivó la duda>"
        }},
      {"kind": "propagate_change",
        "old_text": "<texto literal a reemplazar globalmente, ej: 'TRANSPORTES VELOZ DEL VALLE S.A.S.'>",
        "new_text": "<texto nuevo, ej: 'TDX Transformación Digital S.A.S.'>",
        "case_insensitive": false,
        "exclude_block_ids": ["<opcional: bloques donde NO aplicar>"]}
    ]
  }

REGLAS DE ACCIÓN (M19.17.A):
- **SI HAY SELECCIÓN DEL USUARIO** (campo "SELECCIÓN ACTIVA"): SIEMPRE usa "replace_selection"
  con el original_text exacto que el usuario seleccionó (literal, copy-paste sin parafrasear).
  Esto garantiza que SOLO cambias lo seleccionado, no el resto del bloque.
  Para anchor_before/after, copia 3-6 palabras del bloque (NO inventes ni resumas).
  Si la selección es ambigua (aparece varias veces en el bloque), incluye anchors largos.
- Si NO hay selección y el usuario da datos (nombre, cédula, fecha, etc), busca los bloques con
  placeholders [PLACEHOLDER] y emite "update_block" reemplazando esos placeholders.
- Si pide regenerar una sección entera (ej. "regenera los hechos"), usa "regenerate_section".
- Si pregunta algo sin cambiar el doc, usa "info_only".
- NO inventes block_ids — usa solo los que aparecen en el contexto.
- NO inventes texto que el usuario no seleccionó.
- Sé conciso en "reply" (máximo 200 chars).

REGLA — CUÁNDO USAR "propagate_change" (M19.18.B):
Cuando el usuario pide explícitamente o implícitamente CAMBIAR un dato en todo
el documento (un nombre, una fecha, un monto, etc.), prefiere "propagate_change"
sobre múltiples "update_block". Ejemplos:
  - "cámbialo en todo el documento" → propagate_change con el old/new
  - "renombra X por Y" → propagate_change
  - "actualiza todas las menciones de…" → propagate_change
  - Si el usuario solo dice "cambia el nombre" sin especificar el nuevo,
    primero "clarify" preguntando el nuevo nombre, luego propagate_change.

Después de "propagate_change", PUEDES adicionalmente devolver update_block
puntuales para bloques estructurados (firma, norma_citada) que no se tocan
por el rename global.

REGLA — CUÁNDO USAR "clarify" (M19.17.B):
Si la petición del usuario es AMBIGUA, INCOMPLETA o tiene IMPLICACIONES legales
que requieren confirmación antes de actuar, emite UNA acción "clarify" con una
pregunta CONCRETA (cerrada, idealmente sí/no o con 2-3 opciones) y NO ejecutes
ninguna otra acción. Ejemplos:
  - El usuario dice "cámbiale el nombre del demandado" pero el documento tiene
    3 menciones del demandado en distintas formas → preguntar cuál mantener.
  - El usuario seleccionó un texto que cita una norma con vigencia parcial →
    "¿quieres mantener la referencia a la versión derogada o cambiar por la
     versión vigente?"
  - El usuario pide "agrega referencia jurisprudencial" pero no especifica
    cuál → preguntar tema concreto.
  - El usuario pide algo que afectaría la cuantía o competencia → confirmar.

EVITA clarify cuando la petición es clara (resumir, reescribir, corregir typo,
cambiar dato simple). Máximo 1 clarify por turn. Si el LLM ya preguntó y el
usuario respondió, EJECUTA la acción sin volver a preguntar.

OUTPUT: SOLO JSON válido, sin markdown ni texto adicional.
"""


def _serialize_blocks_for_chat(blocks: list[dict]) -> str:
    """Resume los bloques para el LLM (limitado, con block_id)."""
    out = []
    for b in blocks[:200]:  # tope
        bid = b.get("block_id") or "?"
        bt = b.get("block_type") or "?"
        bd = b.get("block_data") or {}
        if bt == "title":
            out.append(f"[{bid}] TITLE: {bd.get('text', '')}")
        elif bt == "section_heading":
            out.append(f"[{bid}] §{bd.get('roman', '')}. {bd.get('text', '')} (key={bd.get('section_key')})")
        elif bt == "paragraph":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))[:200]
            out.append(f"[{bid}] PARRAFO: {text}")
        elif bt == "hecho":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))[:200]
            out.append(f"[{bid}] HECHO #{bd.get('num')}: {text}")
        elif bt == "pretension":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))[:200]
            out.append(f"[{bid}] PRETENSION {bd.get('ord')}: {text}")
        elif bt == "norma_citada":
            out.append(f"[{bid}] NORMA: {bd.get('norma')}")
        elif bt == "jurisprudencia":
            out.append(f"[{bid}] JURISP: {bd.get('id')} M.P. {bd.get('mp')}")
        elif bt == "firma":
            out.append(f"[{bid}] FIRMA: {bd.get('nombre')} TP {bd.get('tp')}")
        elif bt == "juramento":
            out.append(f"[{bid}] JURAMENTO")
    return "\n".join(out)


@router.post("/documents/{document_id}/chat")
async def chat_with_document(
    document_id: str,
    body: ChatBody,
    _claims: dict = Depends(_require_session),
):
    if not _flag_enabled():
        raise HTTPException(status_code=503, detail="docgen_v2_disabled")
    storage = await get_storage()
    repo = BlocksRepo(storage.pool)

    blocks = await repo.get_blocks_for_document(document_id)
    if not blocks:
        raise HTTPException(status_code=404, detail="document_not_found_or_empty")

    blocks_summary = _serialize_blocks_for_chat(blocks)
    history_text = "\n".join(
        f"{m.role}: {m.content[:300]}" for m in body.history[-6:]
    ) if body.history else "(sin historial)"

    # M19.16.B3 + M19.17.A — si hay selección, contextualizar al LLM para edit puntual
    selection_block = ""
    if body.selection:
        sel = body.selection
        anchor_before_line = f"anchor_before (3-6 palabras inmediatamente antes): \"{sel.anchor_before}\"" if sel.anchor_before else ""
        anchor_after_line = f"anchor_after (3-6 palabras inmediatamente después): \"{sel.anchor_after}\"" if sel.anchor_after else ""
        selection_block = f"""
=== SELECCIÓN ACTIVA DEL USUARIO EN EL CANVAS ===
Bloque destino: [{sel.block_id}]
Texto EXACTAMENTE seleccionado por el usuario:
\"\"\"
{sel.text[:1200]}
\"\"\"
{anchor_before_line}
{anchor_after_line}
{f"Instrucción rápida: {sel.instruction}" if sel.instruction else ""}

REGLA ESTRICTA — ACCIÓN OBLIGATORIA \"replace_selection\":
Cuando hay selección activa, DEBES emitir exactamente UNA acción \"replace_selection\":
  {{
    "kind": "replace_selection",
    "block_id": "{sel.block_id}",
    "original_text": "<copia literal del texto seleccionado arriba, sin reformular>",
    "replacement_text": "<el texto nuevo que reemplaza la selección, aplicando la instrucción del usuario>",
    "anchor_before": "<copia literal del anchor_before mostrado arriba>",
    "anchor_after": "<copia literal del anchor_after mostrado arriba>"
  }}
NO uses \"update_block\" (reescribir todo el bloque) salvo que el usuario lo pida explícitamente.
NO modifiques texto fuera de la selección. NO inventes contenido nuevo no solicitado.
=== FIN SELECCIÓN ===
"""

    # M19.17.B — si hay pending_context (respuesta del usuario a un clarify previo)
    pending_block = ""
    if body.pending_context:
        pc = body.pending_context
        pending_block = f"""
=== RESPUESTA A PREGUNTA ACLARATORIA PREVIA ===
El usuario está respondiendo a una pregunta que tú hiciste en el turn anterior.
Contexto guardado:
  - block_id: {pc.get('block_id', '?')}
  - selection_text: {(pc.get('selection_text') or '')[:300]}
  - original_instruction: {(pc.get('original_instruction') or '')[:300]}

Con la respuesta del usuario y este contexto, EJECUTA la acción original. NO
vuelvas a preguntar (a menos que haya nueva ambigüedad).
=== FIN ===
"""

    user_prompt = f"""DOCUMENTO ACTUAL (bloques con block_id):
{blocks_summary}
{selection_block}
{pending_block}
HISTORIAL RECIENTE:
{history_text}

NUEVO MENSAJE DEL USUARIO:
{body.message}

Responde con JSON {{"reply": "...", "actions": [...]}}."""

    try:
        client = get_openai_client()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        reply = data.get("reply", "(sin respuesta)")
        actions = data.get("actions", []) or []
    except Exception as e:
        logger.exception("chat LLM failed for doc=%s msg=%r", document_id[:12], body.message[:80])
        return {
            "reply": f"Error procesando: {str(e)[:120]}",
            "actions": [],
            "blocks_changed": 0,
        }

    # M19.18.C — log de cada chat con qué decidió el LLM (debug)
    action_kinds = [a.get("kind", "?") for a in actions]
    logger.info(
        "chat: doc=%s msg=%r selection=%s pending=%s -> %d actions: %s",
        document_id[:12],
        body.message[:80],
        bool(body.selection),
        bool(body.pending_context),
        len(actions),
        ",".join(action_kinds),
    )

    # Aplicar acciones
    blocks_changed = 0
    applied_actions: list[dict] = []
    for act in actions:
        kind = act.get("kind")
        try:
            if kind == "update_block":
                bid = act.get("block_id")
                new_runs = act.get("new_runs") or []
                if bid and isinstance(new_runs, list):
                    ok = await _update_block_runs(storage.pool, document_id, bid, new_runs)
                    if ok:
                        blocks_changed += 1
                        applied_actions.append({"kind": "update_block", "block_id": bid, "ok": True})
            elif kind == "regenerate_section":
                section_key = act.get("section_key")
                if section_key:
                    n = await repo.delete_section_blocks(document_id, section_key)
                    if n > 0:
                        blocks_changed += n
                        applied_actions.append({"kind": "regenerate_section", "section_key": section_key, "deleted": n})
            elif kind == "add_block_after":
                # M19.19.A — implementación real (antes era stub pending_m10)
                after_bid = act.get("after_block_id") or act.get("block_id")
                new_block_raw = act.get("block") or {}
                if not after_bid or not isinstance(new_block_raw, dict):
                    applied_actions.append({
                        "kind": "add_block_after", "ok": False,
                        "reason": "missing_after_block_id_or_block",
                    })
                    continue
                btype = new_block_raw.get("type") or "paragraph"
                # Generar block_id si el LLM no lo dio
                import uuid as _uuid_addb
                new_bid = new_block_raw.get("block_id") or f"blk_{_uuid_addb.uuid4().hex[:10]}"
                # Sanitizar block_data: quitar campos top-level que NO van en block_data
                bd = {k: v for k, v in new_block_raw.items() if k not in ("block_id",)}
                bd.setdefault("type", btype)
                inserted = await repo.insert_block_after(
                    document_id=document_id,
                    after_block_id=after_bid,
                    block_id=new_bid,
                    block_type=btype,
                    block_data=bd,
                    section_key=new_block_raw.get("section_key"),
                )
                if inserted is None:
                    applied_actions.append({
                        "kind": "add_block_after", "ok": False,
                        "after_block_id": after_bid,
                        "reason": "anchor_block_not_found_or_db_error",
                    })
                else:
                    blocks_changed += 1
                    applied_actions.append({
                        "kind": "add_block_after",
                        "after_block_id": after_bid,
                        "new_block_id": inserted["block_id"],
                        "block_type": inserted["block_type"],
                        "ok": True,
                    })
            elif kind == "info_only":
                applied_actions.append({"kind": "info_only"})
            elif kind == "propagate_change":
                # M19.18.B — rename global de old_text por new_text en todos los
                # bloques editables del documento.
                old_text = (act.get("old_text") or "").strip()
                new_text = (act.get("new_text") or "").strip()
                if not old_text or not new_text:
                    applied_actions.append({
                        "kind": "propagate_change", "ok": False,
                        "reason": "missing_old_or_new_text",
                    })
                    continue
                res = await _apply_rename_across_document(
                    storage.pool, document_id,
                    old_text=old_text, new_text=new_text,
                    case_insensitive=bool(act.get("case_insensitive", False)),
                    exclude_block_ids=act.get("exclude_block_ids") or [],
                )
                blocks_changed += res.get("blocks_modified", 0)
                applied_actions.append({
                    "kind": "propagate_change",
                    "old_text": old_text[:120],
                    "new_text": new_text[:120],
                    "blocks_modified": res.get("blocks_modified", 0),
                    "total_replacements": res.get("total_replacements", 0),
                    "ok": res.get("blocks_modified", 0) > 0,
                    "reason": res.get("error"),
                })
            elif kind == "clarify":
                # M19.17.B — el LLM pide aclaración antes de actuar. NO modifica el
                # documento; el frontend muestra la pregunta y re-envía con
                # pending_context en el siguiente turn.
                applied_actions.append({
                    "kind": "clarify",
                    "question": str(act.get("question", "¿Puedes dar más detalle?"))[:500],
                    "pending_context": act.get("pending_context") or {},
                })
            elif kind == "replace_selection":
                # M19.17.A — reemplazo quirúrgico de fragmento dentro de un bloque
                bid = act.get("block_id")
                original_text = act.get("original_text", "")
                replacement_text = act.get("replacement_text", "")
                anchor_before = act.get("anchor_before", "") or ""
                anchor_after = act.get("anchor_after", "") or ""
                if not bid or not original_text:
                    applied_actions.append({
                        "kind": "replace_selection", "ok": False,
                        "reason": "missing_block_id_or_original_text",
                    })
                    continue
                ok, err = await _replace_selection_in_block(
                    storage.pool, document_id, bid,
                    original_text, replacement_text,
                    anchor_before, anchor_after,
                )
                if ok:
                    blocks_changed += 1
                    applied_actions.append({
                        "kind": "replace_selection", "block_id": bid, "ok": True,
                    })
                else:
                    # Fallback: si el LLM también envió un new_runs intentamos update_block
                    fallback_runs = act.get("new_runs")
                    if isinstance(fallback_runs, list):
                        ok2 = await _update_block_runs(storage.pool, document_id, bid, fallback_runs)
                        if ok2:
                            blocks_changed += 1
                            applied_actions.append({
                                "kind": "replace_selection", "block_id": bid, "ok": True,
                                "fallback": "update_block", "reason": err,
                            })
                            continue
                    applied_actions.append({
                        "kind": "replace_selection", "block_id": bid, "ok": False,
                        "reason": err or "unknown",
                    })
        except Exception as e:
            logger.warning("apply action %s failed: %s", kind, e)
            applied_actions.append({"kind": kind, "ok": False, "error": str(e)[:120]})

    # M19.16.B3 — si hubo cambios, invalidar cache DOCX y crear snapshot version
    if blocks_changed > 0:
        try:
            from lex.storage.docx_storage import invalidate_cache
            await invalidate_cache(storage.pool, document_id)
        except Exception as e:
            logger.debug("chat: docx cache invalidate failed (non-fatal): %s", e)
        try:
            from lex.storage.versions_repo import VersionsRepo
            versions_repo = VersionsRepo(storage.pool)
            fresh_blocks = await repo.get_blocks_for_document(document_id)
            await versions_repo.create_version(
                document_id=document_id,
                change_type="chat_edit",
                blocks_snapshot=fresh_blocks,
                feedback=body.message[:200],
            )
        except Exception as e:
            logger.debug("chat: version snapshot failed (non-fatal): %s", e)

    return {
        "reply": reply,
        "actions": applied_actions,
        "blocks_changed": blocks_changed,
    }


async def _apply_rename_across_document(
    pool,
    document_id: str,
    old_text: str,
    new_text: str,
    case_insensitive: bool = False,
    exclude_block_ids: list[str] | None = None,
) -> dict[str, Any]:
    """M19.18.B — Reemplazo global de `old_text` por `new_text` en TODOS los
    bloques editables (paragraph/hecho/pretension/list_item) del documento.

    No toca bloques tipo norma_citada/jurisprudencia/table/firma para no romper
    metadata estructural. Si quieres tocar esos, hazlo con update_block puntual.

    Devuelve {blocks_modified: int, total_replacements: int, skipped: int}.
    """
    if not old_text or not new_text:
        return {"blocks_modified": 0, "total_replacements": 0, "skipped": 0, "error": "empty"}
    if old_text == new_text:
        return {"blocks_modified": 0, "total_replacements": 0, "skipped": 0, "error": "same"}
    exclude = set(exclude_block_ids or [])
    EDITABLE = ("paragraph", "hecho", "pretension", "list_item")
    blocks_modified = 0
    total_replacements = 0
    skipped = 0
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT block_id, block_type, block_data FROM document_blocks
                WHERE document_id = $1::uuid AND block_type = ANY($2::text[])
                ORDER BY block_order ASC
                """,
                document_id, list(EDITABLE),
            )
            for row in rows:
                bid = row["block_id"]
                if bid in exclude:
                    skipped += 1
                    continue
                bd = row["block_data"] if isinstance(row["block_data"], dict) \
                     else json.loads(row["block_data"])
                runs = bd.get("runs") or []
                if not isinstance(runs, list):
                    skipped += 1
                    continue
                modified_in_block = 0
                new_runs: list[dict] = []
                for r in runs:
                    if not isinstance(r, dict):
                        new_runs.append(r)
                        continue
                    rtxt = r.get("text", "") or ""
                    if case_insensitive:
                        # contar matches case-insensitive, reemplazar preservando case del original
                        # simplificado: solo case-sensitive si el LLM no lo pidió
                        count = rtxt.lower().count(old_text.lower())
                    else:
                        count = rtxt.count(old_text)
                    if count > 0:
                        if case_insensitive:
                            import re
                            rtxt_new = re.sub(re.escape(old_text), new_text, rtxt, flags=re.IGNORECASE)
                        else:
                            rtxt_new = rtxt.replace(old_text, new_text)
                        new_r = dict(r)
                        new_r["text"] = rtxt_new
                        new_runs.append(new_r)
                        modified_in_block += count
                    else:
                        new_runs.append(r)
                if modified_in_block > 0:
                    bd["runs"] = new_runs
                    await conn.execute(
                        """
                        UPDATE document_blocks
                        SET block_data = $1::jsonb
                        WHERE document_id = $2::uuid AND block_id = $3
                        """,
                        json.dumps(bd, ensure_ascii=False, default=str),
                        document_id, bid,
                    )
                    blocks_modified += 1
                    total_replacements += modified_in_block
        return {
            "blocks_modified": blocks_modified,
            "total_replacements": total_replacements,
            "skipped": skipped,
        }
    except Exception as e:
        logger.warning("apply_rename_across_document failed: %s", e)
        return {"blocks_modified": 0, "total_replacements": 0, "skipped": 0, "error": str(e)[:120]}


async def _replace_selection_in_block(
    pool,
    document_id: str,
    block_id: str,
    original_text: str,
    replacement_text: str,
    anchor_before: str = "",
    anchor_after: str = "",
) -> tuple[bool, str | None]:
    """M19.17.A — Reemplazo quirúrgico de un fragmento dentro de un bloque.

    Estrategia anti-ambiguous-match:
      1. Lee runs[] del bloque y los junta en texto plano (sin perder marks).
      2. Localiza el match usando `anchor_before + original_text + anchor_after`
         (los anchors son 3-6 palabras de contexto que el LLM debe enviar).
      3. Si hay 1 match único → reemplaza y vuelve a partir runs[] preservando
         los marks del entorno.
      4. Si hay 0 o >1 matches → fallback: NO modifica. Devuelve (False, motivo).

    Devuelve (ok, error_reason).
    """
    if not original_text.strip():
        return (False, "empty_selection")
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT block_type, block_data FROM document_blocks
                WHERE document_id = $1 AND block_id = $2
            """, uuid.UUID(document_id), block_id)
            if not row:
                return (False, "block_not_found")
            if row["block_type"] not in ("paragraph", "hecho", "pretension", "list_item"):
                return (False, "block_type_not_editable")
            bd = row["block_data"] if isinstance(row["block_data"], dict) else json.loads(row["block_data"])
            runs = bd.get("runs") or []
            if not isinstance(runs, list):
                return (False, "no_runs_array")

            # 1. Flatten a texto plano + map de offsets a (run_index, char_in_run, mark_signature)
            flat = ""
            offsets: list[tuple[int, int, dict]] = []  # (run_idx, char_idx, marks)
            for ri, r in enumerate(runs):
                rtxt = (r.get("text") or "") if isinstance(r, dict) else str(r)
                marks = {
                    "bold": bool(r.get("bold", False)) if isinstance(r, dict) else False,
                    "italic": bool(r.get("italic", False)) if isinstance(r, dict) else False,
                    "underline": bool(r.get("underline", False)) if isinstance(r, dict) else False,
                }
                for ci, ch in enumerate(rtxt):
                    offsets.append((ri, ci, marks))
                flat += rtxt

            # 2. Localizar match único con anchors
            # Solo usar anchors si al menos uno es no-vacío; sino, búsqueda directa
            ab = anchor_before or ""
            aa = anchor_after or ""
            has_anchor = bool(ab) or bool(aa)
            if has_anchor:
                needle = ab + original_text + aa
                if needle not in flat:
                    # Fallback: probar sin anchors si el LLM se equivocó
                    if original_text not in flat:
                        return (False, "selection_not_found")
                    if flat.count(original_text) > 1:
                        return (False, "ambiguous_match_anchor_invalid")
                    match_start = flat.index(original_text)
                    match_end = match_start + len(original_text)
                elif flat.count(needle) > 1:
                    return (False, "ambiguous_match_with_anchor")
                else:
                    anchor_offset = flat.index(needle)
                    match_start = anchor_offset + len(ab)
                    match_end = match_start + len(original_text)
            else:
                if original_text not in flat:
                    return (False, "selection_not_found")
                if flat.count(original_text) > 1:
                    return (False, "ambiguous_match_no_anchor")
                match_start = flat.index(original_text)
                match_end = match_start + len(original_text)

            # 3. Reconstruir runs[]: prefijo + replacement (con marks del primer char del match) + sufijo
            new_runs: list[dict] = []
            current_text = ""
            current_marks: dict | None = None

            def flush():
                nonlocal current_text, current_marks
                if current_text:
                    new_runs.append({
                        "text": current_text,
                        "bold": bool(current_marks.get("bold")) if current_marks else False,
                        "italic": bool(current_marks.get("italic")) if current_marks else False,
                        "underline": bool(current_marks.get("underline")) if current_marks else False,
                    })
                current_text = ""
                current_marks = None

            # Prefijo (antes del match)
            for i in range(match_start):
                _, _, marks = offsets[i]
                if current_marks is None:
                    current_marks = marks
                if marks != current_marks:
                    flush()
                    current_marks = marks
                current_text += flat[i]
            flush()

            # Reemplazo (hereda marks del primer char del match)
            if match_start < len(offsets):
                rep_marks = offsets[match_start][2]
            else:
                rep_marks = {"bold": False, "italic": False, "underline": False}
            if replacement_text:
                new_runs.append({
                    "text": replacement_text,
                    "bold": bool(rep_marks.get("bold")),
                    "italic": bool(rep_marks.get("italic")),
                    "underline": bool(rep_marks.get("underline")),
                })

            # Sufijo (después del match)
            for i in range(match_end, len(offsets)):
                _, _, marks = offsets[i]
                if current_marks is None:
                    current_marks = marks
                if marks != current_marks:
                    flush()
                    current_marks = marks
                current_text += flat[i]
            flush()

            if not new_runs:
                new_runs = [{"text": "", "bold": False, "italic": False, "underline": False}]

            bd["runs"] = new_runs
            await conn.execute("""
                UPDATE document_blocks
                SET block_data = $3::jsonb
                WHERE document_id = $1 AND block_id = $2
            """, uuid.UUID(document_id), block_id, json.dumps(bd, ensure_ascii=False, default=str))
        return (True, None)
    except Exception as e:
        logger.warning("replace_selection_in_block failed: %s", e)
        return (False, f"exception:{str(e)[:80]}")


async def _update_block_runs(pool, document_id: str, block_id: str, new_runs: list[dict]) -> bool:
    """Actualiza el block_data.runs de un bloque tipo paragraph/hecho/pretension."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT block_type, block_data FROM document_blocks
                WHERE document_id = $1 AND block_id = $2
            """, uuid.UUID(document_id), block_id)
            if not row:
                return False
            bd = row["block_data"] if isinstance(row["block_data"], dict) else json.loads(row["block_data"])
            # Solo actualizar bloques que tienen 'runs'
            if row["block_type"] not in ("paragraph", "hecho", "pretension", "list_item"):
                return False
            bd["runs"] = [
                {
                    "text": r.get("text", ""),
                    "bold": bool(r.get("bold", False)),
                    "italic": bool(r.get("italic", False)),
                    "underline": bool(r.get("underline", False)),
                }
                for r in new_runs if isinstance(r, dict)
            ]
            await conn.execute("""
                UPDATE document_blocks
                SET block_data = $3::jsonb
                WHERE document_id = $1 AND block_id = $2
            """, uuid.UUID(document_id), block_id, json.dumps(bd, ensure_ascii=False, default=str))
        return True
    except Exception as e:
        logger.warning("update_block_runs failed: %s", e)
        return False
