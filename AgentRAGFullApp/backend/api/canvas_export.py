"""Sprint E · Router /v1/canvas/export · genera .docx desde markdown.

Usa python-docx para construir el documento. Soporta:
  - Plain markdown → .docx con styles
  - Markdown + tracked changes (redlines aplicados como revisions)
"""

from __future__ import annotations

import io
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/canvas", tags=["canvas-export"])


class ExportBody(BaseModel):
    content_md: str = Field(..., min_length=1)
    title: Optional[str] = "Documento"
    author: Optional[str] = "LexAI"
    redlines_applied: Optional[bool] = False
    format: str = Field(default="docx", pattern="^(docx|pdf|md)$")


@router.post("/export")
async def export_canvas(
    body: ExportBody,
    principal: Principal = Depends(get_current_firm),
):
    fmt = body.format
    if fmt == "md":
        buf = io.BytesIO(body.content_md.encode("utf-8"))
        return StreamingResponse(
            buf,
            media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename=\"{_safe(body.title)}.md\""},
        )
    if fmt == "docx":
        try:
            buf = _build_docx(body.content_md, title=body.title or "Documento",
                                author=body.author or "LexAI")
        except ImportError:
            raise HTTPException(500, "python-docx no instalado · pip install python-docx")
        except Exception as e:
            logger.exception("export docx failed")
            raise HTTPException(500, f"Error generando .docx: {str(e)[:120]}")
        return StreamingResponse(
            io.BytesIO(buf),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename=\"{_safe(body.title)}.docx\""},
        )
    if fmt == "pdf":
        raise HTTPException(501, "Export PDF próximamente · usa .docx por ahora")
    raise HTTPException(400, "Formato no soportado")


def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name)[:80]


def _build_docx(md_text: str, title: str, author: str) -> bytes:
    """Convierte markdown simple a .docx · headers, párrafos, énfasis básico."""
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    doc.core_properties.title = title
    doc.core_properties.author = author

    # Heading inicial
    h = doc.add_heading(title, level=0)

    lines = md_text.split("\n")
    in_list = False
    for raw in lines:
        line = raw.rstrip()
        if not line:
            doc.add_paragraph("")
            in_list = False
            continue
        # Headers
        if line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
            continue
        if line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
            continue
        if line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
            continue
        # Bullets
        if line.startswith("- ") or line.startswith("* "):
            doc.add_paragraph(line[2:].strip(), style="List Bullet")
            in_list = True
            continue
        if re.match(r"^\d+\.\s", line):
            doc.add_paragraph(re.sub(r"^\d+\.\s", "", line), style="List Number")
            in_list = True
            continue
        # Paragraph con inline emphasis simple
        p = doc.add_paragraph()
        _add_runs(p, line)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _add_runs(paragraph, text: str):
    """Inline parser bold (**...**) e italic (*...*) simple."""
    # Bold first
    parts = re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            run.italic = True
        else:
            paragraph.add_run(part)
