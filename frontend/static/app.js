/* MT GeoApp — frontend logic */
const API_BASE = ""; // same origin

// ---------- MAP SETUP ----------
const map = L.map("map", { zoomControl: true }).setView([-12.6, -55.7], 6); // centro aproximado de MT

const baseLayers = {
  "Satélite (Esri)": L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { attribution: "Esri, Maxar, Earthstar Geographics", maxZoom: 19 }
  ),
  "OpenStreetMap": L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors", maxZoom: 19,
  }),
  "Topográfico (OpenTopoMap)": L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    attribution: "Map data: © OpenStreetMap contributors, SRTM | Style: © OpenTopoMap (CC-BY-SA)", maxZoom: 17,
  }),
};
baseLayers["Satélite (Esri)"].addTo(map);
L.control.layers(baseLayers, {}, { position: "topleft" }).addTo(map);

// Limite aproximado do estado de Mato Grosso (retângulo largo apenas para orientação inicial)
const mtBounds = [[-18.1, -61.6], [-7.3, -50.1]];
L.rectangle(mtBounds, { color: "#2e7d4f", weight: 1, fillOpacity: 0, dashArray: "6,6" }).addTo(map);

// ---------- DRAW CONTROL ----------
const drawnItems = new L.FeatureGroup();
map.addLayer(drawnItems);

const drawControl = new L.Control.Draw({
  position: "topright",
  draw: {
    polygon: { allowIntersection: false, showArea: true, shapeOptions: { color: "#ff7a00" } },
    rectangle: { shapeOptions: { color: "#ff7a00" } },
    circle: false,
    circlemarker: false,
    marker: false,
    polyline: false,
  },
  edit: { featureGroup: drawnItems, remove: true },
});
map.addControl(drawControl);

let currentGeometry = null; // GeoJSON geometry

map.on(L.Draw.Event.CREATED, (e) => {
  drawnItems.clearLayers();
  drawnItems.addLayer(e.layer);
  updateGeometryFromLayer(e.layer);
});
map.on(L.Draw.Event.EDITED, (e) => {
  e.layers.eachLayer((layer) => updateGeometryFromLayer(layer));
});
map.on(L.Draw.Event.DELETED, () => {
  currentGeometry = null;
  toggleAreaStats(false);
  document.getElementById("btn-search").disabled = true;
  document.getElementById("btn-report").disabled = true;
});

function updateGeometryFromLayer(layer) {
  const geojson = layer.toGeoJSON();
  currentGeometry = geojson.geometry;
  const areaM2 = turf.area(geojson);
  const areaKm2 = areaM2 / 1e6;
  const perimeterKm = turf.length(turf.polygonToLine(geojson), { units: "kilometers" });
  const centroid = turf.centroid(geojson).geometry.coordinates;

  document.getElementById("stat-area").textContent = `${areaKm2.toFixed(3)} km²`;
  document.getElementById("stat-perimeter").textContent = `${perimeterKm.toFixed(3)} km`;
  document.getElementById("stat-centroid").textContent = `${centroid[0].toFixed(5)}, ${centroid[1].toFixed(5)}`;
  toggleAreaStats(true);

  window.__areaKm2 = areaKm2;
  window.__perimeterKm = perimeterKm;
  window.__centroid = centroid;

  document.getElementById("btn-search").disabled = false;
}

function toggleAreaStats(show) {
  document.getElementById("area-stats").classList.toggle("hidden", !show);
  document.getElementById("btn-clear-geom").classList.toggle("hidden", !show);
}

document.getElementById("btn-clear-geom").addEventListener("click", () => {
  drawnItems.clearLayers();
  currentGeometry = null;
  toggleAreaStats(false);
  document.getElementById("btn-search").disabled = true;
  document.getElementById("btn-report").disabled = true;
});

// ---------- CREDENTIALS (localStorage) ----------
function loadCreds() {
  return JSON.parse(localStorage.getItem("mtgeoapp_creds") || "{}");
}
function saveCreds(obj) {
  localStorage.setItem("mtgeoapp_creds", JSON.stringify(obj));
}

const settingsModal = document.getElementById("settings-modal");
document.getElementById("btn-settings").addEventListener("click", () => {
  const c = loadCreds();
  document.getElementById("cred-cop-id").value = c.copernicus_client_id || "";
  document.getElementById("cred-cop-secret").value = c.copernicus_client_secret || "";
  document.getElementById("cred-bdc-token").value = c.inpe_bdc_token || "";
  settingsModal.classList.remove("hidden");
});
document.getElementById("close-settings").addEventListener("click", () => settingsModal.classList.add("hidden"));

