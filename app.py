# -*- coding: utf-8 -*-
# ============================================================
# Painel de Qualidade — Starcheck (multi-meses)
# ============================================================

import os, io, json, re
from datetime import datetime, date
from typing import Tuple, Optional

import streamlit as st
import pandas as pd
import numpy as np
import altair as alt

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Drive API (fallback XLSX)
from google.oauth2 import service_account as gcreds
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


# ------------------ CONFIG BÁSICA ------------------
st.set_page_config(page_title="Painel de Qualidade — Starcheck", layout="wide")
st.title("🎯 Painel de Qualidade — Starcheck")

st.markdown(
    """
<style>
.card-wrap{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0 6px;}
.card{background:#f7f7f9;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.06);padding:14px 16px;min-width:180px;flex:1;text-align:center}
.card h4{margin:0 0 6px;font-size:14px;color:#b02300;font-weight:700}
.card h2{margin:0;font-size:26px;font-weight:800;color:#222}
.section{font-size:18px;font-weight:800;margin:22px 0 8px}
.small{color:#666;font-size:13px}
.table-note{margin-top:8px;color:#666;font-size:12px}
</style>
""",
    unsafe_allow_html=True,
)


# ------------------ CREDENCIAL ------------------
def _get_client_and_drive():
    try:
        block = st.secrets["gcp_service_account"]
    except Exception:
        st.error("Não encontrei [gcp_service_account] no .streamlit/secrets.toml.")
        st.stop()

    if "json_path" in block:
        path = block["json_path"]
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(__file__), path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception as e:
            st.error(f"Não consegui abrir o JSON da service account: {path}")
            with st.expander("Detalhes"):
                st.exception(e)
            st.stop()
    else:
        info = dict(block)

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scopes)
    gc = gspread.authorize(creds)

    dscopes = ["https://www.googleapis.com/auth/drive.readonly"]
    gcred = gcreds.Credentials.from_service_account_info(info, scopes=dscopes)
    drive = build("drive", "v3", credentials=gcred, cache_discovery=False)

    return gc, drive, info.get("client_email", "(sem client_email)")


client, DRIVE, SA_EMAIL = _get_client_and_drive()


# ------------------ SECRETS: IDs ------------------
QUAL_INDEX_ID = st.secrets.get("qual_index_sheet_id", "").strip()
PROD_INDEX_ID = st.secrets.get("prod_index_sheet_id", "").strip()
if not QUAL_INDEX_ID:
    st.error("Faltou `qual_index_sheet_id` no secrets.toml"); st.stop()
if not PROD_INDEX_ID:
    st.error("Faltou `prod_index_sheet_id` no secrets.toml"); st.stop()


# ------------------ HELPERS ------------------
ID_RE = re.compile(r"/d/([a-zA-Z0-9-_]+)")

def _sheet_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    m = ID_RE.search(s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9-_]{20,}", s) else None

def parse_date_any(x):
    if pd.isna(x) or x == "":
        return pd.NaT
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            return (pd.to_datetime("1899-12-30") + pd.to_timedelta(int(x), unit="D")).date()
        except Exception:
            pass
    s = str(x).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return pd.NaT

def _upper(x):
    return str(x).upper().strip() if pd.notna(x) else ""

def _yes(v) -> bool:
    return str(v).strip().upper() in {"S", "SIM", "Y", "YES", "TRUE", "1"}


# ------------------ LEITURA DOS ÍNDICES ------------------
def read_index(sheet_id: str, tab: str = "ARQUIVOS") -> pd.DataFrame:
    sh = client.open_by_key(sheet_id)
    ws = sh.worksheet(tab)
    rows = ws.get_all_records()
    if not rows:
        return pd.DataFrame(columns=["URL", "MÊS", "ATIVO"])
    df = pd.DataFrame(rows)
    df.columns = [c.strip().upper() for c in df.columns]
    for need in ["URL", "MÊS", "ATIVO"]:
        if need not in df.columns:
            df[need] = ""
    return df


# ------------------ FALLBACK XLSX / QUALIDADE ------------------
def _drive_get_file_metadata(file_id: str) -> dict:
    return DRIVE.files().get(fileId=file_id, fields="id, name, mimeType").execute()

def _drive_download_bytes(file_id: str) -> bytes:
    req = DRIVE.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req, chunksize=1024 * 1024)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    return buf.getvalue()

