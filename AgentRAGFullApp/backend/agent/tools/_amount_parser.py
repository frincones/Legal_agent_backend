"""Parser de cantidades en español para tools que reciben prompts naturales.

Maneja:
  '2 millones'             → 2_000_000
  '2.5 millones'           → 2_500_000
  'dos millones'           → 2_000_000
  '300 mil'                → 300_000
  '50000 pesos'            → 50_000
  '$1.250.000'             → 1_250_000
  '1,250,000 cop'          → 1_250_000
"""

from __future__ import annotations

import re
from typing import Optional


WORD_NUMBERS = {
    "un": 1, "uno": 1, "una": 1,
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
    "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    "veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50,
    "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90,
    "cien": 100, "cienmil": 100, "doscientos": 200, "trescientos": 300,
    "quinientos": 500, "mil": 1000,
}


def parse_amount_from_text(text: str) -> Optional[float]:
    """Devuelve la cantidad en COP detectada en el texto, o None.

    Robusto a separadores ',' '.', sufijos millones/mil/pesos/cop/$.
    """
    if not text:
        return None
    low = text.lower()

    # 1) '$X' / 'X pesos' / 'X cop' · capturar primero
    m = re.search(
        r"(?:\$\s*)?(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
        r"\s*(mill[oó]n(?:es)?|mil|pesos|cop)",
        low,
    )
    if m:
        try:
            num_str = m.group(1).replace(".", "").replace(",", "")
            num = float(num_str)
            suffix = m.group(2)
            if suffix.startswith("mill"):
                num *= 1_000_000
            elif suffix == "mil":
                num *= 1_000
            return num
        except Exception:
            pass

    # 2) Número solo con $ prefix o muy grande
    m = re.search(r"\$\s*(\d{1,3}(?:[.,]\d{3})+|\d{4,})", low)
    if m:
        try:
            return float(m.group(1).replace(".", "").replace(",", ""))
        except Exception:
            pass

    # 3) Numbers in words · ej "dos millones" / "tres mil"
    word_match = re.search(
        r"\b(un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|"
        r"veinte|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa|"
        r"cien|doscientos|trescientos|quinientos|mil)\s+"
        r"(mill[oó]n(?:es)?|mil)",
        low,
    )
    if word_match:
        base = WORD_NUMBERS.get(word_match.group(1), 0)
        mult = 1_000_000 if word_match.group(2).startswith("mill") else 1_000
        if base > 0:
            return float(base * mult)

    # 4) Bare number ≥ 4 digits, asume pesos
    m = re.search(r"\b(\d{4,})\b", low)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass

    return None
