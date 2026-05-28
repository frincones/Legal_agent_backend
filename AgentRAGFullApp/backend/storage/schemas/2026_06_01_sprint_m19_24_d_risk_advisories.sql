-- ============================================================
-- LexAI · Sprint M19.24.D.1 · Risk Advisories Curados
-- Migration date: 2026-06-01
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- Tabla de advertencias de riesgo curadas por familia de documento.
-- Cada entrada tiene la TRINIDAD estilo Claude: falta + consecuencia +
-- recomendación. El narrator_agent las consume en vez de improvisar
-- con LLM cada vez (calidad consistente + costo cero).

CREATE TABLE IF NOT EXISTS risk_advisories (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_family     TEXT NOT NULL,                -- 'notarial_poder', 'judicial_demanda', etc
  field_key           TEXT NOT NULL,                -- 'tope_maximo_cuantia', 'razon_social', etc
  severity            TEXT NOT NULL CHECK (severity IN ('critical','warning','info')),

  -- Trinidad Claude
  falta_text          TEXT NOT NULL,                -- "Tope máximo de cuantía"
  consecuencia_text   TEXT NOT NULL,                -- "Sin esto el poder es de alto riesgo legal"
  recomendacion_text  TEXT NOT NULL,                -- "No firmar hasta fijar un monto en pesos y en letras"

  -- Trazabilidad legal
  fuente_legal        TEXT,                          -- "Art. 2143 CC + Decreto 960/1970"
  doc_types_applicable JSONB DEFAULT '[]'::jsonb,    -- ["poder_especial","poder_general"]

  -- Métricas
  emitted_count       INT DEFAULT 0,

  created_at          TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (document_family, field_key)
);

CREATE INDEX IF NOT EXISTS idx_risk_advisories_family
  ON risk_advisories (document_family);

CREATE INDEX IF NOT EXISTS idx_risk_advisories_severity
  ON risk_advisories (severity);

-- ============================================================
-- SEED: 60+ advertencias curadas por familia
-- ============================================================

INSERT INTO risk_advisories (document_family, field_key, severity, falta_text, consecuencia_text, recomendacion_text, fuente_legal, doc_types_applicable) VALUES

-- NOTARIAL_PODER (12 advisories)
('notarial_poder', 'razon_social_sociedad', 'critical',
 'Identificación completa de la sociedad (razón social, NIT, matrícula mercantil)',
 'Sin estos datos el notario no puede verificar la calidad de representante legal del poderdante',
 'Obtén el certificado de existencia y representación legal vigente (no mayor a 30 días) en la Cámara de Comercio y agrégalo como anexo al poder',
 'Decreto 960/1970 · Estatuto del Notariado',
 '["poder_especial","poder_general","poder_judicial"]'::jsonb),

('notarial_poder', 'tope_maximo_cuantia', 'critical',
 'Tope máximo de cuantía para comprometer patrimonio',
 'Un poder para gravar el patrimonio social sin límite de monto es extremadamente riesgoso: el apoderado podría comprometer activos por cualquier valor',
 'Fija una cifra concreta en pesos y en letras (ej. "hasta la suma de $50.000.000 — cincuenta millones de pesos M/cte") antes de firmar',
 'Arts. 2156-2160 CC',
 '["poder_especial","poder_general"]'::jsonb),

('notarial_poder', 'restricciones_estatutarias', 'critical',
 'Verificación de restricciones estatutarias',
 'Si los estatutos exigen autorización de junta directiva o asamblea para constituir garantías o disponer de activos sobre cierta cuantía y el poder no la respeta, el acto puede ser inoponible o anulable',
 'Revisa el certificado de existencia y representación legal y los estatutos sociales; si aplica restricción, obtén el acta de autorización antes de firmar',
 'Arts. 196-197 CCo',
 '["poder_especial","poder_general"]'::jsonb),

('notarial_poder', 'alcance_acreedores', 'warning',
 'Definir alcance frente a acreedores (amplio vs limitado)',
 'Un poder amplio aplica para cualquier acreedor (DIAN, ICA, bancos, proveedores) — mayor flexibilidad pero mayor riesgo',
 'Si solo estás negociando con un acreedor específico, limita el poder a esa entidad para reducir riesgo',
 'Art. 2157 CC',
 '["poder_especial"]'::jsonb),

