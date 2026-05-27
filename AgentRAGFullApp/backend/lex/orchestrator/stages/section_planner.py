"""Sprint M19.8 · SectionPlanner — declara qué citas legales debe contener cada sección.

LLM (gpt-4o-mini) toma:
  - intent del usuario
  - brief con datos del caso
  - doc_type clasificado
  - plan de secciones del template
  - jurisprudencia/normas pre-buscadas por hunters

Y devuelve un mapping {section_key: [ExpectedCitation]} donde cada
ExpectedCitation es una referencia legal que el LLM anticipa que
esa sección debería contener.

Esto habilita el loop iterativo de M19.8:
  por cada sección:
    1. Narrar plan declarado
    2. Verificar citas esperadas ANTES de redactar
    3. Inyectar verdicts en el prompt del block_generator
    4. Redactar sección con citas YA verificadas
    5. Narrar sección lista

Costo: ~$0.001-0.002 por documento (1 LLM call gpt-4o-mini).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

PLANNER_MODEL = "gpt-4o-mini"
PLANNER_MAX_TOKENS = 1500
PLANNER_TEMPERATURE = 0.0


@dataclass
class ExpectedCitation:
    """Una cita legal esperada en una sección."""
    ref: str            # "Art. 26 Ley 361 de 1997"
    type: str           # "norma" | "jurisprudencia"
    relevance: str      # "alta" | "media" | "baja"
    purpose: str = ""   # "fundamento del derecho a estabilidad reforzada"

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "type": self.type,
            "relevance": self.relevance,
            "purpose": self.purpose,
        }


@dataclass
class SectionPlan:
    """Plan declarado para todo el documento."""
    by_section: dict[str, list[ExpectedCitation]] = field(default_factory=dict)
    duration_ms: int = 0
    tokens_used: int = 0
    error: Optional[str] = None
    raw_response: Optional[str] = None

    def for_section(self, section_key: str) -> list[ExpectedCitation]:
        return self.by_section.get(section_key, [])

    def total_citations(self) -> int:
        return sum(len(v) for v in self.by_section.values())

    def to_summary_dict(self) -> dict:
        return {
            sec: [c.to_dict() for c in cits]
            for sec, cits in self.by_section.items()
        }


SYSTEM_PROMPT = """Eres un abogado colombiano que PLANIFICA documentos legales.

Recibirás:
- El prompt del usuario (intent + brief)
- Tipo de documento (clasificación previa)
- Lista de secciones del documento
- Lista de jurisprudencia/normas pre-encontradas en el corpus

Tu trabajo: para CADA sección del documento, predecir qué citas legales DEBERÍAN integrarse en esa sección para que la redacción sea sólida.

REGLAS:
1. Solo asignar citas a secciones donde tienen sentido jurídico:
   - PARTES/COMPETENCIA: usualmente sin citas legales
   - HECHOS: solo si los hechos invocan derechos específicos (Art. 13 CN para igualdad, etc.)
   - PRETENSIONES: citas sustantivas (Art. 65 CST sanción moratoria, etc.)
   - FUNDAMENTOS DE DERECHO: la MAYORÍA de las citas van aquí
   - RAZONAMIENTO JURÍDICO: jurisprudencia para argumentar
   - LIQUIDACIÓN: artículos con fórmulas (Art. 65 CST, Art. 64 CST, etc.)
   - PRUEBAS/ANEXOS/JURAMENTO: sin citas
2. Una cita puede aparecer en VARIAS secciones (ej. Art. 26 Ley 361/1997 en HECHOS + PRETENSIONES + FUNDAMENTOS).
3. Marca relevance="alta" para las citas centrales del caso, "media" para soporte, "baja" para mención tangencial.
4. NO inventes citas. Trabaja con las del prompt + el corpus pre-buscado.

Output JSON estricto:
{
  "by_section": {
    "hechos": [
      {"ref": "Art. 13 Constitución Política de 1991", "type": "norma", "relevance": "alta", "purpose": "fundamenta el derecho a la igualdad invocado"},
      ...
    ],
    "fundamentos_derecho": [
      {"ref": "Art. 26 Ley 361 de 1997", "type": "norma", "relevance": "alta", "purpose": "estabilidad laboral reforzada"},
      {"ref": "SU-049 de 2017", "type": "jurisprudencia", "relevance": "alta", "purpose": "sentencia hito sobre estabilidad ocupacional"},
      ...
    ],
    ...
  }
}