def read_quality_month(month_id: str) -> Tuple[pd.DataFrame, str]:
    meta = _drive_get_file_metadata(month_id)
    title = meta.get("name", month_id)
    mime = meta.get("mimeType", "")

    if mime == "application/vnd.google-apps.spreadsheet":
        sh = client.open_by_key(month_id)
        try:
            ws = sh.worksheet("GERAL")
        except Exception as e:
            raise RuntimeError(f"O arquivo '{title}' não possui aba 'GERAL'.") from e
        dq = pd.DataFrame(ws.get_all_records())
        if dq.empty:
            return pd.DataFrame(), title
        dq.columns = [c.strip() for c in dq.columns]
    else:
        if not mime.startswith("application/vnd.openxmlformats-officedocument") and \
           not mime.startswith("application/vnd.ms-excel"):
            raise RuntimeError(f"Tipo de arquivo não suportado para Qualidade: {mime} ({title})")
        content = _drive_download_bytes(month_id)
        try:
            dq = pd.read_excel(io.BytesIO(content), sheet_name="GERAL", engine="openpyxl")
        except ValueError as e:
            raise RuntimeError(f"O arquivo '{title}' não possui aba 'GERAL'.") from e
        dq.columns = [str(c).strip() for c in dq.columns]

    rename_map = {}
    for c in dq.columns:
        cu = c.upper()
        if cu == "DATA": rename_map[c] = "DATA"
        elif cu == "PLACA": rename_map[c] = "PLACA"
        elif cu in {"VISTORIADORES", "VISTORIADOR"}: rename_map[c] = "VISTORIADOR"
        elif cu in {"CIDADE", "UNIDADE"}: rename_map[c] = "UNIDADE"
        elif cu in {"ERROS","ERRO"}: rename_map[c] = "ERRO"
        elif cu.startswith("GRAVIDADE"): rename_map[c] = "GRAVIDADE"
        elif cu in {"OBSERVAÇÃO","OBSERVACAO","OBS"}: rename_map[c] = "OBS"
        elif cu == "ANALISTA": rename_map[c] = "ANALISTA"
        elif cu in {"EMPRESA","MARCA"}: rename_map[c] = "EMPRESA"
    dq = dq.rename(columns=rename_map)

    for need in ["DATA","PLACA","VISTORIADOR","UNIDADE","ERRO","GRAVIDADE","ANALISTA","EMPRESA"]:
        if need not in dq.columns:
            dq[need] = ""

    dq["DATA"] = dq["DATA"].apply(parse_date_any)
    for c in ["VISTORIADOR","UNIDADE","ERRO","GRAVIDADE","ANALISTA","EMPRESA","PLACA"]:
        dq[c] = dq[c].astype(str).map(_upper)

    dq = dq[(dq["VISTORIADOR"] != "") & (dq["ERRO"] != "")]
    return dq, title


# ------------------ LEITURA / PRODUÇÃO ------------------
def read_prod_month(month_sheet_id: str) -> Tuple[pd.DataFrame, str]:
    sh = client.open_by_key(month_sheet_id)
    title = sh.title or month_sheet_id
    ws = sh.sheet1
    df = pd.DataFrame(ws.get_all_records())
    if df.empty:
        return pd.DataFrame(), title

    df.columns = [c.strip().upper() for c in df.columns]

    col_unid = "UNIDADE" if "UNIDADE" in df.columns else None
    col_data = "DATA" if "DATA" in df.columns else None
    col_chas = "CHASSI" if "CHASSI" in df.columns else None
    col_per  = "PERITO" if "PERITO" in df.columns else None
    col_dig  = "DIGITADOR" if "DIGITADOR" in df.columns else None
    req = [col_unid, col_data, col_chas, (col_per or col_dig)]
    if any(r is None for r in req):
        return pd.DataFrame(), title

    df[col_unid] = df[col_unid].map(_upper)
    df["__DATA__"] = df[col_data].apply(parse_date_any)
    df[col_chas] = df[col_chas].map(_upper)

    if col_per and col_dig:
        df["VISTORIADOR"] = np.where(
            df[col_per].astype(str).str.strip() != "",
            df[col_per].map(_upper),
            df[col_dig].map(_upper),
        )
    elif col_per:
        df["VISTORIADOR"] = df[col_per].map(_upper)
    else:
        df["VISTORIADOR"] = df[col_dig].map(_upper)

    df = df.sort_values(["__DATA__", col_chas], kind="mergesort").reset_index(drop=True)
    df["__ORD__"] = df.groupby(col_chas).cumcount()
    df["IS_REV"] = (df["__ORD__"] >= 1).astype(int)
    return df, title


# ------------------ CARREGA INDEX ------------------
# Oculto no app publicado
show_tech = False

# Ler índices e usar automaticamente todos os meses ATIVOS
idx_q = read_index(QUAL_INDEX_ID)
if "ATIVO" in idx_q.columns:
    idx_q = idx_q[idx_q["ATIVO"].map(_yes)].copy()
