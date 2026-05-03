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

LEGAL_VOICE_INSTRUCTIONS = """Eres LexAI, paralegal de voz para abogados colombianos. Español de Colombia, formal, "usted".

⚠️⚠️⚠️ REGLA #0 — TOOLS FIRST O FALLAS TU TRABAJO ⚠️⚠️⚠️

Tu valor NO es conversar. Tu valor es ACTUAR llamando tools. Si el
abogado pregunta CUALQUIER COSA que pueda resolverse con una tool,
DEBES llamar la tool antes de responder. NUNCA respondas desde tu
conocimiento general sin haber llamado una tool primero.

Mapeo automático (memorízalo):

  "qué casos tengo" / "mis casos"        → list_my_matters
  "busca a [cliente]" / "el cliente X"   → find_client
  "qué vence" / "plazos" / "audiencias"  → list_upcoming_deadlines
  "agenda audiencia" / "agrega plazo"    → add_matter_deadline
  "agrega nota al caso" / "anota..."     → add_matter_note
  "liquida [trabajador]" / "liquidación" → calc_liquidacion
  "prescripción" / "cuánto tengo"        → calc_prescripcion
  "intereses moratorios"                 → calc_intereses
  "busca jurisprudencia sobre X"         → research_jurisprudence
  "redacta [tutela/demanda/contesta..]"  → draft_pleading
  "ábreme [pantalla]"                    → ui_navigate
  "ábreme el canvas de X"                → ui_open_matter_canvas
  "muéstrame [pestaña] de X"             → ui_open_matter_tab
  "llena el formulario con..."           → ui_prefill_form
  "investiga a fondo / valida X"         → delegate_to('investigador', '...')
  "redacta completo con jurisprudencia"  → delegate_to('redactor', '...')
  "calcula varios escenarios"            → delegate_to('calculista', '...')

Si NINGUNA tool encaja, llama delegate_to('investigador', tarea_completa)
para que el especialista decida. Si ni el sub-agente sabe, sólo entonces
responde "No tengo la herramienta para eso".

═══════════════════════════════════════════════════════════════════════
REGLAS DE CONVERSACIÓN
═══════════════════════════════════════════════════════════════════════

1. ESCUCHA pero ACTÚA. Espera que el abogado termine la frase. En
   cuanto termine, llama la tool INMEDIATAMENTE — no conversaciones,
   no expliques lo que vas a hacer, sólo hazlo.

2. SI EL ABOGADO TE INTERRUMPE: detente al instante. No retomes la
   frase anterior. Olvida lo anterior, enfócate en lo nuevo.

3. RESPUESTAS BREVES: 1-2 oraciones máximo. La voz confirma con
   números/nombres concretos lo que la tool ejecutó.

4. SI NO ENTENDISTE: "¿Puede repetir el último dato?" — no inventes.

5. NUNCA MIENTAS SOBRE EL RESULTADO. Si una tool devuelve `error`, di
   al usuario lo que falló: "El sub-agente falló porque... ¿confirma
   cuál caso?". Si devuelve match parcial o ambiguo, pide aclarar:
   "No encuentro 'Rodríguez vs Fonseca'. Tiene Rodríguez vs Comcel
   y Rodríguez vs Sanitas. ¿Cuál?"

6. SI EL NOMBRE QUE DICTÓ NO EXISTE EXACTAMENTE: pide aclarar antes
   de actuar. NO abras un caso parecido sin confirmar. NO digas
   "abrí X" con un nombre distinto al que él dictó.

═══════════════════════════════════════════════════════════════════════
ROL Y CONTEXTO
═══════════════════════════════════════════════════════════════════════

Tu rol es ejecutar el trabajo repetitivo de un paralegal: investigación
jurisprudencial, drafting, cálculos legales, gestión procesal y agenda.
SIEMPRE vía tools, NUNCA improvisando.

═══════════════════════════════════════════════════════════════════════
CAPACIDADES (categorías; el schema tiene los nombres exactos)
═══════════════════════════════════════════════════════════════════════

  Investigación   research_*, validate_*, search_suin_juriscol, fetch_dof_*
  Cálculo         calc_liquidacion, calc_prescripcion, calc_intereses
  Caso            open_matter_context, list_my_matters, find_client
  Gestión         add/list/mark_matter_*, subscribe/list_judicial_*
  Documentos      list/summarize/extract_document_*
  Drafting        draft_pleading
  Aprobaciones    request_human_approval, list_pending_hitl
  Memoria         remember, recall, recall_relevant, forget
  UI              ui_navigate, ui_open_matter_canvas, ui_open_matter_tab,
                  ui_prefill_form, ui_show_toast, ui_open_command_palette
  Externos        verify_rue_persona, fetch_banrep_dtf
  Delegación      delegate_to(subagent='investigador'|'redactor'|'calculista')

═══════════════════════════════════════════════════════════════════════
REGLAS DE EJECUCIÓN
═══════════════════════════════════════════════════════════════════════

A. TOOLS FIRST. Cualquier petición que pueda resolverse con tools, USA
   tools. Nunca improvises conocimiento.

B. UI ON DEMAND. "Ábreme/muéstrame/llévame" → ui_*. "Llena el formulario
   con X" → ui_prefill_form. NO digas "abriendo..." — llama la tool y al
   final una frase corta ("Listo, abrí el canvas de Rodríguez").

C. JURISPRUDENCIA VERIFICADA. Sólo cita sentencias que aparezcan en
   outputs de research_jurisprudence o validate_citation. Si no hay
   resultados, di "No identifiqué jurisprudencia verificada para ese
   punto."

D. CÁLCULOS DETERMINÍSTICOS. SIEMPRE usa calc_*. NUNCA estimes números
   de cabeza.

E. DELEGA si la tarea requiere ≥3 tools encadenadas (investigación
   profunda → delegate_to('investigador'); redacción completa →
   'redactor'; cálculos múltiples → 'calculista'). Para 1 tool llama
   directamente, no delegues.

   IMPORTANTE: cuando delegues NO necesitas pasar matter_id —
   se inyecta automático desde el contexto activo. Sólo describe la
   tarea con el QUÉ y el POR QUÉ.

F. DRAFTING REAL (no plantilla genérica). Cuando el abogado pide redactar:
   PASO 1: open_matter_context(matter_id) → obtiene partes, hechos, plazos
   PASO 2: research_jurisprudence(query relevante) → 2-3 sentencias
   PASO 3: draft_pleading con `facts` derivados del context Y `citations`
           con los `citation_ref` del paso 2.
   PASO 4: canvas_set_text con el markdown del draft → aparece en el
           Live Canvas para que el abogado lo edite.
   NUNCA llames draft_pleading sin facts; el output queda con
   placeholders [NOMBRE DEL ACTOR] y es inutilizable.

   CO-EDICIÓN DEL CANVAS (cuando ya hay un documento en pantalla):
   · "Agrega una sección de hechos" → canvas_append con el markdown
   · "Corrige la sección de fundamentos" → canvas_replace_section(
        heading="Fundamentos jurídicos", markdown=...)
   · "Lee lo que está escrito y dime qué falta" → canvas_get_current
        primero, luego analiza el text_preview y reporta
   · "Guarda esta versión" → canvas_save_version
   El abogado puede tipear manualmente al mismo tiempo: cuando él está
   tipeando, tus ops se encolan automáticamente y se aplican cuando él
   pause >2 segundos.

G. ANÁLISIS DE DOCUMENTO ("léeme la demanda", "analiza el documento",
   "dime si está bien"):
   PASO 1: list_matter_documents(matter_id)
   PASO 2: para el doc relevante (kind='demanda' o 'sentencia') →
           extract_document_entities(document_id) o summarize_document
   PASO 3: REPORTA POR VOZ los hallazgos (partes, fechas clave,
           inconsistencias, vacíos probatorios). 1-3 oraciones.
   NO te limites a abrir la pestaña Documentos — eso no es análisis.

F. HITL para acciones externas (email, firma digital, radicar, pagos
   >$50M COP, escrito a juez): request_human_approval primero. Para
   gestión interna (notas, plazos, listados) NO se requiere HITL.

G. UPL. Nunca "soy abogado" / "garantizo" / "ganará su caso". Eres
   asistente documental — el abogado titulado revisa y firma.

H. HABEAS DATA. Si el abogado dicta cédula/NIT/datos sensibles,
   confirma el consentimiento del cliente (Ley 1581/2012).

I. IDIOMA CO. "tutela" (Art. 86 CP), "CST", "SMMLV", "Corte Constitucional",
   "Corte Suprema" (Sala Laboral/Civil/Penal), "Consejo de Estado",
   "Honorable Magistrado", "Juzgado XX Laboral del Circuito de Bogotá",
   "demanda ordinaria laboral".

═══════════════════════════════════════════════════════════════════════
EJEMPLOS DE TURNS BIEN HECHOS
═══════════════════════════════════════════════════════════════════════

Abogado: "¿Qué casos tengo activos?"
Tú: [llama list_my_matters] "Ocho activos. Tres altos: Rodríguez,
    Comcel y Constructora del Valle."

Abogado: "Ábreme el de Rodríguez."
Tú: [llama ui_open_matter_canvas] "Listo, abrí Rodríguez."

Abogado: "Calcula liquidación de María, ingreso enero 2018, salario
4.5 millones, despido injustificado."
Tú: [llama calc_liquidacion] "Total reclamable: 22.5 millones."

Abogado [te interrumpe]: "Espera, mejor con interés legal."
Tú: [DETENTE, llama calc_intereses] "Ajustado: 23.8 millones." """


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

    # Filtrar tools al subset Tier-1 (~15 más usadas) para evitar abrumar al
    # modelo Realtime con 40+ descriptores. El resto sigue accesible vía
    # delegate_to → sub-agentes especialistas (cada uno con su subset).
    all_tools = _tool_descriptors()
    tier1 = [t for t in all_tools if t["name"] in TIER1_TOOLS]

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
                "threshold": 0.6,
                "prefix_padding_ms": 500,
                "silence_duration_ms": 900,
                "create_response": True,
                "interrupt_response": True,
            },
            # OpenAI Realtime API exige temperature >= 0.6 (distinto a Chat
            # Completions que acepta 0.0). Bajar de 0.6 hace que session.update
            # sea rechazado y el agente entra sin instrucciones ni tools.
            "temperature": 0.6,
            "max_response_output_tokens": 1500,
            "instructions": instructions,
            "tools": tier1,
            "tool_choice": "auto",
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Tier-1 tools: las que el orquestador (voz) ve directamente.
# El resto (Tier-2) sólo se invocan vía delegate_to → sub-agentes,
# que tienen su propio allowed_tools subset.
# ─────────────────────────────────────────────────────────────────────
TIER1_TOOLS = {
    # Caso · contexto
    "open_matter_context",
    "list_my_matters",
    "find_client",
    # Calendario · gestión rápida
    "list_upcoming_deadlines",
    "add_matter_deadline",
    "add_matter_note",
    # Cálculos determinísticos (los 3)
    "calc_liquidacion",
    "calc_prescripcion",
    "calc_intereses",
    # Investigación legal directa
    "research_jurisprudence",
    # Documentos · necesarios para análisis "léeme la demanda"
    "list_matter_documents",
    "extract_document_entities",
    "summarize_document",
    # Drafting (escribe a Canvas)
    "draft_pleading",
    # Canvas · co-edición agente↔abogado en TipTap editor
    "canvas_set_text",
    "canvas_append",
    "canvas_replace_section",
    "canvas_save_version",
    "canvas_get_current",
    # UI bridge esenciales
    "ui_navigate",
    "ui_open_matter_canvas",
    "ui_open_matter_tab",
    "ui_prefill_form",
    # Compliance gate
    "request_human_approval",
    # Delegación a especialistas para tareas complejas
    "delegate_to",
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
        # ─── F3 · Calculadoras adicionales ─────────────────────────────
        {
            "type": "function",
            "name": "calc_prescripcion",
            "description": (
                "Calcula la fecha de prescripción de una acción legal en Colombia. "
                "Soporta: civil_ordinaria (10 años), civil_ejecutiva (5 años), "
                "comercial_ordinaria (10 años), comercial_ejecutiva (3 años), "
                "laboral (3 años · CST 488), familiar_alimentos (5 años), "
                "accion_revision (2 años · CGP 354), penal_querella (6 meses · CPP 73). "
                "Si hay interrupción (notificación demanda, reconocimiento), "
                "el plazo se re-cuenta desde esa fecha (CGP Art. 94)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tipo_accion": {
                        "type": "string",
                        "enum": ["civil_ordinaria", "civil_ejecutiva", "comercial_ordinaria",
                                 "comercial_ejecutiva", "laboral", "familiar_alimentos",
                                 "accion_revision", "penal_querella"],
                    },
                    "fecha_exigibilidad": {"type": "string", "format": "date"},
                    "fecha_interrupcion": {"type": "string", "format": "date"},
                    "fecha_calculo": {"type": "string", "format": "date"},
                    "case_label": {"type": "string"},
                },
                "required": ["tipo_accion", "fecha_exigibilidad"],
            },
        },
        {
            "type": "function",
            "name": "calc_intereses",
            "description": (
                "Calcula intereses moratorios determinísticamente. Tipos: "
                "comercial_moratorio (Decreto 519/2007 — 1.5× corriente, ~29.22% anual 2026), "
                "civil_legal (CC Art. 1617 par. 2 — 6% supletivo), "
                "convencional (tasa pactada, requiere `tasa_anual`). "
                "Métodos: simple (lineal) o compuesto (1+r)^t. "
                "Base 360 (comercial) o 365 (civil)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tipo_interes": {
                        "type": "string",
                        "enum": ["comercial_moratorio", "civil_legal", "convencional"],
                    },
                    "capital_cop": {"type": "number"},
                    "fecha_inicio": {"type": "string", "format": "date"},
                    "fecha_fin": {"type": "string", "format": "date"},
                    "tasa_anual": {"type": "number", "description": "Sólo si tipo='convencional'"},
                    "base_calculo": {"type": "integer", "enum": [360, 365], "default": 360},
                    "metodo": {"type": "string", "enum": ["simple", "compuesto"], "default": "simple"},
                    "case_label": {"type": "string"},
                },
                "required": ["tipo_interes", "capital_cop", "fecha_inicio"],
            },
        },
        # ─── F1 · Análisis profundo IA del documento ───────────────────
        {
            "type": "function",
            "name": "extract_document_entities",
            "description": (
                "Extrae entidades estructuradas (partes, fechas, obligaciones, "
                "montos, inconsistencias, riesgos legales, vacíos probatorios) "
                "de un documento del expediente. Llama OCR si es necesario. "
                "Auto-puebla matter_parties faltantes con origen='ai_extracted'. "
                "Si ya hay extracción reciente, devuelve la cacheada salvo que "
                "regenerate=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "uuid de matter_documents"},
                    "regenerate": {"type": "boolean", "default": False},
                },
                "required": ["document_id"],
            },
        },
        # ─── F2 · Judicial notifications ───────────────────────────────
        {
            "type": "function",
            "name": "subscribe_to_expediente",
            "description": (
                "Suscribe el caso a polling automático de actuaciones en Rama "
                "Judicial / DOF. Cada vez que el poller corre, detecta nuevas "
                "actuaciones y las inserta en judicial_notifications. Si la "
                "suscripción ya existe, la re-activa."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expediente": {"type": "string", "description": "Número de radicado completo"},
                    "matter_id": {"type": "string"},
                    "fuente": {
                        "type": "string",
                        "enum": ["rama_judicial_demo", "rama_judicial_live", "dof_co_demo"],
                        "default": "rama_judicial_demo",
                    },
                    "juzgado": {"type": "string"},
                    "ciudad": {"type": "string"},
                },
                "required": ["expediente"],
            },
        },
        {
            "type": "function",
            "name": "list_judicial_notifications",
            "description": (
                "Lista notificaciones judiciales del despacho, ordenadas por "
                "severidad. Por defecto sólo las no leídas."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "only_unread": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "default": 10, "maximum": 25},
                },
            },
        },
        {
            "type": "function",
            "name": "poll_judicial_now",
            "description": (
                "Forza el polling inmediato de todas las suscripciones activas "
                "del despacho. Útil cuando el abogado pregunta '¿hay novedades "
                "en mis casos?'. Devuelve cuántas notificaciones nuevas se "
                "insertaron."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        # ─── F1 v2 · UI Bridge · controla el browser del abogado ──────
        {
            "type": "function",
            "name": "ui_navigate",
            "description": (
                "Navega a una ruta de la aplicación. Usar cuando el abogado "
                "diga 'ábreme', 'muéstrame', 'llévame a', 've a'. Paths "
                "permitidos: /inicio, /casos, /casos/:id, /casos/:id/canvas, "
                "/casos/nuevo, /clientes, /clientes/:id, /clientes/nuevo, "
                "/calendario, /documentos, /notificaciones, /liquidacion, "
                "/calc/prescripcion, /calc/intereses, /canvas, "
                "/settings/despacho, /settings/privacidad."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "type": "function",
            "name": "ui_open_matter_canvas",
            "description": (
                "Abre el Live Canvas de un caso para redactar/dictar. Atajo "
                "preferido cuando el abogado dice 'trabajemos en Canvas', "
                "'dictemos alegatos para X', 'redactemos en X'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string", "description": "uuid del matter (default: matter_id activo)"},
                },
            },
        },
        {
            "type": "function",
            "name": "ui_open_matter_tab",
            "description": (
                "Abre el detalle del caso y selecciona una pestaña específica. "
                "Cuando el abogado dice 'muéstrame las partes del caso X' "
                "o 'enseñame la cronología'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string"},
                    "tab": {
                        "type": "string",
                        "enum": ["Resumen", "Análisis IA", "Cronología", "Documentos", "Partes", "Notas", "Calendario"],
                    },
                },
                "required": ["tab"],
            },
        },
        {
            "type": "function",
            "name": "ui_scroll_to",
            "description": (
                "Hace scroll a una sección dentro de la página actual. "
                "Usa el atributo data-scroll-target. Cuando el abogado dice "
                "'muéstrame la sección X', 'sube a X', 'bájame a X'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
        },
        {
            "type": "function",
            "name": "ui_open_command_palette",
            "description": "Abre el Command Palette (⌘K) opcionalmente con query inicial.",
            "parameters": {
                "type": "object",
                "properties": {"initial_query": {"type": "string"}},
            },
        },
        {
            "type": "function",
            "name": "ui_prefill_form",
            "description": (
                "Pre-llena un formulario con valores dictados por voz. "
                "Usar cuando el abogado dicta datos de un cálculo/registro. "
                "Forms disponibles: 'liquidacion' (trabajadorNombre, "
                "fechaIngreso, fechaTerminacion, salarioMensual, causa, "
                "tipoContrato, salarioIntegral); 'prescripcion' (caseLabel, "
                "tipoAccion, fechaExigibilidad, fechaInterrupcion); "
                "'intereses' (caseLabel, tipoInteres, capital, fechaInicio, "
                "fechaFin, tasaAnual, base, metodo); 'new_matter' (clientId, "
                "titulo, materia, tribunal, expediente, priority); "
                "'new_client' (tipo, nombre, taxId, personalId, email, telefono, vip)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "form": {
                        "type": "string",
                        "enum": ["liquidacion", "prescripcion", "intereses", "new_matter", "new_client"],
                    },
                    "values": {"type": "object", "description": "Mapa parcial de campos a setear"},
                    "submit": {"type": "boolean", "default": False, "description": "Si true, envía el form tras rellenar"},
                },
                "required": ["form", "values"],
            },
        },
        {
            "type": "function",
            "name": "ui_show_toast",
            "description": "Muestra un toast notification breve al usuario (info/success/warning/error).",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "variant": {"type": "string", "enum": ["info", "success", "warning", "error"]},
                },
                "required": ["message"],
            },
        },
        {
            "type": "function",
            "name": "ui_open_modal",
            "description": "Abre un modal de confirmación con título, cuerpo y botones aceptar/cancelar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "confirm_label": {"type": "string"},
                    "cancel_label": {"type": "string"},
                },
                "required": ["title", "body"],
            },
        },
        # ─── F2 · External research (portales públicos) ──────────────
        {
            "type": "function",
            "name": "search_suin_juriscol",
            "description": (
                "Consulta SUIN-Juriscol (Función Pública) para verificar "
                "existencia y datos de una norma colombiana. Usar cuando "
                "research_jurisprudence/validate_norm_vigencia no tienen el "
                "dato y necesitas la fuente oficial. Cacheado 24h."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": ["LEY", "DECRETO", "RESOLUCION", "CIRCULAR", "CODIGO"]},
                    "numero": {"type": "integer"},
                    "anio": {"type": "integer"},
                },
                "required": ["tipo", "numero", "anio"],
            },
        },
        {
            "type": "function",
            "name": "verify_rue_persona",
            "description": (
                "Verifica persona natural/jurídica en RUES (Cámaras de Comercio). "
                "Útil para validar contraparte antes de demanda. Cacheado 12h."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "NIT (con o sin dígito verificación) o razón social"}},
                "required": ["query"],
            },
        },
        {
            "type": "function",
            "name": "fetch_dof_co_publicacion",
            "description": (
                "Lista publicaciones recientes del Diario Oficial colombiano. "
                "Filtra por keyword opcional. Útil para detectar normas nuevas "
                "que afecten al despacho. Cacheado 6h."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "limit": {"type": "integer", "default": 10, "maximum": 25},
                },
            },
        },
        {
            "type": "function",
            "name": "fetch_banrep_dtf",
            "description": (
                "Consulta tasas Banrep (DTF, IBR, IPC, TRM). Para cálculos de "
                "intereses moratorios o ajustes inflacionarios. Si no está "
                "disponible, devuelve valor de fallback de legal_constants."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "serie": {"type": "string", "enum": ["DTF", "IBR_OVERNIGHT", "IPC", "TRM"], "default": "DTF"},
                },
            },
        },
        # ─── F5 · Memoria persistente ─────────────────────────────────
        {
            "type": "function",
            "name": "remember",
            "description": (
                "Guarda una preferencia o dato persistente para recordar entre "
                "sesiones. Scope='firm' (todo el despacho), 'user' (sólo este "
                "usuario), 'matter' (vinculado a un caso). Útil cuando el "
                "abogado dice 'recuerda que...', 'mi preferencia es...', "
                "'no olvides X para este caso'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"description": "JSON serializable"},
                    "scope": {"type": "string", "enum": ["firm", "user", "matter"], "default": "firm"},
                    "ttl_days": {"type": "integer", "description": "Auto-borrar tras N días (null = permanente)"},
                },
                "required": ["key", "value"],
            },
        },
        {
            "type": "function",
            "name": "recall",
            "description": "Recupera una memoria por key exacto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "scope": {"type": "string", "enum": ["firm", "user", "matter"], "default": "firm"},
                },
                "required": ["key"],
            },
        },
        {
            "type": "function",
            "name": "recall_relevant",
            "description": (
                "Búsqueda semántica de memorias relevantes a un query. "
                "Útil al inicio de una sesión: 'recall_relevant(query=titulo del "
                "matter activo)' devuelve preferencias y notas pasadas relevantes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_n": {"type": "integer", "default": 3, "maximum": 10},
                },
                "required": ["query"],
            },
        },
        {
            "type": "function",
            "name": "forget",
            "description": "Borra una memoria por key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "scope": {"type": "string", "enum": ["firm", "user", "matter"], "default": "firm"},
                },
                "required": ["key"],
            },
        },
        # ─── F-Canvas · co-edición del documento ──────────────────────
        {
            "type": "function",
            "name": "canvas_set_text",
            "description": (
                "Reemplaza TODO el contenido del Live Canvas con un markdown. "
                "Usar después de draft_pleading cuando quieres poner el draft "
                "completo en pantalla. El editor mantiene autosave cada 3s."
            ),
            "parameters": {
                "type": "object",
                "properties": {"markdown": {"type": "string"}},
                "required": ["markdown"],
            },
        },
        {
            "type": "function",
            "name": "canvas_append",
            "description": (
                "Añade un fragmento de markdown AL FINAL del documento del "
                "Canvas. Útil para agregar una nueva sección sin tocar lo "
                "existente. Soporta headings (#, ##, ###), listas, énfasis."
            ),
            "parameters": {
                "type": "object",
                "properties": {"markdown": {"type": "string"}},
                "required": ["markdown"],
            },
        },
        {
            "type": "function",
            "name": "canvas_replace_section",
            "description": (
                "Reemplaza el contenido bajo un heading (h1/h2/h3) específico "
                "con nuevo markdown. Match por substring case-insensitive del "
                "título. Si no encuentra el heading, hace append al final."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string", "description": "Nombre del heading a reemplazar"},
                    "markdown": {"type": "string"},
                },
                "required": ["heading", "markdown"],
            },
        },
        {
            "type": "function",
            "name": "canvas_save_version",
            "description": "Fuerza guardar versión del documento actual a matter_document_versions.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "canvas_get_current",
            "description": (
                "Lee el contenido actual del documento del Canvas (última "
                "versión persistida en matter_document_versions). Útil cuando "
                "el abogado pide 'analiza el documento' o 'corrige tal sección'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string"},
                    "document_id": {"type": "string"},
                },
            },
        },
        # ─── F3 · Sub-agent delegation ────────────────────────────────
        {
            "type": "function",
            "name": "delegate_to",
            "description": (
                "Delega una tarea compleja a un sub-agente especializado. "
                "Usar para: investigación profunda jurisprudencial (subagent="
                "'investigador'), redacción de escritos completos (subagent="
                "'redactor'), o cálculos legales sin alucinación (subagent="
                "'calculista'). El sub-agente ejecutará tools específicas y "
                "devolverá un resumen con findings clave. ALTERNATIVA a llamar "
                "tools directamente cuando la tarea requiere ≥3 tool calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subagent": {
                        "type": "string",
                        "enum": ["investigador", "redactor", "calculista"],
                    },
                    "task": {"type": "string", "description": "Descripción clara de la tarea para el sub-agente"},
                    "context_extra": {
                        "type": "object",
                        "description": "Datos adicionales (matter_id, citations preliminares, etc.)",
                    },
                },
                "required": ["subagent", "task"],
            },
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

        # 3) Persistir transcripción de turns (user + assistant) en agent_traces
        # para poder diagnosticar conversaciones sin depender del browser.
        if etype == "conversation.item.input_audio_transcription.completed":
            user_text = (evt.get("transcript") or "").strip()
            if user_text:
                asyncio.create_task(_persist_trace(
                    kind="llm_call", name="user_turn",
                    firm_id=ctx.get("firm_id"),
                    matter_id=ctx.get("matter_id"),
                    session_id=ctx.get("session_id"),
                    input_obj=None,
                    output_obj={"text": user_text},
                    duration_ms=None,
                ))
        elif etype == "response.audio_transcript.done":
            assistant_text = (evt.get("transcript") or "").strip()
            if assistant_text:
                asyncio.create_task(_persist_trace(
                    kind="llm_call", name="assistant_turn",
                    firm_id=ctx.get("firm_id"),
                    matter_id=ctx.get("matter_id"),
                    session_id=ctx.get("session_id"),
                    input_obj=None,
                    output_obj={"text": assistant_text},
                    duration_ms=None,
                ))

        # 4) Lightweight event mapping for the browser HUD
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

    # F1 · UI BRIDGE
    ui_command = None
    if isinstance(result, dict) and "_ui_command" in result:
        ui_command = result.pop("_ui_command")
        try:
            await websocket.send_json({
                "type": "ui.command",
                "id": call_id,
                "tool": name,
                "command": ui_command,
            })
        except Exception as e:
            logger.warning("failed to relay ui.command: %s", e)

    # F6 · trace per-tool-call (no bloqueante, no levanta)
    asyncio.create_task(_persist_trace(
        kind="tool_call",
        name=name,
        firm_id=ctx.get("firm_id"),
        matter_id=ctx.get("matter_id"),
        session_id=ctx.get("session_id"),
        input_obj=args,
        output_obj=result,
        duration_ms=duration_ms,
        error=(result.get("error") if isinstance(result, dict) else None),
    ))

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


async def _persist_trace(
    *,
    kind: str,
    name: str,
    firm_id: Optional[str],
    matter_id: Optional[str],
    session_id: Optional[str],
    input_obj: Any = None,
    output_obj: Any = None,
    duration_ms: Optional[int] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cost_usd: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """F6 · best-effort trace persist. Never raises."""
    if not firm_id:
        return
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            return
        async with storage.pool.acquire() as conn:
            await conn.execute(
                """
                insert into agent_traces
                  (firm_id, matter_id, session_id, kind, name, input,
                   output_preview, duration_ms, tokens_in, tokens_out,
                   cost_usd, error)
                values
                  ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::jsonb,
                   $7::jsonb, $8, $9, $10, $11, $12)
                """,
                firm_id, matter_id, session_id, kind, name,
                json.dumps(_preview(input_obj), default=str) if input_obj is not None else None,
                json.dumps(_preview(output_obj), default=str) if output_obj is not None else None,
                duration_ms, tokens_in, tokens_out, cost_usd, error,
            )
    except Exception as e:
        logger.debug("trace persist failed: %s", e)


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
