---
name: docx
description: |
  Built-in skill que enseña a Claude a generar archivos .docx profesionales
  para LexAI usando la librería docx-js@9.5 (Node.js). Estilo basado en el
  patrón de Anthropic Skills: progressive disclosure.
version: 1.0.0
runtime: nodejs/20
library: docx@9.5
language: javascript
tier: builtin
allowed-tools:
  - require('docx')
disallowed:
  - require('fs')
  - require('http')
  - require('child_process')
  - require('os')
  - eval
  - Function
---

# Skill: docx (built-in renderer LexAI)

## Tu trabajo

Recibes:
1. Un **template SKILL.md** del marketplace (ej. `notarial_poder_especial_co`)
   con `Document Structure`, `Style Conventions`, `docx-js Style Hints`, etc.
2. Un **objeto `data`** con los placeholders del usuario (`{NOMBRE_PODERDANTE, CC_PODERDANTE, ...}`).
3. Un **prompt del usuario** describiendo qué redactar.

Debes producir **un único bloque JavaScript** que defina:

```javascript
async function build(data) {
  // ... construye y retorna new Document({...})
  return new Document({ /* ... */ });
}
```

**NO** uses `require`. Los constructores ya están en el scope global:
`Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
Table, TableRow, TableCell, WidthType, BorderStyle, ShadingType,
PageOrientation, PageBreak, Header, Footer, LineRuleType, TabStopType,
TabStopPosition, ImageRun, LevelFormat, SectionType, convertInchesToTwip,
convertMillimetersToTwip`.

**NO** uses `fs`, `http`, `child_process`, `process.env`, `eval`, `Function`.

Solo lectura del objeto `data` que recibes como parámetro.

## Patrón de estructura (todos los docs LexAI)

Un `Document` tiene `sections: [{...}]`. Cada section tiene:

- `properties.page.size` — Letter por defecto (216x279 mm o `convertInchesToTwip(8.5)` x `convertInchesToTwip(11)`)
- `properties.page.margin` — top/bottom/left/right en twips. Default LexAI: 1" = `convertInchesToTwip(1)`
- `properties.page.orientation` — `PageOrientation.PORTRAIT` (default) o `LANDSCAPE`
- `headers` y `footers` opcionales
- `children: [...]` — array de `Paragraph` y `Table`

## Estilo profesional LexAI (defaults)

| Aspecto | Valor |
|---|---|
| Hoja | Letter (US) 8.5"×11" |
| Márgenes | 1" todos los lados (`convertInchesToTwip(1)`) |
| Fuente cuerpo | "Arial" 11pt |
| Fuente título | "Arial" 14pt bold uppercase center |
| Interlineado | 1.15 (`line: 276, lineRule: LineRuleType.AUTO`) |
| Espaciado entre párrafos | `before: 0, after: 120` twips |
| Sangría primera línea | `firstLine: 720` twips (= 0.5") para cuerpo |
| Sin emojis | nunca |
| Idioma | es-CO |

## Patrón: Paragraph con TextRun

```javascript
new Paragraph({
  spacing: { before: 0, after: 120, line: 276, lineRule: LineRuleType.AUTO },
  alignment: AlignmentType.JUSTIFIED,
  indent: { firstLine: 720 },
  children: [
    new TextRun({ text: 'Texto normal en cuerpo. ', font: 'Arial', size: 22 }), // size en half-points (22 = 11pt)
    new TextRun({ text: 'Texto bold.', bold: true, font: 'Arial', size: 22 }),
  ],
})
```

⚠️ docx-js usa `size` en **half-points**: 11pt = `size: 22`, 14pt = `size: 28`.

## Patrón: Heading centrado

```javascript
new Paragraph({
  heading: HeadingLevel.HEADING_1,
  alignment: AlignmentType.CENTER,
  spacing: { before: 240, after: 240 },
  children: [
    new TextRun({ text: 'PODER ESPECIAL', bold: true, font: 'Arial', size: 28 }),
  ],
})
```

## Patrón: Tabla simple con bordes

```javascript
new Table({
  width: { size: 100, type: WidthType.PERCENTAGE },
  rows: [
    new TableRow({
      children: [
        new TableCell({
          width: { size: 40, type: WidthType.PERCENTAGE },
          children: [new Paragraph({ children: [new TextRun({ text: 'Placa', bold: true })] })],
        }),
        new TableCell({
          width: { size: 60, type: WidthType.PERCENTAGE },
          children: [new Paragraph({ children: [new TextRun({ text: data.placa || '[PLACA]' })] })],
        }),
      ],
    }),
    // ... más filas
  ],
})
```

## Patrón: Footer con cita LexAI

```javascript
footers: {
  default: new Footer({
    children: [
      new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [
          new TextRun({
            text: 'Documento generado con LexAI · ' + (data.fecha || new Date().toISOString().slice(0,10)),
            italics: true,
            size: 18, // 9pt
            color: '666666',
          }),
        ],
      }),
    ],
  }),
}
```

## Patrón: Lista numerada (cláusulas)

```javascript
new Paragraph({
  numbering: { reference: 'clausulas', level: 0 },
  children: [
    new TextRun({ text: 'PRIMERA. ', bold: true }),
    new TextRun({ text: data.clausula1 }),
  ],
})
```

Y registra el `numbering` a nivel de `Document`:

```javascript
new Document({
  numbering: {
    config: [{
      reference: 'clausulas',
      levels: [{
        level: 0,
        format: LevelFormat.UPPER_ROMAN,
        text: '%1.',
        alignment: AlignmentType.START,
      }],
    }],
  },
  sections: [{ ... }],
})
```

## Reglas obligatorias (rejection si no cumples)

1. Define `async function build(data)` exactamente con ese nombre.
2. Retorna **una sola instancia** de `new Document({...})`.
3. NO uses placeholders sin sustituir: si `data.nombre_poderdante` falta, escribe `'[NOMBRE_PODERDANTE]'` literal.
4. NO uses `console.log` (excepto para debug que será descartado).
5. NO uses promesas no-resueltas, async sin await.
6. NO uses APIs externas (fetch, http, etc.). Todo el contenido viene de `data` y del template.
7. NO uses caracteres no-imprimibles ni tabs literales: usa `\t` o `TabStop`.
8. NO superes 256KB de JS — si el doc es enorme, parametriza secciones con bucles sobre `data.clausulas[]`.

## Cómo manejar el SKILL.md del template

Cuando te pasen `notarial_poder_especial_co` (por ejemplo), tienes:

- `## Document Structure` — orden exacto de secciones
- `## Style Conventions` — cláusulas sacramentales, encabezados, citas obligatorias
- `## Common Placeholders` — mapea `[PODERDANTE]` → `data.nombre_poderdante`
- `## docx-js Style Hints` — tamaño hoja, fuente, márgenes específicos
- `## Risk Warnings` — NO incluir en el .docx, pero úsalas para validar lo que escribes

## Validación post-generación

El sandbox de LexAI (`node_executor.py`) valida:
- exit code 0 de Node
- archivo `.docx` ≥ 100 bytes y ≤ 10MB
- magic bytes `PK\x03\x04` (es zip válido)
- no errores en stderr

Si fallas, te llaman de nuevo con `retry_count > 0` y el `stderr` para que corrijas.

## Referencias

- docx-js API: https://docx.js.org/api/
- Hints adicionales en `validation/` (templates de validación) si los necesitas.