sel_meses = sorted([str(m).strip() for m in idx_q["MÊS"] if str(m).strip()])  # usa todos

idx_p = read_index(PROD_INDEX_ID)
if "ATIVO" in idx_p.columns:
    idx_p = idx_p[idx_p["ATIVO"].map(_yes)].copy()
sel_meses_p = sorted([str(m).strip() for m in idx_p["MÊS"] if str(m).strip()])  # usa todos

# Aplica (sem UI)
if sel_meses:
    idx_q = idx_q[idx_q["MÊS"].isin(sel_meses)]
if sel_meses_p:
    idx_p = idx_p[idx_p["MÊS"].isin(sel_meses_p)]

dq_all, ok_q, er_q = [], [], []
for _, r in idx_q.iterrows():
    sid = _sheet_id(r["URL"])
    if not sid:
        continue
    try:
        dq, ttl = read_quality_month(sid)
        if not dq.empty:
            dq_all.append(dq)
        ok_q.append(f"✅ {ttl} — {len(dq):,} linhas".replace(",", "."))
    except Exception as e:
        er_q.append((sid, e))

dp_all, ok_p, er_p = [], [], []
for _, r in idx_p.iterrows():
    sid = _sheet_id(r["URL"])
    if not sid:
        continue
    try:
        dp, ttl = read_prod_month(sid)
        if not dp.empty:
            dp_all.append(dp)
        ok_p.append(f"✅ {ttl} — {len(dp):,} linhas".replace(",", "."))
    except Exception as e:
        er_p.append((sid, e))

if show_tech:
    if ok_q: st.success("Qualidade conectado em:\n\n- " + "\n- ".join(ok_q))
    if er_q:
        with st.expander("Falhas (Qualidade)"):
            for sid, e in er_q: st.write(sid); st.exception(e)
    if ok_p: st.success("Produção conectada em:\n\n- " + "\n- ".join(ok_p))
    if er_p:
        with st.expander("Falhas (Produção)"):
            for sid, e in er_p: st.write(sid); st.exception(e)

if not dq_all:
    st.error("Não consegui ler dados de Qualidade de nenhum mês."); st.stop()

dfQ = pd.concat(dq_all, ignore_index=True)
dfP = pd.concat(dp_all, ignore_index=True) if dp_all else pd.DataFrame(columns=["VISTORIADOR","__DATA__","IS_REV","UNIDADE"])


# ------------------ FILTROS PRINCIPAIS ------------------
if "EMPRESA" in dfQ.columns:
    dfQ = dfQ[dfQ["EMPRESA"] == "STARCHECK"].copy()

# meses únicos (YYYY-MM) a partir da coluna DATA
s_all_dt = pd.to_datetime(dfQ["DATA"], errors="coerce")
ym_all = (
    s_all_dt.dt.to_period("M")
    .dropna()
    .astype(str)              # ex.: "2025-09"
    .unique()
    .tolist()
)
ym_all = sorted(ym_all)

if not ym_all:
    st.error("Qualidade sem colunas de Data válidas."); st.stop()

# mapeia para "MM/YYYY" para exibição
label_map = {f"{m[5:]}/{m[:4]}": m for m in ym_all}
sel_label = st.selectbox("Mês de referência", options=list(label_map.keys()), index=len(ym_all)-1)
ym_sel = label_map[sel_label]
ref_year, ref_month = int(ym_sel[:4]), int(ym_sel[5:7])

# máscara do mês escolhido
mask_mes = (s_all_dt.dt.year.eq(ref_year) & s_all_dt.dt.month.eq(ref_month))

dfQ_mes = dfQ[mask_mes].copy()

s_mes_dates = pd.to_datetime(dfQ_mes["DATA"], errors="coerce").dt.date
min_d, max_d = min(s_mes_dates.dropna()), max(s_mes_dates.dropna())
col1, col2 = st.columns([1.2, 2.8])
with col1:
    drange = st.date_input("Período (dentro do mês)",
                           value=(min_d, max_d), min_value=min_d, max_value=max_d,
                           format="DD/MM/YYYY")

start_d, end_d = (drange if isinstance(drange, tuple) and len(drange)==2 else (min_d, max_d))
mask_dias = s_mes_dates.map(lambda d: isinstance(d, date) and start_d <= d <= end_d)
viewQ = dfQ_mes[mask_dias].copy()

# -------- Filtros extras (ANTES de Produção) --------
unids = sorted(viewQ["UNIDADE"].dropna().unique().tolist()) if "UNIDADE" in viewQ.columns else []
vist_opts = sorted(viewQ["VISTORIADOR"].dropna().unique().tolist()) if "VISTORIADOR" in viewQ.columns else []

