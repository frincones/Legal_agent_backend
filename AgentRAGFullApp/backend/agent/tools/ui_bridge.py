"""F1 · UI Bridge tools.

Estas tools NO modifican base de datos. Su único side-effect es emitir un
`_ui_command` que el voice relay convierte en evento `ui.command` y el
browser ejecuta vía UICommandBus (router.push, scroll, prefill, etc.).

Cada tool retorna {summary, _ui_command} donde:
  - summary: lo que el modelo ve (string corto para narrar al usuario)
  - _ui_command: lo que el browser ejecuta (action + payload)

El campo `_ui_command` es strippeado por voice.py antes de enviar al modelo,
así que el modelo no se confunde con metadata de UI.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Whitelist de paths permitidos para `ui_navigate` — evita deep-links
# arbitrarios fuera del producto.
ALLOWED_PATHS = {
    "/inicio",
    "/casos",
    "/casos/nuevo",
    "/clientes",
    "/clientes/nuevo",
    "/calendario",
    "/documentos",
    "/notificaciones",
    "/liquidacion",
    "/calc/prescripcion",
    "/calc/intereses",
    "/canvas",
    "/settings/despacho",
    "/settings/privacidad",
}

ALLOWED_FORMS = {
    "liquidacion", "prescripcion", "intereses",
    "new_matter", "new_client",
}

ALLOWED_TABS = {
    "Resumen", "Análisis IA", "Cronología", "Documentos",
    "Partes", "Notas", "Calendario",
}


def _ui(action: str, **payload) -> dict:
    """Helper para construir el shape del comando UI."""
    return {"action": action, **payload}


# ─────────────────────────────────────────────────────────────────────


async def ui_navigate_tool(args: dict, ctx: dict) -> dict:
    """Navegar a una ruta del producto. Solo paths whitelisted."""
    path = (args.get("path") or "").strip()
    if not path.startswith("/"):
        return {"error": "path debe empezar con '/'"}
    # Permitir también /casos/<uuid> y /casos/<uuid>/canvas + /clientes/<uuid>
    base = path.split("?")[0].rstrip("/")
    is_allowed = (
        base in ALLOWED_PATHS
        or base.startswith("/casos/")
        or base.startswith("/clientes/")
    )
    if not is_allowed:
        return {"error": f"path '{path}' no está permitido"}
    return {
        "summary": f"Navegando a {path}",
        "_ui_command": _ui("navigate", path=path),
    }


async def ui_open_matter_canvas_tool(args: dict, ctx: dict) -> dict:
    """Abrir el Live Canvas de un caso específico."""
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not matter_id:
        return {"error": "matter_id requerido"}
    path = f"/casos/{matter_id}/canvas"
    return {
        "summary": f"Abriendo Canvas del caso {matter_id}",
        "_ui_command": _ui("navigate", path=path),
    }


async def ui_open_matter_tab_tool(args: dict, ctx: dict) -> dict:
    """Abrir el detalle del caso y seleccionar una pestaña específica.

    Las pestañas son client-side (estado en MatterTabs); navegamos al matter
    y emitimos un segundo comando 'select_tab' que el frontend escucha.
    """
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    tab = args.get("tab") or "Resumen"
    if not matter_id:
        return {"error": "matter_id requerido"}
    if tab not in ALLOWED_TABS:
        return {"error": f"tab '{tab}' inválida. Opciones: {sorted(ALLOWED_TABS)}"}
    return {
        "summary": f"Abriendo pestaña '{tab}' del caso {matter_id}",
        "_ui_command": _ui("open_matter_tab", matter_id=matter_id, tab=tab),
    }


async def ui_scroll_to_tool(args: dict, ctx: dict) -> dict:
    """Hacer scroll a un elemento por su data-scroll-target (selector estable)."""
    target = (args.get("target") or "").strip()
    if not target or len(target) > 60 or any(c in target for c in '<>"\''):
        return {"error": "target inválido"}
    return {
        "summary": f"Mostrando sección '{target}'",
        "_ui_command": _ui("scroll_to", target=target),
    }


async def ui_open_command_palette_tool(args: dict, ctx: dict) -> dict:
    """Abrir el Command Palette (⌘K)."""
    initial_query = args.get("initial_query") or ""
    return {
        "summary": "Abriendo buscador",
        "_ui_command": _ui("open_command_palette", initial_query=initial_query),
    }


async def ui_prefill_form_tool(args: dict, ctx: dict) -> dict:
    """Pre-llenar un formulario de la app con valores dictados por voz.

    El frontend tiene un registry de forms (UICommandBus). Cada form expone
    una API `setValues(partial)`. Si `submit=true`, dispara el submit tras
    rellenar.
    """
    form_name = (args.get("form") or "").strip()
    values = args.get("values") or {}
    submit = bool(args.get("submit") or False)
    if form_name not in ALLOWED_FORMS:
        return {"error": f"form '{form_name}' inválido. Opciones: {sorted(ALLOWED_FORMS)}"}
    if not isinstance(values, dict):
        return {"error": "values debe ser objeto JSON"}
    return {
        "summary": f"Llenando formulario {form_name} con {len(values)} valor(es)",
        "_ui_command": _ui("prefill_form", form=form_name, values=values, submit=submit),
    }


async def ui_show_toast_tool(args: dict, ctx: dict) -> dict:
    """Mostrar un toast notification al usuario."""
    message = (args.get("message") or "").strip()
    variant = args.get("variant") or "info"
    if not message:
        return {"error": "message requerido"}
    if variant not in ("info", "success", "warning", "error"):
        variant = "info"
    return {
        "summary": f"Toast: {message[:60]}",
        "_ui_command": _ui("toast", message=message, variant=variant),
    }


async def ui_open_modal_tool(args: dict, ctx: dict) -> dict:
    """Abrir un modal de confirmación con título y body."""
    title = (args.get("title") or "Confirmación").strip()
    body = (args.get("body") or "").strip()
    confirm_label = args.get("confirm_label") or "Aceptar"
    cancel_label = args.get("cancel_label") or "Cancelar"
    return {
        "summary": f"Modal: {title}",
        "_ui_command": _ui(
            "open_modal",
            title=title,
            body=body,
            confirm_label=confirm_label,
            cancel_label=cancel_label,
        ),
    }
