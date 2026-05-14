"""Sprint 19 · Intake form validators.

Cada `intake_form.fields` es un JSON array así:
  [
    {"id": "nombre", "label": "Nombre completo", "kind": "text",
     "required": true, "placeholder": "...", "max_length": 120},
    {"id": "email", "label": "Email", "kind": "email", "required": true},
    {"id": "telefono", "label": "Teléfono", "kind": "phone", "required": false},
    {"id": "materia", "label": "Tipo de caso", "kind": "select",
     "required": true, "options": ["Laboral", "Civil", "Familia"]},
    {"id": "descripcion", "label": "Cuéntanos", "kind": "textarea",
     "required": true, "max_length": 2000},
    {"id": "cuantia", "label": "Cuantía aproximada (COP)", "kind": "number",
     "required": false, "min": 0},
    {"id": "consent", "label": "Acepto Habeas Data", "kind": "checkbox",
     "required": true},
  ]

Validation:
  - required: si falta o vacío → error
  - kind: email/phone/number/checkbox/select tienen reglas extras
  - max_length cap (defensa contra payloads enormes)
  - select: value debe estar en options
  - honeypot: campo trampa · si llega cualquier valor, marcar como spam
"""

from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[+0-9()\s\-]{7,20}$")
MAX_TEXT_DEFAULT = 5000
MAX_SELECT_OPTIONS = 200


VALID_KINDS = {
    "text", "email", "phone", "textarea", "number", "select",
    "checkbox", "radio", "date",
}


def validate_field_schema(fields: list) -> list[str]:
    """Verifica que el array de fields esté bien formado (admin saving form)."""
    errors: list[str] = []
    if not isinstance(fields, list):
        return ["fields debe ser una lista"]
    ids_seen: set[str] = set()
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            errors.append(f"field {i}: debe ser objeto")
            continue
        fid = (f.get("id") or "").strip()
        if not fid:
            errors.append(f"field {i}: id requerido")
            continue
        if fid in ids_seen:
            errors.append(f"field {i}: id duplicado '{fid}'")
            continue
        ids_seen.add(fid)
        if not re.fullmatch(r"[a-z0-9_\-]+", fid):
            errors.append(f"field '{fid}': solo lowercase, dígitos, _ y -")
        if not (f.get("label") or "").strip():
            errors.append(f"field '{fid}': label requerido")
        kind = (f.get("kind") or "").strip()
        if kind not in VALID_KINDS:
            errors.append(f"field '{fid}': kind inválido (válidos: {sorted(VALID_KINDS)})")
        if kind in ("select", "radio"):
            opts = f.get("options") or []
            if not isinstance(opts, list) or len(opts) == 0:
                errors.append(f"field '{fid}': options requerido para {kind}")
            elif len(opts) > MAX_SELECT_OPTIONS:
                errors.append(f"field '{fid}': demasiadas opciones (max {MAX_SELECT_OPTIONS})")
    return errors


def validate_submission(fields: list, payload: dict, honeypot_field: str | None = None) -> dict:
    """Valida una submission contra el schema. Devuelve:
      { errors: [...], cleaned: {...}, is_spam: bool, extracted_meta: {...} }
    """
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    # Honeypot: si trae valor, marca spam pero no muestra error (engaña al bot)
    is_spam = False
    if honeypot_field and payload.get(honeypot_field):
        is_spam = True

    submitter_email: str | None = None
    submitter_nombre: str | None = None
    submitter_phone: str | None = None

    by_id: dict[str, dict] = {(f.get("id") or ""): f for f in (fields or []) if isinstance(f, dict)}

    # Field-by-field validation
    for fid, f in by_id.items():
        if not fid:
            continue
        kind = (f.get("kind") or "text").strip()
        required = bool(f.get("required"))
        raw = payload.get(fid)

        # Empty check (treating None / "" / "  " as empty)
        empty = raw is None or (isinstance(raw, str) and not raw.strip())
        if required and empty and kind != "checkbox":
            errors.append(f"{f.get('label', fid)} es obligatorio")
            continue
        if empty:
            cleaned[fid] = None
            continue

        if kind in ("text", "textarea"):
            s = str(raw).strip()
            max_len = int(f.get("max_length", MAX_TEXT_DEFAULT))
            if len(s) > max_len:
                errors.append(f"{f.get('label', fid)}: máximo {max_len} caracteres")
                continue
            cleaned[fid] = s
            # Heurística para extraer "nombre"
            if fid.lower() in ("nombre", "name", "full_name", "nombre_completo"):
                submitter_nombre = s

        elif kind == "email":
            s = str(raw).strip().lower()
            if not EMAIL_RE.match(s):
                errors.append(f"{f.get('label', fid)} no parece un email válido")
                continue
            cleaned[fid] = s
            submitter_email = s

        elif kind == "phone":
            s = str(raw).strip()
            if not PHONE_RE.match(s):
                errors.append(f"{f.get('label', fid)} no parece un teléfono válido")
                continue
            cleaned[fid] = s
            submitter_phone = s

        elif kind == "number":
            try:
                n = float(raw)
            except (TypeError, ValueError):
                errors.append(f"{f.get('label', fid)} debe ser numérico")
                continue
            if "min" in f and n < float(f["min"]):
                errors.append(f"{f.get('label', fid)} debe ser ≥ {f['min']}")
                continue
            if "max" in f and n > float(f["max"]):
                errors.append(f"{f.get('label', fid)} debe ser ≤ {f['max']}")
                continue
            cleaned[fid] = n

        elif kind == "checkbox":
            cleaned[fid] = bool(raw)
            if required and not cleaned[fid]:
                errors.append(f"{f.get('label', fid)} es obligatorio")

        elif kind in ("select", "radio"):
            s = str(raw).strip()
            options = f.get("options") or []
            if s not in options:
                errors.append(f"{f.get('label', fid)}: opción inválida")
                continue
            cleaned[fid] = s

        elif kind == "date":
            cleaned[fid] = str(raw).strip()

        else:
            cleaned[fid] = str(raw)[:MAX_TEXT_DEFAULT]

    return {
        "errors": errors,
        "cleaned": cleaned,
        "is_spam": is_spam,
        "submitter_email": submitter_email,
        "submitter_nombre": submitter_nombre,
        "submitter_phone": submitter_phone,
    }