with col2:
    c21, c22 = st.columns(2)
    with c21:
        f_unids = st.multiselect("Unidades (opcional)", unids, default=unids)
    with c22:
        f_vists = st.multiselect("Vistoriadores (opcional)", vist_opts)

if f_unids and "UNIDADE" in viewQ.columns:
    viewQ = viewQ[viewQ["UNIDADE"].isin([_upper(u) for u in f_unids])]
if f_vists:
    viewQ = viewQ[viewQ["VISTORIADOR"].isin([_upper(v) for v in f_vists])]

if viewQ.empty:
    st.info("Sem registros de Qualidade no período/filtros."); st.stop()

# -------- Produção: mesmo mês, mesmo intervalo e MESMOS filtros --------
if not dfP.empty:
    s_p_dates_all = pd.to_datetime(dfP["__DATA__"], errors="coerce").dt.date
    maskp_mes = s_p_dates_all.map(lambda d: isinstance(d, date) and d.year == ref_year and d.month == ref_month)
    viewP = dfP[maskp_mes].copy()

    s_p_dates_mes = pd.to_datetime(viewP["__DATA__"], errors="coerce").dt.date
    maskp_dias = s_p_dates_mes.map(lambda d: isinstance(d, date) and start_d <= d <= end_d)
    viewP = viewP[maskp_dias].copy()

    if f_unids and "UNIDADE" in viewP.columns:
        viewP = viewP[viewP["UNIDADE"].isin([_upper(u) for u in f_unids])]
    if f_vists and "VISTORIADOR" in viewP.columns:
        viewP = viewP[viewP["VISTORIADOR"].isin([_upper(v) for v in f_vists])]
else:
    viewP = dfP.copy()


# ------------------ KPIs ------------------
grav_gg = {"GRAVE", "GRAVISSIMO", "GRAVÍSSIMO"}
total_erros = int(len(viewQ))
total_gg = int(viewQ["GRAVIDADE"].isin(grav_gg).sum()) if "GRAVIDADE" in viewQ.columns else 0
vist_avaliados = int(viewQ["VISTORIADOR"].nunique()) if "VISTORIADOR" in viewQ.columns else 0
media_por_vist = (total_erros / vist_avaliados) if vist_avaliados else 0

if "GRAVIDADE" in viewQ.columns:
    gg_by_vist = (
        viewQ[viewQ["GRAVIDADE"].isin(grav_gg)]
        .groupby("VISTORIADOR")["ERRO"].size().reset_index(name="GG")
    )
    vist_5gg = int((gg_by_vist["GG"] >= 5).sum())
else:
    vist_5gg = 0

# >>> NOVO: taxa de erro bruta geral (erros / vistorias brutas)
total_vist_brutas = int(len(viewP)) if not viewP.empty else 0
taxa_geral = (total_erros / total_vist_brutas * 100) if total_vist_brutas else np.nan
taxa_geral_str = "—" if np.isnan(taxa_geral) else f"{taxa_geral:.1f}%".replace(".", ",")

cards = [
    ("Total de erros (período)", f"{total_erros:,}".replace(",", ".")),
    ("Vistoriadores com ≥5 erros GG", f"{vist_5gg:,}".replace(",", ".")),
    ("Erros Grave+Gravíssimo", f"{total_gg:,}".replace(",", ".")),
    ("Vistoriadores avaliados", f"{vist_avaliados:,}".replace(",", ".")),
    ("Média de erros / vistoriador", f"{media_por_vist:.1f}".replace(".", ",")),
    # >>> NOVO cartão
    ("Taxa de erro (bruta)", taxa_geral_str),
]
st.markdown(
    '<div class="card-wrap">' + "".join([f"<div class='card'><h4>{t}</h4><h2>{v}</h2></div>" for t, v in cards]) + "</div>",
    unsafe_allow_html=True,
)


# ------------------ GRÁFICOS ------------------
def bar_with_labels(df, x_col, y_col, x_title="", y_title="QTD", height=320):
    base = alt.Chart(df).encode(
        x=alt.X(f"{x_col}:N", sort='-y', title=x_title,
                axis=alt.Axis(labelAngle=0, labelLimit=180, labelOverlap=False)),
        y=alt.Y(f"{y_col}:Q", title=y_title),
        tooltip=[x_col, y_col],
    )
    bars = base.mark_bar()
    labels = base.mark_text(dy=-6).encode(text=alt.Text(f"{y_col}:Q", format=".0f"))
    return (bars + labels).properties(height=height)

c1, c2 = st.columns(2)

