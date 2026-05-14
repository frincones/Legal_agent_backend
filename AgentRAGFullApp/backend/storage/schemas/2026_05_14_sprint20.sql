-- ============================================================
-- LexAI · Sprint 20 · M25 Judge Perspective Simulator
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- Aporte:
--   1. judges                · perfiles normalizados de jueces/magistrados
--   2. judge_predictions     · simulaciones IA por matter
--   3. RPCs (search, stats, profile, decisions)
--   4. RLS · judges es shared cross-firm (data legal pública)
--             judge_predictions es firm-scoped
--   5. Seed de demo (15 jueces representativos CO) sin sobre-escribir
-- ============================================================

-- ------------------------------------------------------------
-- 1. judges
-- Perfiles ya normalizados · linked a `jurisprudencia.magistrado` por nombre.
-- ------------------------------------------------------------
create table if not exists judges (
  id                uuid primary key default gen_random_uuid(),
  -- Identificación
  full_name         text not null,
  -- variantes ortográficas o alias del nombre · usado para matching contra `jurisprudencia.magistrado`
  name_variants     text[] default array[]::text[],
  corte             text not null
    check (corte in ('CORTE_CONSTITUCIONAL','CORTE_SUPREMA','CONSEJO_ESTADO',
                     'TRIBUNAL_SUPERIOR','JUZGADO_CIRCUITO','JUZGADO_MUNICIPAL','OTRO')),
  sala              text,                                       -- 'Plena', 'Casación Laboral', 'Sección Cuarta', etc.
  cargo             text,                                       -- 'Magistrado', 'Magistrada Auxiliar', 'Juez', etc.
  ciudad            text default 'Bogotá D.C.',
  -- Especialidades / áreas de práctica
  especialidades    text[] default array[]::text[],
  -- Perfil del juez · texto narrativo sobre su línea jurisprudencial, postura, etc.
  perfil            text,
  -- Embedding del perfil + decisiones recientes (text-embedding-3-small)
  embedding         vector(1536),
  embedding_at      timestamptz,
  -- Stats cacheadas (computed por worker · re-cómputo periódico)
  decisions_total   int not null default 0,
  decisions_won_pct real,                                       -- % de decisiones favorables al accionante (estimado)
  decisions_lost_pct real,
  decisions_settled_pct real,
  -- Meta
  source_url        text,                                       -- página oficial del juez
  active            boolean not null default true,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (corte, full_name)
);
create index if not exists judges_corte_idx on judges (corte, sala);
create index if not exists judges_name_idx on judges (full_name);
create index if not exists judges_variants_gin on judges using gin (name_variants);
create index if not exists judges_especialidades_gin on judges using gin (especialidades);
-- Vector index (HNSW si disponible, sino IVFFlat fallback)
do $$ begin
  if not exists (select 1 from pg_class where relname = 'judges_embedding_idx') then
    begin
      execute 'create index judges_embedding_idx on judges '
              'using hnsw (embedding vector_cosine_ops)';
    exception when others then
      execute 'create index judges_embedding_idx on judges '
              'using ivfflat (embedding vector_cosine_ops) with (lists = 50)';
    end;
  end if;
end $$;

-- ------------------------------------------------------------
-- 2. judge_predictions · simulación IA por matter
-- ------------------------------------------------------------
create table if not exists judge_predictions (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid not null references matters(id) on delete cascade,
  judge_id          uuid not null references judges(id) on delete cascade,
  -- Input
  document_excerpt  text,                                        -- snippet del escrito analizado (auditable)
  document_hash     text,                                        -- hash del texto completo · evita re-correr lo mismo
  -- Output del LLM (estructurado)
  alignment_score   real,                                        -- 0..1 · qué tan alineado está el escrito con la línea del juez
  reception         text                                         -- 'favorable' | 'mixto' | 'desfavorable' | 'incierto'
    check (reception in ('favorable','mixto','desfavorable','incierto')),
  summary           text,                                        -- 3-5 líneas
  strengths         jsonb default '[]'::jsonb,                   -- ["argumento 1...", ...]
  risk_factors      jsonb default '[]'::jsonb,                   -- factores que el juez probablemente cuestionará
  suggested_revisions jsonb default '[]'::jsonb,                 -- cómo mejorar el escrito
  similar_decisions jsonb default '[]'::jsonb,                   -- [{numero, fecha, outcome, relevance}]
  -- Meta
  generated_by      text not null default 'llm'
    check (generated_by in ('llm','manual')),
  model_used        text,
  generated_at      timestamptz default now(),
  reviewed_by       uuid references users(id) on delete set null,
  reviewed_at       timestamptz,
  created_at        timestamptz default now()
);
create index if not exists jp_firm_matter_idx
  on judge_predictions (firm_id, matter_id, generated_at desc);
