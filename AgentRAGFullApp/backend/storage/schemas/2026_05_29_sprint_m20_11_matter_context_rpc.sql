-- ============================================================
-- LexAI · Sprint M20.11 · RPC lexai_matter_full_context
-- Migration date: 2026-05-29
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- Consolidador SQL para que la tool `load_matter_context` traiga
-- en UN solo round-trip todo el contexto de un matter:
--   matter, parties, deadlines, risks, timeline, documents.
--
-- Esto reemplaza N queries individuales (5+ round-trips) por 1
-- y devuelve JSONB ya estructurado para inyectar al Brain.

create or replace function lexai_matter_full_context(p_matter_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_matter      jsonb;
  v_parties     jsonb;
  v_deadlines   jsonb;
  v_risks       jsonb;
  v_timeline    jsonb;
  v_documents   jsonb;
  v_result      jsonb;
begin
  -- matter base
  select to_jsonb(m) into v_matter
  from (
    select
      id, firm_id, client_id, display_id, titulo, materia, etapa_procesal,
      tribunal, juzgado, expediente, status, priority,
      proxima_fecha, proxima_tipo, cuantia, cuantia_currency, pendientes
    from matters
    where id = p_matter_id
  ) m;

  if v_matter is null then
    return jsonb_build_object('_warning', 'matter no encontrado', 'matter_id', p_matter_id);
  end if;

  -- parties (max 20)
  select coalesce(jsonb_agg(to_jsonb(p)), '[]'::jsonb) into v_parties
  from (
    select id, rol, nombre, tax_id, client_id, origen
    from matter_parties
    where matter_id = p_matter_id
    order by created_at desc
    limit 20
  ) p;

  -- deadlines (próximos 5, no completados)
  select coalesce(jsonb_agg(to_jsonb(d)), '[]'::jsonb) into v_deadlines
  from (
    select id, titulo, fecha, tipo, completado
    from matter_deadlines
    where matter_id = p_matter_id
      and (completado is null or completado = false)
    order by fecha asc nulls last
    limit 5
  ) d;

  -- risks abiertos (max 5)
  begin
    select coalesce(jsonb_agg(to_jsonb(r)), '[]'::jsonb) into v_risks
    from (
      select id, type, severity, title, description, mitigation, detected_at
      from case_risks
      where matter_id = p_matter_id
        and resolved_at is null
      order by severity desc
      limit 5
    ) r;
  exception when undefined_table then
    v_risks := '[]'::jsonb;
  end;

  -- timeline (últimos 20 eventos)
  begin
    select coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) into v_timeline
    from (
      select ts, kind, payload
      from matter_timeline
      where matter_id = p_matter_id
      order by ts desc
      limit 20
    ) t;
  exception when undefined_table then
    v_timeline := '[]'::jsonb;
  end;

  -- documents (últimos 10)
  begin
    select coalesce(jsonb_agg(to_jsonb(doc)), '[]'::jsonb) into v_documents
    from (
      select id, kind, titulo, status, resumen_ia, byte_size, created_at
      from matter_documents
      where matter_id = p_matter_id
      order by created_at desc
      limit 10
    ) doc;
  exception when undefined_table then
    v_documents := '[]'::jsonb;
  end;

  v_result := jsonb_build_object(
    'matter',     v_matter,
    'parties',    v_parties,
    'deadlines',  v_deadlines,
    'risks',      v_risks,
    'timeline',   v_timeline,
    'documents',  v_documents,
    'generated_at', now()
  );

  return v_result;
end;
$$;

comment on function lexai_matter_full_context(uuid) is
  'M20.11: consolida matter+parties+deadlines+risks+timeline+documents en 1 JSONB.
   Usado por tool load_matter_context (Brain ReAct).';

-- ============================================================
-- Grants para que el role authenticated pueda invocar (multi-tenant)
-- RLS se respeta porque la función es STABLE y las SELECT internas
-- son contra tablas con RLS por firm_id.
-- ============================================================
grant execute on function lexai_matter_full_context(uuid) to authenticated;
grant execute on function lexai_matter_full_context(uuid) to service_role;
