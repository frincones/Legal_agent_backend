-- ============================================================
-- Sprint 3 · Canvas + Citas pulido
-- ============================================================
-- Date: 2026-05-09
-- Adds:
--   1. legal_alerts            (proactive alerts: derogations,
--                               new jurisprudence, code updates)
--   2. document_citations
--      .replacement_ref        (sugerencia de reemplazo cuando
--                               la cita original está superada)
--      .replacement_at         (fecha en que se sugirió)
-- Idempotent.
-- ============================================================

begin;

-- ──────────────────────────────────────────────────────────────
-- 1. legal_alerts (M07.07 + M24 storage)
-- ──────────────────────────────────────────────────────────────
create table if not exists legal_alerts (
  id           uuid primary key default gen_random_uuid(),
  firm_id      uuid not null references firms(id) on delete cascade,
  user_id      uuid references users(id) on delete cascade,
  -- target_ref: citation_ref (T-388/2019), norma_ref (Ley 50/1990) o tema (string libre)
  target_type  text not null check (target_type in ('citation','norma','codigo','tema','articulo')),
  target_ref   text not null,
  -- kind del cambio detectado:
  kind         text not null check (kind in (
    'derogada','modificada','nueva_jurisprudencia','cambio_normativo',
    'sentencia_relevante','suspendida'
  )),
  severity     text not null default 'info' check (severity in ('info','warning','critical')),
  title        text not null,
  description  text,
  source_url   text,
  source       text default 'rules' check (source in (
    'rules','grafo_derogacion','watcher_diario_oficial',
    'watcher_corte_constitucional','watcher_senado','manual'
  )),
  -- a qué casos afecta (referencias a matters)
  affected_matter_ids uuid[] default '{}',
  -- a qué documentos afecta (matter_documents · vía citas)
  affected_document_ids uuid[] default '{}',
  metadata     jsonb default '{}'::jsonb,
  detected_at  timestamptz default now(),
  read_at      timestamptz,
  dismissed_at timestamptz
);

create index if not exists legal_alerts_firm_unread_idx
  on legal_alerts (firm_id, detected_at desc) where read_at is null;
create index if not exists legal_alerts_target_idx
  on legal_alerts (target_type, target_ref);
create index if not exists legal_alerts_user_idx
  on legal_alerts (user_id) where user_id is not null;

alter table legal_alerts enable row level security;

drop policy if exists legal_alerts_select on legal_alerts;
create policy legal_alerts_select on legal_alerts
  for select to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()));

drop policy if exists legal_alerts_modify on legal_alerts;
create policy legal_alerts_modify on legal_alerts
  for all to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()))
  with check (firm_id = (select firm_id from users where id = auth.uid()));

-- ──────────────────────────────────────────────────────────────
-- 2. document_citations: replacement suggestion fields
-- ──────────────────────────────────────────────────────────────
alter table document_citations
  add column if not exists replacement_ref  text;

alter table document_citations
  add column if not exists replacement_at   timestamptz;

alter table document_citations
  add column if not exists replacement_reason text;

commit;
