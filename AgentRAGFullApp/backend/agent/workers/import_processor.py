"""Sprint 13 · CSV import processor.

Procesa un import_job:
  1. Lee import_rows en status 'pending'
  2. Para cada fila, valida + parsea según kind
  3. Si validate-only (dry_run), solo marca status='ok'/'error'/'duplicate'
  4. Si commit, inserta el registro real en la tabla destino y guarda created_id

Soporta kinds: clients, matters, time_entries, expenses, leads, contacts.

Mapeo de columnas: column_mapping en el job define {csv_col: target_field}.
Si no se especifica, se hace mapeo heurístico por nombre.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Field maps por kind (target_field → tipo/required)
# ──────────────────────────────────────────────────────────────────────


CLIENTS_FIELDS = {
    "nombre": {"required": True, "type": "str"},
    "tipo": {"required": False, "type": "str", "default": "natural"},
    "tax_id": {"required": False, "type": "str"},
    "personal_id": {"required": False, "type": "str"},
    "email": {"required": False, "type": "str"},
    "telefono": {"required": False, "type": "str"},
    "domicilio": {"required": False, "type": "str"},
}

MATTERS_FIELDS = {
    "titulo": {"required": True, "type": "str"},
    "client_tax_id": {"required": False, "type": "str", "alias": "client"},
    "client_nombre": {"required": False, "type": "str"},
    "materia": {"required": True, "type": "str"},
    "expediente": {"required": False, "type": "str"},
    "juzgado": {"required": False, "type": "str"},
    "etapa_procesal": {"required": False, "type": "str"},
    "status": {"required": False, "type": "str", "default": "activo"},
    "priority": {"required": False, "type": "str", "default": "media"},
    "cuantia": {"required": False, "type": "decimal"},
}

LEADS_FIELDS = {
    "nombre": {"required": True, "type": "str"},
    "email": {"required": False, "type": "str"},
    "telefono": {"required": False, "type": "str"},
    "source": {"required": False, "type": "str"},
    "materia": {"required": False, "type": "str"},
    "estimated_value_cop": {"required": False, "type": "decimal"},
    "notes": {"required": False, "type": "str"},
}

TIME_ENTRIES_FIELDS = {
    "matter_expediente": {"required": True, "type": "str"},
    "user_email": {"required": False, "type": "str"},
    "duration_min": {"required": True, "type": "int"},
    "description": {"required": False, "type": "str"},
    "occurred_on": {"required": False, "type": "date"},
    "rate_cop": {"required": False, "type": "decimal"},
    "billable": {"required": False, "type": "bool", "default": True},
}

EXPENSES_FIELDS = {
    "matter_expediente": {"required": True, "type": "str"},
    "kind": {"required": True, "type": "str"},
    "amount_cop": {"required": True, "type": "decimal"},
    "occurred_on": {"required": False, "type": "date"},
    "description": {"required": False, "type": "str"},
    "billable": {"required": False, "type": "bool", "default": True},
}

FIELDS_BY_KIND = {
    "clients": CLIENTS_FIELDS,
    "matters": MATTERS_FIELDS,
    "leads": LEADS_FIELDS,
    "time_entries": TIME_ENTRIES_FIELDS,
    "expenses": EXPENSES_FIELDS,
}


def _coerce(value: Any, type_: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    s = str(value).strip()
    if type_ == "str":
        return s
    if type_ == "int":
        try:
            return int(float(s.replace(",", "")))
        except Exception:
            raise ValueError(f"no es entero: {s!r}")
    if type_ == "decimal":
        try:
            cleaned = s.replace("$", "").replace(" ", "")
            if "," in cleaned and "." in cleaned:
                if cleaned.rfind(",") > cleaned.rfind("."):
                    cleaned = cleaned.replace(".", "").replace(",", ".")
                else:
                    cleaned = cleaned.replace(",", "")
            elif "," in cleaned:
                cleaned = cleaned.replace(",", ".") if cleaned.count(",") == 1 and len(cleaned.split(",")[-1]) <= 2 else cleaned.replace(",", "")
            return float(cleaned)
        except Exception:
            raise ValueError(f"no es número: {s!r}")
    if type_ == "bool":
        return s.lower() in ("1", "true", "si", "sí", "yes", "y", "x")
    if type_ == "date":
        from datetime import datetime
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except Exception:
                continue
        raise ValueError(f"fecha inválida: {s!r}")
    return s


def _normalize_header(h: str) -> str:
    h = h.strip().lower()
    return (
        h.replace(" ", "_").replace("-", "_")
         .replace("á", "a").replace("é", "e").replace("í", "i")
         .replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    )


HEURISTIC_MAP = {
    # generic
    "nombre": "nombre", "name": "nombre", "razon_social": "nombre",
    "email": "email", "correo": "email", "correo_electronico": "email",
    "telefono": "telefono", "phone": "telefono", "celular": "telefono",
    "tax_id": "tax_id", "nit": "tax_id", "rut": "tax_id",
    "personal_id": "personal_id", "cedula": "personal_id", "cc": "personal_id",
    "domicilio": "domicilio", "direccion": "domicilio", "address": "domicilio",
    "tipo": "tipo",
    # matter
    "titulo": "titulo", "title": "titulo", "asunto": "titulo",
    "materia": "materia", "area": "materia",
    "expediente": "expediente", "exp": "expediente", "radicado": "expediente",
    "juzgado": "juzgado", "tribunal": "juzgado",
    "etapa": "etapa_procesal", "etapa_procesal": "etapa_procesal",
    "status": "status", "estado": "status",
    "cuantia": "cuantia", "monto": "cuantia",
    "client_tax_id": "client_tax_id", "cliente_nit": "client_tax_id",
    "client_nombre": "client_nombre", "cliente_nombre": "client_nombre", "cliente": "client_nombre",
    # leads
    "source": "source", "fuente": "source",
    "estimated_value_cop": "estimated_value_cop", "valor_estimado": "estimated_value_cop",
    "notes": "notes", "notas": "notes",
    # time/expenses
    "matter_expediente": "matter_expediente",
    "user_email": "user_email", "abogado_email": "user_email",
    "duration_min": "duration_min", "minutos": "duration_min", "minutes": "duration_min",
    "description": "description", "descripcion": "description",
    "occurred_on": "occurred_on", "fecha": "occurred_on", "date": "occurred_on",
    "rate_cop": "rate_cop", "tarifa": "rate_cop",
    "amount_cop": "amount_cop", "monto_cop": "amount_cop", "valor": "amount_cop",
    "kind": "kind", "tipo_gasto": "kind",
    "billable": "billable", "facturable": "billable",
}


def heuristic_column_mapping(headers: list[str], target_fields: dict) -> dict:
    mapping = {}
    for h in headers:
        n = _normalize_header(h)
        if n in target_fields:
            mapping[h] = n
        elif n in HEURISTIC_MAP and HEURISTIC_MAP[n] in target_fields:
            mapping[h] = HEURISTIC_MAP[n]
    return mapping


# ──────────────────────────────────────────────────────────────────────
# Process
# ──────────────────────────────────────────────────────────────────────


async def process_job(job_id: str, commit: bool = False) -> dict:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage unavailable"}

    async with storage.pool.acquire() as conn:
        job = await conn.fetchrow(
            "select id, firm_id, kind, column_mapping, options, status from import_jobs where id = $1::uuid",
            job_id,
        )
        if not job:
            return {"error": "job no encontrado"}
        if job["status"] in ("committed", "canceled"):
            return {"error": f"job en estado terminal: {job['status']}"}

        target_fields = FIELDS_BY_KIND.get(job["kind"])
        if not target_fields:
            return {"error": f"kind no soportado: {job['kind']}"}

        await conn.execute(
            "update import_jobs set status = $2, started_at = coalesce(started_at, now()) where id = $1::uuid",
            job_id, "committing" if commit else "validating",
        )

        # Cargar mapping (manual o heurístico desde primera fila) ·
        # asyncpg puede devolver JSONB como string · normalizar.
        cm_raw = job["column_mapping"]
        if isinstance(cm_raw, str):
            try:
                cm_raw = json.loads(cm_raw)
            except Exception:
                cm_raw = {}
        mapping = dict(cm_raw or {})
        rows = await conn.fetch(
            """
            select id, line_number, raw_payload from import_rows
             where import_job_id = $1::uuid and status = 'pending'
             order by line_number
            """,
            job_id,
        )
        if not rows:
            await conn.execute(
                "update import_jobs set status = 'validated', completed_at = now() where id = $1::uuid",
                job_id,
            )
            return {"ok": True, "rows_processed": 0}

        if not mapping:
            raw_payload = rows[0]["raw_payload"]
            if isinstance(raw_payload, str):
                try:
                    raw_payload = json.loads(raw_payload)
                except Exception:
                    raw_payload = {}
            headers = list((raw_payload or {}).keys())
            mapping = heuristic_column_mapping(headers, target_fields)
            await conn.execute(
                "update import_jobs set column_mapping = $2::jsonb where id = $1::uuid",
                job_id, json.dumps(mapping),
            )

    ok_count, error_count, dup_count = 0, 0, 0
    for r in rows:
        line_number = r["line_number"]
        raw = r["raw_payload"] or {}
        parsed: dict = {}
        warnings: list[str] = []
        try:
            for csv_col, target in mapping.items():
                spec = target_fields.get(target)
                if not spec:
                    continue
                value = raw.get(csv_col)
                parsed[target] = _coerce(value, spec.get("type", "str"))

            # Defaults + required
            for field, spec in target_fields.items():
                if "default" in spec and not parsed.get(field):
                    parsed[field] = spec["default"]
                if spec.get("required") and not parsed.get(field):
                    raise ValueError(f"falta campo requerido: {field}")

            # Insert real si commit
            created_id: Optional[str] = None
            if commit:
                created_id = await _insert_record(
                    str(job["firm_id"]), job["kind"], parsed,
                )
            await _mark_row(r["id"], 'ok', None, created_id, warnings)
            ok_count += 1
        except Exception as e:
            await _mark_row(r["id"], 'error', str(e)[:300], None, warnings)
            error_count += 1

    final_status = "committed" if commit else "validated"
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update import_jobs set
              status = $2, rows_ok = $3, rows_error = $4,
              completed_at = now()
             where id = $1::uuid
            """,
            job_id, final_status, ok_count, error_count,
        )

    return {
        "ok": True,
        "status": final_status,
        "rows_processed": len(rows),
        "rows_ok": ok_count,
        "rows_error": error_count,
        "rows_duplicate": dup_count,
    }


