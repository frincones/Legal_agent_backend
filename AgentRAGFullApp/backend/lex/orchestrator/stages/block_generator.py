"""Stage 3: Block Generator — genera bloques tipados por sección con streaming.

En M1 genera Block[] sintetizados a partir del LLM output en formato JSON.
M3 conectará calculadora + hunters para enriquecer el contexto.

Diseño:
- Por cada sección del plan, invoca gpt-4o (o gpt-4o-mini para encabezado/firma)
- LLM devuelve JSON con array de blocks
- Stream a nivel de bloque: emitimos block_emit cuando llega cada bloque
- M1 no hace streaming token-by-token (eso requiere streaming JSON parser, M5)
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from lex.blocks.schema import (
    Block,
    BlankBlock,
    CalcStepBlock,
    FirmaBlock,
    HechoBlock,
    JuramentoBlock,
    JurisprudenciaBlock,
    NormaCitadaBlock,
    ParagraphBlock,
    PretensionBlock,
    Run,
    SectionHeadingBlock,
    SilogismoBlock,
    SubsectionBlock,
    TableBlock,
    TitleBlock,
    new_block_id,
)

logger = logging.getLogger(__name__)


# Modelos por sección — secciones críticas usan gpt-4o, resto gpt-4o-mini
HEAVY_SECTIONS = {
    "fundamentos", "fundamentos_derecho", "razonamiento", "pretensiones",
    "hechos", "liquidacion", "calculos", "analisis", "tesis",
}


def _model_for_section(section_key: str) -> str:
    if section_key in HEAVY_SECTIONS:
        return "gpt-4o"
    return "gpt-4o-mini"


SYSTEM_PROMPT_GENERATOR = """Eres un ABOGADO LITIGANTE SENIOR colombiano con más de 20 años de experiencia en redacción forense.
Tu redacción debe ser EQUIVALENTE a la de un memorial presentado por un bufete top de Bogotá.

Tu OUTPUT es JSON con un array de BLOQUES TIPADOS (no markdown). Cada bloque tiene:
- "type": tipo del bloque (lista a continuación)
- campos específicos por tipo

TIPOS DISPONIBLES:
{
  "title":           {"text": "...", "level": 0|1|2},
  "section_heading": {"roman": "I", "text": "...", "section_key": "..."},
  "subsection":      {"number": "1.1", "text": "..."},
  "paragraph":       {"runs": [{"text": "...", "bold": false, "italic": false, "underline": false}], "align": "justify"},
  "hecho":           {"num": 1, "runs": [...]},
  "pretension":      {"ord": "PRIMERA", "kind": "declarativa|condena|general", "runs": [...]},
  "norma_citada":    {"norma": "Art. 64 CST", "contenido": [...], "verified": false},
  "jurisprudencia":  {"id": "SL1430-2022", "mp": "Iván Mauricio Lenis Gómez", "corte": "CSJ Sala Laboral", "ratio": [...], "verified": false},
  "silogismo":       {"premisa_mayor": [...], "premisa_menor": [...], "conclusion": [...]},
  "table":           {"header": [...], "rows": [[...]], "has_total_row": true|false},
  "calc_step":       {"label": "...", "formula": "...", "aplicacion": "...", "total": "..."},
  "list_item":       {"kind": "anexo|documental|testimonial|pericial|generic", "num": "1", "runs": [...]},
  "juramento":       {"text": "...", "norma_ref": "Art. 28 CPTSS"},
  "firma":           {"ciudad_fecha": "...", "nombre": "...", "tp": "...", "cc": "...", "email": "...", "telefono": "..."},
  "blank":           {}
}

REGLAS DE CALIDAD:
1. Tono solemne: "Honorable señor Juez", "respetuosamente solicito", "comedidamente expongo"
2. Citas exactas con artículo + ley + año. Jurisprudencia con M.P. y radicación.
3. Numeración romana en pretensiones, arábiga en hechos
4. Si hay cálculos, usa "calc_step" para mostrar fórmula + aplicación + total
5. Si hay tabla resumen, usa "table"
6. Mezcla runs con bold para destacar normas y conceptos clave
7. NO inventes citas/jurisprudencia que no aparezcan en el CONTEXTO LEGAL
8. Cada sección debe tener 5-12 bloques sustantivos (excepto encabezado/firma)
9. Usa placeholders entre corchetes: [NOMBRE_DEMANDANTE], [FECHA_INGRESO], etc, si faltan datos

