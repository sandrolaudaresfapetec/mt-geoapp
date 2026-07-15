"""
MT GeoApp - WebGIS para download de imagens de satelite em Mato Grosso
Backend FastAPI - proxy/orquestrador para Copernicus (Sentinel-2), INPE BDC (CBERS-4A)
e TerraBrasilis (DETER/PRODES).
"""
import os
import io
import json
import time
import base64
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from services import copernicus, inpe_bdc, terrabrasilis, pdf_report, context_layers, sema_mt

app = FastAPI(title="MT GeoApp", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- MODELS ----------

class Geometry(BaseModel):
    type: str
    coordinates: list


class CredentialsPayload(BaseModel):
    copernicus_client_id: Optional[str] = None
    copernicus_client_secret: Optional[str] = None
    inpe_bdc_token: Optional[str] = None


class SearchRequest(BaseModel):
    geometry: Geometry
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    credentials: Optional[CredentialsPayload] = None


class ImageRequestItem(BaseModel):
    source: str  # 'sentinel2' | 'cbers4a'
    item_id: Optional[str] = None
    collection: Optional[str] = None  # colecao STAC do CBERS-4A (2m ou 55m); auto-detectada se omitida
    date: Optional[str] = None
    geometry: Geometry
    credentials: Optional[CredentialsPayload] = None
    max_dim: Optional[int] = 1024


class ReportRequest(BaseModel):
    geometry: Geometry
    area_km2: float
    perimeter_km: float
    centroid: list
    images: list  # list of {source, date, resolution, preview_base64, note}
    deter_summary: Optional[dict] = None
    prodes_summary: Optional[dict] = None
    context_summary: Optional[dict] = None  # TI / UC / focos de calor
    car_sema_summary: Optional[dict] = None  # CAR/UC/fiscalizacao via SEMA-MT (fonte nao oficial)
    generated_at: Optional[str] = None


class ContextRequest(BaseModel):
    geometry: Geometry
    focos_days: Optional[int] = 30


# ---------- HEALTH ----------

@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# ---------- SENTINEL-2 (Copernicus Data Space) ----------

@app.post("/api/sentinel2/search")
async def search_sentinel2(req: SearchRequest):
    if not req.credentials or not req.credentials.copernicus_client_id or not req.credentials.copernicus_client_secret:
        raise HTTPException(400, "Credenciais Copernicus (client_id/client_secret) sao obrigatorias.")
    try:
        items = await copernicus.search_scenes(
            geometry=req.geometry.dict(),
            date_start=req.date_start,
            date_end=req.date_end,
            client_id=req.credentials.copernicus_client_id,
            client_secret=req.credentials.copernicus_client_secret,
        )
        return {"source": "sentinel2", "count": len(items), "items": items}
    except copernicus.CopernicusAuthError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"Erro ao consultar Sentinel-2: {e}")


@app.post("/api/sentinel2/preview")
async def preview_sentinel2(req: ImageRequestItem):
    if not req.credentials or not req.credentials.copernicus_client_id or not req.credentials.copernicus_client_secret:
        raise HTTPException(400, "Credenciais Copernicus sao obrigatorias.")
    try:
        png_bytes, meta = await copernicus.get_preview_png(
            geometry=req.geometry.dict(),
            date=req.date,
            client_id=req.credentials.copernicus_client_id,
            client_secret=req.credentials.copernicus_client_secret,
            max_dim=req.max_dim or 1024,
        )
        b64 = base64.b64encode(png_bytes).decode()
        return {"preview_base64": f"data:image/png;base64,{b64}", "meta": meta}
    except copernicus.CopernicusAuthError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"Erro ao gerar preview Sentinel-2: {e}")


@app.post("/api/sentinel2/download")
async def download_sentinel2(req: ImageRequestItem):
    if not req.credentials or not req.credentials.copernicus_client_id or not req.credentials.copernicus_client_secret:
        raise HTTPException(400, "Credenciais Copernicus sao obrigatorias.")
    try:
        tiff_bytes, filename = await copernicus.get_geotiff(
            geometry=req.geometry.dict(),
            date=req.date,
            client_id=req.credentials.copernicus_client_id,
            client_secret=req.credentials.copernicus_client_secret,
        )
        return StreamingResponse(
            io.BytesIO(tiff_bytes),
            media_type="image/tiff",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except copernicus.CopernicusAuthError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"Erro ao baixar GeoTIFF Sentinel-2: {e}")


# ---------- CBERS-4A (INPE Brazil Data Cube) ----------

@app.post("/api/cbers4a/search")
async def search_cbers4a(req: SearchRequest):
    # Acesso STAC do Brazil Data Cube (INPE) é público — nenhum token é necessário.
    try:
        items = await inpe_bdc.search_scenes(
            geometry=req.geometry.dict(),
            date_start=req.date_start,
            date_end=req.date_end,
        )
        return {"source": "cbers4a", "count": len(items), "items": items}
    except Exception as e:
        raise HTTPException(502, f"Erro ao consultar CBERS-4A: {e}")


