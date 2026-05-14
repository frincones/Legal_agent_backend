"""Sprint 22 · Wizard helpers.

Renderer mustache-like + helpers de navegación de steps.

Sintaxis soportada:
  · {{var}}                    · interpolación
  · {{#var}}...{{/var}}        · sección condicional (renderea solo si truthy)
  · {{#var_es_X}}...{{/var_es_X}}  · pseudo-conditional: var_es_X = (answers[var] == 'X')
                                    convención: prefijo `<field>_es_<value>` donde value
                                    es slugify(option). Esto permite plantillas con
                                    select sin lógica compleja.

Validators:
  · validate_step_answers(step, answers)  → list[error]
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from typing import Any


# --------------------------------------------------------------------
# Slug + helpers
# --------------------------------------------------------------------
def slugify(value: Any) -> str:
    """Convierte 'Prima Media (Colpensiones)' → 'prima_media'."""
    if value is None:
        return ""
    s = str(value)
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def random_token(prefix: str = "wsess", length: int = 24) -> str:
    return f"{prefix}_{secrets.token_urlsafe(length)}"


# --------------------------------------------------------------------
# Mustache-like renderer
# --------------------------------------------------------------------
_VAR_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")
_SECTION_RE = re.compile(
    r"\{\{#([a-zA-Z_][a-zA-Z0-9_]*)\}\}([\s\S]*?)\{\{/\1\}\}",
    re.DOTALL,
)


def _enrich_context(answers: dict) -> dict:
    """Añade pseudo-vars `<field>_es_<slugvalue>` para conditionals.

    Ejemplo: answers["regimen"] = "Prima Media (Colpensiones)"
       → contexto incluye `regimen_es_prima_media = True`
    """
    enriched = dict(answers)
    for k, v in (answers or {}).items():
        if not isinstance(k, str):
            continue
        if isinstance(v, (str, int, float)) and v not in (None, ""):
            slug = slugify(v)
            if slug:
                enriched[f"{k}_es_{slug}"] = True
    return enriched


def _truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    if isinstance(v, (list, dict)) and len(v) == 0:
        return False
    return True


def render_template(template: str, answers: dict) -> str:
    """Renderiza la plantilla con los answers."""
    if not template:
        return ""
    ctx = _enrich_context(answers or {})

    # 1) Secciones condicionales (procesar primero para no romper interpolación interna)
    def section_repl(m: re.Match) -> str:
        name = m.group(1)
        body = m.group(2)
        if _truthy(ctx.get(name)):
            # render variables internas
            return _interp(body, ctx)
        return ""

    out = template
    # Loop hasta que no haya más secciones (soporta anidadas simples)
    prev = None
    while prev != out:
        prev = out
        out = _SECTION_RE.sub(section_repl, out)

    # 2) Variables planas
    out = _interp(out, ctx)
    # Limpieza: colapsar múltiples newlines vacíos
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + "\n"


def _interp(text: str, ctx: dict) -> str:
    """Reemplaza {{var}} por ctx[var] (o vacío si falta)."""
    def repl(m: re.Match) -> str:
        name = m.group(1)
        v = ctx.get(name)
        if v is None or v is False:
            return ""
        return str(v)
    return _VAR_RE.sub(repl, text)


# --------------------------------------------------------------------
# Step validation
# --------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[+0-9()\s\-]{7,20}$")


def validate_step_answers(step: dict, answers: dict) -> list[str]:
    """Valida los campos requeridos del step contra los answers actuales.
    Devuelve lista de errores en string."""
    errors: list[str] = []
    fields = (step or {}).get("fields") or []
    for f in fields:
        if not isinstance(f, dict):
            continue
        fid = (f.get("id") or "").strip()
        if not fid:
            continue
        kind = (f.get("kind") or "text").strip()
        required = bool(f.get("required"))
        v = (answers or {}).get(fid)

        empty = v is None or (isinstance(v, str) and not v.strip())
        if required and empty and kind != "checkbox":
            errors.append(f"{f.get('label', fid)} es obligatorio")
            continue
        if empty:
            continue

        if kind == "email":
            if not EMAIL_RE.match(str(v).strip()):
                errors.append(f"{f.get('label', fid)} debe ser un email válido")
        elif kind == "phone":
            if not PHONE_RE.match(str(v).strip()):
                errors.append(f"{f.get('label', fid)} debe ser un teléfono válido")
        elif kind == "number":
            try:
                n = float(v)
                if "min" in f and n < float(f["min"]):
                    errors.append(f"{f.get('label', fid)} debe ser ≥ {f['min']}")
                if "max" in f and n > float(f["max"]):
                    errors.append(f"{f.get('label', fid)} debe ser ≤ {f['max']}")
            except (ValueError, TypeError):
                errors.append(f"{f.get('label', fid)} debe ser numérico")
        elif kind == "select" or kind == "radio":
            opts = f.get("options") or []
            if str(v) not in opts:
                errors.append(f"{f.get('label', fid)}: opción inválida")
        elif kind == "date":
            # Acepta YYYY-MM-DD o cadena no-vacía
            pass
        elif kind == "checkbox":
            # nada
            pass
    return errors


def all_steps_completed(steps: list, completed_steps: list) -> bool:
    """¿Se completaron todos los step_ids?"""
    needed = {(s.get("id") or "") for s in (steps or []) if isinstance(s, dict)}
    needed.discard("")
    done = set(completed_steps or [])
    return needed.issubset(done)