Si no puedes asignar citas a una sección, déjala vacía: `"section_key": []`.

Responde SOLO con el JSON. Sin markdown, sin comentarios."""


def _build_user_prompt(
    intent: str,
    brief: str,
    doc_type: str,
    sections_plan: list[dict[str, Any]],
    hunters_results: list[dict[str, Any]],
) -> str:
    sections_str = "\n".join([
        f"  - {s['key']}: {s['title']}"
        for s in sections_plan
    ])
    # Truncar hunters para no saturar prompt
    hunters_str = "(ninguna pre-buscada)"
    if hunters_results:
        items = []
        for h in hunters_results[:25]:
            ref = h.get("providencia") or h.get("citation_ref") or h.get("doc_title") or "?"
            items.append(f"  - {ref}")
        hunters_str = "\n".join(items)

    return f"""INTENT DEL USUARIO:
{intent[:3000]}

BRIEF / DATOS DEL CASO:
{brief[:1500] or "(sin brief adicional)"}

TIPO DE DOCUMENTO:
{doc_type}

SECCIONES DEL DOCUMENTO:
{sections_str}

JURISPRUDENCIA Y NORMAS PRE-BUSCADAS EN EL CORPUS:
{hunters_str}

Asigna las citas a las secciones donde tengan sentido jurídico. Output JSON."""


async def plan_sections(
    client,
    intent: str,
    brief: str,
    doc_type: str,
    sections_plan: list[dict[str, Any]],
    hunters_results: Optional[list[dict[str, Any]]] = None,
    enabled: Optional[bool] = None,
) -> SectionPlan:
    """Genera el plan de citas por sección.

    Returns SectionPlan vacío si:
      - LLM falla
      - client no configurado
      - enabled=False explícito

    Esto es safe — el orchestrator cae al flow tradicional sin plan.
    """
    started = time.time()

    if enabled is False or client is None:
        return SectionPlan(
            duration_ms=int((time.time() - started) * 1000),
            error="disabled" if enabled is False else "no_client",
        )

    user_prompt = _build_user_prompt(intent, brief, doc_type, sections_plan, hunters_results or [])

    try:
        resp = await client.chat.completions.create(
            model=PLANNER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=PLANNER_TEMPERATURE,
            max_tokens=PLANNER_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        tokens = (resp.usage.total_tokens if resp.usage else 0) if resp else 0
    except Exception as e:
        logger.warning("section_planner LLM call failed: %s", e)
        return SectionPlan(
            duration_ms=int((time.time() - started) * 1000),
            error=str(e)[:200],
        )

    try:
        parsed = json.loads(raw)
        by_section_raw = parsed.get("by_section", {})
        if not isinstance(by_section_raw, dict):
            by_section_raw = {}

        by_section: dict[str, list[ExpectedCitation]] = {}
        for sec_key, cits_raw in by_section_raw.items():
            if not isinstance(cits_raw, list):
                continue
            cits: list[ExpectedCitation] = []
            for c in cits_raw[:15]:  # cap 15 citas por sección
                if not isinstance(c, dict):
                    continue
                ref = (c.get("ref") or "").strip()
                if not ref:
                    continue
                ctype = (c.get("type") or "norma").lower()
                if ctype not in ("norma", "jurisprudencia"):
                    ctype = "norma"
                relevance = (c.get("relevance") or "media").lower()
                if relevance not in ("alta", "media", "baja"):
                    relevance = "media"
                cits.append(ExpectedCitation(
                    ref=ref[:200],
                    type=ctype,
                    relevance=relevance,
                    purpose=(c.get("purpose") or "")[:200],
                ))
            by_section[sec_key] = cits

        return SectionPlan(
            by_section=by_section,
            duration_ms=int((time.time() - started) * 1000),
            tokens_used=tokens,
            raw_response=raw[:1500],
        )
    except Exception as e:
        logger.warning("section_planner JSON parse failed: %s", e)
        return SectionPlan(
            duration_ms=int((time.time() - started) * 1000),
            error=f"parse_error:{e}",
            raw_response=raw[:500],
        )