@app.post("/api/cbers4a/preview")
async def preview_cbers4a(req: ImageRequestItem):
    try:
        png_bytes, meta = await inpe_bdc.get_preview_png(
            item_id=req.item_id,
            geometry=req.geometry.dict(),
            max_dim=req.max_dim or 1024,
            collection=req.collection,
        )
        b64 = base64.b64encode(png_bytes).decode()
        return {"preview_base64": f"data:image/png;base64,{b64}", "meta": meta}
    except Exception as e:
        raise HTTPException(502, f"Erro ao gerar preview CBERS-4A: {e}")


@app.post("/api/cbers4a/download")
async def download_cbers4a(req: ImageRequestItem):
    try:
        tiff_bytes, filename = await inpe_bdc.get_geotiff_clip(
            item_id=req.item_id,
            geometry=req.geometry.dict(),
            collection=req.collection,
        )
        return StreamingResponse(
            io.BytesIO(tiff_bytes),
            media_type="image/tiff",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        raise HTTPException(502, f"Erro ao baixar GeoTIFF CBERS-4A: {e}")


# ---------- DETER / PRODES (TerraBrasilis) ----------

@app.post("/api/deforestation/deter")
async def get_deter(req: SearchRequest):
    try:
        data = await terrabrasilis.query_deter(
            geometry=req.geometry.dict(),
            date_start=req.date_start,
            date_end=req.date_end,
        )
        return data
    except Exception as e:
        raise HTTPException(502, f"Erro ao consultar DETER: {e}")


@app.post("/api/deforestation/prodes")
async def get_prodes(req: SearchRequest):
    try:
        data = await terrabrasilis.query_prodes(
            geometry=req.geometry.dict(),
        )
        return data
    except Exception as e:
        raise HTTPException(502, f"Erro ao consultar PRODES: {e}")


# ---------- CONTEXTO SOCIOAMBIENTAL (TI / UC / Focos de calor) ----------

@app.post("/api/context/summary")
async def get_context_summary(req: ContextRequest):
    """Agrega, para o poligono desenhado: Terras Indigenas, Unidades de
    Conservacao (federais/estaduais) e focos de calor recentes que
    intersectam a area. Dados publicos do GeoServer do TerraBrasilis/INPE,
    o mesmo backend usado pelo painel de alertas da SEMA-MT."""
    try:
        data = await context_layers.get_context_summary(
            geometry=req.geometry.dict(),
            focos_days=req.focos_days or 30,
        )
        return data
    except Exception as e:
        raise HTTPException(502, f"Erro ao consultar contexto socioambiental: {e}")


# ---------- CAMADAS ADICIONAIS: CAR / SEMA-MT ----------

@app.post("/api/context/car")
async def get_car_sema_context(req: ContextRequest):
    """Agrega, para o poligono desenhado: Unidades de Conservacao (via
    Geoportal SEMA-MT), Area de Preservacao Permanente (CAR_APP), Reserva
    Legal (CAR_ARL), Area Consolidada/uso antropico e Autuacoes/Embargos
    ambientais da SEMA-MT que intersectam a area.

    ATENCAO: estas camadas sao obtidas via um GeoServer da SEMA-MT cujo
    acesso depende de uma chave (authkey) localizada em documentacao
    tecnica de terceiros, SEM confirmacao oficial da SEMA-MT sobre
    estabilidade ou termos de uso. Use como informacao complementar as
    fontes oficiais (TerraBrasilis/INPE) exibidas em /api/context/summary.
    """
    try:
        data = await sema_mt.get_car_sema_summary(geometry=req.geometry.dict())
        return data
    except Exception as e:
        raise HTTPException(502, f"Erro ao consultar camadas SEMA-MT/CAR: {e}")


# ---------- CREDENTIAL VALIDATION ----------

@app.post("/api/credentials/validate")
async def validate_credentials(payload: CredentialsPayload):
    result = {"copernicus": None, "inpe_bdc": None}
    if payload.copernicus_client_id and payload.copernicus_client_secret:
        try:
            await copernicus.get_token(payload.copernicus_client_id, payload.copernicus_client_secret)
            result["copernicus"] = {"valid": True}
        except Exception as e:
            result["copernicus"] = {"valid": False, "error": str(e)}
    if payload.inpe_bdc_token:
        try:
            valid = await inpe_bdc.validate_token(payload.inpe_bdc_token)
            result["inpe_bdc"] = {"valid": valid}
        except Exception as e:
            result["inpe_bdc"] = {"valid": False, "error": str(e)}
    else:
        # BDC nao exige token; confirma apenas que o catalogo publico esta acessivel.
        try:
            result["inpe_bdc"] = {"valid": await inpe_bdc.validate_token(None), "note": "Acesso publico, sem necessidade de token."}
        except Exception as e:
            result["inpe_bdc"] = {"valid": False, "error": str(e)}
    return result


# ---------- PDF REPORT ----------

@app.post("/api/report/generate")
async def generate_report(req: ReportRequest):
    try:
        pdf_bytes = pdf_report.build_pdf(req.dict())
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="relatorio_mt_geoapp.pdf"'},
        )
    except Exception as e:
        raise HTTPException(500, f"Erro ao gerar PDF: {e}")


# ---------- STATIC FRONTEND ----------

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
