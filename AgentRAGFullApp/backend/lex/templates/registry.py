"""Registry singleton de templates disponibles.

Auto-carga todos los TemplateDef desde lex/templates/defs/*.py al primer get.
"""
from __future__ import annotations

import logging
from typing import Iterable

from lex.templates.base import TemplateDef

logger = logging.getLogger(__name__)


class _Registry:
    def __init__(self):
        self._templates: dict[str, TemplateDef] = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        # Lazy imports para evitar ciclos
        from lex.templates.defs import (
            concepto_juridico,
            contrato_arrendamiento,
            contrato_prestacion_servicios,
            demanda_alimentos,
            demanda_civil_ordinaria,
            demanda_ejecutivo_singular,
            demanda_laboral_ordinaria,
            denuncia_penal,
            derecho_peticion,
            poder_especial,
            recurso_apelacion,
            tutela,
        )
        for mod in [
            demanda_laboral_ordinaria,
            demanda_civil_ordinaria,
            demanda_ejecutivo_singular,
            demanda_alimentos,
            tutela,
            derecho_peticion,
            contrato_arrendamiento,
            contrato_prestacion_servicios,
            denuncia_penal,
            recurso_apelacion,
            concepto_juridico,
            poder_especial,
        ]:
            try:
                t = getattr(mod, "TEMPLATE", None)
                if isinstance(t, TemplateDef):
                    self._templates[t.id] = t
                    logger.debug("registered template: %s", t.id)
            except Exception as e:
                logger.warning("template load failed for %s: %s", mod.__name__, e)
        self._loaded = True
        logger.info("Template registry loaded: %d templates", len(self._templates))

    def get(self, template_id: str) -> TemplateDef | None:
        self._load()
        return self._templates.get(template_id)

    def list_all(self) -> list[TemplateDef]:
        self._load()
        return list(self._templates.values())

    def list_by_jurisdiccion(self, jurisdiccion: str) -> list[TemplateDef]:
        self._load()
        return [t for t in self._templates.values() if t.jurisdiccion == jurisdiccion]

    def ids(self) -> Iterable[str]:
        self._load()
        return self._templates.keys()


registry = _Registry()
