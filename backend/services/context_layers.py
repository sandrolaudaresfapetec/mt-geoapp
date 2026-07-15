"""
Contexto Socioambiental - GeoServer publico do TerraBrasilis/BDQueimadas (INPE).
Fornece, para o poligono desenhado pelo usuario:
  - Terras Indigenas (TI) que intersectam a area
  - Unidades de Conservacao (UC federais e estaduais) que intersectam a area
  - Focos de calor recentes (INPE, dados quase em tempo real) dentro da area

Nenhuma credencial e necessaria - dados publicos via WFS (OGC).
Mesmo GeoServer usado pelo painel "Mato Grosso Alertas" (SEMA-MT / SCCON) para
contextualizar alertas de desmatamento com informacoes fundiarias e de fogo.
"""
import httpx
from shapely.geometry import shape, box

from services import sema_mt

GEOSERVER_BASE = "https://terrabrasilis.dpi.inpe.br/queimadas/geoserver/bdqueimadas2/ows"

# workspace fixo: bdqueimadas2
TI_LAYER = "ti"
UC_FEDERAL_LAYER = "uc_f_nao_reservas"
UC_ESTADUAL_LAYER = "uc_e_nao_reservas"
RPPN_LAYER = "rppn"
FOCOS_LAYER = "focos"

# Campo geometrico de cada camada (descoberto via DescribeFeatureType)
GEOM_FIELD = {
    TI_LAYER: "geom",
    UC_FEDERAL_LAYER: "geom",
    UC_ESTADUAL_LAYER: "geom",
    RPPN_LAYER: "geom",
    FOCOS_LAYER: "geometria",
}


def _bbox_of(geometry: dict):
    geom = shape(geometry)
    return geom.bounds  # minx, miny, maxx, maxy


async def _wfs_getfeature(layer: str, cql_filter: str, count: int = 500):
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": f"bdqueimadas2:{layer}",
        "outputFormat": "application/json",
        "cql_filter": cql_filter,
        "count": count,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(GEOSERVER_BASE, params=params)
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:300], "features": []}
    try:
        return resp.json()
    except Exception:
        return {"error": "resposta nao-JSON", "features": []}


async def _query_polygon_layer(layer: str, geometry: dict):
    """Busca features que intersectam o bbox do poligono, depois refina com
    interseccao geometrica precisa (shapely) para eliminar falsos positivos
    do bounding box."""
    minx, miny, maxx, maxy = _bbox_of(geometry)
    geom_field = GEOM_FIELD[layer]
    cql = f"INTERSECTS({geom_field},ENVELOPE({minx},{maxx},{miny},{maxy}))"
    data = await _wfs_getfeature(layer, cql)
    raw_features = data.get("features", [])
    if "error" in data:
        return {"error": data["error"], "features": []}

    aoi = shape(geometry)
    matched = []
    for f in raw_features:
        try:
            fgeom = shape(f["geometry"])
            if fgeom.intersects(aoi):
                matched.append(f)
        except Exception:
            continue
    return {"features": matched, "raw_count": len(raw_features)}


async def query_terras_indigenas(geometry: dict):
    result = await _query_polygon_layer(TI_LAYER, geometry)
    features = result.get("features", [])
    items = []
    for f in features:
        p = f.get("properties", {})
        items.append({
            "nome": p.get("nome"),
            "id_ti": p.get("id_ti"),
        })
    return {
        "source": "Terras Indígenas (FUNAI via TerraBrasilis/INPE)",
        "count": len(items),
        "items": items,
        "error": result.get("error"),
    }


async def query_unidades_conservacao(geometry: dict):
    fed = await _query_polygon_layer(UC_FEDERAL_LAYER, geometry)
    est = await _query_polygon_layer(UC_ESTADUAL_LAYER, geometry)

    def _fmt(result, esfera):
        items = []
        for f in result.get("features", []):
            p = f.get("properties", {})
            items.append({
                "nome": p.get("nome"),
                "esfera": esfera,
            })
        return items

    items = _fmt(fed, "Federal") + _fmt(est, "Estadual")
    partial_errors = []
    if fed.get("error"):
        partial_errors.append(f"UC federais indisponível no momento ({fed['error']})")
    if est.get("error"):
        partial_errors.append(f"UC estaduais indisponível no momento ({est['error']})")

    fallback_used = False
    fallback_note = None
    # Fallback: quando a consulta oficial (TerraBrasilis/INPE) falha para
    # alguma esfera, tenta complementar com a camada unificada do
    # Geoportal SEMA-MT (fonte nao oficialmente confirmada - ver
    # services/sema_mt.py). O resultado do fallback e' mesclado e
    # sinalizado explicitamente, nunca substitui silenciosamente a fonte
    # oficial.
    if partial_errors:
        try:
            sema_result = await sema_mt.query_unidades_conservacao_sema(geometry)
            if not sema_result.get("error") and sema_result.get("count", 0) >= 0:
                nomes_existentes = {i["nome"] for i in items if i.get("nome")}
                for i in sema_result.get("items", []):
                    if i.get("nome") and i["nome"] not in nomes_existentes:
                        items.append({
                            "nome": i.get("nome"),
                            "esfera": i.get("esfera") or "N/D",
                            "via_fallback_sema_mt": True,
                        })
                fallback_used = True
                fallback_note = (
                    "Complementado com dados do Geoportal SEMA-MT (fonte não "
                    "oficialmente confirmada, ver documentação) para cobrir a "
                    "falha da fonte oficial acima."
                )
        except Exception as e:
            fallback_note = f"Fallback SEMA-MT também falhou: {e}"

    return {
        "source": "Unidades de Conservação (ICMBio/SEMA-MT via TerraBrasilis/INPE)",
        "count": len(items),
        "items": items,
        "error": fed.get("error") or est.get("error"),
        "partial": bool(partial_errors) and not fallback_used,
        "partial_errors": partial_errors,
        "fallback_used": fallback_used,
        "fallback_note": fallback_note,
    }


