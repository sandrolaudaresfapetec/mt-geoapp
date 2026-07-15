"""
Geracao de relatorio PDF completo: mapa da area, imagens de cada fonte,
estatisticas de area/perimetro, coordenadas e tabela comparativa entre fontes.
"""
import io
import base64
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from PIL import Image as PILImage


def _format_date(date_str):
    """Formata datas ISO longas (com timestamp) para exibicao compacta."""
    if not date_str or date_str == "N/D":
        return "N/D"
    s = str(date_str)
    if "T" in s:
        s = s.split("T")[0]
    return s


def _decode_b64_image(data_url: str):
    if not data_url:
        return None
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    raw = base64.b64decode(data_url)
    return io.BytesIO(raw)


def build_pdf(payload: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        topMargin=1.6 * cm, bottomMargin=1.6 * cm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleMT", parent=styles["Title"], fontSize=20, spaceAfter=6)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], spaceBefore=10, spaceAfter=6)
    normal = styles["Normal"]
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=colors.grey)

    elements = []

    generated_at = payload.get("generated_at") or datetime.utcnow().isoformat()
    elements.append(Paragraph("Relatório de Imagens de Satélite — Mato Grosso", title_style))
    elements.append(Paragraph(f"Gerado em: {generated_at}", small))
    elements.append(Spacer(1, 0.4 * cm))

    # ---- Area info ----
    elements.append(Paragraph("1. Área de Interesse", h2))
    centroid = payload.get("centroid", [None, None])
    area_km2 = payload.get("area_km2", 0)
    perim_km = payload.get("perimeter_km", 0)

    info_table_data = [
        ["Área (km²)", f"{area_km2:,.3f}".replace(",", ".")],
        ["Perímetro (km)", f"{perim_km:,.3f}".replace(",", ".")],
        ["Centróide (lon, lat)", f"{centroid[0]:.6f}, {centroid[1]:.6f}" if centroid[0] is not None else "N/D"],
    ]
    t = Table(info_table_data, colWidths=[6 * cm, 9 * cm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef3ee")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 0.3 * cm))

    geom = payload.get("geometry", {})
    coords_preview = str(geom.get("coordinates"))[:500]
    elements.append(Paragraph("Coordenadas do polígono (GeoJSON, resumo):", normal))
    elements.append(Paragraph(f"<font size=7 face='Courier'>{coords_preview}</font>", normal))
    elements.append(Spacer(1, 0.3 * cm))

    # ---- Images per source ----
    elements.append(Paragraph("2. Imagens Coletadas por Fonte", h2))
    images = payload.get("images", [])
    comparison_rows = [["Fonte", "Data", "Resolução", "Observação"]]

    if not images:
        elements.append(Paragraph("Nenhuma imagem foi anexada a este relatório.", normal))
    for img in images:
        source = img.get("source", "Fonte desconhecida")
        date_raw = img.get("date", "N/D")
        date = _format_date(date_raw)
        resolution = img.get("resolution", "N/D")
        note = img.get("note", "")
        elements.append(Paragraph(f"<b>{source}</b>", normal))
        elements.append(Paragraph(f"Data/hora da imagem: {date} &nbsp;&nbsp;|&nbsp;&nbsp; Resolução espacial: {resolution}", small))

        preview_b64 = img.get("preview_base64")
        if preview_b64:
            try:
                img_buf = _decode_b64_image(preview_b64)
                pil_img = PILImage.open(img_buf)
                pil_img = pil_img.convert("RGB")
                img_w, img_h = pil_img.size
                max_w = 14 * cm
                ratio = max_w / img_w
                display_w = max_w
                display_h = img_h * ratio
                if display_h > 10 * cm:
                    display_h = 10 * cm
                    display_w = img_w * (display_h / img_h)
                tmp_buf = io.BytesIO()
                pil_img.save(tmp_buf, format="PNG")
                tmp_buf.seek(0)
                elements.append(RLImage(tmp_buf, width=display_w, height=display_h))
            except Exception as e:
                elements.append(Paragraph(f"[Erro ao renderizar imagem: {e}]", small))
        elements.append(Spacer(1, 0.4 * cm))

        comparison_rows.append([source, str(date), str(resolution), note[:60]])

    # ---- Comparison table ----
    elements.append(Paragraph("3. Tabela Comparativa entre Fontes", h2))
    comp_table = Table(comparison_rows, colWidths=[4.5 * cm, 3.2 * cm, 2.6 * cm, 5.2 * cm])
    comp_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2e5339")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("PADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8f5")]),
    ]))
    elements.append(comp_table)
    elements.append(Spacer(1, 0.4 * cm))

    # ---- DETER / PRODES summary ----
    deter = payload.get("deter_summary")
    prodes = payload.get("prodes_summary")
    if deter or prodes:
        elements.append(PageBreak())
        elements.append(Paragraph("4. Monitoramento de Desmatamento (INPE)", h2))

        if deter:
            elements.append(Paragraph("<b>DETER — Alertas quase em tempo real</b>", normal))
            rows = [["Período", "Alertas", "Área (km²)"]]
            period = deter.get("period", ["", ""])
            rows.append([f"{period[0]} a {period[1]}", str(deter.get("alert_count", 0)),
                         f"{deter.get('total_area_km2', 0):,.3f}".replace(",", ".")])
            dt = Table(rows, colWidths=[6 * cm, 4 * cm, 5 * cm])
            dt.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7a3b1e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]))
            elements.append(dt)
            by_class = deter.get("by_class", {})
            if by_class:
                elements.append(Spacer(1, 0.2 * cm))
                elements.append(Paragraph("Por classe: " + ", ".join(f"{k}: {v}" for k, v in by_class.items()), small))
            elements.append(Spacer(1, 0.4 * cm))

        if prodes:
            elements.append(Paragraph("<b>PRODES — Desmatamento anual consolidado</b>", normal))
            rows2 = [["Polígonos", "Área total desmatada (km²)"]]
            rows2.append([str(prodes.get("polygon_count", 0)),
                          f"{prodes.get('total_deforested_area_km2', 0):,.3f}".replace(",", ".")])
            pt = Table(rows2, colWidths=[6 * cm, 9 * cm])
            pt.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e4a7a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]))
            elements.append(pt)

    # ---- Contexto Socioambiental (TI / UC / Focos de calor) ----
    context = payload.get("context_summary")
    if context:
        elements.append(PageBreak())
        elements.append(Paragraph("5. Contexto Socioambiental (TerraBrasilis / INPE)", h2))
        elements.append(Paragraph(
            "Mesma base de dados pública usada pelo painel de alertas da SEMA-MT "
            "(Secretaria de Estado de Meio Ambiente de Mato Grosso).", small
        ))
        elements.append(Spacer(1, 0.2 * cm))

        ti = context.get("terras_indigenas", {})
        uc = context.get("unidades_conservacao", {})
        focos = context.get("focos_calor", {})
        alerts = context.get("alerts", [])

        if alerts:
            for a in alerts:
                a_clean = a.replace("\u26a0\ufe0f", "[ALERTA]").replace("\U0001f525", "[FOGO]").replace("\u2139\ufe0f", "[INFO]").strip()
                elements.append(Paragraph(a_clean, ParagraphStyle(
                    "alert", parent=normal, textColor=colors.HexColor("#a83232"), fontSize=9,
                )))
            elements.append(Spacer(1, 0.3 * cm))
        else:
            elements.append(Paragraph(
                "[OK] Nenhuma sobreposição com Terras Indígenas ou Unidades de Conservação; "
                "nenhum foco de calor recente detectado na área.", normal
            ))
            elements.append(Spacer(1, 0.3 * cm))

        uc_label = "Unidades de Conservação"
        uc_value = str(uc.get("count", 0))
        if uc.get("partial"):
            uc_label += " (dados parciais)"
            uc_value += " *"
        rows3 = [["Camada", "Ocorrências na área"]]
        rows3.append(["Terras Indígenas", str(ti.get("count", 0))])
        rows3.append([uc_label, uc_value])
        rows3.append([f"Focos de calor (últimos {focos.get('period_days', 30)} dias)", str(focos.get("count", 0))])
        ct = Table(rows3, colWidths=[9 * cm, 6 * cm])
        ct.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#5a3b7a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        elements.append(ct)
        if uc.get("partial"):
            elements.append(Paragraph(
                "* Consulta parcial: uma ou mais bases de Unidades de Conservação (federal/estadual) "
                "não responderam no momento da geração deste relatório. O valor acima pode estar subestimado.",
                ParagraphStyle("uc_partial", parent=small, textColor=colors.HexColor("#a83232")),
            ))
        elements.append(Spacer(1, 0.3 * cm))

        if ti.get("items"):
            nomes = ", ".join(i.get("nome", "") for i in ti["items"] if i.get("nome"))
            elements.append(Paragraph(f"<b>Terras Indígenas:</b> {nomes}", small))
        if uc.get("items"):
            nomes = ", ".join(f"{i.get('nome','')} ({i.get('esfera','')})" for i in uc["items"] if i.get("nome"))
            elements.append(Paragraph(f"<b>Unidades de Conservação:</b> {nomes}", small))
        if focos.get("items"):
            top_focos = focos["items"][:5]
            focos_txt = "; ".join(
                f"{f.get('data_hora_gmt','')[:10]} ({f.get('municipio','N/D')}, sat. {f.get('satelite','N/D')})"
                for f in top_focos
            )
            elements.append(Paragraph(f"<b>Focos de calor mais recentes:</b> {focos_txt}", small))

    # ---- Camadas complementares CAR / SEMA-MT (fonte nao oficial) ----
    car_sema = payload.get("car_sema_summary")
    if car_sema:
        elements.append(PageBreak())
        elements.append(Paragraph("6. Camadas Complementares — CAR / SEMA-MT", h2))
        elements.append(Paragraph(
            "<b>[FONTE NÃO OFICIALMENTE CONFIRMADA]</b> As informações desta seção foram "
            "obtidas de um GeoServer da SEMA-MT (Secretaria de Estado de Meio Ambiente de "
            "Mato Grosso) acessado por meio de uma chave (authkey) localizada em documentação "
            "técnica de terceiros publicamente disponível, SEM confirmação oficial da SEMA-MT "
            "sobre estabilidade do serviço ou termos de uso. Utilize como informação "
            "complementar às fontes oficiais (Copernicus/INPE/TerraBrasilis) apresentadas nas "
            "seções anteriores, e não como base isolada para decisões de compliance.",
            ParagraphStyle("car_warn", parent=small, textColor=colors.HexColor("#a86b00"), fontSize=8),
        ))
        elements.append(Spacer(1, 0.25 * cm))

        uc_sema = car_sema.get("unidades_conservacao_sema", {})
        car_app = car_sema.get("car_app", {})
        car_arl = car_sema.get("car_arl", {})
        area_consolidada = car_sema.get("area_consolidada", {})
        autuacoes = car_sema.get("autuacoes_fiscalizacao", {})
        car_alerts = car_sema.get("alerts", [])

        if car_alerts:
            for a in car_alerts:
                a_clean = a.replace("\u26a0\ufe0f", "[ALERTA]").replace("\u2139\ufe0f", "[INFO]").strip()
                elements.append(Paragraph(a_clean, ParagraphStyle(
                    "car_alert", parent=normal, textColor=colors.HexColor("#a83232"), fontSize=9,
                )))
            elements.append(Spacer(1, 0.25 * cm))

        rows4 = [["Camada (SEMA-MT / SIMCAR)", "Ocorrências", "Área total (ha)"]]
        rows4.append(["Unidades de Conservação (Fed+Est+Mun)", str(uc_sema.get("count", 0)), "—"])
        rows4.append(["CAR - Área de Preservação Permanente", str(car_app.get("count", 0)), str(car_app.get("area_total_ha", 0))])
        rows4.append(["CAR - Reserva Legal", str(car_arl.get("count", 0)), str(car_arl.get("area_total_ha", 0))])
        rows4.append(["Área Consolidada / uso antrópico", str(area_consolidada.get("count", 0)), str(area_consolidada.get("area_total_ha", 0))])
        rows4.append(["Autuações / Embargos ambientais", str(autuacoes.get("count", 0)), "—"])
        ct2 = Table(rows4, colWidths=[9 * cm, 3.5 * cm, 3.5 * cm])
        ct2.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#8a6d1f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        elements.append(ct2)
        elements.append(Spacer(1, 0.25 * cm))

        if uc_sema.get("items"):
            nomes = ", ".join(f"{i.get('nome','')} ({i.get('esfera','')})" for i in uc_sema["items"] if i.get("nome"))
            elements.append(Paragraph(f"<b>Unidades de Conservação (SEMA-MT):</b> {nomes}", small))
        if autuacoes.get("items"):
            top_aut = autuacoes["items"][:5]
            aut_txt = "; ".join(
                f"Auto {a.get('numero_auto_infracao','N/D')} ({a.get('municipio','N/D')}, {a.get('situacao','N/D')})"
                for a in top_aut
            )
            elements.append(Paragraph(f"<b>Autuações mais recentes:</b> {aut_txt}", small))

    elements.append(Spacer(1, 0.6 * cm))

    elements.append(Paragraph(
        "Fontes de dados: Copernicus Data Space Ecosystem (Sentinel-2 L2A, ESA/UE) · "
        "INPE Brazil Data Cube (CBERS-4A/WFI) · TerraBrasilis DETER/PRODES (INPE) · "
        "Geoportal SEMA-MT (camadas complementares, fonte não oficialmente confirmada). "
        "Gerado automaticamente pelo MT GeoApp.", small
    ))

    doc.build(elements)
    buf.seek(0)
    return buf.read()
