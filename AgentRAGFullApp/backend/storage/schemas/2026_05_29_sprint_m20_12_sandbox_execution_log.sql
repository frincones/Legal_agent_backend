-- ============================================================
-- LexAI · Sprint M20.12 · sandbox_execution_log
-- Migration date: 2026-05-29
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- Audit completo de cada invocación del sandbox bubblewrap.
-- Permite forense de qué código LLM se ejecutó, qué network requests
-- hizo, y detectar patrones de uso abusivo.

create table if not exists sandbox_execution_log (
  id                  uuid primary key default gen_random_uuid(),
  generation_id       uuid not null,
  firm_id             uuid,
  user_id             uuid,
  tool_name           text,                            -- 'calc_legal', 'build_docx', etc.
  started_at          timestamptz not null default now(),
  duration_ms         int,
  exit_code           int,
  success             boolean,
  error_class         text,
  error_message       text,
  backend_used        text,                            -- 'bubblewrap' | 'subprocess_fallback'
  code_hash           text,                            -- sha256 del código ejecutado (auditoría)
  code_preview        text,                            -- primeros 500 chars del código
  input_hash          text,                            -- sha256 del INPUT data
  stdout_preview      text,                            -- primeros 1000 chars de stdout
  stderr_preview      text,                            -- primeros 1000 chars de stderr
  files_created       jsonb default '[]'::jsonb,       -- lista de archivos generados en /workdir
  network_requests    jsonb default '[]'::jsonb,       -- {host, path, status, bytes} de cada request
  bytes_read_network  bigint default 0,
  memory_peak_mb      int,
  timeout_s           int,
  metadata            jsonb default '{}'::jsonb
);

create index if not exists sandbox_log_generation_idx
  on sandbox_execution_log (generation_id, started_at desc);
create index if not exists sandbox_log_firm_started_idx
  on sandbox_execution_log (firm_id, started_at desc);
create index if not exists sandbox_log_tool_success_idx
  on sandbox_execution_log (tool_name, success);
create index if not exists sandbox_log_backend_idx
  on sandbox_execution_log (backend_used, started_at desc);

alter table sandbox_execution_log enable row level security;

drop policy if exists sandbox_log_select on sandbox_execution_log;
create policy sandbox_log_select on sandbox_execution_log for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists sandbox_log_modify on sandbox_execution_log;
create policy sandbox_log_modify on sandbox_execution_log for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

comment on table sandbox_execution_log is
  'M20.12: audit forense del sandbox bubblewrap (S8). Cada call al
   sandbox queda registrada con código_hash + network_requests para
   detectar abuso o regresión.';
