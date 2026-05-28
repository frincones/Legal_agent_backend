"""Stage 3: Block Generator — genera bloques tipados por sección con streaming.

En M1 genera Block[] sintetizados a partir del LLM output en formato JSON.
M3 conectará calculadora + hunters para enriquecer el contexto.

M19.24.D — Soporte UNIVERSAL para cualquier documento legal colombiano
(no solo demandas judiciales).

Feature flag `BLOCK_GENERATOR_UNIVERSAL`:
  - true: usa SYSTEM_PROMPT_UNIVERSAL + playbooks del structure_recipe
  - false (default): usa SYSTEM_PROMPT_GENERATOR clásico (demanda judicial)
  - Por doc_type: BLOCK_GENERATOR_UNIVERSAL_DOC_TYPES env var con CSV
    de doc_types (e.g. "poder_especial,contrato_arrendamiento") activa
    el universal SOLO para esos doc_types, dejando las demandas con
    el prompt clásico hasta que se validen.

Diseño:
- Por cada sección del plan, invoca gpt-4o (o gpt-4o-mini para encabezado/firma)
- LLM devuelve JSON con array de blocks
- Stream a nivel de bloque: emitimos block_emit cuando llega cada bloque
"""
from __future__ import annotations

import json
import logging
import os
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


# ============================================================
# M19.24.D — Universal block generator prompt + feature flag
# ============================================================

def _is_universal_enabled(doc_type: str | None = None) -> bool:
    """Decide si usar SYSTEM_PROMPT_UNIVERSAL o el clásico.

    Lógica:
      1. Si BLOCK_GENERATOR_UNIVERSAL=true → universal SIEMPRE
      2. Si BLOCK_GENERATOR_UNIVERSAL_DOC_TYPES contiene el doc_type → universal
      3. Sino → clásico (demanda)
    """
    if os.getenv("BLOCK_GENERATOR_UNIVERSAL", "false").lower() in ("1", "true", "yes"):
        return True
    if doc_type:
        whitelist = os.getenv("BLOCK_GENERATOR_UNIVERSAL_DOC_TYPES", "")
        if whitelist:
            normalized = {x.strip().lower() for x in whitelist.split(",") if x.strip()}
            if doc_type.strip().lower() in normalized:
                return True
    return False


