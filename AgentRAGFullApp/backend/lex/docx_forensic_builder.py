"""DocX Forensic Builder — imperativo a partir de bloques tipados.

Replica el patrón de Claude (Anthropic) con docx.js — pero en Python con python-docx.
Construye .docx con helpers de bajo nivel: p, p_mix, title, subtitle, hecho,
pretension, celda, table, juramento, firma.

WYSIWYG: mismo árbol de bloques que renderiza el canvas frontend.
"""
from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

FONT = "Times New Roman"
FONT_SIZE_PT = 12


def build_docx_from_blocks(
    blocks: list[dict[str, Any]],
    title: str = "Documento",
    author: str = "LexAI",
) -> bytes:
    """Construye .docx forense a partir de lista de bloques tipados."""
    from docx import Document
    from docx.shared import Cm, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    doc.core_properties.title = title
    doc.core_properties.author = author

    # Setup forense
    for section in doc.sections:
        section.top_margin = Cm(3)
        section.bottom_margin = Cm(3)
        section.left_margin = Cm(3)
        section.right_margin = Cm(2.5)

    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(FONT_SIZE_PT)
    pf = normal.paragraph_format
    pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    pf.line_spacing = 1.5
    pf.space_after = Pt(6)
    pf.first_line_indent = Cm(1.25)

    for lvl, size in [(1, 14), (2, 13), (3, 12)]:
        try:
            h = doc.styles[f"Heading {lvl}"]
            h.font.name = FONT
            h.font.size = Pt(size)
            h.font.bold = True
            h.font.color.rgb = RGBColor(0x00, 0x00, 0x00)
        except Exception:
            pass

    # Render bloques
    for b in blocks:
        bt = b.get("block_type") or b.get("block_data", {}).get("type")
        bd = b.get("block_data") or {}
        try:
            _render_block(doc, bt, bd)
        except Exception as e:
            logger.warning("render block %s failed: %s", bt, e)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _render_block(doc, btype: str, bd: dict[str, Any]) -> None:
    from docx.shared import Cm, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_ALIGN_VERTICAL

    if btype == "title":
        text = (bd.get("text") or "").upper()
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.space_after = Pt(18)
        run = p.add_run(text)
        run.bold = True
        run.font.size = Pt(16) if bd.get("level", 1) <= 1 else Pt(14)
        return

    if btype == "section_heading":
        text = f"{bd.get('roman', '')}. {(bd.get('text') or '').upper()}"
        h = doc.add_heading(text, level=2)
        h.alignment = WD_ALIGN_PARAGRAPH.LEFT
        h.paragraph_format.space_before = Pt(14)
        h.paragraph_format.space_after = Pt(6)
        return

    if btype == "subsection":
        text = f"{bd.get('number', '')}. {bd.get('text', '')}"
        h = doc.add_heading(text, level=3)
        return

    if btype == "paragraph":
        p = doc.add_paragraph()
        align = bd.get("align", "justify")
        p.alignment = {
            "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
            "left": WD_ALIGN_PARAGRAPH.LEFT,
            "right": WD_ALIGN_PARAGRAPH.RIGHT,
            "center": WD_ALIGN_PARAGRAPH.CENTER,
        }.get(align, WD_ALIGN_PARAGRAPH.JUSTIFY)
        _add_runs(p, bd.get("runs", []))
        return

    if btype == "hecho":
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFIED if hasattr(WD_ALIGN_PARAGRAPH, "JUSTIFIED") else WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.left_indent = Cm(1)
        run = p.add_run(f"{bd.get('num', '?')}.\t")
        run.bold = True
        run.font.name = FONT
        run.font.size = Pt(FONT_SIZE_PT)
        _add_runs(p, bd.get("runs", []))
        return

    if btype == "pretension":
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.left_indent = Cm(1)
        run = p.add_run(f"{bd.get('ord', '?')}.- ")
        run.bold = True
        run.font.name = FONT
        run.font.size = Pt(FONT_SIZE_PT)
        _add_runs(p, bd.get("runs", []))
        return

    if btype == "norma_citada":
        # Inline-style con badge: lo agregamos como párrafo simple
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0)
        run = p.add_run(f"  · {bd.get('norma', '')}")
        run.bold = True
        if bd.get("derogada"):
            run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
            p.add_run(" [DEROGADA]").italic = True
        elif bd.get("verified"):
            p.add_run(" ✓").bold = True
        return

    if btype == "jurisprudencia":
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.left_indent = Cm(0.5)
        hdr = p.add_run(f"  · {bd.get('id', '')}, M.P. {bd.get('mp', '')} ({bd.get('corte', '')})")
        hdr.bold = True
        if bd.get("ratio"):
            p2 = doc.add_paragraph()
            p2.paragraph_format.left_indent = Cm(1.0)
            p2.paragraph_format.first_line_indent = Cm(0)
            r2 = p2.add_run("    “")
            r2.italic = True
            _add_runs(p2, bd.get("ratio", []), italic_default=True)
            r3 = p2.add_run("”")
            r3.italic = True
        return

    if btype == "silogismo":
        for label, runs_key in [
            ("Premisa Mayor (norma): ", "premisa_mayor"),
            ("Premisa Menor (hecho): ", "premisa_menor"),
            ("Conclusión: ", "conclusion"),
        ]:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            r = p.add_run(label)
            r.bold = True
            _add_runs(p, bd.get(runs_key, []))
        return

    if btype == "table":
        header = bd.get("header", [])
        rows = bd.get("rows", [])
        if not header or not rows:
            return
        tbl = doc.add_table(rows=len(rows) + 1, cols=len(header))
        tbl.style = "Light Grid Accent 1"
        for ci, h in enumerate(header):
            cell = tbl.rows[0].cells[ci]
            cell.text = ""
            p = cell.paragraphs[0]
            p.paragraph_format.first_line_indent = None
            r = p.add_run(h)
            r.font.name = FONT
            r.font.size = Pt(11)
            r.bold = True
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for ri, row in enumerate(rows):
            for ci in range(len(header)):
                cell = tbl.rows[ri + 1].cells[ci]
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                cell.text = ""
                p = cell.paragraphs[0]
                p.paragraph_format.first_line_indent = None
                txt = row[ci] if ci < len(row) else ""
                r = p.add_run(str(txt))
                r.font.name = FONT
                r.font.size = Pt(11)
                if bd.get("has_total_row") and ri == len(rows) - 1:
                    r.bold = True
        return

    if btype == "calc_step":
        p1 = doc.add_paragraph()
        p1.paragraph_format.first_line_indent = Cm(0)
        r = p1.add_run(bd.get("label", ""))
        r.bold = True
        r.underline = True

        p2 = doc.add_paragraph()
        p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p2.paragraph_format.first_line_indent = Cm(0)
        r2 = p2.add_run(f"Fórmula: {bd.get('formula', '')}")
        r2.italic = True

        p3 = doc.add_paragraph()
        p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p3.paragraph_format.first_line_indent = Cm(0)
        r3 = p3.add_run(f"Aplicación: {bd.get('aplicacion', '')}")
        r3.italic = True

        p4 = doc.add_paragraph()
        p4.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p4.paragraph_format.first_line_indent = Cm(0)
        r4 = p4.add_run(bd.get("total", ""))
        r4.bold = True
        return

    if btype == "list_item":
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.left_indent = Cm(1.5)
        r = p.add_run(f"{bd.get('num', '')}) ")
        r.bold = True
        _add_runs(p, bd.get("runs", []))
        return

    if btype == "juramento":
        doc.add_paragraph("")
        h = doc.add_heading("JURAMENTO", level=2)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if bd.get("norma_ref"):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(bd["norma_ref"])
            r.italic = True
        p2 = doc.add_paragraph(bd.get("text", ""))
        p2.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        return

    if btype == "firma":
        doc.add_paragraph("")
        doc.add_paragraph("")
        p1 = doc.add_paragraph()
        p1.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p1.paragraph_format.first_line_indent = Cm(0)
        r = p1.add_run(bd.get("ciudad_fecha", ""))
        r.italic = True

        doc.add_paragraph("")
        doc.add_paragraph("Atentamente,")
        doc.add_paragraph("")
        doc.add_paragraph("_____________________________________")

        p2 = doc.add_paragraph()
        p2.paragraph_format.first_line_indent = Cm(0)
        r2 = p2.add_run(bd.get("nombre", ""))
        r2.bold = True

        p3 = doc.add_paragraph()
        p3.paragraph_format.first_line_indent = Cm(0)
        p3.add_run(f"Abogado · T.P. No. {bd.get('tp', '')} del C.S.J.")

        if bd.get("cc"):
            p4 = doc.add_paragraph()
            p4.paragraph_format.first_line_indent = Cm(0)
            p4.add_run(f"C.C. No. {bd.get('cc')}")
        contact = []
        if bd.get("email"):
            contact.append(f"Email: {bd['email']}")
        if bd.get("telefono"):
            contact.append(f"Tel.: {bd['telefono']}")
        if contact:
            p5 = doc.add_paragraph()
            p5.paragraph_format.first_line_indent = Cm(0)
            p5.add_run(" · ".join(contact))
        return

    if btype == "blank":
        doc.add_paragraph("")
        return


def _add_runs(paragraph, runs: list[dict[str, Any]], italic_default: bool = False) -> None:
    from docx.shared import Pt
    for r in runs or []:
        if not isinstance(r, dict):
            continue
        text = r.get("text", "")
        if not text:
            continue
        run = paragraph.add_run(text)
        run.font.name = FONT
        run.font.size = Pt(FONT_SIZE_PT)
        if r.get("bold"):
            run.bold = True
        if r.get("italic") or italic_default:
            run.italic = True
        if r.get("underline"):
            run.underline = True
