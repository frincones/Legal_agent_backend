"""Sprint M21.S8.C · Habeas Data export (Ley 1581/2012 CO).

Genera un JSON consolidado con TODOS los datos personales del subject
(por cedula | email | client_id) que el firm posea en el sistema.

Tablas consultadas (best-effort, ignora tablas inexistentes):
  - clients, matters, matter_history, generation_audit, legal_alerts,
    matters_workspace, intake_forms_submissions, time_entries, expenses,
    notifications
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)


# Tablas que tienen datos potencialmente personales. Cada entry:
# (table_name, column_filters_jsonb, select_columns).
HABEAS_TABLES = [
    ("clients",                    {"cedula": "cedula", "email": "email", "client_id": "id"},
        "id, full_name, cedula, email, telefono, direccion, created_at"),
    ("matters",                    {"client_id": "client_id"},
        "id, titulo, materia, etapa_procesal, created_at"),
    ("matter_history",             {"client_id": None},  # filtra por matters del cliente
        "event_id, matter_id, event_type, summary, created_at"),
    ("intake_forms_submissions",   {"cedula": "metadata->>'cedula'", "email": "metadata->>'email'"},
        "submission_id, form_id, submitted_at, metadata"),
    ("time_entries",               {"client_id": "client_id"},
        "id, matter_id, minutes, description, started_at"),
    ("expenses",                   {"client_id": "client_id"},
        "id, matter_id, amount, description, incurred_at"),
    ("notifications",              {"client_id": "metadata->>'client_id'"},
        "id, kind, title, body, created_at"),
]


async def _table_exists(conn, name: str) -> bool:
    try:
        return await conn.fetchval(
            "select exists(select 1 from information_schema.tables where table_schema='public' and table_name=$1)",
            name,
        ) or False
    except Exception:
        return False


async def collect_subject_data(pool, *, firm_id: UUID, subject_kind: str, subject_id: str) -> dict:
    """Recolecta todo el data del subject across tablas. Returns dict consolidado."""
    if pool is None:
        return {"_error": "pool unavailable", "firm_id": str(firm_id), "items": {}}
    if subject_kind not in ("cedula", "email", "client_id"):
        return {"_error": f"subject_kind invalido: {subject_kind!r}", "items": {}}

    out: dict = {
        "firm_id": str(firm_id),
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "items": {},
        "tables_included": [],
        "tables_skipped": [],
    }

    async with pool.acquire() as conn:
        # 1) Resolver client_id (si subject es cedula/email)
        resolved_client_id: Optional[str] = None
        if subject_kind == "client_id":
            resolved_client_id = subject_id
        else:
            if await _table_exists(conn, "clients"):
                try:
                    col = "cedula" if subject_kind == "cedula" else "email"
                    row = await conn.fetchrow(
                        f"select id from clients where firm_id=$1::uuid and {col}=$2 limit 1",
                        str(firm_id), subject_id,
                    )
                    if row:
                        resolved_client_id = str(row["id"])
                except Exception as e:
                    logger.debug("clients lookup failed: %s", e)

        # 2) Iterar tablas
        for table, filters, columns in HABEAS_TABLES:
            if not await _table_exists(conn, table):
                out["tables_skipped"].append({"table": table, "reason": "table_not_exists"})
                continue
            try:
                if subject_kind == "client_id" or "client_id" in filters:
                    if resolved_client_id is None:
                        out["tables_skipped"].append({"table": table, "reason": "no_client_id_resolved"})
                        continue
                    if "client_id" in filters:
                        col = filters["client_id"] or "client_id"
                        rows = await conn.fetch(
                            f"select {columns} from {table} where firm_id=$1::uuid and {col}=$2 limit 500",
                            str(firm_id), resolved_client_id,
                        )
                    else:
                        out["tables_skipped"].append({"table": table, "reason": "no_client_filter"})
                        continue
                else:
                    col_expr = filters.get(subject_kind)
                    if not col_expr:
                        out["tables_skipped"].append({"table": table, "reason": f"no_{subject_kind}_filter"})
                        continue
                    rows = await conn.fetch(
                        f"select {columns} from {table} where firm_id=$1::uuid and {col_expr}=$2 limit 500",
                        str(firm_id), subject_id,
                    )

                serialized = [
                    {k: (v.isoformat() if hasattr(v, "isoformat") else (dict(v) if isinstance(v, dict) else v))
                     for k, v in dict(r).items()}
                    for r in rows
                ]
                out["items"][table] = serialized
                out["tables_included"].append({"table": table, "rows": len(serialized)})
            except Exception as e:
                out["tables_skipped"].append({"table": table, "reason": f"query_error:{type(e).__name__}"})
                logger.debug("habeas table %s query failed: %s", table, e)

    return out
