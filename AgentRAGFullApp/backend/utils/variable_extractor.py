"""Sprint 19 · LLM variable extractor.

Dado un texto libre (intake submission, PDF text, matter description) y una
lista de variables esperadas, llama al LLM para extraer cada variable como
JSON estricto. Útil para auto-rellenar plantillas legales.

Uso típico:
  vars = await extract_variables(
    text="Cliente: María González, cédula 52123456, demanda laboral...",
    variables=[
      {"name": "nombre_cliente", "kind": "text", "hint": "nombre completo"},
      {"name": "cedula", "kind": "text", "hint": "número de cédula"},
      {"name": "cuantia", "kind": "number"},
      {"name": "materia", "kind": "select", "options": ["laboral", "civil", "familia"]},
    ]
  )
  # → {"nombre_cliente": "María González", "cedula": "52123456", ...}

Si el LLM no puede determinar un valor, devuelve null (no inventa).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Eres un asistente que extrae datos estructurados de texto legal en español.
Recibes:
  1) Un texto fuente (puede ser una descripción de caso, intake de cliente, fragmento de PDF).
  2) Una lista de variables que se necesitan extraer.

Tu trabajo es devolver UN SOLO objeto JSON con las variables como claves y los
valores extraídos como valores. Reglas estrictas:

- Si no puedes determinar una variable con CERTEZA, pon `null`. NO inventes.
- Para `kind: number`, devuelve un número (no string). Si hay separadores
  de miles ("50.000.000"), conviértelos. Si dice "50M", convierte a 50000000.
- Para `kind: date`, formato ISO `YYYY-MM-DD` o `null`.
- Para `kind: select`, devuelve EXACTAMENTE una de las options.
- Para `kind: text`, devuelve string limpio (sin newlines extra, sin espacios al
  inicio/fin).
- Para `kind: checkbox/boolean`, devuelve `true` o `false`.
- No devuelvas texto explicativo. Solo el JSON.
"""


async def extract_variables(
    text: str,
    variables: list[dict],
    *,
    purpose: str = "template_autofill",
    session_id: str = "",
    model: str = "gpt-4o-mini",
) -> dict:
    """Pide al LLM que extraiga las variables especificadas del texto.

    `variables` es una lista de dicts con al menos:
      {"name": "...", "kind": "text|number|date|select|checkbox", ...}
    Opcional: `hint`, `options` (para select).
    """
    if not text or not text.strip():
        return {v["name"]: None for v in variables if v.get("name")}
    if not variables:
        return {}

    # Sanitiza variables
    clean_vars: list[dict] = []
    for v in variables:
        name = (v.get("name") or "").strip()
        if not name:
            continue
        clean_vars.append({
            "name": name,
            "kind": (v.get("kind") or "text").strip(),
            "hint": v.get("hint"),
            "options": v.get("options"),
        })
    if not clean_vars:
        return {}

    vars_spec = "\n".join(
        f"- {v['name']} ({v['kind']})" +
        (f" · opciones: {v['options']}" if v.get("options") else "") +
        (f" · pista: {v['hint']}" if v.get("hint") else "")
        for v in clean_vars
    )

    # Truncar texto demasiado largo (máx ~30k chars como en utils/embeddings)
    src = text.strip()
    if len(src) > 30_000:
        src = src[:30_000] + "…"

    user_prompt = (
        f"Variables a extraer:\n{vars_spec}\n\n"
        f"Texto fuente:\n{src}\n\n"
        "Devuelve el objeto JSON con las variables como claves."
    )

    try:
        from utils.llm import llm_generate_json
        parsed = await llm_generate_json(
            prompt=user_prompt,
            model=model,
            system_prompt=SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=1200,
            purpose=purpose,
            session_id=session_id,
        )
    except Exception as e:
        logger.warning("variable extraction LLM failed: %s", e)
        return {v["name"]: None for v in clean_vars}

    if not isinstance(parsed, dict):
        return {v["name"]: None for v in clean_vars}

    # Post-process: coerce kinds + drop unknown keys
    out: dict = {}
    for v in clean_vars:
        name = v["name"]
        val = parsed.get(name)
        if val is None:
            out[name] = None
            continue
        kind = v["kind"]
        if kind == "number":
            out[name] = _coerce_number(val)
        elif kind == "date":
            out[name] = _coerce_date(val)
        elif kind in ("select", "radio"):
            options = v.get("options") or []
            sv = str(val).strip()
            out[name] = sv if sv in options else None
        elif kind in ("checkbox", "boolean"):
            out[name] = bool(val) if isinstance(val, bool) else _coerce_bool(val)
        else:
            out[name] = str(val).strip() or None
    return out


