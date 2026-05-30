"""Sprint M20.06 · S5 · scripts de rollout gradual A/B 10% → 50%.

Scripts:
  - set_percentage.py        → cambia LEAN_ORCHESTRATOR_PERCENTAGE en Railway
  - ab_metrics_compare.py    → comparación rigurosa por arm (lean vs legacy) con stats
  - weekly_ab_report.py      → reporte automatizado semanal MD + JSON (envía Slack opcional)
  - alert_thresholds.py      → polea métricas y dispara alerta si cruza thresholds
  - hotfix_helper.py         → asistente para iterar system prompt o tools sin redeploy
"""