if "UNIDADE" in viewQ.columns:
   with c1:
    st.markdown('<div class="section">🏙️ Erros por unidade</div>', unsafe_allow_html=True)

    # Contagem de erros por unidade
    by_city = (
        viewQ.groupby("UNIDADE", dropna=False)["ERRO"]
        .size()
        .reset_index(name="QTD")
    )

    # Produção (vistorias brutas) por unidade — pode estar vazia
    if not viewP.empty and "UNIDADE" in viewP.columns:
        prod_city = (
            viewP.groupby("UNIDADE", dropna=False)["IS_REV"]
            .size()
            .reset_index(name="VIST")   # vistorias brutas
        )
    else:
        prod_city = pd.DataFrame(columns=["UNIDADE", "VIST"])

    # Junta e calcula %ERRO por unidade (usando vistorias brutas)
    by_city = by_city.merge(prod_city, on="UNIDADE", how="left").fillna({"VIST": 0})
    by_city["%ERRO"] = np.where(by_city["VIST"] > 0, (by_city["QTD"] / by_city["VIST"]) * 100, np.nan)

    # Se não houver produção, usa % de participação nos erros como fallback
    if by_city["%ERRO"].isna().all():
        total_err = by_city["QTD"].sum()
        by_city["%ERRO"] = np.where(total_err > 0, (by_city["QTD"] / total_err) * 100, np.nan)
        y2_title = "% dos erros"
    else:
        y2_title = "% de erro (erros/vistorias)"

    # >>> NOVO: coluna 0–1 para formatar eixo/tooltip com '%'
    by_city["PCT"] = by_city["%ERRO"] / 100.0

    # Ordena pelo volume de erros (como estava)
    by_city = by_city.sort_values("QTD", ascending=False).reset_index(drop=True)
    order = by_city["UNIDADE"].tolist()  # força a ordem no eixo X

    # --- barras (QTD) ---
    bars = (
        alt.Chart(by_city)
        .mark_bar()
        .encode(
            x=alt.X("UNIDADE:N", sort=order, axis=alt.Axis(labelAngle=0, labelLimit=180), title="UNIDADE"),
            y=alt.Y("QTD:Q", title="QTD"),
            tooltip=["UNIDADE", "QTD", alt.Tooltip("PCT:Q", format=".1%", title=y2_title)],
        )
    )
    bar_labels = (
        alt.Chart(by_city)
        .mark_text(dy=-6)
        .encode(
            x=alt.X("UNIDADE:N", sort=order),
            y="QTD:Q",
            text=alt.Text("QTD:Q", format=".0f"),
        )
    )

    # --- linha (%ERRO) em eixo Y secundário (usando PCT 0–1) ---
    line = (
        alt.Chart(by_city)
        .mark_line(point=True, color="#b02300")
        .encode(
            x=alt.X("UNIDADE:N", sort=order),
            y=alt.Y("PCT:Q", axis=alt.Axis(title=y2_title, format=".1%")),
        )
    )
    line_labels = (
        alt.Chart(by_city)
        .mark_text(color="#b02300", dy=-8, fontWeight="bold")
        .encode(
            x=alt.X("UNIDADE:N", sort=order),
            y="PCT:Q",
            text=alt.Text("PCT:Q", format=".1%"),
        )
    )

    chart = alt.layer(bars, bar_labels, line, line_labels).resolve_scale(y="independent").properties(height=340)
    st.altair_chart(chart, use_container_width=True)

if "GRAVIDADE" in viewQ.columns:
    with c2:
        st.markdown('<div class="section">🧲 Erros por gravidade</div>', unsafe_allow_html=True)
        by_grav = (viewQ.groupby("GRAVIDADE", dropna=False)["ERRO"]
                   .size().reset_index(name="QTD").sort_values("QTD", ascending=False))
        if len(by_grav):
            st.altair_chart(bar_with_labels(by_grav, "GRAVIDADE", "QTD", x_title="GRAVIDADE", height=340),
                            use_container_width=True)

c3, c4 = st.columns(2)

if "ANALISTA" in viewQ.columns:
    with c3:
        st.markdown('<div class="section">🧑‍💻 Erros por analista</div>', unsafe_allow_html=True)
        by_ana = (viewQ.groupby("ANALISTA", dropna=False)["ERRO"]
                  .size().reset_index(name="QTD").sort_values("QTD", ascending=False))
        if len(by_ana):
            st.altair_chart(bar_with_labels(by_ana, "ANALISTA", "QTD", x_title="ANALISTA"),
                            use_container_width=True)

with c4:
    st.markdown('<div class="section">🏷️ Top 5 erros</div>', unsafe_allow_html=True)
    top5 = (viewQ.groupby("ERRO", dropna=False)["ERRO"]
            .size().reset_index(name="QTD").sort_values("QTD", ascending=False).head(5))
    if len(top5):
        st.altair_chart(bar_with_labels(top5, "ERRO", "QTD", x_title="ERRO"),
                        use_container_width=True)


