"""LexAI v2 — módulo unificado para Sprint M (Document Generation v3.1).

Estructura:
  blocks/        — schema Pydantic + eventos SSE tipados
  orchestrator/  — pipeline multi-stage (classifier → extractor → generator → polish → qa)
  templates/     — TemplateDef catalog (M2)
  calc/          — calculadoras puras por materia (M3)
  hunters/       — RAG hunters especializados (M3)
  verify/        — citation + derogation verifiers (M4)
  storage/       — repos para document_blocks / generation_audit
"""
