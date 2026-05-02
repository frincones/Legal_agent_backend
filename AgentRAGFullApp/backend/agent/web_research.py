"""
Fact-verification pass powered by OpenAI tool use.

Runs between Phase 6 (context build) and Phase 7 (main streaming LLM)
when enabled. An LLM orchestrator (gpt-4o-mini by default for cost)
receives the user's question plus a condensed summary of what the
agent has gathered so far, and is given two tools:

    search_web(query, site_filter=True)   -> list of {title, url, snippet}
    fetch_url(url)                        -> full page text

The orchestrator decides how many times to call each tool (capped by
config). It returns a Markdown block of VERIFIED FACTS that gets
injected into the main prompt context. This is how we catch things
the RAG alone cannot catch — derogaciones recientes, sentencias
nuevas de la Corte, montos actualizados (SMLMV), verificación de
que una cita existe realmente.

Design choices:
- Orchestrator runs AFTER case_state extraction and ingestion, so
  by that point we already know which norms the question needs.
- Maximum tool calls capped at config.max_tool_calls to bound latency
  (~5-8s per tool call, so 4 calls = 20-30s extra).
- On failure (LLM error, no token, tool timeout) returns "" silently
  — the main pipeline keeps its previous behavior.
- Results are streamed back to the frontend as `web_search` and
  `web_result` events so the user sees the research happening,
  Claude-Code-style.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncGenerator, Optional

from utils.llm import get_openai_client
from utils.usage_tracker import tracker
from legal_sources.web_search import search_web
from legal_sources.url_fetcher import fetch_url

logger = logging.getLogger(__name__)


WEB_RESEARCH_SYSTEM_PROMPT = """Eres un investigador legal profundo para un agente jurídico colombiano. Tu misión: NO responder con snippets de búsqueda; ABRIR los documentos oficiales y extraer los hechos textuales.

Recibes una consulta del usuario y un resumen de lo que el agente ya sabe. Usas dos herramientas:
- `search_web(query, site_filter=True)` → devuelve snippets (NO te limites a estos)
- `fetch_url(url)` → descarga el documento completo (ESTE es tu trabajo principal)

ESTRATEGIA OBLIGATORIA:

0. **FETCH OBLIGATORIO DE LEYES PRINCIPALES**: si la consulta cita una ley clave (ej. "Ley 2445 de 2025", "Ley 361 de 1997", "Decreto 1072 de 2015"), tu primera acción debe ser un search_web específico de esa ley seguido de un fetch_url del primer resultado oficial (.gov.co). NO cites artículos específicos de una ley sin haber fetcheado su texto. El LLM principal citará con base en lo que tú le entregues verificado.

1. **SIEMPRE haz al menos 3-5 fetches a documentos oficiales**. Los snippets de búsqueda son engañosos — una ley puede decir "facilita el acceso" en el snippet y en realidad derogar todo un título. Solo el texto completo te lo confirma.

2. **Para cada norma citada, verifica derogaciones/modificaciones**: busca el texto de la ley modificadora y lee sus artículos finales (siempre dicen "deróguense / modifíquense los artículos X a Y de la ley Z"). Distingue claramente:
   - **DEROGADA** → la norma ya no existe, fue suprimida
   - **SUSTITUIDA / REEMPLAZADA** → un artículo/título fue reescrito completamente (es derogación tácita)
   - **MODIFICADA** → cambios parciales, el resto sigue vigente
   - Usa el verbo EXACTO que encuentres en el texto oficial, no suavices.

3. **Busca jurisprudencia específica**: al menos 3 sentencias recientes (últimos 3 años) relevantes al tema. Cita número, año, magistrado ponente si aparece, y ratio decidendi en 1 frase.

4. **Confirma cifras y plazos**: SMLMV del año en curso (2026 es $1.750.905), plazos exactos ante MinTrabajo, porcentajes de indemnización, artículos específicos citados por número.

REGLAS DE EFICIENCIA:
- Máximo %d llamadas a herramientas en total. Sé estratégico pero completo.
- Prioriza: corteconstitucional.gov.co, suin-juriscol.gov.co, funcionpublica.gov.co, secretariasenado.gov.co, mintrabajo.gov.co.
- Si fetch_url falla (ok=false), intenta una URL alternativa del mismo dato en otro dominio oficial.
- NO repitas búsquedas idénticas. NO hagas fetch si ya tienes la info del snippet + otra fuente.

FORMATO DE SALIDA FINAL (tras usar las herramientas):

Markdown estructurado con este header exacto: `## HECHOS VERIFICADOS EN LÍNEA`

Organiza en subsecciones:
```
## HECHOS VERIFICADOS EN LÍNEA

### Vigencias confirmadas
- ⚠️ CORRECCIÓN: Ley 1564/2012, Título IV (insolvencia persona natural no comerciante) fue DEROGADO TÁCITAMENTE por el Título V de la Ley 2445/2025, que sustituye integralmente el régimen de negociación de deudas (Art. 45 de la Ley 2445/2025 "deróguese los arts. 531 a 576 de la Ley 1564/2012"). Fuente: suin-juriscol.gov.co
- SMLMV 2026: $1.750.905 COP. Fuente: Decreto 1572/2024.

### Jurisprudencia relevante (últimos 3 años)
- Sentencia T-045/2025 (M.P. Juan Carlos Cortés): la estabilidad por salud y embarazo son fueros distintos pero concurrentes. Ratio: el despido simultáneo exige desvirtuar ambas presunciones. Fuente: corteconstitucional.gov.co
- Sentencia SU-075/2018 (M.P. Antonio Lizarazo): unifica fuero de maternidad. Ratio: despido durante embarazo se presume discriminatorio salvo prueba en contrario ante Inspector. Fuente: corteconstitucional.gov.co

### Precisiones técnicas
- Ley 1010/2006 Art. 10: multa por acoso 2-10 SMLMV = $3.501.810 a $17.509.050 (con SMLMV 2026). Fuente: funcionpublica.gov.co
- Ley 1010/2006 Art. 11: garantía anti-retaliación = despido dentro de 6 meses siguientes a queja formal carece de efecto. Fuente: funcionpublica.gov.co

### Correcciones a citas previas
- La sentencia "T-388/2019" que suele citarse para maternidad trata en realidad sobre seguridad personal. La sentencia laboral correcta es T-388/2020. Fuente: corteconstitucional.gov.co
```

