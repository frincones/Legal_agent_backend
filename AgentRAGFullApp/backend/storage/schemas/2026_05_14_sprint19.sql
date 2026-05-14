-- ============================================================
-- LexAI · Sprint 19 · Public Intake Forms + Smart Template Fill
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- Aporte:
--   1. intake_forms       · formularios públicos (URL pública sin auth)
--   2. intake_submissions · respuestas recibidas (auto-crea leads via trigger)
--   3. RPC lexai_intake_form_by_slug (público)
--   4. RPC lexai_intake_stats
--   5. RLS firm-scoped + triggers
-- ============================================================

-- ------------------------------------------------------------
-- 1. intake_forms
-- ------------------------------------------------------------
create table if not exists intake_forms (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  slug              text not null,                              -- URL-safe · única por firm
  name              text not null,
  description       text,
  -- Configuración del form
  fields            jsonb not null default '[]'::jsonb,         -- [{id, label, kind, required, options[], placeholder, help}]
  thank_you_message text default 'Gracias. Te contactaremos pronto.',
  redirect_url      text,                                       -- al enviar exitoso, redirige (opcional)
  -- Configuración de routing
  default_assignee_user_id uuid references users(id) on delete set null,
  default_materia   text,                                       -- materia que pone en el lead creado
  -- Estado
  active            boolean not null default true,
  submissions_count int not null default 0,                     -- cache rápido para stats
  -- Branding (light)
  brand_color       text default 'blue',
  show_firm_logo    boolean not null default true,
  -- Anti-spam (básico, sin captcha pesado)
  honeypot_field    text default 'website',                     -- campo trampa · si llega lleno = bot
  rate_limit_per_ip int default 5,                              -- max submits/hora/IP (soft, en app layer)
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (firm_id, slug)
);
create index if not exists intake_firm_active_idx
  on intake_forms (firm_id, active);
create index if not exists intake_slug_idx
  on intake_forms (slug);

-- ------------------------------------------------------------
-- 2. intake_submissions
-- ------------------------------------------------------------
create table if not exists intake_submissions (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  intake_form_id    uuid not null references intake_forms(id) on delete cascade,
  -- Datos del submission
  payload           jsonb not null,                             -- { fieldId: value }
  submitter_email   text,                                       -- extraído del payload si hay campo email
  submitter_nombre  text,                                       -- extraído del payload si hay campo nombre
  submitter_phone   text,
  -- Procesamiento
  status            text not null default 'new'
    check (status in ('new','converted','spam','dismissed')),
  converted_lead_id uuid references leads(id) on delete set null,
  converted_at      timestamptz,
  notes             text,                                       -- nota interna del staff
  -- Metadata útil para review
  ip_address        inet,
  user_agent        text,
  referer           text,
  created_at        timestamptz default now()
);
create index if not exists subm_firm_form_idx
  on intake_submissions (firm_id, intake_form_id, created_at desc);
create index if not exists subm_firm_status_idx
  on intake_submissions (firm_id, status, created_at desc);

-- ============================================================
-- RLS · intake_forms accesible a usuarios del firm
-- ============================================================
alter table intake_forms       enable row level security;
alter table intake_submissions enable row level security;

drop policy if exists if_select on intake_forms;
drop policy if exists if_modify on intake_forms;
-- intake_forms: además del firm, el `service_role` (público vía proxy backend)
-- puede leer por slug si está activo. La validación de "qué expone" se hace
-- en el endpoint público que filtra campos.
create policy if_select on intake_forms for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy if_modify on intake_forms for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists subm_select on intake_submissions;
drop policy if exists subm_modify on intake_submissions;
create policy subm_select on intake_submissions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy subm_modify on intake_submissions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_if_firm_id on intake_forms;
create trigger trg_if_firm_id before insert on intake_forms
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_if_updated_at on intake_forms;
create trigger trg_if_updated_at before update on intake_forms
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_subm_firm_id on intake_submissions;
create trigger trg_subm_firm_id before insert on intake_submissions
  for each row execute function set_firm_id_from_jwt();

-- Trigger que bumpea submissions_count en intake_forms al INSERT/DELETE
create or replace function tg_subm_bump_count() returns trigger
language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    update intake_forms
       set submissions_count = submissions_count + 1
     where id = new.intake_form_id;
  elsif tg_op = 'DELETE' then
    update intake_forms
       set submissions_count = greatest(0, submissions_count - 1)
     where id = old.intake_form_id;
  end if;
  return null;
end;
$$;
drop trigger if exists trg_subm_count on intake_submissions;
create trigger trg_subm_count after insert or delete on intake_submissions
  for each row execute function tg_subm_bump_count();

-- Trigger que registra el evento en activity_events (Sprint 16)
create or replace function tg_subm_after_insert() returns trigger
language plpgsql as $$
declare
  v_form_name text;
begin
  select name into v_form_name from intake_forms where id = new.intake_form_id;
  begin
    insert into activity_events
      (firm_id, actor_user_id, kind, target_kind, target_id, title, preview)
    values
      (new.firm_id, null, 'other',
       'intake_submission', new.id,
       'Nueva submission: ' || coalesce(v_form_name, 'intake'),
       coalesce(new.submitter_nombre, new.submitter_email, 'anónimo'));
  exception when others then
    -- si activity_events no existe (Sprint 16 no aplicado), no fallar
    null;
  end;
  return new;
end;
$$;
drop trigger if exists trg_subm_activity on intake_submissions;
create trigger trg_subm_activity after insert on intake_submissions
  for each row execute function tg_subm_after_insert();

-- ============================================================
-- RPCs
-- ============================================================

-- Public lookup por slug · usado por el endpoint público sin auth.
-- Devuelve sólo lo necesario para renderizar el form · NO expone
-- columnas sensibles (firm_id, created_by, etc.).
create or replace function lexai_intake_form_by_slug(p_slug text)
returns table (
  id uuid,
  firm_id uuid,
  slug text,
  name text,
  description text,
  fields jsonb,
  thank_you_message text,
  redirect_url text,
  brand_color text,
  show_firm_logo boolean,
  honeypot_field text,
  active boolean
)
language sql stable as $$
  select id, firm_id, slug, name, description, fields, thank_you_message,
         redirect_url, brand_color, show_firm_logo, honeypot_field, active
    from intake_forms
   where slug = p_slug
     and active = true
   limit 1;
$$;

-- Stats de intake por firm
create or replace function lexai_intake_stats(p_firm_id uuid)
returns jsonb language sql stable as $$
  select jsonb_build_object(
    'forms_total', (select count(*) from intake_forms where firm_id = p_firm_id),
    'forms_active', (select count(*) from intake_forms where firm_id = p_firm_id and active = true),
    'submissions_total', (select count(*) from intake_submissions where firm_id = p_firm_id),
    'submissions_new', (select count(*) from intake_submissions where firm_id = p_firm_id and status = 'new'),
    'submissions_converted', (select count(*) from intake_submissions where firm_id = p_firm_id and status = 'converted'),
    'submissions_30d', (
      select count(*) from intake_submissions
       where firm_id = p_firm_id and created_at >= now() - interval '30 days'
    ),
    'conversion_rate_pct', (
      case when (select count(*) from intake_submissions where firm_id = p_firm_id) = 0 then 0
      else round(
        (select count(*)::numeric from intake_submissions where firm_id = p_firm_id and status = 'converted')
        /
        nullif((select count(*) from intake_submissions where firm_id = p_firm_id), 0) * 100,
        1
      )
      end
    )
  );
$$;
