"""Sprint E · Redline diff · genera array de redlines desde 2 textos.

Output schema:
[
  {
    "id": "uuid",
    "type": "deletion" | "insertion" | "replacement",
    "start": 45,
    "end": 89,
    "original": "texto original",
    "suggested": "texto sugerido",
    "reason": "passive voice",
    "severity": "yellow",
    "citation": null | "T-760/2008"
  }
]
"""

from __future__ import annotations

import difflib
import uuid as _uuid
from dataclasses import dataclass
from typing import Optional


@dataclass
class Redline:
    id: str
    type: str
    start: int
    end: int
    original: Optional[str]
    suggested: Optional[str]
    reason: Optional[str]
    severity: str = "info"
    citation: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type,
            "start": self.start, "end": self.end,
            "original": self.original, "suggested": self.suggested,
            "reason": self.reason, "severity": self.severity,
            "citation": self.citation,
        }


def compute_redlines(
    original: str,
    suggested: str,
    *,
    reason_default: Optional[str] = None,
) -> list[Redline]:
    """SequenceMatcher word-level → arrays de redlines con offsets en chars original.

    Estrategia:
      1. Split por palabras preservando whitespace
      2. SequenceMatcher entre los splits
      3. Para cada bloque equal/delete/insert/replace, calcular char offsets
    """
    if original == suggested:
        return []

    redlines: list[Redline] = []
    sm = difflib.SequenceMatcher(a=original, b=suggested, autojunk=False)
    for op, a_start, a_end, b_start, b_end in sm.get_opcodes():
        if op == "equal":
            continue
        rl_id = _uuid.uuid4().hex[:12]
        if op == "delete":
            redlines.append(Redline(
                id=rl_id, type="deletion",
                start=a_start, end=a_end,
                original=original[a_start:a_end],
                suggested=None,
                reason=reason_default,
            ))
        elif op == "insert":
            redlines.append(Redline(
                id=rl_id, type="insertion",
                start=a_start, end=a_start,
                original=None,
                suggested=suggested[b_start:b_end],
                reason=reason_default,
            ))
        elif op == "replace":
            redlines.append(Redline(
                id=rl_id, type="replacement",
                start=a_start, end=a_end,
                original=original[a_start:a_end],
                suggested=suggested[b_start:b_end],
                reason=reason_default,
            ))
    return redlines


def apply_redlines(original: str, redlines: list[dict], *, only_ids: Optional[set[str]] = None) -> str:
    """Aplica redlines en reverse offset order para no invalidar índices."""
    sorted_rl = sorted(
        [r for r in redlines if not only_ids or r.get("id") in only_ids],
        key=lambda r: r.get("start", 0),
        reverse=True,
    )
    text = original
    for r in sorted_rl:
        s = r.get("start", 0)
        e = r.get("end", s)
        if r.get("type") == "deletion":
            text = text[:s] + text[e:]
        elif r.get("type") == "insertion":
            text = text[:s] + (r.get("suggested") or "") + text[s:]
        elif r.get("type") == "replacement":
            text = text[:s] + (r.get("suggested") or "") + text[e:]
    return text


def reject_redlines_passthrough(original: str) -> str:
    """Si todas las redlines se rechazan, el original queda igual."""
    return original
