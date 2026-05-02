# LexAI · Migration Guide

## Apply order

Run in your Supabase SQL editor (or via `psql`) **in this exact order**:

```bash
1. init.sql                          # base RAG schema (documents/chunks/conversations)
2. legal_migration.sql               # normas + derogaciones + jurisprudencia (CO)
3. activity_migration.sql            # conversation activity tracking
4. case_state_migration.sql          # multi-turn case state accumulator
5. lexai_multi_tenant_migration.sql  # ← THIS FILE · firms/users/clients/matters/HITL/audit
```

`lexai_multi_tenant_migration.sql` is **additive** — it does not drop or alter
existing tables in destructive ways. It only:
- Creates new tables for multi-tenant data (firms, users, matters, etc.)
- Adds `firm_id` columns to `documents`, `chunks`, `conversations`,
  `conversation_chunks`, `normas`, `jurisprudencia` (nullable on existing
  rows so the corpus público sigue funcionando).
- Adds `vigencia`, `superada_por`, `rubro`, `sha256` to `jurisprudencia`.
- Creates the country-agnostic `match_juris(...)` SQL function.
- Enables RLS policies scoped by `auth_firm_id()`.

## Post-apply checklist

### 1. Custom JWT claim hook (Supabase Auth)

In Supabase dashboard → Authentication → Hooks → Custom Access Token, register
this Postgres function:

```sql
create or replace function public.custom_access_token_hook(event jsonb)
returns jsonb
language plpgsql
as $$
declare
  claims jsonb;
  user_firm_id uuid;
  user_role text;
  user_cedula text;
begin
  claims := event->'claims';

  select u.firm_id, u.role::text, u.cedula_profesional
    into user_firm_id, user_role, user_cedula
  from public.users u
  where u.id = (event->>'user_id')::uuid;

  if user_firm_id is not null then
    claims := jsonb_set(claims, '{firm_id}', to_jsonb(user_firm_id::text));
    claims := jsonb_set(claims, '{role_lexai}', to_jsonb(coalesce(user_role,'lawyer')));
    if user_cedula is not null then
      claims := jsonb_set(claims, '{cedula_profesional}', to_jsonb(user_cedula));
    end if;
  end if;

  event := jsonb_set(event, '{claims}', claims);
  return event;
end;
$$;

grant execute on function public.custom_access_token_hook to supabase_auth_admin;
revoke execute on function public.custom_access_token_hook from authenticated, anon, public;
grant all on public.users to supabase_auth_admin;
```

### 2. Storage buckets (private)

```bash
supabase storage create-bucket matters --public=false
supabase storage create-bucket voice   --public=false
supabase storage create-bucket exports --public=false
```

Add RLS policies on each bucket so users only access objects under
`{firm_id}/...` paths.

### 3. RLS smoke test

```sql
-- 1. Create two firms manually
insert into firms (id, razon_social, country) values
  ('11111111-1111-1111-1111-111111111111','Despacho A','co'),
  ('22222222-2222-2222-2222-222222222222','Despacho B','co');

-- 2. Create test users (in Supabase Auth UI), then:
insert into users (id, firm_id, email, full_name) values
  ('<auth_user_id_a>','11111111-1111-1111-1111-111111111111','a@example.com','User A'),
  ('<auth_user_id_b>','22222222-2222-2222-2222-222222222222','b@example.com','User B');

-- 3. Create a matter as User A
insert into matters (firm_id, client_id, display_id, titulo, materia)
values ('11111111-1111-1111-1111-111111111111','<client_id>','C-2026-0001','Test','laboral');

-- 4. Login as User B and try:
select * from matters;
-- MUST return 0 rows. If it returns the User A row, RLS is broken.
```

### 4. Custom JWT secret in Railway env

In Railway, set:
```
SUPABASE_JWT_SECRET=<from Supabase dashboard → Settings → API → JWT Secret>
SUPABASE_JWT_AUDIENCE=authenticated
VOICE_TICKET_HMAC_SECRET=<random 64 hex bytes>
OPENAI_REALTIME_MODEL=gpt-realtime
OPENAI_REALTIME_VOICE=marin
```

### 5. Initial seed (optional, demo firm)

For Sprint 7 (white-glove onboarding) + onboarding demo (F13):

```sql
insert into firms (id, razon_social, country, plan)
values ('00000000-0000-0000-0000-000000000000','LexAI Demo','co','trial');
```

A Pérez vs ACME demo case will be seeded by the frontend onboarding flow
once a user accepts the demo path.

## Rollback

The migration is fully reversible (all `if not exists` and additive).
To drop just the LexAI MVP tables (keeping the existing CO corpus):

```sql
drop table if exists nsm_daily, billing_subscriptions, audit_log,
  voice_sessions, liquidacion_calculations, document_citations,
  hitl_interrupts, agent_tool_calls, agent_runs, agent_sessions,
  matter_document_versions, matter_documents, matter_notes,
  matter_timeline, matter_deadlines, matter_parties, matters,
  clients, users, firms cascade;

drop function if exists match_juris cascade;
drop function if exists set_firm_id_from_jwt cascade;
drop function if exists tg_set_updated_at cascade;
drop function if exists audit_immutable cascade;
drop function if exists auth_firm_id cascade;

-- The firm_id columns added to existing tables can stay; they're nullable.
```