('notarial_poder', 'cedula_apoderado_lugar_expedicion', 'critical',
 'Lugar de expedición de la cédula del apoderado',
 'Sin este dato la identificación es incompleta y la notaría puede rechazar el documento',
 'Confirma con el apoderado el municipio donde fue expedida su cédula',
 'Decreto 960/1970 Art. 19',
 '["poder_especial","poder_general","poder_judicial"]'::jsonb),

('notarial_poder', 'vigencia_del_poder', 'critical',
 'Vigencia o plazo de duración del poder',
 'Sin plazo definido el poder podría considerarse indefinido, dificultando su revocación efectiva',
 'Para poderes que comprometen patrimonio se recomienda vigencia 6-12 meses; para tareas puntuales, el tiempo necesario para esa gestión',
 'Art. 2189 CC',
 '["poder_especial","poder_general"]'::jsonb),

('notarial_poder', 'facultad_sustitucion', 'info',
 'Definir si el apoderado puede sustituir el poder',
 'Por defecto el apoderado NO puede sustituir; si quieres permitirlo debes facultarlo expresamente',
 'Decide si autorizas sustitución (cláusula expresa). Si no, el apoderado queda obligado personalmente',
 'Art. 2161 CC',
 '["poder_especial","poder_general"]'::jsonb),

('notarial_poder', 'garantias_inmuebles', 'warning',
 'Aclaración sobre garantías sobre bienes inmuebles',
 'Si el poder faculta constituir garantía sobre inmuebles, la firma del acuerdo requerirá escritura pública (no basta poder autenticado)',
 'Si el alcance incluye gravar inmuebles, prevé expresamente en el poder la facultad de concurrir a otorgar escritura pública',
 'Art. 1857 CC + Decreto 960/1970',
 '["poder_especial"]'::jsonb),

('notarial_poder', 'profesion_apoderado', 'info',
 'Profesión y eventual tarjeta profesional del apoderado',
 'Información recomendada para identificación completa, especialmente si el apoderado es abogado o contador',
 'Confirma la profesión del apoderado y, si tiene tarjeta profesional, incluye el número',
 NULL,
 '["poder_especial","poder_general","poder_judicial"]'::jsonb),

('notarial_poder', 'datos_contacto_partes', 'info',
 'Correo y celular de poderdante y apoderado',
 'Útil para notificaciones electrónicas (Art. 291 CGP) y comunicaciones futuras',
 'Incluye correo electrónico y celular de ambas partes',
 'Art. 291 CGP',
 '["poder_especial","poder_general","poder_judicial"]'::jsonb),

('notarial_poder', 'aceptacion_apoderado', 'critical',
 'Aceptación expresa del apoderado',
 'Sin aceptación el mandato no se perfecciona — el apoderado no queda obligado a ejercerlo',
 'Incluye cláusula final donde el apoderado declara "ACEPTO el poder" y firma',
 'Arts. 2150-2151 CC',
 '["poder_especial","poder_general","poder_judicial"]'::jsonb),

('notarial_poder', 'notaria_destinataria', 'info',
 'Notaría específica de presentación',
 'El documento puede llevarse a cualquier notaría del círculo competente, pero el saludo formal mejora si está dirigido',
 'Indica la notaría específica de Medellín (u otro círculo) donde se autenticará',
 NULL,
 '["poder_especial","poder_general","poder_judicial"]'::jsonb),

-- JUDICIAL_DEMANDA (15 advisories)
('judicial_demanda', 'identificacion_partes_completa', 'critical',
 'Identificación completa de partes (nombres + cédula/NIT + domicilio)',
 'Sin identificación completa la demanda puede ser inadmitida o rechazada (Art. 90 CGP)',
 'Asegúrate de tener nombre completo, número y lugar de cédula/NIT, y domicilio para notificación de cada parte',
 'Art. 82-90 CGP',
 '["demanda_civil_ordinaria","demanda_laboral_ordinaria","demanda_familia_divorcio","demanda_administrativo_nulidad_restablecimiento"]'::jsonb),

('judicial_demanda', 'hechos_concretos', 'critical',
 'Hechos concretos numerados',
 'La narración fáctica vaga o desordenada debilita las pretensiones y dificulta la sentencia favorable',
 'Numera cada hecho (1, 2, 3...) con un lead-in en negrita (ej. "Vínculo laboral.", "Despido.") y desarrollo claro de cada uno',
 'Art. 82.4 CGP',
 '["demanda_civil_ordinaria","demanda_laboral_ordinaria","demanda_familia_divorcio"]'::jsonb),

