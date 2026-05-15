-- ================================================================
-- Sprint B · Calendarios completos (Google + Outlook)
-- ================================================================
-- ALCANCE: habilitar push de eventos LexAI → Calendar + delta sync
-- bidireccional + Realtime para frontend.
--
-- ADITIVO: solo añade columnas + Realtime publication + pg_cron.
-- NO toca contratos de email_integrations/whatsapp_integrations/
-- firm_integrations existentes.
-- ================================================================

-- ----------------------------------------------------------------
-- 1. calendar_integrations · flag de auto-push y conferencia preferida
-- ----------------------------------------------------------------

alter table calendar_integrations
  add column if not exists auto_push_lexai_events boolean not null default true;

alter table calendar_integrations
  add column if not exists preferred_conference text
    check (preferred_conference is null or
           preferred_conference in ('meet', 'teams', 'none'));

-- backfill: si null, set default según provider
update calendar_integrations
   set preferred_conference = case
     when provider = 'google' then 'meet'
     when provider = 'outlook' then 'teams'
     else null
   end
 where preferred_conference is null;

-- ----------------------------------------------------------------
-- 2. calendar_events · enriquecer con campos para push LexAI
-- ----------------------------------------------------------------
-- Sólo columnas opcionales · no rompe inserts existentes

alter table calendar_events
  add column if not exists pushed_by_lexai boolean default false;

alter table calendar_events
  add column if not exists lexai_event_kind text
    check (lexai_event_kind is null or
           lexai_event_kind in ('audiencia', 'conciliacion', 'reunion',
                                'plazo', 'otro'));

alter table calendar_events
  add column if not exists conference_provider text
    check (conference_provider is null or
           conference_provider in ('meet', 'teams', 'zoom', 'none'));

alter table calendar_events
  add column if not exists last_synced_at timestamptz;

create index if not exists calendar_events_pushed_idx
  on calendar_events (firm_id, pushed_by_lexai) where pushed_by_lexai = true;

create index if not exists calendar_events_matter_start_idx
  on calendar_events (matter_id, start_at) where matter_id is not null;

-- ----------------------------------------------------------------
-- 3. Realtime publication para frontend (live updates en /mi-dia)
-- ----------------------------------------------------------------

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'calendar_events'
  ) then
    alter publication supabase_realtime add table calendar_events;
  end if;
end $$;

-- ----------------------------------------------------------------
-- 4. pg_cron · delta sync cada 15 min via pg_net → Railway
-- ----------------------------------------------------------------
-- Llama al endpoint admin del backend (protegido con X-Cron-Secret).
-- El backend hace el sync pesado (delta de Google syncToken / MS deltaLink).
--
-- Idempotente: si el job ya existe, lo unschedule y reschedule.

do $$
declare
  v_jobid bigint;
  v_railway_url text := 'https://legal-agent-backend-production-fcfa.up.railway.app';
  v_cron_secret text;
begin
  -- Buscar secret en env via custom GUC; si no existe usar placeholder.
  begin
    v_cron_secret := current_setting('app.cron_secret', true);
  exception when others then
    v_cron_secret := null;
  end;
  if v_cron_secret is null or v_cron_secret = '' then
    v_cron_secret := 'NOT_SET';
  end if;

  select jobid into v_jobid from cron.job where jobname = 'calendar_delta_sync';
  if v_jobid is not null then
    perform cron.unschedule(v_jobid);
  end if;

  perform cron.schedule(
    'calendar_delta_sync',
    '*/15 * * * *',
    format(
      $cron$
      select net.http_post(
        url := %L,
        headers := jsonb_build_object(
          'Content-Type', 'application/json',
          'X-Cron-Secret', %L
        ),
        body := '{"type":"calendar"}'::jsonb
      )
      $cron$,
      v_railway_url || '/v1/admin/sync-tick?type=calendar',
      v_cron_secret
    )
  );
end $$;

-- ----------------------------------------------------------------
-- 5. Función helper · próximos eventos del usuario (para /mi-dia)
-- ----------------------------------------------------------------

create or replace function lexai_upcoming_events(
  p_firm_id uuid,
  p_user_id uuid,
  p_window_hours int default 24
)
returns table (
  id uuid,
  matter_id uuid,
  matter_titulo text,
  title text,
  description text,
  location text,
  start_at timestamptz,
  end_at timestamptz,
  meeting_url text,
  conference_provider text,
  lexai_event_kind text,
  attendees jsonb,
  pushed_by_lexai boolean,
  provider text
)
language sql stable security definer as $$
  select e.id, e.matter_id, m.titulo as matter_titulo,
         e.title, e.description, e.location,
         e.start_at, e.end_at, e.meeting_url, e.conference_provider,
         e.lexai_event_kind, e.attendees, e.pushed_by_lexai,
         ci.provider
    from calendar_events e
    left join matters m on m.id = e.matter_id
    left join calendar_integrations ci on ci.id = e.integration_id
   where e.firm_id = p_firm_id
     and e.start_at between now() and now() + (p_window_hours || ' hours')::interval
   order by e.start_at asc
   limit 50;
$$;

grant execute on function lexai_upcoming_events(uuid, uuid, int) to authenticated, service_role;

-- ----------------------------------------------------------------
-- 6. Verificación
-- ----------------------------------------------------------------

select
  'calendar_integrations new cols' as check,
  count(*) filter (where column_name = 'auto_push_lexai_events') as auto_push,
  count(*) filter (where column_name = 'preferred_conference') as pref_conf
  from information_schema.columns
 where table_schema = 'public' and table_name = 'calendar_integrations'
union all
select 'calendar_events new cols',
  count(*) filter (where column_name in ('pushed_by_lexai','lexai_event_kind',
                                          'conference_provider','last_synced_at')),
  null
  from information_schema.columns
 where table_schema = 'public' and table_name = 'calendar_events'
union all
select 'realtime cal events publication',
  (select count(*) from pg_publication_tables
    where pubname='supabase_realtime' and tablename='calendar_events'),
  null
union all
select 'pg_cron calendar_delta_sync',
  (select count(*) from cron.job where jobname='calendar_delta_sync'),
  null
union all
select 'lexai_upcoming_events fn',
  (select count(*) from pg_proc where proname='lexai_upcoming_events'),
  null;
