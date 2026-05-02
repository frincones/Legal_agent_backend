"""Voice API: ticket issuance + OpenAI Realtime WS relay.

Pipeline:
  Browser  →  POST /v1/voice/ticket  (auth: Supabase JWT)
            ←  { ticket, expires_at }
  Browser  →  WS /v1/voice/ws?ticket=...
            ↔  Railway (this module)
            ↔  WSS api.openai.com/v1/realtime?model=gpt-realtime
            ↑  OpenAI

Railway responsibilities:
  1. Verify ticket → load Principal (firm_id, user_id, role, matter_id?).
  2. Open upstream WebSocket to OpenAI Realtime with server API key.
  3. Send session.update with LexAI tools + voice + Mexican legal instructions.
  4. Forward audio in (browser → OpenAI) + audio out (OpenAI → browser).
  5. Intercept response.function_call_arguments.done events → execute tool
     locally against Postgres (citations, calc, matter context, HITL request).
  6. Send conversation.item.create + function_call_output back to OpenAI.
  7. Persist agent_sessions / agent_runs / agent_tool_calls / voice_sessions.
  8. Surface key events to the browser as JSON control frames so the
     Voice HUD + Live Canvas can react in real time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

import websockets
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from utils.auth import (
    Principal,
    get_current_user,
    issue_voice_ticket,
    verify_voice_ticket,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/voice", tags=["voice"])

# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "marin")
OPENAI_REALTIME_URL_TEMPLATE = "wss://api.openai.com/v1/realtime?model={model}"

# Tool execution registry (filled by main.py at startup or lazy import)
_tool_registry: dict[str, Callable[..., Awaitable[dict]]] = {}


def register_tool(name: str, fn: Callable[..., Awaitable[dict]]) -> None:
    """Register a server-side tool implementation. Called from main.py
    after dependencies (storage, embedder, source_router) are wired up."""
    _tool_registry[name] = fn


# ─────────────────────────────────────────────────────────────────────
# REST: ticket issuance
# ─────────────────────────────────────────────────────────────────────


@router.post("/ticket")
async def voice_ticket(
    matter_id: Optional[str] = None,
    principal: Principal = Depends(get_current_user),
):
    """Issue a short-lived HMAC ticket so the browser can open the WS.

    The ticket carries firm_id and user_id so the WS handshake doesn't
    need to round-trip Supabase again. TTL configured by env var.
    """
    return issue_voice_ticket(principal, matter_id=matter_id)


# ─────────────────────────────────────────────────────────────────────
# Session config sent to OpenAI on connect
# ─────────────────────────────────────────────────────────────────────

LEGAL_VOICE_INSTRUCTIONS = """Eres LexAI, asistente legal voice-first para abogados colombianos.

Hablas español de Colombia (formal, profesional, "usted"). Tu rol es ejecutar el trabajo
repetitivo de un paralegal con la urgencia de un secretario jurídico experto: investigación
jurisprudencial, drafting de demandas/tutelas, cálculo de liquidaciones laborales, gestión
procesal y agenda.

CAPACIDADES (TOOLS DISPONIBLES):

  Investigación · research_jurisprudence, validate_citation, validate_norm_vigencia
  Cálculo       · calc_liquidacion (CST + Ley 50/1990 + Ley 789/2002)
  Caso          · open_matter_context (carga partes/plazos/timeline del caso activo)
  Gestión       · list_my_matters, find_client, list_upcoming_deadlines,
                  add_matter_deadline, mark_deadline_done, add_matter_note,
                  list_matter_documents, summarize_document
  Aprobación    · list_pending_hitl, request_human_approval
  Métricas      · get_firm_metrics (mes/semana del despacho)
  Drafting      · draft_pleading (escribe a Live Canvas en streaming)

REGLAS ABSOLUTAS:

