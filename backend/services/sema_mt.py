"""
Camadas adicionais do Geoportal da SEMA-MT (Secretaria de Estado de Meio
Ambiente de Mato Grosso) - GeoServer publico com WFS (OGC).

Fornece, para o poligono desenhado pelo usuario:
  - Unidades de Conservacao (Federal + Estadual + Municipal, numa unica
    camada) - usada como fonte alternativa/fallback quando a camada
    federal do GeoServer do INPE/TerraBrasilis falha (bug conhecido,
    ver services/context_layers.py).
  - CAR - Area de Preservacao Permanente declarada (CAR_APP)
  - CAR - Reserva Legal declarada (CAR_ARL)
  - CAR - Area Consolidada / uso antropico (SIMCAR_D_AREA_CONSOLIDADA)
  - Autuacoes e embargos ambientais da SEMA (fiscalizacao)

AVISO IMPORTANTE SOBRE A FONTE:
O acesso a este GeoServer requer um parametro "authkey" na URL. Essa chave
NAO esta documentada oficialmente pela SEMA-MT (nao ha portal de
desenvolvedor, termos de uso publicados ou processo formal de solicitacao
de acesso conhecido); ela foi localizada em um documento tecnico de
terceiros publicamente disponivel e validada empiricamente (HTTP 200 em
consultas WFS reais). Por isso, TODAS as respostas deste modulo sao
marcadas com "unverified_source": True e devem ser exibidas ao usuario
final com uma nota explicita de que a fonte nao foi oficialmente
confirmada pela SEMA-MT e pode deixar de funcionar sem aviso previo.
"""
import os
import httpx
from shapely.geometry import shape

GEOSERVER_BASE = "https://geo.sema.mt.gov.br/geoserver/Geoportal/ows"

# A authkey pode ser sobrescrita via variavel de ambiente SEMA_MT_AUTHKEY.
# Valor padrao: chave publica localizada em documentacao tecnica de
# terceiros (nao oficialmente confirmada pela SEMA-MT - ver aviso acima).
DEFAULT_AUTHKEY = "541085de-9a2e-454e-bdba-eb3d57a2f492"
AUTHKEY = os.environ.get("SEMA_MT_AUTHKEY", DEFAULT_AUTHKEY)

SOURCE_NOTE = (
    "Fonte: Geoportal SEMA-MT (GeoServer publico). Acesso via chave (authkey) "
    "localizada em documentacao tecnica de terceiros, SEM confirmacao oficial "
    "da SEMA-MT sobre estabilidade ou termos de uso. Trate como complementar "
    "as fontes oficiais (TerraBrasilis/INPE) e valide criticamente antes de "
    "uso em decisoes de compliance."
)

# workspace fixo: Geoportal
UC_LAYER = "UNIDADES_CONSERVACAO"
CAR_APP_LAYER = "CAR_APP"
CAR_ARL_LAYER = "CAR_ARL"
AREA_CONSOLIDADA_LAYER = "SIMCAR_D_AREA_CONSOLIDADA"
AUTOS_INFRACAO_LAYER = "AUTOS_DE_INFRACAO_SIGA_POLIGONO"

# Campo geometrico de cada camada (descoberto via DescribeFeatureType)
GEOM_FIELD = {
    UC_LAYER: "SHAPE",
    CAR_APP_LAYER: "GEOMETRY",
    CAR_ARL_LAYER: "GEOMETRY",
    AREA_CONSOLIDADA_LAYER: "GEOMETRY",
    AUTOS_INFRACAO_LAYER: "GEO_SHAPE",
}


def _bbox_of(geometry: dict):
    geom = shape(geometry)
    return geom.bounds  # minx, miny, maxx, maxy


async def _wfs_getfeature(layer: str, cql_filter: str, count: int = 500):
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "authkey": AUTHKEY,
        "request": "GetFeature",
        "typeName": f"Geoportal:{layer}",
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
        return {"error": "resposta nao-JSON (possivel ServiceExceptionReport)", "features": []}


async def _query_polygon_layer(layer: str, geometry: dict, count: int = 500):
    """Busca features via BBOX (a camada do Geoportal SEMA-MT nao aceita
    o operador INTERSECTS com ENVELOPE para todas as camadas - BBOX e o
    filtro compativel confirmado empiricamente), depois refina com
    interseccao geometrica precisa (shapely)."""
    minx, miny, maxx, maxy = _bbox_of(geometry)
    geom_field = GEOM_FIELD[layer]
    cql = f"BBOX({geom_field},{minx},{miny},{maxx},{maxy})"
    data = await _wfs_getfeature(layer, cql, count=count)
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


async def query_unidades_conservacao_sema(geometry: dict):
    """UCs Federal + Estadual + Municipal numa unica camada do Geoportal
    SEMA-MT. Usada como fallback quando a camada federal do INPE falha."""
    result = await _query_polygon_layer(UC_LAYER, geometry)
    items = []
    for f in result.get("features", []):
        p = f.get("properties", {})
        items.append({
            "nome": p.get("NOME"),
            "categoria": p.get("CATEGORIA"),
            "esfera": p.get("JURISDICAO"),
            "ato_legal": p.get("ATO_LEGAL"),
            "area_oficial_ha": p.get("AREA_OFICI"),
        })
    return {
        "source": "Unidades de Conservação (SEMA-MT / Geoportal)",
        "unverified_source": True,
        "source_note": SOURCE_NOTE,
        "count": len(items),
        "items": items,
        "error": result.get("error"),
    }


