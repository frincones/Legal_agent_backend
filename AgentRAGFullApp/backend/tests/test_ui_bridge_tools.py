"""F1 · Tests para las 8 ui_* tools.

Verifican shape del _ui_command y validación de inputs.
"""

from __future__ import annotations

import asyncio
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.tools.ui_bridge import (  # noqa: E402
    ui_navigate_tool, ui_open_matter_canvas_tool, ui_open_matter_tab_tool,
    ui_scroll_to_tool, ui_open_command_palette_tool, ui_prefill_form_tool,
    ui_show_toast_tool, ui_open_modal_tool,
)


async def case_navigate_ok():
    r = await ui_navigate_tool({"path": "/calendario"}, {"firm_id": "x"})
    assert r["_ui_command"] == {"action": "navigate", "path": "/calendario"}, r


async def case_navigate_matter_uuid_ok():
    r = await ui_navigate_tool({"path": "/casos/abc-uuid/canvas"}, {"firm_id": "x"})
    assert r["_ui_command"]["action"] == "navigate"


async def case_navigate_disallowed():
    r = await ui_navigate_tool({"path": "/admin/eval"}, {"firm_id": "x"})
    assert "error" in r, r


async def case_navigate_external_blocked():
    r = await ui_navigate_tool({"path": "https://evil.com"}, {"firm_id": "x"})
    assert "error" in r, r


async def case_open_matter_canvas_default_ctx():
    r = await ui_open_matter_canvas_tool({}, {"matter_id": "m-1"})
    assert "/casos/m-1/canvas" in r["_ui_command"]["path"], r


async def case_open_matter_canvas_explicit():
    r = await ui_open_matter_canvas_tool({"matter_id": "m-2"}, {})
    assert r["_ui_command"]["path"] == "/casos/m-2/canvas", r


async def case_open_matter_canvas_missing():
    r = await ui_open_matter_canvas_tool({}, {})
    assert "error" in r


async def case_open_matter_tab_ok():
    r = await ui_open_matter_tab_tool({"matter_id": "m-1", "tab": "Documentos"}, {})
    assert r["_ui_command"]["action"] == "open_matter_tab"
    assert r["_ui_command"]["tab"] == "Documentos"


async def case_open_matter_tab_invalid():
    r = await ui_open_matter_tab_tool({"matter_id": "m-1", "tab": "Hackeo"}, {})
    assert "error" in r


async def case_scroll_to_ok():
    r = await ui_scroll_to_tool({"target": "documentos"}, {})
    assert r["_ui_command"]["action"] == "scroll_to"


async def case_scroll_to_xss_blocked():
    r = await ui_scroll_to_tool({"target": "<script>alert(1)</script>"}, {})
    assert "error" in r


async def case_open_command_palette():
    r = await ui_open_command_palette_tool({"initial_query": "rodriguez"}, {})
    assert r["_ui_command"]["action"] == "open_command_palette"
    assert r["_ui_command"]["initial_query"] == "rodriguez"


async def case_prefill_liquidacion():
    r = await ui_prefill_form_tool({
        "form": "liquidacion",
        "values": {"salarioMensual": 4_500_000, "fechaIngreso": "2019-01-15"},
    }, {})
    assert r["_ui_command"]["form"] == "liquidacion"
    assert r["_ui_command"]["values"]["salarioMensual"] == 4_500_000


async def case_prefill_invalid_form():
    r = await ui_prefill_form_tool({"form": "hack", "values": {}}, {})
    assert "error" in r


async def case_prefill_invalid_values():
    r = await ui_prefill_form_tool({"form": "liquidacion", "values": "string"}, {})
    assert "error" in r


async def case_show_toast_ok():
    r = await ui_show_toast_tool({"message": "Listo", "variant": "success"}, {})
    assert r["_ui_command"]["action"] == "toast"
    assert r["_ui_command"]["variant"] == "success"


async def case_show_toast_invalid_variant_falls_back():
    r = await ui_show_toast_tool({"message": "Listo", "variant": "xyz"}, {})
    assert r["_ui_command"]["variant"] == "info"


async def case_open_modal():
    r = await ui_open_modal_tool({"title": "OK", "body": "Confirma"}, {})
    assert r["_ui_command"]["action"] == "open_modal"


async def main() -> int:
    cases = [
        ("navigate ok", case_navigate_ok),
        ("navigate matter uuid ok", case_navigate_matter_uuid_ok),
        ("navigate disallowed", case_navigate_disallowed),
        ("navigate external blocked", case_navigate_external_blocked),
        ("open canvas default ctx", case_open_matter_canvas_default_ctx),
        ("open canvas explicit", case_open_matter_canvas_explicit),
        ("open canvas missing matter_id", case_open_matter_canvas_missing),
        ("open tab ok", case_open_matter_tab_ok),
        ("open tab invalid", case_open_matter_tab_invalid),
        ("scroll ok", case_scroll_to_ok),
        ("scroll xss blocked", case_scroll_to_xss_blocked),
        ("cmdk", case_open_command_palette),
        ("prefill liquidacion", case_prefill_liquidacion),
        ("prefill invalid form", case_prefill_invalid_form),
        ("prefill invalid values", case_prefill_invalid_values),
        ("toast ok", case_show_toast_ok),
        ("toast invalid variant", case_show_toast_invalid_variant_falls_back),
        ("modal", case_open_modal),
    ]
    fails = 0
    for name, fn in cases:
        try:
            await fn()
            print(f"[ OK ] {name}")
        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            fails += 1
        except Exception as e:
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            fails += 1
    print(f"\n{len(cases) - fails}/{len(cases)} OK ({fails} failed)")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
