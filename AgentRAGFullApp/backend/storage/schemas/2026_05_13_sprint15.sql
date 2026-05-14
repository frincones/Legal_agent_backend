-- ============================================================
-- LexAI · Sprint 15 · Knowledge Base + Memoria del despacho
-- Migration date: 2026-05-13
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. kb_collections · agrupación lógica (carpetas) opcional
-- ------------------------------------------------------------
create table if not exists kb_collections (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  name              text not null,
  description       text,
  color             text default 'blue',
  parent_id         uuid references kb_collections(id) on delete cascade,
  sort_order        int not null default 0,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  unique (firm_id, parent_id, name)
);
create index if not exists kb_collections_firm_idx
  on kb_collections (firm_id, sort_order);

-- ------------------------------------------------------------
-- 2. knowledge_entries · piezas individuales (artículos, precedentes, tips)
-- ------------------------------------------------------------
create table if not exists knowledge_entries (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  collection_id     uuid references kb_collections(id) on delete set null,
  kind              text not null default 'note'
    check (kind in ('note','precedent','strategy','template_comment','citation_note',
                    'lesson_learned','procedure','case_summary','contact_note')),
  title             text not null,
  body              text not null,
  tags              text[] default array[]::text[],
  source_matter_id  uuid references matters(id) on delete set null,
  source_document_id uuid references matter_documents(id) on delete set null,
  related_citations jsonb default '[]'::jsonb,           -- ["T-388/2019", "CST Art. 64"]
  visibility        text not null default 'firm'
    check (visibility in ('private','firm','public')),
  pinned            boolean not null default false,
  view_count        int not null default 0,
  last_used_at      timestamptz,
  embedding         vector(1536),                         -- text-embedding-3-small
  embedding_at      timestamptz,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists kb_entries_firm_kind_idx
  on knowledge_entries (firm_id, kind, updated_at desc);
create index if not exists kb_entries_firm_pinned_idx
  on knowledge_entries (firm_id, pinned, updated_at desc) where pinned = true;
create index if not exists kb_entries_tags_gin
  on knowledge_entries using gin (tags);
create index if not exists kb_entries_text_search
  on knowledge_entries using gin (to_tsvector('spanish', coalesce(title,'') || ' ' || coalesce(body,'')));
-- Vector index (HNSW si está disponible, sino IVFFlat)
do $$ begin
  if not exists (select 1 from pg_class where relname = 'kb_entries_embedding_idx') then
    begin
      execute 'create index kb_entries_embedding_idx on knowledge_entries '
              'using hnsw (embedding vector_cosine_ops)';
    exception when others then
      execute 'create index kb_entries_embedding_idx on knowledge_entries '
              'using ivfflat (embedding vector_cosine_ops) with (lists = 100)';
    end;
  end if;
end $$;

-- ------------------------------------------------------------
-- 3. kb_annotations · highlights / comentarios sobre documentos
-- ------------------------------------------------------------
create table if not exists kb_annotations (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_document_id uuid not null references matter_documents(id) on delete cascade,
  matter_id         uuid references matters(id) on delete set null,
  user_id           uuid references users(id) on delete set null,
  page              int,
  text_quote        text,                                  -- el texto resaltado
  body              text not null,                         -- el comentario del abogado
  color             text default 'yellow',
  kind              text default 'highlight'
    check (kind in ('highlight','question','important','red_flag','reference')),
  created_at        timestamptz default now()
);
create index if not exists kb_ann_doc_idx
  on kb_annotations (matter_document_id, page);
create index if not exists kb_ann_firm_idx
  on kb_annotations (firm_id, created_at desc);

-- ------------------------------------------------------------
-- 4. case_lessons · "memoria del despacho" extraída de casos cerrados
-- ------------------------------------------------------------
create table if not exists case_lessons (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid not null references matters(id) on delete cascade,
  outcome           text not null default 'unknown'
    check (outcome in ('won','lost','settled','abandoned','unknown')),
  summary           text not null,                         -- 3-5 líneas
  strategy_used     text,                                  -- qué estrategia funcionó (o no)
  what_worked       text,
  what_failed       text,
  key_citations     jsonb default '[]'::jsonb,
  key_arguments     jsonb default '[]'::jsonb,
  tags              text[] default array[]::text[],
  generated_by      text not null default 'manual'
    check (generated_by in ('manual','llm','llm_curated')),
  reviewed_by       uuid references users(id) on delete set null,
  reviewed_at       timestamptz,
  embedding         vector(1536),
  embedding_at      timestamptz,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (matter_id, generated_by)
);
create index if not exists case_lessons_firm_idx
  on case_lessons (firm_id, outcome, created_at desc);
create index if not exists case_lessons_tags_gin
  on case_lessons using gin (tags);
do $$ begin
  if not exists (select 1 from pg_class where relname = 'case_lessons_embedding_idx') then
    begin
      execute 'create index case_lessons_embedding_idx on case_lessons '
              'using hnsw (embedding vector_cosine_ops)';
    exception when others then
      execute 'create index case_lessons_embedding_idx on case_lessons '
              'using ivfflat (embedding vector_cosine_ops) with (lists = 50)';
    end;
  end if;
end $$;

-- ============================================================
-- RLS
-- ============================================================
alter table kb_collections      enable row level security;
alter table knowledge_entries   enable row level security;
alter table kb_annotations      enable row level security;
alter table case_lessons        enable row level security;

drop policy if exists kbc_select on kb_collections;
drop policy if exists kbc_modify on kb_collections;
create policy kbc_select on kb_collections for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy kbc_modify on kb_collections for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists kbe_select on knowledge_entries;
drop policy if exists kbe_modify on knowledge_entries;
create policy kbe_select on knowledge_entries for select
  using (
    firm_id = auth_firm_id() or
    visibility = 'public' or
    auth.role() = 'service_role'
  );
create policy kbe_modify on knowledge_entries for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists kba_select on kb_annotations;
drop policy if exists kba_modify on kb_annotations;
create policy kba_select on kb_annotations for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy kba_modify on kb_annotations for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists cl_select on case_lessons;
drop policy if exists cl_modify on case_lessons;
create policy cl_select on case_lessons for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy cl_modify on case_lessons for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_kbc_firm_id on kb_collections;
create trigger trg_kbc_firm_id before insert on kb_collections
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_kbe_firm_id on knowledge_entries;
create trigger trg_kbe_firm_id before insert on knowledge_entries
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_kbe_updated_at on knowledge_entries;
create trigger trg_kbe_updated_at before update on knowledge_entries
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_kba_firm_id on kb_annotations;
create trigger trg_kba_firm_id before insert on kb_annotations
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_cl_firm_id on case_lessons;
create trigger trg_cl_firm_id before insert on case_lessons
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_cl_updated_at on case_lessons;
create trigger trg_cl_updated_at before update on case_lessons
  for each row execute function tg_set_updated_at();

-- ============================================================
-- RPCs · búsqueda semántica
-- ============================================================

-- Búsqueda híbrida en knowledge_entries: vector + texto
create or replace function lexai_kb_search(
  p_firm_id   uuid,
  p_query     text,
  p_embedding vector(1536) default null,
  p_kind      text default null,
  p_limit     int default 15
) returns table (
  id uuid,
  title text,
  body text,
  kind text,
  tags text[],
  source_matter_id uuid,
  source_document_id uuid,
  pinned boolean,
  rank real
)
language sql stable as $$
  with q as (select coalesce(nullif(trim(p_query),''),'') as text),
       qts as (select plainto_tsquery('spanish', (select text from q)) as ts)
  select e.id, e.title, e.body, e.kind, e.tags,
         e.source_matter_id, e.source_document_id, e.pinned,
         -- rank combinado: vector ↑ + text rank + pinned boost
         (case when p_embedding is not null and e.embedding is not null
                then (1.0 - (e.embedding <=> p_embedding))::real
                else 0
           end
          + coalesce(ts_rank(
              to_tsvector('spanish', coalesce(e.title,'') || ' ' || coalesce(e.body,'')),
              (select ts from qts)
            )::real, 0) * 0.5
          + case when e.pinned then 0.15::real else 0::real end
         ) as rank
    from knowledge_entries e
   where e.firm_id = p_firm_id
     and (p_kind is null or e.kind = p_kind)
     and (
       (p_embedding is not null and e.embedding is not null)
       or (length((select text from q)) > 0 and (
         (select ts from qts) @@ to_tsvector('spanish', coalesce(e.title,'') || ' ' || coalesce(e.body,''))
         or e.title ilike '%' || (select text from q) || '%'
       ))
     )
   order by rank desc nulls last
   limit p_limit;
$$;

-- Búsqueda en case_lessons (memoria del despacho)
create or replace function lexai_lessons_search(
  p_firm_id   uuid,
  p_embedding vector(1536),
  p_outcome   text default null,
  p_limit     int default 10
) returns table (
  id uuid,
  matter_id uuid,
  outcome text,
  summary text,
  strategy_used text,
  what_worked text,
  tags text[],
  similarity real
)
language sql stable as $$
  select l.id, l.matter_id, l.outcome, l.summary, l.strategy_used,
         l.what_worked, l.tags,
         (1.0 - (l.embedding <=> p_embedding))::real as similarity
    from case_lessons l
   where l.firm_id = p_firm_id
     and l.embedding is not null
     and (p_outcome is null or l.outcome = p_outcome)
   order by l.embedding <=> p_embedding
   limit p_limit;
$$;

-- Stats KB
create or replace function lexai_kb_stats(p_firm_id uuid default null)
returns jsonb language sql stable as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id)
  select jsonb_build_object(
    'entries_total', (select count(*) from knowledge_entries where firm_id = (select id from f)),
    'entries_embedded', (select count(*) from knowledge_entries where firm_id = (select id from f) and embedding is not null),
    'entries_pinned', (select count(*) from knowledge_entries where firm_id = (select id from f) and pinned = true),
    'lessons_total', (select count(*) from case_lessons where firm_id = (select id from f)),
    'lessons_won', (select count(*) from case_lessons where firm_id = (select id from f) and outcome = 'won'),
    'annotations_total', (select count(*) from kb_annotations where firm_id = (select id from f)),
    'collections_total', (select count(*) from kb_collections where firm_id = (select id from f))
  );
$$;
