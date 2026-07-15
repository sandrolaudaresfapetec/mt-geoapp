"""
Integracao com o Brazil Data Cube (INPE) via STAC API para imagens CBERS-4A.
Acesso 100% aberto: catalogo STAC e download/leitura dos assets COG NAO exigem
token (confirmado). O parametro `token` e mantido apenas por compatibilidade,
mas nao e mais obrigatorio para nenhuma operacao.

Recorte real: em vez de baixar a cena inteira (que pode ter varios GB), os
GeoTIFFs (Cloud Optimized GeoTIFF) sao lidos remotamente via GDAL /vsicurl/,
e apenas a janela (window) correspondente ao poligono desenhado pelo usuario
e efetivamente transferida/decodificada. Para evitar leituras impraticaveis
(areas grandes na resolucao de 2m podem gerar janelas com centenas de milhoes
de pixels), a leitura remota e sempre feita de forma decimada (respeitando um
limite maximo de pixels), e a mascara exata do poligono e aplicada localmente
apos o download da janela ja reduzida.
"""
import io
import os
import asyncio
import httpx
import numpy as np
import rasterio
from rasterio.windows import from_bounds
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from datetime import date as _date, timedelta
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform
from pyproj import Transformer
from PIL import Image

# Configuracoes GDAL para leitura remota eficiente via HTTP range requests
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "YES")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
os.environ.setdefault("VSI_CACHE", "TRUE")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "30")
os.environ.setdefault("GDAL_HTTP_CONNECTTIMEOUT", "15")

STAC_BASE = "https://data.inpe.br/bdc/stac/v1"

# Coleção ampla (WFI): cobertura frequente, 55m, bandas separadas (composicao RGB manual).
WIDE_COLLECTION = "CB4A-WFI-L4-DN-1"
WIDE_BANDS = {"blue": "BAND13", "green": "BAND14", "red": "BAND15"}

# Coleção de alta resolução (WPM fusionado pancromatico+multiespectral): 2m, RGB já pronto (asset 'tci').
HIRES_COLLECTION = "CB4A-WPM-PCA-FUSED-1"
HIRES_ASSET = "tci"

DEFAULT_COLLECTION = WIDE_COLLECTION

# Limite de pixels (lado maior) para a leitura de PREVIEW e para o DOWNLOAD (GeoTIFF).
# Acima disso a imagem e reamostrada (decimada) na propria leitura remota, para
# manter o tempo de resposta em segundos mesmo em conexoes HTTP remotas.
PREVIEW_MAX_DIM = 1200
DOWNLOAD_MAX_DIM = 4000


def _bbox_of(geometry: dict):
    geom = shape(geometry)
    return geom.bounds