SYSTEM_PROMPT_UNIVERSAL = """Eres un ABOGADO SENIOR colombiano con 20+ años de experiencia redactando
TODO tipo de documento legal: demandas, poderes, contratos, escrituras,
estatutos, actas, conceptos, derechos de petición, declaraciones extrajuicio.

Tu OUTPUT es JSON con un array de BLOQUES TIPADOS. Cada bloque tiene:
- "type": tipo del bloque
- campos específicos por tipo

TIPOS DISPONIBLES (los mismos para CUALQUIER documento):
{
  "title":           {"text": "...", "level": 0|1|2},
  "section_heading": {"roman": "I"|null, "text": "...", "section_key": "..."},
  "subsection":      {"number": "1.1"|"PRIMERA"|"Art. 1", "text": "..."},
  "paragraph":       {"runs": [{"text": "...", "bold": false, "italic": false, "underline": false}], "align": "justify"|"left"|"center"|"right"},
  "hecho":           {"num": 1, "runs": [...]},
  "pretension":      {"ord": "PRIMERA", "kind": "declarativa|condena|general", "runs": [...]},
  "norma_citada":    {"norma": "Art. 2142 CC", "contenido": [...], "verified": false},
  "jurisprudencia":  {"id": "T-622/2015", "mp": "...", "corte": "...", "ratio": [...]},
  "table":           {"header": [...], "rows": [[...]], "has_total_row": true|false},
  "list_item":       {"kind": "anexo|documental|testimonial|pericial|generic", "num": "1", "runs": [...]},
  "juramento":       {"text": "...", "norma_ref": "Art. 206 CGP"},
  "firma":           {"cierre_tipo": "<del recipe>", "nombre": "...", "ciudad_fecha": "", ...},
  "blank":           {}
}

═══════════════════════════════════════════════════════════════
CÓMO TRABAJAR (M19.24 universal)
═══════════════════════════════════════════════════════════════

El sistema te pasará en CADA llamada:

  DOC_TYPE + DOCUMENT_FAMILY + ENCABEZADO_TIPO + CIERRE_TIPO + NUMERACION_ESTILO
  + SECCIÓN ACTUAL + PLAYBOOK específico de esa sección
  + BLOQUES PREVIOS (ya emitidos en secciones anteriores) — anti-repeat

REGLA #1 — RESPETA LA NATURALEZA DEL DOCUMENTO:
  - Si DOCUMENT_FAMILY es notarial_poder o contractual_* → NO emitas hechos
    ni pretensiones ni juramento (eso es de demandas judiciales).
  - Si DOCUMENT_FAMILY es judicial_demanda → SÍ emite hechos/pretensiones.
  - Si DOCUMENT_FAMILY es corporate_estatutos → numera por artículos (Art. 1, 2, 3).
  - Si DOCUMENT_FAMILY es contractual_* → numera por cláusulas (PRIMERA, SEGUNDA).
  - Si DOCUMENT_FAMILY es notarial_poder → numera por cláusulas (PRIMERA, SEGUNDA).
  - Si DOCUMENT_FAMILY es corporate_acta → numeración simple (1, 2, 3).

REGLA #2 — RESPETA EL PLAYBOOK DE LA SECCIÓN:
  Cada sección viene con instrucciones específicas. Síguelas literalmente.
  El playbook reemplaza al SYSTEM_PROMPT clásico — confía en lo que dice.

REGLA #3 — ANTI-REPETICIÓN CROSS-SECTION:
  Te paso los bloques YA EMITIDOS de secciones previas. NO repitas el
  encabezado, partes, comparecencia, ni nada que ya esté redactado.
  Tu output es SOLO lo que falta de la SECCIÓN ACTUAL.

REGLA #4 — ENCABEZADO Y CIERRE SE EMITEN UNA SOLA VEZ:
  - El "encabezado" como sección emite los párrafos de saludo y referencia.
    En las demás secciones NO repitas el saludo.
  - El "firma" como sección emite el bloque firma con el cierre_tipo apropiado.
    En las demás secciones NO emitas el bloque firma.

REGLA #5 — PLACEHOLDERS VISIBLES:
  Si necesitas un dato no disponible, usa formato [NOMBRE_DESCRIPTIVO_MAYUS]
  con corchetes. El renderer lo resaltará en amarillo. Ejemplo:
  [RAZON_SOCIAL], [NIT], [FECHA_MATRIMONIO], [TOPE_MAXIMO_CUANTIA].

REGLA #6 — FIRMA: usa el cierre_tipo del recipe.
  El bloque firma debe llevar el campo "cierre_tipo" idéntico al del recipe.
  No improvises. El renderer del docx escoge la variante correcta.
  Para firmas con múltiples partes (notarial, contractual, corporativa),
  usa el campo "parties" como array de {rol, nombre, cc, cargo, razon_social}.

REGLA #7 — CITAS NORMATIVAS:
  Cuando cites una norma usa un bloque norma_citada SEPARADO con su contenido.
  NO mezcles citas dentro de párrafos en prosa.
  Usa el fundamento_normativo del legal_classifier como guía.

REGLA #8 — PROHIBIDO:
  - Emitir caracteres ✓ ✗ ⚠ en el texto del documento (son metadata)
  - Inventar normas o jurisprudencia que no aparezcan en el contexto
  - Emitir bloque firma fuera de la sección con section_key="firma"
  - Repetir contenido que ya esté en BLOQUES PREVIOS
  - Usar numeración romana en contratos o poderes (van con cláusulas ordinales)
  - Emitir hechos/pretensiones en documentos no demanda

═══════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════

JSON estricto:
{
  "blocks": [
    {"type": "paragraph", "runs": [{"text": "..."}]},
    {"type": "subsection", "number": "PRIMERA", "text": "Objeto del Poder"},
    ...
  ]
}

NO devuelvas markdown ni explicaciones. SOLO el JSON."""


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
  "juramento":       {"text": "...", "norma_ref": "Art. 206 CGP" /* ver tabla por doc_type abajo */},
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

REGLAS ESTRICTAS — PROHIBICIONES ABSOLUTAS (M19.15):

A. PROHIBIDO — Caracteres de validación dentro del documento.
   NUNCA emitas ✓, ✗, ⚠, ⓘ, ❌, ✅ ni "verified" / "verificada" en ningún texto de un bloque.
   El estado de verificación es METADATA INTERNA del sistema, NO va al documento final.
   ✗ MAL: {"text": "· Art. 239 CST ✓"}
   ✗ MAL: {"text": "Ley 1010/2006 ✓ verificada"}
   ✓ BIEN: {"text": "Art. 239 CST"} (sin marcas)