async def query_focos_calor(geometry: dict, days: int = 30):
    """Focos de calor (queimadas) dentro do poligono nos ultimos N dias.
    Dados quase em tempo real (satelites GOES/NOAA/Aqua/Terra, INPE)."""
    from datetime import datetime, timedelta, timezone

    minx, miny, maxx, maxy = _bbox_of(geometry)
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    geom_field = GEOM_FIELD[FOCOS_LAYER]
    cql = (
        f"data_hora_gmt AFTER {date_from} AND "
        f"INTERSECTS({geom_field},ENVELOPE({minx},{maxx},{miny},{maxy}))"
    )
    data = await _wfs_getfeature(FOCOS_LAYER, cql, count=1000)
    raw_features = data.get("features", [])
    if "error" in data:
        return {"error": data["error"], "count": 0, "items": []}

    aoi = shape(geometry)
    matched = []
    for f in raw_features:
        try:
            fgeom = shape(f["geometry"])
            if fgeom.intersects(aoi):
                matched.append(f)
        except Exception:
            continue

    items = []
    for f in matched:
        p = f.get("properties", {})
        items.append({
            "data_hora_gmt": p.get("data_hora_gmt"),
            "satelite": p.get("satelite"),
            "municipio": p.get("municipio"),
            "bioma": p.get("bioma"),
            "risco_fogo": p.get("risco_fogo"),
            "frp": p.get("frp"),
            "dias_sem_chuva": p.get("numero_dias_sem_chuva"),
        })
    items.sort(key=lambda x: x.get("data_hora_gmt") or "", reverse=True)

    return {
        "source": "Focos de Calor (Programa Queimadas / INPE, quase tempo real)",
        "period_days": days,
        "count": len(items),
        "items": items[:100],
    }


async def get_context_summary(geometry: dict, focos_days: int = 30):
    """Agrega as 3 camadas de contexto socioambiental para o poligono."""
    import asyncio

    ti_result, uc_result, focos_result = await asyncio.gather(
        query_terras_indigenas(geometry),
        query_unidades_conservacao(geometry),
        query_focos_calor(geometry, days=focos_days),
    )

    alerts = []
    if ti_result.get("count", 0) > 0:
        nomes = ", ".join([i["nome"] for i in ti_result["items"] if i.get("nome")])
        alerts.append(f"⚠️ Área sobrepõe Terra Indígena: {nomes}")
    if uc_result.get("count", 0) > 0:
        nomes = ", ".join([i["nome"] for i in uc_result["items"] if i.get("nome")])
        alerts.append(f"⚠️ Área sobrepõe Unidade de Conservação: {nomes}")
    if uc_result.get("partial"):
        alerts.append(
            "⚠️ Consulta de Unidades de Conservação incompleta (" +
            "; ".join(uc_result.get("partial_errors", [])) +
            "). O resultado acima NÃO deve ser interpretado como confirmação de ausência de UC."
        )
    if uc_result.get("fallback_used"):
        alerts.append(
            "ℹ️ A fonte oficial (TerraBrasilis/INPE) falhou para uma ou mais esferas de "
            "Unidades de Conservação; o resultado foi complementado com o Geoportal SEMA-MT "
            "(fonte não oficialmente confirmada, ver /api/context/car para detalhes)."
        )
    if focos_result.get("count", 0) > 0:
        alerts.append(
            f"🔥 {focos_result['count']} foco(s) de calor detectado(s) "
            f"nos últimos {focos_days} dias dentro da área"
        )

    return {
        "terras_indigenas": ti_result,
        "unidades_conservacao": uc_result,
        "focos_calor": focos_result,
        "alerts": alerts,
    }
