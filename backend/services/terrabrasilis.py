"""
Integracao com TerraBrasilis (INPE) - GeoServer WFS publico.
DETER (alertas quase em tempo real) e PRODES (desmatamento anual consolidado).
Nenhuma credencial e necessaria - dados publicos.
"""
import httpx
from shapely.geometry import shape

GEOSERVER_HOST = "https://terrabrasilis.dpi.inpe.br/geoserver"

# workspace:layer para cada bioma / dataset
DETER_LAYERS = {
    "amazonia": ("deter-amz", "deter_amz"),
    "cerrado": ("deter-cerrado", "deter_cerrado"),
}

PRODES_LAYERS = {
    "amazonia": ("prodes-amazon-nb", "yearly_deforestation_biome"),
    "cerrado": ("prodes-cerrado-nb", "yearly_deforestation"),
    "pantanal": ("prodes-pantanal-nb", "yearly_deforestation"),
}


def _bbox_of(geometry: dict):
    geom = shape(geometry)
    return geom.bounds  # minx, miny, maxx, maxy


def _wkt_polygon(geometry: dict) -> str:
    geom = shape(geometry)
    return geom.wkt


async def _wfs_getfeature(workspace: str, layer: str, geometry: dict, extra_cql: str = None,
                            date_start=None, date_end=None, date_field="view_date"):
    bbox = _bbox_of(geometry)
    minx, miny, maxx, maxy = bbox

    cql_parts = [f"BBOX({_geom_field_guess(layer)},{minx},{miny},{maxx},{maxy},'EPSG:4326')"]
    if date_start and date_end:
        cql_parts.append(f"{date_field} BETWEEN '{date_start}' AND '{date_end}'")
    if extra_cql:
        cql_parts.append(extra_cql)
    cql_filter = " AND ".join(cql_parts)

    url = f"{GEOSERVER_HOST}/{workspace}/{layer}/wfs"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": f"{workspace}:{layer}",
        "outputFormat": "application/json",
        "cql_filter": cql_filter,
        "srsName": "EPSG:4674",
        "count": 2000,
    }
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.get(url, params=params)
    if resp.status_code != 200:
        # fallback: tenta sem CQL BBOX field guess (algumas layers usam 'geom')
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:300], "features": []}
    try:
        return resp.json()
    except Exception:
        return {"error": "resposta nao-JSON", "features": []}


def _geom_field_guess(layer: str) -> str:
    return "geom"


async def query_deter(geometry: dict, date_start=None, date_end=None, bioma="amazonia"):
    workspace, layer = DETER_LAYERS.get(bioma, DETER_LAYERS["amazonia"])
    if not date_start:
        from datetime import date, timedelta
        date_start = (date.today() - timedelta(days=365)).isoformat()
    if not date_end:
        from datetime import date
        date_end = date.today().isoformat()

    data = await _wfs_getfeature(workspace, layer, geometry, date_start=date_start, date_end=date_end,
                                   date_field="view_date")
    features = data.get("features", [])
    total_area = sum((f.get("properties", {}).get("area_km") or f.get("properties", {}).get("areamunkm") or 0)
                      for f in features)
    by_class = {}
    for f in features:
        cls = f.get("properties", {}).get("classname", "DESCONHECIDO")
        by_class[cls] = by_class.get(cls, 0) + 1

    return {
        "source": "DETER (TerraBrasilis / INPE)",
        "bioma": bioma,
        "period": [date_start, date_end],
        "alert_count": len(features),
        "total_area_km2": round(total_area, 3),
        "by_class": by_class,
        "features": features[:200],  # limita payload
    }


async def query_prodes(geometry: dict, bioma="amazonia"):
    workspace, layer = PRODES_LAYERS.get(bioma, PRODES_LAYERS["amazonia"])
    data = await _wfs_getfeature(workspace, layer, geometry)
    features = data.get("features", [])
    by_year = {}
    total_area = 0
    for f in features:
        props = f.get("properties", {})
        year = props.get("year") or props.get("ano")
        area = props.get("area_km") or props.get("areakm") or 0
        total_area += area
        if year:
            by_year[str(year)] = by_year.get(str(year), 0) + area

    return {
        "source": "PRODES (TerraBrasilis / INPE)",
        "bioma": bioma,
        "polygon_count": len(features),
        "total_deforested_area_km2": round(total_area, 3),
        "by_year_km2": {k: round(v, 3) for k, v in by_year.items()},
        "features": features[:200],
    }
