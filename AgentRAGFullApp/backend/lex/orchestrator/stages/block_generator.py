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

REGLAS DE CALIDAD (FORMATO FORENSE COLOMBIANO):
1. Tono solemne: "Honorable señor Juez", "respetuosamente solicito", "comedidamente expongo"
2. Citas exactas con artículo + ley + año. Jurisprudencia con M.P. y radicación.
3. Numeración: arábiga en hechos (1, 2, 3...), ordinales en MAYÚSCULAS en pretensiones (PRIMERA, SEGUNDA, TERCERA...)
4. Si hay cálculos, usa "calc_step" para mostrar fórmula + aplicación + total
5. Si hay tabla resumen, usa "table"
6. Mezcla runs con bold para destacar normas y conceptos clave
7. NO inventes citas/jurisprudencia que no aparezcan en el CONTEXTO LEGAL
8. Cada sección debe tener 5-12 bloques sustantivos (excepto encabezado/firma)
9. Usa placeholders entre corchetes: [NOMBRE_DEMANDANTE], [FECHA_INGRESO], etc, si faltan datos

REGLA — HECHOS CON BOLD LEAD-IN (OBLIGATORIO):
   Cada "hecho" DEBE iniciar con una frase corta en BOLD (etiqueta temática)
   seguida de un punto y luego el desarrollo del hecho en texto normal.
   ✓ EJEMPLO CORRECTO:
       {"type": "hecho", "num": 1, "runs": [
         {"text": "Vínculo laboral.", "bold": true},
         {"text": " La señora MARÍA FERNANDA GUTIÉRREZ RAMÍREZ suscribió contrato individual de trabajo a término indefinido el 15 de febrero de 2018...", "bold": false}
       ]}
   ✗ MAL: {"type": "hecho", "num": 1, "runs": [{"text": "La señora suscribió contrato..."}]}  (sin lead-in)
   Etiquetas típicas: "Vínculo laboral.", "Despido.", "Salario.", "Acoso laboral.", "Cuantía.", etc.

REGLA — PRETENSIONES CON VERBO EN MAYÚSCULAS (OBLIGATORIO):
   Cada "pretension" DEBE iniciar con el verbo procesal en MAYÚSCULAS Y BOLD,
   seguido del desarrollo de la pretensión.
   ✓ EJEMPLO CORRECTO:
       {"type": "pretension", "ord": "PRIMERA", "kind": "declarativa", "runs": [
         {"text": "DECLARAR", "bold": true},
         {"text": " la existencia del contrato de trabajo a término indefinido celebrado entre...", "bold": false}
       ]}
   Verbos válidos: DECLARAR (declarativas), CONDENAR / ORDENAR / DISPONER (de condena).
   El campo "ord" debe ser ordinal en MAYÚSCULAS: PRIMERA, SEGUNDA, TERCERA, CUARTA, QUINTA, SEXTA, SÉPTIMA, OCTAVA, NOVENA, DÉCIMA, etc.
   El campo "kind" debe coincidir con el verbo: DECLARAR→"declarativa", CONDENAR/ORDENAR→"condena".

REGLA — SUBSECCIONES NUMERADAS (RECOMENDADO):
   Dentro de secciones con varios bloques temáticos (PRETENSIONES, FUNDAMENTOS DE DERECHO, PRUEBAS),
   usa "subsection" con numeración arábiga jerárquica:
   ✓ {"type": "subsection", "number": "3.1", "text": "Pretensiones principales declarativas"}
   ✓ {"type": "subsection", "number": "3.2", "text": "Pretensiones principales de condena"}
   ✓ {"type": "subsection", "number": "3.3", "text": "Pretensiones subsidiarias"}
   ✓ {"type": "subsection", "number": "4.1", "text": "Estabilidad ocupacional reforzada por razones de salud"}
   ✓ {"type": "subsection", "number": "7.1", "text": "Documentales"}
   ✓ {"type": "subsection", "number": "7.2", "text": "Testimoniales"}