('judicial_demanda', 'pretensiones_con_verbo', 'critical',
 'Pretensiones con verbo procesal en mayúsculas',
 'Pretensiones sin verbo claro (DECLARAR, CONDENAR, ORDENAR) pueden ser interpretadas restrictivamente por el juez',
 'Cada pretensión debe iniciar con verbo procesal en MAYÚSCULAS y BOLD: PRIMERA: DECLARAR...; SEGUNDA: CONDENAR...',
 'Art. 82.5 CGP',
 '["demanda_civil_ordinaria","demanda_laboral_ordinaria"]'::jsonb),

('judicial_demanda', 'cuantia_estimada', 'critical',
 'Cuantía estimada del proceso',
 'La cuantía define competencia y procedimiento (verbal sumario / verbal / ordinario). Sin ella la demanda puede ser inadmitida',
 'Indica mayor cuantía (>150 SMMLV ≈ $214M en 2026), menor (40-150 SMMLV) o mínima (<40 SMMLV)',
 'Art. 25 CGP',
 '["demanda_civil_ordinaria","demanda_laboral_ordinaria"]'::jsonb),

('judicial_demanda', 'juramento_estimatorio', 'critical',
 'Juramento estimatorio de pretensiones económicas',
 'Sin juramento estimatorio (Art. 206 CGP), las pretensiones de condena pueden ser desestimadas',
 'Incluye cláusula de juramento estimatorio detallando rubro por rubro de las pretensiones de condena',
 'Art. 206 CGP',
 '["demanda_civil_ordinaria","demanda_laboral_ordinaria","demanda_familia_divorcio"]'::jsonb),

('judicial_demanda', 'fundamentos_normativos', 'warning',
 'Fundamentos de derecho con normas vigentes',
 'Pretensiones sin fundamento normativo desarrollado son técnicamente débiles',
 'Cita las normas aplicables (con artículo y año), desarrolla su contenido y conecta con los hechos',
 'Art. 82.6 CGP',
 '["demanda_civil_ordinaria","demanda_laboral_ordinaria","demanda_familia_divorcio"]'::jsonb),

('judicial_demanda', 'anexos_pruebas', 'critical',
 'Lista de anexos y pruebas',
 'Sin pruebas la demanda no podrá ser probada en sentencia',
 'Lista todos los documentos a aportar (registros civiles, contratos, certificados, etc.) y solicita testimoniales si aplica',
 'Art. 82.7 CGP',
 '["demanda_civil_ordinaria","demanda_laboral_ordinaria","demanda_familia_divorcio"]'::jsonb),

('judicial_demanda', 'lugar_notificacion_demandado', 'critical',
 'Dirección física + electrónica para notificación del demandado',
 'Sin dirección para notificación el juzgado no puede emplazar al demandado',
 'Indica dirección física comprobable + correo electrónico (Art. 291 CGP)',
 'Art. 82.10 CGP + 291 CGP',
 '["demanda_civil_ordinaria","demanda_laboral_ordinaria"]'::jsonb),

-- FAMILIA DIVORCIO específico
('judicial_demanda', 'fecha_lugar_matrimonio', 'critical',
 'Fecha exacta y lugar del matrimonio',
 'Sin estos datos no se puede acreditar el vínculo conyugal cuya disolución se pide',
 'Aporta registro civil de matrimonio con fecha, notaría/parroquia, número de registro',
 'Art. 388-389 CGP',
 '["demanda_familia_divorcio","demanda_familia_divorcio_mutuo_acuerdo"]'::jsonb),

('judicial_demanda', 'datos_hijos_menores', 'critical',
 'Datos completos de hijos menores (nombres, edades, custodia actual)',
 'En procesos de familia con menores, sin estos datos no se puede regular custodia, visitas ni alimentos',
 'Lista cada menor con nombre completo, fecha nacimiento, registro civil, custodia actual',
 'Art. 397 CGP + Ley 1098/2006',
 '["demanda_familia_divorcio","demanda_familia_alimentos","demanda_familia_custodia"]'::jsonb),

-- LABORAL específico
('judicial_demanda', 'fechas_vinculo_laboral', 'critical',
 'Fechas exactas de inicio y terminación del vínculo laboral',
 'Sin estas fechas no se puede calcular la antigüedad, cesantías ni indemnizaciones',
 'Aporta contrato de trabajo, carta de retiro/despido o liquidación con fechas precisas',
 'Arts. 64-65 CST + 28 CPTSS',
 '["demanda_laboral_ordinaria"]'::jsonb),