create index if not exists jp_matter_judge_idx
  on judge_predictions (matter_id, judge_id, generated_at desc);

-- ============================================================
-- RLS
-- ============================================================
-- judges: data pública (no PII de clientes) · todos los usuarios autenticados pueden leer
-- judge_predictions: firm-scoped
alter table judges enable row level security;
alter table judge_predictions enable row level security;

drop policy if exists j_select on judges;
drop policy if exists j_modify on judges;
-- Lectura: cualquier usuario autenticado puede leer
create policy j_select on judges for select
  using (auth.role() = 'authenticated' or auth.role() = 'service_role');
-- Escritura: solo service_role (admin / worker) puede modificar
create policy j_modify on judges for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

drop policy if exists jp_select on judge_predictions;
drop policy if exists jp_modify on judge_predictions;
create policy jp_select on judge_predictions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy jp_modify on judge_predictions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_jp_firm_id on judge_predictions;
create trigger trg_jp_firm_id before insert on judge_predictions
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_j_updated_at on judges;
create trigger trg_j_updated_at before update on judges
  for each row execute function tg_set_updated_at();

-- ============================================================
-- RPCs
-- ============================================================

-- Búsqueda de jueces (texto + filtros)
create or replace function lexai_judge_search(
  p_q          text default null,
  p_corte      text default null,
  p_especialidad text default null,
  p_limit      int default 20
) returns table (
  id uuid,
  full_name text,
  corte text,
  sala text,
  cargo text,
  ciudad text,
  especialidades text[],
  decisions_total int,
  decisions_won_pct real,
  rank real
)
language sql stable as $$
  select j.id, j.full_name, j.corte, j.sala, j.cargo, j.ciudad,
         j.especialidades, j.decisions_total, j.decisions_won_pct,
         case
           when p_q is null or trim(p_q) = '' then 1.0
           else greatest(
             similarity(j.full_name, p_q),
             0.5 * similarity(coalesce(j.perfil, ''), p_q)
           )
         end::real as rank
    from judges j
   where j.active = true
     and (p_corte is null or j.corte = p_corte)
     and (p_especialidad is null or p_especialidad = any(j.especialidades))
     and (
       p_q is null
       or trim(p_q) = ''
       or j.full_name ilike '%' || p_q || '%'
       or exists (select 1 from unnest(j.name_variants) v where v ilike '%' || p_q || '%')
       or coalesce(j.perfil, '') ilike '%' || p_q || '%'
     )
   order by rank desc, decisions_total desc nulls last
   limit p_limit;
$$;

-- Stats de un juez (computed sobre jurisprudencia + judge_predictions)
create or replace function lexai_judge_stats(p_judge_id uuid)
returns jsonb language plpgsql stable as $$
declare
  v_judge judges%rowtype;
  v_juris_count int := 0;
begin
  select * into v_judge from judges where id = p_judge_id;
  if not found then
    return null;
  end if;

  -- Contar decisiones en jurisprudencia que matcheen el nombre
  begin
    select count(*) into v_juris_count
      from jurisprudencia
     where magistrado ilike '%' || v_judge.full_name || '%'
        or magistrado = any(v_judge.name_variants);
  exception when others then
    v_juris_count := 0;
  end;

  return jsonb_build_object(
    'judge_id', v_judge.id,
    'full_name', v_judge.full_name,
    'corte', v_judge.corte,
    'sala', v_judge.sala,
    'decisions_in_db', v_juris_count,
    'decisions_total', coalesce(v_judge.decisions_total, 0),
    'decisions_won_pct', coalesce(v_judge.decisions_won_pct, 0),
    'decisions_lost_pct', coalesce(v_judge.decisions_lost_pct, 0),
    'decisions_settled_pct', coalesce(v_judge.decisions_settled_pct, 0),
    'predictions_run', (select count(*) from judge_predictions where judge_id = v_judge.id)
  );