# ------------------ VISUALIZAÇÕES EXTRAS ------------------
ex1, ex2 = st.columns(2)

with ex1:
    st.markdown('<div class="section">📈 Pareto de erros</div>', unsafe_allow_html=True)

    n_err = int(viewQ["ERRO"].nunique()) if "ERRO" in viewQ.columns else 0
    if n_err == 0:
        st.info("Sem dados para montar o Pareto no período/filtros atuais.")
    else:
        max_cats = min(30, n_err)
        top_cats = st.slider(
            "Categorias no Pareto",
            min_value=3, max_value=max_cats, value=min(10, max_cats),
            step=1, key="pareto_cats",
        )

        pareto = (
            viewQ.groupby("ERRO", sort=False)["ERRO"]
            .size()
            .reset_index(name="QTD")
            .sort_values("QTD", ascending=False)
            .head(top_cats)
            .reset_index(drop=True)
        )

        if pareto.empty:
            st.info("Sem dados para montar o Pareto no período/filtros atuais.")
        else:
            pareto["ACUM"] = pareto["QTD"].cumsum()
            total = pareto["QTD"].sum()
            pareto["%ACUM"] = pareto["ACUM"] / total * 100

            x_enc = alt.X(
                "ERRO:N",
                sort=alt.SortField(field="QTD", order="descending"),
                axis=alt.Axis(labelAngle=0, labelLimit=180),
                title="ERRO",
            )

            bars = alt.Chart(pareto).mark_bar().encode(
                x=x_enc,
                y=alt.Y("QTD:Q", title="QTD"),
                tooltip=["ERRO", "QTD", alt.Tooltip("%ACUM:Q", format=".1f", title="% acumulado")],
            )
            bar_labels = alt.Chart(pareto).mark_text(dy=-6).encode(
                x=x_enc, y="QTD:Q", text=alt.Text("QTD:Q", format=".0f")
            )

            line = alt.Chart(pareto).mark_line(point=True).encode(
                x=x_enc,
                y=alt.Y("%ACUM:Q", title="% Acumulado"),
                color=alt.value("#b02300"),
            )
            line_labels = alt.Chart(pareto).mark_text(
                dy=-8, baseline="bottom", color="#b02300", fontWeight="bold"
            ).encode(
                x=x_enc, y="%ACUM:Q", text=alt.Text("%ACUM:Q", format=".1f")
            )

            chart_pareto = alt.layer(bars, bar_labels, line, line_labels) \
                               .resolve_scale(y='independent') \
                               .properties(height=360)

            st.altair_chart(chart_pareto, use_container_width=True)

            max_topN = int(len(pareto))
            topN_sim = st.slider(
                "Quantos erros do topo considerar?",
                min_value=1, max_value=max_topN, value=min(8, max_topN),
                key="pareto_topN",
            )
            reducao = st.slider(
                "Redução esperada nesses erros (%)",
                min_value=0, max_value=100, value=25,
                key="pareto_reducao",
            )

            idx = min(topN_sim, max_topN) - 1
            frac = float(pareto["%ACUM"].iloc[idx]) / 100.0
            queda_total = frac * (reducao / 100.0) * 100.0

            st.info(
                f"Os **Top {topN_sim}** explicam **{frac*100:.1f}%** do total. "
                f"Se você reduzir esses erros em **{reducao}%**, "
                f"o total cai cerca de **{queda_total:.1f}%**."
            )

with ex2:
    st.markdown('<div class="section">🗺️ Heatmap Cidade × Gravidade</div>', unsafe_allow_html=True)
    if "UNIDADE" in viewQ.columns and "GRAVIDADE" in viewQ.columns:
        hm = (viewQ.groupby(["UNIDADE","GRAVIDADE"])["ERRO"].size().reset_index(name="QTD"))
        rects = alt.Chart(hm).mark_rect().encode(
            x=alt.X("GRAVIDADE:N", axis=alt.Axis(labelAngle=0, title="GRAVIDADE")),
            y=alt.Y("UNIDADE:N", sort='-x', title="UNIDADE"),
            color=alt.Color("QTD:Q", scale=alt.Scale(scheme="blues")),
            tooltip=["UNIDADE","GRAVIDADE","QTD"]
        )
        texts = alt.Chart(hm).mark_text(baseline="middle").encode(
            x="GRAVIDADE:N", y="UNIDADE:N", text=alt.Text("QTD:Q", format=".0f"),
            color=alt.condition("datum.QTD > 0", alt.value("#111"), alt.value("#111"))
        )
        st.altair_chart((rects + texts).properties(height=340), use_container_width=True)