B. PROHIBIDO — Firma fuera de la sección "firma".
   El bloque "firma" y los paragraphs "Del Señor Juez," / "Atentamente,"
   SOLO pueden aparecer en la sección con section_key="firma" (la ÚLTIMA).
   En cualquier otra sección (encabezado, partes, hechos, pretensiones, fundamentos,
   liquidacion, pruebas, anexos, notificaciones, etc.), está PROHIBIDO emitir bloques
   "firma" o paragraphs que contengan "Atentamente", "T.P. del C.S.J.", "_______" (raya
   de firma), o el nombre del apoderado como firma.
   Si la sección que se te pide NO es "firma", terminas tu output ANTES de la firma.

C. PROHIBIDO — Cross-section bleeding.
   La sección que se te pide se identifica con SECCIÓN: N. TÍTULO (key=...) y por la
   INSTRUCCIÓN ESPECÍFICA. Genera ÚNICAMENTE contenido propio de esa sección.
   Si te piden "notificaciones", emite SOLO direcciones para notificar a las partes.
   NUNCA repitas hechos, pretensiones, fundamentos ni liquidación en otra sección.
   Si te piden "pruebas", NO emitas anexos. Si te piden "anexos", NO emitas pruebas.

D. PROHIBIDO — Subsection con numeración fuera de la sección actual.
   Si la sección actual tiene roman "VIII", las subsections deben numerarse "8.1",
   "8.2", "8.3"... NUNCA reuses "1.1" o "2.1" en secciones distintas a I o II.
   Mapeo: I→1.x, II→2.x, III→3.x, IV→4.x, V→5.x, VI→6.x, VII→7.x, VIII→8.x, IX→9.x.

E. PROHIBIDO — M.P. inventado o "N/A".
   Si NO conoces el Magistrado Ponente de una sentencia con certeza, OMITE el campo "mp"
   o NO emitas el bloque "jurisprudencia". NUNCA pongas "N/A", "Desconocido", "(sin datos)"
   ni inventes un nombre.

F. PROHIBIDO — Repetir info en bullets con normas ya citadas como bloques.
   Si emites un bloque "norma_citada" para Art. 239 CST, NO emitas adicionalmente
   un paragraph con "· Art. 239 CST" o "- Art. 239 CST". La cita ya está representada.

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

REGLA — JURAMENTO POR ÁREA DEL DERECHO (M19.19.C, OBLIGATORIO):
Cada jurisdicción tiene su norma de juramento específica. NUNCA uses la norma
equivocada (sería un error procesal grave). Mapeo obligatorio:

  doc_type / área                            norma_ref del JuramentoBlock
  ─────────────────────────────────────────  ─────────────────────────────────────
  CIVIL ordinaria, contratos, ejecutiva,     "Art. 206 CGP (Ley 1564/2012)"
    declarativos, pertenencia (usucapión),
    responsabilidad civil
  LABORAL ordinaria, ejecutiva laboral       "Art. 28 CPTSS"
  FAMILIA (divorcio, alimentos, custodia)    "Art. 206 CGP (Ley 1564/2012)"
  ADMINISTRATIVO (nulidad, restablecimiento) "Art. 167 CPACA (Ley 1437/2011)"
  PENAL (denuncias, querellas)               "Art. 269 CPP (Ley 906/2004)"
  TUTELA (acción de tutela)                  NO requiere juramento separado (Art. 86 CN)
  POPULAR / GRUPO                            "Art. 18 Ley 472/1998"

Antes de emitir el JuramentoBlock, identifica la jurisdicción del documento por
DOC TYPE del contexto y elige norma_ref de la tabla. Si el doc_type es TUTELA,
NO emitas juramento (es una protesta solemne implícita en el escrito).

