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
from utils.entitlements import requires_module

logger = logging.getLogger(__name__)
# El gate de entitlement se aplica al endpoint POST /ticket (no al router
# completo) para evitar inyectar el dependency basado en Request en el
# endpoint @router.websocket("/ws"), que recibe WebSocket en su lugar.
# Sin ticket válido → no se puede abrir el WS, así que el gate del ticket
# protege todo el flujo voice.
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


@router.post("/ticket", dependencies=[Depends(requires_module("voice_agent"))])
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

  "qué casos tengo" / "mis casos" / "casos pendientes" / "qué tengo hoy"
                                         → list_my_matters
                                            (NO list_upcoming_deadlines —
                                             "casos" ≠ "plazos")
  "qué plazos / qué vence / audiencias"  → list_upcoming_deadlines
  "busca a [cliente]" / "el cliente X"   → find_client
  "agenda audiencia" / "agrega plazo"    → add_matter_deadline
  "agrega nota al caso" / "anota..."     → add_matter_note
  "liquida [trabajador]" / "liquidación" → calc_liquidacion
  "prescripción" / "cuánto tengo"        → calc_prescripcion
  "intereses moratorios"                 → calc_intereses
  "busca jurisprudencia sobre X"         → research_jurisprudence
  "redacta [tutela/demanda/contesta..]"  → draft_pleading + canvas_set_text
  "ábreme [pantalla]"                    → ui_navigate
  "ábreme el canvas de X"                → ui_open_matter_canvas
  "muéstrame [pestaña] de X"             → ui_open_matter_tab
  "llena el formulario con..."           → ui_prefill_form
  "léeme la demanda" / "cárgame el doc"  → list_matter_documents +
                                            get_document_content
                                            (NO summarize_document)
  "ponme el doc en el canvas"            → get_document_content +
                                            canvas_set_text(text obtenido)
  "dame un resumen del doc"              → summarize_document
  "qué partes tiene el doc"              → extract_document_entities
  "investiga a fondo / valida X"         → delegate_to('investigador', '...')
  "redacta completo con jurisprudencia"  → delegate_to('redactor', '...')
  "calcula varios escenarios"            → delegate_to('calculista', '...')

DIFERENCIA CRÍTICA entre tools de documento:
  · summarize_document      = RESUMEN IA breve (1-3 oraciones). NO sirve
                              para mostrar el doc completo.
  · extract_document_entities = partes, fechas, obligaciones (estructura).
                              NO devuelve el texto del doc.
  · get_document_content    = TEXTO COMPLETO del doc. ÚSALO cuando el
                              abogado quiere LEER o CARGAR el documento.

Si NINGUNA tool encaja, llama delegate_to('investigador', tarea_completa)
para que el especialista decida. Si ni el sub-agente sabe, sólo entonces
responde "No tengo la herramienta para eso".

⚠️ INPUT ESPURIO / ECHO: si el `transcript` que recibes tiene <8 caracteres,
NO está claramente en español, o son sólo interjecciones ("ehh", "umm",
"みたい", "okay" sin contexto), ASUME que es echo de tu propia voz capturado
por el micrófono. NO respondas. Espera al siguiente turn real del abogado.
NUNCA generes una respuesta basada en input que no sea claramente español
con intención legal o conversacional clara.

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

