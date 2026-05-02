"""
TEST CASE: Comité de Convivencia Laboral — Frecuencia de reuniones

ESCENARIO (de la transcripción con Emma Castillo):
  Un abogado pregunta: "¿Cada cuánto se reúne el Comité de Convivencia Laboral?"

  ANTES: Resolución 652 de 2012 -> reuniones cada 3 meses (trimestrales)
  AHORA: Resolución 3461 de 2025 -> reuniones MENSUALES (derogó la 652 y 1356 de 2012)

  El agente DEBE:
  1. Encontrar la Resolución 3461 de 2025 en fuentes vivas
  2. Detectar que derogó las Resoluciones 652 y 1356 de 2012
  3. Registrar la derogación en el grafo
  4. Responder que las reuniones son MENSUALES, no trimestrales
  5. Citar la fuente correcta con URL

PROBLEMA QUE RESUELVE:
  "No encuentran las normas que ya son derogadas y sustituidas por otras"
  — Emma Castillo, abogada
"""
import asyncio
import sys
import os
import io

# Force UTF-8 output on Windows terminals
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))


async def run_test():
    from config.schema import load_config
    from utils.db import get_storage, close_storage
    from legal_sources.source_router import LegalSourceRouter
    from derogation.graph import DerogationGraph
    from derogation.vigencia_checker import VigenciaChecker
    from derogation.detector import detect_derogations
    from derogation.models import NormaCreate, DerogacionCreate, TipoNorma, TipoDerogacion, FuenteLegal, EstadoNorma

    config = load_config()
    storage = await get_storage(config.storage)
    router = LegalSourceRouter(config.legal_sources.model_dump())
    graph = DerogationGraph(storage.pool)
    checker = VigenciaChecker(graph)

    results = {"passed": 0, "failed": 0, "errors": []}

    def check(name, condition, detail=""):
        if condition:
            results["passed"] += 1
            print(f"  PASS: {name}")
        else:
            results["failed"] += 1
            results["errors"].append(f"{name}: {detail}")
            print(f"  FAIL: {name} — {detail}")

    # ══════════════════════════════════════════════════════════════
    # PASO 1: Buscar en fuentes vivas — ¿Existe la Res 3461/2025?
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PASO 1: Buscar Resolución 3461 de 2025 en fuentes vivas")
    print("=" * 70)

    search_result = await router.search("Resolución 3461 de 2025 comité convivencia", limit=5)
    found_3461 = any(
        r.numero == 3461 or (r.titulo and "3461" in str(r.titulo))
        for r in search_result["results"]
    )
    check(
        "Res 3461/2025 encontrada en fuentes vivas",
        found_3461,
        f"Solo encontró: {[f'{r.source}:{r.titulo}' for r in search_result['results']]}"
    )
    check(
        "Fuentes consultadas incluyen funcion_publica",
        "funcion_publica" in search_result["sources_consulted"],
        f"Fuentes: {search_result['sources_consulted']}"
    )

    # ══════════════════════════════════════════════════════════════
    # PASO 2: Descargar texto completo de la Res 3461/2025
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PASO 2: Descargar texto completo de Res 3461/2025")
    print("=" * 70)

    norm_data = await router.fetch_norm("RESOLUCION", 3461, 2025, preferred_source="funcion_publica")

    check(
        "Texto completo descargado",
        norm_data is not None and len(norm_data.get("texto_completo", "")) > 1000,
        f"Chars: {len(norm_data.get('texto_completo', '')) if norm_data else 0}"
    )
    check(
        "URL de fuente presente",
        norm_data is not None and "funcionpublica.gov.co" in (norm_data.get("fuente_url") or ""),
        f"URL: {norm_data.get('fuente_url') if norm_data else 'None'}"
    )

    texto_3461 = norm_data.get("texto_completo", "") if norm_data else ""

    # Verificar que el texto menciona reuniones mensuales
    texto_lower = texto_3461.lower()
    has_mensual = "mensual" in texto_lower or "una vez al mes" in texto_lower or "cada mes" in texto_lower
    check(
        "Texto menciona frecuencia MENSUAL",
        has_mensual,
        "No se encontró 'mensual' en el texto descargado"
    )

    # ══════════════════════════════════════════════════════════════
    # PASO 3: Detectar derogaciones en el texto de la Res 3461
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PASO 3: Detectar derogaciones automáticamente en Res 3461")
    print("=" * 70)

    derogations = detect_derogations(texto_3461)
    print(f"  Derogaciones detectadas: {len(derogations)}")
    for d in derogations:
        print(f"    {d.tipo_derogacion}: {d.norma_afectada_tipo} {d.norma_afectada_numero} de {d.norma_afectada_anio}")
        print(f"    Texto fuente: {d.texto_fuente[:100]}")

    found_652 = any(d.norma_afectada_numero == 652 for d in derogations)
    found_1356 = any(d.norma_afectada_numero == 1356 for d in derogations)

    check(
        "Detecta derogación de Resolución 652 de 2012",
        found_652,
        f"Derogaciones encontradas: {[(d.norma_afectada_tipo, d.norma_afectada_numero, d.norma_afectada_anio) for d in derogations]}"
    )
    check(
        "Detecta derogación de Resolución 1356 de 2012",
        found_1356,
        f"Derogaciones encontradas: {[(d.norma_afectada_tipo, d.norma_afectada_numero, d.norma_afectada_anio) for d in derogations]}"
    )

    # ══════════════════════════════════════════════════════════════
    # PASO 4: Registrar normas y derogaciones en el grafo
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PASO 4: Registrar en el grafo de derogaciones")
    print("=" * 70)

    # Insertar Res 652/2012 (la derogada)
    norma_652 = NormaCreate(
        tipo=TipoNorma.RESOLUCION, numero=652, anio=2012,
        titulo="Resolución 652 de 2012 - Comité de Convivencia Laboral",
        estado=EstadoNorma.VIGENTE,  # Se pondrá como DEROGADA al registrar la derogación
        entidad_emisora="Ministerio del Trabajo",
        sector="Laboral",
        fuente=FuenteLegal.MANUAL,
    )
    id_652 = await graph.insert_norma(norma_652)
    print(f"  Res 652/2012 insertada: {id_652}")

    # Insertar Res 1356/2012 (también derogada)
    norma_1356 = NormaCreate(
        tipo=TipoNorma.RESOLUCION, numero=1356, anio=2012,
        titulo="Resolución 1356 de 2012 - Modifica Res 652 Comité Convivencia",
        estado=EstadoNorma.VIGENTE,
        entidad_emisora="Ministerio del Trabajo",
        sector="Laboral",
        fuente=FuenteLegal.MANUAL,
    )
    id_1356 = await graph.insert_norma(norma_1356)
    print(f"  Res 1356/2012 insertada: {id_1356}")

    # Insertar Res 3461/2025 (la vigente)
    norma_3461 = NormaCreate(
        tipo=TipoNorma.RESOLUCION, numero=3461, anio=2025,
        titulo="Resolución 3461 de 2025 - Nuevo reglamento Comité de Convivencia Laboral",
        estado=EstadoNorma.VIGENTE,
        entidad_emisora="Ministerio del Trabajo",
        sector="Laboral",
        fuente=FuenteLegal.FUNCION_PUBLICA,
        fuente_url="https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i=262916",
        fuente_id="i=262916",
        temas=["convivencia laboral", "acoso laboral", "comité"],
    )
    id_3461 = await graph.insert_norma(norma_3461)
    print(f"  Res 3461/2025 insertada: {id_3461}")

    # Insertar Ley 1010/2006 (marco legal)
    norma_1010 = NormaCreate(
        tipo=TipoNorma.LEY, numero=1010, anio=2006,
        titulo="Ley 1010 de 2006 - Acoso Laboral",
        estado=EstadoNorma.VIGENTE,
        sector="Laboral",
        fuente=FuenteLegal.SENADO,
        fuente_url="http://www.secretariasenado.gov.co/senado/basedoc/ley_1010_2006.html",
        temas=["acoso laboral", "convivencia laboral"],
    )
    id_1010 = await graph.insert_norma(norma_1010)
    print(f"  Ley 1010/2006 insertada: {id_1010}")

    # Registrar derogaciones: 3461 deroga 652 y 1356
    derog_652 = DerogacionCreate(
        norma_origen_id=id_3461,
        norma_destino_id=id_652,
        tipo=TipoDerogacion.DEROGA_TOTAL,
        fuente_texto="Artículo 26 - Deróguense las Resoluciones 652 y 1356 de 2012",
        detectado_por="auto_regex",
        confianza=0.95,
    )
    await graph.insert_derogacion(derog_652)
    print(f"  Derogacion Res 3461 -> Res 652: registrada")

    derog_1356 = DerogacionCreate(
        norma_origen_id=id_3461,
        norma_destino_id=id_1356,
        tipo=TipoDerogacion.DEROGA_TOTAL,
        fuente_texto="Artículo 26 - Deróguense las Resoluciones 652 y 1356 de 2012",
        detectado_por="auto_regex",
        confianza=0.95,
    )
    await graph.insert_derogacion(derog_1356)
    print(f"  Derogacion Res 3461 -> Res 1356: registrada")

    # Registrar que 3461 reglamenta la Ley 1010
    derog_1010 = DerogacionCreate(
        norma_origen_id=id_3461,
        norma_destino_id=id_1010,
        tipo=TipoDerogacion.REGLAMENTA,
        fuente_texto="Por la cual se reglamenta la Ley 1010 de 2006 en lo relativo al Comité de Convivencia Laboral",
        detectado_por="manual",
        confianza=1.0,
    )
    await graph.insert_derogacion(derog_1010)
    print(f"  Reglamentacion Res 3461 -> Ley 1010: registrada")

    # ══════════════════════════════════════════════════════════════
    # PASO 5: Verificar vigencia — El test definitivo
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PASO 5: Verificar vigencia (el test que importa)")
    print("=" * 70)

    # ¿La Resolución 652 de 2012 sigue vigente?
    v_652 = await checker.check("RESOLUCION", 652, 2012)
    print(f"  Res 652/2012: estado={v_652.estado}, encontrada={v_652.encontrada}")
    print(f"  Badge: {checker.format_vigencia_badge(v_652)}")
    check(
        "Res 652/2012 marcada como DEROGADA",
        v_652.estado == "DEROGADA",
        f"Estado actual: {v_652.estado}"
    )
    check(
        "Derogada por Res 3461",
        any(d.get("norma_numero") == 3461 for d in v_652.derogaciones),
        f"Derogaciones: {v_652.derogaciones}"
    )

    # ¿La Resolución 1356 de 2012 sigue vigente?
    v_1356 = await checker.check("RESOLUCION", 1356, 2012)
    print(f"\n  Res 1356/2012: estado={v_1356.estado}")
    check(
        "Res 1356/2012 marcada como DEROGADA",
        v_1356.estado == "DEROGADA",
        f"Estado actual: {v_1356.estado}"
    )

    # ¿La Resolución 3461 de 2025 está vigente?
    v_3461 = await checker.check("RESOLUCION", 3461, 2025)
    print(f"\n  Res 3461/2025: estado={v_3461.estado}")
    check(
        "Res 3461/2025 marcada como VIGENTE",
        v_3461.estado == "VIGENTE",
        f"Estado actual: {v_3461.estado}"
    )

    # ¿La Ley 1010 de 2006 sigue vigente?
    v_1010 = await checker.check("LEY", 1010, 2006)
    print(f"\n  Ley 1010/2006: estado={v_1010.estado}")
    check(
        "Ley 1010/2006 sigue VIGENTE (solo fue reglamentada, no derogada)",
        v_1010.estado == "VIGENTE",
        f"Estado actual: {v_1010.estado}"
    )

    # ══════════════════════════════════════════════════════════════
    # PASO 6: Cadena de derogaciones
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PASO 6: Cadena de derogaciones de Res 652")
    print("=" * 70)

    chain = await graph.get_derogation_chain(id_652)
    print(f"  Cadena ({len(chain)} nodos):")
    for node in chain:
        print(f"    [{node.get('relacion', '?')}] {node.get('tipo', '')} {node.get('numero', '')} de {node.get('anio', '')} — {node.get('estado', '')}")

    check(
        "Cadena muestra Res 3461 como la que derogó a Res 652",
        any(n.get("numero") == 3461 and n.get("relacion") == "DEROGADA_POR" for n in chain),
        f"Cadena: {chain}"
    )

    # ══════════════════════════════════════════════════════════════
    # PASO 7: Simular la consulta del abogado al agente
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PASO 7: Simular consulta del abogado")
    print("=" * 70)

    query = "¿Cada cuánto tiene que reunirse el comité de convivencia laboral?"
    print(f"  Consulta: {query}")

    # El agente buscaría en fuentes vivas
    live = await router.search(query, limit=5)
    print(f"  Fuentes vivas: {len(live['results'])} resultados de {live['sources_consulted']}")

    # El agente verificaría vigencia de normas mencionadas en el contexto
    # Simular: el RAG devuelve chunks que mencionan Res 652/2012
    print(f"\n  Verificando vigencia de normas del contexto:")
    normas_en_contexto = [
        {"tipo": "RESOLUCION", "numero": "652", "anio": "2012"},
        {"tipo": "RESOLUCION", "numero": "3461", "anio": "2025"},
        {"tipo": "LEY", "numero": "1010", "anio": "2006"},
    ]
    vigencia_results = await checker.verify_results(normas_en_contexto)
    for v in vigencia_results:
        badge = checker.format_vigencia_badge(v)
        print(f"    {badge}")

    # Verificar que el badge de Res 652 dice DEROGADA
    badge_652 = next((checker.format_vigencia_badge(v) for v in vigencia_results if v.numero == 652), "")
    check(
        "Badge de Res 652 muestra DEROGADA por 3461",
        "DEROGADA" in badge_652 and "3461" in badge_652,
        f"Badge: {badge_652}"
    )

    # ══════════════════════════════════════════════════════════════
    # PASO 8: Respuesta esperada del agente
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PASO 8: Respuesta esperada del agente")
    print("=" * 70)

    print("""
  RESPUESTA CORRECTA que el agente debería dar:

  El Comité de Convivencia Laboral debe reunirse de manera MENSUAL en
  sesiones ordinarias (fuente: Resolución 3461 de 2025, Art. 8).
  https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i=262916

  VIGENCIA VERIFICADA:
  ❌ Resolución 652 de 2012 — DEROGADA por Resolución 3461 de 2025
  ❌ Resolución 1356 de 2012 — DEROGADA por Resolución 3461 de 2025
  ✅ Resolución 3461 de 2025 — VIGENTE
  ✅ Ley 1010 de 2006 — VIGENTE

  La antigua periodicidad trimestral (cada 3 meses) de la Resolución
  652 de 2012 fue derogada. Ahora las reuniones son mensuales.

  Adicionalmente, debe realizarse una sesión extraordinaria cada vez
  que se reciba una queja de acoso laboral.
""")

    # ══════════════════════════════════════════════════════════════
    # RESUMEN
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print(f"RESULTADO FINAL: {results['passed']} passed, {results['failed']} failed")
    print("=" * 70)

    if results["errors"]:
        print("\nFallas:")
        for e in results["errors"]:
            print(f"  - {e}")

    if results["failed"] == 0:
        print("\n  EL AGENTE PUEDE RESPONDER CORRECTAMENTE LA CONSULTA DEL ABOGADO")
        print("  El grafo de derogaciones resuelve el problema de normas desactualizadas")
    else:
        print(f"\n  {results['failed']} test(s) fallaron — revisar errores arriba")

    # Cleanup
    await router.close()
    await close_storage()

    return results["failed"] == 0


if __name__ == "__main__":
    success = asyncio.run(run_test())
    sys.exit(0 if success else 1)
