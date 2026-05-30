-- ============================================================
-- LexAI · Sprint M20.12 · firm_playbook_history
-- Migration date: 2026-05-29
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- Historial de versiones del firm_playbook. Cada UPDATE en firm_playbook
-- dispara un trigger que guarda la versión anterior aquí.
-- Permite al usuario ver diffs y restaurar versiones previas.

create table if not exists firm_playbook_history (
  id                    uuid primary key default gen_random_uuid(),
  firm_id               uuid not null,
  version               int not null,
  jurisdiction_default  text,
  redline_style         text,
  tone                  text,
  preferred_clauses     jsonb,
  forbidden_terms       text[],
  required_clauses      text[],
  escalation_matrix     jsonb,
  raw_md                text,
  updated_by            uuid,
  archived_at           timestamptz not null default now(),
  unique (firm_id, version)
);

create index if not exists firm_playbook_history_firm_idx
  on firm_playbook_history (firm_id, version desc);

alter table firm_playbook_history enable row level security;

drop policy if exists fph_select on firm_playbook_history;
create policy fph_select on firm_playbook_history for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists fph_modify on firm_playbook_history;
create policy fph_modify on firm_playbook_history for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- Trigger: cada UPDATE en firm_playbook archiva la versión anterior
create or replace function archive_firm_playbook_version()
returns trigger
language plpgsql
security definer
as $$
begin
  -- Solo archivar si cambió algo material (no en updates triviales)
  if old.raw_md is distinct from new.raw_md
     or old.preferred_clauses is distinct from new.preferred_clauses
     or old.forbidden_terms is distinct from new.forbidden_terms
     or old.required_clauses is distinct from new.required_clauses
     or old.escalation_matrix is distinct from new.escalation_matrix then
    insert into firm_playbook_history (
      firm_id, version, jurisdiction_default, redline_style, tone,
      preferred_clauses, forbidden_terms, required_clauses,
      escalation_matrix, raw_md, updated_by
    ) values (
      old.firm_id, old.version, old.jurisdiction_default, old.redline_style,
      old.tone, old.preferred_clauses, old.forbidden_terms,
      old.required_clauses, old.escalation_matrix, old.raw_md, old.updated_by
    )
    on conflict (firm_id, version) do nothing;
  end if;
  return new;
end;
$$;

drop trigger if exists firm_playbook_archive_trigger on firm_playbook;
create trigger firm_playbook_archive_trigger
  before update on firm_playbook
  for each row execute function archive_firm_playbook_version();

comment on table firm_playbook_history is
  'M20.12: historial de versiones de firm_playbook. Trigger automático
   archiva la versión anterior en cada UPDATE material.';
