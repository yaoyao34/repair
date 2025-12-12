import streamlit as st
import pandas as pd
from datetime import datetime, date
import re
import io
import gspread
from google.oauth2.service_account import Credentials

# PDF (ReportLab)
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont


# ================= 工具 =================
def norm(x):
    if x is None:
        return ""
    return str(x).strip()

def to_ymd(ts):
    s = norm(ts)
    if not s:
        return ""
    d = pd.to_datetime(s, errors="coerce")
    if not pd.isna(d):
        return d.strftime("%Y-%m-%d")
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        y, mo, da = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{y}-{mo:02d}-{da:02d}"
    return ""

def split_links(cell):
    if not cell:
        return []
    return [x.strip() for x in str(cell).split(",") if x.strip()]

def media_label(url, i):
    u = (url or "").lower()
    if any(u.endswith(e) for e in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
        return f"照片 {i}"
    if any(u.endswith(e) for e in [".mp4", ".mov", ".webm", ".mkv"]):
        return f"影片 {i}"
    return f"檔案 {i}"

def status_icon(s):
    s = s or ""
    if "已完成" in s:
        return "✅"
    if "送修" in s:
        return "🚚"
    if "待料" in s:
        return "📦"
    if "處理中" in s:
        return "🛠️"
    if "退回" in s or "無法" in s:
        return "⛔"
    if "待觀查" in s:
        return "👀"
    return "🔧"

def safe_key(s):
    s = norm(s)
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+", "_", s)
    return s[:80]

from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

def now_ts_full():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


# ================= GSpread =================
SHEET_URL = st.secrets["SHEET_URL"]

@st.cache_resource
def gs_client():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["google_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)

gc = gs_client()


# ================= 讀資料（穩定版） =================
def read_sheet_as_df(ws, headers):
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame(columns=headers)

    header = values[0]
    rows = values[1:]

    idx_map = {}
    for i, h in enumerate(header):
        h = norm(h)
        if h and h not in idx_map:
            idx_map[h] = i

    data = {h: [] for h in headers}
    for r in rows:
        for h in headers:
            i = idx_map.get(h)
            data[h].append(r[i] if i is not None and i < len(r) else "")

    return pd.DataFrame(data)


@st.cache_data(ttl=120)
def load_data():
    sh = gc.open_by_url(SHEET_URL)

    report = read_sheet_as_df(
        sh.worksheet("報修資料"),
        ["時間戳記", "班級地點", "損壞設備", "損壞情形描述", "照片或影片", "案件編號"]
    )

    repair = read_sheet_as_df(
        sh.worksheet("維修紀錄"),
        ["時間戳記", "案件編號", "處理進度", "維修說明"]
    )

    pwd = norm(sh.worksheet("密碼設定").acell("A1").value)
    return report, repair, pwd


# ================= 寫回（完整時間戳記） =================
def save_repair(case_id, status, note):
    ws = gc.open_by_url(SHEET_URL).worksheet("維修紀錄")
    values = ws.get_all_values()
    header = values[0]

    def col(name):
        for i, h in enumerate(header):
            if norm(h) == name:
                return i
        return None

    c_ts, c_case, c_stat, c_note = map(col, ["時間戳記", "案件編號", "處理進度", "維修說明"])
    if None in (c_ts, c_case, c_stat, c_note):
        raise RuntimeError("維修紀錄表頭缺少必要欄位：時間戳記/案件編號/處理進度/維修說明")

    ts = now_ts_full()

    last = None
    for i in range(1, len(values)):
        row = values[i]
        if c_case < len(row) and norm(row[c_case]) == case_id:
            last = i + 1

    if last:
        ws.update_cell(last, c_ts + 1, ts)
        ws.update_cell(last, c_stat + 1, status)
        ws.update_cell(last, c_note + 1, note)
    else:
        row = [""] * len(header)
        row[c_ts] = ts
        row[c_case] = case_id
        row[c_stat] = status
        row[c_note] = note
        ws.append_row(row, value_input_option="USER_ENTERED")


# ================= PDF 匯出 =================
def build_export_df(df_all: pd.DataFrame) -> pd.DataFrame:
    """
    產生匯出用欄位：
    報修時間、班級地點、損壞設備、完工時間、處理進度、維修說明
    完工時間：僅當處理進度包含「已完成」才填維修更新時間，否則空白
    """
    out = pd.DataFrame()
    out["報修時間"] = df_all["報修時間"].astype(str)
    out["班級地點"] = df_all["班級地點"].astype(str)
    out["損壞設備"] = df_all["損壞設備"].astype(str)

    def done_time(row):
        s = str(row.get("處理進度", ""))
        if "已完成" in s:
            return str(row.get("維修更新時間", ""))
        return ""

    out["完工時間"] = df_all.apply(done_time, axis=1)
    out["處理進度"] = df_all["處理進度"].astype(str)
    out["維修說明"] = df_all["維修說明"].astype(str)
    return out.fillna("")


def make_pdf_bytes(title: str, df_export: pd.DataFrame) -> bytes:
    """
    產生 PDF bytes（支援中文，使用 STSong-Light）
    """
    # ReportLab 內建中文字型
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        cjk_font = "STSong-Light"
    except Exception:
        cjk_font = "Helvetica"  # 理論上不會走到這裡

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=24,
        rightMargin=24,
        topMargin=24,
        bottomMargin=24,
        title=title,
    )

    styles = getSampleStyleSheet()
    styleN = styles["Normal"]
    styleN.fontName = cjk_font
    styleN.fontSize = 9
    styleH = styles["Heading2"]
    styleH.fontName = cjk_font

    elements = []
    elements.append(Paragraph(title, styleH))
    elements.append(Spacer(1, 8))
    elements.append(Paragraph(f"匯出時間：{now_ts_full()}", styleN))
    elements.append(Spacer(1, 10))

    # 表格資料（含表頭）
    headers = ["報修時間", "班級地點", "損壞設備", "完工時間", "處理進度", "維修說明"]
    data = [headers]

    # 避免太長造成版面炸裂：維修說明與設備文字適度截斷（PDF 可讀性）
    def cut(s, n):
        s = str(s or "")
        return s if len(s) <= n else (s[: n - 1] + "…")

    for _, r in df_export.iterrows():
        data.append([
            cut(r["報修時間"], 19),
            cut(r["班級地點"], 20),
            cut(r["損壞設備"], 20),
            cut(r["完工時間"], 19),
            cut(r["處理進度"], 10),
            cut(r["維修說明"], 60),
        ])

    # 欄寬（A4 內容寬約 540pt；視覺上以說明欄較寬）
    col_widths = [80, 80, 80, 80, 60, 160]

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), cjk_font),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F0F0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-2, -1), "LEFT"),
        ("ALIGN", (-1, 1), (-1, -1), "LEFT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))

    elements.append(table)
    doc.build(elements)
    return buf.getvalue()


# ================= 主程式 =================
def main():
    st.title("報修 / 維修整合系統")

    report, repair, correct_pwd = load_data()

    # ---- Sidebar ----
    with st.sidebar:
        st.subheader("管理登入")
        pwd = st.text_input("密碼", type="password")
        authed = (correct_pwd == "") or (pwd == correct_pwd)

        st.divider()
        kw = st.text_input("搜尋關鍵字")

        status_list = sorted(set(repair["處理進度"].fillna("").tolist()))
        status_filter = st.multiselect("處理進度", status_list)

    # ---- 合併 ----
    r = report.copy()
    r["案件編號"] = r["案件編號"].astype(str).str.strip()
    r["_ts"] = pd.to_datetime(r["時間戳記"], errors="coerce")
    r = r.sort_values("_ts").groupby("案件編號", as_index=False).tail(1)
    r["報修日期"] = r["時間戳記"].apply(to_ymd)
    r = r.rename(columns={"時間戳記": "報修時間"})  # 重要：保留完整報修時間

    w = repair.copy()
    w["案件編號"] = w["案件編號"].astype(str).str.strip()
    w["_ts"] = pd.to_datetime(w["時間戳記"], errors="coerce")
    w = w.sort_values("_ts").groupby("案件編號", as_index=False).tail(1)
    w = w.rename(columns={"時間戳記": "維修更新時間"})

    df = r.merge(
        w[["案件編號", "維修更新時間", "處理進度", "維修說明"]],
        on="案件編號",
        how="left"
    ).fillna("")

    df = df.sort_values("報修日期", ascending=False)

    # ---- 搜尋/篩選（影響畫面，不影響PDF匯出範圍選擇）----
    if kw:
        df = df[df.apply(lambda x: kw.lower() in " ".join(x.astype(str)).lower(), axis=1)]
    if status_filter:
        df = df[df["處理進度"].isin(status_filter)]

    # ---- 登入才顯示 PDF 匯出 ----
    if authed:
        st.subheader("匯出維修紀錄 PDF（登入限定）")

        # 以目前資料推可用日期範圍（如果空，就用今天）
        all_dates = pd.to_datetime(df["報修日期"], errors="coerce")
        min_d = all_dates.min().date() if pd.notna(all_dates.min()) else date.today()
        max_d = all_dates.max().date() if pd.notna(all_dates.max()) else date.today()

        c1, c2, c3 = st.columns([1, 1, 1])
        with c1:
            start_d = st.date_input("報修日期起", value=min_d)
        with c2:
            end_d = st.date_input("報修日期迄", value=max_d)
        with c3:
            st.caption("欄位：報修時間/班級地點/損壞設備/完工時間/處理進度/維修說明")

        # 範圍保護
        if start_d > end_d:
            st.error("日期範圍錯誤：起始日期不可大於結束日期。")
        else:
            # 範圍資料
            df_range = df.copy()
            dcol = pd.to_datetime(df_range["報修日期"], errors="coerce").dt.date
            df_range = df_range[(dcol >= start_d) & (dcol <= end_d)]

            exp_df = build_export_df(df_range)

            # 預覽（可選）
            with st.expander("匯出預覽（前 50 筆）", expanded=False):
                st.dataframe(exp_df.head(50), use_container_width=True)

            if st.button("產生 PDF", type="primary"):
                title = f"維修紀錄（{start_d.strftime('%Y-%m-%d')} ～ {end_d.strftime('%Y-%m-%d')}）"
                pdf_bytes = make_pdf_bytes(title, exp_df)

                filename = f"維修紀錄_{start_d.strftime('%Y%m%d')}-{end_d.strftime('%Y%m%d')}.pdf"
                st.download_button(
                    label="下載 PDF",
                    data=pdf_bytes,
                    file_name=filename,
                    mime="application/pdf",
                )

        st.divider()

    # ---- 分頁（固定 10 筆）----
    PAGE_SIZE = 10
    total = len(df)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    page = st.number_input("頁碼", 1, pages, 1)
    start, end = (page - 1) * PAGE_SIZE, page * PAGE_SIZE
    page_df = df.iloc[start:end]

    st.caption(f"共 {total} 筆，顯示第 {start+1}–{min(end, total)} 筆（第 {page}/{pages} 頁）")

    # ---- 顯示 ----
    for i, row in enumerate(page_df.to_dict("records")):
        icon = status_icon(row.get("處理進度", ""))
        last_update = norm(row.get("維修更新時間", ""))
        update_tag = f"｜維修更新：{last_update}" if last_update else "｜維修更新：—"

        title = (
            f'{row.get("報修日期","")}｜{row.get("班級地點","")}｜{row.get("損壞設備","")}'
            f'｜{icon} {row.get("處理進度","")}{update_tag}'
        ).strip()

        case_id = norm(row.get("案件編號", ""))
        form_key = f"f_{safe_key(case_id)}_{page}_{i}"

        with st.expander(title):
            st.markdown(f"**損壞情形**：{row.get('損壞情形描述','')}")

            links = split_links(row.get("照片或影片", ""))
            if links:
                st.markdown("**照片 / 影片（點連結查看）**")
                for j, url in enumerate(links, 1):
                    st.markdown(f"- [{media_label(url,j)}]({url})")

            st.divider()

            if last_update:
                st.caption(f"維修更新時間（完整）：{last_update}")
            else:
                st.caption("維修更新時間（完整）：（尚無維修紀錄）")

            if not authed:
                st.markdown(f"**處理進度**：{row.get('處理進度','')}")
                st.markdown(f"**維修說明**：{row.get('維修說明','')}")
                continue

            with st.form(form_key):
                options = ["", "待觀查", "處理中", "待料", "送修", "已完成", "退回/無法處理"]
                cur = row.get("處理進度", "") if row.get("處理進度", "") in options else ""
                status = st.selectbox("處理進度", options, index=options.index(cur))
                note = st.text_area("維修說明", row.get("維修說明", ""))

                st.caption(f"本次儲存時間：{now_ts_full()}")

                if st.form_submit_button("儲存"):
                    save_repair(case_id, status, note)
                    st.success("已儲存")
                    st.cache_data.clear()
                    st.rerun()


if __name__ == "__main__":
    main()

