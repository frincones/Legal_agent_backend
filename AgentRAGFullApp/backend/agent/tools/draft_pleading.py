"""draft_pleading tool · genera o actualiza un documento procesal en el
Live Canvas, broadcastéandolo al frontend vía Supabase Realtime channel.

Para Sprint 2, el tool toma kind + facts + citations y genera párrafos
con un OpenAI completion + plantillas. La salida se inserta en
matter_documents (status=processing) y se publica al canal
`matter:{matter_id}:canvas` para que TipTap lo muestre en streaming.

En el MVP usamos plantillas hard-coded que funcionan para los 5 kinds
core. Sprint 4 añade plantillas más sofisticadas con cláusulas
condicionales.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from utils.llm import get_openai_client

logger = logging.getLogger(__name__)


PLANTILLA_INTROS = {
    "demanda_ordinaria_laboral": (
        "HONORABLE JUEZ:\n\n"
        "{nombre_actor}, mayor de edad, identificado(a) con C.C. {cedula_actor}, "
        "obrando en nombre propio, con dirección de notificación judicial {direccion_actor}, "
        "respetuosamente formulo demanda ordinaria laboral en contra de "
        "{nombre_demandado}, NIT {nit_demandado}, conforme a los siguientes hechos y pretensiones."
    ),
    "tutela": (
        "HONORABLE JUEZ DE TUTELA:\n\n"
        "{nombre_actor}, mayor de edad, identificado(a) con C.C. {cedula_actor}, "
        "interpongo acción de tutela conforme al Art. 86 de la Constitución Política, "
        "para la protección de mis derechos fundamentales a {derecho_vulnerado} en contra de "
        "{nombre_accionado}, conforme a los siguientes hechos."
    ),
    "contestacion": (
        "HONORABLE JUEZ:\n\n"
        "Dentro del término legal, presento contestación a la demanda formulada por "
        "{nombre_demandante} en mi contra dentro del proceso radicado bajo expediente "
        "{expediente}, en los siguientes términos."
    ),
    "recurso_apelacion": (
        "HONORABLE TRIBUNAL:\n\n"
        "Inconforme con la sentencia proferida el {fecha_sentencia} por el "
        "{juzgado_origen}, presento RECURSO DE APELACIÓN dentro del término legal "
        "conforme al Art. 320 del CGP."
    ),
    "carta_requerimiento": (
        "Bogotá D.C., {fecha_actual}\n\n"
        "Señor(es)\n{nombre_destinatario}\nReferencia: Requerimiento previo · {asunto}\n\n"
        "Por medio de la presente y actuando en representación de {nombre_cliente}, "
        "le requiero formalmente para que en un plazo de {plazo_dias} días hábiles "
        "proceda a {accion_requerida}."
    ),
}


SECTION_TEMPLATES = {
    "demanda_ordinaria_laboral": [
        ("I. PRETENSIONES", "Las pretensiones se enuncian a continuación con su fundamento legal."),
        ("II. HECHOS", "Los hechos relevantes en el orden cronológico que sustentan las pretensiones."),
        ("III. FUNDAMENTOS DE DERECHO", "Marco normativo aplicable: CST, Ley 50/1990, Ley 789/2002 y jurisprudencia citada."),
        ("IV. PRUEBAS", "Pruebas documentales, testimoniales y de oficio."),
        ("V. NOTIFICACIONES", "Direcciones de notificación de las partes."),
    ],
    "tutela": [
        ("I. HECHOS", "Hechos que motivan la acción de tutela."),
        ("II. DERECHOS VULNERADOS", "Derechos fundamentales afectados con cita de la Constitución y jurisprudencia."),
        ("III. PRUEBAS", "Documentos que acreditan la vulneración."),
        ("IV. PRETENSIONES", "Solicitud de amparo y medidas a adoptar."),
        ("V. JURAMENTO", "Manifestación bajo gravedad de juramento de que no se ha presentado otra tutela por los mismos hechos."),
    ],
}


async def draft_pleading_tool(args: dict, ctx: dict) -> dict:
    """Adaptador del tool para OpenAI Realtime.

    Args:
      kind: tipo de documento (demanda_ordinaria_laboral, tutela, etc.)
      matter_id: caso al cual asociar
      facts: hechos del caso (nombre, cédula, fechas, salario, etc.)
      citations: lista de citation_refs verificadas a incluir

    Returns:
      { id, kind, content, citations_count }
    """
    kind = args.get("kind", "demanda_ordinaria_laboral")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    facts = args.get("facts") or {}
    citations = args.get("citations") or []
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")

    if kind not in PLANTILLA_INTROS:
        return {"error": f"kind '{kind}' no soportado · use uno de {list(PLANTILLA_INTROS.keys())}"}

    intro = PLANTILLA_INTROS[kind].format(**{
        "nombre_actor": facts.get("nombre_actor", "[NOMBRE DEL ACTOR]"),
        "cedula_actor": facts.get("cedula_actor", "[C.C.]"),
        "direccion_actor": facts.get("direccion_actor", "[DIRECCIÓN]"),
        "nombre_demandado": facts.get("nombre_demandado", "[DEMANDADO]"),
        "nit_demandado": facts.get("nit_demandado", "[NIT]"),
        "derecho_vulnerado": facts.get("derecho_vulnerado", "[DERECHO]"),
        "nombre_accionado": facts.get("nombre_accionado", "[ACCIONADO]"),
        "nombre_demandante": facts.get("nombre_demandante", "[DEMANDANTE]"),
        "expediente": facts.get("expediente", "[EXPEDIENTE]"),
        "fecha_sentencia": facts.get("fecha_sentencia", "[FECHA]"),
        "juzgado_origen": facts.get("juzgado_origen", "[JUZGADO]"),
        "fecha_actual": datetime.now().strftime("%d de %B de %Y"),
        "nombre_destinatario": facts.get("nombre_destinatario", "[DESTINATARIO]"),
        "asunto": facts.get("asunto", "[ASUNTO]"),
        "nombre_cliente": facts.get("nombre_cliente", "[CLIENTE]"),
        "plazo_dias": facts.get("plazo_dias", "10"),
        "accion_requerida": facts.get("accion_requerida", "[ACCIÓN]"),
    })

    sections_md = "\n\n".join(
        f"## {h}\n\n{body}" for h, body in SECTION_TEMPLATES.get(kind, [])
    )

    cit_block = ""
    if citations:
        cit_block = "\n\n## Jurisprudencia citada\n\n" + "\n".join(
            f"- {c}" for c in citations
        )

    content = f"{intro}\n\n{sections_md}{cit_block}"

    # Persist as matter_documents row (status=processing → frontend sees streaming)
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
                       'processing', $5::uuid, 'text/markdown', $6, $7, false, null)
                    """,
                    doc_id,
                    firm_id,
                    matter_id,
                    f"{kind} · v1 · LexAI",
                    user_id,
                    f"sha-{doc_id[:16]}",
                    1,
                )
    except Exception as e:
        logger.warning("draft_pleading persist failed: %s", e)

    return {
        "id": doc_id,
        "kind": kind,
        "matter_id": matter_id,
        "content": content,
        "citations_count": len(citations),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