document.getElementById("btn-save-creds").addEventListener("click", () => {
  const creds = {
    copernicus_client_id: document.getElementById("cred-cop-id").value.trim(),
    copernicus_client_secret: document.getElementById("cred-cop-secret").value.trim(),
    inpe_bdc_token: document.getElementById("cred-bdc-token").value.trim(),
  };
  saveCreds(creds);
  showToast("Credenciais salvas neste navegador.", "success");
  settingsModal.classList.add("hidden");
});

document.getElementById("btn-validate-creds").addEventListener("click", async () => {
  const creds = {
    copernicus_client_id: document.getElementById("cred-cop-id").value.trim(),
    copernicus_client_secret: document.getElementById("cred-cop-secret").value.trim(),
    inpe_bdc_token: document.getElementById("cred-bdc-token").value.trim(),
  };
  const resultDiv = document.getElementById("cred-validation-result");
  resultDiv.textContent = "Validando...";
  try {
    const resp = await fetch(`${API_BASE}/api/credentials/validate`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(creds),
    });
    const data = await resp.json();
    let html = "";
    if (data.copernicus) html += `Copernicus: ${data.copernicus.valid ? "✅ válido" : "❌ inválido — " + (data.copernicus.error || "")}<br>`;
    if (data.inpe_bdc) html += `INPE BDC: ${data.inpe_bdc.valid ? "✅ válido" : "❌ inválido"}<br>`;
    if (!html) html = "Nenhuma credencial informada para validar.";
    resultDiv.innerHTML = html;
  } catch (e) {
    resultDiv.textContent = "Erro ao validar: " + e.message;
  }
});

// ---------- TOAST / LOADING ----------
function showToast(msg, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById("toast-container").appendChild(el);
  setTimeout(() => el.remove(), 5000);
}
function showLoading(text) {
  document.getElementById("loading-text").textContent = text || "Processando...";
  document.getElementById("loading-overlay").classList.remove("hidden");
}
function hideLoading() {
  document.getElementById("loading-overlay").classList.add("hidden");
}

// ---------- SEARCH & RESULTS ----------
let collectedImages = []; // for report
let lastDeter = null, lastProdes = null, lastContext = null;

document.getElementById("btn-search").addEventListener("click", async () => {
  if (!currentGeometry) return showToast("Desenhe uma área no mapa primeiro.", "error");

  const creds = loadCreds();
  const dateStart = document.getElementById("date-start").value || null;
  const dateEnd = document.getElementById("date-end").value || null;

  const wantSentinel = document.getElementById("src-sentinel2").checked;
  const wantCbers = document.getElementById("src-cbers4a").checked;
  const wantDeter = document.getElementById("src-deter").checked;
  const wantProdes = document.getElementById("src-prodes").checked;
  const wantContext = document.getElementById("src-context").checked;

  const resultsContainer = document.getElementById("results-container");
  resultsContainer.innerHTML = "";
  document.getElementById("results-panel").style.display = "block";
  collectedImages = [];

  showLoading("Consultando fontes de dados...");

  try {
    if (wantSentinel) {
      if (!creds.copernicus_client_id || !creds.copernicus_client_secret) {
        addResultCard("Sentinel-2 (Copernicus)", null, "⚠️ Configure as credenciais Copernicus em ⚙️ Configurações.", true);
      } else {
        await handleSentinel2(creds, dateStart, dateEnd);
      }
    }
    if (wantCbers) {
      await handleCbers4a(creds, dateStart, dateEnd);
    }
    if (wantDeter) {
      await handleDeter(dateStart, dateEnd);
    }
    if (wantProdes) {
      await handleProdes();
    }
    if (wantContext) {
      await handleContextSummary();
    }
    document.getElementById("btn-report").disabled = false;
  } catch (e) {
    showToast("Erro durante a busca: " + e.message, "error");
  } finally {
    hideLoading();
  }
});