async def validate_token(token: str) -> bool:
    """Mantido por compatibilidade com o painel de credenciais; o acesso ao BDC
    e publico e funciona mesmo sem token."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{STAC_BASE}/collections/{DEFAULT_COLLECTION}/items", params={"limit": 1})
    return resp.status_code == 200


async def _stac_search(collection: str, bbox, date_start, date_end, limit=20):
    params = {
        "bbox": ",".join(str(round(v, 6)) for v in bbox),
        "limit": limit,
    }
    if date_start or date_end:
        params["datetime"] = f"{date_start or '2015-01-01'}T00:00:00Z/{date_end or _date.today().isoformat()}T23:59:59Z"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{STAC_BASE}/collections/{collection}/items", params=params)
    if resp.status_code != 200:
        return []
    return resp.json().get("features", [])


async def search_scenes(geometry: dict, date_start, date_end, token=None, collection=None):
    """Busca cenas CBERS-4A que cobrem a area de interesse. Consulta as duas
    colecoes (alta resolucao 2m quando disponivel, e ampla 55m como fallback/
    complemento) e devolve a lista ordenada por data (mais recente primeiro)."""
    bbox = _bbox_of(geometry)
    if not date_start:
        date_start = (_date.today() - timedelta(days=365)).isoformat()
    if not date_end:
        date_end = _date.today().isoformat()

    collections_to_try = [collection] if collection else [HIRES_COLLECTION, WIDE_COLLECTION]

    results = await asyncio.gather(*[
        _stac_search(coll, bbox, date_start, date_end, limit=20) for coll in collections_to_try
    ])

    items = []
    for coll, feats in zip(collections_to_try, results):
        for f in feats:
            props = f.get("properties", {})
            resolution_m = 2 if coll == HIRES_COLLECTION else 55
            items.append({
                "id": f.get("id"),
                "collection": coll,
                "date": props.get("datetime"),
                "cloud_cover": props.get("eo:cloud_cover"),
                "resolution_m": resolution_m,
                "source": "CBERS-4A/WPM (2m, RGB fusionado)" if coll == HIRES_COLLECTION else "CBERS-4A/WFI (55m)",
                "assets": list(f.get("assets", {}).keys()),
            })

    items.sort(key=lambda x: x["date"] or "", reverse=True)
    return items


async def _get_item(item_id: str, collection: str):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{STAC_BASE}/collections/{collection}/items/{item_id}")
    if resp.status_code != 200:
        raise Exception(f"Nao foi possivel obter item {item_id} na colecao {collection}: HTTP {resp.status_code}")
    return resp.json()


def _vsicurl(url: str) -> str:
    return f"/vsicurl/{url}"


def _reproject_geom(geometry: dict, dst_crs):
    geom = shape(geometry)
    transformer = Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True)
    return shp_transform(transformer.transform, geom)


def _percentile_stretch(arr: np.ndarray, low=2, high=98):
    """Aplica realce de contraste (percentil) por banda e converte para uint8,
    apenas para fins de visualizacao (preview). O GeoTIFF de download mantem
    os valores originais (DN), sem essa transformacao."""
    out = np.zeros_like(arr, dtype=np.uint8)
    for i in range(arr.shape[0]):
        band = arr[i].astype(np.float32)
        valid = band[band > 0]
        if valid.size == 0:
            continue
        lo, hi = np.percentile(valid, [low, high])
        if hi <= lo:
            hi = lo + 1
        band = np.clip((band - lo) / (hi - lo) * 255, 0, 255)
        out[i] = band.astype(np.uint8)
    return out


def _read_window_decimated(src, aoi_geom_projected, max_dim, single_band=False, apply_exact_mask=True):
    """Le, de forma decimada (respeitando max_dim no lado maior), a janela do
    raster remoto que cobre o AOI. Sempre calcula a leitura em resolucao reduzida
    quando necessario (out_shape < window real), o que faz o GDAL requisitar bem
    menos dados via HTTP. Em seguida, se apply_exact_mask, aplica a mascara exata
    do poligono (recorte fino, nao so bbox) sobre o array já decimado.
    Retorna (data, transform) onde data tem shape (bands,H,W) ou (H,W) se single_band.
    """
    minx, miny, maxx, maxy = aoi_geom_projected.bounds
    rb = src.bounds
    minx, miny = max(minx, rb.left), max(miny, rb.bottom)
    maxx, maxy = min(maxx, rb.right), min(maxy, rb.top)
    if minx >= maxx or miny >= maxy:
        raise Exception("A área desenhada não intersecta esta cena.")

    window = from_bounds(minx, miny, maxx, maxy, transform=src.transform)
    full_h, full_w = max(1, int(round(window.height))), max(1, int(round(window.width)))

    out_h, out_w = full_h, full_w
    if max_dim and max(out_h, out_w) > max_dim:
        scale = max_dim / max(out_h, out_w)
        out_h, out_w = max(1, int(full_h * scale)), max(1, int(full_w * scale))

    count = 1 if single_band else src.count
    data = src.read(
        window=window,
        out_shape=(count, out_h, out_w) if not single_band else (out_h, out_w),
        resampling=Resampling.bilinear,
    )

    out_transform = src.window_transform(window)
    if out_h != full_h or out_w != full_w:
        scale_x = window.width / out_w
        scale_y = window.height / out_h
        out_transform = out_transform * rasterio.Affine.scale(scale_x, scale_y)

    if apply_exact_mask:
        shape_hw = (out_h, out_w)
        geom_mask = geometry_mask([mapping(aoi_geom_projected)], out_shape=shape_hw, transform=out_transform, invert=True)
        if single_band:
            data = np.where(geom_mask, data, 0)
        else:
            data = np.where(geom_mask[np.newaxis, :, :], data, 0)

    if single_band and data.ndim == 3:
        data = data[0]
    return data, out_transform


def _read_rgb_wide(item: dict, geometry: dict, max_dim, apply_exact_mask=True):
    """Compoe RGB a partir das 3 bandas separadas (Blue/Green/Red) da colecao WFI.
    `geometry` e sempre um dict GeoJSON em EPSG:4326 (formato recebido da API)."""
    assets = item["assets"]
    hrefs = {c: assets[b]["href"] for c, b in WIDE_BANDS.items() if b in assets}
    if len(hrefs) < 3:
        raise Exception("Item WFI nao possui as 3 bandas RGB necessarias.")

    channels = {}
    common_transform = None
    common_crs = None
    for chan in ("red", "green", "blue"):
        with rasterio.open(_vsicurl(hrefs[chan])) as src:
            if common_crs is None:
                common_crs = src.crs
            aoi_proj = _reproject_geom(geometry, src.crs)
            arr, transform_out = _read_window_decimated(src, aoi_proj, max_dim, single_band=True, apply_exact_mask=apply_exact_mask)
            channels[chan] = arr
            if common_transform is None:
                common_transform = transform_out
    stacked = np.stack([channels["red"], channels["green"], channels["blue"]], axis=0)
    return stacked, common_transform, common_crs


async def get_preview_png(item_id: str, geometry: dict, token=None, max_dim=None, collection=None):
    """Gera um preview PNG recortado (nao a cena inteira) usando leitura remota
    decimada do COG, com realce de contraste apenas para visualizacao."""
    max_dim = max_dim or PREVIEW_MAX_DIM
    if not item_id or not collection:
        items = await search_scenes(geometry, None, None)
        if not items:
            raise Exception("Nenhuma cena CBERS-4A encontrada para a area/periodo informado.")
        item_id = item_id or items[0]["id"]
        collection = collection or items[0]["collection"]

    item = await _get_item(item_id, collection)

    def _work():
        if collection == HIRES_COLLECTION:
            with rasterio.open(_vsicurl(item["assets"][HIRES_ASSET]["href"])) as src:
                aoi_proj = _reproject_geom(geometry, src.crs)
                data, _ = _read_window_decimated(src, aoi_proj, max_dim, apply_exact_mask=True)
            return data, 2, "CBERS-4A/WPM (INPE Brazil Data Cube) — 2m RGB fusionado"
        else:
            data, _, _ = _read_rgb_wide(item, geometry, max_dim, apply_exact_mask=True)
            return data, 55, "CBERS-4A/WFI (INPE Brazil Data Cube) — 55m"

    data, resolution_m, source_label = await asyncio.wait_for(asyncio.to_thread(_work), timeout=60)

    rgb_u8 = _percentile_stretch(data)
    img = Image.fromarray(np.transpose(rgb_u8, (1, 2, 0)), mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    props = item.get("properties", {})
    meta = {
        "item_id": item_id,
        "collection": collection,
        "date": props.get("datetime"),
        "resolution_m": resolution_m,
        "source": source_label,
        "clipped_to_aoi": True,
        "fetched_at": _now_iso(),
    }
    return buf.getvalue(), meta


async def get_geotiff_clip(item_id: str, geometry: dict, token=None, collection=None):
    """Baixa (via leitura remota decimada + recorte preciso pelo poligono) apenas
    a area de interesse, entregando um GeoTIFF georreferenciado com os valores
    originais (sem realce de contraste), pronto para uso em QGIS/ArcGIS.
    Se a area desenhada for muito grande para a resolucao nativa, a imagem e
    entregue reamostrada (decimada) respeitando DOWNLOAD_MAX_DIM pixels no lado
    maior — isso e informado no nome do arquivo e nos metadados/tags do GeoTIFF."""
    if not item_id or not collection:
        items = await search_scenes(geometry, None, None)
        if not items:
            raise Exception("Nenhuma cena CBERS-4A encontrada para a area/periodo informado.")
        item_id = item_id or items[0]["id"]
        collection = collection or items[0]["collection"]

    item = await _get_item(item_id, collection)

    def _work():
        if collection == HIRES_COLLECTION:
            href = item["assets"][HIRES_ASSET]["href"]
            with rasterio.open(_vsicurl(href)) as src:
                aoi_proj = _reproject_geom(geometry, src.crs)
                data, out_transform = _read_window_decimated(src, aoi_proj, DOWNLOAD_MAX_DIM, apply_exact_mask=True)
                profile = src.profile.copy()
                crs = src.crs
            band_desc = "RGB (2m nativo; reamostrado se a area exceder o limite de pixels do servico)"
        else:
            data, out_transform, crs = _read_rgb_wide(item, geometry, DOWNLOAD_MAX_DIM, apply_exact_mask=True)
            with rasterio.open(_vsicurl(item["assets"][WIDE_BANDS["red"]]["href"])) as ref:
                profile = ref.profile.copy()
            band_desc = "RGB (55m nativo; composicao Blue/Green/Red - Bandas 13/14/15)"
        return data, out_transform, profile, crs, band_desc

    data, out_transform, profile, crs, band_desc = await asyncio.wait_for(asyncio.to_thread(_work), timeout=90)

    profile.update({
        "height": data.shape[-2],
        "width": data.shape[-1],
        "count": data.shape[0] if data.ndim == 3 else 1,
        "transform": out_transform,
        "crs": crs,
        "driver": "GTiff",
        "compress": "deflate",
    })

    buf = io.BytesIO()
    with rasterio.io.MemoryFile() as memfile:
        with memfile.open(**profile) as dst:
            if data.ndim == 2:
                dst.write(data, 1)
            else:
                dst.write(data)
            dst.update_tags(
                fonte="INPE Brazil Data Cube (STAC, acesso publico)",
                item_id=item_id,
                colecao=collection,
                bandas=band_desc,
                gerado_em=_now_iso(),
            )
        buf.write(memfile.read())

    filename = f"cbers4a_{item_id}_clip.tif"
    return buf.getvalue(), filename


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
