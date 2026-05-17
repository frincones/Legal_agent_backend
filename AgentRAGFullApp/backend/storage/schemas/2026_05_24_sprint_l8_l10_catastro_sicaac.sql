-- Sprint L8 + L10 · IGAC catastro + SICAAC centros conciliacion
-- ==========================================================================
-- L8: cedula catastral verification via IGAC gestores dataset (datos.gov.co)
-- L10: directorio centros conciliacion (scrape SICAAC + datos abiertos)
-- ==========================================================================

-- 1) Gestores catastrales por municipio (DIVIPOLA)
-- Fuente: datos.gov.co dataset bhcx-bx97 (~1,103 municipios)
create table if not exists gestores_catastrales (
  divipola      text primary key,            -- '11001', '05001', '76001'
  municipio     text not null,
  departamento  text not null,
  gestor_cat    text,                        -- 'IGAC', 'CATASTRO BOGOTA', 'CATASTRO MEDELLIN', etc.
  gestor_con    text,                        -- 'GESTOR IGAC' / 'GESTOR <ENTIDAD>'
  estado_act    text,                        -- 'EN OPERACION' | 'INACTIVO'
  area_km2      numeric,
  fuente        text default 'datos_gov_bhcx',
  fetched_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

create index if not exists idx_gestores_municipio on gestores_catastrales (municipio);
create index if not exists idx_gestores_depto on gestores_catastrales (departamento);

comment on table gestores_catastrales is 'Sprint L8 · mapeo DIVIPOLA -> gestor catastral (IGAC u local). Fuente: datos.gov.co/bhcx-bx97';

-- 2) Cache de consultas catastrales (cuando se hayan hecho contra IGAC o gestor local)
create table if not exists predios_cache (
  id            uuid primary key default gen_random_uuid(),
  cedula_catastral text not null unique,    -- '110011234567890' (11-30 chars)
  divipola      text,                        -- primeros 5 del cedula
  municipio     text,
  direccion     text,
  area_terreno  numeric,
  area_construccion numeric,
  destino_economico text,                    -- 'HABITACIONAL', 'COMERCIAL', etc.
  matricula_inmobiliaria text,
  estado_valido boolean default true,
  fuente        text,                        -- 'igac' | 'catastro_bogota' | 'estructura_only'
  fuente_url    text,
  texto_preview text,
  raw_json      jsonb,
  verified_at   timestamptz,
  fetched_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

create index if not exists idx_predios_cedula on predios_cache (cedula_catastral);
create index if not exists idx_predios_divipola on predios_cache (divipola);

comment on table predios_cache is 'Sprint L8 · cache de verificacion catastral por cedula. estructura_only = solo se valido el DIVIPOLA, no se accedio al gestor.';

-- 3) Centros de conciliacion autorizados (SICAAC)
-- Fuente: scrape periodica de SICAAC + dataset datos.gov.co si disponible.
create table if not exists centros_conciliacion (
  id            uuid primary key default gen_random_uuid(),
  nombre        text not null,
  numero_resolucion text,                    -- '0123 de 2018'
  fecha_resolucion date,
  entidad_origen text,                       -- 'Camara de Comercio', 'Notaria', 'Universidad', 'ONG'
  ciudad        text,
  departamento  text,
  divipola      text,
  direccion     text,
  telefono      text,
  email         text,
  modalidades   text[],                      -- {extrajudicial,judicial,virtual}
  estado        text default 'autorizado' check (estado in ('autorizado','suspendido','cancelado','vencido')),
  vigencia_hasta date,
  url_oficial   text,
  fuente        text default 'sicaac',
  raw_json      jsonb,
  verified_at   timestamptz,
  fetched_at    timestamptz default now(),
  updated_at    timestamptz default now(),
  unique (nombre, ciudad)
);

create index if not exists idx_centros_ciudad on centros_conciliacion (ciudad);
create index if not exists idx_centros_estado on centros_conciliacion (estado);
create index if not exists idx_centros_entidad on centros_conciliacion (entidad_origen);

comment on table centros_conciliacion is 'Sprint L10 · centros conciliacion autorizados por Minjusticia (SICAAC). Verifica si abogado cita un centro real.';

-- 4) Verificacion
select 'gestores_catastrales' as tabla, count(*) as total from gestores_catastrales
union all
select 'predios_cache', count(*) from predios_cache
union all
select 'centros_conciliacion', count(*) from centros_conciliacion;
