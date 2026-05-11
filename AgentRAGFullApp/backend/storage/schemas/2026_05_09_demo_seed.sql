-- Demo seed v2 · Aguilar vs Avianca (corrected to real schema)
-- Idempotent. Applied via Supabase Management API (no \set / no client directives).

begin;

do $$
declare
  v_firm_id        uuid := 'd0000001-0000-0000-0000-00000000d1f0';
  v_client_id      uuid := 'd0000002-0000-0000-0000-00000000c11e';
  v_matter_id      uuid := 'd0000003-0000-0000-0000-00000000ca50';
  v_doc_id         uuid := 'd0000004-0000-0000-0000-00000000d0c0';
  v_version_id    uuid := 'd0000005-0000-0000-0000-00000000d0c1';
  v_demo_user_id   uuid := '5c972681-f535-469f-856a-fda77571561a'::uuid;
begin

-- 1. Firm
insert into firms (id, razon_social, country, domain, plan, seats)
values (v_firm_id, 'LexAI Demo Firm', 'co', 'demo.lexai.co', 'trial', 3)
on conflict (id) do update set razon_social = excluded.razon_social;

-- 2. Promote demo user to be member of demo firm
update users set firm_id = v_firm_id, role = 'lawyer'
 where id = v_demo_user_id;

-- 2b. If users row missing, create the profile
insert into users (id, firm_id, email, full_name, role)
values (v_demo_user_id, v_firm_id, 'demo@lexai.co', 'LexAI Demo', 'lawyer')
on conflict (id) do nothing;

-- 3. Client
insert into clients (
  id, firm_id, tipo, nombre, personal_id, email, telefono,
  consent_at, consent_version, consent_finalidades, metadata
)
values (
  v_client_id, v_firm_id, 'persona_natural',
  'Juan Aguilar Velásquez', '79.534.821',
  'juan.aguilar@example.com', '+57 300 555 0123',
  now() - interval '14 days', 1,
  array['representacion_judicial','asesoria_legal'],
  jsonb_build_object('demo', true)
)
on conflict (id) do update set nombre = excluded.nombre;

-- 4. Matter
insert into matters (
  id, firm_id, client_id, display_id, titulo, materia, etapa_procesal,
  tribunal, juzgado, expediente, status, priority, owner_user_id,
  proxima_fecha, proxima_tipo, cuantia, cuantia_currency,
  pendientes, is_demo, metadata
)
values (
  v_matter_id, v_firm_id, v_client_id, 'AGU-2026-001',
  'Aguilar vs. Avianca · Acción de Tutela',
  'civil', 'Primera instancia',
  'Tribunal Superior de Bogotá',
  'Juzgado 14 Civil del Circuito de Bogotá',
  '11001-31-03-014-2026-00128-00',
  'activo', 'alta', v_demo_user_id,
  now() + interval '5 days', 'audiencia',
  18000000, 'COP', 3, true,
  jsonb_build_object(
    'demo', true,
    'contraparte_nombre', 'Aerovías del Continente Americano S.A. (Avianca)',
    'contraparte_nit', '890.100.577-6',
    'tipo_proceso', 'tutela'
  )
)
on conflict (id) do update
  set titulo = excluded.titulo,
      proxima_fecha = excluded.proxima_fecha;

-- 5. Matter parties (rol, not role; client_id, not contact_user_id)
insert into matter_parties (firm_id, matter_id, rol, nombre, tax_id, client_id, metadata)
values
  (v_firm_id, v_matter_id, 'demandante',
   'Juan Aguilar Velásquez', null, v_client_id,
   jsonb_build_object('source','demo')),
  (v_firm_id, v_matter_id, 'demandada',
   'Aerovías del Continente Americano S.A. (Avianca)',
   '890.100.577-6', null,
   jsonb_build_object('representante_legal','Sin información'))
on conflict do nothing;

-- 6. Timeline
insert into matter_timeline (firm_id, matter_id, ts, kind, payload)
values
  (v_firm_id, v_matter_id, now() - interval '21 days', 'caso_creado',
   jsonb_build_object('descripcion','Apertura del caso · primera entrevista')),
  (v_firm_id, v_matter_id, now() - interval '18 days', 'documento_recibido',
   jsonb_build_object('descripcion','Recepción de tiquete cancelado y comunicaciones')),
  (v_firm_id, v_matter_id, now() - interval '14 days', 'consentimiento_habeas_data',
   jsonb_build_object('descripcion','Cliente firma autorización Habeas Data')),
  (v_firm_id, v_matter_id, now() - interval '10 days', 'derecho_peticion',
   jsonb_build_object('descripcion','Radicado derecho de petición a Avianca')),
  (v_firm_id, v_matter_id, now() - interval '5 days', 'tutela_radicada',
   jsonb_build_object('descripcion','Acción de tutela radicada · Juzgado 14 Civil')),
  (v_firm_id, v_matter_id, now() - interval '3 days', 'admision_tutela',
   jsonb_build_object('descripcion','Admisión de la tutela · traslado por 2 días')),
  (v_firm_id, v_matter_id, now() - interval '1 day', 'contestacion_recibida',
   jsonb_build_object('descripcion','Avianca contesta la tutela')),
  (v_firm_id, v_matter_id, now() + interval '5 days', 'audiencia_programada',
   jsonb_build_object('descripcion','Audiencia de pruebas · Juzgado 14 Civil'))
on conflict do nothing;