REGLA — FIRMA DE APODERADO (sección key="firma"):
   La firma estándar colombiana incluye fórmula de cierre y bloque de identificación.
   Esta sección debe contener EXACTAMENTE estos bloques (en este orden) y nada más:
   ✓ {"type": "paragraph", "align": "justify", "runs": [{"text": "Del Señor Juez,"}]}
   ✓ {"type": "blank"}
   ✓ {"type": "firma",
        "ciudad_fecha": "",
        "nombre": "[NOMBRE_APODERADO]",
        "tp": "245.678",
        "cc": "52.345.678",
        "email": "[email_apoderado]",
        "telefono": "[telefono_apoderado]"}

   FORMATO ESTRICTO DE CAMPOS DE FIRMA:
   - "ciudad_fecha": dejar string VACÍO "" — el sistema rellena con ciudad y fecha actual.
     NO pongas "Bogotá D.C., [FECHA_INGRESO]" ni "[CIUDAD], [FECHA]" — solo "".
   - "tp": SOLO el número de tarjeta profesional (ej: "245.678"). NO incluyas "T.P. No.",
     "del C.S.J.", "T.P. ", ni puntos al inicio. El sistema añade los prefijos y sufijos.
     ✗ MAL: "tp": "T.P. 245.678 del C.S.J."
     ✓ BIEN: "tp": "245.678"
   - "cc": SOLO el número de cédula del APODERADO (NO del demandante). NO incluyas
     "C.C. No.", "de Bogotá", etc. Solo el número con separadores de miles.
     ✗ MAL: "cc": "[NOMBRE_DEMANDANTE]"
     ✗ MAL: "cc": "C.C. No. 52.345.678 de Bogotá"
     ✓ BIEN: "cc": "52.345.678"
   - "email" y "telefono": valores directos sin etiquetas ni "Email:" / "Tel.:".

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
    # M19.24.D — modo universal
    structure_recipe: dict | None = None,
    previous_blocks: list[dict] | None = None,
) -> AsyncIterator[Block]:
    """Genera bloques tipados para una sección. Yield Block uno por uno.

    M19.8: `verified_citations` permite pasar citas ya verificadas por el
    VerificationAgent (con fuente_url, vigencia, correcciones del Judge)
    para que el LLM las use literalmente sin "inventar" o citar mal.
    """
    model = _model_for_section(section_key)
    use_universal = _is_universal_enabled(doc_type)

    # Construir contexto enriquecido
    ctx_parts = [
        f"DOC TYPE: {doc_type}",
        f"SECCIÓN: {section_order}. {section_title} (key={section_key})",
        f"INTENT: {intent}",
        f"BRIEF: {brief}",
        f"DATOS EXTRAÍDOS: {json.dumps(extracted_data, ensure_ascii=False)}",
    ]

    # M19.24.D — metadata universal (siempre se incluye, pero solo si es modo universal afecta)
    if use_universal and structure_recipe:
        recipe_summary = {
            "document_family": structure_recipe.get("document_family"),
            "regimen_aplicable": structure_recipe.get("regimen_aplicable"),
            "naturaleza_acto": structure_recipe.get("naturaleza_acto"),
            "encabezado_tipo": structure_recipe.get("encabezado_tipo"),
            "cierre_tipo": structure_recipe.get("cierre_tipo"),
            "numeracion_estilo": structure_recipe.get("numeracion_estilo"),
            "requires_pretensiones": structure_recipe.get("requires_pretensiones"),
            "requires_hechos": structure_recipe.get("requires_hechos"),
            "requires_juramento": structure_recipe.get("requires_juramento"),
        }
        ctx_parts.append("RECIPE M19.24:\n" + json.dumps(recipe_summary, ensure_ascii=False, indent=2))

        # Playbook específico de esta sección (instrucciones curadas)
        playbooks = structure_recipe.get("playbooks") or {}
        section_playbook = playbooks.get(section_key) if isinstance(playbooks, dict) else None
        if section_playbook and isinstance(section_playbook, list):
            ctx_parts.append("PLAYBOOK DE LA SECCIÓN ACTUAL (sigue cada bullet):")
            for i, b in enumerate(section_playbook[:10], 1):
                ctx_parts.append(f"  {i}. {b}")

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

    # M19.24.D.3 — cross-section blocks completos (anti-repeat) en modo universal
    if use_universal and previous_blocks:
        # Resumen estructurado de cada bloque previo (block_id + type + preview)
        prev_lines = []
        total_chars = 0
        for pb in previous_blocks:
            if total_chars > 3500:  # token budget ~1000
                break
            try:
                preview = _block_text_preview_for_context(pb)
                bt = pb.get("block_type") or pb.get("type", "?")
                sk = pb.get("section_key", "?")
                line = f"  [{sk}/{bt}] {preview[:120]}"
                if line.strip():
                    prev_lines.append(line)
                    total_chars += len(line)
            except Exception:
                continue
        if prev_lines:
            ctx_parts.append(
                "\nBLOQUES YA EMITIDOS (NO los repitas, son contexto solamente):\n"
                + "\n".join(prev_lines[:60])
            )

    system_prompt_to_use = SYSTEM_PROMPT_UNIVERSAL if use_universal else SYSTEM_PROMPT_GENERATOR

    user_prompt = (
        "\n\n".join(ctx_parts)
        + "\n\nREDACTA AHORA SOLO ESTA SECCIÓN como JSON {\"blocks\": [...]}. NO incluyas título de sección (ya se renderiza aparte)."
    )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt_to_use},
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
            # M19.15.A.1 — no aceptar firma ni "Atentamente"/"Del Señor Juez" fuera de sección "firma"
            if section_key != "firma":
                bt = rb.get("type")
                if bt == "firma":
                    logger.info("dropping out-of-place firma block in section=%s", section_key)
                    continue
                if bt == "paragraph":
                    runs = rb.get("runs") or []
                    flat = " ".join(r.get("text", "") if isinstance(r, dict) else str(r) for r in runs).strip()
                    lower = flat.lower()
                    if (
                        "atentamente" in lower
                        or "del señor juez" in lower
                        or "_________________" in flat
                        or "t.p. del c.s.j" in lower
                    ):
                        logger.info("dropping signature-like paragraph in section=%s: %r", section_key, flat[:80])
                        continue
            block = _materialize_block(rb, doc_type=doc_type)
            if block is not None:
                yield block
        except Exception as e:
            logger.warning("block parsing failed for %s: %s", rb.get("type"), e)
            continue


