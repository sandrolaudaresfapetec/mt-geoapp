"""
Integracao com Copernicus Data Space Ecosystem (Sentinel-2).
Usa OAuth2 client_credentials + Sentinel Hub Process API + Catalog (STAC) para
busca de cenas, preview e download recortado pela area de interesse (AOI).

Recorte real: a Process API do Sentinel Hub aceita bbox como area de saida.
Para respeitar o poligono exato desenhado pelo usuario (nao apenas o retangulo
envolvente), pedimos a imagem em GeoTIFF georreferenciado cobrindo a bbox e, em
seguida, aplicamos localmente uma mascara (rasterio) com os pixels fora do
poligono zerados — igual à abordagem usada para o CBERS-4A/INPE.
"""
import io
import time
import asyncio
import httpx
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.io import MemoryFile
from shapely.geometry import shape, mapping
from PIL import Image

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
SH_BASE = "https://sh.dataspace.copernicus.eu"
CATALOG_URL = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"

# Resolucao nativa das bandas usadas (B02/B03/B04) e limites de saida para
# manter tempos de resposta razoaveis mesmo em areas grandes.
NATIVE_RESOLUTION_M = 10
PREVIEW_MAX_DIM = 1200
DOWNLOAD_MAX_DIM = 2500  # Sentinel Hub Process API limita a 2500px por eixo por request

_token_cache = {}  # key: client_id -> (token, expires_at)


class CopernicusAuthError(Exception):
    pass


async def get_token(client_id: str, client_secret: str) -> str:
    cached = _token_cache.get(client_id)
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        raise CopernicusAuthError(
            f"Falha na autenticacao Copernicus (HTTP {resp.status_code}). "
            "Verifique client_id/client_secret nas Configuracoes."
        )
    data = resp.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", 300)
    _token_cache[client_id] = (token, time.time() + expires_in)
    return token


def _bbox_of(geometry: dict):
    geom = shape(geometry)
    return geom.bounds  # (minx, miny, maxx, maxy) em WGS84 (graus)


def _meters_per_degree_lat():
    return 111320.0


def _meters_per_degree_lon(lat_deg):
    import math
    return 111320.0 * math.cos(math.radians(lat_deg))


def _clip_bbox(bbox, max_deg=3.0):
    """Limita a bbox para evitar requests absurdamente grandes (Sentinel Hub
    tem limite pratico de area por request de imagem em resolucao alta)."""
    minx, miny, maxx, maxy = bbox
    if (maxx - minx) > max_deg or (maxy - miny) > max_deg:
        raise ValueError(
            f"Área desenhada excede o limite máximo permitido para download direto (~{max_deg}°). "
            "Desenhe uma área menor ou reduza a resolução solicitada."
        )
    return bbox


def _output_size_for_bbox(bbox, max_dim, resolution_m=NATIVE_RESOLUTION_M):
    """Calcula width/height proporcionais ao tamanho real da area (mantendo a
    resolucao nativa quando possivel), respeitando um limite maximo de pixels
    por eixo (max_dim) para nao exceder os limites da Process API nem deixar
    a requisicao lenta demais."""
    minx, miny, maxx, maxy = bbox
    lat_mid = (miny + maxy) / 2
    width_m = (maxx - minx) * _meters_per_degree_lon(lat_mid)
    height_m = (maxy - miny) * _meters_per_degree_lat()

    width_px = max(1, int(round(width_m / resolution_m)))
    height_px = max(1, int(round(height_m / resolution_m)))

    scale = min(1.0, max_dim / max(width_px, height_px)) if max(width_px, height_px) > max_dim else 1.0
    width_px = max(1, int(width_px * scale))
    height_px = max(1, int(height_px * scale))
    effective_resolution_m = resolution_m / scale if scale > 0 else resolution_m
    return width_px, height_px, effective_resolution_m