-- 7. Matter document (canvas)
insert into matter_documents (
  id, firm_id, matter_id, kind, titulo, status, pages, uploaded_by, metadata
)
values (
  v_doc_id, v_firm_id, v_matter_id, 'generado',
  'Canvas · AGU-2026-001', 'completed', 1, v_demo_user_id,
  jsonb_build_object('demo', true)
)
on conflict (id) do update set titulo = excluded.titulo;

-- 8. First version (storage_path + sha256 are NOT NULL)
insert into matter_document_versions (
  id, matter_document_id, firm_id, version,
  storage_path, sha256, generated_by, diff_from_prev
)
values (
  v_version_id, v_doc_id, v_firm_id, 1,
  format('demo/%s/v1.html', v_doc_id),
  '0000000000000000000000000000000000000000000000000000000000000000',
  'demo_seed',
  jsonb_build_object(
    'html',
    '<h1>ACCIÓN DE TUTELA</h1>'
    '<p><strong>HONORABLE JUEZ:</strong></p>'
    '<p>Yo, <strong>Juan Aguilar Velásquez</strong>, mayor de edad, identificado con cédula '
    'de ciudadanía No. 79.534.821 expedida en Bogotá, en ejercicio del derecho consagrado '
    'en el artículo 86 de la Constitución Política de Colombia, presento ante su Despacho '
    'la presente <strong>ACCIÓN DE TUTELA</strong> contra <strong>Aerovías del Continente '
    'Americano S.A. (Avianca)</strong>, NIT 890.100.577-6.</p>'
    '<h2>I. HECHOS</h2>'
    '<p><strong>Primero:</strong> El día 12 de marzo de 2026 adquirí el tiquete aéreo '
    'No. 134-9876543210 con la aerolínea accionada para el trayecto Bogotá–Cartagena por '
    'valor de COP $1.420.000.</p>'
    '<p><strong>Segundo:</strong> Cuatro horas antes de la salida programada, la aerolínea '
    'canceló el vuelo unilateralmente sin ofrecer reubicación oportuna ni reembolso.</p>'
    '<p><strong>Tercero:</strong> Mi viaje obedecía a una cita médica programada con un '
    'especialista en cardiología.</p>'
    '<h2>II. DERECHOS VULNERADOS</h2>'
    '<ul>'
    '<li><strong>Derecho a la salud</strong> (Art. 49 C.P.).</li>'
    '<li><strong>Trato digno</strong> (Art. 78 C.P.).</li>'
    '<li><strong>Debido proceso</strong> (Art. 29 C.P.).</li>'
    '</ul>'
    '<h2>III. FUNDAMENTACIÓN DE DERECHO</h2>'
    '<p>La Corte Constitucional, en sentencia <strong>T-388/2019</strong>, ha reiterado que '
    'las empresas prestadoras de servicios públicos esenciales tienen el deber de proteger '
    'los derechos fundamentales de sus usuarios. Igualmente, la sentencia '
    '<strong>SU-449/2020</strong> establece el deber de diligencia reforzado cuando media '
    'una circunstancia médica acreditada. La sentencia <strong>C-200/1995</strong> '
    'desarrolla el alcance del derecho al debido proceso en relaciones de consumo.</p>'
    '<p>El artículo 1880 del Código de Comercio regula las obligaciones del transportador '
    'aéreo. La <strong>Ley 1480 de 2011</strong> (Estatuto del Consumidor) consagra el '
    'derecho a la información clara y veraz.</p>'
    '<h2>IV. PRETENSIONES</h2>'
    '<ol>'
    '<li>Tutelar los derechos fundamentales invocados.</li>'
    '<li>Ordenar a Avianca el reembolso de COP $1.420.000.</li>'
    '<li>Ordenar a la accionada presentar disculpas públicas.</li>'
    '</ol>'
    '<p>Atentamente,</p>'
    '<p><strong>Juan Aguilar Velásquez</strong><br>C.C. 79.534.821</p>',
    'byte_size', 3200,
    'note', 'demo seed v1'
  )
)
on conflict (id) do nothing;

-- 9. Document citations (real schema: matter_document_id, citation_ref, rubro_inserted, estado)
insert into document_citations (
  firm_id, matter_document_id, citation_ref, rubro_inserted, estado, match_score
)
values
  (v_firm_id, v_doc_id, 'T-388/2019',
   'Deber de protección de derechos fundamentales por prestadores de servicios públicos esenciales.',
   'verificada', 0.95),
  (v_firm_id, v_doc_id, 'SU-449/2020',
   'Deber de diligencia reforzado del transportador frente a circunstancias médicas acreditadas.',
   'verificada', 0.92),
  (v_firm_id, v_doc_id, 'C-200/1995',
   'Alcance del derecho al debido proceso en relaciones de consumo.',
   'verificada', 0.88)
on conflict do nothing;

-- 10. Deadlines (real columns: titulo, fecha, tipo, origen, completado)
insert into matter_deadlines (firm_id, matter_id, titulo, fecha, tipo, origen, completado, metadata)
values
  (v_firm_id, v_matter_id,
   'Audiencia de pruebas · Juzgado 14 Civil del Circuito',
   now() + interval '5 days', 'audiencia', 'manual', false,
   jsonb_build_object('severity','alta')),
  (v_firm_id, v_matter_id,
   'Presentar alegatos finales',
   now() + interval '12 days', 'pliego_alegaciones', 'manual', false,
   jsonb_build_object('severity','media'))
on conflict do nothing;

end $$;

commit;
