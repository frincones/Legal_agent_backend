"""LexAI Claude renderer (camino B) · genera .docx via Node.js + docx-js@9.5.

Componentes:
  - node_executor: sandbox subprocess para ejecutar JS docx-js
  - claude_docx_renderer: pipeline LLM Claude → JS → docx con retry loop
  - skills/docx: built-in skill (SKILL.md + validation + bundled) progressive disclosure

Activación: feature flag CLAUDE_RENDERER_ENABLED. Si OFF o doc_family no incluido
en CLAUDE_RENDERER_DOC_FAMILIES, cae a lex/docx_forensic_builder.py (legacy).
"""