1. Tools first. Si el abogado pide algo que se resuelve llamando una tool, lláma la tool
   ANTES de responder. No improvises con conocimiento general. "¿Qué casos tengo?" →
   list_my_matters. "¿Qué vence esta semana?" → list_upcoming_deadlines. "Busca a
   Rodríguez" → find_client. "Agrega esta nota" → add_matter_note. "Liquida a este
   trabajador" → calc_liquidacion. Nunca digas "déjame revisar" sin invocar la tool.

2. Encadena tools cuando ayuda. Ejemplo: el abogado dicta "redacta tutela por estabilidad
   reforzada para mi cliente Rodríguez". Flujo correcto: find_client → open_matter_context
   → research_jurisprudence (estabilidad reforzada laboral) → draft_pleading. Reportas el
   resultado en una sola frase final.

3. Jurisprudencia verificada. SOLO citas sentencias (T-XXX/AAAA, C-XXX/AAAA, SU-XXX/AAAA,
   números de casación) que aparezcan en el output de `research_jurisprudence` o
   `validate_citation`. NUNCA inventes una sentencia. Si no hay resultados verificados,
   díselo: "No identifiqué jurisprudencia verificada para ese punto en las fuentes
   consultadas."

4. Cálculos numéricos. SIEMPRE usa `calc_liquidacion` u otras tools deterministas. NUNCA
   calcules de cabeza cesantías, intereses, prima, vacaciones o indemnización.

5. HITL gates. Antes de ejecutar acciones que afecten al mundo exterior (envío de email,
   firma digital, radicación en juzgado, pago > $50M COP, sobrescribir doc del cliente,
   escrito a juez/contraparte), llamas `request_human_approval` y esperas la decisión.
   No ejecutas hasta tener `approved`. Para gestión interna (notas, plazos en calendario,
   marcar completado, listas) NO requieres HITL.

6. Estilo voz: 1-3 oraciones por turno. Confirma con números/fechas concretas, no con
   "perfecto" abstracto. Ejemplos buenos:
     · "Encontré 8 casos activos. Tres altos: Rodríguez, Comcel y Constructora del Valle."
     · "Audiencia agendada el lunes 12 a las 10:00 en Juzgado 18 Civil. ¿Algo más?"
     · "Liquidación: 18.4 millones reclamables. Lo escribí en Canvas para que revise."
   El detalle largo (texto del escrito) va a `draft_pleading`, no a la voz.

7. Idioma colombiano. "tutela" (Art. 86 CP), "CST", "SMMLV", "Corte Constitucional",
   "Corte Suprema de Justicia" (Sala Laboral/Civil/Penal), "Consejo de Estado",
   "Honorable Magistrado", "Despacho judicial", "Juzgado XX Laboral del Circuito de
   Bogotá", "demanda ordinaria laboral" (no "demanda laboral por despido").

8. Habeas Data (Ley 1581/2012). Si el abogado dicta cédula, NIT o datos sensibles,
   confirma que el cliente firmó consentimiento informado.

9. UPL. Nunca digas "soy abogado", "garantizo el resultado", "su caso ganará". Eres
   asistente documental con IA. El abogado titulado con tarjeta profesional vigente
   revisa y firma todo antes de presentar.

