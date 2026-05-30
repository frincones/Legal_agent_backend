"""Sprint M20.01 · Tests de contrato (regression net).

Estos tests congelan el shape de:
  - SSE events emitidos al frontend (30+)
  - Audit payloads (generation_audit, document_blocks, verification_attempts, chat_messages)
  - Block types (15+)

Sirven como red de seguridad antes y durante el refactor a LeanOrchestrator.
DEBEN seguir verdes:
  - contra pipeline actual (legacy)
  - contra el nuevo LeanOrchestrator (lean)

Si un test rompe → el refactor está cambiando el contrato → fix antes de mergear.
"""
