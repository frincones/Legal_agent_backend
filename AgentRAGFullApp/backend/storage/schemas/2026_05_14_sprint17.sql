-- ============================================================
-- LexAI · Sprint 17 · AI Predicciones + Smart Tasks + My Day
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. case_predictions · forecasting IA del outcome del caso
-- ------------------------------------------------------------
create table if not exists case_predictions (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid not null references matters(id) on delete cascade,
  -- distribución de probabilidad (0..1) · idealmente suman ~1.0
  prob_won          real default 0,
  prob_lost         real default 0,
  prob_settled      real default 0,
  prob_abandoned    real default 0,
  confidence        real default 0,                          -- "qué tan seguro estoy" (0..1)
  primary_outcome   text                                     -- 'won' | 'lost' | 'settled' | 'abandoned' | 'unknown'
    check (primary_outcome in ('won','lost','settled','abandoned','unknown')),
  summary           text,                                    -- 3-5 líneas explicando el pronóstico
  recommended_strategy text,
  risks             jsonb default '[]'::jsonb,               -- top riesgos identificados
  similar_lessons   jsonb default '[]'::jsonb,               -- [{lesson_id, similarity, outcome, summary}]
  inputs_signature  text,                                    -- hash de inputs para detectar staleness
  generated_by      text not null default 'llm'              -- 'llm' | 'manual'
    check (generated_by in ('llm','manual')),
  generated_at      timestamptz default now(),
  reviewed_by       uuid references users(id) on delete set null,
  reviewed_at       timestamptz,
  created_at        timestamptz default now()
);
create index if not exists case_pred_firm_matter_idx
  on case_predictions (firm_id, matter_id, generated_at desc);
create index if not exists case_pred_matter_latest_idx
  on case_predictions (matter_id, generated_at desc);