10. Sé un facilitador real. Si el abogado dicta algo ambiguo, propone la mejor
    interpretación y lánzala con tools, no le hagas preguntas innecesarias. Solo
    pregunta cuando falta un dato indispensable (fecha exacta, NIT, monto)."""


def build_session_update(matter_id: Optional[str] = None) -> dict:
    """Build the initial session.update payload with tools and config.

    Tool catalog matches server-side `_tool_registry` keys.
    """
    instructions = LEGAL_VOICE_INSTRUCTIONS
    if matter_id:
        instructions += (
            f"\n\nContexto activo: matter_id={matter_id}. Carga partes/hechos/plazos "
            f"con `open_matter_context` antes del primer dictado si no lo has hecho."
        )

    return {
        "type": "session.update",
        "session": {
            "modalities": ["audio", "text"],
            "voice": OPENAI_REALTIME_VOICE,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "gpt-4o-transcribe"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "create_response": True,
            },
            "temperature": 0.7,
            "max_response_output_tokens": 4096,
            "instructions": instructions,
            "tools": _tool_descriptors(),
            "tool_choice": "auto",
        },
    }


def _tool_descriptors() -> list[dict]:
    """OpenAI Realtime function descriptors. Update when adding tools."""
    return [
        {
            "type": "function",
            "name": "research_jurisprudence",
            "description": (
                "Busca jurisprudencia colombiana (Corte Constitucional, Corte Suprema, "
                "Consejo de Estado) sobre un tema. Devuelve sentencias verificadas con "
                "número (T-XXX/AAAA, C-XXX/AAAA, SU-XXX/AAAA), rubro, vigencia y URL oficial."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "materia": {
                        "type": "string",
                        "enum": ["laboral", "civil", "comercial", "penal", "familiar",
                                 "administrativo", "constitucional", "fiscal", "seguridad_social"],
                    },
                    "corte": {
                        "type": "string",
                        "enum": ["CORTE_CONSTITUCIONAL", "CORTE_SUPREMA", "CONSEJO_ESTADO"],
                    },
                    "limit": {"type": "integer", "default": 6},
                },
                "required": ["query"],
            },
        },
        {
            "type": "function",
            "name": "validate_citation",
            "description": (
                "Verifica que una sentencia colombiana (T-XXX/AAAA, C-XXX/AAAA, etc.) "
                "exista, esté vigente y no haya sido modulada o sustituida. Devuelve "
                "estado: verificada | no_encontrada | superada | sospechosa."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "citation_ref": {
                        "type": "string",
                        "description": "Número de sentencia (ej: T-388/2019, C-200/1995, SU-449/2020) o número de casación.",
                    },
                    "corte": {"type": "string"},
                },
                "required": ["citation_ref"],
            },
        },
        {
            "type": "function",
            "name": "validate_norm_vigencia",
            "description": (
                "Verifica vigencia de una norma colombiana (Ley/Decreto/Resolución). "
                "Detecta si fue derogada, modificada o sustituida y devuelve la cadena "
                "de derogaciones. Usa el grafo de derogaciones existente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": ["LEY", "DECRETO", "RESOLUCION", "CODIGO", "CIRCULAR"]},
                    "numero": {"type": "integer"},
                    "anio": {"type": "integer"},
                },
                "required": ["tipo", "numero", "anio"],
            },
        },
        {
            "type": "function",
            "name": "calc_liquidacion",
            "description": (
                "Calcula liquidación laboral según CST + Ley 50/1990 + Ley 789/2002: "
                "cesantías, intereses sobre cesantías, prima de servicios, vacaciones e "
                "indemnización por despido sin justa causa (Art. 64 CST). Cálculo "
                "determinista en COP, cero alucinación numérica."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fecha_ingreso": {"type": "string", "format": "date"},
                    "fecha_terminacion": {"type": "string", "format": "date"},
                    "salario_mensual_cop": {"type": "number"},
                    "causa": {
                        "type": "string",
                        "enum": ["injustificado", "justa_causa", "renuncia", "mutuo_acuerdo", "terminacion_contrato", "fin_obra"],
                    },
                    "tipo_contrato": {
                        "type": "string",
                        "enum": ["indefinido", "fijo", "obra_labor", "aprendizaje"],
                        "default": "indefinido",
                    },
                    "salario_integral": {"type": "boolean", "default": False},
                    "trabajador_nombre": {"type": "string"},
                },
                "required": ["fecha_ingreso", "fecha_terminacion", "salario_mensual_cop", "causa"],
            },
        },
        {
            "type": "function",
            "name": "draft_pleading",
            "description": (
                "Genera o actualiza el documento procesal en el Live Canvas. "
                "Streamea el contenido al frontend para edición en vivo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "demanda_ordinaria_laboral",
                            "tutela",
                            "contestacion",
                            "recurso_apelacion",
                            "recurso_casacion",
                            "escrito",
                            "carta_requerimiento",
                            "contrato",
                        ],
                    },
                    "facts": {"type": "object"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["matter_id", "kind", "facts"],
            },
        },
        {
            "type": "function",
            "name": "request_human_approval",
            "description": (
                "Solicita confirmación humana antes de ejecutar acciones externas "
                "(email, firma digital, radicación a juzgado, pago, sobrescribir doc, "
                "escrito a contraparte). Devuelve approved | edited | rejected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "email_externo",
                            "firma_digital",
                            "cita_jurisprudencia",
                            "accion_financiera",
                            "sobrescribir",
                            "escrito_juzgado",
                            "dato_sensible_habeas_data",
                        ],
                    },
                    "preview": {"type": "object"},
                },
                "required": ["kind", "preview"],
            },
        },
        {
            "type": "function",
            "name": "open_matter_context",
            "description": (
                "Carga partes, hechos, plazos y documentos del caso activo "
                "para enriquecer el contexto del agente."
            ),
            "parameters": {
                "type": "object",
                "properties": {"matter_id": {"type": "string"}},
                "required": ["matter_id"],
            },
        },
        # ─── Paralegal-grade extensions ────────────────────────────────
        {
            "type": "function",
            "name": "list_my_matters",
            "description": (
                "Lista los casos activos del despacho del usuario, ordenados por "
                "prioridad y siguiente fecha. Filtros opcionales por materia o priority."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "materia": {"type": "string", "enum": ["laboral", "civil", "comercial", "penal", "familiar", "administrativo", "constitucional", "fiscal"]},
                    "priority": {"type": "string", "enum": ["alta", "media", "baja"]},
                    "limit": {"type": "integer", "default": 10, "maximum": 25},
                },
            },
        },
        {
            "type": "function",
            "name": "find_client",
            "description": (
                "Busca clientes del despacho por nombre parcial, NIT o cédula. "
                "Acento-insensible. Devuelve hasta 6 coincidencias con cantidad de casos activos."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "nombre, NIT o cédula"}},
                "required": ["query"],
            },
        },
        {
            "type": "function",
            "name": "list_upcoming_deadlines",
            "description": (
                "Lista los plazos pendientes (audiencias, vencimientos, contestaciones) "
                "del despacho dentro de una ventana de N días. Filtro opcional por matter_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 7, "maximum": 60},
                    "matter_id": {"type": "string"},
                },
            },
        },
        {
            "type": "function",
            "name": "add_matter_deadline",
            "description": (
                "Agenda un nuevo plazo o audiencia en el calendario del caso. "
                "fecha en ISO 8601 (ej: '2026-05-20T10:00:00-05:00')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string"},
                    "titulo": {"type": "string"},
                    "fecha": {"type": "string", "description": "ISO 8601 con zona horaria"},
                    "tipo": {"type": "string", "enum": ["audiencia", "vencimiento", "contestacion", "termino", "otro"]},
                },
                "required": ["matter_id", "titulo", "fecha"],
            },
        },
        {
            "type": "function",
            "name": "mark_deadline_done",
            "description": "Marca un plazo del calendario como completado.",
            "parameters": {
                "type": "object",
                "properties": {"deadline_id": {"type": "string"}},
                "required": ["deadline_id"],
            },
        },
        {
            "type": "function",
            "name": "add_matter_note",
            "description": (
                "Agrega una nota dictada por voz al expediente. La nota queda visible "
                "en la pestaña Notas del caso."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["matter_id", "body"],
            },
        },
        {
            "type": "function",
            "name": "list_matter_documents",
            "description": "Lista los documentos del expediente con su estado de OCR/verificación y un preview del resumen IA.",
            "parameters": {
                "type": "object",
                "properties": {"matter_id": {"type": "string"}},
                "required": ["matter_id"],
            },
        },
        {
            "type": "function",
            "name": "summarize_document",
            "description": (
                "Devuelve el resumen IA cacheado de un documento. Si aún no está, "
                "indica que el OCR está en proceso."
            ),
            "parameters": {
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
            },
        },
        {
            "type": "function",
            "name": "list_pending_hitl",
            "description": "Lista las acciones pendientes de aprobación humana (HITL) del despacho.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "get_firm_metrics",
            "description": (
                "Devuelve métricas del despacho: documentos verificados este mes, "
                "voice commands semana/mes, horas ahorradas, casos activos, plazos próximos."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    ]


# ─────────────────────────────────────────────────────────────────────
# WebSocket relay
# ─────────────────────────────────────────────────────────────────────


@router.websocket("/ws")
async def voice_relay(
    websocket: WebSocket,
    ticket: str = Query(..., description="Short-lived HMAC ticket from /v1/voice/ticket"),
):
    """OpenAI Realtime WebSocket relay.

    Browser ↔ Railway: control frames (JSON) + binary PCM16 24kHz mono.
    Railway ↔ OpenAI: control frames (JSON, base64-encoded audio in deltas).

    The relay is mostly transparent for audio. Function-call events are
    intercepted, executed locally, and the result is fed back to OpenAI
    so the model can continue speaking.
    """
    await websocket.accept()

    # 1) Verify ticket
    try:
        payload = verify_voice_ticket(ticket)
    except HTTPException as e:
        await websocket.send_json({"type": "error", "code": 401, "message": e.detail})
        await websocket.close(code=4401)
        return

    if not OPENAI_API_KEY:
        await websocket.send_json({"type": "error", "code": 500, "message": "OPENAI_API_KEY missing"})
        await websocket.close(code=1011)
        return

    firm_id = payload.get("firm_id")
    user_id = payload.get("sub")
    matter_id = payload.get("matter_id")
    session_db_id = str(uuid.uuid4())

    # 2) Open upstream WebSocket to OpenAI Realtime
    upstream_url = OPENAI_REALTIME_URL_TEMPLATE.format(model=OPENAI_REALTIME_MODEL)
    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1"),
    ]

    started_at = time.time()
    voice_metrics = {
        "user_id": user_id,
        "firm_id": firm_id,
        "matter_id": matter_id,
        "started_at": started_at,
        "tool_calls": 0,
        "bargeins": 0,
    }

    try:
        async with websockets.connect(
            upstream_url,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
        ) as upstream:
            logger.info(
                "Voice relay open: firm=%s user=%s matter=%s session=%s",
                firm_id, user_id, matter_id, session_db_id,
            )

            # 3) Push initial session.update
            await upstream.send(json.dumps(build_session_update(matter_id=matter_id)))
            await websocket.send_json({"type": "session.ready", "session_id": session_db_id})

            # 4) Pump messages bidirectionally
            principal_ctx = {
                "firm_id": firm_id,
                "user_id": user_id,
                "matter_id": matter_id,
                "session_id": session_db_id,
            }

            client_to_upstream = asyncio.create_task(
                _pump_client_to_upstream(websocket, upstream, voice_metrics)
            )
            upstream_to_client = asyncio.create_task(
                _pump_upstream_to_client(upstream, websocket, principal_ctx, voice_metrics)
            )

            done, pending = await asyncio.wait(
                {client_to_upstream, upstream_to_client},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception():
                    logger.warning("voice pump finished with: %s", t.exception())

    except websockets.WebSocketException as e:
        logger.warning("OpenAI WS error: %s", e)
        try:
            await websocket.send_json({"type": "error", "code": 502, "message": "upstream WS error"})
            await websocket.close(code=1011)
        except Exception:
            pass
    except WebSocketDisconnect:
        logger.info("client disconnected")
    except Exception as e:
        logger.exception("voice relay crashed: %s", e)
        try:
            await websocket.send_json({"type": "error", "code": 500, "message": str(e)})
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        # 5) Persist voice_session row (best-effort, non-blocking failure)
        try:
            await _persist_voice_session(session_db_id, voice_metrics)
        except Exception as e:
            logger.debug("could not persist voice_session: %s", e)


# ─────────────────────────────────────────────────────────────────────
# Pumps
# ─────────────────────────────────────────────────────────────────────


async def _pump_client_to_upstream(
    websocket: WebSocket,
    upstream: websockets.ClientConnection,
    metrics: dict,
) -> None:
    """Browser → OpenAI.

    Browser sends JSON control frames + binary PCM frames.
    OpenAI expects JSON only, so we wrap binary as input_audio_buffer.append
    with base64-encoded payload.
    """
    import base64

    while True:
        msg = await websocket.receive()
        if msg["type"] == "websocket.disconnect":
            return

        if "bytes" in msg and msg["bytes"] is not None:
            audio_b64 = base64.b64encode(msg["bytes"]).decode()
            await upstream.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": audio_b64,
            }))
        elif "text" in msg and msg["text"]:
            try:
                obj = json.loads(msg["text"])
            except json.JSONDecodeError:
                continue

            mtype = obj.get("type")
            if mtype == "client.ping":
                await websocket.send_json({"type": "client.pong"})
                continue
            if mtype == "input_audio_buffer.commit":
                metrics["bargeins"] += 1 if obj.get("reason") == "bargein" else 0
            # Forward the control frame as-is to OpenAI
            await upstream.send(msg["text"])


async def _pump_upstream_to_client(
    upstream: websockets.ClientConnection,
    websocket: WebSocket,
    ctx: dict,
    metrics: dict,
) -> None:
    """OpenAI → Browser.

    For every message:
      - audio deltas → forwarded as binary frames after b64-decode
      - function_call_arguments.done → execute tool locally, send result back
      - other events → forwarded as JSON for the HUD/Canvas to react
    """
    import base64

    async for raw in upstream:
        # OpenAI Realtime always sends text JSON; binary path stays for safety.
        if isinstance(raw, bytes):
            await websocket.send_bytes(raw)
            continue

        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue

        etype = evt.get("type", "")

        # 1) Audio out → binary to browser
        if etype == "response.audio.delta":
            audio_b64 = evt.get("delta") or evt.get("audio") or ""
            if audio_b64:
                try:
                    await websocket.send_bytes(base64.b64decode(audio_b64))
                except Exception as e:
                    logger.debug("audio.delta decode/send failed: %s", e)
            continue

        # 2) Function call → execute server-side
        if etype == "response.function_call_arguments.done":
            metrics["tool_calls"] += 1
            await _handle_function_call(evt, upstream, websocket, ctx)
            continue

        # 3) Lightweight event mapping for the browser HUD
        client_evt = _translate_event_for_client(etype, evt)
        if client_evt is not None:
            try:
                await websocket.send_json(client_evt)
            except Exception:
                pass


def _translate_event_for_client(etype: str, evt: dict) -> Optional[dict]:
    """Map OpenAI Realtime event names to a slim client schema.

    Returning None drops the event silently; passes the high-signal ones
    to the Voice HUD / Live Canvas.
    """
    if etype == "input_audio_buffer.speech_started":
        return {"type": "vad.user_started"}
    if etype == "input_audio_buffer.speech_stopped":
        return {"type": "vad.user_stopped"}
    if etype == "conversation.item.input_audio_transcription.delta":
        return {"type": "transcript.partial", "text": evt.get("delta", "")}
    if etype == "conversation.item.input_audio_transcription.completed":
        return {"type": "transcript.final", "text": evt.get("transcript", "")}
    if etype == "response.audio_transcript.delta":
        return {"type": "answer.text.delta", "text": evt.get("delta", "")}
    if etype == "response.audio_transcript.done":
        return {"type": "answer.text.done"}
    if etype == "response.audio.done":
        return {"type": "speak.end"}
    if etype == "response.created":
        return {"type": "speak.start"}
    if etype == "response.cancelled":
        return {"type": "speak.cancelled"}
    if etype == "response.done":
        usage = evt.get("response", {}).get("usage")
        return {"type": "response.done", "usage": usage}
    if etype == "error":
        err = evt.get("error", {})
        return {"type": "error", "code": err.get("code"), "message": err.get("message", "")}
    # session.created, session.updated, rate_limits.updated → drop
    return None


# ─────────────────────────────────────────────────────────────────────
# Tool execution
# ─────────────────────────────────────────────────────────────────────


async def _handle_function_call(
    evt: dict,
    upstream: websockets.ClientConnection,
    websocket: WebSocket,
    ctx: dict,
) -> None:
    """Execute a tool locally and feed the result back into the conversation."""
    name = evt.get("name", "")
    call_id = evt.get("call_id", "")
    args_str = evt.get("arguments", "{}")
    try:
        args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
    except json.JSONDecodeError:
        args = {}

    started = time.time()
    await websocket.send_json({
        "type": "tool.started",
        "id": call_id,
        "name": name,
        "preview": {k: v for k, v in args.items() if k not in ("facts",)},
    })

    fn = _tool_registry.get(name)
    if fn is None:
        result = {"error": f"tool '{name}' not implemented"}
    else:
        try:
            result = await fn(args=args, ctx=ctx)
        except Exception as e:
            logger.exception("tool '%s' raised: %s", name, e)
            result = {"error": str(e)}

    duration_ms = int((time.time() - started) * 1000)

    await websocket.send_json({
        "type": "tool.finished",
        "id": call_id,
        "name": name,
        "ms": duration_ms,
        "output_preview": _preview(result),
    })

    # Special HITL flow: if the tool blocks pending human decision,
    # the registered impl returns {pending: true, interrupt_id}.
    # The frontend resolves it via /v1/hitl/{id}/decide; we wait and
    # then send the resolution back into the response.
    if name == "request_human_approval" and result.get("pending"):
        interrupt_id = result["interrupt_id"]
        await websocket.send_json({
            "type": "hitl.requested",
            "interrupt_id": interrupt_id,
            "kind": args.get("kind"),
            "preview": args.get("preview"),
        })
        # Wait for the decision via a registered resolver (api/hitl.py).
        try:
            from api.hitl import wait_for_decision
            decision = await wait_for_decision(interrupt_id, timeout_s=120.0)
            result = {"decision": decision.get("decision"), "payload": decision.get("decision_payload")}
        except Exception as e:
            logger.warning("HITL wait failed: %s", e)
            result = {"decision": "timeout", "error": str(e)}
        await websocket.send_json({"type": "hitl.resolved", "interrupt_id": interrupt_id, "decision": result.get("decision")})

    # Push function_call_output back to OpenAI so the model continues.
    await upstream.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result, ensure_ascii=False, default=str),
        },
    }))
    # Ask the model to continue speaking with the new info.
    await upstream.send(json.dumps({"type": "response.create"}))


def _preview(obj: Any, max_chars: int = 220) -> Any:
    """Trim large values for the client HUD."""
    try:
        if isinstance(obj, dict):
            out = {}
            for k, v in list(obj.items())[:8]:
                if isinstance(v, str) and len(v) > max_chars:
                    out[k] = v[:max_chars] + "…"
                else:
                    out[k] = v
            return out
        if isinstance(obj, str) and len(obj) > max_chars:
            return obj[:max_chars] + "…"
        return obj
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────


async def _persist_voice_session(session_id: str, metrics: dict) -> None:
    """Write a row to voice_sessions for telemetry."""
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            return
        ended = time.time()
        duration_ms = int((ended - metrics["started_at"]) * 1000)
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                insert into voice_sessions
                  (id, firm_id, user_id, duration_ms, bargeins, started_at, ended_at, metadata)
                values
                  ($1::uuid, $2::uuid, $3::uuid, $4, $5, to_timestamp($6), to_timestamp($7), $8::jsonb)
                """,
                session_id,
                metrics.get("firm_id"),
                metrics.get("user_id"),
                duration_ms,
                metrics.get("bargeins", 0),
                metrics["started_at"],
                ended,
                json.dumps({
                    "matter_id": metrics.get("matter_id"),
                    "tool_calls": metrics.get("tool_calls", 0),
                }),
            )
    except Exception as e:
        logger.debug("persist voice_session failed: %s", e)