end;
$$;

-- Decisiones recientes del juez (desde jurisprudencia)
create or replace function lexai_judge_decisions(
  p_judge_id uuid,
  p_limit int default 10
) returns table (
  id uuid,
  numero text,
  corte text,
  sala text,
  tipo_sentencia text,
  fecha date,
  temas text[],
  ratio_decidendi text,
  fuente_url text
)
language plpgsql stable as $$
declare
  v_judge judges%rowtype;
begin
  select * into v_judge from judges where id = p_judge_id;
  if not found then
    return;
  end if;
  begin
    return query
    select j.id, j.numero, j.corte, j.sala, j.tipo_sentencia,
           j.fecha, j.temas, j.ratio_decidendi, j.fuente_url
      from jurisprudencia j
     where j.magistrado ilike '%' || v_judge.full_name || '%'
        or j.magistrado = any(v_judge.name_variants)
     order by j.fecha desc nulls last
     limit p_limit;
  exception when others then
    return;
  end;
end;
$$;

-- Predicción más reciente por matter+judge
create or replace function lexai_latest_judge_prediction(
  p_firm_id   uuid,
  p_matter_id uuid,
  p_judge_id  uuid
) returns table (
  id uuid,
  alignment_score real,
  reception text,
  summary text,
  strengths jsonb,
  risk_factors jsonb,
  suggested_revisions jsonb,
  similar_decisions jsonb,
  generated_at timestamptz,
  reviewed_at timestamptz
)
language sql stable as $$
  select id, alignment_score, reception, summary, strengths, risk_factors,
         suggested_revisions, similar_decisions, generated_at, reviewed_at
    from judge_predictions
   where firm_id = p_firm_id
     and matter_id = p_matter_id
     and judge_id = p_judge_id
   order by generated_at desc
   limit 1;
$$;

-- ============================================================
-- Seed · 15 jueces representativos de Colombia (idempotente)
-- ============================================================
-- Datos de carácter público · referencia rápida para demo.
-- Los stats se recomputan en background.
insert into judges (full_name, name_variants, corte, sala, cargo, ciudad,
                    especialidades, perfil)
