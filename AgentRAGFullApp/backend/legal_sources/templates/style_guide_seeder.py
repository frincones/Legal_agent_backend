"""Style guide seeder · base CO + per-materia overlays.

Produces 12 documents that are ingested as `documents` rows with
`doc_type='style_guide'`, then chunked + embedded so the multi-agent
critic + judge can retrieve relevant style rules per generation.

The content here is the v1 baseline. Future iterations refine these
based on `feedback_learner` analysis of lawyer edits (Sprint 4 / post-MVP).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StyleGuideDoc:
    """A single style guide entry · base or materia overlay."""
    slug: str                  # unique id, e.g. 'style_guide_co_base' or 'style_guide_co_laboral'
    title: str
    materia: str               # 'base' | one of materia_legal enum values
    content_md: str


def _base_co_style_guide() -> StyleGuideDoc:
    return StyleGuideDoc(
        slug="style_guide_co_base",
        title="Guía de estilo · base Colombia",
        materia="base",
        content_md="""# Guía de estilo · LexAI Colombia (base)

Reglas universales para todo documento legal generado en jurisdicción colombiana.
Las guías por materia (laboral, civil, etc.) **complementan**, nunca contradicen
estas reglas base.

## 1. Lenguaje y tono

- **Formal pero claro.** El lenguaje jurídico colombiano es preciso, no ornamental.
  Preferir oraciones cortas y verbos en voz activa cuando sea posible.
- **Tratamiento de cortesía:**
  - Jueces de la República: `Honorable Magistrado/a` (Corte) · `Señor/a Juez`
    (juzgados de instancia).
  - Funcionarios administrativos: `Señor/a` + cargo.
  - Contrapartes: `el accionado`, `la demandada`, `el contratante`.
- **Sin coloquialismos** ni anglicismos innecesarios. Si una expresión inglesa
  no tiene equivalente jurídico claro (ej. *due diligence*), usarla en itálicas.

## 2. Estructura general de escritos judiciales

Orden estándar (con variaciones por tipo procesal):

1. **Encabezado / Destinatario** — autoridad ante la que se presenta.
2. **Identificación de las partes** — nombre, identificación, domicilio,
   apoderado y TP.
3. **Asunto / Tipo de acción** — una línea con el tipo procesal.
4. **Hechos** — numerados (I, II, III…), redactados en pretérito, una
   afirmación por numeral.
5. **Pretensiones / Peticiones** — numeradas, determinables, congruentes con
   los hechos.
6. **Fundamentos de derecho** — bloque normativo + jurisprudencial.
7. **Pruebas** — documentales, testimoniales, periciales.
8. **Anexos** — referenciados desde el texto en mayúscula sostenida.
9. **Notificaciones** — dirección física + correo + número de despacho.
10. **Lugar, fecha y firma.**

## 3. Citación de normas

Formato canónico colombiano:

- Leyes: `Ley 1564 de 2012, artículo 82, numeral 4`. (NO usar APA ni ICONTEC.)
- Decretos: `Decreto 2591 de 1991, artículo 8`.
- Constitución: `artículo 86 de la Constitución Política` o `C.P. art. 86`.
- Códigos: `CGP art. 82` · `CST art. 64` · `CC art. 1602` · `CPACA art. 138`.
- Acuerdos / Resoluciones: especificar entidad expedidora.

**Vigencia obligatoria:** toda norma citada debe estar vigente al momento de
redacción. Si una norma fue derogada, citar la vigente equivalente y, si es
pertinente, mencionar la derogación con `derogado por …`.

## 4. Citación de jurisprudencia

- Corte Constitucional: `Sentencia T-XXX/AAAA, M.P. NOMBRE`.
- Corte Suprema de Justicia (Sala Laboral / Civil / Penal): `CSJ Sala XX,
  Sentencia SL-XXXX-AAAA, M.P. NOMBRE`.
- Consejo de Estado: `Consejo de Estado, Sección XX, Sentencia AAAA-XXXX
  del DD/MM/AAAA, C.P. NOMBRE`.

Citar siempre que sea jurisprudencia consolidada (no aislada). Preferir
sentencias de los últimos 3 años cuando exista.

## 5. Formato de fechas y montos

- Fechas: `15 de marzo de 2026` (forma extendida en cuerpo de escritos);
  `2026-03-15` en metadatos.
