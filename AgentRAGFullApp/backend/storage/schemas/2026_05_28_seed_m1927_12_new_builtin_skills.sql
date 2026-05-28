-- ============================================================================
-- Sprint M19.27 · Seed 12 NEW builtin skills (no duplica sprint_h)
-- ============================================================================
-- Generado por scripts/seed_12_new_templates_co.py desde
-- TEST Freddy/templates_review/{03,04,06,09,10,12,13,15,17,18,19,21}_*.md
-- IDEMPOTENTE: ON CONFLICT (firm_id, command, version) DO UPDATE.
-- ============================================================================

begin;


-- /redactar/revocatoria-poder
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/revocatoria-poder',
    'notarial_revocatoria_poder_co',
    'Revocatoria de poder notarial en Colombia. Extingue un mandato previo\nconforme a los Arts. 2189-2191 CC. Debe autenticarse ante notaría y\nnotificarse al apoderado revocado y a los terceros relevantes.',
    'drafting',
    '{"name": "notarial_revocatoria_poder_co", "description": "Revocatoria de poder notarial en Colombia. Extingue un mandato previo\\nconforme a los Arts. 2189-2191 CC. Debe autenticarse ante notaría y\\nnotificarse al apoderado revocado y a los terceros relevantes.", "category": "notarial", "doc_family": "notarial_poder", "doc_type": "revocatoria_poder", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/codigo_civil.html"]}'::jsonb,
    E'# Revocatoria de Poder Notarial — Colombia\n\n## When to use\nCuando el poderdante quiere DEJAR SIN EFECTO un poder otorgado previamente.\n\n## Document Structure\n1. Título: REVOCATORIA DE PODER\n2. Destinatario notarial\n3. Identificación del poderdante\n4. **PRIMERA. Identificación exacta del poder a revocar**\n   (fecha original, notaría, escritura/acta, apoderado)\n5. **SEGUNDA. Manifestación expresa de revocatoria**\n6. **TERCERA. Vigencia de la revocatoria**\n7. **CUARTA. Notificación al apoderado revocado**\n8. **QUINTA. Notificación a terceros relevantes**\n9. Firma del poderdante\n10. Diligencia notarial\n\n## Style Conventions\n\n### Cláusula PRIMERA — Identificación del poder revocado\nDEBE incluir:\n- Fecha exacta del poder original\n- Notaría que lo autenticó\n- Número de escritura/acta de autenticación\n- Nombre completo del apoderado revocado + CC\n- Síntesis de las facultades que se otorgaban\n\nEjemplo:\n"El presente acto tiene por objeto revocar el **PODER ESPECIAL** otorgado\npor el suscrito en favor del señor **[NOMBRE_APODERADO]**, identificado\ncon cédula **[CC]**, autenticado mediante escritura/acta No. **[NÚMERO]**\nde fecha **[FECHA]** ante la **Notaría [NÚMERO] del Círculo de [CIUDAD]**,\nmediante el cual le confería facultades para **[SÍNTESIS]**."\n\n### Cláusula SEGUNDA — Manifestación\nFórmula obligatoria:\n"En ejercicio de la facultad de revocación que me confieren los **artículos\n2189 y siguientes del Código Civil**, manifiesto que **REVOCO EN SU\nTOTALIDAD** el poder identificado en la cláusula anterior, de tal manera\nque **a partir de la fecha de autenticación** del presente instrumento el\napoderado quedará sin facultad alguna para actuar en mi nombre y\nrepresentación."\n\n### Citas normativas OBLIGATORIAS\n- **Arts. 2189-2191 del Código Civil** (Revocación del mandato)\n- **Decreto 960/1970** (Autenticación notarial)\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NOMBRE_PODERDANTE]`, `[CC_PODERDANTE]` | Quien revoca |\n| `[NOMBRE_APODERADO]`, `[CC_APODERADO]` | Apoderado revocado |\n| `[FECHA_PODER_ORIGINAL]` | Fecha del poder revocado |\n| `[NOTARIA_ORIGINAL]`, `[NUMERO_ESCRITURA_ORIGINAL]` | Datos del poder |\n| `[SINTESIS_FACULTADES]` | Resumen de facultades del poder revocado |\n\n## Risk Warnings\n\n- ⚠ **CRÍTICO**: La revocatoria solo produce efectos frente a terceros\n  DESDE el momento en que les sea NOTIFICADA o tengan conocimiento de\n  ella (Art. 2191 CC). Los actos celebrados por el apoderado antes de\n  la notificación CONSERVAN PLENA VALIDEZ.\n\n- ⚠ Si el poder revocado se registró en alguna entidad (Cámara de\n  Comercio, Oficina de Registro, banco), notifique también a esas\n  entidades.\n\n## Bibliography\n1. **Código Civil** Arts. 2189-2191 (Revocación del mandato)\n   https://www.secretariasenado.gov.co/senado/basedoc/codigo_civil.html',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/declaracion-extrajuicio
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/declaracion-extrajuicio',
    'notarial_declaracion_extrajuicio_co',
    'Declaraciones extraprocesales (extrajuicio) en Colombia rendidas ante\nnotario bajo gravedad de juramento (Art. 7 Decreto 960/1970, Art. 442 CP).\nAplican para acreditar hechos ante entidades cuando no se requiere proceso\njudicial: estado civil, supervivencia, dependencia económica, etc.',
    'drafting',
    '{"name": "notarial_declaracion_extrajuicio_co", "description": "Declaraciones extraprocesales (extrajuicio) en Colombia rendidas ante\\nnotario bajo gravedad de juramento (Art. 7 Decreto 960/1970, Art. 442 CP).\\nAplican para acreditar hechos ante entidades cuando no se requiere proceso\\njudicial: estado civil, supervivencia, dependencia económica, etc.", "category": "notarial", "doc_family": "notarial_extrajuicio", "doc_type": "declaracion_extrajuicio", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.suin-juriscol.gov.co/viewDocument.asp?ruta=Decretos/1058919", "https://www.secretariasenado.gov.co/senado/basedoc/ley_0599_2000.html"]}'::jsonb,
    E'# Declaración Extrajuicio Notarial — Colombia\n\n## When to use\nCuando se requiere acreditar hechos mediante manifestación bajo gravedad\nde juramento ante notaría, para uso ante entidades públicas o privadas:\n- Acreditar supervivencia\n- Acreditar estado civil\n- Acreditar dependencia económica\n- Acreditar paz y salvo, no posesión, etc.\n- Acreditar hechos para trámites de seguridad social, pensiones, etc.\n\n## Document Structure\n1. Título: DECLARACIÓN EXTRAJUICIO BAJO JURAMENTO\n2. Destinatario notarial\n3. Identificación del declarante (1 o varios)\n4. Identificación del trámite/entidad destinataria\n5. **HECHOS DECLARADOS** (numerados PRIMERO, SEGUNDO, ...)\n6. Manifestación bajo gravedad de juramento\n7. Firma del/los declarante(s)\n8. Diligencia notarial bajo juramento\n\n## Style Conventions\n\n### Encabezado\n```\n                DECLARACIÓN EXTRAJUICIO\n                  BAJO JURAMENTO\n\nSeñor(a) NOTARIO(A) [___] DEL CÍRCULO DE [CIUDAD]\nE. S. D.\n```\n\n### Identificación declarante(s)\n"En la ciudad de **[CIUDAD]**, a los **[DIA]** días del mes de **[MES]**\nde **[ANIO]**, comparecemos:\n\n**[NOMBRE COMPLETO]**, mayor de edad, vecino(a) de **[CIUDAD]**,\nidentificado(a) con cédula de ciudadanía No. **[CC]** de **[LUGAR]**,\nde profesión **[PROFESIÓN]**, con domicilio en **[DIRECCIÓN]**, en\nnuestro propio nombre y manifestamos:"\n\n### Hechos (numeración ordinal en MAYÚSCULAS)\n- PRIMERO: [Hecho concreto]\n- SEGUNDO: [Hecho concreto]\n- TERCERO: [Hecho concreto]\n- ...\n\nCada hecho:\n- Tiempo verbal: presente o pretérito perfecto\n- Datos concretos: fechas, lugares, números, nombres completos\n- NO conjeturas ni opiniones\n\n### Manifestación juramentada (OBLIGATORIA)\nFórmula sacramental:\n"Declaramos bajo la gravedad del juramento, con pleno conocimiento de las\nsanciones civiles, penales y disciplinarias en que incurriríamos en caso\nde faltar a la verdad, en los términos del **artículo 442 del Código\nPenal (falso testimonio)** y artículo 8 del Decreto 960 de 1970, que los\nhechos aquí declarados son **veraces y nos constan personalmente**."\n\n### Citas normativas OBLIGATORIAS\n- **Art. 7 del Decreto 960 de 1970** (declaraciones extrajuicio)\n- **Art. 442 del Código Penal** (falso testimonio - sanción)\n- **Art. 250 del Código General del Proceso** (valor probatorio)\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NOMBRE_DECLARANTE]`, `[CC_DECLARANTE]` | Quien declara |\n| `[ENTIDAD_DESTINATARIA]` | Para qué entidad/trámite (Colpensiones, Notaría, etc.) |\n| `[OBJETO_TRAMITE]` | Trámite que se busca acreditar |\n| `[HECHO_PRIMERO]`, `[HECHO_SEGUNDO]`, ... | Hechos declarados |\n\n### Cierre\n```\nPara constancia firma(n) en [CIUDAD], en la fecha indicada.\n\nEL/LA DECLARANTE,\n\n_________________________________________\n[NOMBRE]\nC.C. No. [CC] de [LUGAR]\n\nDILIGENCIA NOTARIAL DE PRESENTACIÓN PERSONAL\n\n(Espacio reservado para el Notario, quien deja constancia de la\npresentación personal del declarante, de su identificación y de\nhaber sido informado de las sanciones legales del falso testimonio.)\n```\n\n## Risk Warnings\n- ⚠ Solo declarar HECHOS de los que se tiene CONSTANCIA PERSONAL\n- ⚠ Falso testimonio puede generar pena privativa de la libertad hasta 6 años (Art. 442 CP)\n- ⚠ No usar para hechos sometidos a proceso judicial activo\n\n## Bibliography\n1. **Decreto 960 de 1970** Art. 7 (Declaraciones extrajuicio)\n   https://www.suin-juriscol.gov.co/viewDocument.asp?ruta=Decretos/1058919\n2. **Código Penal** Art. 442 (Falso testimonio)\n   https://www.secretariasenado.gov.co/senado/basedoc/ley_0599_2000.html\n3. **CGP** Art. 250 (Valor probatorio)',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/demanda-civil-ordinaria
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/demanda-civil-ordinaria',
    'judicial_demanda_civil_ordinaria_co',
    'Demanda Civil Ordinaria en Colombia conforme al CGP (Ley 1564/2012).\nAplica para procesos declarativos de mayor cuantía que no tengan\nprocedimiento especial. Formato forense colombiano estándar.',
    'drafting',
    '{"name": "judicial_demanda_civil_ordinaria_co", "description": "Demanda Civil Ordinaria en Colombia conforme al CGP (Ley 1564/2012).\\nAplica para procesos declarativos de mayor cuantía que no tengan\\nprocedimiento especial. Formato forense colombiano estándar.", "category": "judicial", "doc_family": "judicial_demanda", "doc_type": "demanda_civil_ordinaria", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/ley_1564_2012.html", "https://www.ramajudicial.gov.co", "https://www.icdp.org.co"]}'::jsonb,
    E'# Demanda Civil Ordinaria — Colombia\n\n## When to use\nProcesos declarativos civiles de mayor cuantía sin procedimiento especial\nasignado (cobro de perjuicios, declaraciones de derechos, prescripciones,\nacciones rescisorias, etc.). Si es proceso especial (ejecutivo, pertenencia,\ndivorcio, alimentos), use el template específico.\n\n## Document Structure\n\n1. **Encabezado**: dirigido al Juez con calidad (REPARTO si aplica)\n2. **Referencia** y comparecencia del apoderado\n3. **I. PARTES**: demandante(s) y demandada(s)\n4. **II. HECHOS** (numerados arábigos con bold lead-in)\n5. **III. PRETENSIONES** (ordinales en MAYÚSCULAS)\n6. **IV. FUNDAMENTOS DE DERECHO** (norma + jurisprudencia)\n7. **V. COMPETENCIA Y CUANTÍA**\n8. **VI. PRUEBAS** (documentales, testimoniales, periciales)\n9. **VII. ANEXOS**\n10. **VIII. NOTIFICACIONES** (cada parte)\n11. **IX. JURAMENTO ESTIMATORIO** (Art. 206 CGP)\n12. **Cierre** "Del Señor Juez, / Atentamente," + firma apoderado con T.P.\n\n## Style Conventions\n\n### Encabezado forense\n```\n                              Señor\n                JUEZ CIVIL DEL CIRCUITO DE [CIUDAD]\n                            (REPARTO)\n\n\nReferencia:    DEMANDA ORDINARIA CIVIL DE MAYOR CUANTÍA\n               de [NOMBRE_DEMANDANTE] contra [NOMBRE_DEMANDADA]\n```\n\n### Apoderado (comparecencia)\n"**[NOMBRE_APODERADO]**, mayor de edad, vecino(a) de **[CIUDAD]**,\nidentificado(a) con C.C. No. **[CC_APODERADO]** de **[LUGAR]**, abogado(a)\nen ejercicio, portador(a) de la **T.P. No. [TP] del C.S.J.**, obrando en\ncalidad de **apoderado(a) judicial** de **[NOMBRE_DEMANDANTE]**, conforme\nal **poder que se adjunta**, respetuosamente formulo la presente\n**DEMANDA ORDINARIA CIVIL DE MAYOR CUANTÍA** contra **[NOMBRE_DEMANDADA]**,\ncon base en los siguientes hechos, pretensiones y fundamentos."\n\n### I. PARTES\n- Demandante: nombres, CC/NIT, domicilio, dirección notificación, correo\n- Demandado: nombres, CC/NIT, domicilio, dirección notificación, correo\n- Si rep legal: mencionar calidad\n\n### II. HECHOS (formato OBLIGATORIO)\nCada hecho:\n- Numerado en arábigos\n- **Lead-in en bold** (etiqueta temática)\n- Texto justificado normal\n\nEjemplo:\n**1. Vínculo contractual.** El día 15 de febrero de 2024 las partes\ncelebraron contrato de [tipo] mediante documento privado autenticado, en\nvirtud del cual mi representado adquirió [...].\n\n### III. PRETENSIONES\nCada pretensión:\n- Ordinal en MAYÚSCULAS BOLD: PRIMERA, SEGUNDA, TERCERA...\n- Verbo procesal en MAYÚSCULAS BOLD al inicio: DECLARAR, CONDENAR, ORDENAR\n\nEjemplo:\n**PRIMERA. DECLARAR** la existencia del contrato de compraventa celebrado\nentre las partes el 15 de febrero de 2024.\n\n**SEGUNDA. CONDENAR** a la demandada al pago de la suma de\n**$120.000.000** por concepto de [...].\n\n### IV. FUNDAMENTOS DE DERECHO\n- Cite normas específicas con artículo + ley + año\n- Use bloques `norma_citada` separados (no en prosa)\n- Cite jurisprudencia con identificador + M.P. + fecha\n- Conecte cada norma con los hechos del caso\n\n### V. COMPETENCIA Y CUANTÍA\n"Es competente el Juez Civil del Circuito de [CIUDAD] por razón del\n**territorio** (domicilio de la demandada) y la **cuantía** que se estima\nen **[MAYOR/MENOR/MÍNIMA]** de conformidad con el Art. 25 CGP."\n\nCuantía 2025: Mayor > 150 SMMLV (~$214M) · Menor 40-150 SMMLV · Mínima <40 SMMLV\n\n### IX. JURAMENTO ESTIMATORIO (OBLIGATORIO Art. 206 CGP)\n```\nBajo la gravedad del juramento, en los términos del Art. 206 CGP, estimo\nlas pretensiones de condena en la suma de $[MONTO_TOTAL] discriminada así:\n\n| Concepto | Valor estimado |\n| --- | --- |\n| [CONCEPTO_1] | $[VALOR_1] |\n| [CONCEPTO_2] | $[VALOR_2] |\n| ... | ... |\n| **TOTAL** | **$[TOTAL]** |\n```\n\n### Citas normativas mínimas\n- **Arts. 82-90 CGP** (Requisitos de la demanda)\n- **Art. 206 CGP** (Juramento estimatorio)\n- **Art. 25 CGP** (Cuantía)\n- **Arts. 28-29 CGP** (Competencia territorial)\n- Normas sustantivas del CC según el caso\n\n### Cierre (firma OBLIGATORIA estilo demanda)\n```\nDel Señor Juez,\n\nAtentamente,\n\n_____________________________________\n[NOMBRE_APODERADO]\nAbogada(o) · T.P. No. [TP] del C.S.J.\nC.C. No. [CC]\nEmail: [CORREO]  /  Tel.: [TELEFONO]\n```\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NOMBRE_DEMANDANTE]`, `[CC_DEMANDANTE]` | Demandante |\n| `[NOMBRE_DEMANDADA]`, `[CC_DEMANDADA]` | Demandada |\n| `[NOMBRE_APODERADO]`, `[TP_APODERADO]` | Apoderado |\n| `[CIUDAD_DEMANDA]` | Donde se presenta |\n| `[MONTO_TOTAL]`, `[MONTO_CADA_PRETENSIÓN]` | Cuantías |\n| `[HECHO_1]`, `[HECHO_2]`, ... | Cada hecho narrado |\n| `[FECHA_CONTRATO]`, `[FECHA_INCUMPLIMIENTO]` | Fechas relevantes |\n\n### docx-js Style Hints\n- Page Letter, márgenes 3 cm (estándar forense)\n- Font: Times New Roman 12pt\n- **section_heading**: roman + texto MAYÚSCULAS, centered, bold, con border bottom\n- **hecho**: indent left 1cm, primer run bold (lead-in)\n- **pretension**: indent left 1cm, primer run bold MAYÚSCULAS\n- **firma**: alineada derecha (ciudad_fecha), centro (signature lines)\n\n## Risk Warnings\n- ⚠ **Caducidad y prescripción**: validar plazos antes de presentar\n- ⚠ **Juramento estimatorio**: si error >50% genera sanción del 10% de diferencia\n- ⚠ **Cuantía mal estimada**: puede invalidar competencia → inadmisión\n- ⚠ **Anexos completos**: certificación de no conciliación si requerida\n\n## Bibliography\n1. **CGP** Ley 1564/2012 (Código General del Proceso)\n   https://www.secretariasenado.gov.co/senado/basedoc/ley_1564_2012.html\n2. **Instituto Colombiano de Derecho Procesal**\n   https://www.icdp.org.co\n3. **Rama Judicial** - Formatos oficiales\n   https://www.ramajudicial.gov.co',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/demanda-laboral
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/demanda-laboral',
    'judicial_demanda_laboral_co',
    'Demanda Laboral Ordinaria en Colombia ante Juez Laboral del Circuito\n(CST + CPTSS Arts. 25-38). Para cobro de prestaciones, indemnizaciones,\nreintegro, etc.',
    'drafting',
    '{"name": "judicial_demanda_laboral_co", "description": "Demanda Laboral Ordinaria en Colombia ante Juez Laboral del Circuito\\n(CST + CPTSS Arts. 25-38). Para cobro de prestaciones, indemnizaciones,\\nreintegro, etc.", "category": "judicial", "doc_family": "judicial_demanda", "doc_type": "demanda_laboral_ordinaria", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/codigo_sustantivo_trabajo.html"]}'::jsonb,
    E'# Demanda Laboral Ordinaria — Colombia\n\n## When to use\nDespido injusto, cobro de salarios, prestaciones, indemnizaciones,\nreintegro, fuero sindical, fuero por maternidad/salud (estabilidad\nreforzada), etc.\n\n## Document Structure\n1. Encabezado: Juez Laboral del Circuito (REPARTO)\n2. Apoderado + Referencia\n3. I. PARTES (demandante trabajador, demandada empleador)\n4. II. HECHOS (vínculo laboral, salario, terminación)\n5. III. PRETENSIONES\n6. IV. LIQUIDACIÓN DE PRESTACIONES (tabla con conceptos y montos)\n7. V. FUNDAMENTOS DE DERECHO\n8. VI. COMPETENCIA\n9. VII. PRUEBAS (contrato, nómina, liquidación, EPS, ARL, AFP)\n10. VIII. ANEXOS\n11. IX. NOTIFICACIONES\n12. X. JURAMENTO ESTIMATORIO (Art. 28 CPTSS + 206 CGP)\n13. Firma apoderado\n\n## Style Conventions\n\n### II. HECHOS — datos OBLIGATORIOS\n- **Vínculo laboral**: fecha inicio, fecha terminación, modalidad\n  (término fijo/indefinido/obra-labor), cargo, jornada\n- **Salario**: último salario devengado (con detalle de componentes)\n- **Justa causa alegada** (si despido)\n- **Fechas críticas**: pre-aviso, terminación efectiva, pago liquidación\n\n### IV. LIQUIDACIÓN DE PRESTACIONES (tabla calc_step + table)\n| Concepto | Base / Fórmula | Valor (COP) |\n|---|---|---|\n| Cesantías 2018-2025 (Art. 249 CST) | 1 mes salario × 7,79 años | $XX |\n| Intereses cesantías 12% EA | Cesantías × 12% × años | $XX |\n| Prima servicios pendiente | 30 días/año × prorrata | $XX |\n| Vacaciones compensadas | 15 días/año × prorrata | $XX |\n| Indemnización despido sin J.C. (Art. 64 CST) | Depende cargo + años | $XX |\n| Sanción Art. 65 CST (1 día salario por día mora) | Hasta 24 meses | $XX |\n| **TOTAL APROXIMADO** | | **$XX** |\n\n### Citas normativas OBLIGATORIAS\n- **CST** Arts. 22-43 (Contrato de trabajo)\n- **Art. 64 CST** (Indemnización despido sin justa causa)\n- **Art. 65 CST** (Sanción moratoria)\n- **Art. 249 CST** (Cesantías)\n- **CPTSS** Arts. 25-38 (Procedimiento)\n- **Art. 28 CPTSS + Art. 206 CGP** (Juramento estimatorio)\n- Si fuero materno: **Art. 239 CST**, **Sentencia C-005/2017**\n- Si fuero salud/discapacidad: **Ley 361/1997 + SU-049/2017**\n\n### Pretensiones obligatorias\nPRIMERA. DECLARAR la existencia del contrato de trabajo\nSEGUNDA. DECLARAR su terminación injusta (si aplica)\nTERCERA. CONDENAR al pago de prestaciones (relacionadas en liquidación)\nCUARTA. CONDENAR a sanción del Art. 65 CST (si aplica)\nQUINTA. CONDENAR a costas y agencias en derecho\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NOMBRE_TRABAJADOR]`, `[CC]` | Demandante |\n| `[RAZON_SOCIAL_EMPLEADOR]`, `[NIT]` | Demandada |\n| `[CARGO]`, `[MODALIDAD_CONTRATO]` | Tipo vínculo |\n| `[FECHA_INGRESO]`, `[FECHA_TERMINACION]` | Vínculo |\n| `[ULTIMO_SALARIO]`, `[COMPONENTES_SALARIO]` | Remuneración |\n| `[JUSTA_CAUSA_ALEGADA]` | Si despido |\n\n## Risk Warnings\n- ⚠ **Prescripción 3 años**: Art. 488 CST\n- ⚠ **Conciliación previa**: voluntaria pero recomendada (Ministerio Trabajo)\n- ⚠ **Salario integral**: requiere ≥13 SMMLV (~$18.5M en 2025), NO menos\n- ⚠ **Indemnización Art. 64**: depende del tipo de contrato\n\n## Bibliography\n1. **CST** Decreto 2663/1950\n2. **CPTSS** Decreto 2158/1948\n3. **Ley 50/1990** (Modificaciones laborales)\n4. **Ley 789/2002** (Indemnizaciones)',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/demanda-admin-nulidad
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/demanda-admin-nulidad',
    'judicial_demanda_admin_nulidad_co',
    'Demanda de Nulidad y Restablecimiento del Derecho en Colombia ante\njurisdicción contencioso-administrativa (CPACA Arts. 137-138 + 162).\nPara impugnar actos administrativos particulares y obtener restablecimiento.',
    'drafting',
    '{"name": "judicial_demanda_admin_nulidad_co", "description": "Demanda de Nulidad y Restablecimiento del Derecho en Colombia ante\\njurisdicción contencioso-administrativa (CPACA Arts. 137-138 + 162).\\nPara impugnar actos administrativos particulares y obtener restablecimiento.", "category": "judicial", "doc_family": "judicial_demanda", "doc_type": "demanda_nulidad_restablecimiento", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/ley_1437_2011.html"]}'::jsonb,
    E'# Demanda Nulidad y Restablecimiento del Derecho — Colombia\n\n## When to use\nImpugnar acto administrativo PARTICULAR que afecta derechos subjetivos\n(resoluciones DIAN, SIC, ICBF, alcaldías, sanciones disciplinarias, etc.).\n\n## Document Structure\n1. Encabezado: Juez Administrativo o Tribunal Administrativo (según cuantía)\n2. Apoderado + Referencia\n3. I. PARTES (demandante + entidad pública demandada)\n4. **II. ACTO ADMINISTRATIVO DEMANDADO** (identificación COMPLETA)\n5. III. AGOTAMIENTO DE LA VÍA GUBERNATIVA\n6. IV. HECHOS\n7. V. PRETENSIONES (nulidad + restablecimiento)\n8. VI. CAUSALES DE NULIDAD (Art. 137 CPACA)\n9. VII. FUNDAMENTOS DE DERECHO\n10. VIII. COMPETENCIA Y CADUCIDAD\n11. IX. PRUEBAS\n12. X. ANEXOS\n13. XI. NOTIFICACIONES\n14. XII. JURAMENTO (Art. 167 CPACA)\n15. Firma apoderado\n\n## Style Conventions\n\n### II. ACTO ADMINISTRATIVO (datos OBLIGATORIOS)\nDEBE incluir:\n- Tipo de acto: Resolución / Decreto / Auto / Decisión\n- **Número** exacto del acto\n- **Fecha de expedición**\n- **Entidad emisora** (con identificación completa)\n- **Funcionario** que lo suscribió (cargo + nombre si conocido)\n- **Fecha de notificación** al demandante\n- Síntesis del contenido\n\nEjemplo:\n"El acto administrativo objeto de la presente demanda es la **Resolución\nNo. 12345 del 15 de marzo de 2024**, expedida por la **Subdirección de\nRecaudo y Cobranzas** de la **Dirección de Impuestos y Aduanas Nacionales\n(DIAN)**, mediante la cual se [...]."\n\n### III. AGOTAMIENTO VÍA GUBERNATIVA (OBLIGATORIO Art. 76 CPACA)\nDEBE acreditar:\n- Recurso de reposición interpuesto (fecha)\n- Respuesta de la administración (fecha + número)\n- Si aplica: recurso de apelación + respuesta\n\n### VI. CAUSALES DE NULIDAD (Art. 137 CPACA)\nIndicar específicamente cuáles:\n1. Infracción de normas en que debía fundarse\n2. Falta de competencia\n3. Expedición irregular\n4. Desconocimiento del derecho de audiencias y defensa\n5. Falsa motivación\n6. Desviación de las atribuciones propias del funcionario\n\n### Citas normativas OBLIGATORIAS\n- **CPACA Arts. 137-138** (Causales de nulidad)\n- **CPACA Art. 162** (Demanda contencioso administrativa)\n- **CPACA Art. 167** (Juramento estimatorio)\n- **CPACA Art. 164** (Caducidad - 4 meses general)\n- **Constitución Política** Arts. 6, 29, 121 (Principios)\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NOMBRE_DEMANDANTE]`, `[CC/NIT]` | Demandante |\n| `[ENTIDAD_DEMANDADA]` | Entidad pública |\n| `[TIPO_ACTO]`, `[NUMERO_ACTO]`, `[FECHA_ACTO]` | Identificación acto |\n| `[FECHA_NOTIFICACION]` | Para cómputo caducidad |\n| `[FECHA_RECURSO_REPOSICION]` | Agotamiento |\n| `[CAUSAL_NULIDAD_INVOCADA]` | Cuáles del Art. 137 |\n\n## Risk Warnings\n- ⚠ **CADUCIDAD 4 MESES** desde notificación del acto definitivo (Art. 164 CPACA)\n- ⚠ **AGOTAMIENTO VÍA GUBERNATIVA**: imprescindible o se inadmite\n- ⚠ Si el acto fue ratificado: la caducidad cuenta desde el acto ratificatorio\n- ⚠ Si silencio administrativo: cuenta desde término legal de respuesta\n\n## Bibliography\n1. **CPACA** Ley 1437/2011\n   https://www.secretariasenado.gov.co/senado/basedoc/ley_1437_2011.html\n2. **CN** Constitución Política',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/recurso-apelacion
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/recurso-apelacion',
    'judicial_recurso_apelacion_co',
    'Recurso de Apelación en Colombia conforme al CGP (Arts. 320-330).\nImpugna sentencias de primera instancia ante el superior.',
    'drafting',
    '{"name": "judicial_recurso_apelacion_co", "description": "Recurso de Apelación en Colombia conforme al CGP (Arts. 320-330).\\nImpugna sentencias de primera instancia ante el superior.", "category": "judicial", "doc_family": "judicial_recurso", "doc_type": "recurso_apelacion", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/ley_1564_2012.html"]}'::jsonb,
    E'# Recurso de Apelación — Colombia\n\n## When to use\nImpugnar sentencia o auto interlocutorio susceptible de apelación\nen primera instancia. Plazos: 3 días sentencia oral, 5 días sentencia\nescrita (Art. 322 CGP).\n\n## Document Structure\n1. Encabezado: dirigido al **Juez de primera instancia** (NO al superior)\n2. Apoderado + Referencia (expediente)\n3. **I. PROVIDENCIA APELADA** (identificación COMPLETA)\n4. II. REPAROS CONCRETOS (cargos específicos)\n5. **III. SUSTENTACIÓN** (argumentos por cada reparo)\n6. IV. PRETENSIONES DEL RECURSO\n7. V. FUNDAMENTOS DE DERECHO\n8. Firma apoderado\n\n## Style Conventions\n\n### I. PROVIDENCIA APELADA (datos OBLIGATORIOS)\n- Tipo: Sentencia o Auto\n- **Fecha** exacta de la providencia\n- **Juez** que la dictó\n- **Número** de expediente/radicación\n- **Síntesis** del fallo recurrido\n\n### II-III. REPAROS Y SUSTENTACIÓN\nEstructura crítica del recurso. Cada reparo:\n- Identificar QUÉ se ataca (parte resolutiva, motivación, ratio decidendi)\n- POR QUÉ es errado (norma aplicable mal interpretada, prueba mal valorada, hecho omitido)\n- CON QUÉ argumento (precedente, doctrina, lógica)\n\nNumerados:\n- PRIMER REPARO: ...\n- SEGUNDO REPARO: ...\n\n### IV. PRETENSIONES\nVerbos típicos:\n- REVOCAR (parcial o totalmente)\n- MODIFICAR\n- ACLARAR\n- ADICIONAR\n\n### Citas normativas OBLIGATORIAS\n- **CGP Art. 320** (Procedencia)\n- **CGP Art. 321** (Procedencia específica)\n- **CGP Art. 322** (Plazos)\n- **CGP Art. 323** (Sustentación)\n- **CGP Art. 327** (Efectos del recurso)\n- Las normas del fondo del proceso\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[TIPO_PROVIDENCIA]` | Sentencia/Auto |\n| `[FECHA_PROVIDENCIA]` | Cuando se dictó |\n| `[NUMERO_EXPEDIENTE]` | Radicación |\n| `[REPARO_1]`, `[REPARO_2]`, ... | Cargos específicos |\n| `[NORMA_INVOCADA]` | Por cada reparo |\n\n## Risk Warnings\n- ⚠ **PLAZO CRÍTICO**: 3 días (oral) / 5 días (escrito) tras notificación\n- ⚠ **SUSTENTACIÓN OBLIGATORIA**: sin ella, el recurso es declarado desierto\n- ⚠ **NO se admiten** pretensiones nuevas no planteadas en primera instancia\n- ⚠ **Apelación adhesiva**: la otra parte puede adherir si pierde algo\n\n## Bibliography\n1. **CGP** Arts. 320-330 (Recurso de apelación)',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/contestacion-demanda
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/contestacion-demanda',
    'judicial_contestacion_demanda_co',
    'Contestación de Demanda en Colombia (CGP Arts. 96-101).\nPronunciamiento del demandado sobre hechos, oposición a pretensiones,\nexcepciones previas y de mérito.',
    'drafting',
    '{"name": "judicial_contestacion_demanda_co", "description": "Contestación de Demanda en Colombia (CGP Arts. 96-101).\\nPronunciamiento del demandado sobre hechos, oposición a pretensiones,\\nexcepciones previas y de mérito.", "category": "judicial", "doc_family": "judicial_memorial", "doc_type": "contestacion_demanda", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/ley_1564_2012.html"]}'::jsonb,
    E'# Contestación de Demanda — Colombia\n\n## When to use\nPara responder demanda dentro del término de traslado (típicamente 20 días).\nAplica para procesos civiles, comerciales, laborales, administrativos.\n\n## Document Structure\n1. Encabezado: dirigido al **mismo juez** que conoce el proceso\n2. Apoderado + Referencia (expediente)\n3. I. PRONUNCIAMIENTO SOBRE LOS HECHOS (uno por uno)\n4. II. OPOSICIÓN A LAS PRETENSIONES\n5. III. EXCEPCIONES PREVIAS (si aplica)\n6. IV. EXCEPCIONES DE MÉRITO\n7. V. FUNDAMENTOS DE LA DEFENSA\n8. VI. PRUEBAS (que propone el demandado)\n9. VII. ANEXOS\n10. VIII. JURAMENTO (si pretensiones económicas)\n11. Firma apoderado\n\n## Style Conventions\n\n### I. PRONUNCIAMIENTO SOBRE LOS HECHOS (OBLIGATORIO Art. 96 CGP)\nPara CADA hecho de la demanda, indicar:\n- **CIERTO**: si se acepta\n- **PARCIALMENTE CIERTO**: aceptado en parte (especificar qué se acepta y qué no)\n- **NO ES CIERTO**: si se niega (dar la versión)\n- **NO ME CONSTA**: si no se tiene conocimiento\n\nEjemplo:\n**HECHO 1:** *"El demandante celebró contrato con la demandada el 15 de febrero..."*\n**RESPUESTA:** Es CIERTO el hecho.\n\n**HECHO 2:** *"El demandante pagó la suma de $50.000.000..."*\n**RESPUESTA:** NO ES CIERTO. El demandante pagó únicamente la suma de\n$30.000.000, según consta en los recibos que se aportan.\n\n### III. EXCEPCIONES PREVIAS (Art. 100 CGP)\nSolo procesales. Numeradas:\n- PRIMERA: Falta de jurisdicción\n- SEGUNDA: Falta de competencia\n- TERCERA: Compromiso o cláusula compromisoria\n- CUARTA: Cosa juzgada\n- QUINTA: Litispendencia\n- SEXTA: Trámite inadecuado\n- SÉPTIMA: Demanda no cumple requisitos\n- ...\n\n### IV. EXCEPCIONES DE MÉRITO\nLas de fondo. Ejemplos:\n- PAGO\n- PRESCRIPCIÓN\n- COMPENSACIÓN\n- NOVACIÓN\n- INEXISTENCIA DE LA OBLIGACIÓN\n- DOLO DEL DEMANDANTE\n- ENRIQUECIMIENTO SIN CAUSA\n- INVERSIÓN DE LA CARGA DE LA PRUEBA\n- BUENA FE\n\nCada una con su sustentación.\n\n### Citas normativas OBLIGATORIAS\n- **CGP Arts. 96-101** (Contestación)\n- **CGP Art. 100** (Excepciones previas)\n- **CGP Art. 280** (Excepciones de mérito)\n- Las normas sustantivas de las excepciones invocadas\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NUMERO_EXPEDIENTE]` | Radicación |\n| `[NOMBRE_DEMANDADO]`, `[CC]` | Demandado |\n| `[RESPUESTA_HECHO_N]` | Por cada hecho de la demanda |\n| `[EXCEPCION_PREVIA_N]` | Si aplica |\n| `[EXCEPCION_MERITO_N]` | Con sustentación |\n\n## Risk Warnings\n- ⚠ **TÉRMINO 20 DÍAS** desde notificación auto admisorio\n- ⚠ **No contestar** = se entienden negados los hechos pero el silencio sobre\n  obligaciones de hacer puede generar consecuencias procesales\n- ⚠ **Excepciones previas** se proponen JUNTO a contestación (no separado)\n- ⚠ **Pretensiones reconvenidas** (demanda de reconvención): si las hay, se proponen aquí\n\n## Bibliography\n1. **CGP** Arts. 96-101, 280',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/requerimiento-extrajudicial
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/requerimiento-extrajudicial',
    'petitorio_requerimiento_extrajudicial_co',
    'Requerimiento Extrajudicial / Cobro Prejurídico en Colombia.\nComunicación formal previa al inicio de proceso judicial para\nconstituir en mora y exigir cumplimiento de obligaciones.',
    'drafting',
    '{"name": "petitorio_requerimiento_extrajudicial_co", "description": "Requerimiento Extrajudicial / Cobro Prejurídico en Colombia.\\nComunicación formal previa al inicio de proceso judicial para\\nconstituir en mora y exigir cumplimiento de obligaciones.", "category": "petitorio", "doc_family": "petitorio_extrajudicial", "doc_type": "requerimiento_extrajudicial", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/codigo_civil.html"]}'::jsonb,
    E'# Requerimiento Extrajudicial / Cobro Prejurídico — Colombia\n\n## When to use\nCobro previo a la instauración de proceso ejecutivo o demanda civil.\nSirve para:\n- Constituir en mora al deudor (interrumpe prescripción, Art. 94 CGP)\n- Documentar el intento de cobro\n- Iniciar conteo de intereses moratorios\n\n## Document Structure\n1. Encabezado: dirigido al deudor (carta)\n2. Identificación del remitente\n3. Identificación del destinatario (deudor)\n4. **ANTECEDENTES** (origen de la obligación)\n5. **OBLIGACIÓN ADEUDADA** (capital, intereses, gastos)\n6. **LIQUIDACIÓN DETALLADA**\n7. **REQUERIMIENTO DE PAGO**\n8. **PLAZO CONCEDIDO**\n9. **CONSECUENCIAS POR INCUMPLIMIENTO**\n10. Firma del remitente\n\n## Style Conventions\n\n### Encabezado (carta formal)\n```\n[CIUDAD], [FECHA]\n\n\nSeñor(a):\n[NOMBRE_DEUDOR]\n[DIRECCIÓN]\n[CIUDAD]\n\n\nAsunto: REQUERIMIENTO EXTRAJUDICIAL DE PAGO\n```\n\n### Identificación remitente\n"Cordial saludo. Por medio de la presente comunicación, en mi calidad de\n**[CALIDAD: acreedor / apoderado / representante legal]** de\n**[NOMBRE_ACREEDOR]**, identificado(a) con **[CC/NIT]**, me permito\nformular **REQUERIMIENTO EXTRAJUDICIAL DE PAGO** en los términos\nsiguientes:"\n\n### Antecedentes\n"**PRIMERO:** En fecha [FECHA] usted suscribió con [ACREEDOR] el documento\n[TIPO: pagaré / contrato / factura], identificado con [NÚMERO], mediante\nel cual reconoció a su cargo la obligación de pagar la suma de\n[$CAPITAL_NUMERO] ([CAPITAL_LETRAS]) en el plazo de [PLAZO]."\n\n"**SEGUNDO:** Vencido el plazo señalado, usted no ha cumplido con la\nobligación de pago, encontrándose en **estado de mora** desde el día\n[FECHA_MORA] hasta la fecha del presente requerimiento."\n\n### Liquidación\n| Concepto | Valor |\n|---|---|\n| Capital adeudado | $[CAPITAL] |\n| Intereses moratorios (12% EA o tasa máxima permitida) | $[INTERESES] |\n| Gastos de cobro / honorarios | $[GASTOS] |\n| **TOTAL ADEUDADO A LA FECHA** | **$[TOTAL]** |\n\n### Requerimiento (cláusula sacramental)\n"**REQUIERO** a usted para que dentro del término improrrogable de\n**[PLAZO_DIAS] días hábiles** contados a partir del recibo de la presente\ncomunicación, proceda al pago de la suma de **$[TOTAL_ADEUDADO]** mediante\n[FORMA_PAGO: consignación / cheque / transferencia] a [DATOS_BANCARIOS]."\n\n### Consecuencias por incumplimiento\n"**Advertimos** que de no atender el presente requerimiento en el plazo\nseñalado, **iniciaremos las acciones judiciales correspondientes** para\nel cobro forzoso de las sumas adeudadas, incluyendo:\n\n(a) Proceso ejecutivo singular o hipotecario según corresponda;\n(b) Cobro de intereses moratorios hasta el pago efectivo;\n(c) Reporte a centrales de riesgo crediticio (DataCrédito, CIFIN);\n(d) Cobro de costas y agencias en derecho;\n(e) Solicitud de medidas cautelares sobre bienes."\n\n### Citas normativas (cuando se invocan)\n- **Art. 1608 CC** (Mora)\n- **Art. 1617 CC** (Indemnización por mora)\n- **Art. 884 CCo** (Intereses moratorios mercantiles)\n- **Art. 94 CGP** (Interrupción civil de la prescripción por requerimiento)\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NOMBRE_ACREEDOR]`, `[CC/NIT]` | Quien cobra |\n| `[NOMBRE_DEUDOR]`, `[CC]` | Deudor |\n| `[TIPO_DOCUMENTO_OBLIGACION]` | Pagaré, contrato, factura |\n| `[CAPITAL_ADEUDADO]`, `[FECHA_MORA]` | Liquidación |\n| `[TASA_INTERES]` | Mora aplicable |\n| `[PLAZO_PAGO_DIAS]` | Días hábiles concedidos |\n\n## Risk Warnings\n- ⚠ **Tasa de interés**: máximo permitido = Bancario corriente certificado por\n  Superfinanciera × 1.5 (Art. 884 CCo). Excederlo es usura.\n- ⚠ **Requerimiento como prueba**: enviar por correo certificado o servicio\n  con recibo de entrega para acreditar el envío\n- ⚠ **Reporte centrales de riesgo**: solo procede si previamente se informó\n  sobre la posibilidad y se concedió plazo razonable (Habeas Data Ley 1266/2008)\n\n## Bibliography\n1. **Código Civil** Arts. 1608, 1617\n2. **Código de Comercio** Art. 884\n3. **CGP** Art. 94 (Interrupción prescripción)\n4. **Ley 1266/2008** Habeas Data financiero',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/prestacion-servicios
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/prestacion-servicios',
    'contractual_prestacion_servicios_co',
    'Contrato de Prestación de Servicios en Colombia (Arts. 2063-2069 CC).\nRégimen civil NO laboral. Debe evitarse simulación de relación laboral.',
    'drafting',
    '{"name": "contractual_prestacion_servicios_co", "description": "Contrato de Prestación de Servicios en Colombia (Arts. 2063-2069 CC).\\nRégimen civil NO laboral. Debe evitarse simulación de relación laboral.", "category": "contractual", "doc_family": "contractual_civil", "doc_type": "contrato_prestacion_servicios", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/codigo_civil.html"]}'::jsonb,
    E'# Contrato de Prestación de Servicios — Colombia\n\n## When to use\nCuando se contrata a una PERSONA NATURAL O JURÍDICA para prestar servicios\nprofesionales/técnicos/administrativos SIN SUBORDINACIÓN. Caso típico:\nasesoría, consultoría, contadores, abogados, etc.\n\n**NO use para**:\n- Relación con subordinación, horario fijo, herramientas suministradas → es contrato de trabajo\n- Obra material → contrato de obra (Arts. 2053 ss. CC)\n\n## Document Structure\n1. Identificación partes (contratante + contratista)\n2. **PRIMERA. OBJETO** (servicio específico)\n3. **SEGUNDA. ALCANCE Y ACTIVIDADES**\n4. **TERCERA. OBLIGACIONES DEL CONTRATISTA**\n5. **CUARTA. OBLIGACIONES DEL CONTRATANTE**\n6. **QUINTA. VALOR Y FORMA DE PAGO**\n7. **SEXTA. PLAZO DE EJECUCIÓN**\n8. **SÉPTIMA. AUTONOMÍA TÉCNICA Y ADMINISTRATIVA**\n9. **OCTAVA. AFILIACIÓN A SEGURIDAD SOCIAL** (contratista)\n10. **NOVENA. CONFIDENCIALIDAD**\n11. **DÉCIMA. PROPIEDAD INTELECTUAL**\n12. **UNDÉCIMA. CAUSALES DE TERMINACIÓN**\n13. **DUODÉCIMA. CLÁUSULA PENAL**\n14. **DÉCIMA TERCERA. NATURALEZA NO LABORAL**\n15. **OTRAS CLÁUSULAS**\n16. Firmas\n\n## Style Conventions\n\n### Cláusula DÉCIMA TERCERA — NATURALEZA NO LABORAL (CRÍTICA)\nCláusula sacramental para evitar simulación laboral:\n"Las partes manifiestan expresamente que el presente contrato es de\n**NATURALEZA CIVIL (PRESTACIÓN DE SERVICIOS)** conforme a los artículos\n**2063 y siguientes del Código Civil**, y que NO existe entre ellas\nrelación laboral alguna. En consecuencia, el CONTRATISTA conserva\n**plena autonomía técnica y administrativa** en el desarrollo del objeto\ncontractual, asume sus propios riesgos, NO está sujeto a horario, NO\nrecibe instrucciones disciplinarias del CONTRATANTE, y NO genera\nprestaciones sociales propias del contrato de trabajo."\n\n### VII. AUTONOMÍA TÉCNICA Y ADMINISTRATIVA\nElementos OBLIGATORIOS para diferenciar de laboral:\n- Sin horario fijo (excepto reuniones puntuales)\n- Sin subordinación jerárquica (recibe directrices del objeto, no órdenes)\n- Provee sus propias herramientas (laptop, software propio)\n- Trabaja desde su lugar (no en sede obligatoria, salvo reuniones)\n- Atiende otros clientes simultáneamente (no exclusividad)\n\n### Pagos\n- Honorarios totales o por entrega\n- **NO usar** términos "salario", "sueldo", "nómina"\n- Sin retención salarial; sí retención en la fuente según tarifa aplicable\n- Contratista responsable de sus aportes a Seguridad Social (Art. 1 Ley 1562/2012)\n\n### Citas normativas OBLIGATORIAS\n- **Arts. 2063-2069 CC** (Prestación de servicios)\n- **Art. 23 CST + Art. 24 CST** (Elementos del contrato de trabajo — para diferenciar)\n- **Ley 100/1993** (Aportes seguridad social del contratista)\n- **Ley 1562/2012** (Cobertura ARL contratista)\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NOMBRE_CONTRATANTE]`, `[CC/NIT]` | Quien contrata |\n| `[NOMBRE_CONTRATISTA]`, `[CC/NIT]` | Quien presta servicio |\n| `[OBJETO_DEL_SERVICIO]` | Descripción específica |\n| `[VALOR_TOTAL]` o `[VALOR_MENSUAL]` | Honorarios |\n| `[PLAZO_EJECUCION]` | Duración |\n| `[FORMA_PAGO]` | Modalidad |\n| `[PRODUCTOS_ENTREGABLES]` | Si aplica |\n\n## Risk Warnings\n- ⚠ **SIMULACIÓN LABORAL**: si la realidad muestra subordinación, horario,\n  exclusividad → el juez puede declarar contrato de trabajo y obligar a\n  pagar prestaciones (principio "primacía de la realidad" Art. 53 CN)\n- ⚠ **Sin afiliación SGSS**: el contratista debe aportar a salud (12.5%) y\n  pensión (16%) sobre el 40% del valor del contrato (mínimo 1 SMMLV)\n- ⚠ **ARL nivel de riesgo**: el contratante paga ARL si el riesgo es IV o V\n- ⚠ **Cláusula penal**: típicamente 10-20% del valor del contrato\n\n## Bibliography\n1. **Código Civil** Arts. 2063-2069\n2. **CST** Arts. 23-24\n3. **Ley 100/1993** Sistema seguridad social\n4. **Ley 1562/2012** ARL',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/compraventa-vehiculo
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/compraventa-vehiculo',
    'contractual_compraventa_vehiculo_co',
    'Contrato de Compraventa de Vehículo Automotor en Colombia. Régimen del\nCCo (mercantil) o CC (civil). Requiere traspaso ante RUNT después.',
    'drafting',
    '{"name": "contractual_compraventa_vehiculo_co", "description": "Contrato de Compraventa de Vehículo Automotor en Colombia. Régimen del\\nCCo (mercantil) o CC (civil). Requiere traspaso ante RUNT después.", "category": "contractual", "doc_family": "contractual_mercantil", "doc_type": "contrato_compraventa_vehiculo", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/codigo_comercio.html"]}'::jsonb,
    E'# Contrato Compraventa de Vehículo Automotor — Colombia\n\n## When to use\nCompraventa de vehículo entre particulares o entre comerciante y\nparticular. NO requiere escritura pública (a diferencia de inmuebles)\npero SÍ requiere autenticación de firmas para el traspaso en RUNT.\n\n## Document Structure\n1. Identificación partes (vendedor + comprador)\n2. **PRIMERA. OBJETO E IDENTIFICACIÓN DEL VEHÍCULO**\n3. **SEGUNDA. PRECIO Y FORMA DE PAGO**\n4. **TERCERA. ENTREGA Y RECIBO**\n5. **CUARTA. SANEAMIENTO**\n6. **QUINTA. PAZ Y SALVOS** (SOAT, técnico-mecánica, infracciones, impuesto)\n7. **SEXTA. TRASPASO EN RUNT**\n8. **SÉPTIMA. ESTADO TÉCNICO**\n9. **OCTAVA. GARANTÍA** (si aplica)\n10. **NOVENA. DOMICILIO Y JURISDICCIÓN**\n11. Firmas\n\n## Style Conventions\n\n### PRIMERA. Identificación del vehículo (CRÍTICA)\nDEBE incluir TODOS los siguientes datos:\n- **Placa**\n- **Marca**\n- **Modelo** (año)\n- **Línea/Referencia**\n- **Color**\n- **Número de motor**\n- **Número de chasis / VIN**\n- **Número de serie**\n- **Cilindraje**\n- **Tipo de combustible**\n- **Carrocería** (sedán, camioneta, SUV, etc.)\n- **Kilometraje** actual\n\nEjemplo:\n"**OBJETO**: El vendedor transfiere al comprador, quien acepta, el derecho\nde propiedad sobre el siguiente vehículo automotor:\n\n| Característica | Dato |\n|---|---|\n| Placa | [PLACA] |\n| Marca | [MARCA] |\n| Modelo | [ANO_MODELO] |\n| Línea | [LINEA] |\n| Color | [COLOR] |\n| No. Motor | [NUMERO_MOTOR] |\n| No. Chasis | [NUMERO_CHASIS] |\n| Cilindraje | [CILINDRAJE] |\n| Carrocería | [TIPO_CARROCERIA] |\n| Kilometraje | [KILOMETRAJE] km |\n"\n\n### SEGUNDA. Precio\n- Monto en números y letras\n- Forma de pago (contado / cuotas / con crédito)\n- Si crédito: identificar entidad financiera + número crédito\n\n### Citas normativas\n- **Art. 905 CCo** (Compraventa mercantil)\n- **Arts. 1849-1954 CC** (Si compraventa civil)\n- **Ley 769/2002** Código Nacional de Tránsito Arts. 38-39 (Traspaso)\n- **Decreto 1079/2015** (Reglamento RUNT)\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NOMBRE_VENDEDOR]`, `[CC]` | Vendedor |\n| `[NOMBRE_COMPRADOR]`, `[CC]` | Comprador |\n| `[PLACA]`, `[MARCA]`, `[MODELO]` | Datos vehículo |\n| `[NUMERO_MOTOR]`, `[NUMERO_CHASIS]` | Identificadores únicos |\n| `[KILOMETRAJE]` | KM actual |\n| `[PRECIO_NUMERO]`, `[PRECIO_LETRAS]` | Precio |\n| `[FECHA_ENTREGA]` | Cuándo se entrega |\n\n## Risk Warnings\n- ⚠ **TRASPASO RUNT**: el comprador DEBE realizar el traspaso en RUNT\n  dentro de 60 días. De lo contrario, el vendedor sigue siendo\n  responsable de comparendos y multas (Ley 769/2002 Art. 39)\n- ⚠ **Autenticación de firmas**: requerida para el traspaso en RUNT\n- ⚠ **Paz y salvos**: verificar antes de firmar — Simit, Tributo, RTM, SOAT\n- ⚠ **Gravámenes**: verificar en RUNT que el vehículo no tenga prenda,\n  embargo, ni reporte de hurto\n- ⚠ **Si hay crédito vigente**: requiere cancelación del gravamen previo\n\n## Bibliography\n1. **CCo** Arts. 905 ss. (Compraventa mercantil)\n2. **CC** Arts. 1849-1954\n3. **Ley 769/2002** Código Nacional de Tránsito\n4. **Decreto 1079/2015** Reglamento RUNT',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/acta-asamblea
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/acta-asamblea',
    'corporate_acta_asamblea_co',
    'Acta de Asamblea de Accionistas / Junta de Socios en Colombia.\nDocumento societario que recoge decisiones de máximo órgano social.\nRégimen: Ley 1258/2008 (SAS), Ley 222/1995 (sociedades en general),\nCódigo de Comercio (Arts. 181-203).',
    'drafting',
    '{"name": "corporate_acta_asamblea_co", "description": "Acta de Asamblea de Accionistas / Junta de Socios en Colombia.\\nDocumento societario que recoge decisiones de máximo órgano social.\\nRégimen: Ley 1258/2008 (SAS), Ley 222/1995 (sociedades en general),\\nCódigo de Comercio (Arts. 181-203).", "category": "corporate", "doc_family": "corporate_societario", "doc_type": "acta_asamblea", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": ["https://www.secretariasenado.gov.co/senado/basedoc/ley_1258_2008.html", "https://www.secretariasenado.gov.co/senado/basedoc/ley_0222_1995.html"]}'::jsonb,
    E'# Acta de Asamblea de Accionistas / Junta de Socios — Colombia\n\n## When to use\nCuando se reúne el máximo órgano social (Asamblea General de Accionistas\nen SA y SAS, o Junta de Socios en SRL y Ltda) para tomar decisiones que\ndeben quedar registradas. Casos típicos:\n- Asamblea ordinaria anual (aprobación EEFF, dividendos, renovación junta)\n- Asamblea extraordinaria (reformas estatutarias, capitalización, fusión)\n- Decisiones puntuales (autorización para enajenar activos, modificación objeto)\n\n## Document Structure\n1. **Encabezado**: Tipo de acta + número consecutivo\n2. **Datos generales**: fecha, hora, lugar, sociedad, NIT\n3. **Convocatoria y quórum**\n4. **Orden del día**\n5. **Designación presidente y secretario**\n6. **Desarrollo de cada punto**\n7. **Decisiones adoptadas** (numeradas)\n8. **Cierre, lectura y aprobación**\n9. **Firmas presidente + secretario**\n\n## Style Conventions\n\n### Encabezado\n```\nACTA No. [NUMERO_CONSECUTIVO]\nASAMBLEA GENERAL [ORDINARIA / EXTRAORDINARIA] DE ACCIONISTAS\nDE [RAZON_SOCIAL] S.A.S.\nNIT: [NIT]\n```\n\n### Datos generales (sacramentales)\n"En la ciudad de **[CIUDAD]**, departamento de **[DEPARTAMENTO]**, siendo\nlas **[HORA] [a.m./p.m.]** del día **[DIA]** de **[MES]** de **[ANO]**,\nse reunieron en las oficinas de la sociedad ubicadas en\n**[DIRECCION_DOMICILIO_SOCIAL]**, los accionistas de la sociedad\n**[RAZON_SOCIAL] S.A.S.**, identificada con NIT **[NIT]**, con el objeto\nde celebrar Asamblea General **[Ordinaria / Extraordinaria]** de\nAccionistas."\n\n### Convocatoria y quórum (CRÍTICO)\nDEBE incluir:\n- **Convocatoria**: quién convocó, medio, antelación (Art. 424 CCo: 15 días\n  hábiles para ordinaria; Art. 423 para extraordinaria)\n- **Lista de accionistas presentes o representados**:\n\n| Accionista | C.C./NIT | No. Acciones | % Participación | Presencia |\n|---|---|---|---|---|\n| [NOMBRE_1] | [CC_1] | [ACCIONES_1] | [%_1] | Personal/Apoderado |\n| [NOMBRE_2] | [CC_2] | [ACCIONES_2] | [%_2] | Personal/Apoderado |\n\n- **Verificación de quórum**:\n  - **Quórum deliberativo SAS**: mitad + 1 de las acciones suscritas\n    (Art. 22 Ley 1258/2008, salvo estatutos exijan más)\n  - **Quórum decisorio SAS**: mayoría simple presentes\n  - **Para reformas estatutarias**: estatutos pueden exigir mayoría calificada\n\n"Se deja constancia de que se encuentran presentes o debidamente\nrepresentados accionistas titulares de **[NUMERO_ACCIONES_PRESENTES]**\nacciones suscritas y pagadas, equivalentes al **[PORCENTAJE]%** del capital\nsocial, por lo cual existe **QUÓRUM DELIBERATORIO Y DECISORIO** suficiente\nconforme a los estatutos sociales y el Art. 22 de la Ley 1258/2008."\n\n### Orden del día\nNumerado. Ejemplos típicos:\n1. Verificación del quórum\n2. Designación de presidente y secretario de la reunión\n3. Aprobación del orden del día\n4. Informe del representante legal\n5. Lectura y aprobación del informe de gestión\n6. Lectura y aprobación de los estados financieros a 31 de diciembre de [AÑO]\n7. Proyecto de distribución de utilidades\n8. Elección de revisor fiscal y fijación de su remuneración\n9. Renovación de la Junta Directiva\n10. [PUNTO_ESPECIAL]\n11. Proposiciones y varios\n12. Aprobación del acta\n\n### Designación presidente y secretario\n"Por unanimidad de los presentes, se designa como **Presidente** de la\nreunión a **[NOMBRE_PRESIDENTE]** y como **Secretario** a\n**[NOMBRE_SECRETARIO]**, quienes aceptan los cargos y proceden a desarrollar\nlos puntos del orden del día."\n\n### Desarrollo de cada punto\nPara cada decisión, registrar:\n- Presentación del tema\n- Debate (resumen)\n- Votación (a favor / en contra / abstenciones)\n- Decisión final\n\nEjemplo:\n"**PUNTO QUINTO. APROBACIÓN DE LOS ESTADOS FINANCIEROS**\n\nEl Representante Legal presenta a la Asamblea los Estados Financieros\nde propósito general a 31 de diciembre de [AÑO], los cuales fueron\ndebidamente auditados por el Revisor Fiscal [NOMBRE], quien emitió\ndictamen sin salvedades.\n\nSometido a consideración, se aprueban por **[VOTOS_AFAVOR]%** de las\nacciones representadas.\n\n**DECISIÓN**: Se **APRUEBAN** los Estados Financieros de propósito general\nde la sociedad a 31 de diciembre de [AÑO]."\n\n### Cierre y firmas\n"Agotado el orden del día, siendo las **[HORA_CIERRE]**, se levanta la\nsesión, se procede a la lectura del acta, la cual es aprobada por\nunanimidad de los asistentes, en señal de lo cual firman:"\n\n```\n________________________               ________________________\n[NOMBRE_PRESIDENTE]                     [NOMBRE_SECRETARIO]\nPresidente                              Secretario\nC.C. [CC_PRESIDENTE]                    C.C. [CC_SECRETARIO]\n```\n\n### Citas normativas OBLIGATORIAS\n- **Ley 1258/2008** (Régimen SAS) — si la sociedad es SAS\n- **Ley 222/1995** (Reforma sociedades) — sociedades generales\n- **Código de Comercio Arts. 181-203** (Asambleas, Juntas)\n- **Art. 424 CCo** (Convocatoria asamblea ordinaria)\n- **Art. 425 CCo** (Lugar de reunión)\n- Para reformas estatutarias: **Art. 158 CCo** (registro mercantil)\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[RAZON_SOCIAL]`, `[NIT]` | Identificación sociedad |\n| `[NUMERO_ACTA]` | Consecutivo |\n| `[TIPO_ASAMBLEA]` | Ordinaria/Extraordinaria |\n| `[FECHA_REUNION]`, `[HORA]` | Datos de la sesión |\n| `[NOMBRE_PRESIDENTE]`, `[CC]` | Presidente |\n| `[NOMBRE_SECRETARIO]`, `[CC]` | Secretario |\n| `[LISTA_ACCIONISTAS]` | Tabla de asistentes |\n| `[QUORUM_PORCENTAJE]` | % representado |\n| `[ORDEN_DEL_DIA]` | Puntos a tratar |\n| `[DECISIONES]` | Decisiones por punto |\n\n## docx-js Style Hints\n- Tamaño hoja: Letter (Letter US)\n- Fuente: Arial 11pt\n- Heading1 (título "ACTA No. X"): 14pt bold center\n- Tablas con bordes sólidos para quórum\n- Numeración decimal para orden del día\n- Espaciado entre puntos: 12pt antes / 6pt después\n\n## Risk Warnings\n- ⚠ **Convocatoria irregular**: si no se respetan plazos del Art. 424 CCo\n  → asamblea nula. Excepción: asamblea universal (100% asistencia)\n- ⚠ **Falta de quórum**: decisiones nulas (Art. 433 CCo)\n- ⚠ **Reformas estatutarias**: requieren mayoría calificada según estatutos\n  + registro en Cámara de Comercio (Art. 158 CCo)\n- ⚠ **Reparto de utilidades**: máximo el 50% de utilidades líquidas si no\n  hay reserva legal del 50% del capital (Art. 451 CCo)\n- ⚠ **Reuniones no presenciales** (Ley 222/1995 Art. 19, modificada por\n  Decreto 398/2020): permitidas SI los estatutos las autorizan + medio\n  permite verificación de identidad\n\n## Bibliography\n1. **Ley 1258/2008** Régimen SAS\n   https://www.secretariasenado.gov.co/senado/basedoc/ley_1258_2008.html\n2. **Ley 222/1995** Reforma sociedades\n3. **CCo** Arts. 181-203 (Asambleas), 419-446 (S.A.)\n4. **Decreto 398/2020** Reuniones no presenciales',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();