ex3, ex4 = st.columns(2)

with ex3:
    st.markdown('<div class="section">♻️ Reincidência por vistoriador (≥3)</div>', unsafe_allow_html=True)
    rec = (viewQ.groupby(["VISTORIADOR","ERRO"])["ERRO"]
           .size().reset_index(name="QTD").sort_values("QTD", ascending=False))
    rec = rec[rec["QTD"] >= 3]
    st.dataframe(rec, use_container_width=True, hide_index=True)

with ex4:
    st.markdown('<div class="section">⚖️ Calibração por analista (% GG)</div>', unsafe_allow_html=True)
    if "ANALISTA" in viewQ.columns and "GRAVIDADE" in viewQ.columns:
        ana = (viewQ.assign(_gg=viewQ["GRAVIDADE"].isin(grav_gg).astype(int))
               .groupby("ANALISTA")["_gg"].mean().reset_index(name="%GG")
               .sort_values("%GG", ascending=False))
        ana["%GG"] = (ana["%GG"] * 100).round(1)
        st.altair_chart(
            alt.Chart(ana).mark_bar().encode(
                x=alt.X("ANALISTA:N", axis=alt.Axis(labelAngle=0, labelLimit=180)),
                y=alt.Y("%GG:Q"), tooltip=["ANALISTA", alt.Tooltip("%GG:Q", format=".1f")]
            ).properties(height=340),
            use_container_width=True,
        )

st.markdown('<div class="section">📅 Erros por dia da semana</div>', unsafe_allow_html=True)
dow_map = {0:"Seg",1:"Ter",2:"Qua",3:"Qui",4:"Sex",5:"Sáb",6:"Dom"}
dow = pd.to_datetime(viewQ["DATA"], errors="coerce").dt.dayofweek.map(dow_map)
dow_df = pd.DataFrame({"DIA": dow}).value_counts().reset_index(name="QTD").rename(columns={"index":"DIA"})
dow_df = dow_df.sort_index() if "DIA" not in dow_df.columns else dow_df
if not dow_df.empty:
    st.altair_chart(bar_with_labels(dow_df, "DIA", "QTD", x_title="DIA DA SEMANA"),
                    use_container_width=True)


# ------------------ % ERRO (casamento com Produção) ------------------
st.markdown("---")
st.markdown('<div class="section">📐 % de erro por vistoriador</div>', unsafe_allow_html=True)
denom_mode = st.radio("Base para %Erro", ["Bruta (recomendado)", "Líquida"], horizontal=True, index=0)

if not viewP.empty:
    prod = (viewP.groupby("VISTORIADOR", dropna=False)
            .agg(vist=("IS_REV","size"), rev=("IS_REV","sum")).reset_index())
    prod["liq"] = prod["vist"] - prod["rev"]
else:
    prod = pd.DataFrame(columns=["VISTORIADOR","vist","rev","liq"])

qual = (viewQ.groupby("VISTORIADOR", dropna=False)
        .agg(erros=("ERRO","size"),
             erros_gg=("GRAVIDADE", lambda s: s.isin(grav_gg).sum()))
        .reset_index())

base = prod.merge(qual, on="VISTORIADOR", how="outer").fillna(0)
den = base["liq"] if denom_mode.startswith("Líquida") else base["vist"]
base["%ERRO"] = np.where(den > 0, (base["erros"] / den) * 100, np.nan)
base["%ERRO_GG"] = np.where(den > 0, (base["erros_gg"] / den) * 100, np.nan)

show_cols = ["VISTORIADOR","vist","rev","liq","erros","erros_gg","%ERRO","%ERRO_GG"]
fmt = base.copy()
for c in ["vist","rev","liq","erros","erros_gg"]:
    if c in fmt.columns: fmt[c] = fmt[c].map(lambda x: int(x))
for c in ["%ERRO","%ERRO_GG"]:
    if c in fmt.columns: fmt[c] = fmt[c].map(lambda x: ("—" if pd.isna(x) else f"{x:.1f}%".replace(".", ",")))
st.dataframe(fmt[show_cols], use_container_width=True, hide_index=True)


# ------------------ TABELAS ------------------
st.markdown("---")
st.markdown('<div class="section">🧾 Detalhamento (linhas da base)</div>', unsafe_allow_html=True)
det_cols = ["DATA","UNIDADE","VISTORIADOR","PLACA","ERRO","GRAVIDADE","ANALISTA","OBS"]
det = viewQ.copy()
for c in det_cols:
    if c not in det.columns: det[c] = ""
