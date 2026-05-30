"""Sprint M20.14 · validate_block_contract.py · valida que blocks del backend
matchean shape que BlockRenderer del frontend espera.

Lee el reporte más reciente de validate_real_poder_e2e.py y valida cada
block_emit contra el shape esperado por BlockRenderer (frontend TipTap/HTML).

Si hay mismatches → los reporta + sugiere fix.
Si no hay → el agente generará blocks que el frontend renderiza sin problemas.

USO:
    python scripts/validate_block_contract.py
    python scripts/validate_block_contract.py reports/real_poder_e2e_*.json
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

_BACKEND_ROOT = Path(__file__).parent.parent


# Schema EXACTO esperado por components/v2/document-gen/v2/BlockRenderer.tsx
# (extraído leyendo el archivo)
BLOCK_SCHEMAS = {
    "title": {
        "required": ["block_id", "type", "text"],
        "optional": ["level"],
        "renderer_uses": "block.text + block.level",
    },
    "section_heading": {
        "required": ["block_id", "type", "text"],
        "optional": ["roman", "section_key"],
        "renderer_uses": "block.roman + block.text",
    },
    "subsection": {
        "required": ["block_id", "type", "text"],
        "optional": ["number"],
        "renderer_uses": "block.number + block.text",
    },
    "paragraph": {
        "required": ["block_id", "type", "runs"],
        "optional": ["align", "indent_left_cm"],
        "renderer_uses": "block.runs + block.align",
    },
    "hecho": {
        "required": ["block_id", "type", "runs"],
        "optional": ["num"],
        "renderer_uses": "block.num + block.runs",
    },
    "pretension": {
        "required": ["block_id", "type", "runs"],
        "optional": ["ord", "kind"],
        "renderer_uses": "block.ord + block.runs + block.kind",
    },
    "norma_citada": {
        "required": ["block_id", "type", "norma"],
        "optional": ["contenido", "verified", "derogada", "fuente_url",
                     "fuente_url_vigente", "fuente_url_oficial", "tier",
                     "derogada_por", "suggested_correction"],
        "renderer_uses": "block.norma + block.fuente_url + (M20.10 tier)",
    },
    "jurisprudencia": {
        "required": ["block_id", "type", "id"],
        "optional": ["mp", "corte", "fecha", "ratio", "verified",
                     "fuente_url", "tier"],
        "renderer_uses": "block.id + block.mp + block.corte + block.ratio",
    },
    "silogismo": {
        "required": ["block_id", "type", "premisa_mayor", "premisa_menor", "conclusion"],
        "optional": [],
        "renderer_uses": "premisa_mayor + premisa_menor + conclusion",
    },
    "table": {
        "required": ["block_id", "type", "header", "rows"],
        "optional": ["header_shading", "total_row_shading", "has_total_row"],
        "renderer_uses": "block.header + block.rows",
    },
    "calc_step": {
        "required": ["block_id", "type", "label", "formula", "aplicacion", "total"],
        "optional": [],
        "renderer_uses": "label + formula + aplicacion + total",
    },
    "list_item": {
        "required": ["block_id", "type", "runs"],
        "optional": ["kind", "num"],
        "renderer_uses": "block.num + block.runs",
    },
    "juramento": {
        "required": ["block_id", "type", "text"],
        "optional": ["norma_ref"],
        "renderer_uses": "block.text",
    },
    "firma": {
        "required": ["block_id", "type", "nombre", "tp"],
        "optional": ["ciudad_fecha", "cc", "email", "telefono"],
        "renderer_uses": "block.nombre + block.tp + block.cc",
    },
    "blank": {
        "required": ["block_id", "type"],
        "optional": [],
        "renderer_uses": "(div vacío)",
    },
}


def validate_block(block: dict, idx: int) -> list[str]:
    """Retorna lista de issues (vacía si OK)."""
    issues = []
    btype = block.get("type")
    if not btype:
        return [f"#{idx}: missing 'type' field"]
    schema = BLOCK_SCHEMAS.get(btype)
    if not schema:
        return [f"#{idx}: type {btype!r} no en schema conocido"]
    # required fields
    for req in schema["required"]:
        if req not in block:
            issues.append(f"#{idx} [{btype}]: missing required field {req!r}")
    # runs validation (debe ser lista de dicts con 'text')
    if "runs" in block:
        runs = block.get("runs")
        if not isinstance(runs, list):
            issues.append(f"#{idx} [{btype}]: 'runs' is not list (got {type(runs).__name__})")
        else:
            for j, r in enumerate(runs):
                if not isinstance(r, dict):
                    issues.append(f"#{idx} [{btype}]: runs[{j}] not dict")
                elif "text" not in r:
                    issues.append(f"#{idx} [{btype}]: runs[{j}] missing 'text'")
    # align valid value
    if "align" in block and block["align"] not in (None, "left", "right", "center", "justify"):
        issues.append(f"#{idx} [{btype}]: align={block['align']!r} not in valid set")
    # tier valid value (M20.10/M20.13)
    if "tier" in block and block["tier"] not in (None, "GROUNDED", "VERIFY_FLAG",
                                                    "DEROGADA", "NOT_FOUND", "MODULADA"):
        issues.append(f"#{idx} [{btype}]: tier={block['tier']!r} not in 5-tier valid set")
    return issues


def main() -> int:
    if len(sys.argv) > 1:
        report_path = Path(sys.argv[1])
    else:
        reports = sorted((_BACKEND_ROOT / "reports").glob("real_poder_e2e_*.json"))
        if not reports:
            print("ERR: no hay reportes en reports/real_poder_e2e_*.json")
            print("Corre primero: python scripts/validate_real_poder_e2e.py")
            return 1
        report_path = reports[-1]

    print(f"=== validate_block_contract.py · {report_path.name} ===\n")
    data = json.loads(report_path.read_text(encoding="utf-8"))

    block_events = [e for e in data["events"] if e["name"] == "block_emit"]
    if not block_events:
        print("[WARN] no hay block_emit en el reporte")
        return 1

    print(f"Total block_emit events: {len(block_events)}\n")

    # tipos distintos emitidos
    type_counts = Counter()
    all_issues = []
    blocks_per_type_sample: dict[str, dict] = {}

    for idx, ev in enumerate(block_events):
        block = ev.get("payload", {}).get("block", {})
        if not block:
            all_issues.append(f"#{idx}: payload.block missing")
            continue
        btype = block.get("type", "unknown")
        type_counts[btype] += 1
        if btype not in blocks_per_type_sample:
            blocks_per_type_sample[btype] = block
        issues = validate_block(block, idx)
        all_issues.extend(issues)

    # === reporte ===
    print(f"Tipos de Block emitidos por el LeanOrchestrator:")
    print(f"{'TYPE':<22} {'COUNT':>6} {'STATUS':<12} {'NOTAS'}")
    print("-" * 80)
    for btype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        in_schema = btype in BLOCK_SCHEMAS
        sample = blocks_per_type_sample[btype]
        type_issues = [i for i in all_issues if f"[{btype}]" in i]
        status = "OK" if not type_issues and in_schema else ("ISSUES" if type_issues else "UNK_TYPE")
        notes = f"{len(type_issues)} issues" if type_issues else ("schema OK" if in_schema else "type no en schema")
        print(f"{btype:<22} {count:>6} {status:<12} {notes}")

    print(f"\nTotal blocks: {len(block_events)} · Tipos distintos: {len(type_counts)}")
    print(f"Total issues: {len(all_issues)}\n")

    if all_issues:
        print("=== Issues encontrados (primeros 20) ===")
        for issue in all_issues[:20]:
            print(f"  [FAIL] {issue}")
        if len(all_issues) > 20:
            print(f"  ... y {len(all_issues) - 20} más")
        # mostrar sample del primer bloque con issues
        first_issue_idx = int(all_issues[0].split("#")[1].split(" ")[0])
        first_block = block_events[first_issue_idx].get("payload", {}).get("block", {})
        print(f"\n=== Sample del primer block con issue ===")
        print(json.dumps(first_block, indent=2, ensure_ascii=False)[:600])
        return 1

    print("=" * 75)
    print("[100% PASS] Backend emite blocks con shape esperado por BlockRenderer.")
    print("=" * 75)
    print(f"\nTypes verificados:")
    for btype in sorted(type_counts.keys()):
        if btype in BLOCK_SCHEMAS:
            print(f"  [OK] {btype} · renderer_uses: {BLOCK_SCHEMAS[btype]['renderer_uses']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