REGLA — TABLA DE LIQUIDACIÓN (3 COLUMNAS ESTÁNDAR):
   Cuando la sección sea LIQUIDACIÓN o CÁLCULOS y haya múltiples conceptos a cuantificar,
   emite UNA SOLA tabla con EXACTAMENTE 3 columnas y has_total_row=true:
   ✓ EJEMPLO:
       {"type": "table",
        "header": ["CONCEPTO", "BASE / FÓRMULA", "VALOR APROXIMADO (COP)"],
        "rows": [
          ["Cesantías 2018-2025 (Art. 249 CST)", "1 mes salario × 7,79 años", "$37.781.500"],
          ["Intereses sobre cesantías 12% EA", "Cesantías × 12% × años", "$4.500.000"],
          ...
          ["TOTAL APROXIMADO", "Suma de los anteriores", "$95.000.000"]
        ],
        "has_total_row": true}
   NUNCA 4 ni 5 columnas. El total SIEMPRE en la última fila con etiqueta "TOTAL ..." en mayúsculas.

REGLA — ENCABEZADO FORENSE (sección key="encabezado"):
   El encabezado se compone de paragraphs centrados (uno por línea) seguidos de un bloque
   de referencia justificado con etiquetas en bold y un párrafo de comparecencia del apoderado:
   ✓ {"type": "paragraph", "align": "center", "runs": [{"text": "Señor", "bold": true}]}
   ✓ {"type": "paragraph", "align": "center", "runs": [{"text": "JUEZ LABORAL DEL CIRCUITO DE BOGOTÁ D.C.", "bold": true}]}
   ✓ {"type": "paragraph", "align": "center", "runs": [{"text": "(REPARTO)", "bold": true}]}
   ✓ {"type": "blank"}
   ✓ {"type": "paragraph", "align": "justify", "runs": [
        {"text": "Referencia:", "bold": true},
        {"text": " DEMANDA ORDINARIA LABORAL DE MAYOR CUANTÍA", "bold": false}
      ]}
   ✓ Otros 3 paragraphs con etiquetas "Demandante:", "Demandado:", "Asunto:" en bold.
   ✓ {"type": "paragraph", "align": "justify", "runs": [
        {"text": "[NOMBRE_APODERADO]", "bold": true},
        {"text": ", mayor de edad, vecina/o de Bogotá D.C., identificado/a con C.C. N.° __________ de _________, abogado/a en ejercicio, portadora/or de la T.P. N.° __________ del C.S.J., obrando en calidad de apoderado/a judicial de ", "bold": false},
        {"text": "[NOMBRE_DEMANDANTE]", "bold": true},
        {"text": ", conforme al poder que se adjunta, respetuosamente formulo la presente DEMANDA ORDINARIA LABORAL DE MAYOR CUANTÍA contra ", "bold": false},
        {"text": "[NOMBRE_DEMANDADA]", "bold": true},
        {"text": ", con base en los siguientes hechos, pretensiones y fundamentos.", "bold": false}
      ]}

REGLA — FIRMA DE APODERADO (sección key="firma"):
   La firma estándar colombiana incluye fórmula de cierre y bloque de identificación:
   ✓ {"type": "paragraph", "align": "justify", "runs": [{"text": "Del Señor Juez,"}]}
   ✓ {"type": "blank"}
   ✓ {"type": "firma",
        "ciudad_fecha": "Bogotá D.C., [FECHA]",
        "nombre": "[NOMBRE_APODERADO]",
        "tp": "__________ del C.S.J.",
        "cc": "__________ de _________",
        "email": "[email]", "telefono": "[telefono]"}

REGLA CRÍTICA — NORMAS COMO BLOQUE TIPADO (OBLIGATORIO):
   ✗ MAL: {"type": "paragraph", "runs": [{"text": "Conforme al artículo 64 del CST, se debe..."}]}
   ✓ BIEN (dos bloques separados):
       1) {"type": "norma_citada", "norma": "Art. 64 CST",
           "contenido": [{"text": "El empleador que despida sin justa causa..."}], "verified": false}
       2) {"type": "paragraph", "runs": [{"text": "Conforme a la norma citada, se debe..."}]}

   Cada vez que menciones una norma específica (Art. X de Y, Ley N/AAAA, Decreto N/AAAA)
   DEBES emitir un bloque norma_citada separado ADEMÁS del párrafo de contexto.