async def query_car_app(geometry: dict):
    """Area de Preservacao Permanente (APP) declarada no CAR."""
    result = await _query_polygon_layer(CAR_APP_LAYER, geometry, count=1000)
    items = []
    for f in result.get("features", []):
        p = f.get("properties", {})
        items.append({
            "numero_car": p.get("NUMERO_CAR"),
            "situacao": p.get("SITUACAO"),
            "area_ha": p.get("AREA_HA"),
        })
    total_area = sum(i["area_ha"] for i in items if isinstance(i.get("area_ha"), (int, float)))
    return {
        "source": "CAR - Área de Preservação Permanente declarada (SEMA-MT / SIMCAR)",
        "unverified_source": True,
        "source_note": SOURCE_NOTE,
        "count": len(items),
        "area_total_ha": round(total_area, 4),
        "items": items[:100],
        "error": result.get("error"),
    }


async def query_car_arl(geometry: dict):
    """Reserva Legal (ARL) declarada no CAR."""
    result = await _query_polygon_layer(CAR_ARL_LAYER, geometry, count=1000)
    items = []
    for f in result.get("features", []):
        p = f.get("properties", {})
        items.append({
            "numero_car": p.get("NUMERO_CAR"),
            "situacao": p.get("SITUACAO"),
            "situacao_averbacao": p.get("SITUACAO_AVERBACAO"),
            "situacao_vegetal": p.get("SITUACAO_VEGETAL"),
            "area_ha": p.get("AREA_HA"),
        })
    total_area = sum(i["area_ha"] for i in items if isinstance(i.get("area_ha"), (int, float)))
    return {
        "source": "CAR - Reserva Legal declarada (SEMA-MT / SIMCAR)",
        "unverified_source": True,
        "source_note": SOURCE_NOTE,
        "count": len(items),
        "area_total_ha": round(total_area, 4),
        "items": items[:100],
        "error": result.get("error"),
    }


async def query_area_consolidada(geometry: dict):
    """Area consolidada / uso antropico (base de referencia do CAR)."""
    result = await _query_polygon_layer(AREA_CONSOLIDADA_LAYER, geometry, count=1000)
    items = []
    for f in result.get("features", []):
        p = f.get("properties", {})
        items.append({
            "cod_ibge": p.get("COD_IBGE"),
            "area_ha": p.get("AREA_HA"),
        })
    total_area = sum(i["area_ha"] for i in items if isinstance(i.get("area_ha"), (int, float)))
    return {
        "source": "Área Consolidada / uso antrópico (SEMA-MT / SIMCAR)",
        "unverified_source": True,
        "source_note": SOURCE_NOTE,
        "count": len(items),
        "area_total_ha": round(total_area, 4),
        "items": items[:100],
        "error": result.get("error"),
    }


async def query_autuacoes_fiscalizacao(geometry: dict):
    """Autos de infracao / embargos ambientais lavrados pela SEMA-MT que
    intersectam a area (fiscalizacao ambiental)."""
    result = await _query_polygon_layer(AUTOS_INFRACAO_LAYER, geometry, count=500)
    items = []
    for f in result.get("features", []):
        p = f.get("properties", {})
        items.append({
            "numero_auto_infracao": p.get("NUMERO_AUTO_INFRACAO"),
            "numero_termo_embargo": p.get("NUMERO_TERMO_EMBARGO"),
            "tipo": p.get("TIPO"),
            "subtipo": p.get("SUBTIPO"),
            "data_auto": p.get("DATA_DO_AUTO"),
            "municipio": p.get("MUNICIPIO_DO_DANO"),
            "situacao": p.get("SITUACAO"),
            "embargado": bool(p.get("EMBARGADO")),
            "valor_total_multa": p.get("VALOR_TOTAL_DA_MULTA"),
            "dispositivo_legal_infringido": p.get("DISPOSITIVO_LEGAL_INFRINGIDO"),
        })
    return {
        "source": "Autuações e Embargos Ambientais (SEMA-MT / fiscalização)",
        "unverified_source": True,
        "source_note": SOURCE_NOTE,
        "count": len(items),
        "items": items[:100],
        "error": result.get("error"),
    }


async def get_car_sema_summary(geometry: dict):
    """Agrega as camadas do CAR/SEMA-MT (APP, ARL, Área Consolidada,
    Autuações/Embargos e Unidades de Conservação via SEMA-MT) para o
    poligono. Endpoint dedicado: /api/context/car."""
    import asyncio

    uc_result, app_result, arl_result, consolidada_result, autuacoes_result = await asyncio.gather(
        query_unidades_conservacao_sema(geometry),
        query_car_app(geometry),
        query_car_arl(geometry),
        query_area_consolidada(geometry),
        query_autuacoes_fiscalizacao(geometry),
    )

    alerts = []
    if autuacoes_result.get("count", 0) > 0:
        n = autuacoes_result["count"]
        alerts.append(
            f"⚠️ {n} auto(s) de infração/embargo ambiental da SEMA-MT sobrepõe(m) a área"
        )
    if app_result.get("count", 0) > 0:
        alerts.append(
            f"ℹ️ {app_result['count']} registro(s) de APP declarada no CAR "
            f"({app_result.get('area_total_ha', 0)} ha) na área"
        )
    if arl_result.get("count", 0) > 0:
        alerts.append(
            f"ℹ️ {arl_result['count']} registro(s) de Reserva Legal declarada no CAR "
            f"({arl_result.get('area_total_ha', 0)} ha) na área"
        )

    return {
        "unidades_conservacao_sema": uc_result,
        "car_app": app_result,
        "car_arl": arl_result,
        "area_consolidada": consolidada_result,
        "autuacoes_fiscalizacao": autuacoes_result,
        "alerts": alerts,
        "unverified_source": True,
        "source_note": SOURCE_NOTE,
    }