values
  ('Cristina Pardo Schlesinger',
   array['Cristina Pardo','C. Pardo'],
   'CORTE_CONSTITUCIONAL', 'Sala Plena', 'Magistrada', 'Bogotá D.C.',
   array['derechos_fundamentales','tutela','salud'],
   'Magistrada de la Corte Constitucional · línea protectora en derechos fundamentales · constante en tutelas de salud y mínimo vital.'),

  ('Diana Fajardo Rivera',
   array['Diana Fajardo','D. Fajardo'],
   'CORTE_CONSTITUCIONAL', 'Sala Plena', 'Magistrada', 'Bogotá D.C.',
   array['derechos_fundamentales','género','medio_ambiente'],
   'Magistrada de la Corte Constitucional · línea fuerte en perspectiva de género, derechos colectivos y medio ambiente.'),

  ('Jorge Enrique Ibáñez Najar',
   array['Jorge Ibáñez','J. Ibáñez Najar'],
   'CORTE_CONSTITUCIONAL', 'Sala Plena', 'Magistrado', 'Bogotá D.C.',
   array['administrativo','constitucionalidad','tributario'],
   'Magistrado de la Corte Constitucional · enfoque técnico-jurídico, riguroso en control de constitucionalidad y administrativo.'),

  ('Antonio José Lizarazo Ocampo',
   array['Antonio Lizarazo','A. Lizarazo'],
   'CORTE_CONSTITUCIONAL', 'Sala Plena', 'Magistrado', 'Bogotá D.C.',
   array['derechos_sociales','laboral','pensiones'],
   'Magistrado de la Corte Constitucional · firme en derechos sociales y prestacionales. Línea progresista en pensiones y mínimo vital.'),

  ('Carlos Bernal Pulido',
   array['Carlos Bernal','C. Bernal'],
   'CORTE_CONSTITUCIONAL', 'Sala Plena', 'Magistrado (ex)', 'Bogotá D.C.',
   array['constitucionalidad','derechos_fundamentales','filosofía_jurídica'],
   'Ex-magistrado · academicista · línea ponderada con énfasis en proporcionalidad y test estricto en derechos fundamentales.'),

  ('Jorge Mauricio Burgos Ruiz',
   array['Mauricio Burgos','M. Burgos'],
   'CORTE_SUPREMA', 'Sala de Casación Laboral', 'Magistrado', 'Bogotá D.C.',
   array['laboral','seguridad_social','pensiones'],
   'Magistrado · referencia en casación laboral · postura técnica en interpretación de CST y pensiones del régimen de prima media.'),

  ('Iván Mauricio Lenis Gómez',
   array['Iván Lenis','M. Lenis Gómez'],
   'CORTE_SUPREMA', 'Sala de Casación Laboral', 'Magistrado', 'Bogotá D.C.',
   array['laboral','solidaridad_pensional','ius_variandi'],
   'Magistrado laboral · sensibilidad a la dignidad del trabajador y solidaridad en la responsabilidad solidaria.'),

  ('Aroldo Wilson Quiroz Monsalvo',
   array['Aroldo Quiroz','A. Quiroz'],
   'CORTE_SUPREMA', 'Sala de Casación Civil', 'Magistrado', 'Bogotá D.C.',
   array['civil','responsabilidad_civil','familia'],
   'Magistrado civil · técnico-doctrinario · línea ordenada en responsabilidad civil y derecho de familia.'),

  ('Octavio Augusto Tejeiro Duque',
   array['Octavio Tejeiro','O. Tejeiro'],
   'CORTE_SUPREMA', 'Sala de Casación Civil', 'Magistrado', 'Bogotá D.C.',
   array['civil','comercial','contractual'],
   'Magistrado civil · línea constructiva en responsabilidad contractual y derecho de los contratos comerciales.'),

  ('Hernando Sánchez Sánchez',
   array['Hernando Sánchez','H. Sánchez'],
   'CONSEJO_ESTADO', 'Sección Tercera', 'Consejero', 'Bogotá D.C.',
   array['responsabilidad_estatal','contractual','reparación_directa'],
   'Consejero de Estado · referencia en responsabilidad estatal extracontractual y daño antijurídico.'),

  ('Marta Nubia Velásquez Rico',
   array['Marta Velásquez','M. Velásquez Rico'],
   'CONSEJO_ESTADO', 'Sección Tercera', 'Consejera', 'Bogotá D.C.',
   array['contractual','responsabilidad_estatal','contratación'],
   'Consejera · enfoque en contratación pública y litigio contractual del Estado.'),

  ('Carmelo Perdomo Cuéter',
   array['Carmelo Perdomo','C. Perdomo'],
   'CONSEJO_ESTADO', 'Sección Cuarta', 'Consejero', 'Bogotá D.C.',
   array['tributario','impuestos','administrativo'],
   'Consejero tributarista · línea técnica en obligaciones tributarias y procedimiento administrativo tributario.'),

  ('Stella Jeannette Carvajal Basto',
   array['Stella Carvajal','S. Carvajal'],
   'CONSEJO_ESTADO', 'Sección Cuarta', 'Consejera', 'Bogotá D.C.',
   array['tributario','aduanero','administrativo'],
   'Consejera · referencia en derecho tributario y aduanero · acuciosa en cuestiones probatorias.'),

  ('Camilo Montoya Reyes',
   array['Camilo Montoya','C. Montoya'],
   'TRIBUNAL_SUPERIOR', 'Sala Civil', 'Magistrado', 'Bogotá D.C.',
   array['civil','familia','sucesiones'],
   'Magistrado del Tribunal Superior de Bogotá · sala civil · primera instancia de apelación en familia y sucesiones.'),

  ('Patricia Salazar Cuéllar',
   array['Patricia Salazar','P. Salazar'],
   'TRIBUNAL_SUPERIOR', 'Sala Laboral', 'Magistrada', 'Bogotá D.C.',
   array['laboral','seguridad_social','ius_variandi'],
   'Magistrada del Tribunal Superior de Bogotá · sala laboral · línea protectora del trabajador.')
on conflict (corte, full_name) do nothing;
