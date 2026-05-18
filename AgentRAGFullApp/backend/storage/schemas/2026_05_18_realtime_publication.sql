-- Capa 3 · Habilitar Supabase Realtime para tablas de dominio.
-- ====================================================================
-- Las 4 tablas con Realtime preexistentes (canvas_redlines, hitl_interrupts,
-- signature_envelopes, firm_integrations) probaron el patrón. Esta
-- migración extiende cobertura a las P0 + P1 que más sufren la
-- desconexión agente→UI según docs/agent-ui-sync-audit.md.
--
-- Idempotente: alter publication ... add table NO falla si ya existe.

do $$
declare
  tbl text;
  tables_to_add text[] := array[
    'matter_deadlines',
    'matter_notes',
    'tasks',
    'matters',
    'comments',
    'time_entries',
    'expenses',
    'invoices',
    'trust_transactions',
    'leads',
    'case_lessons',
    'case_predictions',
    'knowledge_entries',
    'judicial_notifications',
    'legal_alerts',
    'ai_insights',
    'matter_documents',
    'matter_parties'
  ];
begin
  -- Crea la publication si no existe (Supabase la trae por default
  -- pero es defensivo).
  if not exists (
    select 1 from pg_publication where pubname = 'supabase_realtime'
  ) then
    create publication supabase_realtime;
  end if;

  foreach tbl in array tables_to_add loop
    -- Solo añade si la tabla existe en el schema public.
    if exists (
      select 1 from information_schema.tables
       where table_schema = 'public' and table_name = tbl
    ) and not exists (
      select 1 from pg_publication_tables
       where pubname = 'supabase_realtime'
         and schemaname = 'public'
         and tablename = tbl
    ) then
      execute format(
        'alter publication supabase_realtime add table public.%I',
        tbl
      );
      raise notice 'realtime · added %', tbl;
    end if;
  end loop;
end $$;

comment on publication supabase_realtime is
  'Capa 3 sync agente↔UI · 18 tablas de dominio + las 4 originales (canvas_redlines, hitl_interrupts, signature_envelopes, firm_integrations).';