-- /redactar/concepto-juridico
insert into firm_skills (
    firm_id, command, name, description, category, frontmatter,
    system_prompt, references_md, output_schema, jurisdiction,
    user_invocable, tier, status, version, metadata
) values (
    null, '/redactar/concepto-juridico',
    'conceptual_concepto_juridico_co',
    'Concepto Jurídico / Opinión Legal en Colombia. Documento académico-\nprofesional emitido por abogado, firma o área jurídica que analiza\nuna situación o consulta y arroja conclusiones razonadas con sustento\nnormativo, jurisprudencial y doctrinal.',
    'analysis',
    '{"name": "conceptual_concepto_juridico_co", "description": "Concepto Jurídico / Opinión Legal en Colombia. Documento académico-\\nprofesional emitido por abogado, firma o área jurídica que analiza\\nuna situación o consulta y arroja conclusiones razonadas con sustento\\nnormativo, jurisprudencial y doctrinal.", "category": "conceptual", "doc_family": "conceptual_opinion", "doc_type": "concepto_juridico", "default_scope": "doc_type", "language": "es-CO", "jurisdiction": "CO", "version": "1.0.0", "tier": "public", "sources": "[]"}'::jsonb,
    E'# Concepto Jurídico / Opinión Legal — Colombia\n\n## When to use\nSolicitudes en que el cliente o área interna requiere:\n- Análisis de viabilidad jurídica de operación, contrato o proyecto\n- Opinión sobre interpretación normativa\n- Riesgos legales de una conducta o decisión\n- Soporte para due diligence o auditoría legal\n- Memorandos de asesoría regulatoria\n\nNO es: demanda, recurso, contrato. Es un documento de **opinión razonada**.\n\n## Document Structure\n1. **Encabezado**: destinatario + emisor + fecha + radicado\n2. **ASUNTO** (resumen en 1 frase)\n3. **CONSULTA PLANTEADA** (preguntas concretas)\n4. **HECHOS RELEVANTES**\n5. **MARCO NORMATIVO APLICABLE**\n6. **ANÁLISIS JURÍDICO**\n   6.1. Planteamiento del problema jurídico\n   6.2. Doctrina y jurisprudencia aplicables\n   6.3. Aplicación al caso concreto\n7. **CONCLUSIONES** (numeradas, respondiendo a cada pregunta)\n8. **RECOMENDACIONES**\n9. **LIMITACIONES Y SUPUESTOS**\n10. **Firma del concepto** + cédula profesional T.P.\n\n## Style Conventions\n\n### Encabezado profesional\n```\n                            [CIUDAD], [FECHA]\n\n\nCONCEPTO JURÍDICO No. [NUMERO]/[ANO]\n\n\nPara:        [NOMBRE_DESTINATARIO]\n             [CARGO_DESTINATARIO]\n             [ENTIDAD_DESTINATARIO]\n\nDe:          [NOMBRE_EMISOR]\n             [CARGO_EMISOR]\n             T.P. No. [TARJETA_PROFESIONAL] del C.S.J.\n\nAsunto:      [ASUNTO_BREVE]\n\nReferencia:  [REFERENCIAS_INTERNAS]\n```\n\n### Estilo de redacción\n- **Tono profesional impersonal** ("este Despacho concluye", "se considera",\n  "en opinión del suscrito")\n- **Tercera persona** o pasiva refleja\n- **Cita normativa COMPLETA**: ley/decreto + número + año + artículo +\n  inciso/parágrafo si aplica\n- **Cita jurisprudencial**: corporación + número de sentencia + magistrado\n  ponente + fecha\n- **Cita doctrinal**: autor, título, editorial, año, página\n\n### Consulta planteada (precisa)\n"La consulta versa sobre las siguientes preguntas jurídicas concretas:\n\n**(i)** ¿Es jurídicamente viable [PREGUNTA_1]?\n\n**(ii)** ¿Cuáles son los riesgos legales asociados a [PREGUNTA_2]?\n\n**(iii)** ¿Existe alternativa contractual que permita [PREGUNTA_3]?"\n\n### Hechos relevantes\n"Para el análisis se han tenido en cuenta los siguientes hechos\nproporcionados por el consultante, los cuales se presumen ciertos para\nefectos del presente concepto:\n\n**1.** [HECHO_1]\n\n**2.** [HECHO_2]\n\n**3.** [HECHO_3]"\n\n### Marco normativo (jerarquía)\nListar SIEMPRE en orden jerárquico:\n\n**A. Normativa constitucional**\n- **Constitución Política**, Art. [X]: [descripción]\n\n**B. Normativa legal**\n- **[Ley XXXX/YYYY]**, Art. [X]: [descripción]\n- **Código [XX]**, Arts. [X-Y]: [descripción]\n\n**C. Normativa reglamentaria**\n- **Decreto [XXXX/YYYY]**: [descripción]\n- **Resolución [XXX] de [ENTIDAD]**: [descripción]\n\n**D. Jurisprudencia**\n- **Sentencia [TIPO] [NUMERO]/[ANO]**, MP. [NOMBRE_MAGISTRADO]: [tesis]\n\n**E. Doctrina**\n- [AUTOR], *[Título]*, [Editorial], [año], pp. [X-Y].\n\n### Análisis jurídico (CORAZÓN del concepto)\nEstructura recomendada (método silogístico):\n\n**Premisa mayor (norma)**: "El Art. X de la Ley Y establece que..."\n\n**Premisa menor (hechos)**: "En el caso consultado, los hechos muestran que..."\n\n**Conclusión parcial**: "Por lo tanto, [consecuencia jurídica]..."\n\nSub-secciones recomendadas:\n- **6.1. Problema jurídico**\n- **6.2. Régimen aplicable**\n- **6.3. Posiciones jurisprudenciales**\n- **6.4. Aplicación al caso concreto**\n\n### Conclusiones (FORMATO OBLIGATORIO)\nNumeradas y vinculadas a las preguntas de la consulta.\n\n"**CONCLUSIONES**\n\n**PRIMERA**. Respecto a la pregunta (i) sobre [TEMA_1], este Despacho\nconcluye que **[SI / NO / DEPENDE]**, por las razones expuestas en el\nnumeral 6.1 del presente concepto.\n\n**SEGUNDA**. Respecto a la pregunta (ii) sobre [TEMA_2]: [CONCLUSION].\n\n**TERCERA**. Respecto a la pregunta (iii) sobre [TEMA_3]: [CONCLUSION]."\n\n### Recomendaciones (accionables)\n"**RECOMENDACIONES**\n\n**1.** Se recomienda [ACCION_1] con el fin de [PROPOSITO].\n\n**2.** Se sugiere documentar [SOPORTE_PROBATORIO].\n\n**3.** Se aconseja considerar [ALTERNATIVA]."\n\n### Limitaciones y supuestos (OBLIGATORIO)\n"**LIMITACIONES DEL CONCEPTO**\n\nEl presente concepto:\n\n**(a)** Se basa exclusivamente en los hechos proporcionados por el\nconsultante y la normativa vigente a la fecha de emisión.\n\n**(b)** No analiza aspectos contables, financieros, tributarios o técnicos\nsalvo que se mencionen expresamente.\n\n**(c)** No constituye asesoría procesal; cualquier decisión de iniciar o\ncontestar procesos requiere análisis adicional.\n\n**(d)** Su vigencia está condicionada a que las normas citadas no sean\nmodificadas, derogadas o declaradas inexequibles."\n\n### Cierre\n```\nAtentamente,\n\n\n_________________________________\n[NOMBRE_EMISOR]\n[CARGO_EMISOR]\nT.P. [NUMERO] del C.S.J.\n[EMAIL] / [TELEFONO]\n```\n\n### Common Placeholders\n| Placeholder | Descripción |\n|---|---|\n| `[NOMBRE_DESTINATARIO]`, `[CARGO]` | A quién va |\n| `[NOMBRE_EMISOR]`, `[T.P.]` | Quien lo emite |\n| `[ASUNTO_BREVE]` | Resumen 1 frase |\n| `[CONSULTA_1]`...`[CONSULTA_N]` | Preguntas concretas |\n| `[HECHOS_RELEVANTES]` | Lista numerada |\n| `[NORMAS_APLICABLES]` | Marco normativo |\n| `[CONCLUSIONES]` | Por pregunta |\n| `[RECOMENDACIONES]` | Acciones sugeridas |\n\n## docx-js Style Hints\n- Letter US\n- Times New Roman 12pt (estilo académico) o Arial 11pt\n- Heading1 (CONCEPTO JURÍDICO No. X): 14pt bold center\n- Heading2 (secciones): 12pt bold\n- Cita normativa: cursiva\n- Cita jurisprudencial: cursiva + "MP." después de magistrado\n- Notas al pie: 10pt\n- Sangría primera línea: 0.5" (excepto encabezado)\n\n## Risk Warnings\n- ⚠ **Responsabilidad profesional**: el concepto compromete al abogado.\n  Verificar siempre vigencia de normas citadas (NO citar derogadas)\n- ⚠ **Citas verificables**: cada norma debe poder buscarse en SUIN-Juriscol;\n  cada sentencia debe existir en relatoría de la Corte respectiva\n- ⚠ **No "papel mojado"**: evitar conclusiones evasivas tipo "depende";\n  si depende, indicar **DE QUÉ** depende y CUÁL es la respuesta en cada escenario\n- ⚠ **Privilegio profesional**: marcar como "CONFIDENCIAL" si va a cliente\n- ⚠ **Conflicto de intereses**: declararlo si emisor tiene interés en el asunto\n- ⚠ **Citas inventadas (alucinaciones)**: PROHIBIDO citar sentencias que\n  no existen. SIEMPRE verificar con relatoría de cada corporación\n\n## Bibliography\n1. **Constitución Política de Colombia**\n2. **Código Civil**\n3. **Código General del Proceso** (Ley 1564/2012)\n4. **Sentencias** (verificar en relatoría)\n   - Corte Constitucional: https://www.corteconstitucional.gov.co\n   - Corte Suprema de Justicia: https://cortesuprema.gov.co\n   - Consejo de Estado: https://www.consejodeestado.gov.co\n5. **SUIN-Juriscol**: https://www.suin-juriscol.gov.co (verificación normas)',
    null,
    null,
    'CO',
    true, 'public', 'published', 1,
    '{"seeded_by":"sprint_m1927","seed_date":"2026-05-28"}'::jsonb
)
on conflict (firm_id, command, version) do update set
    name = excluded.name,
    description = excluded.description,
    category = excluded.category,
    frontmatter = excluded.frontmatter,
    system_prompt = excluded.system_prompt,
    tier = excluded.tier,
    status = 'published',
    updated_at = now();


-- Verificación final
do $$
declare
  v_count int;
begin
  select count(*) into v_count
    from firm_skills
   where firm_id is null
     and status = 'published'
     and command in (
       '/redactar/revocatoria-poder',
       '/redactar/declaracion-extrajuicio',
       '/redactar/demanda-civil-ordinaria',
       '/redactar/demanda-laboral',
       '/redactar/demanda-admin-nulidad',
       '/redactar/recurso-apelacion',
       '/redactar/contestacion-demanda',
       '/redactar/requerimiento-extrajudicial',
       '/redactar/prestacion-servicios',
       '/redactar/compraventa-vehiculo',
       '/redactar/acta-asamblea',
       '/redactar/concepto-juridico'
     );
  if v_count < 12 then
    raise exception 'Sprint M19.27 seeded only % of 12 expected skills', v_count;
  end if;
  raise notice 'Sprint M19.27 seeded % skills OK', v_count;
end$$;

commit;