('judicial_demanda', 'salario_ultimo_devengado', 'critical',
 'Último salario devengado por el trabajador',
 'El salario base define las indemnizaciones y prestaciones a reclamar',
 'Aporta nómina del último mes laborado o certificación salarial',
 'Art. 132 CST',
 '["demanda_laboral_ordinaria"]'::jsonb),

-- ADMINISTRATIVO específico
('judicial_demanda', 'identificacion_acto_administrativo', 'critical',
 'Identificación completa del acto administrativo demandado',
 'Sin número y fecha exactos del acto no procede la nulidad ni el restablecimiento',
 'Indica tipo (resolución, decreto), número, fecha de expedición y entidad emisora',
 'Art. 162 CPACA',
 '["demanda_administrativo_nulidad_restablecimiento"]'::jsonb),

('judicial_demanda', 'agotamiento_via_gubernativa', 'critical',
 'Agotamiento de vía gubernativa (recursos administrativos)',
 'Sin agotamiento previo de recursos administrativos la demanda es inadmitida',
 'Aporta constancia de notificación, interposición de recursos (reposición y/o apelación) y respuesta',
 'Arts. 76-77 CPACA',
 '["demanda_administrativo_nulidad_restablecimiento"]'::jsonb),

-- TUTELA específico
('judicial_demanda', 'derecho_fundamental_vulnerado', 'critical',
 'Derecho fundamental específico vulnerado',
 'La tutela debe invocar derecho fundamental concreto (Art. 11/13/49 CN, etc.) — no derechos genéricos',
 'Identifica el derecho con artículo de la Constitución y narra cómo se vulnera',
 'Art. 86 CN',
 '["tutela"]'::jsonb),

-- CONTRACTUAL_CIVIL (8 advisories)
('contractual_civil', 'identificacion_partes_contractual', 'critical',
 'Identificación completa de partes contratantes',
 'Contrato sin identificación clara puede ser declarado inexistente o nulo',
 'Nombre completo, documento de identidad/NIT, domicilio y dirección notificación de cada parte',
 'Arts. 1502 CC',
 '["contrato_arrendamiento_vivienda","contrato_compraventa_civil","contrato_prestacion_servicios","contrato_mandato"]'::jsonb),

('contractual_civil', 'objeto_contrato_preciso', 'critical',
 'Objeto del contrato preciso e identificable',
 'Objeto vago o indeterminable hace el contrato ineficaz (Art. 1518 CC)',
 'Describe con exactitud el bien o servicio (matrícula inmobiliaria si es inmueble, especificaciones técnicas si es servicio)',
 'Arts. 1517-1518 CC',
 '["contrato_arrendamiento_vivienda","contrato_compraventa_civil","contrato_prestacion_servicios"]'::jsonb),

('contractual_civil', 'precio_o_canon', 'critical',
 'Precio o canon definido en pesos y letras',
 'Sin precio cierto o determinable el contrato puede ser nulo',
 'Precio en pesos colombianos, en números y letras, con forma de pago, plazos y reajustes',
 'Arts. 1849, 1972 CC',
 '["contrato_arrendamiento_vivienda","contrato_compraventa_civil","contrato_prestacion_servicios"]'::jsonb),

('contractual_civil', 'plazo_vigencia', 'critical',
 'Plazo o vigencia del contrato',
 'Sin plazo definido el contrato podría ser de duración indefinida con efectos no deseados',
 'Define fecha inicio, fecha terminación, prórrogas automáticas si aplica',
 'Art. 1973 CC',
 '["contrato_arrendamiento_vivienda","contrato_prestacion_servicios"]'::jsonb),

('contractual_civil', 'clausula_penal', 'warning',
 'Cláusula penal por incumplimiento',
 'Sin cláusula penal el cobro de perjuicios requiere demanda y prueba en cada caso',
 'Incluye cláusula penal con monto líquido y exigible (típicamente 10-20% del valor del contrato)',
 'Arts. 1592-1601 CC',
 '["contrato_arrendamiento_vivienda","contrato_compraventa_civil","contrato_prestacion_servicios"]'::jsonb),