async def search_scenes(geometry: dict, date_start, date_end, client_id, client_secret):
    token = await get_token(client_id, client_secret)
    bbox = _bbox_of(geometry)
    body = {
        "collections": ["sentinel-2-l2a"],
        "datetime": f"{date_start or '2023-01-01'}T00:00:00Z/{date_end or _today()}T23:59:59Z",
        "bbox": list(bbox),
        "limit": 20,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            CATALOG_URL,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise Exception(f"Catalog search falhou: HTTP {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    items = []
    for f in data.get("features", []):
        props = f.get("properties", {})
        items.append({
            "id": f.get("id"),
            "date": props.get("datetime"),
            "cloud_cover": props.get("eo:cloud_cover"),
            "resolution_m": NATIVE_RESOLUTION_M,
            "source": "Sentinel-2 L2A (Copernicus Data Space)",
        })
    items.sort(key=lambda x: x.get("cloud_cover") or 100)
    return items


def _today():
    from datetime import date
    return date.today().isoformat()


EVALSCRIPT_TRUE_COLOR = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B02", "B03", "B04"] }],
    output: { bands: 3, sampleType: "UINT8" }
  };
}
function evaluatePixel(sample) {
  return [
    Math.min(255, sample.B04 * 255 * 3.5),
    Math.min(255, sample.B03 * 255 * 3.5),
    Math.min(255, sample.B02 * 255 * 3.5),
  ];
}
"""

# Evalscript para o GeoTIFF de download: mantem reflectancia (float32), sem
# realce/stretch, para uso analitico real (nao apenas visualizacao).
EVALSCRIPT_ANALYTIC = """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B02", "B03", "B04"] }],
    output: { bands: 3, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(sample) {
  return [sample.B04, sample.B03, sample.B02];
}
"""


async def _process_request(geometry, date, client_id, client_secret, out_format, max_dim, evalscript):
    token = await get_token(client_id, client_secret)
    bbox = _clip_bbox(_bbox_of(geometry))
    minx, miny, maxx, maxy = bbox

    if date:
        # 'date' pode vir como data simples ('2026-07-04') ou timestamp ISO
        # completo do STAC ('2026-07-04T14:05:54.301Z'). Normalizamos para
        # extrair apenas a parte YYYY-MM-DD antes de montar o timeRange.
        date_only = str(date).split("T")[0]
        time_from = f"{date_only}T00:00:00Z"
        time_to = f"{date_only}T23:59:59Z"
    else:
        from datetime import date as d, timedelta
        end = d.today()
        start = end - timedelta(days=30)
        time_from = f"{start.isoformat()}T00:00:00Z"
        time_to = f"{end.isoformat()}T23:59:59Z"

    width, height, effective_res = _output_size_for_bbox(bbox, max_dim)
    width = min(width, DOWNLOAD_MAX_DIM)
    height = min(height, DOWNLOAD_MAX_DIM)

    payload = {
        "input": {
            "bounds": {
                "bbox": [minx, miny, maxx, maxy],
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": time_from, "to": time_to},
                    "mosaickingOrder": "leastCC",
                    "maxCloudCoverage": 60,
                },
            }],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [{"identifier": "default", "format": {"type": out_format}}],
        },
        "evalscript": evalscript,
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{SH_BASE}/api/v1/process",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    if resp.status_code != 200:
        raise Exception(f"Process API falhou: HTTP {resp.status_code} {resp.text[:300]}")
    meta = {
        "bbox": [minx, miny, maxx, maxy],
        "time_range": [time_from, time_to],
        "resolution_m": round(effective_res, 2),
        "width": width,
        "height": height,
        "source": "Sentinel-2 L2A (Copernicus Data Space Ecosystem)",
        "fetched_at": _now_iso(),
    }
    return resp.content, meta


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _mask_geotiff_to_polygon(tiff_bytes: bytes, geometry: dict):
    """Recebe um GeoTIFF (bbox retangular) e retorna uma copia com os pixels
    fora do poligono exato zerados (recorte fino, nao so bbox), preservando
    georreferenciamento e metadados."""
    geom = shape(geometry)
    with MemoryFile(tiff_bytes) as memfile:
        with memfile.open() as src:
            data = src.read()
            profile = src.profile.copy()
            mask_arr = geometry_mask([mapping(geom)], out_shape=(src.height, src.width), transform=src.transform, invert=True)
            data = np.where(mask_arr[np.newaxis, :, :], data, 0)

        out_buf = io.BytesIO()
        with MemoryFile() as out_memfile:
            with out_memfile.open(**profile) as dst:
                dst.write(data)
                dst.update_tags(
                    fonte="Copernicus Data Space Ecosystem (Sentinel-2 L2A)",
                    recorte="poligono exato (mascara aplicada localmente)",
                    gerado_em=_now_iso(),
                )
            out_buf.write(out_memfile.read())
        return out_buf.getvalue()


async def get_preview_png(geometry, date, client_id, client_secret, max_dim=1024):
    """Preview PNG (RGB 8-bit com realce visual) recortado pelo poligono exato."""
    max_dim = min(max_dim, PREVIEW_MAX_DIM)
    tiff_bytes, meta = await _process_request(
        geometry, date, client_id, client_secret, "image/tiff", max_dim, EVALSCRIPT_TRUE_COLOR
    )
    masked_tiff = await asyncio.to_thread(_mask_geotiff_to_polygon, tiff_bytes, geometry)

    def _to_png(tif_bytes):
        with MemoryFile(tif_bytes) as memfile:
            with memfile.open() as src:
                arr = src.read()  # (3,H,W) uint8
        img = Image.fromarray(np.transpose(arr, (1, 2, 0)), mode="RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    png_bytes = await asyncio.to_thread(_to_png, masked_tiff)
    meta["clipped_to_aoi"] = True
    return png_bytes, meta


async def get_geotiff(geometry, date, client_id, client_secret):
    """GeoTIFF analitico (reflectancia float32, sem realce) recortado pelo
    poligono exato do usuario, pronto para uso em QGIS/ArcGIS/analises."""
    tiff_bytes, meta = await _process_request(
        geometry, date, client_id, client_secret, "image/tiff", DOWNLOAD_MAX_DIM, EVALSCRIPT_ANALYTIC
    )
    masked_tiff = await asyncio.to_thread(_mask_geotiff_to_polygon, tiff_bytes, geometry)
    fname = f"sentinel2_{date or 'ultimo'}_clip.tiff"
    return masked_tiff, fname
