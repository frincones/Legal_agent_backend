-- ============================================================
-- LexAI · Sprint 13 · Firma digital + Importadores CSV
-- Migration date: 2026-05-13
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. signature_envelopes · sobre de firma (1 o varios docs + 1+ firmantes)
-- ------------------------------------------------------------
create table if not exists signature_envelopes (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid references matters(id) on delete set null,
  title             text not null,
  message           text,
  provider          text not null default 'demo'
    check (provider in ('demo','certicamara','docusign')),
  external_id       text,                                   -- envelope_id de Certicamara/DocuSign
  status            text not null default 'draft'
    check (status in ('draft','sent','viewed','signed','partially_signed','declined','expired','canceled')),
  signer_order      text not null default 'parallel'
    check (signer_order in ('parallel','sequential')),
  expires_at        timestamptz,
  sent_at           timestamptz,
  completed_at      timestamptz,
  canceled_at       timestamptz,
  signer_count      int not null default 0,
  signed_count      int not null default 0,
  signed_pdf_storage_path text,                             -- final con sello digital
  webhook_secret    text,                                    -- shared secret para verify
  metadata          jsonb default '{}'::jsonb,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists sig_env_firm_idx
  on signature_envelopes (firm_id, status, created_at desc);
create index if not exists sig_env_matter_idx
  on signature_envelopes (matter_id) where matter_id is not null;
create index if not exists sig_env_external_idx
  on signature_envelopes (external_id) where external_id is not null;

-- ------------------------------------------------------------
-- 2. signature_signers · firmantes del sobre
-- ------------------------------------------------------------
create table if not exists signature_signers (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  envelope_id       uuid not null references signature_envelopes(id) on delete cascade,
  role              text not null,                          -- 'firmante','testigo','revisor'
  name              text not null,
  email             text,
  phone             text,
  identity_id       text,                                    -- CC/NIT para autenticación
  sort_order        int not null default 0,
  auth_method       text default 'email'                    -- 'email','sms','otp','biometric'
    check (auth_method in ('email','sms','otp','biometric','none')),
  status            text not null default 'pending'
    check (status in ('pending','sent','viewed','signed','declined','expired')),
  signed_at         timestamptz,
  signed_ip         inet,
  signed_user_agent text,
  decline_reason    text,
  external_signer_id text,                                  -- id del provider
  signing_url       text,                                    -- URL del provider para firmar
  reminder_sent_count int not null default 0,
  metadata          jsonb default '{}'::jsonb,
  created_at        timestamptz default now()
);
create index if not exists sig_signers_envelope_idx
  on signature_signers (envelope_id, sort_order);
create index if not exists sig_signers_firm_pending_idx
  on signature_signers (firm_id, status) where status in ('pending','sent','viewed');

-- ------------------------------------------------------------
-- 3. signature_documents · docs en el sobre (source + signed)
-- ------------------------------------------------------------
create table if not exists signature_documents (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  envelope_id       uuid not null references signature_envelopes(id) on delete cascade,
  source_document_id uuid references matter_documents(id) on delete set null,
  filename          text not null,
  storage_path_source text,
  storage_path_signed text,                                  -- post-sello
  sha256_source     text,
  sha256_signed     text,
  pages             int,
  position          int not null default 0,
  created_at        timestamptz default now()
);
create index if not exists sig_docs_envelope_idx
  on signature_documents (envelope_id, position);

-- ------------------------------------------------------------
-- 4. signature_events · audit trail por sobre
-- ------------------------------------------------------------
create table if not exists signature_events (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  envelope_id       uuid not null references signature_envelopes(id) on delete cascade,
  signer_id         uuid references signature_signers(id) on delete set null,
  kind              text not null,                          -- 'created','sent','viewed','signed','declined','expired','reminder','webhook'
  actor             text,                                    -- 'firmante:nombre','sistema','provider'
  ip_address        inet,
  user_agent        text,
  payload           jsonb default '{}'::jsonb,
  occurred_at       timestamptz not null default now()
);
create index if not exists sig_events_envelope_idx
  on signature_events (envelope_id, occurred_at desc);

-- ------------------------------------------------------------
-- 5. import_jobs · trabajos de importación masiva CSV
-- ------------------------------------------------------------
create table if not exists import_jobs (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  kind              text not null check (kind in (
    'clients','matters','time_entries','expenses','leads','contacts','custom'
  )),
  source_filename   text,
  source_format     text default 'csv',
  column_mapping    jsonb default '{}'::jsonb,              -- {csv_col: our_field}
  options           jsonb default '{}'::jsonb,              -- {dry_run, on_duplicate, delimiter}
  status            text not null default 'pending'
    check (status in ('pending','validating','validated','committing','committed','failed','canceled')),
  rows_total        int default 0,
  rows_ok           int default 0,
  rows_error        int default 0,
  rows_warnings     int default 0,
  error_summary     text,
  started_at        timestamptz,
  completed_at      timestamptz,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now()
);
create index if not exists import_jobs_firm_idx
  on import_jobs (firm_id, created_at desc);

-- ------------------------------------------------------------
-- 6. import_rows · cada fila del CSV (auditable)
-- ------------------------------------------------------------
create table if not exists import_rows (
  id                bigserial primary key,
  firm_id           uuid not null references firms(id) on delete cascade,
  import_job_id     uuid not null references import_jobs(id) on delete cascade,
  line_number       int not null,
  raw_payload       jsonb,
  parsed_payload    jsonb,
  status            text not null default 'pending'
    check (status in ('pending','ok','error','duplicate','skipped','warning')),
  error             text,
  created_id        uuid,                                    -- id del registro creado (client/matter/etc.)
  warnings          jsonb default '[]'::jsonb
);
create index if not exists import_rows_job_idx
  on import_rows (import_job_id, line_number);
create index if not exists import_rows_status_idx
  on import_rows (import_job_id, status);

-- ============================================================
-- RLS
-- ============================================================
alter table signature_envelopes enable row level security;
alter table signature_signers   enable row level security;
alter table signature_documents enable row level security;
alter table signature_events    enable row level security;
alter table import_jobs         enable row level security;
alter table import_rows         enable row level security;

drop policy if exists se_select on signature_envelopes;
drop policy if exists se_modify on signature_envelopes;
create policy se_select on signature_envelopes for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy se_modify on signature_envelopes for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ss_select on signature_signers;
drop policy if exists ss_modify on signature_signers;
create policy ss_select on signature_signers for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ss_modify on signature_signers for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists sd_select on signature_documents;
drop policy if exists sd_modify on signature_documents;
create policy sd_select on signature_documents for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy sd_modify on signature_documents for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists sev_select on signature_events;
drop policy if exists sev_modify on signature_events;
create policy sev_select on signature_events for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy sev_modify on signature_events for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ij_select on import_jobs;
drop policy if exists ij_modify on import_jobs;
create policy ij_select on import_jobs for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ij_modify on import_jobs for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ir_select on import_rows;
drop policy if exists ir_modify on import_rows;
create policy ir_select on import_rows for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ir_modify on import_rows for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_se_firm_id on signature_envelopes;
create trigger trg_se_firm_id before insert on signature_envelopes
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_se_updated_at on signature_envelopes;
create trigger trg_se_updated_at before update on signature_envelopes
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_ss_firm_id on signature_signers;
create trigger trg_ss_firm_id before insert on signature_signers
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_sd_firm_id on signature_documents;
create trigger trg_sd_firm_id before insert on signature_documents
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_sev_firm_id on signature_events;
create trigger trg_sev_firm_id before insert on signature_events
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_ij_firm_id on import_jobs;
create trigger trg_ij_firm_id before insert on import_jobs
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_ir_firm_id on import_rows;
create trigger trg_ir_firm_id before insert on import_rows
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- RPCs
-- ============================================================
create or replace function lexai_signature_stats(p_firm_id uuid default null)
returns jsonb language sql stable as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id)
  select jsonb_build_object(
    'envelopes_total', (select count(*) from signature_envelopes where firm_id = (select id from f)),
    'envelopes_pending', (select count(*) from signature_envelopes
                            where firm_id = (select id from f)
                              and status in ('sent','viewed','partially_signed')),
    'envelopes_signed_30d', (select count(*) from signature_envelopes
                               where firm_id = (select id from f) and status = 'signed'
                                 and completed_at >= now() - interval '30 days'),
    'signers_pending', (select count(*) from signature_signers
                          where firm_id = (select id from f) and status in ('pending','sent','viewed'))
  );
$$;
