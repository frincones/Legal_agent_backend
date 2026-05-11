"""Sprint 5 · parse_legal_email · LLM tool.

Recibe un correo (subject + body_text) y devuelve clasificación legal:
  - is_legal: bool
  - legal_kind: 'auto'|'sentencia'|'citacion'|'requerimiento'|'traslado'|'otro'
  - matched_expediente: str | None
  - matched_juzgado: str | None
  - matched_fecha: 'YYYY-MM-DD' | None
  - severidad: 'info'|'alta'|'critica'
  - parsed_summary: str (3-5 líneas)
  - matter_hint: str | None  (cliente, número de proceso, etc.)

Persistencia:
  - Si se llama con `email_message_id`, actualiza el row en email_messages.
  - Si solo se quiere un parse one-shot, devuelve el JSON sin escribir.

Llamada típica del worker de polling Gmail/Outlook (Sprint 6) o desde el agente
de voz: "LexAI, clasifica este correo".
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


_EXPEDIENTE_RE = re.compile(r"\b\d{2}[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{3}[-\s]?\d{4}[-\s]?\d{5}[-\s]?\d{2}\b")
_FECHA_ISO_RE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")

_SYSTEM_PROMPT = """Eres un paralegal jurídico colombiano experto en correo legal.
Clasificas correos electrónicos enviados a un abogado para detectar:
  - notificaciones judiciales (autos, sentencias, citaciones, traslados)
  - requerimientos administrativos (DIAN, Supersalud, Mintrabajo, etc.)
  - oficios de juzgados o entidades

Devuelve estricto JSON con esta estructura:

{
  "is_legal": true/false,
  "legal_kind": "auto|sentencia|citacion|requerimiento|traslado|oficio|otro",
  "matched_expediente": "string o null",
  "matched_juzgado": "string o null",
  "matched_fecha": "YYYY-MM-DD o null",
  "severidad": "info|alta|critica",
  "parsed_summary": "3-5 líneas resumen accionable",
  "matter_hint": "string o null (parte, NIT, número de proceso)",
  "confidence": 0.0-1.0
}

REGLAS:
- "is_legal": true SOLO si el correo proviene/menciona un órgano judicial,
  contiene un número de radicación, o describe una actuación procesal.
- "severidad" 'critica' si menciona sentencia, audiencia inminente, o plazo
  perentorio < 5 días.
- "severidad" 'alta' si requiere acción del abogado en < 30 días.
- "severidad" 'info' lo demás.
- Si NO encuentras un dato, devuelve null. NO inventes.
- "matched_expediente" debe ser el número de 23 dígitos del CGP cuando exista.
"""


async def parse_legal_email_tool(args: dict, ctx: dict) -> dict:
    """Voice / agent tool entrypoint.

    args:
      subject: str
      body: str (body_text del correo)
      from_address: str
      email_message_id: str | None  (para persistir resultado)
    """
    subject = (args.get("subject") or "").strip()[:500]
    body = (args.get("body") or "").strip()[:8000]
    from_address = (args.get("from_address") or "").strip()[:300]
    email_message_id = args.get("email_message_id")

    if not (subject or body):
        return {"error": "subject o body requerido"}

    # 1. Heurística rápida — si encontramos un radicado, aumenta confianza
    expediente_hint = None
    m = _EXPEDIENTE_RE.search(f"{subject} {body}")
    if m:
        expediente_hint = m.group(0)

    # 2. LLM clasifica
    try:
        from utils.llm import get_openai_client
        client = get_openai_client()
        user_prompt = (
            f"FROM: {from_address}\n"
            f"SUBJECT: {subject}\n\n"
            f"BODY:\n{body}\n\n"
            f"HINT: expediente_detectado_regex={expediente_hint or 'ninguno'}"
        )
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=600,
        )
        result = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        logger.warning("parse_legal_email LLM failed: %s", e)
        # Fallback heurístico simple
        result = {
            "is_legal": bool(expediente_hint) or "juzgado" in (subject + body).lower(),
            "legal_kind": "otro",
            "matched_expediente": expediente_hint,
            "matched_juzgado": None,
            "matched_fecha": None,
            "severidad": "info",
            "parsed_summary": (subject[:200] or "Correo sin clasificar"),
            "matter_hint": None,
            "confidence": 0.3,
        }

    # 3. Persistir si nos pasaron message_id
    if email_message_id:
        try:
            from utils.db import get_storage
            storage = await get_storage()
            if hasattr(storage, "pool"):
                async with storage.pool.acquire() as conn:
                    await conn.execute(
                        """
                        update email_messages set
                          is_legal = $2, legal_kind = $3,
                          matched_expediente = $4, matched_juzgado = $5,
                          matched_fecha = case when $6::text ~ '^\\d{4}-\\d{2}-\\d{2}$'
                                            then $6::date else null end,
                          severidad = $7, parsed_summary = $8,
                          parser_metadata = $9::jsonb, parser_version = 'v1'
                         where id = $1::uuid
                        """,
                        email_message_id,
                        bool(result.get("is_legal")),
                        result.get("legal_kind"),
                        result.get("matched_expediente"),
                        result.get("matched_juzgado"),
                        result.get("matched_fecha"),
                        result.get("severidad", "info"),
                        result.get("parsed_summary"),
                        json.dumps({
                            "matter_hint": result.get("matter_hint"),
                            "confidence": result.get("confidence"),
                            "from": from_address,
                        }),
                    )
        except Exception as e:
            logger.warning("parse_legal_email persist failed: %s", e)

    return result