async function handleSentinel2(creds, dateStart, dateEnd) {
  showLoading("Buscando cenas Sentinel-2...");
  const body = {
    geometry: currentGeometry, date_start: dateStart, date_end: dateEnd,
    credentials: creds,
  };
  const resp = await fetch(`${API_BASE}/api/sentinel2/search`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    return addResultCard("Sentinel-2 (Copernicus)", null, `Erro: ${err.detail || resp.statusText}`, true);
  }
  const data = await resp.json();
  const best = data.items[0];
  const date = best ? best.date : null;

  showLoading("Gerando preview Sentinel-2...");
  const prevResp = await fetch(`${API_BASE}/api/sentinel2/preview`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: "sentinel2", date: date ? date.split("T")[0] : null, geometry: currentGeometry, credentials: creds }),
  });
  if (!prevResp.ok) {
    const err = await prevResp.json().catch(() => ({}));
    return addResultCard("Sentinel-2 (Copernicus)", null, `Erro no preview: ${err.detail || prevResp.statusText}`, true);
  }
  const prevData = await prevResp.json();
  const meta = prevData.meta;
  const cardId = addResultCard("Sentinel-2 L2A (Copernicus)", prevData.preview_base64,
    `Cenas encontradas: ${data.count} · Data: ${meta.time_range ? meta.time_range[0].split("T")[0] : "N/D"} · Resolução: ${meta.resolution_m}m`,
    false, {
      source: "Sentinel-2", date: date ? date.split("T")[0] : "última disponível", resolution: "10m",
    });

  addDownloadButton(cardId, async () => {
    showLoading("Baixando GeoTIFF Sentinel-2...");
    const dlResp = await fetch(`${API_BASE}/api/sentinel2/download`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "sentinel2", date: date ? date.split("T")[0] : null, geometry: currentGeometry, credentials: creds }),
    });
    hideLoading();
    if (!dlResp.ok) return showToast("Erro ao baixar GeoTIFF Sentinel-2.", "error");
    triggerDownload(await dlResp.blob(), `sentinel2_${date ? date.split("T")[0] : "download"}.tiff`);
  });
}

async function handleCbers4a(creds, dateStart, dateEnd) {
  showLoading("Buscando cenas CBERS-4A...");
  const body = { geometry: currentGeometry, date_start: dateStart, date_end: dateEnd, credentials: creds };
  const resp = await fetch(`${API_BASE}/api/cbers4a/search`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    return addResultCard("CBERS-4A (INPE)", null, `Erro: ${err.detail || resp.statusText}`, true);
  }
  const data = await resp.json();
  if (data.count === 0) {
    return addResultCard("CBERS-4A (INPE)", null, "Nenhuma cena encontrada para o período/área.", true);
  }
  const best = data.items[0]; // ja ordenado por data (mais recente primeiro); pode ser 2m (WPM) ou 55m (WFI)

  showLoading("Gerando preview CBERS-4A (recorte da área)...");
  const prevResp = await fetch(`${API_BASE}/api/cbers4a/preview`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: "cbers4a", item_id: best.id, collection: best.collection, geometry: currentGeometry, credentials: creds }),
  });
  if (!prevResp.ok) {
    const err = await prevResp.json().catch(() => ({}));
    return addResultCard("CBERS-4A (INPE)", null, `Erro no preview: ${err.detail || prevResp.statusText}`, true);
  }
  const prevData = await prevResp.json();
  const meta = prevData.meta;
  const cardId = addResultCard(meta.source || "CBERS-4A (INPE Brazil Data Cube)", prevData.preview_base64,
    `Cenas encontradas: ${data.count} · Data: ${meta.date ? meta.date.split("T")[0] : "N/D"} · Resolução: ${meta.resolution_m}m · Recorte já ajustado à área desenhada · Acesso público (sem token)`,
    false, {
      source: "CBERS-4A", date: meta.date ? meta.date.split("T")[0] : "N/D", resolution: `${meta.resolution_m}m`,
    });

  addDownloadButton(cardId, async () => {
    showLoading("Recortando e baixando GeoTIFF CBERS-4A...");
    const dlResp = await fetch(`${API_BASE}/api/cbers4a/download`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "cbers4a", item_id: best.id, collection: best.collection, geometry: currentGeometry, credentials: creds }),
    });
    hideLoading();
    if (!dlResp.ok) {
      const err = await dlResp.json().catch(() => ({}));
      return showToast("Erro ao baixar GeoTIFF CBERS-4A: " + (err.detail || dlResp.statusText), "error");
    }
    triggerDownload(await dlResp.blob(), `cbers4a_${best.id}_clip.tif`);
  });
}

async function handleDeter(dateStart, dateEnd) {
  showLoading("Consultando alertas DETER...");
  const resp = await fetch(`${API_BASE}/api/deforestation/deter`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ geometry: currentGeometry, date_start: dateStart, date_end: dateEnd }),
  });
  if (!resp.ok) return addResultCard("DETER (Desmatamento)", null, "Erro ao consultar DETER.", true);
  const data = await resp.json();
  lastDeter = data;
  const classesTxt = Object.entries(data.by_class || {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "nenhum";
  addResultCard("DETER — Alertas de Desmatamento", null,
    `Período: ${data.period[0]} a ${data.period[1]} · Alertas: ${data.alert_count} · Área: ${data.total_area_km2} km² · Classes: ${classesTxt}`,
    data.alert_count === 0);
}

async function handleProdes() {
  showLoading("Consultando PRODES...");
  const resp = await fetch(`${API_BASE}/api/deforestation/prodes`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ geometry: currentGeometry }),
  });
  if (!resp.ok) return addResultCard("PRODES (Desmatamento anual)", null, "Erro ao consultar PRODES.", true);
  const data = await resp.json();
  lastProdes = data;
  addResultCard("PRODES — Desmatamento Anual Consolidado", null,
    `Polígonos: ${data.polygon_count} · Área total desmatada: ${data.total_deforested_area_km2} km²`,
    data.polygon_count === 0);
}