Si tras agotar las búsquedas no encuentras NADA útil, responde solo: `SIN_HALLAZGOS`. No inventes."""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Busca en Google/DuckDuckGo restringido a dominios oficiales "
                "colombianos. Úsalo para encontrar normas nuevas, sentencias "
                "recientes, o verificar datos (SMLMV, plazos, procedimientos)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Query en español, específico (nombres de normas, años, temas).",
                    },
                    "site_filter": {
                        "type": "boolean",
                        "description": "Si true, restringe a dominios .gov.co oficiales. Default true.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Descarga el contenido completo de una URL (típicamente un "
                "resultado de search_web). Devuelve título + texto limpio. "
                "Úsalo cuando el snippet del search no sea suficiente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL completa (debe incluir https://).",
                    }
                },
                "required": ["url"],
            },
        },
    },
]


async def run_web_research(
    user_question: str,
    agent_context_summary: str,
    model: str = "gpt-4o-mini",
    max_tool_calls: int = 5,
    session_id: str = "",
    scrape_do_token: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Run the tool-using verifier. Yields progress events and finally the
    verified-facts text.

    Yielded events (for the frontend):
      {"type": "web_research_start"}
      {"type": "web_search", "query": "..."}
      {"type": "web_result", "count": N, "first_title": "..."}
      {"type": "web_fetch", "url": "..."}
      {"type": "web_fetch_done", "url": "...", "ok": true/false, "title": "..."}
      {"type": "web_research_done", "facts": "markdown block or SIN_HALLAZGOS"}
    """
    yield {"type": "web_research_start"}

    if not scrape_do_token:
        scrape_do_token = os.getenv("SCRAPE_DO_TOKEN") or None

    client = get_openai_client()
    system_prompt = WEB_RESEARCH_SYSTEM_PROMPT % max_tool_calls
    user_prompt = (
        f"=== Consulta del usuario ===\n{user_question[:1200]}\n\n"
        f"=== Lo que el agente ya ha reunido ===\n{agent_context_summary[:2500]}\n\n"
        f"Empieza la verificación. Usa como máximo {max_tool_calls} herramientas en total."
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tool_calls_made = 0
    final_facts = ""

    # Hard ceiling on iterations to prevent infinite tool loops.
    for iteration in range(max_tool_calls + 3):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto" if tool_calls_made < max_tool_calls else "none",
                temperature=0.1,
                max_tokens=800,
            )
        except Exception as e:
            logger.warning("web_research LLM call failed: %s", e)
            break

        if response.usage:
            tracker.record_chat(
                model=model,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                purpose="web_research",
                session_id=session_id,
            )

        msg = response.choices[0].message
        # Append assistant message (with tool_calls if any) to messages.
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in (msg.tool_calls or [])
                ],
            }
            if msg.tool_calls
            else {"role": "assistant", "content": msg.content or ""}
        )

        if not msg.tool_calls:
            # No more tool calls → this is the final answer.
            final_facts = (msg.content or "").strip()
            break

        for tc in msg.tool_calls:
            if tool_calls_made >= max_tool_calls:
                # Tool budget exhausted — feed a sentinel back so the
                # model stops calling and writes its conclusion.
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(
                            {"error": "tool_budget_exhausted",
                             "hint": "Escribe tu conclusión con los hechos hasta ahora."}
                        ),
                    }
                )
                continue

            tool_calls_made += 1
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            try:
                if tc.function.name == "search_web":
                    query = str(args.get("query", ""))[:300]
                    site_filter = bool(args.get("site_filter", True))
                    yield {"type": "web_search", "query": query}
                    results = await search_web(
                        query=query,
                        limit=6,
                        site_filter=site_filter,
                        scrape_do_token=scrape_do_token,
                    )
                    yield {
                        "type": "web_result",
                        "count": len(results),
                        "first_title": results[0]["title"] if results else "",
                    }
                    tool_payload = json.dumps({"results": results})[:5000]
                elif tc.function.name == "fetch_url":
                    url = str(args.get("url", ""))
                    yield {"type": "web_fetch", "url": url[:200]}
                    fetched = await fetch_url(url, scrape_do_token=scrape_do_token)
                    yield {
                        "type": "web_fetch_done",
                        "url": url[:200],
                        "ok": bool(fetched.get("ok")),
                        "title": fetched.get("title", "")[:120],
                    }
                    tool_payload = json.dumps(fetched)[:9000]
                else:
                    tool_payload = json.dumps({"error": "unknown_tool"})
            except Exception as e:
                logger.warning("web_research tool %s failed: %s", tc.function.name, e)
                tool_payload = json.dumps({"error": str(e)[:200]})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_payload,
                }
            )

    if not final_facts:
        final_facts = "SIN_HALLAZGOS"

    logger.info(
        "web_research done: %d tool calls, final length=%d chars, says %s",
        tool_calls_made,
        len(final_facts),
        "SIN_HALLAZGOS" if final_facts.upper().startswith("SIN_HALLAZGOS") else "facts",
    )

    yield {"type": "web_research_done", "facts": final_facts}
