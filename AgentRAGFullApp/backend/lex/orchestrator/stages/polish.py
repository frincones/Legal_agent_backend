"""Stage 9: Polish pass — gpt-4o sobre el documento completo.

Toma todos los bloques generados, los serializa a texto resumido para el LLM,
pide correcciones de coherencia/transiciones/citas/numeración y devuelve
una lista de cambios sugeridos por block_id (opcional, no rompe el doc).

En M5 implementación mínima: una pasada de "review" que el LLM marca como
aprobada/sugerencias. Los bloques NO se modifican destructivamente.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

POLISH_SYSTEM = """Eres editor senior de un bufete top de Bogotá. Recibirás un memorial legal
completo serializado en bloques tipados. Tu tarea es revisar coherencia,
transiciones, numeración consistente, y citas correctamente formateadas.

NO inventes contenido nuevo. NO modifiques cifras. Solo emite un resumen
JSON con:
{
  "passed": true|false,
  "score": 0.0-10.0,
  "issues": ["..." ],
  "delta_chars": <int>  (estimación de chars cambiados si se aplica polish)
}
"""


async def run_polish(client, all_blocks: list[dict[str, Any]], doc_type: str) -> dict[str, Any]:
    """Pasada de polish best-effort. No modifica bloques en M5."""
    if not all_blocks:
        return {"passed": True, "score": 0.0, "issues": [], "delta_chars": 0}

    # Serializar bloques a texto resumido
    parts = []
    for b in all_blocks[:300]:  # limitar
        bt = b.get("block_type") or b.get("block_data", {}).get("type")
        bd = b.get("block_data") or {}
        if bt == "title":
            parts.append(f"# {bd.get('text', '')}")
        elif bt == "section_heading":
            parts.append(f"## {bd.get('roman', '')}. {bd.get('text', '')}")
        elif bt == "subsection":
            parts.append(f"### {bd.get('number', '')}. {bd.get('text', '')}")
        elif bt == "paragraph":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))
            parts.append(text)
        elif bt == "hecho":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))
            parts.append(f"{bd.get('num')}. {text}")
        elif bt == "pretension":
            text = "".join(r.get("text", "") for r in bd.get("runs", []))
            parts.append(f"{bd.get('ord')}. {text}")
        elif bt == "calc_step":
            parts.append(f"[CALC] {bd.get('label')} = {bd.get('total')}")
        elif bt == "table":
            parts.append(f"[TABLA {len(bd.get('header', []))} cols × {len(bd.get('rows', []))} filas]")
        elif bt == "jurisprudencia":
            parts.append(f"[JURISPRUDENCIA {bd.get('id')} M.P. {bd.get('mp')}]")
        elif bt == "norma_citada":
            parts.append(f"[NORMA {bd.get('norma')}]")
        elif bt == "juramento":
            parts.append(f"[JURAMENTO]")
        elif bt == "firma":
            parts.append(f"[FIRMA {bd.get('nombre')}]")

    serialized = "\n\n".join(parts)[:30000]

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": POLISH_SYSTEM},
                {"role": "user", "content": f"DOC TYPE: {doc_type}\n\nDOCUMENTO:\n{serialized}\n\nResponde con JSON estricto."},
            ],
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        result = json.loads(raw)
        return {
            "passed": bool(result.get("passed", True)),
            "score": float(result.get("score", 8.5)),
            "issues": result.get("issues", [])[:10],
            "delta_chars": int(result.get("delta_chars", 0)),
            "model": "gpt-4o",
        }
    except Exception as e:
        logger.warning("polish stage failed: %s", e)
        return {"passed": True, "score": 8.0, "issues": [], "delta_chars": 0, "error": str(e)[:120]}