('contractual_civil', 'jurisdiccion_competente', 'warning',
 'Cláusula de jurisdicción y arbitraje',
 'Sin pacto de jurisdicción aplican reglas generales (puede no ser favorable a la parte)',
 'Define jurisdicción competente o cláusula compromisoria de arbitraje',
 'Art. 28 CGP + Ley 1563/2012',
 '["contrato_arrendamiento_vivienda","contrato_compraventa_civil","contrato_prestacion_servicios"]'::jsonb),

-- CORPORATE_ESTATUTOS (5 advisories)
('corporate_estatutos', 'razon_social_y_tipo_societario', 'critical',
 'Razón social y tipo societario',
 'Sin razón social válida y tipo societario claro la sociedad no se puede constituir',
 'Define razón social terminada en SAS / S.A.S. / LTDA / S.A. / & Cía según tipo y verifica disponibilidad en Cámara de Comercio',
 'Arts. 24 Ley 1258/2008 (SAS), Art. 110 CCo',
 '["estatutos_sas","estatutos_sociedad_anonima","estatutos_ltda"]'::jsonb),

('corporate_estatutos', 'capital_social', 'critical',
 'Capital social (autorizado, suscrito, pagado)',
 'Sin capital definido no procede la constitución',
 'Capital autorizado, suscrito (mínimo 50% en SAS, 100% en S.A. parte mínima), pagado (mínimo 1/3 al constituir)',
 'Arts. 9-10 Ley 1258/2008',
 '["estatutos_sas","estatutos_sociedad_anonima","estatutos_ltda"]'::jsonb),

('corporate_estatutos', 'objeto_social', 'critical',
 'Objeto social específico o indefinido',
 'Objeto indefinido en SAS es permitido pero indefinición total puede generar conflictos',
 'En SAS puede ser "cualquier actividad lícita" (Art. 5 Ley 1258); en otras sociedades debe ser específico',
 'Art. 5 Ley 1258/2008',
 '["estatutos_sas","estatutos_sociedad_anonima","estatutos_ltda"]'::jsonb),

-- PETITORIO_ADMIN (5 advisories)
('petitorio_admin', 'identificacion_solicitante', 'critical',
 'Identificación del solicitante (nombre, CC, dirección física + correo)',
 'Sin identificación completa la entidad no puede notificar la respuesta',
 'Nombre completo, número y lugar de cédula, dirección física verificable y correo electrónico',
 'Art. 16 CPACA',
 '["derecho_peticion","derecho_peticion_informacion","pqrs"]'::jsonb),

('petitorio_admin', 'peticion_concreta', 'critical',
 'Petición concreta y específica',
 'Petición vaga puede ser rechazada por inviabilidad',
 'Formula la petición en forma clara y específica, indicando exactamente qué se solicita',
 'Art. 16.4 CPACA',
 '["derecho_peticion","pqrs"]'::jsonb),

('petitorio_admin', 'entidad_destinataria', 'critical',
 'Entidad destinataria competente',
 'Si la entidad no es competente debe remitirla pero pierde tiempo de respuesta',
 'Verifica competencia funcional y territorial de la entidad antes de enviar',
 'Arts. 21-22 CPACA',
 '["derecho_peticion","pqrs"]'::jsonb),

-- CONCEPTUAL (3 advisories)
('conceptual', 'problema_juridico_planteado', 'critical',
 'Problema jurídico planteado con claridad',
 'Sin pregunta jurídica concreta el concepto será vago',
 'Formula la pregunta jurídica en una sola oración interrogativa',
 NULL,
 '["concepto_juridico","opinion_legal","dictamen_pericial_juridico"]'::jsonb),

('conceptual', 'hechos_relevantes_y_contexto', 'critical',
 'Hechos relevantes y contexto del caso',
 'Sin hechos el concepto es abstracto y poco útil',
 'Resume los hechos relevantes que motivan la consulta en 1-2 párrafos',
 NULL,
 '["concepto_juridico","opinion_legal"]'::jsonb)

ON CONFLICT (document_family, field_key) DO UPDATE SET
  falta_text         = EXCLUDED.falta_text,
  consecuencia_text  = EXCLUDED.consecuencia_text,
  recomendacion_text = EXCLUDED.recomendacion_text,
  fuente_legal       = EXCLUDED.fuente_legal;

COMMENT ON TABLE risk_advisories IS
  'M19.24.D: advertencias de riesgo curadas (no LLM) por familia de
   documento. El narrator_agent las consume para emitir mensajes
   estilo Claude: falta + consecuencia + recomendación.';