OUTPUT: JSON con clave "blocks" que es un array de bloques.
Ejemplo: {"blocks": [{"type": "paragraph", "runs": [{"text": "..."}]}, ...]}
"""


async def generate_section_blocks(
    client,
    doc_type: str,
    section_key: str,
    section_title: str,
    section_order: int,
    intent: str,
    brief: str,
    extracted_data: dict[str, Any],
    previous_sections_summary: str = "",
    rag_context: list[dict] | None = None,
    calculations: dict[str, Any] | None = None,
    jurisprudencia: list[dict] | None = None,
    section_instruction: str = "",
    expected_blocks: list[str] | None = None,
) -> AsyncIterator[Block]:
    """Genera bloques tipados para una sección. Yield Block uno por uno."""
    model = _model_for_section(section_key)

    # Construir contexto enriquecido
    ctx_parts = [
        f"DOC TYPE: {doc_type}",
        f"SECCIÓN: {section_order}. {section_title} (key={section_key})",
        f"INTENT: {intent}",
        f"BRIEF: {brief}",
        f"DATOS EXTRAÍDOS: {json.dumps(extracted_data, ensure_ascii=False)}",
    ]
    if section_instruction:
        ctx_parts.append(f"INSTRUCCIÓN ESPECÍFICA DE LA SECCIÓN: {section_instruction}")
    if expected_blocks:
        ctx_parts.append(f"BLOQUES PRIORITARIOS A USAR: {', '.join(expected_blocks)}")
    if calculations:
        ctx_parts.append(f"CÁLCULOS DETERMINÍSTICOS:\n{json.dumps(calculations, ensure_ascii=False, indent=2)}")
    if jurisprudencia:
        ctx_parts.append("JURISPRUDENCIA RELEVANTE (úsala con cita exacta + M.P.):")
        for j in jurisprudencia[:5]:
            ctx_parts.append(
                f"  - {j.get('id', '?')} M.P. {j.get('mp', '?')} | {j.get('corte', '?')}: "
                f"{(j.get('ratio') or '')[:300]}"
            )
    if rag_context:
        ctx_parts.append("CONTEXTO LEGAL ADICIONAL (RAG):")
        for i, c in enumerate(rag_context[:5], 1):
            ctx_parts.append(
                f"  [{i}] {c.get('source', '?')} — {(c.get('title') or '')[:80]}: "
                f"{(c.get('text') or '')[:400]}"
            )
    if previous_sections_summary:
        ctx_parts.append(f"\nSECCIONES PREVIAS (resumen): {previous_sections_summary}")

    user_prompt = (
        "\n\n".join(ctx_parts)
        + "\n\nREDACTA AHORA SOLO ESTA SECCIÓN como JSON {\"blocks\": [...]}. NO incluyas título de sección (ya se renderiza aparte)."
    )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_GENERATOR},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or '{"blocks": []}'
        data = json.loads(raw)
        raw_blocks = data.get("blocks", [])
    except Exception as e:
        logger.exception("block_generator failed for section %s: %s", section_key, e)
        # Fallback: bloque de error visible
        yield ParagraphBlock(runs=[Run(text=f"[Error generando sección {section_title}: {str(e)[:120]}]")])
        return

    for rb in raw_blocks:
        try:
            block = _materialize_block(rb)
            if block is not None:
                yield block
        except Exception as e:
            logger.warning("block parsing failed for %s: %s", rb.get("type"), e)
            continue


def _materialize_block(raw: dict) -> Block | None:
    """Convierte dict crudo del LLM a un Block Pydantic. Defensive parsing."""
    btype = raw.get("type")
    if not btype:
        return None

    # Helper para runs
    def _runs(field_val: Any) -> list[Run]:
        if isinstance(field_val, str):
            return [Run(text=field_val)]
        if isinstance(field_val, list):
            return [
                Run(
                    text=r.get("text", "") if isinstance(r, dict) else str(r),
                    bold=bool(r.get("bold", False)) if isinstance(r, dict) else False,
                    italic=bool(r.get("italic", False)) if isinstance(r, dict) else False,
                    underline=bool(r.get("underline", False)) if isinstance(r, dict) else False,
                )
                for r in field_val if r is not None
            ]
        return []

    bid = raw.get("block_id") or new_block_id()
    try:
        if btype == "title":
            return TitleBlock(block_id=bid, text=raw.get("text", ""), level=int(raw.get("level", 1)))
        if btype == "section_heading":
            return SectionHeadingBlock(
                block_id=bid, roman=raw.get("roman", ""),
                text=raw.get("text", ""), section_key=raw.get("section_key", ""),
            )
        if btype == "subsection":
            return SubsectionBlock(block_id=bid, number=raw.get("number", ""), text=raw.get("text", ""))
        if btype == "paragraph":
            return ParagraphBlock(
                block_id=bid, runs=_runs(raw.get("runs", [])),
                align=raw.get("align", "justify"),
            )
        if btype == "hecho":
            return HechoBlock(block_id=bid, num=int(raw.get("num", 0)), runs=_runs(raw.get("runs", [])))
        if btype == "pretension":
            return PretensionBlock(
                block_id=bid, ord=raw.get("ord", ""), kind=raw.get("kind", "general"),
                runs=_runs(raw.get("runs", [])),
            )
        if btype == "norma_citada":
            return NormaCitadaBlock(
                block_id=bid, norma=raw.get("norma", ""),
                contenido=_runs(raw.get("contenido", [])),
                verified=bool(raw.get("verified", False)),
                derogada=bool(raw.get("derogada", False)),
                fuente_ref=raw.get("fuente_ref"),
            )
        if btype == "jurisprudencia":
            return JurisprudenciaBlock(
                block_id=bid, id=raw.get("id", ""), mp=raw.get("mp", ""),
                corte=raw.get("corte", ""), fecha=raw.get("fecha"),
                ratio=_runs(raw.get("ratio", [])),
                chunk_id=raw.get("chunk_id"), verified=bool(raw.get("verified", False)),
                sim_score=raw.get("sim_score"),
            )
        if btype == "silogismo":
            return SilogismoBlock(
                block_id=bid,
                premisa_mayor=_runs(raw.get("premisa_mayor", [])),
                premisa_menor=_runs(raw.get("premisa_menor", [])),
                conclusion=_runs(raw.get("conclusion", [])),
            )
        if btype == "table":
            return TableBlock(
                block_id=bid,
                header=list(raw.get("header", [])),
                rows=[list(r) for r in raw.get("rows", [])],
                has_total_row=bool(raw.get("has_total_row", False)),
            )
        if btype == "calc_step":
            return CalcStepBlock(
                block_id=bid, label=raw.get("label", ""),
                formula=raw.get("formula", ""), aplicacion=raw.get("aplicacion", ""),
                total=raw.get("total", ""),
            )
        if btype == "list_item":
            from lex.blocks.schema import ListItemBlock
            return ListItemBlock(
                block_id=bid, kind=raw.get("kind", "generic"),
                num=str(raw.get("num", "")), runs=_runs(raw.get("runs", [])),
            )
        if btype == "juramento":
            return JuramentoBlock(block_id=bid, text=raw.get("text", ""), norma_ref=raw.get("norma_ref"))
        if btype == "firma":
            return FirmaBlock(
                block_id=bid,
                ciudad_fecha=raw.get("ciudad_fecha", ""),
                nombre=raw.get("nombre", "[NOMBRE_APODERADO]"),
                tp=raw.get("tp", "[TP_APODERADO]"),
                cc=raw.get("cc"), email=raw.get("email"), telefono=raw.get("telefono"),
            )
        if btype == "blank":
            return BlankBlock(block_id=bid)
    except Exception as e:
        logger.warning("Block materialization error for %s: %s", btype, e)
        return None

    logger.warning("Unknown block type: %s", btype)
    return None