6. ⚠️⚠️ REGLA ESTRICTA · MATCH AMBIGUO DE CLIENTE/CASO ⚠️⚠️

   Si el abogado dicta un nombre que NO COINCIDE EXACTAMENTE con un
   caso/cliente del firm:
     · NO abras un caso parecido.
     · NO digas "abrí X" con un nombre DISTINTO al dictado.
     · PREGUNTA con las opciones reales del firm.

   EJEMPLO REAL (cometer este error es FALLA GRAVE):

   Abogado: "Ábreme el canvas de Rodríguez contra Concepción."
   list_my_matters() devuelve: Rodríguez vs Comcel, Rodríguez vs Sanitas,
                                Constructora del Valle, ... (NO hay "Concepción")
   ❌ MAL:  ui_open_matter_canvas(matter_id=Comcel) + "Listo, abrí
            Rodríguez contra Comcel"
   ✅ BIEN: "No encuentro 'Rodríguez contra Concepción'. Tiene Rodríguez
            contra Comcel y Rodríguez contra Sanitas. ¿Cuál?"

   El "match aproximado silencioso" es la falla más grave que puedes
   cometer porque te pones a trabajar sobre el caso EQUIVOCADO.

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
   · "Agrega una sección de hechos al final" → canvas_append(markdown)
   · "Inserta esta cláusula aquí donde tengo el cursor" →
        canvas_insert_at_cursor(markdown)
   · "Corrige la sección de fundamentos" → canvas_replace_section(
        heading="Fundamentos jurídicos", markdown=...)
   · "Agrega un párrafo en la sección de pretensiones" →
        canvas_select_section(heading="Pretensiones") + luego
        canvas_insert_at_cursor(markdown)
   · "Cambia todas las menciones de X por Y" →
        canvas_find_replace(needle="X", replacement="Y")
        (case-sensitive, exacto, plain text)
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


async def build_voice_instructions(
    pool,
    firm_id: Optional[str],
    user_id: Optional[str],
    session_id: Optional[str],
) -> str:
    """Retorna las instrucciones de voz usando el persona assembler (ADR-007 Fase 2).

    Si LEXAI_PERSONA_VOICE=false o PHASE < 2, retorna LEGAL_VOICE_INSTRUCTIONS original.
    Si la RPC falla, también retorna el fallback. LEGAL_VOICE_INSTRUCTIONS permanece
    como constante para nunca dejar el canal sin instrucciones.
    """
    try:
        from utils import persona_assembler
        assembled, version_id, _checksum = await persona_assembler.get_assembled_system_prompt(
            pool=pool,
            firm_id=firm_id,
            user_id=user_id,
            channel="voice",
            skill=None,
            session_id=session_id,
            legacy_prompt=LEGAL_VOICE_INSTRUCTIONS,
        )
        if version_id:
            logger.info(
                "build_voice_instructions: persona ensamblada OK "
                "version_id=%s firm_id=%s",
                version_id, firm_id,
            )
        return assembled
    except Exception as exc:
        logger.warning(
            "build_voice_instructions: error al llamar persona_assembler · "
            "fallback a LEGAL_VOICE_INSTRUCTIONS. error=%s firm_id=%s",
            exc, firm_id,
        )
        return LEGAL_VOICE_INSTRUCTIONS


