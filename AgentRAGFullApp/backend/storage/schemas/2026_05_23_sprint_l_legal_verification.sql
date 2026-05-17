-- Sprint L · verificación legal con fuentes oficiales en vivo
-- ==========================================================================
-- Agrega:
--   1. Columnas a jurisprudencia para tracking de fetches live
--   2. Tabla leyes_normas para cachear verificaciones de leyes/decretos/códigos
--   3. Tabla verification_attempts para audit y debugging
-- ==========================================================================

-- 1) Extender jurisprudencia con tracking live ---------------------------
alter table jurisprudencia
  add column if not exists verified_at timestamptz,
  add column if not exists source text,                  -- 'seed' | 'corte_cc' | 'manual'
  add column if not exists fetched_at timestamptz,
  add column if not exists html_hash text,
  add column if not exists magistrado_ponente text,
  add column if not exists texto_preview text;           -- primeros 500 chars

create index if not exists idx_jurisprudencia_verified
  on jurisprudencia (corte, numero, verified_at desc);

-- 2) Tabla leyes_normas (cache permanente de leyes/decretos verificados)
create table if not exists leyes_normas (
  id            uuid primary key default gen_random_uuid(),
  tipo          text not null check (tipo in (
                  'LEY', 'DECRETO', 'RESOLUCION', 'ACUERDO', 'CIRCULAR',
                  'CODIGO', 'CONSTITUCION', 'OTRO'
                )),
  numero        text not null,                            -- '640', '1755', '1377'
  anio          int,                                      -- 2001, 2015, 2013
  citation_ref  text not null unique,                     -- 'LEY 640/2001', 'DECRETO 1377/2013'
  titulo        text,
  vigencia      text default 'vigente' check (vigencia in (
                  'vigente', 'derogada', 'modulada', 'suspendida',
                  'inexequible', 'desconocida'
                )),
  derogada_por  uuid references leyes_normas(id) on delete set null,
  fuente_url    text,
  fuente        text default 'senado',                    -- 'senado' | 'funcion_publica' | 'manual'
  texto_preview text,                                     -- primeros 500 chars
  html_hash     text,
  verified_at   timestamptz,
  fetched_at    timestamptz default now(),
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

create index if not exists idx_leyes_normas_lookup
  on leyes_normas (tipo, numero, anio);

create index if not exists idx_leyes_normas_citation_ref
  on leyes_normas (citation_ref);

-- 3) Verification attempts · audit log para debugging y SLA
create table if not exists verification_attempts (
  id            bigserial primary key,
  firm_id       uuid references firms(id) on delete set null,
  user_id       uuid,
  citation_ref  text not null,
  ref_type      text not null check (ref_type in ('jurisprudencia', 'ley', 'decreto', 'codigo')),
  result_state  text not null check (result_state in (
                  'verificada', 'no_encontrada', 'sospechosa',
                  'derogada', 'modulada', 'cache_hit', 'error'
                )),
  source        text,                          -- 'cache' | 'bd' | 'live_cc' | 'live_senado' | 'web_search'
  duration_ms   int,
  error_message text,
  metadata      jsonb default '{}',
  created_at    timestamptz default now()
);

create index if not exists idx_verification_attempts_firm
  on verification_attempts (firm_id, created_at desc);

create index if not exists idx_verification_attempts_ref
  on verification_attempts (citation_ref, created_at desc);

-- 4) Seed marca · pone source='seed' a las filas que ya existen
update jurisprudencia
   set source = 'seed', fetched_at = created_at
 where source is null;

-- 5) Comentarios para docs
comment on table leyes_normas is 'Cache permanente de leyes/decretos/códigos verificados contra fuentes oficiales (Sprint L)';
comment on table verification_attempts is 'Audit log de cada intento de verificación · usado para métricas y debugging';
comment on column jurisprudencia.source is 'Origen del registro: seed (sembrado manual), corte_cc (fetch live), manual (curaduría)';
comment on column jurisprudencia.verified_at is 'Última vez que se confirmó contra fuente oficial';

-- 6) Verificación
select 'jurisprudencia' as tabla, count(*) as total, count(verified_at) as live_verified from jurisprudencia
union all
select 'leyes_normas', count(*), count(verified_at) from leyes_normas
union all
select 'verification_attempts', count(*), null from verification_attempts;