async def _mark_row(row_id, status: str, error: Optional[str], created_id: Optional[str], warnings: list[str]):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            update import_rows set
              status = $2, error = $3, created_id = $4::uuid, warnings = $5::jsonb
             where id = $1
            """,
            row_id, status, error, created_id, json.dumps(warnings),
        )


async def _insert_record(firm_id: str, kind: str, parsed: dict) -> Optional[str]:
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        if kind == "clients":
            row = await conn.fetchrow(
                """
                insert into clients (firm_id, tipo, nombre, tax_id, personal_id, email, telefono, domicilio)
                values ($1::uuid, $2, $3, $4, $5, $6, $7, $8) returning id
                """,
                firm_id, parsed.get("tipo", "natural"), parsed["nombre"],
                parsed.get("tax_id"), parsed.get("personal_id"),
                parsed.get("email"), parsed.get("telefono"), parsed.get("domicilio"),
            )
            return str(row["id"])
        if kind == "leads":
            row = await conn.fetchrow(
                """
                insert into leads (firm_id, nombre, email, telefono, source, materia, estimated_value_cop, notes)
                values ($1::uuid, $2, $3, $4, $5, $6, $7, $8) returning id
                """,
                firm_id, parsed["nombre"], parsed.get("email"), parsed.get("telefono"),
                parsed.get("source"), parsed.get("materia"),
                parsed.get("estimated_value_cop"), parsed.get("notes"),
            )
            return str(row["id"])
        if kind == "matters":
            # Resolver client
            client_id = None
            if parsed.get("client_tax_id"):
                client_id = await conn.fetchval(
                    "select id from clients where firm_id = $1::uuid and tax_id = $2",
                    firm_id, parsed["client_tax_id"],
                )
            if not client_id and parsed.get("client_nombre"):
                client_id = await conn.fetchval(
                    "select id from clients where firm_id = $1::uuid and nombre ilike $2 limit 1",
                    firm_id, parsed["client_nombre"],
                )
            if not client_id:
                raise ValueError(f"cliente no encontrado (tax_id={parsed.get('client_tax_id')} nombre={parsed.get('client_nombre')})")
            display_id = f"M-{firm_id[:6]}-{parsed.get('expediente', '')[:6]}"
            row = await conn.fetchrow(
                """
                insert into matters (firm_id, client_id, display_id, titulo, materia,
                                     expediente, juzgado, etapa_procesal, status, priority, cuantia)
                values ($1::uuid, $2::uuid, $3, $4, $5::materia_legal,
                        $6, $7, $8, $9, $10, $11)
                returning id
                """,
                firm_id, client_id, display_id, parsed["titulo"], parsed["materia"],
                parsed.get("expediente"), parsed.get("juzgado"), parsed.get("etapa_procesal"),
                parsed.get("status", "activo"), parsed.get("priority", "media"),
                parsed.get("cuantia"),
            )
            return str(row["id"])
        if kind == "time_entries":
            matter_id = await conn.fetchval(
                "select id from matters where firm_id = $1::uuid and expediente = $2",
                firm_id, parsed["matter_expediente"],
            )
            if not matter_id:
                raise ValueError(f"matter expediente no encontrado: {parsed['matter_expediente']}")
            user_id = None
            if parsed.get("user_email"):
                user_id = await conn.fetchval(
                    "select id from users where firm_id = $1::uuid and email = $2",
                    firm_id, parsed["user_email"],
                )
            if not user_id:
                raise ValueError("user_email no encontrado en la firma")
            mins = int(parsed.get("duration_min") or 0)
            if mins <= 0:
                raise ValueError("duration_min debe ser > 0")
            from datetime import datetime, timezone, timedelta
            occ = parsed.get("occurred_on") or datetime.now(timezone.utc).date().isoformat()
            row = await conn.fetchrow(
                """
                insert into time_entries
                  (firm_id, matter_id, user_id, started_at, ended_at,
                   billable, rate_cop, description, source)
                values ($1::uuid, $2::uuid, $3::uuid,
                        ($4::date - interval '1 minute' * $5)::timestamptz,
                        ($4::date)::timestamptz,
                        $6, $7, $8, 'manual')
                returning id
                """,
                firm_id, matter_id, user_id, occ, mins,
                bool(parsed.get("billable", True)),
                parsed.get("rate_cop"), parsed.get("description", ""),
            )
            return str(row["id"])
        if kind == "expenses":
            matter_id = await conn.fetchval(
                "select id from matters where firm_id = $1::uuid and expediente = $2",
                firm_id, parsed["matter_expediente"],
            )
            if not matter_id:
                raise ValueError(f"matter expediente no encontrado: {parsed['matter_expediente']}")
            from datetime import datetime, timezone
            occ = parsed.get("occurred_on") or datetime.now(timezone.utc).date().isoformat()
            row = await conn.fetchrow(
                """
                insert into expenses (firm_id, matter_id, kind, amount_cop, occurred_on, description, billable)
                values ($1::uuid, $2::uuid, $3, $4, $5::date, $6, $7) returning id
                """,
                firm_id, matter_id, parsed["kind"], parsed["amount_cop"],
                occ, parsed.get("description", ""), bool(parsed.get("billable", True)),
            )
            return str(row["id"])
    raise ValueError(f"kind no soportado: {kind}")
