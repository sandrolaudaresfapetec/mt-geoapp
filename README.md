# MT GeoApp

Sistema de gestão e download de imagens de satélite (Sentinel-2, CBERS-4A) e dados de
desmatamento (DETER/PRODES) para áreas de interesse em Mato Grosso, com geração de
relatório PDF consolidado incluindo contexto socioambiental (Terras Indígenas,
Unidades de Conservação e focos de calor).

## Arquitetura

- **Backend**: FastAPI (Python), servindo API REST e arquivos estáticos do frontend.
- **Frontend**: HTML/JS puro com Leaflet (mapa) e Turf.js (geometria).
- **Fontes de dados**:
  - [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/) — Sentinel-2 L2A (busca, preview, download GeoTIFF). Credenciais fornecidas pelo próprio usuário no painel de configurações do frontend (não armazenadas no servidor).
  - [INPE Brazil Data Cube](https://data.inpe.br/bdc/) — CBERS-4A/WFI.
  - [TerraBrasilis (INPE)](https://terrabrasilis.dpi.inpe.br/) — DETER, PRODES, e camadas de contexto socioambiental (Terras Indígenas, Unidades de Conservação, focos de calor), via WFS/GeoServer público.

## Estrutura do projeto

```
mt-geoapp/
├── Dockerfile
├── fly.toml
├── backend/
│   ├── main.py                 # API FastAPI (endpoints)
│   ├── requirements.txt
│   └── services/
│       ├── copernicus.py       # Integração Sentinel-2 / Copernicus Data Space
│       ├── inpe_bdc.py         # Integração CBERS-4A / INPE Brazil Data Cube
│       ├── terrabrasilis.py    # DETER / PRODES
│       ├── context_layers.py   # Contexto socioambiental (TI / UC / focos de calor)
│       └── pdf_report.py       # Geração do relatório PDF consolidado
└── frontend/
    ├── index.html
    └── static/
        ├── app.js
        └── style.css
```

## Executando localmente

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Acesse `http://localhost:8000`.

As credenciais do Copernicus Data Space Ecosystem são inseridas pelo próprio usuário
no painel "⚙️ Configurações" do frontend (ficam salvas apenas no `localStorage` do
navegador) — não é necessário configurar variáveis de ambiente no servidor para isso.

## Deploy (Fly.io)

```bash
flyctl deploy --remote-only
```

App configurado em `fly.toml` (app `mt-geoapp`, região `gru`).

## Principais endpoints

| Método | Rota | Descrição |
|---|---|---|
| GET | `/api/health` | Health check |
| POST | `/api/sentinel2/search` | Busca cenas Sentinel-2 |
| POST | `/api/sentinel2/preview` | Preview PNG recortado à AOI |
| POST | `/api/sentinel2/download` | Download GeoTIFF |
| POST | `/api/cbers4a/search` \| `/preview` \| `/download` | Idem para CBERS-4A |
| POST | `/api/deforestation/deter` | Alertas DETER (near real-time) |
| POST | `/api/deforestation/prodes` | Desmatamento anual PRODES |
| POST | `/api/context/summary` | Contexto socioambiental (TI/UC/focos de calor) |
| POST | `/api/credentials/validate` | Valida credenciais Copernicus/INPE |
| POST | `/api/report/generate` | Gera relatório PDF consolidado |

## Notas

- A camada federal de Unidades de Conservação do GeoServer do INPE
  (`uc_f_nao_reservas`) pode retornar erro 500 intermitente do lado do provedor;
  quando isso ocorre, o relatório sinaliza explicitamente "dados parciais" para não
  ser confundido com "ausência confirmada de UC".
