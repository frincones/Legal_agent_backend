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


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list)


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
      {"kind": "update_block", "block_id": "<id>", "new_runs": [{"text": "...", "bold": false}]},
      {"kind": "regenerate_section", "section_key": "hechos|pretensiones|..."},
      {"kind": "add_block_after", "after_block_id": "<id>", "block": {...}},
      {"kind": "info_only"}
    ]
  }

REGLAS:
- Si el usuario da datos (nombre, cédula, fecha, etc), busca los bloques con placeholders [PLACEHOLDER]
  y emite "update_block" reemplazando esos placeholders con los datos reales.
- Si pide regenerar una sección entera (ej. "regenera los hechos"), usa "regenerate_section".
- Si pregunta algo sin cambiar el doc, usa "info_only".
- NO inventes block_ids — usa solo los que aparecen en el contexto.
- Sé conciso en "reply" (máximo 200 chars).

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

    user_prompt = f"""DOCUMENTO ACTUAL (bloques con block_id):
{blocks_summary}

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
        logger.exception("chat LLM failed")
        return {
            "reply": f"Error procesando: {str(e)[:120]}",
            "actions": [],
            "blocks_changed": 0,
        }

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
                # Simplificado: solo agregar al final si no encontramos after_block_id
                # TODO M10: insertar en posición específica
                applied_actions.append({"kind": "add_block_after", "ok": False, "reason": "pending_m10"})
            elif kind == "info_only":
                applied_actions.append({"kind": "info_only"})
        except Exception as e:
            logger.warning("apply action %s failed: %s", kind, e)
            applied_actions.append({"kind": kind, "ok": False, "error": str(e)[:120]})

    return {
        "reply": reply,
        "actions": applied_actions,
        "blocks_changed": blocks_changed,
    }


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
