-- ============================================================
-- LexAI · Sprint 29 · Google OAuth SSO + Onboarding flexible (firm creation OR invite code)
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- Depends on: lexai_multi_tenant (firms, users), Sprint 23 (firm_subscriptions trigger),
--             Sprint 26 (onboarding trigger)
-- ============================================================

-- ------------------------------------------------------------
-- 0. firm_id nullable · soporta OAuth signup pre-firm
--    (el user existe en public.users antes de elegir/crear firm)
-- ------------------------------------------------------------
alter table public.users alter column firm_id drop not null;

-- ------------------------------------------------------------
-- 1. Auto-create public.users row when new auth.users is inserted
-- ------------------------------------------------------------
-- CRITICAL: sin este trigger, los usuarios que llegan vía Google OAuth
-- quedan en auth.users pero no en public.users, y el custom_access_token_hook
-- no encuentra firm_id/role_lexai → backend rechaza el login.

create or replace function lexai_on_auth_user_created()
returns trigger
language plpgsql
security definer
set search_path = public, auth
as $$
declare
  v_full_name text;
  v_email text;
begin
  v_email := new.email;
  -- Best-effort extracción de full_name de los metadatos
  v_full_name := coalesce(
    new.raw_user_meta_data->>'full_name',
    new.raw_user_meta_data->>'name',
    split_part(v_email, '@', 1)
  );

  -- Skip si ya existe (defensive)
  if exists (select 1 from public.users where id = new.id) then
    return new;
  end if;

  -- Crear fila en public.users con firm_id NULL (wizard lo completa después)
  insert into public.users (id, firm_id, email, full_name, role)
  values (
    new.id,
    null,
    v_email,
    v_full_name,
    'lawyer'
  )
  on conflict (id) do nothing;

  return new;
end;
$$;

drop trigger if exists trg_auth_user_created on auth.users;
create trigger trg_auth_user_created
  after insert on auth.users
  for each row execute function lexai_on_auth_user_created();

-- ------------------------------------------------------------
-- 2. firm_invite_codes · códigos para que un nuevo user se una a firma existente
-- ------------------------------------------------------------
create table if not exists firm_invite_codes (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  code            text unique not null,
  role_to_assign  text not null default 'lawyer',
  created_by      uuid references users(id) on delete set null,
  max_uses        int default 10 check (max_uses > 0),
  used_count      int default 0,
  expires_at      timestamptz default (now() + interval '30 days'),
  metadata        jsonb default '{}'::jsonb,
  created_at      timestamptz default now()
);
create index if not exists firm_invite_codes_firm_idx on firm_invite_codes (firm_id, created_at desc);
create index if not exists firm_invite_codes_code_idx
  on firm_invite_codes (code);
create index if not exists firm_invite_codes_active_idx
  on firm_invite_codes (firm_id, expires_at);

alter table firm_invite_codes enable row level security;
drop policy if exists firm_invite_codes_select on firm_invite_codes;
drop policy if exists firm_invite_codes_modify on firm_invite_codes;
create policy firm_invite_codes_select on firm_invite_codes for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy firm_invite_codes_modify on firm_invite_codes for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 3. RPC · redeem_invite_code
--    Llamado desde el wizard cuando el user elige "unirme con código".
--    Valida + asigna firm_id al usuario.
-- ------------------------------------------------------------
create or replace function lexai_redeem_invite_code(
  p_code      text,
  p_user_id   uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_user_id uuid;
  v_invite firm_invite_codes%rowtype;
  v_user_firm uuid;
begin
  v_user_id := coalesce(p_user_id, auth.uid());
  if v_user_id is null then
    return jsonb_build_object('ok', false, 'error', 'no_user');
  end if;

  -- Verificar que el user no tiene ya firm
  select firm_id into v_user_firm from public.users where id = v_user_id;
  if v_user_firm is not null then
    return jsonb_build_object('ok', false, 'error', 'user_already_in_firm', 'firm_id', v_user_firm);
  end if;

  -- Buscar invite válido
  select * into v_invite from firm_invite_codes
   where code = upper(trim(p_code))
     and used_count < max_uses
     and (expires_at is null or expires_at > now())
   limit 1;

  if v_invite.id is null then
    return jsonb_build_object('ok', false, 'error', 'invalid_or_expired_code');
  end if;

  -- Asignar firm_id + role al user
  update public.users
     set firm_id = v_invite.firm_id,
         role = v_invite.role_to_assign::user_role,
         updated_at = now()
   where id = v_user_id;

  -- Incrementar uso
  update firm_invite_codes set used_count = used_count + 1 where id = v_invite.id;

  return jsonb_build_object(
    'ok', true,
    'firm_id', v_invite.firm_id,
    'role', v_invite.role_to_assign
  );
end;
$$;

grant execute on function lexai_redeem_invite_code(text, uuid) to authenticated, service_role;

-- ------------------------------------------------------------
-- 4. RPC · create_firm_for_user
--    Llamado desde el wizard cuando el user elige "crear nueva firma".
--    Atomicamente crea firms + actualiza users.firm_id.
-- ------------------------------------------------------------
create or replace function lexai_create_firm_for_user(
  p_user_id        uuid,
  p_razon_social   text,
  p_country        text default 'co',
  p_tax_id         text default null,
  p_modo_ejercicio text default null,
  p_role           text default 'independiente'
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_firm_id uuid;
  v_user_firm uuid;
begin
  if p_user_id is null then
    return jsonb_build_object('ok', false, 'error', 'no_user');
  end if;

  -- Si ya tiene firm, no hacer nada (idempotente)
  select firm_id into v_user_firm from public.users where id = p_user_id;
  if v_user_firm is not null then
    return jsonb_build_object('ok', true, 'firm_id', v_user_firm, 'already_had_firm', true);
  end if;

  -- Crear firm (los triggers Sprint 23 + 26 se disparan: plan free + demo data)
  insert into firms (razon_social, country, tax_id, metadata)
  values (
    p_razon_social,
    coalesce(p_country, 'co'),
    p_tax_id,
    jsonb_build_object('created_via', 'oauth_signup', 'created_by_user', p_user_id)
  )
  returning id into v_firm_id;

  -- Asignar al user
  update public.users
     set firm_id = v_firm_id,
         role = p_role::user_role,
         modo_ejercicio = p_modo_ejercicio,
         updated_at = now()
   where id = p_user_id;

  return jsonb_build_object('ok', true, 'firm_id', v_firm_id);
end;
$$;

grant execute on function lexai_create_firm_for_user(uuid, text, text, text, text, text) to authenticated, service_role;

-- ============================================================
-- Done · Sprint 29 OAuth bridge
-- ============================================================