- Montos: `$10.500.000` (separador miles con punto · sin espacios) · agregar
  equivalente en SMMLV cuando aplique: `$10.500.000 (6,48 SMMLV de 2026)`.

## 6. Anexos y pruebas

- Cada anexo se rotula `ANEXO N° X`, en mayúscula sostenida, y se referencia
  desde el cuerpo del escrito.
- En la sección de pruebas: indicar tipo (documental, testimonial, pericial)
  y propósito probatorio breve.

## 7. Cierre estándar

```
Atentamente,

[Firma]
NOMBRE COMPLETO
C.C. XXXXXX
T.P. XXXXX del C.S. de la J.
Correo: xxx@xxx.com
Dirección de notificación: xxxxx
```

## 8. Prohibiciones absolutas

- ❌ No usar lenguaje sexista (preferir `quien suscribe`, `la persona`).
- ❌ No mezclar primera y tercera persona en el mismo escrito.
- ❌ No incluir opiniones personales en `Hechos`.
- ❌ No omitir la firma electrónica o física al final.
- ❌ No usar emojis ni signos no convencionales.

## 9. Cuando el agente debe DETENERSE y consultar al abogado

- Hay datos faltantes que no pueden inferirse del expediente.
- Hay conflicto entre fechas, montos o partes.
- La cuantía supera el umbral configurado en el playbook de la firma.
- Una norma citada está derogada y no existe equivalente claro.
- El escrito involucra firma electrónica externa, envío a juzgado o cualquier
  acción irreversible.