# M19.15.A.2 — strip defensivo de caracteres de validación del output del LLM
_VALIDATION_MARKERS = ("✓", "✗", "⚠", "ⓘ", "❌", "✅", "✔", "❎")


# M19.19.C — mapa autoritativo doc_type → norma de juramento correcta
# Usado defensivamente cuando el LLM emite una norma equivocada (típico: "Art. 28
# CPTSS" pegado en demanda civil porque el system prompt antiguo lo tenía como
# ejemplo). El mapping cubre los doc_types más comunes en el registry de templates.
_JURAMENTO_NORMA_BY_DOC_TYPE: dict[str, str] = {
    # Civil — Art. 206 del Código General del Proceso
    "demanda_civil_ordinaria": "Art. 206 CGP (Ley 1564/2012)",
    "demanda_ejecutiva_singular": "Art. 206 CGP (Ley 1564/2012)",
    "demanda_pertenencia": "Art. 206 CGP (Ley 1564/2012)",
    "pertenencia": "Art. 206 CGP (Ley 1564/2012)",
    "demanda_responsabilidad_civil": "Art. 206 CGP (Ley 1564/2012)",
    "demanda_civil_responsabilidad_extracontractual": "Art. 206 CGP (Ley 1564/2012)",
    "contrato_compraventa": "Art. 206 CGP (Ley 1564/2012)",
    "contrato_arrendamiento": "Art. 206 CGP (Ley 1564/2012)",
    "promesa_compraventa": "Art. 206 CGP (Ley 1564/2012)",
    # Familia — también CGP
    "demanda_divorcio": "Art. 206 CGP (Ley 1564/2012)",
    "demanda_alimentos": "Art. 206 CGP (Ley 1564/2012)",
    "demanda_custodia": "Art. 206 CGP (Ley 1564/2012)",
    # Laboral — Art. 28 CPTSS
    "demanda_laboral_ordinaria": "Art. 28 CPTSS",
    "demanda_laboral": "Art. 28 CPTSS",
    "demanda_ejecutiva_laboral": "Art. 28 CPTSS",
    # Administrativo — Art. 167 CPACA
    "demanda_nulidad_restablecimiento": "Art. 167 CPACA (Ley 1437/2011)",
    "demanda_simple_nulidad": "Art. 167 CPACA (Ley 1437/2011)",
    "demanda_reparacion_directa": "Art. 167 CPACA (Ley 1437/2011)",
    # Constitucional / acciones — sin juramento separado
    "tutela": "",  # cadena vacía indica "omitir bloque juramento"
    "accion_tutela": "",
    "accion_popular": "Art. 18 Ley 472/1998",
    "accion_grupo": "Art. 18 Ley 472/1998",
}


