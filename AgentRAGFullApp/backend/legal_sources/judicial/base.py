"""Abstract base for judicial notification sources."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class JudicialNotification:
    """Normalized result of polling a judicial source."""

    fuente: str
    titulo: str
    fecha_publicacion: date
    expediente: Optional[str] = None
    juzgado: Optional[str] = None
    tipo: Optional[str] = None  # auto, sentencia, edicto, oficio, notificacion
    severidad: str = "info"  # info | alta | critica
    resumen: Optional[str] = None
    url_oficial: Optional[str] = None
    fecha_actuacion: Optional[date] = None
    raw_html: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def hash_dedup(self) -> str:
        """SHA-256 over (fuente, expediente, fecha, titulo) for dedup."""
        seed = "|".join(
            [
                self.fuente,
                self.expediente or "",
                self.fecha_publicacion.isoformat(),
                (self.titulo or "")[:80],
            ]
        )
        return hashlib.sha256(seed.encode()).hexdigest()


class BaseJudicialSource(ABC):
    """Each source polls one expediente at a time and returns 0..N notifications."""

    name: str = "base"
    description: str = ""
    is_live: bool = False  # True = real scraping, False = demo/simulator

    @abstractmethod
    async def poll(
        self,
        expediente: str,
        last_polled_at: Optional[date] = None,
        **kwargs,
    ) -> list[JudicialNotification]:
        """Return all *new* judicial notifications for this expediente.

        `last_polled_at` filters out already-seen actuaciones. Implementations
        should be defensive: return [] on errors, never raise.
        """
        ...

    async def is_available(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} live={self.is_live}>"
