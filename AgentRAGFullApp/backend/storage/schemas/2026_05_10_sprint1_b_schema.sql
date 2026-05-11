-- ============================================================
-- Sprint 1 · B · Schema for identity, teams and practice areas
-- ============================================================
-- Date: 2026-05-09
-- Apply AFTER 2026_05_10_sprint1_a_role_enum.sql.
--
-- Adds:
--   1. users.modo_ejercicio   (M1-M5)
--   2. users.onboarded_at     (idle until wizard completes)
--   3. user_practice_areas    (N:N, leverages enum materia_legal)
--   4. firm_teams             (sub-equipos del socio)
--   5. firm_team_members      (asociados a cargo)
--
-- Idempotent (uses IF NOT EXISTS / DROP POLICY IF EXISTS so it can
-- be re-applied safely).
-- ============================================================

begin;

-- ──────────────────────────────────────────────────────────────
-- 1. users · modo_ejercicio + onboarded_at
-- ──────────────────────────────────────────────────────────────
alter table users
  add column if not exists modo_ejercicio text
  check (modo_ejercicio in (
    'independiente',  -- M1
    'firma',          -- M2
    'in_house',       -- M3
    'sector_publico', -- M4
    'consultoria'     -- M5
  ));

alter table users
  add column if not exists onboarded_at timestamptz;

-- ──────────────────────────────────────────────────────────────
-- 2. user_practice_areas (N:N)
-- ──────────────────────────────────────────────────────────────
create table if not exists user_practice_areas (
  user_id     uuid not null references users(id) on delete cascade,
  area        materia_legal not null,
  is_primary  boolean default false,
  added_at    timestamptz default now(),
  primary key (user_id, area)
);

create index if not exists user_practice_areas_user_idx
  on user_practice_areas (user_id);

alter table user_practice_areas enable row level security;

drop policy if exists upa_owner_select on user_practice_areas;
create policy upa_owner_select on user_practice_areas
  for select to authenticated
  using (user_id = auth.uid());

drop policy if exists upa_owner_modify on user_practice_areas;
create policy upa_owner_modify on user_practice_areas
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- ──────────────────────────────────────────────────────────────
-- 3. firm_teams (sub-equipos del socio)
-- ──────────────────────────────────────────────────────────────
create table if not exists firm_teams (
  id            uuid primary key default gen_random_uuid(),
  firm_id       uuid not null references firms(id) on delete cascade,
  socio_id      uuid not null references users(id) on delete cascade,
  name          text not null,
  area_practica materia_legal,
  description   text,
  metadata      jsonb default '{}'::jsonb,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

create index if not exists firm_teams_firm_idx on firm_teams (firm_id);
create index if not exists firm_teams_socio_idx on firm_teams (socio_id);

alter table firm_teams enable row level security;

drop policy if exists firm_teams_select on firm_teams;
create policy firm_teams_select on firm_teams
  for select to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()));

drop policy if exists firm_teams_modify on firm_teams;
create policy firm_teams_modify on firm_teams
  for all to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()))
  with check (firm_id = (select firm_id from users where id = auth.uid()));

-- ──────────────────────────────────────────────────────────────
-- 4. firm_team_members
-- ──────────────────────────────────────────────────────────────
create table if not exists firm_team_members (
  team_id       uuid not null references firm_teams(id) on delete cascade,
  user_id       uuid not null references users(id) on delete cascade,
  role_in_team  text not null default 'asociado'
    check (role_in_team in ('socio_lider','asociado','asistente','paralegal','externo')),
  added_at      timestamptz default now(),
  primary key (team_id, user_id)
);

create index if not exists firm_team_members_user_idx on firm_team_members (user_id);

alter table firm_team_members enable row level security;

drop policy if exists firm_team_members_select on firm_team_members;
create policy firm_team_members_select on firm_team_members
  for select to authenticated
  using (
    team_id in (
      select id from firm_teams
       where firm_id = (select firm_id from users where id = auth.uid())
    )
  );

drop policy if exists firm_team_members_modify on firm_team_members;
create policy firm_team_members_modify on firm_team_members
  for all to authenticated
  using (
    team_id in (
      select id from firm_teams
       where firm_id = (select firm_id from users where id = auth.uid())
    )
  )
  with check (
    team_id in (
      select id from firm_teams
       where firm_id = (select firm_id from users where id = auth.uid())
    )
  );

-- ──────────────────────────────────────────────────────────────
-- 5. Trigger to keep users.updated_at fresh on row updates
-- (only added if helper exists; skip if you keep updated_at on
-- application side)
-- ──────────────────────────────────────────────────────────────
do $$
begin
  if exists (select 1 from pg_proc where proname = 'tg_set_updated_at') then
    drop trigger if exists firm_teams_set_updated_at on firm_teams;
    create trigger firm_teams_set_updated_at
      before update on firm_teams
      for each row execute function tg_set_updated_at();
  end if;
end $$;

commit;

-- ──────────────────────────────────────────────────────────────
-- Verification queries (manual after applying):
--
--   select column_name from information_schema.columns
--    where table_name='users' and column_name in ('modo_ejercicio','onboarded_at');
--
--   select tablename, rowsecurity from pg_tables
--    where tablename in ('user_practice_areas','firm_teams','firm_team_members');
--
--   select unnest(enum_range(null::user_role));
-- ──────────────────────────────────────────────────────────────
