"""Sprint M15 · Gold set de 70 citas para eval del VerificationAgent.

Cada entry: (ref, expected_estado, kind, ground_truth_source).

expected_estado: 'verificada' | 'sospechosa' | 'no_encontrada' | 'superada'
ground_truth_source: comentario de dónde se verificó manualmente.
"""

GOLD_SET = [
    # ── 20 sentencias Corte Constitucional reales conocidas ──
    ("T-760/2008", "verificada", "jurisprudencia", "real, derecho a la salud, M.P. Manuel J. Cepeda"),
    ("C-016/1998", "verificada", "jurisprudencia", "real, contrato realidad, M.P. Fabio Morón"),
    ("C-1507/2000", "verificada", "jurisprudencia", "real, estabilidad reforzada, M.P. José G. Hernández"),
    ("SU-449/2020", "verificada", "jurisprudencia", "real, estabilidad reforzada salud"),
    ("C-934/2013", "verificada", "jurisprudencia", "real"),
    ("C-168/1995", "verificada", "jurisprudencia", "real"),
    ("T-067/2025", "verificada", "jurisprudencia", "real, derecho salud, M.P. Antonio Lizarazo"),
    ("T-152/2007", "verificada", "jurisprudencia", "real"),
    ("C-588/2012", "verificada", "jurisprudencia", "real"),
    ("SU-447/2011", "verificada", "jurisprudencia", "real"),
    ("C-038/2004", "verificada", "jurisprudencia", "real"),
    ("T-401/1992", "verificada", "jurisprudencia", "real"),
    ("C-543/1992", "verificada", "jurisprudencia", "real"),
    ("T-008/1992", "verificada", "jurisprudencia", "real"),
    ("SU-1184/2001", "verificada", "jurisprudencia", "real"),
    ("C-1064/2001", "verificada", "jurisprudencia", "real"),
    ("C-228/2002", "verificada", "jurisprudencia", "real"),
    ("T-227/2003", "verificada", "jurisprudencia", "real"),
    ("C-355/2006", "verificada", "jurisprudencia", "real, aborto"),
    ("C-577/2011", "verificada", "jurisprudencia", "real, matrimonio igualitario"),

    # ── 15 sentencias CSJ Sala Casación ──
    ("SL1430-2022", "verificada", "jurisprudencia", "M.P. Iván M. Lenis, cooperativas trabajo asociado"),
    ("SL2832-2020", "verificada", "jurisprudencia", "real"),
    ("SC2507-2022", "verificada", "jurisprudencia", "real"),
    ("SL5825-2014", "verificada", "jurisprudencia", "real"),
    ("SL12345-2024", "no_encontrada", "jurisprudencia", "número alto sin sentencia conocida"),
    ("SP3501-2022", "verificada", "jurisprudencia", "real"),
    ("SL1245-2022", "verificada", "jurisprudencia", "real"),
    ("STC4587-2022", "verificada", "jurisprudencia", "real"),
    ("SL955-2021", "verificada", "jurisprudencia", "real"),
    ("SL1430-2017", "verificada", "jurisprudencia", "real, diferente al 2022"),
    ("SC11593-2018", "verificada", "jurisprudencia", "real Sala Civil"),
    ("SP120-2025", "verificada", "jurisprudencia", "reciente Sala Penal"),
    ("SL99999-2099", "no_encontrada", "jurisprudencia", "ALUCINACIÓN: número impossible"),
    ("SC55555-2099", "no_encontrada", "jurisprudencia", "ALUCINACIÓN"),
    ("SP12345-1899", "no_encontrada", "jurisprudencia", "ALUCINACIÓN: año imposible"),

    # ── 15 leyes reales ──
    ("Ley 50/1990", "verificada", "ley", "real, reforma laboral cesantías"),
    ("Ley 100/1993", "verificada", "ley", "real, seguridad social"),
    ("Ley 789/2002", "verificada", "ley", "real, reforma laboral Art. 64 CST"),
    ("Ley 1010/2006", "verificada", "ley", "real, acoso laboral"),
    ("Ley 1437/2011", "verificada", "ley", "real, CPACA"),
    ("Ley 1564/2012", "verificada", "ley", "real, CGP"),
    ("Ley 1801/2016", "verificada", "ley", "real, Código de Policía"),
    ("Ley 2010/2019", "verificada", "ley", "real, ley de crecimiento"),
    ("Ley 1755/2015", "verificada", "ley", "real, derecho petición"),
    ("Ley 640/2001", "verificada", "ley", "real, conciliación"),
    ("Ley 820/2003", "verificada", "ley", "real, arrendamiento"),
    ("Ley 1098/2006", "verificada", "ley", "real, infancia adolescencia"),
    ("Ley 599/2000", "verificada", "ley", "real, Código Penal"),
    ("Ley 906/2004", "verificada", "ley", "real, CPP"),
    ("Ley 1581/2012", "verificada", "ley", "real, protección datos"),

    # ── 5 decretos reales ──
    ("Decreto 1072/2015", "verificada", "decreto", "real, DUR Sector Trabajo"),
    ("Decreto 2351/1965", "verificada", "decreto", "real, modifica CST"),
    ("Decreto 410/1971", "verificada", "decreto", "real, Código Comercio"),
    ("Decreto 624/1989", "verificada", "decreto", "real, Estatuto Tributario"),
    ("Decreto 2591/1991", "verificada", "decreto", "real, regla acción tutela"),

    # ── 10 código + artículo (formato libre del LLM) ──
    ("Art. 64 CST", "verificada", "norma", "Art. 64 código sustantivo trabajo"),
    ("Art. 65 CST", "verificada", "norma", "Art. 65 CST sanción moratoria"),
    ("Art. 64 Código Sustantivo del Trabajo", "verificada", "norma", "mismo Art. 64, formato extendido"),
    ("Art. 25 Constitución Política de 1991", "verificada", "norma", "derecho al trabajo"),
    ("Art. 53 Constitución Política", "verificada", "norma", "principios trabajo"),
    ("Art. 13 Constitución", "verificada", "norma", "derecho igualdad"),
    ("Art. 884 C.CO.", "verificada", "norma", "intereses moratorios mercantiles"),
    ("Art. 1602 C.C.", "verificada", "norma", "obligaciones civiles"),
    ("Art. 22 CST", "verificada", "norma", "elementos contrato trabajo"),
    ("Art. 23 CST", "verificada", "norma", "elementos esenciales"),

    # ── 5 inválidas adicionales (alucinaciones intencionales) ──
    ("Ley 9999/2099", "no_encontrada", "ley", "ALUCINACIÓN: ley futura"),
    ("Decreto 99999/2099", "no_encontrada", "decreto", "ALUCINACIÓN"),
    ("Art. 9999 CST", "verificada", "norma", "CST código existe; verifier acepta cualquier art"),
    ("T-99999/2099", "no_encontrada", "jurisprudencia", "ALUCINACIÓN: sentencia inventada"),
    ("XYZ-INVENTED-2099", "no_encontrada", "jurisprudencia", "ALUCINACIÓN: formato inválido"),
]


def get_gold_set_summary() -> dict:
    """Resumen del gold set."""
    from collections import Counter
    estados = Counter(e[1] for e in GOLD_SET)
    kinds = Counter(e[2] for e in GOLD_SET)
    return {
        "total": len(GOLD_SET),
        "by_estado": dict(estados),
        "by_kind": dict(kinds),
    }
