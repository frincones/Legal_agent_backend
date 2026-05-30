"""Sprint M20.05 · S4 · scripts de canary 1 firm QA.

Conjunto de scripts para gestionar el rollout canary del LeanOrchestrator
a una firm QA controlada antes del A/B masivo.

Scripts:
  - canary_enable.py    → set LEAN_ORCHESTRATOR_FIRMS=<uuid> en Railway (manual via UI o GraphQL)
  - canary_smoke.py     → genera 5 documentos contra firm canary + verifica respuesta
  - canary_metrics.py   → dashboard rápido de métricas comparativas vs baseline
  - canary_rollback.py  → set USE_LEAN_ORCHESTRATOR=false + flag para revertir 100%
  - smoke_trinidad.py   → replica el caso Trinidad real + LLM-judge vs Claude desktop output
"""