det = det[det_cols].sort_values("DATA")
st.dataframe(det, use_container_width=True, hide_index=True)
st.caption('<div class="table-note">* Dados filtrados pelo período, unidades e vistoriadores selecionados.</div>',
           unsafe_allow_html=True)

st.markdown("---")
st.markdown('<div class="section">📊 Comparativo por colaborador (mês atual x anterior)</div>', unsafe_allow_html=True)

dfQ["__DT__"] = pd.to_datetime(dfQ["DATA"], errors="coerce")
dfQ["YM"] = dfQ["__DT__"].dt.strftime("%Y-%m")
ym_ref = f"{ref_year}-{ref_month:02d}"
ym_prev = f"{ref_year-1}-12" if ref_month == 1 else f"{ref_year}-{ref_month-1:02d}"

cur = dfQ[dfQ["YM"] == ym_ref].groupby("VISTORIADOR")["ERRO"].size().reset_index(name="ERROS_ATUAL")
prev = dfQ[dfQ["YM"] == ym_prev].groupby("VISTORIADOR")["ERRO"].size().reset_index(name="ERROS_ANT")

tab = cur.merge(prev, on="VISTORIADOR", how="outer").fillna(0)
tab["Δ"] = tab["ERROS_ATUAL"] - tab["ERROS_ANT"]
tab["VAR_%"] = np.where(tab["ERROS_ANT"] > 0, (tab["Δ"] / tab["ERROS_ANT"]) * 100, np.nan)

def _status(delta):
    if delta < 0: return "✅ Melhorou"
    if delta > 0: return "❌ Piorou"
    return "➡️ Igual"

tab["Status"] = tab["Δ"].map(_status)
tab_fmt = tab.copy()
tab_fmt["VAR_%"] = tab_fmt["VAR_%"].map(lambda x: "—" if pd.isna(x) else f"{x:.1f}%".replace(".", ","))

st.dataframe(
    tab_fmt.sort_values("ERROS_ATUAL", ascending=False)[
        ["VISTORIADOR","ERROS_ATUAL","ERROS_ANT","Δ","VAR_%","Status"]
    ],
    use_container_width=True, hide_index=True,
)

st.markdown("---")
st.markdown('<div class="section">🏁 Top 5 melhores × piores (por % de erro)</div>', unsafe_allow_html=True)

rank = base.copy()
rank = rank[den > 0].replace({np.inf: np.nan}).dropna(subset=["%ERRO"])

den_col = "liq" if denom_mode.startswith("Líquida") else "vist"
col_titulo_den = "vistórias líquidas" if den_col == "liq" else "vistórias"
cols_rank = ["VISTORIADOR", den_col, "erros", "%ERRO", "%ERRO_GG"]
rank_view = rank[cols_rank].rename(columns={den_col: col_titulo_den})

for c in [col_titulo_den, "erros"]:
    if c in rank_view.columns: rank_view[c] = rank_view[c].astype(int)
for c in ["%ERRO", "%ERRO_GG"]:
    if c in rank_view.columns: rank_view[c] = rank_view[c].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "—")

best5  = rank_view.sort_values("%ERRO", ascending=True).head(5)
worst5 = rank_view.sort_values("%ERRO", ascending=False).head(5)

c_best, c_worst = st.columns(2)
with c_best:
    st.subheader("🏆 Top 5 melhores (menor %Erro)")
    st.dataframe(best5.reset_index(drop=True), use_container_width=True, hide_index=True)
with c_worst:
    st.subheader("⚠️ Top 5 piores (maior %Erro)")
    st.dataframe(worst5.reset_index(drop=True), use_container_width=True, hide_index=True)

st.markdown("---")
st.markdown('<div class="section">🚨 Tentativa de Fraude — Detalhamento</div>', unsafe_allow_html=True)
fraude_mask = viewQ["ERRO"].astype(str).str.upper().str.contains(r"\bTENTATIVA DE FRAUDE\b", na=False)
df_fraude = viewQ[fraude_mask].copy()
if df_fraude.empty:
    st.info("Nenhum registro de **Tentativa de Fraude** no período/filtros selecionados.")
else:
    cols_fraude = ["DATA","UNIDADE","VISTORIADOR","PLACA","ERRO","GRAVIDADE","ANALISTA","OBS"]
    for c in cols_fraude:
        if c not in df_fraude.columns: df_fraude[c] = ""
    df_fraude = df_fraude[cols_fraude].sort_values(["DATA","UNIDADE","VISTORIADOR"])
    st.dataframe(df_fraude, use_container_width=True, hide_index=True)
    st.caption('<div class="table-note">* Somente linhas cujo **ERRO** é exatamente “TENTATIVA DE FRAUDE”.</div>',

               unsafe_allow_html=True)

