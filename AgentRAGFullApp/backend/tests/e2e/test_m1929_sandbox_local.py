"""Sprint M19.29 · Tests LOCALES del sandbox (sin Claude API, sin Supabase).

Valida el 80% del camino B sin networking:
  - node + docx@9.5 funcionan
  - sandbox genera .docx válido desde JS hardcoded
  - feature flag toggle
  - extractor de bloque ```javascript del output Claude
  - validaciones de seguridad (JS muy grande, marker reservado, etc.)

El test del pipeline completo (con Claude) requiere ANTHROPIC_API_KEY y se
corre por separado contra el deploy.

Uso:
    pytest -xvs tests/e2e/test_m1929_sandbox_local.py

Env vars opcionales:
    LEXAI_NODE_PATH_OVERRIDE     (default /opt/docx-runtime/node_modules)
                                  En Windows local: c:/tmp/docx-runtime/node_modules
"""

from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytestmark = [pytest.mark.asyncio]


# JS hardcoded de prueba (mínimo válido) — usa el wrapper del executor.
# El JS define build(data) que crea un Document con título y un párrafo.
HARDCODED_VALID_JS = r"""
async function build(data) {
  return new Document({
    creator: 'LexAI',
    title: data.title || 'Documento de prueba',
    sections: [{
      properties: {
        page: {
          size: {
            width: convertInchesToTwip(8.5),
            height: convertInchesToTwip(11),
            orientation: PageOrientation.PORTRAIT,
          },
          margin: {
            top: convertInchesToTwip(1),
            bottom: convertInchesToTwip(1),
            left: convertInchesToTwip(1),
            right: convertInchesToTwip(1),
          },
        },
      },
      children: [
        new Paragraph({
          heading: HeadingLevel.HEADING_1,
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({
              text: (data.title || 'PODER ESPECIAL').toUpperCase(),
              bold: true,
              font: 'Arial',
              size: 28,
            }),
          ],
        }),
        new Paragraph({
          alignment: AlignmentType.JUSTIFIED,
          indent: { firstLine: 720 },
          children: [
            new TextRun({
              text: 'Yo, ' + (data.nombre || '[NOMBRE]') +
                    ', mayor de edad, identificado con cédula de ciudadanía No. ' +
                    (data.cc || '[CC]') +
                    ', confiero PODER ESPECIAL al señor(a) ' +
                    (data.apoderado || '[APODERADO]') + '.',
              font: 'Arial',
              size: 22,
            }),
          ],
        }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          spacing: { before: 480 },
          children: [
            new TextRun({
              text: 'En ' + (data.ciudad || 'Bogotá') + ', a los ' +
                    (data.fecha || '28 de mayo de 2026') + '.',
              font: 'Arial',
              size: 22,
              italics: true,
            }),
          ],
        }),
      ],
    }],
  });
}
"""

HARDCODED_BROKEN_JS = r"""
async function build(data) {
  return new Document({
    sections: [{ children: [
      new Paragraph({ children: [ new TextRun({ text: 'unterminated string  // syntax error
"""

HARDCODED_NO_BUILD_JS = r"""
const x = 42;
// No build function defined
"""