def _juramento_norma_for_doc_type(doc_type: str | None) -> str | None:
    """Devuelve la norma autoritativa de juramento para el doc_type.

    Retorna:
      - str con norma_ref correcta si el doc_type está mapeado.
      - "" (string vacío) si el doc_type no requiere juramento (tutela).
      - None si el doc_type es desconocido (mantener lo que dijo el LLM).
    """
    if not doc_type:
        return None
    return _JURAMENTO_NORMA_BY_DOC_TYPE.get(doc_type.lower().strip())


def _strip_validation_markers(text: str) -> str:
    """Quita marcadores de validación (✓, ✗, etc.) y los espacios sobrantes."""
    if not text:
        return text
    s = text
    for m in _VALIDATION_MARKERS:
        s = s.replace(m, "")
    # limpiar "  " y espacios al final de líneas
    s = " ".join(s.split())
    return s


_PLACEHOLDER_RX = __import__("re").compile(r"\[[A-Z_][A-Z_0-9 ]*\]")


def _clean_ciudad_fecha(val: Any) -> str:
    """M19.15.A.4 — si trae placeholders o vacío, devolver string vacío para que
    el builder lo rellene con ciudad por defecto + fecha actual."""
    s = (str(val or "")).strip()
    if not s or _PLACEHOLDER_RX.search(s):
        return ""
    return s


def _clean_tp(val: Any) -> str:
    """M19.15.A.3 — extraer SOLO el número de TP. El builder añade el prefix/suffix."""
    s = (str(val or "")).strip()
    if not s:
        return ""
    # Quitar prefijos y sufijos típicos que el LLM mete por error
    import re
    s = re.sub(r"(?i)\bt\.?p\.?\s*(no\.?|n[º°]\.?)?\s*", "", s)
    s = re.sub(r"(?i)\s*del\s+c\.?\s*s\.?\s*j\.?\s*$", "", s)
    s = s.strip()
    # Si quedó vacío o solo un placeholder, dejar placeholder estándar
    if not s or _PLACEHOLDER_RX.search(s):
        return ""
    return s


