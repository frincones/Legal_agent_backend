"""Helper · construye `_ui_command` events para tools de ESCRITURA.

Cuando una tool inserta/actualiza/elimina filas en Postgres, el frontend
necesita una señal para refrescar el módulo que las muestra. Esta señal
viaja por el mismo canal que ya usan las canvas/ui tools: el key
`_ui_command` en el dict de retorno.

El frontend (`VoiceProvider.tsx` para voice, `AssistantSidebar.tsx` para
chat) tiene un handler global registrado en `uiCommandBus` que escucha
`action: 'data_changed'` y dispara `router.refresh()` + un CustomEvent
`lexai:data-changed` que componentes client-side específicos (TasksList,
InvoicesList, etc.) pueden escuchar para hacer su `refresh()` quirúrgico.

Convención `resource`:
  - "deadlines"          · matter_deadlines + matter_timeline
  - "notes"              · matter_notes
  - "tasks"              · tasks (productivity)
  - "comments"           · comments + mentions
  - "time_entries"       · time_entries (billable hours)
  - "expenses"           · expenses
  - "invoices"           · invoices + invoice_lines (también actualiza
                          time_entries.invoice_id)
  - "trust_transactions" · trust_transactions (cuenta fiduciaria)
  - "leads"              · leads (CRM)
  - "documents"          · matter_documents + matter_document_versions
                          (drafts, análisis, extracciones)
  - "parties"            · matter_parties
  - "predictions"        · case_predictions
  - "lessons"            · case_lessons
  - "kb"                 · knowledge_entries
  - "evidence"           · verification_attempts + inconsistency_reports +
                          probative_scores
  - "redlines"           · canvas_redlines (apply/reject)
  - "signatures"         · signature_envelopes + signature_signers
  - "wizards"            · wizard_sessions
  - "calendar_events"    · calendar_events
  - "emails"             · email_messages
  - "judicial"           · judicial_notifications + judicial_subscriptions
  - "insights"           · ai_insights
  - "matters"            · matters (cambios de estado / etapa / prioridad /
                          tags / archivado / creación)
  - "calc_results"       · calc_results

Cualquier `resource` nuevo debe documentarse aquí y manejarse en
`components/voice/VoiceProvider.tsx` (handler `data_changed`) y opcional-
mente en componentes client-side que quieran refresh quirúrgico.
"""

from __future__ import annotations

from typing import Any, Optional


def ui_data_changed(
    resource: str,
    *,
    matter_id: Optional[str] = None,
    firm_id: Optional[str] = None,
    op: str = "create",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Construye el dict `_ui_command` que el frontend dispatchea via bus.

    Args:
      resource: clave canónica del recurso (ver módulo docstring).
      matter_id: si la mutación es a un caso específico, lo incluye para
                 que el frontend solo refresque ese caso.
      firm_id:   incluido por consistencia (el handler frontend ya filtra
                 por la sesión del usuario).
      op:        "create" | "update" | "delete" — informativo, el
                 frontend puede usarlo para mostrar toasts diferentes.
      extra:     payload adicional opcional (ej. id de la fila creada).
    """
    payload: dict[str, Any] = {"action": "data_changed", "resource": resource, "op": op}
    if matter_id:
        payload["matter_id"] = str(matter_id)
    if firm_id:
        payload["firm_id"] = str(firm_id)
    if extra:
        payload["extra"] = extra
    return payload


def merge_ui_command(
    base_result: dict[str, Any],
    *,
    resource: str,
    matter_id: Optional[str] = None,
    firm_id: Optional[str] = None,
    op: str = "create",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Helper · añade `_ui_command` a un dict de resultado.

    Si la tool ya tenía su propio `_ui_command` (ej. canvas_set_text
    devuelve action='canvas_set_text'), NO se sobrescribe — la tool ya
    está pidiendo una operación visual específica y no necesita además
    data_changed. En la práctica este caso no ocurre porque las write
    tools nunca emiten ui_command propio.
    """
    if "_ui_command" in base_result:
        return base_result
    base_result["_ui_command"] = ui_data_changed(
        resource, matter_id=matter_id, firm_id=firm_id, op=op, extra=extra,
    )
    return base_result