async def build_session_update(
    matter_id: Optional[str] = None,
    pool=None,
    firm_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Build the initial session.update payload with tools and config.

    Tool catalog matches server-side `_tool_registry` keys.
    Usa build_voice_instructions para obtener las instrucciones según ADR-007 Fase 2.
    """
    if pool is not None:
        instructions = await build_voice_instructions(pool, firm_id, user_id, session_id)
    else:
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
                # threshold 0.7 (era 0.6): más estricto para evitar que el
                # echo del propio TTS active el VAD como user input. En la
                # sesión 16:43 OpenAI transcribió "みたい" como user_turn,
                # echo claro del audio del agente.
                "type": "server_vad",
                "threshold": 0.7,
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
    # Matter management · prioridad, tags, etapa, archivar, crear (Sprint M)
    "set_matter_priority",
    "tag_matter",
    "update_matter_etapa",
    "archive_matter",
    "create_matter",
    # Cálculos determinísticos (los 3)
    "calc_liquidacion",
    "calc_prescripcion",
    "calc_intereses",
    # Investigación legal directa
    "research_jurisprudence",
    # Documentos · necesarios para análisis "léeme la demanda"
    "list_matter_documents",
    "get_document_content",       # texto COMPLETO del doc (no resumen)
    "extract_document_entities",  # estructura: partes/fechas/obligaciones
    "summarize_document",         # resumen IA breve (1-3 oraciones)
    # Drafting (escribe a Canvas) + plantillas
    "draft_pleading",
    "list_legal_templates",
    # Canvas · co-edición agente↔abogado en TipTap editor (v1 + v2 ProseMirror)
    "canvas_set_text",
    "canvas_append",
    "canvas_replace_section",
    "canvas_insert_at_cursor",
    "canvas_find_replace",
    "canvas_select_section",
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
                "Genera un escrito procesal con estructura legal colombiana "
                "completa (encabezamiento, partes, hechos numerados, "
                "pretensiones principales+subsidiarias, fundamentos de "
                "derecho con citas, pruebas documentales/testimoniales/"
                "interrogatorio/inspección, anexos, notificaciones, juramento "
                "estimatorio CGP 206, firma con T.P.). Persiste en "
                "matter_documents + matter_document_versions. Devuelve "
                "markdown listo para canvas_set_text."
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
                            "derecho_peticion",
                            "carta_requerimiento",
                            "contrato",
                            "escrito",
                        ],
                    },
                    "facts": {
                        "type": "object",
                        "description": (
                            "Datos del caso para rellenar la plantilla. Ej: "
                            "{nombre_actor, cedula_actor, nombre_demandado, "
                            "nit_demandado, fecha_ingreso, fecha_terminacion, "
                            "salario_mensual, cargo, hechos_adicionales, ...}. "
                            "Llamar open_matter_context primero para obtenerlos."
                        ),
                    },
                    "citations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "citation_refs verificados (ej. T-388/2019)",
                    },
                },
                "required": ["matter_id", "kind", "facts"],
            },
        },
        {
            "type": "function",
            "name": "list_legal_templates",
            "description": (
                "Lista las plantillas legales disponibles para drafting con "
                "su título, descripción y materia aplicable. Útil cuando el "
                "abogado pregunta '¿qué plantillas tienes?' o el agente debe "
                "decidir cuál usar antes de llamar draft_pleading."
            ),
            "parameters": {"type": "object", "properties": {}},
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
                "PREFERIDA cuando el usuario dice 'agrega/crea una nota', 'anota X', "
                "'pasos pendientes', 'recordatorio', 'observación del caso'. "
                "Guarda la nota como fila en la tabla matter_notes y se ve en la "
                "PESTAÑA NOTAS del caso (módulo separado del editor). "
                "USA ESTA en lugar de canvas_append/canvas_set_text cuando la "
                "intención sea anotar algo SOBRE EL CASO, no escribir contenido "
                "DENTRO de un documento legal en redacción."
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
        # ── Sprint M · Matter management ──────────────────────────────
        {
            "type": "function",
            "name": "set_matter_priority",
            "description": (
                "Cambia la prioridad de un caso. Usa esto cuando el abogado dice "
                "'márcalo como urgente' o 'baja prioridad'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["baja", "media", "alta", "critica", "urgente"],
                    },
                },
                "required": ["matter_id", "priority"],
            },
        },
        {
            "type": "function",
            "name": "tag_matter",
            "description": (
                "Añade una etiqueta (tag) al caso. Útil para clasificar por tema, "
                "cliente VIP, área de práctica especial, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string"},
                    "tag": {"type": "string"},
                },
                "required": ["matter_id", "tag"],
            },
        },
        {
            "type": "function",
            "name": "update_matter_etapa",
            "description": (
                "Actualiza la etapa procesal del caso (ej. 'primera instancia', "
                "'apelación', 'casación', 'cierre')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string"},
                    "etapa": {"type": "string"},
                },
                "required": ["matter_id", "etapa"],
            },
        },
        {
            "type": "function",
            "name": "archive_matter",
            "description": (
                "Archiva un caso (soft delete · status='archivado'). "
                "El caso desaparece de la lista activa pero los datos se preservan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "matter_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["matter_id"],
            },
        },
        {
            "type": "function",
            "name": "create_matter",
            "description": (
                "Crea un nuevo caso (matter). Útil para intake rápido por voz "
                "o cuando el abogado quiere iniciar un expediente nuevo en el chat. "
                "Devuelve `matter_id` del nuevo caso · ÚSALO para llamadas "
                "subsiguientes (add_matter_note, add_matter_deadline, etc.) que "
                "deban aplicar al caso recién creado, NO al matter_id del contexto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "titulo": {"type": "string", "description": "Título del caso · ej. 'Freddy contra Zurich por acoso laboral'."},
                    "materia": {
                        "type": "string",
                        "enum": [
                            "civil", "comercial", "laboral", "familia", "penal",
                            "administrativo", "tributario", "constitucional",
                            "ambiental", "otro",
                        ],
                    },
                    "client_id": {"type": "string", "description": "UUID del cliente si ya existe."},
                    "client_name": {"type": "string", "description": "Nombre del cliente cuando no tienes client_id · la tool lo busca o crea automáticamente. Si el título tiene 'X contra Y', X se toma como cliente por default."},
                    "tribunal": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["baja", "media", "alta", "critica", "urgente"],
                    },
                },
                "required": ["titulo", "materia"],
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
        # ─── Documento · contenido completo ───────────────────────────
        {
            "type": "function",
            "name": "get_document_content",
            "description": (
                "Devuelve el TEXTO COMPLETO de un documento. Usar cuando "
                "el abogado pide 'léeme', 'cárgame', 'analiza la demanda', "
                "'ponme el documento en el canvas'. NO confundir con "
                "summarize_document (que es resumen breve) ni con "
                "extract_document_entities (que es estructura partes/fechas). "
                "Esta tool devuelve el contenido crudo (text/html) de la "
                "última versión guardada del documento."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "uuid de matter_documents"},
                },
                "required": ["document_id"],
            },
        },
        # ─── F-Canvas · co-edición del documento ──────────────────────
        {
            "type": "function",
            "name": "canvas_set_text",
            "description": (
                "REEMPLAZA TODO el contenido del Live Canvas (editor del documento "
                "legal en redacción) con un markdown nuevo. OPERACIÓN DESTRUCTIVA: "
                "borra todo lo que el abogado tenía escrito. "
                "USAR SOLO cuando: (a) acabas de llamar draft_pleading y quieres "
                "colocar el draft completo, o (b) el usuario dice EXPLÍCITAMENTE "
                "'reemplaza el documento entero', 'sustituye todo el contenido'. "
                "NO USAR para 'crear notas', 'agregar anotaciones' o 'pasos "
                "pendientes' del caso → para eso usa add_matter_note. "
                "Para cambios puntuales usa canvas_replace_section (sección por "
                "heading) o canvas_find_replace (string→string). "
                "Si el documento actual ya tiene contenido (>500 chars) la tool "
                "rechaza la operación a menos que pases confirm_overwrite=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "markdown": {"type": "string"},
                    "confirm_overwrite": {
                        "type": "boolean",
                        "description": "Pasar true si el usuario confirmó que "
                                       "quiere reemplazar todo el documento existente.",
                    },
                },
                "required": ["markdown"],
            },
        },
        {
            "type": "function",
            "name": "canvas_append",
            "description": (
                "Añade un fragmento de markdown AL FINAL del documento del "
                "Live Canvas (editor TipTap del caso). Útil para agregar una "
                "nueva sección al borrador legal sin tocar lo existente. "
                "NO USAR para 'agregar notas del caso', 'pasos pendientes', "
                "'recordatorios' o 'observaciones del expediente' → para eso "
                "usa add_matter_note (esas viven en la pestaña Notas, NO dentro "
                "del documento). Esta tool inyecta texto al DOCUMENTO LEGAL "
                "(demanda/contrato/escrito) que se está redactando."
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
                "título. Si no encuentra el heading, hace append al final. "
                "Versión ProseMirror-native: detecta el siguiente heading hermano "
                "y reemplaza solo lo que está entre medias (no toca otras secciones)."
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
            "name": "canvas_insert_at_cursor",
            "description": (
                "Inserta markdown EN LA POSICIÓN EXACTA del caret del usuario "
                "DENTRO DEL DOCUMENTO LEGAL del Live Canvas. Usar cuando el "
                "abogado dice 'agrega aquí esta cláusula', 'inserta donde estoy'. "
                "Diferencia con canvas_append: append siempre va al final; "
                "insert_at_cursor respeta donde está el caret. "
                "NO USAR para anotaciones del caso (use add_matter_note)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"markdown": {"type": "string"}},
                "required": ["markdown"],
            },
        },
        {
            "type": "function",
            "name": "canvas_find_replace",
            "description": (
                "Busca todas las ocurrencias EXACTAS de `needle` (texto plano) en "
                "el documento y las reemplaza por `replacement`. Match es "
                "case-sensitive. Usar para correcciones masivas: 'cambia todas las "
                "menciones de Pedro Pérez por Pedro José Pérez', 'reemplaza CDD "
                "por Cédula de Ciudadanía'. NO usar para cambios de formato (eso "
                "es canvas_replace_section)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "needle": {"type": "string", "description": "Texto exacto a buscar"},
                    "replacement": {"type": "string", "description": "Texto que reemplaza al needle"},
                },
                "required": ["needle", "replacement"],
            },
        },
        {
            "type": "function",
            "name": "canvas_select_section",
            "description": (
                "Sitúa el caret JUSTO DESPUÉS de un heading específico, sin "
                "modificar contenido. Combinar con canvas_insert_at_cursor para "
                "insertar contenido en una sección concreta. Match por substring "
                "case-insensitive del heading. Ejemplo: select_section('Pretensiones') "
                "→ insert_at_cursor('Tercero. ...')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string", "description": "Nombre del heading a localizar"},
                },
                "required": ["heading"],
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

            # 3) Push initial session.update (ADR-007: usa persona assembler si PHASE>=2)
            from utils.db import get_storage as _get_storage_voice
            _voice_storage = await _get_storage_voice()
            _voice_pool = getattr(_voice_storage, "pool", None)
            _session_update_payload = await build_session_update(
                matter_id=matter_id,
                pool=_voice_pool,
                firm_id=firm_id,
                user_id=user_id,
                session_id=session_db_id,
            )
            await upstream.send(json.dumps(_session_update_payload))
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
            # Filtro echo: turns muy cortos (<5 chars) o predominantemente
            # no-latinos (japonés/chino) son casi siempre echo del propio
            # agente capturado por el mic. Marcar como kind='echo' para
            # poder filtrarlos en logs sin perder el rastro.
            non_latin_chars = sum(1 for c in user_text if c and ord(c) > 0x3000)
            looks_echo = (
                len(user_text) < 5
                or (len(user_text) > 0 and non_latin_chars / len(user_text) > 0.3)
            )
            if user_text:
                asyncio.create_task(_persist_trace(
                    kind="llm_call", name=("echo_turn" if looks_echo else "user_turn"),
                    firm_id=ctx.get("firm_id"),
                    matter_id=ctx.get("matter_id"),
                    session_id=ctx.get("session_id"),
                    input_obj=None,
                    output_obj={"text": user_text, "looks_echo": looks_echo},
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
    # Soporta tanto un único `_ui_command` (tool normal) como una lista
    # `_ui_commands` (cuando delegate_to recolecta varios del sub-agente).
    ui_commands_to_send: list = []
    if isinstance(result, dict):
        if "_ui_commands" in result:
            collected = result.pop("_ui_commands") or []
            if isinstance(collected, list):
                ui_commands_to_send.extend([c for c in collected if c])
            # Si también vino _ui_command (envoltorio), evitar duplicado.
            result.pop("_ui_command", None)
        elif "_ui_command" in result:
            single = result.pop("_ui_command")
            if single:
                ui_commands_to_send.append(single)
    for ui_command in ui_commands_to_send:
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