def _node_path() -> str:
    """Path al node_modules con docx@9.5 instalado.

    Default Docker: /opt/docx-runtime/node_modules
    Local Windows: c:/tmp/docx-runtime/node_modules
    """
    override = os.getenv("LEXAI_NODE_PATH_OVERRIDE", "").strip()
    if override:
        return override
    candidates = [
        "/opt/docx-runtime/node_modules",
        "c:/tmp/docx-runtime/node_modules",
        os.path.expanduser("~/tmp/docx-runtime/node_modules"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]  # default Docker, fallará si no existe


# ============================================================================
# Tests sin networking (sandbox + helpers)
# ============================================================================


@pytest.mark.asyncio
async def test_health_check_node_and_docx():
    """Verifica que node y docx-js están disponibles."""
    from lex.renderer.node_executor import health_check_sync
    # En local, fix NODE_PATH antes de llamar
    os.environ["NODE_PATH"] = _node_path()
    h = health_check_sync()
    assert h.get("node_ok"), f"node missing: {h}"
    assert h.get("docx_ok"), f"docx missing: {h}. Run: cd c:/tmp/docx-runtime && npm install docx@9.5.1"
    print(f"[runtime] node={h.get('node_version')} docx={h.get('docx_version')}")


@pytest.mark.asyncio
async def test_sandbox_generates_docx_from_hardcoded_js():
    """Camino B sin Claude: JS hardcoded → sandbox → .docx válido."""
    from lex.renderer.node_executor import execute_docx_js

    data = {
        "title": "Poder Especial Notarial",
        "nombre": "Juan Pérez García",
        "cc": "1.000.000",
        "apoderado": "María López",
        "ciudad": "Bogotá",
        "fecha": "28 de mayo de 2026",
    }
    result = await execute_docx_js(
        HARDCODED_VALID_JS, data,
        timeout_s=30,
        node_path=_node_path(),
    )

    # Validar bytes
    assert result.docx_bytes, "docx vacío"
    assert len(result.docx_bytes) >= 1000, f"docx muy pequeño: {len(result.docx_bytes)}"
    assert result.docx_bytes.startswith(b"PK\x03\x04"), "no es zip"
    assert len(result.sha256) == 64, "sha256 mal formado"
    assert result.duration_ms > 0
    assert "LEXAI_OK" in result.stdout

    # Validar zip válido + contiene word/document.xml + texto sustituido
    with zipfile.ZipFile(io.BytesIO(result.docx_bytes), "r") as zf:
        names = zf.namelist()
        assert "word/document.xml" in names
        doc_xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
        assert "Juan Pérez García" in doc_xml or "Juan P" in doc_xml, "texto sustituido no aparece"
        assert "PODER ESPECIAL NOTARIAL" in doc_xml or "PODER" in doc_xml, "título no aparece"
        assert "Bogotá" in doc_xml or "Bogot" in doc_xml

    print(
        f"[sandbox OK] bytes={len(result.docx_bytes)} sha256={result.sha256[:12]} "
        f"duration_ms={result.duration_ms}"
    )


@pytest.mark.asyncio
async def test_sandbox_rejects_syntax_error():
    """JS roto → kind='runtime' o 'syntax'."""
    from lex.renderer.node_executor import execute_docx_js, NodeExecutionError

    with pytest.raises(NodeExecutionError) as ei:
        await execute_docx_js(HARDCODED_BROKEN_JS, {}, timeout_s=10, node_path=_node_path())
    assert ei.value.kind in ("runtime", "syntax", "io"), f"unexpected kind: {ei.value.kind}"
    print(f"[broken JS OK] kind={ei.value.kind} msg={str(ei.value)[:80]}")


@pytest.mark.asyncio
async def test_sandbox_rejects_no_build_function():
    """JS sin build() → kind='runtime' con mensaje LEXAI_NO_BUILD_FUNCTION."""
    from lex.renderer.node_executor import execute_docx_js, NodeExecutionError

    with pytest.raises(NodeExecutionError) as ei:
        await execute_docx_js(HARDCODED_NO_BUILD_JS, {}, timeout_s=10, node_path=_node_path())
    assert ei.value.kind == "runtime"
    assert "NO_BUILD_FUNCTION" in (ei.value.stderr or str(ei.value))
    print(f"[no build OK] kind={ei.value.kind}")


@pytest.mark.asyncio
async def test_sandbox_rejects_oversized_js():
    """JS > 256KB rechazado antes de spawn."""
    from lex.renderer.node_executor import execute_docx_js, NodeExecutionError, MAX_JS_BYTES

    big_js = HARDCODED_VALID_JS + ("//" + "x" * (MAX_JS_BYTES + 100) + "\n")
    with pytest.raises(NodeExecutionError) as ei:
        await execute_docx_js(big_js, {}, timeout_s=5, node_path=_node_path())
    assert ei.value.kind == "syntax"
    print(f"[oversize OK] kind={ei.value.kind}")


@pytest.mark.asyncio
async def test_sandbox_rejects_reserved_marker():
    """JS con marcador reservado __USER_JS_PLACEHOLDER__ rechazado."""
    from lex.renderer.node_executor import execute_docx_js, NodeExecutionError

    bad_js = "async function build(data) { /* __USER_JS_PLACEHOLDER__ */ return new Document({sections:[]}); }"
    with pytest.raises(NodeExecutionError) as ei:
        await execute_docx_js(bad_js, {}, timeout_s=5, node_path=_node_path())
    assert ei.value.kind == "syntax"
    print(f"[marker OK] kind={ei.value.kind}")


# ============================================================================
# Tests del feature flag y helpers
# ============================================================================


@pytest.mark.asyncio
async def test_flag_disabled_returns_false():
    from lex.renderer.claude_docx_renderer import is_renderer_enabled_for
    os.environ["CLAUDE_RENDERER_ENABLED"] = "false"
    assert not is_renderer_enabled_for(doc_type="x", doc_family="notarial", firm_id=None)
    os.environ["CLAUDE_RENDERER_ENABLED"] = "0"
    assert not is_renderer_enabled_for(doc_type="x", doc_family="notarial", firm_id=None)


@pytest.mark.asyncio
async def test_flag_enabled_returns_true():
    from lex.renderer.claude_docx_renderer import is_renderer_enabled_for
    os.environ["CLAUDE_RENDERER_ENABLED"] = "true"
    os.environ.pop("CLAUDE_RENDERER_DOC_FAMILIES", None)
    os.environ.pop("CLAUDE_RENDERER_FIRM_IDS", None)
    assert is_renderer_enabled_for(doc_type="poder", doc_family="notarial", firm_id="firm-abc")


@pytest.mark.asyncio
async def test_flag_filters_by_doc_family():
    from lex.renderer.claude_docx_renderer import is_renderer_enabled_for
    os.environ["CLAUDE_RENDERER_ENABLED"] = "true"
    os.environ["CLAUDE_RENDERER_DOC_FAMILIES"] = "notarial,contractual"
    assert is_renderer_enabled_for(doc_type="x", doc_family="notarial", firm_id=None)
    assert is_renderer_enabled_for(doc_type="x", doc_family="contractual", firm_id=None)
    assert not is_renderer_enabled_for(doc_type="x", doc_family="judicial", firm_id=None)
    os.environ.pop("CLAUDE_RENDERER_DOC_FAMILIES", None)


@pytest.mark.asyncio
async def test_flag_filters_by_firm_id():
    from lex.renderer.claude_docx_renderer import is_renderer_enabled_for
    os.environ["CLAUDE_RENDERER_ENABLED"] = "true"
    os.environ["CLAUDE_RENDERER_FIRM_IDS"] = "firm-alpha,firm-beta"
    assert is_renderer_enabled_for(doc_type="x", doc_family="notarial", firm_id="firm-alpha")
    assert not is_renderer_enabled_for(doc_type="x", doc_family="notarial", firm_id="firm-zulu")
    os.environ.pop("CLAUDE_RENDERER_FIRM_IDS", None)


@pytest.mark.asyncio
async def test_extract_js_block_from_fenced_output():
    """El extractor saca el JS de un output Claude con fences."""
    from lex.renderer.claude_docx_renderer import _extract_js_block

    samples = [
        ("```javascript\nasync function build(data){return new Document({});}\n```", "build(data)"),
        ("```js\nasync function build(d){}\n```", "build(d)"),
        ("Aquí está el código:\n```javascript\nasync function build(data){}\n```\nGracias.", "build(data)"),
        ("async function build(data) { return new Document({}); }", "build(data)"),
    ]
    for raw, expected_substr in samples:
        out = _extract_js_block(raw)
        assert expected_substr in out, f"failed for {raw[:40]!r}: got {out[:80]!r}"


@pytest.mark.asyncio
async def test_builtin_skill_md_loads():
    """El built-in docx SKILL.md existe y se carga via get_builtin_skill_md()."""
    from lex.renderer.claude_docx_renderer import get_builtin_skill_md
    md = get_builtin_skill_md()
    assert "Skill: docx" in md or "docx" in md.lower()
    assert "async function build(data)" in md
    assert len(md) > 1000
    print(f"[skill.md] {len(md)} bytes loaded")