def _coerce_number(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(".", "").replace(",", "").replace("$", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _coerce_date(v) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return None


def _coerce_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "sí", "si", "yes", "1")
    return bool(v)


# ----------------------------------------------------------------
# Helpers para construir texto fuente desde matter context
# ----------------------------------------------------------------
async def gather_matter_context_text(conn, firm_id: str, matter_id: str) -> str:
    """Compila un blob de texto con todo lo relevante del matter para que el
    LLM tenga contexto rico al rellenar plantillas."""
    parts: list[str] = []

    matter = await conn.fetchrow(
        """
        select m.display_id, m.titulo, m.materia, m.etapa_procesal,
               m.tribunal, m.juzgado, m.expediente, m.cuantia, m.cuantia_currency,
               m.status, m.proxima_fecha, m.proxima_tipo,
               c.nombre as cliente_nombre, c.tax_id as cliente_tax_id,
               c.personal_id as cliente_personal_id, c.email as cliente_email,
               c.telefono as cliente_telefono
          from matters m
          left join clients c on c.id = m.client_id
         where m.firm_id = $1::uuid and m.id = $2::uuid
        """,
        firm_id, matter_id,
    )
    if matter:
        parts.append(
            f"# Caso\n"
            f"ID: {matter['display_id']}\n"
            f"Título: {matter['titulo']}\n"
            f"Materia: {matter['materia']}\n"
            f"Etapa: {matter['etapa_procesal'] or '—'}\n"
            f"Tribunal: {matter['tribunal'] or '—'} · Juzgado: {matter['juzgado'] or '—'}\n"
            f"Expediente: {matter['expediente'] or '—'}\n"
            f"Cuantía: {matter['cuantia'] or '—'} {matter['cuantia_currency'] or ''}\n"
            f"Estado: {matter['status']}\n"
            f"Próxima fecha: {matter['proxima_fecha']} ({matter['proxima_tipo']})\n"
            f"\n# Cliente\n"
            f"Nombre: {matter['cliente_nombre'] or '—'}\n"
            f"NIT/Cédula: {matter['cliente_tax_id'] or matter['cliente_personal_id'] or '—'}\n"
            f"Email: {matter['cliente_email'] or '—'}\n"
            f"Teléfono: {matter['cliente_telefono'] or '—'}"
        )

    try:
        parties = await conn.fetch(
            """
            select rol, nombre, tax_id from matter_parties
             where matter_id = $1::uuid
             order by rol limit 10
            """, matter_id,
        )
        if parties:
            parts.append(
                "# Partes\n" + "\n".join(
                    f"- {p['rol']}: {p['nombre']}" + (f" (NIT {p['tax_id']})" if p['tax_id'] else "")
                    for p in parties
                )
            )
    except Exception:
        pass

    try:
        docs = await conn.fetch(
            """
            select titulo, kind, resumen_ia from matter_documents
             where firm_id = $1::uuid and matter_id = $2::uuid
               and resumen_ia is not null
             order by created_at desc limit 5
            """, firm_id, matter_id,
        )
        if docs:
            parts.append(
                "# Documentos · resúmenes IA\n" + "\n".join(
                    f"- [{d['kind']}] {d['titulo']}: {(d['resumen_ia'] or '')[:400]}"
                    for d in docs
                )
            )
    except Exception:
        pass

    return "\n\n".join(parts)


def parse_template_variables(template_body: str) -> list[str]:
    """Encuentra `{{var_name}}` en un template y devuelve los nombres únicos."""
    if not template_body:
        return []
    matches = re.findall(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", template_body)
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out