""",
    )


def _overlay(slug: str, title: str, materia: str, body_md: str) -> StyleGuideDoc:
    return StyleGuideDoc(slug=slug, title=title, materia=materia, content_md=body_md)


def all_style_guides() -> list[StyleGuideDoc]:
    """Return the 12 documents (1 base + 11 materia overlays)."""
    return [
        _base_co_style_guide(),
        _overlay(
            "style_guide_co_laboral",
            "Guía de estilo · Laboral (CO)",
            "laboral",
            "# Overlay Laboral · CO\n\n"
            "## Citación específica\n"
            "- CST: forma `CST art. 64` (sin paréntesis ni decreto madre).\n"
            "- Ley 100/93 para seguridad social.\n"
            "- Sentencias Sala Laboral CSJ formato `SL-XXXX-AAAA`.\n\n"
            "## Estructura típica demanda laboral\n"
            "1. Identificación demandante (con CC y EPS si aplica)\n"
            "2. Identificación demandado (con NIT y representante legal)\n"
            "3. Cuantía estimada (Art. 25 CGP · expresada en SMMLV)\n"
            "4. Hechos cronológicos (fecha ingreso, salario, terminación)\n"
            "5. Pretensiones (reintegro, indemnización, prestaciones, intereses)\n"
            "6. Fundamentos (CST + jurisprudencia Sala Laboral CSJ)\n"
            "7. Pruebas (contrato, comprobantes, paz y salvo)\n\n"
            "## Cifras laborales clave (verificar vigencia)\n"
            "- SMMLV 2026: $1.620.000 · Auxilio transporte 2026.\n"
            "- Prestaciones: cesantías 8.33% · intereses cesantías 12% anual.\n"
            "- Vacaciones: 15 días hábiles por año.\n",
        ),
        _overlay(
            "style_guide_co_civil",
            "Guía de estilo · Civil (CO)",
            "civil",
            "# Overlay Civil · CO\n\n"
            "## Marco normativo principal\n"
            "- Código Civil (CC) · Código General del Proceso (CGP · Ley 1564/2012).\n"
            "- Cuantía mínima conforme al CGP para selección de juez competente.\n\n"
            "## Demanda ejecutiva\n"
            "- Identificar título ejecutivo · vencido · contiene obligación clara,\n"
            "  expresa y exigible.\n"
            "- Mandamiento de pago según el art. 430 CGP.\n"
            "- Medidas cautelares: solicitar expresamente con caución.\n\n"
            "## Contratos\n"
            "- Cláusulas obligatorias: objeto, precio, plazo, lugar, ley aplicable.\n"
            "- Cláusula penal: monto razonable · no abusiva.\n",
        ),
        _overlay(
            "style_guide_co_mercantil",
            "Guía de estilo · Mercantil (CO)",
            "mercantil",
            "# Overlay Mercantil · CO\n\n"
            "## Marco normativo\n"
            "- Código de Comercio (CCom).\n"
            "- Ley 1258/2008 (SAS).\n\n"
            "## Constitución SAS\n"
            "- Documento privado · registro en Cámara de Comercio.\n"
            "- Cláusulas mínimas: razón social + SAS, objeto, capital, accionistas.\n",
        ),
        _overlay(
            "style_guide_co_comercial",
            "Guía de estilo · Comercial (CO)",
            "comercial",
            "# Overlay Comercial · CO\n\n"
            "Reglas iguales a mercantil con énfasis en contratos mercantiles\n"
            "atípicos (distribución, franquicia, agencia, suministro).\n",
        ),
        _overlay(
            "style_guide_co_penal",
            "Guía de estilo · Penal (CO)",
            "penal",
            "# Overlay Penal · CO\n\n"
            "## Marco\n"
            "- Código Penal (Ley 599/2000) · Código de Procedimiento Penal\n"
            "  (Ley 906/2004).\n\n"
            "## Denuncia\n"
            "- Identificación denunciante · relación clara y cronológica de hechos.\n"
            "- Tipificación tentativa (la Fiscalía decide en última instancia).\n",
        ),
        _overlay(
            "style_guide_co_familiar",
            "Guía de estilo · Familia (CO)",
            "familiar",
            "# Overlay Familia · CO\n\n"
            "## Marco\n"
            "- Código de la Infancia y Adolescencia (Ley 1098/2006).\n"
            "- Código Civil + leyes específicas (divorcio, alimentos, custodia).\n\n"
            "## Demanda de alimentos\n"
            "- Identificar alimentante y alimentario · vínculo.\n"
            "- Capacidad económica probada del alimentante.\n",
        ),
        _overlay(
            "style_guide_co_administrativo",
            "Guía de estilo · Administrativo (CO)",
            "administrativo",
            "# Overlay Administrativo · CO\n\n"
            "## Marco\n"
            "- CPACA (Ley 1437/2011).\n"
            "- Plazos: recurso reposición 10 días · apelación 10 días.\n\n"
            "## Nulidad y restablecimiento del derecho\n"
            "- Caducidad: 4 meses (regla general).\n"
            "- Identificar acto administrativo demandado con fecha y autoridad.\n",
        ),
        _overlay(
            "style_guide_co_constitucional",
            "Guía de estilo · Constitucional / Tutela (CO)",
            "constitucional",
            "# Overlay Constitucional · CO\n\n"
            "## Tutela (Const. art. 86 · Decreto 2591/91)\n"
            "- Cualquier juez con jurisdicción territorial es competente.\n"
            "- Plazo: 10 días para decidir.\n"
            "- Estructura recomendada:\n"
            "  1. Identificación accionante\n"
            "  2. Identificación accionado (con NIT y dirección notificación)\n"
            "  3. Derechos fundamentales vulnerados (al menos 1 · cite art.)\n"
            "  4. Hechos numerados cronológicamente\n"
            "  5. Pretensiones específicas y determinables\n"
            "  6. Pruebas\n"
            "  7. Juramento de no haber presentado otra tutela por los mismos hechos\n"
            "  8. Anexos referenciados\n",
        ),
        _overlay(
            "style_guide_co_fiscal",
            "Guía de estilo · Tributario (CO)",
            "fiscal",
            "# Overlay Tributario · CO\n\n"
            "## Marco\n"
            "- Estatuto Tributario (Decreto 624/1989 con reformas).\n"
            "- Recurso de reconsideración ante DIAN · plazo 2 meses.\n",
        ),
        _overlay(
            "style_guide_co_seguridad_social",
            "Guía de estilo · Seguridad Social (CO)",
            "seguridad_social",
            "# Overlay Seguridad Social · CO\n\n"
            "## Marco\n"
            "- Ley 100/93 (régimen general).\n"
            "- Pensión vejez: 1300 semanas (Ley 797/2003 art. 9) · edad: 62 H / 57 M.\n",
        ),
        _overlay(
            "style_guide_co_otro",
            "Guía de estilo · Otras materias (CO)",
            "otro",
            "# Overlay genérico · materias no cubiertas\n\n"
            "Aplica la guía base. Si una materia se vuelve frecuente,\n"
            "promoverla a overlay dedicado.\n",
        ),
    ]
