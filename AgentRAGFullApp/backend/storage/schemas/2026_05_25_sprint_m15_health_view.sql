-- ============================================================
-- LexAI · Sprint M15 · Verification health view
-- Migration date: 2026-05-25
-- Idempotent
-- ============================================================

-- View: v_verification_health
-- Agrupado por result_state + source, últimos 7 días
create or replace view v_verification_health as
select
  result_state,
  source,
  count(*) as total,
  round(avg(coalesce(confidence_score, 0))::numeric, 3) as avg_confidence,
  round(avg(coalesce(duration_ms, 0))::numeric, 0) as avg_duration_ms,
  percentile_cont(0.5) within group (order by coalesce(duration_ms, 0)) as p50_ms,
  percentile_cont(0.95) within group (order by coalesce(duration_ms, 0)) as p95_ms,
  count(*) filter (where result_state = 'verificada') as verificadas,
  count(*) filter (where result_state = 'no_encontrada') as no_encontradas,
  max(created_at) as last_attempt_at
from verification_attempts
where created_at > now() - interval '7 days'
group by result_state, source
order by total desc;


-- View: v_verification_shadow_summary (M14 SHADOW_MODE)
create or replace view v_verification_shadow_summary as
select
  diff_type,
  count(*) as total,
  count(*) filter (where is_critical) as critical_count,
  count(distinct citation_ref) as unique_citations,
  max(created_at) as last_diff_at
from verification_shadow_diffs
where created_at > now() - interval '7 days'
group by diff_type
order by
  case diff_type
    when 'critical' then 1
    when 'medium' then 2
    when 'minor' then 3
    else 4
  end;