def _clean_cc(val: Any) -> str | None:
    """M19.15.A.5 — extraer SOLO el número de C.C. del APODERADO.
    Rechaza placeholders tipo [NOMBRE_DEMANDANTE] que el LLM mete por error."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # Si el LLM puso un placeholder de nombre/demandante en lugar de cédula → drop
    if _PLACEHOLDER_RX.search(s):
        return None
    import re
    s = re.sub(r"(?i)\bc\.?\s*c\.?\s*(no\.?|n[º°]\.?)?\s*", "", s)
    s = re.sub(r"(?i)\s*de\s+[a-záéíóú\s]+\s*$", "", s)
    s = s.strip()
    return s or None


def _materialize_block(raw: dict, doc_type: str | None = None) -> Block | None:
    """Convierte dict crudo del LLM a un Block Pydantic. Defensive parsing.

    M19.19.C: corrige defensivamente el `norma_ref` del juramento según
    `doc_type` (un LLM despistado puede emitir Art. 28 CPTSS en demanda civil).
    """
    btype = raw.get("type")
    if not btype:
        return None

    # Helper para runs (con strip de marcadores de validación — M19.15.A.2)
    def _runs(field_val: Any) -> list[Run]:
        if isinstance(field_val, str):
            return [Run(text=_strip_validation_markers(field_val))]
        if isinstance(field_val, list):
            return [
                Run(
                    text=_strip_validation_markers(
                        r.get("text", "") if isinstance(r, dict) else str(r)
                    ),
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
            # M19.15.A.8 + M19.18.H — si M.P. es vacío/N/A/desconocido, NO descartar
            # automáticamente. Conservar con M.P. = "(por verificar)" si el ID parece
            # válido (formato T-/C-/SU-/SL-/SC-/CE-/etc.).
            mp_raw = (raw.get("mp") or "").strip()
            if mp_raw.lower() in ("", "n/a", "na", "desconocido", "sin datos", "(sin datos)", "?"):
                jid = (raw.get("id") or "").strip()
                import re as _re_jid
                id_looks_valid = bool(_re_jid.match(
                    r"^(T|C|SU|SL|SC|CE|AP|AC)[-\s]?\d{1,5}([-/]\d{2,4})?", jid, _re_jid.I
                ))
                if not id_looks_valid:
                    logger.info("dropping jurisprudencia: invalid id=%r and no M.P.", jid[:40])
                    return None
                logger.info("jurisprudencia kept with placeholder M.P.: id=%s", jid[:40])
                mp_raw = "(por verificar)"
            return JurisprudenciaBlock(
                block_id=bid, id=raw.get("id", ""), mp=mp_raw,
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
            # M19.19.C — corregir defensivamente norma_ref según doc_type
            llm_norma = (raw.get("norma_ref") or "").strip()
            authoritative = _juramento_norma_for_doc_type(doc_type)
            if authoritative is not None:
                # doc_type conocido: usar la norma autoritativa
                if authoritative == "":
                    # tutela u otro sin juramento → no emitir bloque
                    logger.info(
                        "dropping juramento block: doc_type=%r no requiere juramento",
                        doc_type,
                    )
                    return None
                if llm_norma and llm_norma != authoritative:
                    logger.info(
                        "juramento norma_ref corrected: doc_type=%s LLM=%r -> %r",
                        doc_type, llm_norma[:40], authoritative,
                    )
                final_norma = authoritative
            else:
                # doc_type desconocido: confiar en el LLM
                final_norma = llm_norma or None
            return JuramentoBlock(
                block_id=bid,
                text=raw.get("text", ""),
                norma_ref=final_norma,
            )
        if btype == "firma":
            # M19.15.A.3/4/5 — sanitización defensiva de campos
            # M19.24.D.4/6 — propagar cierre_tipo + parties + razon_social + nit + cargo
            return FirmaBlock(
                block_id=bid,
                ciudad_fecha=_clean_ciudad_fecha(raw.get("ciudad_fecha", "")),
                nombre=(raw.get("nombre") or "[NOMBRE_FIRMANTE]").strip(),
                tp=_clean_tp(raw.get("tp", "")),
                cc=_clean_cc(raw.get("cc")),
                email=raw.get("email"), telefono=raw.get("telefono"),
                cierre_tipo=raw.get("cierre_tipo"),
                parties=raw.get("parties") if isinstance(raw.get("parties"), list) else None,
                razon_social=raw.get("razon_social"),
                nit_sociedad=raw.get("nit_sociedad"),
                cargo=raw.get("cargo"),
                detalle_adicional=raw.get("detalle_adicional"),
            )
        if btype == "blank":
            return BlankBlock(block_id=bid)
    except Exception as e:
        logger.warning("Block materialization error for %s: %s", btype, e)
        return None

    logger.warning("Unknown block type: %s", btype)
    return None


def _block_text_preview_for_context(pb: dict) -> str:
    """M19.24.D.3 — extrae preview text de un bloque persistido (dict) para
    incluirlo como contexto cross-section en el prompt del LLM.

    Soporta block_data anidado (formato persistencia) y campos planos.
    """
    bd = pb.get("block_data") or pb
    bt = pb.get("block_type") or bd.get("type", "")
    if bt == "title":
        return bd.get("text", "")[:120]
    if bt == "section_heading":
        return f"§{bd.get('roman','')}. {bd.get('text','')}"[:120]
    if bt == "subsection":
        return f"{bd.get('number','')}. {bd.get('text','')}"[:120]
    if bt == "paragraph":
        runs = bd.get("runs") or []
        if isinstance(runs, list):
            return "".join(r.get("text", "") if isinstance(r, dict) else str(r) for r in runs)[:200]
    if bt == "hecho":
        runs = bd.get("runs") or []
        text = "".join(r.get("text", "") if isinstance(r, dict) else str(r) for r in runs)
        return f"hecho#{bd.get('num','?')}: {text[:120]}"
    if bt == "pretension":
        runs = bd.get("runs") or []
        text = "".join(r.get("text", "") if isinstance(r, dict) else str(r) for r in runs)
        return f"pretension {bd.get('ord','?')}: {text[:120]}"
    if bt == "list_item":
        runs = bd.get("runs") or []
        text = "".join(r.get("text", "") if isinstance(r, dict) else str(r) for r in runs)
        return f"item {bd.get('num','?')}: {text[:120]}"
    if bt == "norma_citada":
        return f"norma: {bd.get('norma','')}"[:120]
    if bt == "jurisprudencia":
        return f"juris: {bd.get('id','?')}"[:120]
    if bt == "firma":
        return f"firma: {bd.get('nombre','')}"[:80]
    return f"{bt}"[:60]