async function handleContextSummary() {
  showLoading("Consultando contexto socioambiental (TI / UC / focos de calor)...");
  const resp = await fetch(`${API_BASE}/api/context/summary`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ geometry: currentGeometry, focos_days: 30 }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    return addResultCard("Contexto Socioambiental", null, `Erro: ${err.detail || resp.statusText}`, true);
  }
  const data = await resp.json();
  lastContext = data;

  const ti = data.terras_indigenas, uc = data.unidades_conservacao, focos = data.focos_calor;
  const hasOverlap = (ti.count > 0) || (uc.count > 0);
  const hasFocos = focos.count > 0;

  let infoParts = [];
  infoParts.push(ti.count > 0
    ? `⚠️ Terra Indígena: ${ti.items.map(i => i.nome).join(", ")}`
    : "Terras Indígenas: nenhuma sobreposição");
  infoParts.push(uc.count > 0
    ? `⚠️ Unidade de Conservação: ${uc.items.map(i => `${i.nome} (${i.esfera})`).join(", ")}`
    : "Unidades de Conservação: nenhuma sobreposição");
  infoParts.push(focos.count > 0
    ? `🔥 ${focos.count} foco(s) de calor nos últimos ${focos.period_days} dias`
    : `Focos de calor: nenhum detectado nos últimos ${focos.period_days} dias`);

  addResultCard("Contexto Socioambiental — TerraBrasilis/INPE (fonte usada pela SEMA-MT)", null,
    infoParts.join(" · "), hasOverlap || hasFocos);
}

let cardCounter = 0;
function addResultCard(title, previewBase64, infoText, isWarning, imageMeta) {
  cardCounter += 1;
  const id = `card-${cardCounter}`;
  const container = document.getElementById("results-container");
  const div = document.createElement("div");
  div.className = "result-card";
  div.id = id;
  div.innerHTML = `
    <h3>${title} ${isWarning ? '<span class="badge warn">atenção</span>' : '<span class="badge">ok</span>'}</h3>
    <div style="font-size:12px;color:#5a6b5e;">${infoText}</div>
    ${previewBase64 ? `<img src="${previewBase64}" alt="preview">` : ""}
    <div class="result-actions"></div>
  `;
  container.appendChild(div);

  if (previewBase64 && imageMeta) {
    collectedImages.push({
      source: imageMeta.source, date: imageMeta.date, resolution: imageMeta.resolution,
      preview_base64: previewBase64, note: infoText.slice(0, 80),
    });
  }
  return id;
}

function addDownloadButton(cardId, onClick) {
  const card = document.getElementById(cardId);
  if (!card) return;
  const actionsDiv = card.querySelector(".result-actions");
  const btn = document.createElement("button");
  btn.className = "btn btn-primary";
  btn.textContent = "⬇️ Baixar GeoTIFF";
  btn.addEventListener("click", onClick);
  actionsDiv.appendChild(btn);
}

function triggerDownload(blob, filename) {
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  window.URL.revokeObjectURL(url);
}

// ---------- REPORT ----------
document.getElementById("btn-report").addEventListener("click", async () => {
  if (!currentGeometry) return showToast("Nenhuma área desenhada.", "error");
  showLoading("Gerando relatório PDF...");
  try {
    const payload = {
      geometry: currentGeometry,
      area_km2: window.__areaKm2 || 0,
      perimeter_km: window.__perimeterKm || 0,
      centroid: window.__centroid || [0, 0],
      images: collectedImages,
      deter_summary: lastDeter,
      prodes_summary: lastProdes,
      context_summary: lastContext,
      generated_at: new Date().toISOString(),
    };
    const resp = await fetch(`${API_BASE}/api/report/generate`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    const blob = await resp.blob();
    triggerDownload(blob, `relatorio_mt_geoapp_${new Date().toISOString().slice(0, 10)}.pdf`);
    showToast("Relatório gerado com sucesso!", "success");
  } catch (e) {
    showToast("Erro ao gerar relatório: " + e.message, "error");
  } finally {
    hideLoading();
  }
});
