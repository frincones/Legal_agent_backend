-- Sprint M21 · HOTFIX schema drift detectado en testing manual:
--
-- BUG 1: cold_start_sessions.id vs tool expects session_id
-- BUG 2: cold_start_sessions.area is NOT NULL pero tool no la pasa
-- BUG 3: cold_start_sessions falta started_by_user_id (tool lo pasa)
-- BUG 4: cold_start_sessions tiene started_at, tool consulta created_at
-- BUG 5: matter_history.id vs tool expects event_id
--
-- Idempotente: cada cambio guard-checked. Safe re-run.

-- ============================================================
-- FIX 1-4: cold_start_sessions
-- ============================================================
do $$
begin
    -- 1. Rename id -> session_id si todavia es id
    if exists (select 1 from information_schema.columns
              where table_schema='public' and table_name='cold_start_sessions'
              and column_name='id')
       and not exists (select 1 from information_schema.columns
                       where table_schema='public' and table_name='cold_start_sessions'
                       and column_name='session_id') then
        alter table cold_start_sessions rename column id to session_id;
        raise notice 'cold_start_sessions: id -> session_id renamed';
    end if;

    -- 2. Make area nullable (el tool no la pasa; viene del onboarding wizard, opcional)
    if exists (select 1 from information_schema.columns
              where table_schema='public' and table_name='cold_start_sessions'
              and column_name='area' and is_nullable='NO') then
        alter table cold_start_sessions alter column area drop not null;
        raise notice 'cold_start_sessions: area nullable now';
    end if;

    -- 3. Add started_by_user_id si missing (tool lo pasa)
    if not exists (select 1 from information_schema.columns
                  where table_schema='public' and table_name='cold_start_sessions'
                  and column_name='started_by_user_id') then
        alter table cold_start_sessions add column started_by_user_id uuid references users(id);
        raise notice 'cold_start_sessions: started_by_user_id added';
    end if;

    -- 4. Add created_at si missing (tool consulta order by created_at)
    if not exists (select 1 from information_schema.columns
                  where table_schema='public' and table_name='cold_start_sessions'
                  and column_name='created_at') then
        alter table cold_start_sessions add column created_at timestamptz default now();
        update cold_start_sessions set created_at = coalesce(started_at, now()) where created_at is null;
        raise notice 'cold_start_sessions: created_at added + backfilled';
    end if;
end$$;

-- ============================================================
-- FIX 5: matter_history.id -> event_id
-- ============================================================
do $$
begin
    if exists (select 1 from information_schema.columns
              where table_schema='public' and table_name='matter_history'
              and column_name='id')
       and not exists (select 1 from information_schema.columns
                       where table_schema='public' and table_name='matter_history'
                       and column_name='event_id') then
        alter table matter_history rename column id to event_id;
        raise notice 'matter_history: id -> event_id renamed';
    end if;
end$$;

-- ============================================================
-- VALIDATION
-- ============================================================
do $$
declare
    css_session boolean;
    css_started_user boolean;
    css_created boolean;
    css_area_nullable boolean;
    mh_event boolean;
begin
    select exists(select 1 from information_schema.columns
                  where table_schema='public' and table_name='cold_start_sessions' and column_name='session_id')
        into css_session;
    select exists(select 1 from information_schema.columns
                  where table_schema='public' and table_name='cold_start_sessions' and column_name='started_by_user_id')
        into css_started_user;
    select exists(select 1 from information_schema.columns
                  where table_schema='public' and table_name='cold_start_sessions' and column_name='created_at')
        into css_created;
    select (is_nullable='YES') from information_schema.columns
        where table_schema='public' and table_name='cold_start_sessions' and column_name='area'
        into css_area_nullable;
    select exists(select 1 from information_schema.columns
                  where table_schema='public' and table_name='matter_history' and column_name='event_id')
        into mh_event;

    if not (css_session and css_started_user and css_created and css_area_nullable and mh_event) then
        raise exception 'M21 schema drift fix FAILED: session_id=% started_by=% created_at=% area_nullable=% event_id=%',
            css_session, css_started_user, css_created, css_area_nullable, mh_event;
    end if;
    raise notice 'M21 schema drift HOTFIX OK';
end$$;