REGLA CRÍTICA — JURISPRUDENCIA TIPADA (OBLIGATORIO CITAR):
   Cuando hay precedente relevante DEBES citarlo como bloque tipado:
   ✓ {"type": "jurisprudencia", "id": "SL1430-2022", "mp": "Iván Mauricio Lenis Gómez",
      "corte": "CSJ Sala Laboral", "fecha": "2022",
      "ratio": [{"text": "cuando el empleador opta por terminar unilateralmente..."}], "verified": false}

   PRIORIDAD: usa primero la jurisprudencia del CONTEXTO LEGAL (RAG hunter).
   Si necesitas más precedentes que NO están en el contexto, puedes citarlos —
   el agente tiene un VERIFICADOR EN TIEMPO REAL que va a validar contra:
     - BD interna de jurisprudencia
     - Live fetch a Corte Constitucional (corteconstitucional.gov.co)
     - Live fetch a CSJ (cortesuprema.gov.co)
     - Live fetch a Consejo de Estado (consejodeestado.gov.co)
     - Web search restricted a dominios oficiales
     - SUIN Senado (leyes, decretos)
   Cada cita queda con badge ✅ verified o ❌ sospechosa según la validación real.

   Por lo tanto: CITA con confianza precedentes que conozcas con CERTEZA
   (sentencias hito, doctrina pacífica). El verifier dirá si son reales.

   IDs estables conocidos para Colombia:
   - T-760/2008 M.P. Manuel José Cepeda Espinosa (derecho a la salud)
   - C-016/1998 M.P. Fabio Morón Díaz (contrato realidad Art. 24 CST)
   - C-1507/2000 M.P. José Gregorio Hernández Galindo (estabilidad reforzada)
   - SU-449/2020 M.P. Diana Fajardo Rivera (estabilidad reforzada salud)
   - Sentencias SL- de la Sala Laboral CSJ sobre indemnización Art. 64

REGLA — CÁLCULOS NUMÉRICOS:
   Si el CONTEXTO incluye "CÁLCULOS DETERMINÍSTICOS" (de calc/laboral.py), USA esos números
   EXACTOS en los bloques calc_step. NO los inventes. Ejemplo:
       Si conceptos.indemnizacion_art_64.valor = 12708333, emitir:
       {"type": "calc_step", "label": "Indemnización Art. 64 CST", "formula": "...",
        "aplicacion": "$2.500.000/30 × 152.5 días", "total": "$12.708.333"}

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
    verified_citations: list[dict] | None = None,  # M19.8: citas pre-verificadas
) -> AsyncIterator[Block]:
    """Genera bloques tipados para una sección. Yield Block uno por uno.

    M19.8: `verified_citations` permite pasar citas ya verificadas por el
    VerificationAgent (con fuente_url, vigencia, correcciones del Judge)
    para que el LLM las use literalmente sin "inventar" o citar mal.
    """
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

    # M19.8: PRIORIDAD MÁXIMA — citas verificadas inyectadas
    if verified_citations:
        ctx_parts.append("\n=== CITAS PRE-VERIFICADAS PARA ESTA SECCIÓN ===")
        ctx_parts.append("USA EXACTAMENTE ESTAS CITAS (no inventes otras). Si el Judge sugirió corrección, usa la corregida:")
        for vc in verified_citations[:15]:
            ref = vc.get("ref", "?")
            url = vc.get("fuente_url", "")
            estado = vc.get("estado", "?")
            sugg = vc.get("suggested_correction")
            note = vc.get("legal_note")
            line = f"  - {ref}  [{estado}]"
            if url:
                line += f"  → {url}"
            if sugg:
                line += f"  ⚠ SUSTITUIR POR: {sugg}"
            if note:
                line += f"  ⓘ {note}"
            ctx_parts.append(line)
        ctx_parts.append("=== FIN CITAS VERIFICADAS ===\n")

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
