-- Redline tables · used by review_contract / apply_redline / reject_redline
-- tools. The tools assumed these tables existed but they were never created.

begin;

create table if not exists redline_sets (
  id                    uuid primary key default gen_random_uuid(),
  firm_id               uuid not null references firms(id) on delete cascade,
  matter_document_id    uuid,                   -- soft FK · doc may be archived
  generated_by          uuid references users(id) on delete set null,
  source_skill          text default '/revisar/contrato',
  status                text not null default 'pending'
    check (status in ('pending', 'partially_applied', 'fully_applied', 'rejected', 'expired')),
  metadata              jsonb default '{}'::jsonb,
  created_at            timestamptz default now(),
  updated_at            timestamptz default now()
);

create index if not exists redline_sets_firm_idx
  on redline_sets (firm_id, status, created_at desc);
create index if not exists redline_sets_doc_idx
  on redline_sets (matter_document_id)
  where matter_document_id is not null;

create table if not exists redlines (
  id                uuid primary key default gen_random_uuid(),
  redline_set_id    uuid not null references redline_sets(id) on delete cascade,
  firm_id           uuid not null references firms(id) on delete cascade,
  clause_ref        text,                       -- e.g. "Cláusula 5", "Sec. 2.1"
  severity          text not null
    check (severity in ('red', 'yellow', 'green', 'info')),
  issue             text not null,
  original_text     text,
  suggested_text    text,
  status            text not null default 'pending'
    check (status in ('pending', 'accepted', 'rejected', 'modified')),
  decision_by       uuid references users(id) on delete set null,
  decision_at       timestamptz,
  decision_notes    text,
  created_at        timestamptz default now()
);

create index if not exists redlines_set_idx on redlines (redline_set_id, status);
create index if not exists redlines_firm_idx on redlines (firm_id, severity);

-- RLS · scoped by firm_id
alter table redline_sets enable row level security;
alter table redlines enable row level security;

drop policy if exists redline_sets_select on redline_sets;
create policy redline_sets_select on redline_sets
  for select to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()));

drop policy if exists redline_sets_modify on redline_sets;
create policy redline_sets_modify on redline_sets
  for all to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()))
  with check (firm_id = (select firm_id from users where id = auth.uid()));

drop policy if exists redlines_select on redlines;
create policy redlines_select on redlines
  for select to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()));

drop policy if exists redlines_modify on redlines;
create policy redlines_modify on redlines
  for all to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()))
  with check (firm_id = (select firm_id from users where id = auth.uid()));

-- updated_at trigger
do $$
begin
  if exists (select 1 from pg_proc where proname = 'tg_set_updated_at') then
    drop trigger if exists redline_sets_set_updated_at on redline_sets;
    create trigger redline_sets_set_updated_at
      before update on redline_sets
      for each row execute function tg_set_updated_at();
  end if;
end $$;

commit;
