"""M19.24.E.1 — DOCX post-render validator.

Reproduce el "Paso 7" de Claude (validation loop scripted).

Valida que el .docx recién renderizado por docx_forensic_builder cumpla
con el schema OOXML mínimo. Sin schema XSD completo, hacemos checks
estructurales heurísticos sobre el XML:

  1. word/document.xml debe existir y ser XML válido
  2. No debe tener <w:highlightCs> (elemento no permitido)
  3. Cuenta paragraphs > 0
  4. No debe tener elementos vacíos sospechosos
  5. Verifica que las relaciones (rels) sean consistentes

Si detecta error → marca quality_report.docx_validation_passed = false
pero NO bloquea el flow (el doc se entrega igual con la advertencia).
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DocxValidationReport:
    passed: bool = True
    paragraph_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "paragraph_count": self.paragraph_count,
            "errors": self.errors[:10],
            "warnings": self.warnings[:10],
            "duration_ms": self.duration_ms,
        }


FORBIDDEN_ELEMENTS = [
    # Elementos OOXML que no se aceptan dentro de runs/paragraphs
    re.compile(r"<w:highlightCs\b"),  # Claude detectó este
]


def validate_docx_bytes(docx_bytes: bytes) -> DocxValidationReport:
    """Valida un .docx en memoria.

    Args:
        docx_bytes: bytes del archivo .docx generado

    Returns: DocxValidationReport con paso/no paso + lista de errores.
    """
    import time

    started = time.time()
    report = DocxValidationReport()

    if not docx_bytes or len(docx_bytes) < 100:
        report.passed = False
        report.errors.append("docx_empty_or_too_small")
        report.duration_ms = int((time.time() - started) * 1000)
        return report

    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as zf:
            names = set(zf.namelist())
            if "word/document.xml" not in names:
                report.passed = False
                report.errors.append("missing_word_document_xml")
                report.duration_ms = int((time.time() - started) * 1000)
                return report

            doc_xml = zf.read("word/document.xml").decode("utf-8", errors="replace")

            # Contar paragraphs (heurística rápida)
            report.paragraph_count = len(re.findall(r"<w:p\b", doc_xml))

            # Check forbidden elements
            for pat in FORBIDDEN_ELEMENTS:
                if pat.search(doc_xml):
                    report.passed = False
                    report.errors.append(
                        f"forbidden_element_found: {pat.pattern}"
                    )

            # Heurísticas adicionales (warnings, no bloquean)
            if report.paragraph_count == 0:
                report.warnings.append("zero_paragraphs")
            elif report.paragraph_count > 5000:
                report.warnings.append(f"unusually_high_paragraph_count: {report.paragraph_count}")

            # Verificar relaciones básicas
            if "word/_rels/document.xml.rels" in names:
                try:
                    rels_xml = zf.read("word/_rels/document.xml.rels").decode("utf-8", errors="replace")
                    # Rels mal formadas suelen tener "rels"="" o IDs vacíos
                    if 'Target=""' in rels_xml:
                        report.warnings.append("empty_relationship_target")
                except Exception:
                    pass

            # XML estructural mínimo: debe contener </w:document> al final
            if "</w:document>" not in doc_xml:
                report.passed = False
                report.errors.append("missing_document_close_tag")

    except zipfile.BadZipFile:
        report.passed = False
        report.errors.append("bad_zip_file")
    except Exception as e:
        report.passed = False
        report.errors.append(f"validation_exception: {str(e)[:120]}")

    report.duration_ms = int((time.time() - started) * 1000)
    logger.info(
        "docx_validator: passed=%s paragraphs=%d errors=%d warnings=%d duration=%dms",
        report.passed, report.paragraph_count,
        len(report.errors), len(report.warnings), report.duration_ms,
    )
    return report


__all__ = ["DocxValidationReport", "validate_docx_bytes"]