-- ------------------------------------------------------------
-- 2. tasks · tareas asignables (lightweight, no es Jira)
-- ------------------------------------------------------------
create table if not exists tasks (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid references matters(id) on delete cascade,
  title             text not null,
  description       text,
  status            text not null default 'open'
    check (status in ('open','in_progress','blocked','done','cancelled')),
  priority          text not null default 'normal'
    check (priority in ('low','normal','high','urgent')),
  assignee_user_id  uuid references users(id) on delete set null,
  due_at            timestamptz,
  completed_at      timestamptz,
  completed_by      uuid references users(id) on delete set null,
  source            text default 'manual'
    check (source in ('manual','agent','automation','imported')),
  -- vínculos opcionales a artefactos del Sprint 15-16
  source_comment_id uuid references comments(id) on delete set null,
  source_lesson_id  uuid references case_lessons(id) on delete set null,
  source_document_id uuid references matter_documents(id) on delete set null,
  tags              text[] default array[]::text[],
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists tasks_firm_status_idx
  on tasks (firm_id, status, due_at);
create index if not exists tasks_assignee_idx
  on tasks (assignee_user_id, status, due_at) where assignee_user_id is not null;
create index if not exists tasks_matter_idx
  on tasks (matter_id, status, due_at) where matter_id is not null;
create index if not exists tasks_tags_gin
  on tasks using gin (tags);

-- ------------------------------------------------------------
-- 3. saved_filters · vistas guardadas (por usuario)
-- ------------------------------------------------------------
create table if not exists saved_filters (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid not null references users(id) on delete cascade,
  scope             text not null
    check (scope in ('matters','activity','tasks','documents','kb')),
  name              text not null,
  filters           jsonb not null default '{}'::jsonb,
  pinned            boolean not null default false,
  sort_order        int not null default 0,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (user_id, scope, name)
);
create index if not exists saved_filters_user_idx
  on saved_filters (user_id, scope, sort_order);

-- ============================================================
-- RLS · firm-scoped
-- ============================================================
alter table case_predictions enable row level security;
alter table tasks            enable row level security;
alter table saved_filters    enable row level security;

drop policy if exists cp_select on case_predictions;
drop policy if exists cp_modify on case_predictions;
create policy cp_select on case_predictions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy cp_modify on case_predictions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists tk_select on tasks;
drop policy if exists tk_modify on tasks;
create policy tk_select on tasks for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy tk_modify on tasks for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists sf_select on saved_filters;
drop policy if exists sf_modify on saved_filters;
create policy sf_select on saved_filters for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy sf_modify on saved_filters for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_cp_firm_id on case_predictions;
create trigger trg_cp_firm_id before insert on case_predictions
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_tk_firm_id on tasks;
create trigger trg_tk_firm_id before insert on tasks
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_tk_updated_at on tasks;
create trigger trg_tk_updated_at before update on tasks
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_sf_firm_id on saved_filters;
create trigger trg_sf_firm_id before insert on saved_filters
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_sf_updated_at on saved_filters;
create trigger trg_sf_updated_at before update on saved_filters
  for each row execute function tg_set_updated_at();

-- Sprint 16 · activity_events: emitir cuando task se completa o asigna
create or replace function tg_task_after_change() returns trigger
language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    insert into activity_events
      (firm_id, actor_user_id, kind, matter_id, target_kind, target_id,
       title, preview, payload)
    values
      (new.firm_id, new.created_by, 'other', new.matter_id,
       'task', new.id, 'Creó tarea', left(coalesce(new.title,''), 200),
       jsonb_build_object('task_id', new.id, 'priority', new.priority,
                          'assignee', new.assignee_user_id));
  elsif tg_op = 'UPDATE' then
    if old.status <> 'done' and new.status = 'done' then
      insert into activity_events
        (firm_id, actor_user_id, kind, matter_id, target_kind, target_id,
         title, preview)
      values
        (new.firm_id, new.completed_by, 'other', new.matter_id,
         'task', new.id, 'Completó tarea', left(coalesce(new.title,''), 200));
    end if;
    if (old.assignee_user_id is distinct from new.assignee_user_id)
       and new.assignee_user_id is not null then
      insert into activity_events
        (firm_id, actor_user_id, kind, matter_id, target_kind, target_id,
         title, preview, payload)
      values
        (new.firm_id, new.created_by, 'matter_assigned', new.matter_id,
         'task', new.id, 'Asignó tarea',
         left(coalesce(new.title,''), 200),
         jsonb_build_object('assignee', new.assignee_user_id));
    end if;
  end if;
  return new;
end;
$$;
drop trigger if exists trg_tk_activity on tasks;
create trigger trg_tk_activity after insert or update on tasks
  for each row execute function tg_task_after_change();

-- ============================================================
-- RPCs
-- ============================================================

-- Predicción más reciente por matter
create or replace function lexai_latest_prediction(
  p_firm_id   uuid,
  p_matter_id uuid
) returns table (
  id uuid,
  prob_won real,
  prob_lost real,
  prob_settled real,
  prob_abandoned real,
  confidence real,
  primary_outcome text,
  summary text,
  recommended_strategy text,
  risks jsonb,
  similar_lessons jsonb,
  generated_at timestamptz,
  generated_by text,
  reviewed_at timestamptz
)
language sql stable as $$
  select id, prob_won, prob_lost, prob_settled, prob_abandoned,
         confidence, primary_outcome, summary, recommended_strategy,
         risks, similar_lessons, generated_at, generated_by, reviewed_at
    from case_predictions
   where firm_id = p_firm_id and matter_id = p_matter_id
   order by generated_at desc
   limit 1;
$$;

-- Búsqueda de matters similares a uno dado, usando embedding de la lesson más reciente o título
-- Devuelve matters distintos al input · ordenados por proximidad de vector.
create or replace function lexai_similar_matters(
  p_firm_id   uuid,
  p_embedding vector(1536),
  p_exclude_matter_id uuid default null,
  p_limit     int default 10
) returns table (
  matter_id uuid,
  lesson_id uuid,
  similarity real,
  titulo text,
  outcome text,
  summary text,
  tags text[]
)
language sql stable as $$
  with ranked as (
    select l.matter_id, l.id as lesson_id,
           m.titulo,
           l.outcome, l.summary, l.tags,
           (1.0 - (l.embedding <=> p_embedding))::real as similarity,
           row_number() over (partition by l.matter_id
                              order by l.embedding <=> p_embedding) as rn
      from case_lessons l
      join matters m on m.id = l.matter_id
     where l.firm_id = p_firm_id
       and l.embedding is not null
       and (p_exclude_matter_id is null or l.matter_id <> p_exclude_matter_id)
  )
  select matter_id, lesson_id, similarity, titulo, outcome, summary, tags
    from ranked
   where rn = 1
   order by similarity desc
   limit p_limit;
$$;

-- My Day · agregador completo para el dashboard personal
-- Devuelve JSONB con counts + arrays de los top items.
create or replace function lexai_my_day(
  p_firm_id   uuid,
  p_user_id   uuid,
  p_horizon_days int default 7
) returns jsonb
language plpgsql stable as $$
declare
  v_now timestamptz := now();
  v_until timestamptz := v_now + make_interval(days => p_horizon_days);
begin
  return jsonb_build_object(
    'now', v_now,
    'horizon_until', v_until,
    'tasks_open', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'id', id, 'title', title, 'priority', priority, 'status', status,
        'due_at', due_at, 'matter_id', matter_id
      ) order by case priority
                   when 'urgent' then 1 when 'high' then 2
                   when 'normal' then 3 else 4 end,
                due_at nulls last), '[]'::jsonb)
        from tasks
       where firm_id = p_firm_id and assignee_user_id = p_user_id
         and status in ('open','in_progress','blocked')
       limit 20
    ),
    'tasks_open_count', (
      select count(*)::int from tasks
       where firm_id = p_firm_id and assignee_user_id = p_user_id
         and status in ('open','in_progress','blocked')
    ),
    'deadlines_upcoming', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'id', id, 'titulo', titulo, 'fecha', fecha, 'tipo', tipo,
        'matter_id', matter_id
      ) order by fecha), '[]'::jsonb)
        from matter_deadlines
       where firm_id = p_firm_id
         and completado = false
         and fecha between v_now and v_until
       limit 15
    ),
    'mentions_unread', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'id', m.id, 'comment_id', m.comment_id,
        'body_preview', m.body_preview, 'mentioned_by', m.mentioned_by,
        'matter_id', m.matter_id, 'created_at', m.created_at
      ) order by m.created_at desc), '[]'::jsonb)
        from mention_notifications m
       where m.firm_id = p_firm_id and m.user_id = p_user_id
         and m.read_at is null
       limit 10
    ),
    'mentions_unread_count', (
      select count(*)::int from mention_notifications
       where firm_id = p_firm_id and user_id = p_user_id and read_at is null
    ),
    'comments_open_assigned', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'id', c.id, 'body', left(c.body, 240),
        'matter_id', c.matter_id, 'created_by', c.created_by,
        'created_at', c.created_at
      ) order by c.created_at desc), '[]'::jsonb)
        from comments c
       where c.firm_id = p_firm_id
         and c.resolved = false
         and p_user_id = any(c.mentions)
       limit 10
    ),
    'predictions_recent', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'matter_id', cp.matter_id, 'primary_outcome', cp.primary_outcome,
        'prob_won', cp.prob_won, 'confidence', cp.confidence,
        'generated_at', cp.generated_at
      ) order by cp.generated_at desc), '[]'::jsonb)
        from case_predictions cp
       where cp.firm_id = p_firm_id
         and cp.generated_at > v_now - interval '7 days'
       limit 8
    )
  );
end;
$$;
